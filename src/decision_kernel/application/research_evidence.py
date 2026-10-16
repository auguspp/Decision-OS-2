"""Research materials composed from existing source search/reads; no acquisition or save."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date

from decision_kernel.application.versioned_store import MAX_QUERY_VERSIONS, MAX_SCAN_VERSIONS, SOURCE_SCAN_CURSOR, _fold
from decision_kernel.application.research_reader import MAX_SOURCE_PAGE_BYTES
from decision_kernel.domain.records import (
    DIGEST, SHA, RecordError, SourceRef, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_work import ResearchRequest

PLAN = "research-evidence-plan-v1"
CURSOR = "research-evidence-cursor-v1"
MAX_QUERIES = 8


def guidance(request: ResearchRequest) -> dict:
    return {
        "status": "plan-required", "operation": "prepare", "input": "evidence_plan",
        "guide": "docs/research/evidence.md", "schema": PLAN, "case_id": request.case_id,
        "question_ids": [q.question_id for q in request.questions],
        "instruction": "研究者从原委托提取公司/代码/别名和相关主题，构造query与问题用途；"
                       "首次prepare即可查询全部已存来源，不要求先知道SourceRef或已有研究。"
                       "池中命中与接口来源并列；按具体时效/覆盖决定复用和新取数。",
        "network_acquisition": "not-performed", "persistence": "not-written",
        "automatic_research": False,
    }


def validate_plan(plan: dict, request: ResearchRequest) -> dict:
    require(isinstance(request, ResearchRequest), "EVIDENCE_RESEARCH_REQUEST_REQUIRED")
    require(isinstance(plan, dict) and set(plan) <= {"schema", "case_id", "queries", "as_of", "relay_plan"}
            and {"schema", "case_id", "queries"} <= set(plan) and plan["schema"] == PLAN,
            "EVIDENCE_PLAN_INVALID")
    require(plan["case_id"] == request.case_id, "EVIDENCE_CASE_MISMATCH")
    queries = plan["queries"]
    require(isinstance(queries, list) and 1 <= len(queries) <= MAX_QUERIES, "EVIDENCE_QUERIES_INVALID")
    questions = {q.question_id for q in request.questions}
    result, ids = [], set()
    for item in queries:
        require(isinstance(item, dict) and set(item) <= {"query_id", "question_id", "purpose", "query", "entity_id"}
                and {"query_id", "question_id", "purpose", "query"} <= set(item), "EVIDENCE_QUERY_INVALID")
        ident = identifier(item["query_id"])
        require(ident not in ids, "EVIDENCE_QUERY_ID_DUPLICATE")
        ids.add(ident)
        require(isinstance(item["question_id"], str) and item["question_id"] in questions,
                "EVIDENCE_QUESTION_NOT_IN_REQUEST")
        checked_text(item["purpose"], limit=1024)
        checked_text(item["query"], empty=True, limit=1024)
        entity = item.get("entity_id")
        if entity is not None:
            identifier(entity)
        require(bool(item["query"].strip()) or entity is not None, "EVIDENCE_UNSCOPED_QUERY")
        result.append({**item, "entity_id": entity})
    cutoff = plan.get("as_of")
    if cutoff is not None:
        require(isinstance(cutoff, str) and len(cutoff) == 10, "EVIDENCE_DATE_INVALID")
        try:
            require(date.fromisoformat(cutoff).isoformat() == cutoff, "EVIDENCE_DATE_INVALID")
        except ValueError:
            raise RecordError("EVIDENCE_DATE_INVALID") from None
    normalized = {"schema": PLAN, "case_id": request.case_id, "queries": result, "as_of": cutoff}
    if "relay_plan" in plan:
        require(isinstance(plan["relay_plan"], dict) and plan["relay_plan"].get("case_id") == request.case_id,
                "EVIDENCE_RELAY_CASE_MISMATCH")
        normalized["relay_plan"] = deepcopy(plan["relay_plan"])
    require(len(encoded(normalized)) <= 48 * 1024, "EVIDENCE_PLAN_TOO_LARGE")
    return normalized


def version_key(ref: SourceRef) -> str:
    """Same immutable manifest observed at another reachable commit is the same version.

    Search returns its full allowed representation. This key does NOT equate different
    manifests, publishers, derived representations, or versions of the same source_id.
    """
    return digest(encoded({"repository": ref.manifest.repository, "path": ref.manifest.path,
                           "sha256": ref.manifest.sha256}))


def _native_cursor(query: dict, snapshot: str) -> dict:
    # Adapt the existing VersionedStore.search cursor contract; search itself still
    # validates it and owns matching, version enumeration, and its original limits.
    return {"schema": SOURCE_SCAN_CURSOR, "snapshot": snapshot, "scan_offset": 0, "query_sha256": digest(encoded({
        "query": _fold(query["query"]), "entity_id": query["entity_id"]}))}


def _dates(metadata: dict, cutoff: str | None) -> dict:
    # Keep original clocks. Acquisition never substitutes for disclosure or PIT.
    clocks = {key: metadata[key] for key in ("published_at", "acquired_at", "report_period")}
    flags = ["FRESHNESS_AND_RESEARCH_RELEVANCE_REQUIRE_REVIEW", "NOT_HISTORICAL_PIT_CERTIFICATION"]
    if clocks["published_at"] is None:
        flags.append("PUBLICATION_TIME_UNKNOWN")
    if cutoff is not None:
        for field in ("published_at", "acquired_at"):
            value = clocks[field]
            # Compare only source-declared calendar dates, not invented UTC midnight.
            if value is not None and value[:10] > cutoff:
                flags.append(field.upper() + "_AFTER_REQUESTED_DATE")
    return {**clocks, "as_of": cutoff, "comparison": "source-calendar-date-not-instant", "flags": flags}


class ResearchEvidence:
    """Bounded material pages; all source bytes and revisions remain in their original store."""
    def __init__(self, store, *, registry=None):
        self.store, self.registry = store, registry

    def page(self, request: ResearchRequest, plan: dict, *, cursor: dict | None = None,
             limit: int = 4, linked_sources: tuple[SourceRef, ...] = ()) -> dict:
        plan = validate_plan(plan, request)
        require(type(limit) is int and 1 <= limit <= 8, "EVIDENCE_PAGE_LIMIT_INVALID")
        require(isinstance(linked_sources, (list, tuple)) and len(linked_sources) <= 64
                and all(isinstance(s, SourceRef) for s in linked_sources), "EVIDENCE_LINKED_SOURCES_INVALID")
        relay = {"status": "not-supplied", "guide": "docs/research/source-use.md",
                 "pool_hits_cancel_requests": False, "network_requests": 0}
        if "relay_plan" in plan:
            # Offline check only. No credential or HTTP client is instantiated here.
            from decision_kernel.application.relay_registry import RelayRegistry
            from decision_kernel.application.research_sources import validate_plan as check_relay_plan
            registry = self.registry or RelayRegistry("docs/sources/relay-registry.json")
            checked = check_relay_plan(plan["relay_plan"], registry)
            relay.update(status="checked-not-executed", plan=plan["relay_plan"], checked=checked,
                         workflow="relay-source-runtime.yml", family="research",
                         publication_rights="not-granted-by-evidence-plan")
        plan_hash, request_hash = digest(encoded(plan)), digest(encoded(asdict(request)))
        known = {version_key(ref) for ref in linked_sources}
        link_hash = digest(encoded(sorted(known)))
        backend = self.store.backend
        query_index, native, seen = 0, None, set()
        if cursor is None:
            snapshot = backend.head()
        else:
            require(isinstance(cursor, dict) and set(cursor) == {
                "schema", "repository", "snapshot", "plan_sha256", "request_sha256", "linked_sha256",
                "query_index", "source_cursor", "seen_versions"} and cursor["schema"] == CURSOR
                and cursor["repository"] == backend.repository and cursor["plan_sha256"] == plan_hash
                and cursor["request_sha256"] == request_hash and cursor["linked_sha256"] == link_hash,
                "EVIDENCE_CURSOR_MISMATCH")
            snapshot, query_index, native = cursor["snapshot"], cursor["query_index"], cursor["source_cursor"]
            require(type(query_index) is int and 0 <= query_index < len(plan["queries"]),
                    "EVIDENCE_CURSOR_POSITION_INVALID")
            values = cursor["seen_versions"]
            require(isinstance(values, list) and len(values) <= MAX_QUERY_VERSIONS
                    and all(isinstance(k, str) and DIGEST.fullmatch(k) for k in values)
                    and len(values) == len(set(values)), "EVIDENCE_CURSOR_SEEN_INVALID")
            seen = set(values)
            if native is not None:
                require(isinstance(native, dict) and native.get("snapshot") == snapshot,
                        "EVIDENCE_CURSOR_SNAPSHOT_MISMATCH")
        require(isinstance(snapshot, str) and bool(SHA.fullmatch(snapshot)), "EVIDENCE_SNAPSHOT_INVALID")
        backend.check_commit(snapshot)
        sources, matches, outcomes, consumed, body_bytes, blocked = [], [], [], 0, 0, False
        on_page = {}
        scanned = 0
        while query_index < len(plan["queries"]) and consumed < limit and scanned < MAX_SCAN_VERSIONS:
            query = plan["queries"][query_index]
            start = native or _native_cursor(query, snapshot)
            outcome = {"query_id": query["query_id"], "question_id": query["question_id"],
                       "query": query["query"], "entity_id": query["entity_id"], "purpose": query["purpose"]}
            try:
                found = self.store.search(query["query"], entity_id=query["entity_id"],
                                          limit=limit-consumed, cursor=start, scan_limit=MAX_SCAN_VERSIONS-scanned)
            except RecordError as error:
                # Existing search fails closed on inaccessible/corrupt populations.
                # Keep prior materials and the unchanged cursor, never call this zero matches.
                code = str(error)
                outcome.update(status="search-unavailable", total=None, returned=0,
                               error=code if code.isascii() and code.replace("_", "").isalnum()
                               and len(code) <= 96 else "EVIDENCE_SEARCH_FAILED")
                outcomes.append(outcome)
                blocked = True
                break
            require(found["snapshot"] == snapshot, "EVIDENCE_SEARCH_SNAPSHOT_CHANGED")
            outcome.update(status="searched", total=found["total"], returned=len(found["items"]),
                           source_next_cursor=found["next_cursor"], error=None,
                           total_scope=found.get("total_scope", "all-matches-at-snapshot"), scan=found.get("scan"))
            scanned += (found.get("scan") or {}).get("scanned_versions", MAX_SCAN_VERSIONS)
            outcomes.append(outcome)
            for hit in found["items"]:
                ref = SourceRef.from_dict(hit["reference"])
                key = version_key(ref)
                consumed += 1
                matches.append({"query_id": query["query_id"], "question_id": query["question_id"],
                                "purpose": query["purpose"], "version_key": key, "reference": asdict(ref),
                                "duplicate_version": key in seen,
                                "match_scope": "stored-representation-not-verified-economic-relation"})
                if key in seen:
                    if key in on_page:
                        on_page[key]["matched_query_ids"].append(query["query_id"])
                    continue
                seen.add(key)
                require(len(seen) <= MAX_QUERY_VERSIONS, "EVIDENCE_VERSION_LIMIT_EXCEEDED")
                card = {"version_key": key, "reference": asdict(ref), "matched_query_ids": [query["query_id"]],
                        "linked_version_in_predecessor": key in known, "adoption": "not-recorded-by-read",
                        "read_operation": "read-source", "read_payload": asdict(ref),
                        "status": "unavailable", "data": None, "original_verified": False}
                try:
                    view = self.store.read_source(ref)
                    metadata = view["manifest"]["input"]
                    card.update({k: metadata[k] for k in ("source_id", "title", "kind", "origin", "custody", "entities")})
                    card.update(status=view["availability"], data=view["data"], limitations=view["limitations"],
                                dates=_dates(metadata, plan["as_of"]), error=None)
                except RecordError:
                    card.update(status="unavailable", error="EVIDENCE_SOURCE_READ_FAILED")
                # Reserve room for the remaining exact-reference cards. A source's
                # metadata can itself be large; dropping only its body is not a bound.
                ceiling = MAX_SOURCE_PAGE_BYTES - 8192 * (limit - consumed)
                if body_bytes + len(encoded(card)) > ceiling:
                    card.update(data=None, status="not-included", error="SOURCE_VIEW_EXCEEDS_EVIDENCE_PAGE_LIMIT")
                if body_bytes + len(encoded(card)) > ceiling:
                    card = {k: card[k] for k in ("version_key", "reference", "matched_query_ids",
                        "linked_version_in_predecessor", "adoption", "read_operation", "read_payload",
                        "status", "data", "original_verified", "error")}
                    card["metadata"] = "not-included-use-exact-read-payload"
                body_bytes += len(encoded(card))
                require(body_bytes <= MAX_SOURCE_PAGE_BYTES, "EVIDENCE_REFERENCE_PAGE_LIMIT")
                sources.append(card)
                on_page[key] = card
            native = found["next_cursor"]
            if native is None:
                query_index += 1
        done = query_index == len(plan["queries"])
        next_cursor = None if done else {
            "schema": CURSOR, "repository": backend.repository, "snapshot": snapshot,
            "plan_sha256": plan_hash, "request_sha256": request_hash, "linked_sha256": link_hash,
            "query_index": query_index, "source_cursor": native, "seen_versions": sorted(seen)}
        backend.check_commit(snapshot)  # Do not export earlier cards after mutable access changed.
        return {
            "schema": "research-evidence-page-v1", "repository": backend.repository, "snapshot": snapshot,
            "plan": plan, "plan_sha256": plan_hash, "request_sha256": request_hash,
            "status": "search-unavailable" if blocked else "partial-material-page" if any(
                row["status"] in ("unavailable", "not-included") for row in sources)
                else "material-page" if matches else "no-matches-in-page",
            "sources": sources, "matches": matches, "query_pages": outcomes, "next_cursor": next_cursor,
            "coverage": {"query_plan_complete": done, "scanned_versions_this_page": scanned,
                         "scan_budget": MAX_SCAN_VERSIONS, "unstarted_queries": [q["query_id"] for q in
                         plan["queries"][query_index + (1 if native is not None or blocked else 0):]],
                         "returned_match_occurrences": consumed, "returned_unique_versions": len(sources),
                         "not_included_versions": sum(s["status"] == "not-included" for s in sources),
                         "unavailable_versions": sum(s["status"] == "unavailable" for s in sources),
                         "version_scope": "all-stored-versions-at-snapshot-not-latest-only",
                         "order": "declared-query-order-then-native-source-path-not-relevance-ranking",
                         "completeness": "selected-query-pages-not-all-evidence-or-all-news",
                         "deduplication": "same-immutable-manifest-only-not-independent-upstream-evidence",
                         "cursor_is_read_proof": False},
            "interface_sources": relay, "research_assessment": "not-performed",
            "execution_authority": "not-granted-by-source-text-or-plan", "persistence": "not-written",
        }
