"""Normal exact-version reading and durable cursor recovery for A-share data."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.market_custody import market_read_scope, restore_market_query
from decision_kernel.application.market_collection_custody import (
    CHECKPOINT_SCHEMA, COLLECTION_SCHEMA, _checkpoint_manifest, _collection_manifest, checkpoint_source_id,
    collection_source_id, collection_source_prefix, restore_market_collection,
)
from decision_kernel.application.market_collection import (
    EXCHANGES, _partition_complete, _qualified, _reason, unit_request, validate_collection,
)
from decision_kernel.application.market_collection_inputs import read_ashare_collection
from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, digest, encoded, require
from decision_kernel.observations.market_daily import MarketInputError

MAX_CURSOR_VERSIONS = 1024
MAX_CURSOR_BYTES = 128 * 1024 * 1024


def same_reference(left, right):
    if left is None or right is None:
        return left is right
    return (left if isinstance(left, SourceRef) else SourceRef.from_dict(left)) == (
        right if isinstance(right, SourceRef) else SourceRef.from_dict(right))


def latest_market_reference(store, registry, *, collection_id=None, generation=None, checkpoint_id=None):
    """Find one explicit owner's immutable chain tip, never sort by a timestamp.

    The bounded scan pins a store snapshot. Concurrent or orphaned branches are
    ambiguous, so no caller receives a guessed latest version or fresh allowance.
    The returned SourceRef uses the original manifest commit, not the scan head.
    """
    require((checkpoint_id is None) != (collection_id is None), "ASHARE_CURSOR_OWNER_REQUIRED")
    source_id = (checkpoint_source_id(checkpoint_id) if checkpoint_id is not None else
                 collection_source_prefix(collection_id) if generation is None else
                 collection_source_id(collection_id, generation))
    read_manifest = _checkpoint_manifest if checkpoint_id is not None else _collection_manifest
    prefix = "source-views/" + source_id + ("" if checkpoint_id is None and generation is None else "/")
    with market_read_scope(store) as reader:
        snapshot = reader.backend.head()
        paths = [p for p in reader.backend.paths(snapshot, "source-views/")
                 if p.startswith(prefix) and p.endswith("/manifest.json")]
        require(len(paths) <= MAX_CURSOR_VERSIONS, "ASHARE_CURSOR_VERSION_BOUND")
        if not paths:
            return None
        parents, refs, total = {}, {}, 0
        for path in paths:
            ref = SourceRef(reader.backend.reference(snapshot, path))
            _, body = read_manifest(ref, registry, reader)
            require(body["checkpoint_id"] == checkpoint_id if checkpoint_id is not None else
                    body["collection"]["plan"]["collection_id"] == collection_id,
                    "ASHARE_CURSOR_OWNER_CHANGED")
            total += len(encoded(body))
            require(total <= MAX_CURSOR_BYTES, "ASHARE_CURSOR_BYTE_BOUND")
            parent = None if body["predecessor"] is None else SourceRef.from_dict(body["predecessor"])
            require(parent is None or parent.manifest.repository == reader.backend.repository,
                    "ASHARE_CURSOR_FOREIGN_PREDECESSOR")
            if parent is not None:
                # Authenticate the exact parent bytes and selection, including
                # an older generation at the start of a collection chain.
                _, parent_body = read_manifest(parent, registry, reader)
                if checkpoint_id is not None:
                    require(parent_body["checkpoint_id"] == checkpoint_id, "ASHARE_CURSOR_OWNER_CHANGED")
                else:
                    require(parent_body["collection"]["plan"]["collection_id"] == collection_id,
                            "ASHARE_CURSOR_OWNER_CHANGED")
            parents[path] = None if parent is None else parent.manifest.path
            refs[path] = ref
        tips = set(paths) - {p for p in parents.values() if p is not None}
        require(len(tips) == 1, "ASHARE_CURSOR_AMBIGUOUS")
        tip = next(iter(tips))
        seen, cursor = set(), tip
        while cursor in parents:
            require(cursor not in seen, "ASHARE_CURSOR_CYCLE")
            seen.add(cursor)
            cursor = parents[cursor]
        require(len(seen) == len(paths), "ASHARE_CURSOR_AMBIGUOUS")
        original = reader.backend.last_change(snapshot, tip)
        result = asdict(SourceRef(reader.backend.reference(original, tip)))
        read_manifest(result, registry, reader)
        return result


def require_checkpoint_descendant(candidate, known, registry, store):
    """A resolver may advance a known checkpoint, but cannot roll it back."""
    if known is None:
        return
    require(candidate is not None, "ASHARE_CHECKPOINT_ROLLBACK")
    known = asdict(known) if isinstance(known, SourceRef) else known
    candidate = asdict(candidate) if isinstance(candidate, SourceRef) else candidate
    with market_read_scope(store) as reader:
        cursor, seen = candidate, set()
        for _ in range(MAX_CURSOR_VERSIONS):
            if same_reference(cursor, known):
                return
            identity = encoded(cursor)
            require(identity not in seen, "ASHARE_CHECKPOINT_CHAIN_CYCLE")
            seen.add(identity)
            _, body = _checkpoint_manifest(cursor, registry, reader)
            cursor = body["predecessor"]
            require(cursor is not None, "ASHARE_CHECKPOINT_ROLLBACK")
        raise RecordError("ASHARE_CHECKPOINT_CHAIN_BOUND")


def read_collection_version(reference, registry, store, *, offset=0, limit=50,
                            entity_id=None, read_as_of=None, prefer_qualified=True):
    """Serve a pinned version and expose the latest attempt separately.

    A newer partial attempt can keep the previous complete partition version
    available. Its raw/qfq values retain the original source identities; a new
    exact calendar may establish that the served version is stale.
    """
    require(type(prefer_qualified) is bool, "ASHARE_READING_SELECTION")
    with market_read_scope(store) as reader:
        latest = restore_market_collection(reference, registry, reader)
        collection = validate_collection(latest["collection"], registry)
        selected = latest
        serving = "current-complete" if _partition_complete(collection) else "current-partial"
        if prefer_qualified and not _partition_complete(collection) and collection["last_qualified"] is not None:
            selected = restore_market_collection(collection["last_qualified"], registry, reader)
            validate_collection(selected["collection"], registry)
            require(selected["collection"]["plan"]["collection_id"] == collection["plan"]["collection_id"]
                    and _partition_complete(selected["collection"]), "ASHARE_PREVIOUS_VERSION_UNQUALIFIED")
            serving = "previous-complete"
        chosen, outcomes = selected["collection"], {}
        p = chosen["plan"]

        def load(slot, *, owner=chosen, prefix=""):
            unit = owner["units"].get(slot)
            key = prefix + slot
            if unit is None or unit["status"] != "complete":
                outcomes[key] = {"status": "not-retained", "reference": None,
                    "reason_code": None if unit is None else unit["reason_code"]}
                return None
            try:
                result = restore_market_query(unit["query"], registry, reader)["result"]
                use, _ = unit_request(owner["plan"], slot, registry)
                _qualified(result, use, owner["plan"], registry)
            except StoreUnavailable:
                # Lost authorization, a shared transport bound or an unavailable
                # native snapshot stops this entire read; it is not a row gap.
                raise
            except (RecordError, MarketInputError) as exc:
                outcomes[key] = {"status": "unavailable", "reference": deepcopy(unit["query"]),
                    "reason_code": _reason(exc)}
                return None
            outcomes[key] = {"status": "restored", "reference": deepcopy(unit["query"]), "reason_code": None}
            return result

        calendars = {v: load("calendar:" + v) for v in EXCHANGES}
        masters = {v: load("master:" + v) for v in EXCHANGES}
        freshness = None
        if serving == "previous-complete":
            freshness = {v: load("calendar:" + v, owner=collection, prefix="latest:") for v in EXCHANGES}
        view = read_ashare_collection(calendar_results=calendars, master_results=masters,
            load_partition=lambda day, api: load(day + ":" + api), registry=registry,
            start=p["start"], end=p["end"], calendar_end=p["calendar_end"],
            read_as_of=read_as_of, freshness_results=freshness,
            reference_policy="isolate-identified", offset=offset, limit=limit, entity_id=entity_id)
        failed = sum(v["status"] != "restored" for k, v in outcomes.items() if not k.startswith("latest:"))
        return {"status": "partial-restoration" if failed else "restored", "reference": latest["reference"],
            "selected_reference": selected["reference"], "serving": serving,
            "latest_attempt": deepcopy(collection["attempt"]),
            "latest_requested_end": collection["plan"]["end"], "selected_requested_end": p["end"],
            "query_count": len([k for k in outcomes if not k.startswith("latest:")]),
            "unavailable_queries": failed, "sources": outcomes, "view": view, "reading_error": None,
            "source_requests": 0, "original_http_restored": False, "execution_diagnostics": "separate-checkpoints",
            "qualification": "exact-stored-day-partitions-with-explicit-freshness-not-live-health-certification"}
