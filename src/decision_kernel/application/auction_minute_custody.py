"""Supplied minute originals through the existing SourceInput/SourceRef store.

The original journal, authorization, fresh reader and pinned scope own their
existing responsibilities. This module has no source transport or new budget.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import re

from decision_kernel.application.auction_inputs import AUCTION_READING_SCHEMA
from decision_kernel.application.auction_minute_inputs import (
    QUALIFICATION, capture_identity, minute_view, qualify_plan, validate_input,
)
from decision_kernel.application.market_custody import (
    READING_SCHEMA, _document, _put, _read_manifest, _read_source,
    authorize_market_custody, fresh_market_reader, market_read_scope, read_stored_market,
)
from decision_kernel.application.market_reading_scope import SourceReads, operation_failure
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MAX_BYTES, BlobRef, RecordError, Selection, SourceInput, SourceRef, digest, encoded, require,
)
from decision_kernel.observations.auction_minute import ENDPOINT
from decision_kernel.observations.market_daily import MarketInputError

CUSTODY_SCHEMA = "ftshare-auction-minute-custody-v1"
ROOT_PREFIX = "source-views/market-auction-minute-"
LIMITATIONS = (
    "Supplied original UTF-8 representation and receipt; no authenticated HTTP execution or source rights.",
    "SDK raw and decoded/reprinted JSON do not prove original wire bytes.",
    "Historical exact-minute observation; no contemporaneous discovery, full path or cross-source return.",
)


def _body_source(payload, record, body):
    identity = {"capture_sha256": capture_identity(payload), "record": {
        key: value for key, value in record.items() if key != "body"}}
    return SourceInput(source_id="auction-minute-original-" + digest(encoded(identity))[:32],
        kind="content", title="Supplied FTShare minute original " + str(record["index"]),
        origin=ENDPOINT, text=body, custody="provided", acquired_at=record["received_at"], limitations=LIMITATIONS)


def _root_source(content):
    raw = encoded(content)
    return SourceInput(source_id="market-auction-minute-" + digest(raw)[:32], kind="content",
        title="Retained original auction cohort minute observation", origin="https://github.com/auguspp/Decision-OS-2",
        text=raw.decode("utf-8"), custody="provided", acquired_at=content["reading"]["finished_at"], limitations=LIMITATIONS)


def _prepared(payload):
    validate_input(payload)
    sources = {record["index"]: _body_source(payload, record, record["body"])
               for record in payload["records"] if record["state"] == "HTTP_RESPONSE"}
    require(len(encoded([asdict(source) for source in sources.values()])) <= MAX_BYTES, "MINUTE_CUSTODY_INPUT_BOUND")
    return sources


def minute_reading_projection(payload):
    """Typed byte/receipt preflight before a host saves authorized inputs.

    This cannot qualify the seed without the original backend. Save resolves
    that exact root and checks the whole cohort before any source write.
    """
    _prepared(payload)
    return deepcopy(payload)


def _seed(store, reference, registry, backend):
    ref, root = _read_manifest(store, reference, READING_SCHEMA)
    require(isinstance(root.get("reading"), dict) and root["reading"].get("schema") == AUCTION_READING_SCHEMA,
            "MINUTE_AUCTION_ROOT_REQUIRED")
    # The original market reader can localize a nested RecordError. On both
    # return and unwind, give the same remembered operation failure precedence.
    try:
        restored = read_stored_market(ref, registry, store, offset=0, limit=100)
    finally:
        backend.check()
    return restored


def _content(payload, plan, original, references, prepared):
    recipe, sources = deepcopy(payload), []
    for record in recipe["records"]:
        index = record["index"]
        if index not in prepared:
            continue
        record["body"] = {"source": index}
        sources.append({"index": index, "reference": references[index],
                        "source_sha256": digest(encoded(asdict(prepared[index])))})
    return {"schema": CUSTODY_SCHEMA, "reading": recipe, "plan": deepcopy(plan),
            "sources": sources, "view_sha256": digest(encoded(original))}


def retain_minute_reading(payload, registry, store, *, fresh_reader, custody_authorized=False,
                          public_rights_confirmed=False):
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    prepared = _prepared(payload)  # Every original's full SourceInput bound, before writes.
    reader = fresh_market_reader(store, fresh_reader)
    seed_ref = SourceRef.from_dict(payload["auction_reference"])
    require(seed_ref.manifest.repository == store.backend.repository, "FOREIGN_REPOSITORY")
    with market_read_scope(reader) as scoped:
        backend = SourceReads(scoped.backend)
        try:
            restored_seed = _seed(VersionedStore(backend), seed_ref, registry, backend)
            original = minute_view(payload, restored_seed["view"])
            plan = qualify_plan(payload, restored_seed["view"])
            backend.check()
        finally:
            backend.active = False
    # Reserve the largest legal SourceRef path for every original. This only
    # checks capacity; no predicted reference is stored or used for reading.
    placeholder = asdict(SourceRef(BlobRef(store.backend.repository, "0" * 40,
        "source-views/" + "x" * 96 + "/" + "0" * 64 + "/manifest.json", "0" * 64)))
    bound_root = _root_source(_content(payload, plan, original,
        {index: placeholder for index in prepared}, prepared))
    require(len(encoded([*[asdict(s) for s in prepared.values()], asdict(bound_root)])) <= MAX_BYTES,
            "MINUTE_CUSTODY_TOTAL_BOUND")
    references = {}
    for index, source in prepared.items():
        reference = _put(store, source)  # Existing CAS/replay owns unknown writes.
        references[index] = asdict(reference)
        with market_read_scope(reader) as scoped:
            _, saved = _read_source(scoped, reference)
        require({**saved["manifest"]["input"], "text": saved["data"]}
                == json.loads(encoded(asdict(source))), "MINUTE_ORIGINAL_CHANGED")
    reference = _put(store, _root_source(_content(payload, plan, original, references, prepared)))
    final = fresh_market_reader(store, fresh_reader)
    require(final is not reader and final.backend is not reader.backend, "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    restored = read_minute_reading(reference, registry, final)
    require(restored["status"] == "restored" and encoded(restored["view"]) == encoded(original),
            "MARKET_CUSTODY_READING_CHANGED")
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    return {"status": "stored-and-independently-restored", "reference": asdict(reference),
            "query_count": len(references) + 1, "page_count": len(references), "restored": restored}


def read_minute_root(store, reference):
    ref, view = _read_source(store, reference)
    content = _document(view["data"].encode("utf-8"))
    require(isinstance(content, dict) and set(content) == {"schema", "reading", "plan", "sources", "view_sha256"}
            and content["schema"] == CUSTODY_SCHEMA, "MINUTE_CUSTODY_SCHEMA")
    validate_input(content["reading"], retained=True)
    root_source = _root_source(content)
    require({**view["manifest"]["input"], "text": view["data"]}
            == json.loads(encoded(asdict(root_source))), "MINUTE_CUSTODY_ROOT_SOURCE")
    # Even the lower bound must fit before following any dependency. Actual
    # complete source envelopes are accumulated below, not just their bodies.
    require(len(encoded(asdict(root_source))) + sum(r["bytes"] for r in content["reading"]["records"]
            if r["state"] == "HTTP_RESPONSE") <= MAX_BYTES, "MINUTE_CUSTODY_TOTAL_BOUND")
    require(isinstance(content["view_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", content["view_sha256"]),
            "MINUTE_CUSTODY_VIEW_IDENTITY")
    seed = SourceRef.from_dict(content["reading"]["auction_reference"])
    require(seed.manifest.repository == ref.manifest.repository, "FOREIGN_REPOSITORY")
    expected = [r["index"] for r in content["reading"]["records"] if r["state"] == "HTTP_RESPONSE"]
    require(isinstance(content["sources"], list) and len(content["sources"]) == len(expected), "MINUTE_CUSTODY_SOURCES")
    for index, entry in zip(expected, content["sources"], strict=True):
        require(isinstance(entry, dict) and set(entry) == {"index", "reference", "source_sha256"}
                and type(entry["index"]) is int and entry["index"] == index
                and isinstance(entry["source_sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", entry["source_sha256"]),
                "MINUTE_CUSTODY_SOURCE_SLOT")
        source = SourceRef.from_dict(entry["reference"])
        require(source.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
        require(source.manifest.repository == ref.manifest.repository, "FOREIGN_REPOSITORY")
    return ref, content


def _reason(error):
    value = str(error)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else "MINUTE_ORIGINAL_UNAVAILABLE"


def read_minute_reading(reference, registry, store, *, offset=0, limit=50, entity_id=None):
    with market_read_scope(store) as scoped:
        backend = SourceReads(scoped.backend)
        try:
            reader = VersionedStore(backend)
            ref, content = read_minute_root(reader, reference)
            recipe, bodies, outcomes, unavailable = content["reading"], {}, [], []
            original_bytes = len(encoded(asdict(_root_source(content))))
            seed_ref = recipe["auction_reference"]
            try:
                seed = _seed(reader, seed_ref, registry, backend)["view"]
                require(encoded(qualify_plan(recipe, seed)) == encoded(content["plan"]), "MINUTE_COHORT_CHANGED")
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                if operation_failure(error):
                    raise
                # No guessed denominator or cached cohort substitutes for the
                # exact original seed when its own dependencies cannot restore.
                backend.check()
                result = {"status": "partial-restoration", "reference": asdict(ref), "query_count": 1,
                    "unavailable_queries": 1, "sources": [{"path": "auction", "reference": deepcopy(seed_ref),
                        "status": "unavailable", "reason_code": _reason(error)}],
                    "view": None, "reading_error": _reason(error), "source_requests": 0,
                    "original_http_restored": False, "original_bodies_restored": False,
                    "execution_diagnostics": "supplied-receipt-not-authenticated-execution", "qualification": QUALIFICATION}
            else:
                outcomes.append({"path": "auction", "reference": deepcopy(seed_ref), "status": "restored", "reason_code": None})
                for entry in content["sources"]:
                    index, error = entry["index"], None
                    record = recipe["records"][index]
                    try:
                        _, saved = _read_source(reader, entry["reference"])
                        raw = saved["data"].encode("utf-8")
                        require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], "MINUTE_ORIGINAL_CHANGED")
                        expected = _body_source(recipe, record, saved["data"])
                        require(digest(encoded(asdict(expected))) == entry["source_sha256"]
                                and {**saved["manifest"]["input"], "text": saved["data"]}
                                == json.loads(encoded(asdict(expected))), "MINUTE_ORIGINAL_CHANGED")
                    except RecordError as exc:
                        if operation_failure(exc):
                            raise
                        unavailable.append(index)
                        error = _reason(exc)
                    else:
                        original_bytes += len(encoded(asdict(expected)))
                        require(original_bytes <= MAX_BYTES, "MINUTE_CUSTODY_TOTAL_BOUND")
                        bodies[index] = raw
                    backend.check()
                    outcomes.append({"path": "records/" + str(index), "reference": deepcopy(entry["reference"]),
                                     "status": "restored" if error is None else "unavailable", "reason_code": error})
                full = minute_view(recipe, seed, retained=True, bodies=bodies, unavailable=unavailable)
                if not unavailable:
                    require(digest(encoded(full)) == content["view_sha256"], "MINUTE_CUSTODY_VIEW_CHANGED")
                view = minute_view(recipe, seed, retained=True, bodies=bodies, unavailable=unavailable,
                                   offset=offset, limit=limit, entity_id=entity_id)
                result = {"status": "partial-restoration" if unavailable else "restored", "reference": asdict(ref),
                    "query_count": len(outcomes), "unavailable_queries": len(unavailable), "sources": outcomes,
                    "capture_plan": deepcopy(content["plan"]), "provided_receipt": deepcopy(recipe),
                    "view": view, "reading_error": None, "source_requests": 0, "original_http_restored": False,
                    "original_bodies_restored": not unavailable,
                    "execution_diagnostics": "supplied-receipt-not-authenticated-execution", "qualification": QUALIFICATION}
            backend.check()
        finally:
            backend.active = False
    return result
