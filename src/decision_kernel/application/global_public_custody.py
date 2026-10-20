"""Standard-source custody for fixed, supplied public global originals.

The existing store owns CAS, permission checks and submission reconciliation.
The existing market scope owns the pinned snapshot and operation budget. This
module provides no transport, acquisition or alternative storage authority.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import re

from decision_kernel.application.global_public_inputs import (
    QUALIFICATION, capture_identity, public_view, read_global_public_payload, validate_public_input,
)
from decision_kernel.application.market_custody import (
    _document, _put, _read_source, authorize_market_custody, fresh_market_reader, market_read_scope,
)
from decision_kernel.application.market_reading_scope import SourceReads, operation_failure
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, digest, encoded, require
from decision_kernel.observations.global_public import series_info

PUBLIC_CUSTODY_SCHEMA = "global-public-custody-v1"
PUBLIC_ROOT_PREFIX = "source-views/market-global-"
LIMITATIONS = (
    "Supplied original representation; source execution and rights are not self-authenticated.",
    "Original capture/date qualification is retained; no current freshness or natural-update proof.",
    "Source bodies are not authenticated historical-as-known vintages or investment authority.",
)


def _body_source(payload, record, text):
    identity = {"capture_sha256": capture_identity(payload), "record": {
        key: value for key, value in record.items() if key != "body"}}
    # SourceInput checks the entire encoded envelope against the original 2 MiB
    # limit. A raw-body size allowance is never a larger storage allowance.
    return SourceInput(source_id="global-original-" + digest(encoded(identity))[:32], kind="content",
        title="Supplied " + payload["family"] + " original " + record["spec"]["id"],
        origin=record["spec"]["url"], text=text, custody="provided",
        acquired_at=record["received_at"], limitations=LIMITATIONS)


def _root_source(content):
    raw = encoded(content)
    return SourceInput(source_id="market-global-" + digest(raw)[:32], kind="content",
        title="Retained supplied public global reading",
        origin="https://github.com/auguspp/Decision-OS-2", text=raw.decode("utf-8"), custody="provided",
        acquired_at=content["reading"]["finished_at"], limitations=LIMITATIONS)


def _prepared(payload):
    original = read_global_public_payload(payload)
    sources = {record["index"]: _body_source(payload, record, record["body"])
               for record in payload["records"] if record["state"] == "HTTP_RESPONSE"}
    return original, sources


def public_reading_projection(payload):
    """Strict typed input only, before a host publishes any authorized input."""
    _prepared(payload)
    return deepcopy(payload)


def retain_public_reading(payload, store, *, fresh_reader, custody_authorized=False,
                          public_rights_confirmed=False):
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    original, prepared = _prepared(payload)  # All inline originals preflight before any write.
    reader = fresh_market_reader(store, fresh_reader)
    recipe, sources = deepcopy(payload), []
    for record in recipe["records"]:
        index = record["index"]
        if index not in prepared:
            continue
        source = prepared[index]
        reference = _put(store, source)  # UnknownWrite/Conflict propagate; no retry or cleanup.
        with market_read_scope(reader) as scoped:
            _, restored = _read_source(scoped, reference)
        require({**restored["manifest"]["input"], "text": restored["data"]}
                == json.loads(encoded(asdict(source))), "GP_ORIGINAL_CHANGED")
        record["body"] = {"source": index}
        sources.append({"index": index, "reference": asdict(reference),
                        "source_sha256": digest(encoded(asdict(source)))})
    content = {"schema": PUBLIC_CUSTODY_SCHEMA, "reading": recipe, "sources": sources,
               "view_sha256": digest(encoded(original))}
    reference = _put(store, _root_source(content))
    final_reader = fresh_market_reader(store, fresh_reader)
    require(final_reader is not reader and final_reader.backend is not reader.backend,
            "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    restored = read_public_reading(reference, final_reader)
    require(restored["status"] == "restored" and encoded(restored["view"]) == encoded(original),
            "MARKET_CUSTODY_READING_CHANGED")
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    return {"status": "stored-and-independently-restored", "reference": asdict(reference),
            "query_count": len(sources), "page_count": len(sources), "restored": restored}


def read_public_root(store, reference):
    ref, view = _read_source(store, reference)
    content = _document(view["data"].encode("utf-8"))
    require(isinstance(content, dict) and set(content) == {"schema", "reading", "sources", "view_sha256"}
            and content["schema"] == PUBLIC_CUSTODY_SCHEMA, "GP_CUSTODY_SCHEMA")
    validate_public_input(content["reading"], retained=True)
    require({**view["manifest"]["input"], "text": view["data"]}
            == json.loads(encoded(asdict(_root_source(content)))), "GP_CUSTODY_ROOT_SOURCE")
    require(isinstance(content["view_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", content["view_sha256"]), "GP_CUSTODY_VIEW_IDENTITY")
    expected = [r["index"] for r in content["reading"]["records"] if r["state"] == "HTTP_RESPONSE"]
    require(isinstance(content["sources"], list) and len(content["sources"]) == len(expected), "GP_CUSTODY_SOURCES")
    for index, source in zip(expected, content["sources"], strict=True):
        require(isinstance(source, dict) and set(source) == {"index", "reference", "source_sha256"}
                and type(source["index"]) is int and source["index"] == index
                and isinstance(source["source_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", source["source_sha256"]), "GP_CUSTODY_SOURCE_SLOT")
        original = SourceRef.from_dict(source["reference"])
        require(original.manifest.repository == ref.manifest.repository, "FOREIGN_REPOSITORY")
        require(original.selection == ref.selection, "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    return ref, content


def read_public_reading(reference, store, *, offset=0, limit=50, entity_id=None):
    """Restore a fixed root and all its originals before any navigation projection."""
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "PAGE_INVALID")
    require(entity_id is None or isinstance(entity_id, str), "GP_ENTITY_SCOPE")
    with market_read_scope(store) as scoped:
        backend = SourceReads(scoped.backend)
        try:
            reader = VersionedStore(backend)
            ref, content = read_public_root(reader, reference)
            recipe, bodies, outcomes, unavailable = content["reading"], {}, [], []
            require(entity_id is None or entity_id in series_info(recipe["family"]), "GP_ENTITY_SCOPE")
            for source in content["sources"]:
                index, error = source["index"], None
                record = recipe["records"][index]
                try:
                    _, view = _read_source(reader, source["reference"])
                    raw = view["data"].encode("utf-8")
                    require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], "GP_RAW_IDENTITY")
                    expected = _body_source(recipe, record, view["data"])
                    require(digest(encoded(asdict(expected))) == source["source_sha256"]
                            and {**view["manifest"]["input"], "text": view["data"]}
                            == json.loads(encoded(asdict(expected))), "GP_ORIGINAL_CHANGED")
                    bodies[index] = raw
                except RecordError as exc:
                    if operation_failure(exc):
                        raise
                    unavailable.append(index)
                    code = str(exc)
                    error = code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "GP_ORIGINAL_UNAVAILABLE"
                backend.check()
                outcomes.append({"path": "records/" + str(index), "reference": deepcopy(source["reference"]),
                                 "status": "restored" if error is None else "unavailable", "reason_code": error})
            # Always compute and bind the full original denominator first. An
            # unavailable original is an explicit gap, not an empty market.
            full = public_view(recipe, bodies, unavailable=unavailable)
            if not unavailable:
                require(digest(encoded(full)) == content["view_sha256"], "GP_CUSTODY_VIEW_CHANGED")
            view = public_view(recipe, bodies, unavailable=unavailable, offset=offset, limit=limit, entity_id=entity_id)
            result = {"status": "partial-restoration" if unavailable else "restored", "reference": asdict(ref),
                      "query_count": len(outcomes), "unavailable_queries": len(unavailable),
                      "sources": outcomes, "view": view, "reading_error": None,
                      "original_http_restored": False, "original_bodies_restored": not unavailable,
                      "execution_diagnostics": "supplied-receipt-not-authenticated-execution",
                      "source_requests": 0, "qualification": QUALIFICATION}
            backend.check()
        finally:
            backend.active = False
    return result
