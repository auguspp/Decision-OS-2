"""Market capture pages -> existing SourceRefs -> independent, source-free reading.

The retained representation is the existing requested-column excerpt, NOT wire
bytes or execution diagnostics. No Relay calls, source store, or retry loop here.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import re

from decision_kernel.application.global_index_inputs import (
    INDEX_FIELDS, US_CALENDAR_FIELDS, US_INDEX_READING_SCHEMA, US_INDEX_REQUIRED,
)
from decision_kernel.application.market_group_inputs import (
    GROUP_REQUIRED, STOCK_REQUIRED,
)
from decision_kernel.application.market_reading import _route, read_market_payload
from decision_kernel.application.market_relay_inputs import _view
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_sources import CAPTURE, source_inputs
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MAX_BYTES, RecordError, Selection, SourceInput, SourceRef, digest, encoded, require,
)

from decision_kernel.application.shibor_inputs import SHIBOR_FIELDS, SHIBOR_READING_SCHEMA, SHIBOR_REQUIRED

from decision_kernel.application.market_session_inputs import SESSION_SCHEMA, SESSION_REQUIRED

from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS

QUERY_SCHEMA = "market-query-custody-v1"
READING_SCHEMA = "market-reading-custody-v1"
MAX_QUERIES = 32
MAX_PAGES = 100
MARKET_FIELDS = {
    "suspend_d": SUSPENSION_FIELDS,
    "shibor": SHIBOR_FIELDS,
    "us_tradecal": US_CALENDAR_FIELDS,
    "index_global": INDEX_FIELDS,
    "trade_cal": {"exchange", "cal_date", "is_open"},
    "daily": {"ts_code", "trade_date", "close"},
    "adj_factor": {"ts_code", "trade_date", "adj_factor"},
    "stock_basic": {"ts_code", "symbol", "name", "exchange", "curr_type", "list_status", "list_date", "delist_date"},
    "ths_index": {"ts_code", "name", "count", "exchange", "list_date", "type"},
    "ths_member": {"ts_code", "con_code", "con_name"},
    "ths_daily": {"ts_code", "trade_date", "close"},
}
ORIGIN = "https://github.com/auguspp/Decision-OS-2"
LIMITS = ("GENERATED_READING_MANIFEST_NOT_ORIGINAL_HTTP_OR_EXECUTION_DIAGNOSTICS",
          "EXACT_SOURCE_EXCERPTS_NOT_PUBLISHER_AUTHENTICATION_OR_HISTORICAL_PIT",
          "NO_COLLECTION_RETRY_OR_LATEST_SUBSTITUTION; SOURCE_RIGHTS_CHECKED_BY_CALLER")



def _map_results(payload, transform):
    """Visit only existing, explicit result slots, never recursive arbitrary data."""
    _route(payload)
    output = deepcopy(payload)
    def one(value, kind, prefix):
        required = (STOCK_REQUIRED if kind == "stock" else GROUP_REQUIRED)
        if kind == "session":
            required = SESSION_REQUIRED
        if kind == "shibor":
            required = SHIBOR_REQUIRED
        if kind == "us-index":
            required = US_INDEX_REQUIRED
        if kind == "raw":
            required = {"calendar_result", "stock_results", "exchange", "start", "end", "declared_entities", "coverage"}
        extras = {"horizons"} | ({"benchmark"} if kind in ("sector", "concept") else set())
        if kind == "stock":
            extras |= {"suspension_results", "factor_supplements"}
        if kind == "session":
            extras |= {"suspension_result"}
        if kind in ("stock", "sector", "concept", "session"):
            extras |= {"reference_policy"}
        require(isinstance(value, dict) and required <= value.keys() <= required | extras, "MARKET_CUSTODY_SURFACE")
        for field in ("calendar_result", "catalog_result", "rate_result", "suspension_result"):
            if field in value:
                value[field] = transform(value[field], prefix + [field])
        for field in ("stock_results", "master_results", "factor_results", "member_results", "index_results", "calendar_results", "session_results", "suspension_results"):
            if field not in value:
                continue
            mapping = value[field]
            require(isinstance(mapping, dict) and all(isinstance(k, str) and len(k) <= 128 for k in mapping), "MARKET_CUSTODY_RESULT_MAP")
            value[field] = {key: transform(result, prefix + [field, key]) for key, result in mapping.items()}
        if "factor_supplements" in value:
            mapping = value["factor_supplements"]
            require(isinstance(mapping, dict) and all(isinstance(k, str) and len(k) <= 128
                    for k in mapping), "MARKET_CUSTODY_RESULT_MAP")
            for code, points in mapping.items():
                require(isinstance(points, dict) and len(points) <= 16 and all(
                    isinstance(d, str) and len(d) <= 128 for d in points), "MARKET_CUSTODY_RESULT_MAP")
                mapping[code] = {day: transform(result, prefix + ["factor_supplements", code, day])
                                 for day, result in points.items()}
        if value.get("benchmark") is not None:
            baseline = value["benchmark"]
            require(isinstance(baseline, dict) and set(baseline) == {"ts_code", "result"}, "MARKET_CUSTODY_BENCHMARK")
            baseline["result"] = transform(baseline["result"], prefix + ["benchmark", "result"])
    if output["schema"] == "relay-three-market-reading-v1":
        surfaces = output["surfaces"]
        require(isinstance(surfaces, dict) and surfaces.keys() <= {"sector", "concept", "stock"}, "MARKET_CUSTODY_SURFACES")
        for kind, value in surfaces.items():
            one(value, kind, ["surfaces", kind])
    else:
        schema = output.pop("schema")
        kind = output.pop("kind") if schema == "relay-ths-group-reading-v1" else (
            "session" if schema == SESSION_SCHEMA else
            "shibor" if schema == SHIBOR_READING_SCHEMA else
            "us-index" if schema == US_INDEX_READING_SCHEMA else
            "raw" if schema == "relay-stock-reading-v1" else "stock")
        require(kind in ("raw", "stock", "sector", "concept", "us-index", "shibor", "session"), "MARKET_CUSTODY_KIND")
        one(output, kind, [])
        output["schema"] = schema
        if schema == "relay-ths-group-reading-v1":
            output["kind"] = kind
    return output


def _prepare_query(result, registry):
    require(isinstance(result, dict) and isinstance(result.get("use"), dict), "MARKET_CUSTODY_RESULT")
    use = result["use"]
    api, params = use.get("api"), use.get("parameters")
    require(isinstance(api, str) and api in MARKET_FIELDS and isinstance(params, dict), "MARKET_CUSTODY_API")
    scope = {k: v for k, v in params.items() if k not in ("fields", "limit", "offset")}
    view = _view(result, registry, api, scope, MARKET_FIELDS[api])
    require(0 < view["page_count"] <= MAX_PAGES and len(encoded(view)) <= MAX_BYTES, "MARKET_CUSTODY_QUERY_BOUND")
    sources = source_inputs(result, registry)  # The ONLY table/excerpt exporter.
    header = {"schema": QUERY_SCHEMA, "case_id": result["case_id"], "use": deepcopy(use),
              "snapshot": deepcopy(registry.snapshot), "view_sha256": digest(encoded(view))}
    return header, sources


def _manifest_source(schema, content):
    raw = encoded(content)
    return SourceInput(source_id="market-" + ("query-" if schema == QUERY_SCHEMA else "reading-") + digest(raw)[:32],
        kind="content", title="Retained market " + ("query" if schema == QUERY_SCHEMA else "reading"),
        origin=ORIGIN, text=raw.decode(), custody="provided", limitations=LIMITS)


def _put(store, source):
    # The existing submission journal owns CAS/replay. No write retry or new ledger.
    return store.put_source("market-custody-" + digest(encoded(asdict(source))), source)


def authorize_market_custody(store, *, custody_authorized=False, public_rights_confirmed=False):
    """Use the trusted backend binding, never a visibility flag in source data.

    Private custody is not a declaration that the source may be redistributed.
    The synthetic backend remains only an engineering fixture, not a new target.
    """
    require(isinstance(store, VersionedStore), "MARKET_CUSTODY_STORE_REQUIRED")
    require(type(custody_authorized) is bool and type(public_rights_confirmed) is bool,
            "MARKET_CUSTODY_AUTHORIZATION_INVALID")
    binding = getattr(store.backend, "binding", None)
    if binding is not None:
        # A missing authorization is rejected before any extra network/API
        # request. This preserves the original shared request budget and avoids
        # even probing remote visibility for an unauthorized source write.
        if not custody_authorized and not public_rights_confirmed:
            require(False, "MARKET_CUSTODY_AUTHORIZATION_REQUIRED"
                    if binding.visibility == "private" else "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED")
        live = getattr(store.backend, "live_visibility", None)
        visibility = live() if callable(live) else getattr(binding, "visibility", None)
        require(visibility in ("private", "public"), "MARKET_CUSTODY_BINDING_REQUIRED")
        if visibility == "public":
            require(public_rights_confirmed, "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED")
        else:
            require(custody_authorized or public_rights_confirmed, "MARKET_CUSTODY_AUTHORIZATION_REQUIRED")
        return visibility
    require(isinstance(store.backend.repository, str)
            and store.backend.repository.startswith("synthetic-git:"), "MARKET_CUSTODY_BINDING_REQUIRED")
    require(custody_authorized or public_rights_confirmed, "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED")
    return "synthetic"


def fresh_market_reader(store, factory):
    require(callable(factory), "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    reader = factory()
    require(isinstance(reader, VersionedStore) and reader is not store and reader.backend is not store.backend
            and reader.backend.repository == store.backend.repository, "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    api = getattr(store.backend, "api", None)
    if api is not None:
        require(getattr(reader.backend, "api", None) is api, "MARKET_CUSTODY_SHARED_OPERATION_BUDGET_REQUIRED")
        require(getattr(getattr(reader.backend, "binding", None), "write_enabled", None) is False,
                "MARKET_CUSTODY_FRESH_READ_ONLY_REQUIRED")
    return reader


@contextmanager
def market_read_scope(store):
    """Keep one exact native snapshot for a related group of source reads.

    Existing backends continue to use their normal exact-reference API. A
    trusted backend may provide a scoped reader that checks its live binding
    around the group; nested scopes keep the same pinned snapshot.
    """
    require(isinstance(store, VersionedStore), "MARKET_CUSTODY_STORE_REQUIRED")
    scope = getattr(store.backend, "market_read_scope", None)
    if scope is None:
        yield store
        return
    require(callable(scope), "MARKET_CUSTODY_READ_SCOPE_REQUIRED")
    with scope() as backend:
        require(getattr(backend, "repository", None) == store.backend.repository,
                "MARKET_CUSTODY_READ_SCOPE_REPOSITORY_CHANGED")
        yield VersionedStore(backend)


def _retain_prepared_query(header, sources, registry, store, reader, page_cache):
    page_refs = []
    for source in sources:
        identity = digest(encoded(asdict(source)))
        if identity not in page_cache:
            page_cache[identity] = _put(store, source)
        page_refs.append({"reference": asdict(page_cache[identity]), "source_sha256": identity})
    reference = _put(store, _manifest_source(QUERY_SCHEMA, {**header, "pages": page_refs}))
    return restore_market_query(reference, registry, reader)


def _read_source(store, reference):
    ref = reference if isinstance(reference, SourceRef) else SourceRef.from_dict(reference)
    require(ref.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    view = store.read_source(ref)
    require(view["availability"] == "stored-representation" and isinstance(view["data"], str), "MARKET_CUSTODY_BODY_REQUIRED")
    require(len(view["data"].encode()) <= MAX_BYTES, "MARKET_CUSTODY_SOURCE_BOUND")
    return ref, view


def _document(raw):
    try:
        return _json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("MARKET_CUSTODY_STORED_JSON_INVALID") from None


def _read_manifest(store, reference, schema):
    ref, view = _read_source(store, reference)
    content = _document(view["data"].encode())
    require(isinstance(content, dict) and content.get("schema") == schema, "MARKET_CUSTODY_SCHEMA")
    expected = _manifest_source(schema, content)
    require({**view["manifest"]["input"], "text": view["data"]} == json.loads(encoded(asdict(expected))), "MARKET_CUSTODY_MANIFEST_SOURCE")
    return ref, content


def _reading_envelope(data, pages):
    use = data["use"]
    return {"case_id": data["case_id"], "use": use, "status": "captured", "capture": {
        "schema": CAPTURE, "api": use["api"], "parameters": use.get("parameters"),
        "query_key": use.get("query_key"), "snapshot": data["snapshot"], "pages": pages,
        "complete": True, "error": None, "source_identity": "THIRD_PARTY_RELAY_NOT_ISSUER",
        "coverage": "REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS"}}


def market_reading_projection(payload, registry):
    """Remove unretained diagnostics before a public host writes any input artifact."""
    require(len(encoded(payload)) <= MAX_BYTES, "MARKET_CUSTODY_INPUT_BOUND")
    read_market_payload(payload, registry)
    def project(result, _path):
        header, sources = _prepare_query(result, registry)
        pages = [{**_document(s.text.encode())["page"], "attempts": []} for s in sources]
        return _reading_envelope(header, pages)
    return _map_results(payload, project)


def restore_market_query(reference, registry: RelayRegistry, store: VersionedStore):
    """Restore an inspected reading envelope, explicitly without original attempts."""
    with market_read_scope(store) as reader:
        return _restore_market_query(reference, registry, reader)


def _restore_market_query(reference, registry, store):
    ref, data = _read_manifest(store, reference, QUERY_SCHEMA)
    require(set(data) == {"schema", "case_id", "use", "snapshot", "view_sha256", "pages"}, "MARKET_CUSTODY_QUERY_MANIFEST")
    require(isinstance(registry, RelayRegistry) and data["snapshot"] == registry.snapshot, "MARKET_CUSTODY_REGISTRY_CHANGED")
    require(isinstance(data["pages"], list) and 0 < len(data["pages"]) <= MAX_PAGES, "MARKET_CUSTODY_PAGE_BOUND")
    use = data["use"]
    require(isinstance(use, dict) and isinstance(use.get("api"), str) and use["api"] in MARKET_FIELDS, "MARKET_CUSTODY_API")
    pages, originals, seen, total = [], [], set(), 0
    for entry in data["pages"]:
        require(isinstance(entry, dict) and set(entry) == {"reference", "source_sha256"}, "MARKET_CUSTODY_PAGE_REFERENCE")
        page_ref, stored = _read_source(store, entry["reference"])
        require(page_ref.manifest.repository == ref.manifest.repository, "MARKET_CUSTODY_FOREIGN_PAGE")
        identity = encoded(asdict(page_ref))
        require(identity not in seen, "MARKET_CUSTODY_DUPLICATE_PAGE")
        seen.add(identity)
        source = {**stored["manifest"]["input"], "text": stored["data"]}
        require(digest(encoded(source)) == entry["source_sha256"], "MARKET_CUSTODY_PAGE_CHANGED")
        total += len(stored["data"].encode())
        require(total <= 2 * MAX_BYTES, "MARKET_CUSTODY_QUERY_BOUND")
        body = _document(stored["data"].encode())
        require(isinstance(body, dict) and set(body) == {"schema", "api", "parameters", "page"}
                and body["schema"] == CAPTURE and body["api"] == use["api"]
                and body["parameters"] == use.get("parameters"), "MARKET_CUSTODY_PAGE_SCOPE")
        require(isinstance(body["page"], dict) and set(body["page"]) == {"offset", "captured_at", "table", "transport"}, "MARKET_CUSTODY_PAGE_SHAPE")
        pages.append({**body["page"], "attempts": []})
        originals.append(source)
    result = _reading_envelope(data, pages)
    header, expected_sources = _prepare_query(result, registry)
    require(header["view_sha256"] == data["view_sha256"], "MARKET_CUSTODY_VIEW_CHANGED")
    require([json.loads(encoded(asdict(s))) for s in expected_sources] == originals, "MARKET_CUSTODY_SOURCE_CHANGED")
    return {"result": result, "reference": asdict(ref), "page_references": [p["reference"] for p in data["pages"]],
            "representation": "restored-requested-column-reading-envelope", "execution_diagnostics": "not-retained",
            "original_http_restored": False}


def retain_market_query(result, registry, store, *, fresh_reader, custody_authorized=False,
                        public_rights_confirmed=False, checkpoint_reference=None):
    """Retain one complete query through the same page/query SourceRef chain.

    The result is a reading envelope, never an HTTP retry checkpoint. Each fresh
    remote reader uses the writer's existing cumulative operation budget. An
    independently restored complete checkpoint can supply its original page
    references, avoiding redundant replay writes for every accepted page.
    """
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    header, sources = _prepare_query(result, registry)
    reader = fresh_market_reader(store, fresh_reader)
    pages = {}
    if checkpoint_reference is not None:
        from decision_kernel.application.market_collection_custody import restore_market_checkpoint
        retained = restore_market_checkpoint(checkpoint_reference, registry, reader)
        state = retained["state"]
        require(state["control"]["state"] == "complete" and state["capture"]["complete"]
                and state["control"]["request"] == result["use"], "MARKET_CUSTODY_CHECKPOINT_MISMATCH")
        original = source_inputs({"case_id": retained["checkpoint_id"], "use": state["control"]["request"],
            "status": "captured", "capture": state["capture"]}, registry)
        require(encoded([asdict(source) for source in sources]) == encoded([asdict(source) for source in original])
                and len(sources) == len(retained["page_references"]), "MARKET_CUSTODY_CHECKPOINT_MISMATCH")
        pages = {digest(encoded(asdict(source))): SourceRef.from_dict(reference)
                 for source, reference in zip(sources, retained["page_references"])}
    restored = _retain_prepared_query(header, sources, registry, store, reader, pages)
    return {"status": "stored-and-independently-restored-query", "reference": restored["reference"],
            "page_count": len(restored["page_references"]), "restored": restored}


def retain_market_reading(payload, registry, store, *, fresh_reader, public_rights_confirmed=False):
    """Preflight all supplied captures before saving; publish root only after pages.

    Any failed/unknown write stops immediately. Reinvoke the identical payload to
    reconcile with the existing journal. Earlier saved sources are not deleted.
    """
    require(public_rights_confirmed is True, "MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED")
    require(isinstance(store, VersionedStore) and callable(fresh_reader), "MARKET_CUSTODY_STORE_REQUIRED")
    require(len(encoded(payload)) <= MAX_BYTES, "MARKET_CUSTODY_INPUT_BOUND")
    original = read_market_payload(payload, registry)
    if payload["schema"] == "relay-three-market-reading-v1":
        require(all(v["status"] in ("read", "not-supplied") for v in original["surfaces"].values()), "MARKET_CUSTODY_INVALID_SURFACE")
    prepared = {}
    def prepare(result, _path):
        header, sources = _prepare_query(result, registry)
        key = digest(encoded(header))
        prepared.setdefault(key, (header, sources))
        require(len(prepared) <= MAX_QUERIES, "MARKET_CUSTODY_QUERY_BOUND")
        return {"query": key}
    recipe = _map_results(payload, prepare)
    reader = fresh_reader()
    require(isinstance(reader, VersionedStore) and reader is not store and reader.backend is not store.backend
            and reader.backend.repository == store.backend.repository, "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    queries, page_cache = {}, {}
    for key, (header, sources) in prepared.items():
        restored = _retain_prepared_query(header, sources, registry, store, reader, page_cache)
        queries[key] = restored["reference"]
    content = {"schema": READING_SCHEMA, "reading": recipe, "queries": queries}
    reference = _put(store, _manifest_source(READING_SCHEMA, content))
    final_reader = fresh_reader()
    require(isinstance(final_reader, VersionedStore) and final_reader is not store and final_reader is not reader
            and final_reader.backend is not store.backend and final_reader.backend is not reader.backend
            and final_reader.backend.repository == store.backend.repository, "MARKET_CUSTODY_FRESH_READER_REQUIRED")
    restored = read_stored_market(reference, registry, final_reader)
    require(restored["status"] == "restored" and encoded(restored["view"]) == encoded(original), "MARKET_CUSTODY_READING_CHANGED")
    return {"status": "stored-and-independently-restored", "reference": asdict(reference),
            "query_count": len(queries), "page_count": len(page_cache), "restored": restored}


def read_stored_market(reference, registry, store, *, offset=0, limit=50, entity_id=None):
    """Exact stored versions only. A lost query degrades its dependants, not peers."""
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "PAGE_INVALID")
    ref, content = _read_manifest(store, reference, READING_SCHEMA)
    require(set(content) == {"schema", "reading", "queries"} and isinstance(content["queries"], dict)
            and len(content["queries"]) <= MAX_QUERIES, "MARKET_CUSTODY_READING_MANIFEST")
    require(isinstance(content["reading"], dict), "MARKET_CUSTODY_READING_MANIFEST")
    if content["reading"].get("schema") == "relay-three-market-reading-v1":
        require(entity_id is None, "MARKET_RELAY_COMBINED_FILTER_USE_SINGLE_SURFACE")
    queries = content["queries"]
    require(all(isinstance(k, str) and re.fullmatch(r"[a-f0-9]{64}", k) for k in queries), "MARKET_CUSTODY_QUERY_KEY")
    outcomes, cache, used = [], {}, set()
    restored_bytes = 0
    def restore(slot, path):
        nonlocal restored_bytes
        require(isinstance(slot, dict) and set(slot) == {"query"} and isinstance(slot["query"], str)
                and slot["query"] in queries, "MARKET_CUSTODY_QUERY_SLOT")
        key = slot["query"]
        used.add(key)
        if key not in cache:
            try:
                query_ref = SourceRef.from_dict(queries[key])
                require(query_ref.manifest.repository == ref.manifest.repository, "MARKET_CUSTODY_FOREIGN_QUERY")
                out = restore_market_query(query_ref, registry, store)
                header, _ = _prepare_query(out["result"], registry)
                require(digest(encoded(header)) == key, "MARKET_CUSTODY_QUERY_CHANGED")
                restored_bytes += len(encoded(out["result"]))
                require(restored_bytes <= 2 * MAX_BYTES, "MARKET_CUSTODY_READING_BOUND")
                cache[key] = (out["result"], None)
            except (RecordError, MarketInputError) as exc:
                code = str(exc)
                cache[key] = ({}, code if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "MARKET_CUSTODY_READ_FAILED")
        result, error = cache[key]
        outcomes.append({"path": path, "query": key, "status": "restored" if error is None else "unavailable",
                         "reason_code": error, "reference": queries[key]})
        return deepcopy(result)
    payload = _map_results(content["reading"], restore)
    require(used == queries.keys(), "MARKET_CUSTODY_UNUSED_QUERY")
    failed = sum(error is not None for _, error in cache.values())
    # Shared-context failures from a single reader are returned as a failed view;
    # the established three-surface reader already isolates failures by channel.
    try:
        view = read_market_payload(payload, registry, offset=offset, limit=limit, entity_id=entity_id)
        reading_error = None
    except (RecordError, ValueError) as exc:
        if not failed:
            raise
        view, reading_error = None, str(exc)
    return {"status": "partial-restoration" if failed else "restored", "reference": asdict(ref),
            "query_count": len(cache), "unavailable_queries": failed, "sources": outcomes, "view": view,
            "reading_error": reading_error, "original_http_restored": False,
            "execution_diagnostics": "not-retained", "source_requests": 0,
            "qualification": "restored-source-excerpts-not-live-health-discovery-or-economic-approval"}
