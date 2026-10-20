"""Read-only operating and T+5/T+20 reviews over exact existing retained objects.

The original research/Discovery records remain the judgment and scope owners.
This reader neither acquires sources, changes a forecast, nor creates a score.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, localcontext

from decision_kernel.application.brief_research import excerpt, item_input, version_id
from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import _available
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.market_discovery import KINDS, MarketDiscovery
from decision_kernel.application.numerical_audit import _extract
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.conditional_odds import day, timestamp
from decision_kernel.domain.outcome_review import BASIS_FIELDS, HORIZONS, basis, compare_values, horizon_change
from decision_kernel.domain.records import (
    BlobRef, RecordError, SourceRef, StoreUnavailable, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.domain.research_numbers import arithmetic_context, number
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.observations.market_daily import MarketInputError

SCHEMA = "outcome-review-plan-v1"
MAX_ECONOMIC = 16
MAX_INPUT_BYTES = 128 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
ROLES = ("research-forecast", "company-guidance", "company-preannouncement", "sell-side-forecast")
FATAL = ("PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
         "OPERATING_SOURCE_SCOPE_BOUND", "OUTCOME_READING_BOUND")
# These are the existing observe_path policy's history/persistence outcomes,
# not qualification failures. A different source reason must never be upgraded
# merely because the original owner kept its unqualified prices for diagnosis.
PATH_HISTORY_REASONS = (None, "DISCOVERY_INSUFFICIENT_CALENDAR_HISTORY", "DISCOVERY_MISSING_PRICE_WINDOW",
                       "DISCOVERY_PERSISTENCE_GAP", "DISCOVERY_INSUFFICIENT_PERSISTENCE_HISTORY")


def _now():
    return datetime.now(timezone.utc)


def _shape(value, required, optional=()):
    require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
            "OUTCOME_INPUT_SHAPE")


def _locator(value):
    require(isinstance(value, dict), "OUTCOME_LOCATOR")
    if set(value) == {"start", "end"}:
        require(type(value["start"]) is type(value["end"]) is int
                and 0 <= value["start"] < value["end"], "OUTCOME_LOCATOR")
    else:
        _shape(value, ("row", "column"))
        require(type(value["row"]) is int and value["row"] >= 1, "OUTCOME_LOCATOR")
        checked_text(value["column"], limit=256)


def _basis(value):
    basis(value)
    require(day(value["period_start"]) <= day(value["period_end"]), "OUTCOME_PERIOD_ORDER")
    require(value["period_kind"] in ("period-flow", "ytd-flow", "stock", "per-share", "ratio"),
            "OUTCOME_PERIOD_KIND")


def outcome_plan(value):
    _shape(value, ("schema", "as_of", "economic", "market"))
    require(value["schema"] == SCHEMA and len(encoded(value)) <= MAX_INPUT_BYTES, "OUTCOME_PLAN_BOUND")
    timestamp(value["as_of"])
    require(isinstance(value["economic"], list) and len(value["economic"]) <= MAX_ECONOMIC,
            "OUTCOME_ECONOMIC_BOUND")
    ids, selections = set(), set()
    for pair in value["economic"]:
        _shape(pair, ("forecast_id", "forecast", "actual", "missing_reason"))
        ident = identifier(pair["forecast_id"])
        require(ident not in ids, "OUTCOME_DUPLICATE_FORECAST")
        ids.add(ident)
        forecast = pair["forecast"]
        _shape(forecast, ("research", "quote", "role", "issued_on", "issued_on_locator", "basis",
                          "value_locator", "range", "context_bindings"))
        item_input(forecast["research"])
        checked_text(forecast["quote"], limit=8192)
        require(forecast["role"] in ROLES, "OUTCOME_FORECAST_ROLE")
        day(forecast["issued_on"])
        _locator(forecast["issued_on_locator"])
        require(set(forecast["issued_on_locator"]) == {"start", "end"}, "OUTCOME_RESEARCH_TEXT_LOCATOR")
        _basis(forecast["basis"])
        require((forecast["value_locator"] is None) != (forecast["range"] is None), "OUTCOME_POINT_OR_RANGE")
        if forecast["range"] is None:
            _locator(forecast["value_locator"])
            require(set(forecast["value_locator"]) == {"start", "end"}, "OUTCOME_RESEARCH_TEXT_LOCATOR")
        else:
            _shape(forecast["range"], ("lower_locator", "upper_locator"))
            for locator in forecast["range"].values():
                _locator(locator)
                require(set(locator) == {"start", "end"}, "OUTCOME_RESEARCH_TEXT_LOCATOR")
        unique = encoded((version_id(forecast["research"]["reference"]), forecast["research"]["start"],
            forecast["research"]["end"], forecast["basis"], forecast["value_locator"], forecast["range"]))
        require(unique not in selections, "OUTCOME_DUPLICATE_SELECTION")
        selections.add(unique)
        actual = pair["actual"]
        if actual is None:
            checked_text(pair["missing_reason"], limit=2048)
        else:
            require(pair["missing_reason"] is None, "OUTCOME_UNEXPECTED_MISSING_REASON")
            _shape(actual, ("reference", "value_locator", "basis", "reported_on", "context_bindings"))
            SourceRef.from_dict(actual["reference"])
            _locator(actual["value_locator"])
            _basis(actual["basis"])
            day(actual["reported_on"])
        for point in (forecast, actual):
            if point is None:
                continue
            bindings = point["context_bindings"]
            require(isinstance(bindings, dict) and set(bindings) <= set(BASIS_FIELDS), "OUTCOME_CONTEXT_BINDINGS")
            for locator in bindings.values():
                _locator(locator)
    market = value["market"]
    if market is not None:
        _shape(market, ("before", "after"), ("offset", "limit"))
        artifact_reference(market["before"])
        artifact_reference(market["after"])
        require(type(market.get("offset", 0)) is int and 0 <= market.get("offset", 0) <= 30000
            and type(market.get("limit", 50)) is int and 1 <= market.get("limit", 50) <= 64,
            "OUTCOME_PAGE_BOUND")
    require(value["economic"] or market is not None, "OUTCOME_EMPTY_PLAN")
    return deepcopy(value)


def _fatal(exc):
    return isinstance(exc, StoreUnavailable) or str(exc) in FATAL


def _bindings(point, data):
    for field, locator in point["context_bindings"].items():
        if isinstance(data, list):
            require(locator.get("row") == point["value_locator"].get("row"), "OUTCOME_CONTEXT_ROW_MISMATCH")
        require(_extract(data, locator) == point["basis"][field], "OUTCOME_SOURCE_BASIS_MISMATCH:" + field)
    return {"checked_fields": sorted(point["context_bindings"]),
        "declared_fields": sorted(set(BASIS_FIELDS) - set(point["context_bindings"])),
        "semantics": "exact-literal-context-when-bound-not-independent-economic-authentication"}


def _event_key(forecast):
    return digest(encoded([version_id(forecast["research"]["reference"]), forecast["basis"]["subject"],
        forecast["basis"]["period"], forecast["role"], forecast["issued_on"]]))


def _economic(pair, store, read, cutoff):
    forecast = pair["forecast"]
    research = store.read_revision(BlobRef(**forecast["research"]["reference"]))
    require(timestamp(research["record"]["stored_at"]) <= cutoff, "OUTCOME_RESEARCH_AFTER_CUTOFF")
    original = excerpt(research, item_input(forecast["research"]))
    require(original["text"] == forecast["quote"], "OUTCOME_ORIGINAL_QUOTE_MISMATCH")
    require(_extract(original["text"], forecast["issued_on_locator"]) == forecast["issued_on"],
            "OUTCOME_FORECAST_DATE_BINDING")
    require(day(forecast["issued_on"]) <= timestamp(research["record"]["stored_at"]).astimezone(cutoff.tzinfo).date(),
            "OUTCOME_FORECAST_ISSUED_AFTER_RETAINED_RESEARCH")
    forecast_basis = _bindings(forecast, original["text"])
    values = {"lower": None, "upper": None}
    if forecast["range"] is None:
        values["value"] = str(number(_extract(original["text"], forecast["value_locator"], numeric=True)))
    else:
        values.update({side: str(number(_extract(original["text"], forecast["range"][side + "_locator"], numeric=True)))
                       for side in ("lower", "upper")})
        with localcontext(arithmetic_context()):
            lo, hi = number(values["lower"]), number(values["upper"])
            require(lo <= hi, "OUTCOME_RANGE_ORDER")
            values["value"] = format((lo + hi) / Decimal(2), "f")
    result = {"forecast_id": pair["forecast_id"], "event_key": _event_key(forecast),
        "status": "actual-not-retained", "forecast": {**deepcopy(forecast), **values,
            "basis_binding": forecast_basis, "original_excerpt": original,
            "research_body_sha256": research["record"]["body"]["sha256"],
            "original_unresolved": deepcopy(research["record"]["input"]["unresolved"]),
            "research_stored_at": research["record"]["stored_at"]},
        "actual": None, "comparison": None, "missing_reason": pair["missing_reason"],
        "role_qualification": "caller-selected-from-original-text-not-semantic-approval",
        "knowledge": "later-review-not-ex-ante-capture-proof", "independent_forecast_sample": False,
        "ai_forecast_error": None, "probability_score": None, "economic_verdict": None}
    actual = pair["actual"]
    target = day(forecast["basis"]["period_end"])
    if cutoff.date() <= target:
        return {**result, "status": "not-mature", "reason_code": "OUTCOME_PERIOD_NOT_FINISHED"}
    if actual is None:
        return result
    mismatch = [field for field in BASIS_FIELDS if forecast["basis"][field] != actual["basis"][field]
                or forecast["basis"][field].lower() in ("unknown", "未知")]
    if mismatch:
        return {**result, "status": "incomparable", "mismatched_fields": mismatch}
    reported = day(actual["reported_on"])
    if reported > cutoff.date():
        return {**result, "status": "not-published-at-cutoff", "reason_code": "OUTCOME_ACTUAL_AFTER_CUTOFF"}
    issued = day(forecast["issued_on"])
    if not issued < reported or target > reported:
        return {**result, "status": "incomparable", "reason_code": "OUTCOME_TEMPORAL_IDENTITY"}
    if forecast["role"] == "company-preannouncement":
        if not target < issued:
            return {**result, "status": "incomparable", "reason_code": "OUTCOME_PREANNOUNCEMENT_MUST_FOLLOW_PERIOD"}
    else:
        if issued > target:
            return {**result, "status": "incomparable", "reason_code": "OUTCOME_FORECAST_AFTER_PERIOD"}
    try:
        view = read(SourceRef.from_dict(actual["reference"]))
        require(timestamp(view["manifest"]["stored_at"]) <= cutoff, "OUTCOME_ACTUAL_RETAINED_AFTER_CUTOFF")
        meta = view["manifest"]["input"]
        _available(meta, cutoff)
        require(meta["published_at"][:10] == actual["reported_on"], "OUTCOME_REPORTED_DATE_MISMATCH")
        if meta.get("report_period") is not None:
            require(meta["report_period"] == actual["basis"]["period"], "OUTCOME_SOURCE_PERIOD_MISMATCH")
        actual_basis = _bindings(actual, view["data"])
        value = str(number(_extract(view["data"], actual["value_locator"], numeric=True)))
    except RecordError as exc:
        if _fatal(exc):
            raise
        return {**result, "status": "actual-unavailable", "reason_code": str(exc),
                "actual_reference": deepcopy(actual["reference"])}
    result["actual"] = {**deepcopy(actual), "value": value, "basis_binding": actual_basis,
        "stored_at": view["manifest"]["stored_at"],
        "source": deepcopy(meta), "limitations": deepcopy(view["limitations"]), "original_verified": False}
    if forecast_basis["declared_fields"] or actual_basis["declared_fields"]:
        return {**result, "status": "insufficient-basis", "reason_code": "OUTCOME_UNBOUND_BASIS",
            "unbound_fields": {"forecast": forecast_basis["declared_fields"], "actual": actual_basis["declared_fields"]}}
    result["comparison"] = compare_values(values["value"], value, lower=values["lower"], upper=values["upper"])
    result["comparison"].update(unit=forecast["basis"]["unit"], error_owner=forecast["role"],
        source_precision="inherited-not-increased-by-arithmetic")
    result["status"] = "compared-literal-basis"
    return result


def _market(plan, store, registry, cutoff):
    initial = MarketDiscovery(store, registry)
    with market_read_scope(initial.source_store) as sources:
        service = MarketDiscovery(initial.store, initial.registry, source_store=sources)
        documents = []
        for name in ("before", "after"):
            # Use the existing exact Artifact/source replay, retaining its core
            # cache for the selected event proofs. No alternate price validator.
            document = service._decode(plan[name])
            require(timestamp(document["as_of"]) <= cutoff, "OUTCOME_DISCOVERY_AFTER_CUTOFF")
            service._verify_materialized(document)
            require(document == service._build(document["plan"]), "DISCOVERY_REPLAY_MISMATCH")
            documents.append(document)
        before, after = documents
        require(timestamp(before["as_of"]) <= timestamp(after["as_of"]), "OUTCOME_DISCOVERY_ORDER")
        rows, scopes = [], {}
        for kind in KINDS:
            old, new = before["surfaces"][kind], after["surfaces"][kind]
            counts = {str(h): Counter() for h in HORIZONS}
            later = None
            if new.get("proof") is not None:
                proof = after["proofs"][new["proof"]]
                _, later = service._core(kind, proof["plan"], proof["as_of"])
            scope = {"original_scope_count": old["scope_count"],
                "original_qualified_count": old["qualified_count"],
                "original_unknown_count": old["unknown_count"],
                "original_universe_version": old["universe_version"],
                "original_source_reference": deepcopy(old["source_reference"]),
                "later_scope_count": new["scope_count"], "status": old["status"],
                "later_source": {field: deepcopy(new.get(field)) for field in
                    ("status", "reason_code", "source_status", "source_hold", "source_reference", "as_of")}}
            for state in old["states"]:
                event = before["events"].get(state["origin_event"])
                out = {"kind": kind, "entity_id": state["entity_id"], "original_state": deepcopy(state),
                    "original_event": deepcopy(event), "horizons": [],
                    "timely_discovery_claim": False, "original_adjustment": old.get("adjustment"),
                    "price_meaning": "retained-close-change-not-corporate-action-adjusted-total-or-execution-return"}
                if event is not None:
                    proof = before["proofs"][event["current_proof"]]
                    _, origin = service._core(kind, proof["plan"], proof["as_of"])
                    entity = origin["entities"][state["entity_id"]]
                    future = None if later is None else later["entities"].get(state["entity_id"])
                    for h in HORIZONS:
                        comparison = horizon_change(entity, future, event["at"], h)
                        source_reason = None if future is None else future["observation"]["reason_code"]
                        if source_reason not in PATH_HISTORY_REASONS:
                            comparison.update(status="source-needed", reason_code="OUTCOME_LATER_SOURCE_UNQUALIFIED",
                                difference=None, return_pct=None, anchor_close=None, target_close=None)
                        if later is not None and later.get("source_hold") is not None:
                            comparison.update(status="source-needed", reason_code="OUTCOME_SOURCE_HOLD",
                                difference=None, return_pct=None, anchor_close=None, target_close=None)
                        comparison.update(original_price_reference=deepcopy(entity["source_reference"]),
                            later_observation_reason=source_reason,
                            later_price_reference=None if future is None else deepcopy(future["source_reference"]),
                            later_entity_gap=None if later is None else later["gaps"].get(state["entity_id"]))
                        out["horizons"].append(comparison)
                else:
                    out["horizons"] = [{"horizon_sessions": h, "status": "original-unqualified",
                        "reason_code": state["reason_code"], "return_pct": None, "target_date": None} for h in HORIZONS]
                for h in out["horizons"]:
                    counts[str(h["horizon_sessions"])][h["status"]] += 1
                rows.append(out)
            for gap in old["gaps"]:
                rows.append({"kind": kind, "entity_id": gap["entity_id"], "original_gap": deepcopy(gap),
                    "original_state": None, "original_event": None,
                    "horizons": [{"horizon_sessions": h, "status": "original-unqualified",
                        "reason_code": gap["reason_code"], "return_pct": None, "target_date": None} for h in HORIZONS]})
                for h in HORIZONS:
                    counts[str(h)]["original-unqualified"] += 1
            scope["horizons"] = {h: dict(sorted(count.items())) for h, count in counts.items()}
            if old["scope_count"] is not None:
                require(all(sum(count.values()) == old["scope_count"] for count in counts.values()),
                        "OUTCOME_ORIGINAL_SCOPE_MISMATCH")
            scopes[kind] = scope
        offset, limit = plan.get("offset", 0), plan.get("limit", 50)
        result = {"before": deepcopy(plan["before"]), "after": deepcopy(plan["after"]),
            "original_view_sha256": before["view_sha256"], "later_view_sha256": after["view_sha256"],
            "original_observation_as_of": before["as_of"], "later_observation_as_of": after["as_of"],
            "artifact_retention_time": None, "knowledge": "saved-exact-generations-not-ex-ante-capture-proof",
            "scope": scopes, "whole_rows_sha256": digest(encoded(rows)),
            "rows": rows[offset:offset + limit], "page": {"offset": offset, "limit": limit,
                "total": len(rows), "returned": len(rows[offset:offset + limit]),
                "remaining": max(0, len(rows) - offset - limit)},
            "denominator": "all-identities-in-original-discovery-including-gaps-never-reselected-by-return",
            "benchmark_return": None, "excess_return": None, "win_rate": None,
            "method_effectiveness": None, "independent_sample_count": None,
            "source_requests": 0, "writes": 0}
    return result


def open_outcome_review(store, value, *, registry=None):
    """All eligibility and final mutable access checks precede exporting results."""
    plan = outcome_plan(value)
    require(isinstance(store, VersionedStore), "OUTCOME_STORE_REQUIRED")
    cutoff = timestamp(plan["as_of"])
    require(cutoff <= _now(), "OUTCOME_FUTURE_CUTOFF")
    with reading_scope(store.backend) as backend:
        scoped = VersionedStore(backend)
        rows, byte_count, cache = [], 0, {}
        with industry_source_scope(scoped) as source_reader:
            def read(ref):
                nonlocal byte_count
                key = encoded(asdict(ref))
                if key not in cache:
                    view = source_reader.read_source(ref)
                    byte_count += len(encoded(view))
                    require(byte_count <= MAX_READ_BYTES, "OUTCOME_READING_BOUND")
                    cache[key] = view
                return deepcopy(cache[key])
            for pair in plan["economic"]:
                try:
                    rows.append(_economic(pair, scoped, read, cutoff))
                except RecordError as exc:
                    if _fatal(exc):
                        raise
                    rows.append({"forecast_id": pair["forecast_id"], "status": "unavailable",
                        "event_key": _event_key(pair["forecast"]),
                        "reason_code": str(exc), "forecast_reference": deepcopy(pair["forecast"]["research"]),
                        "actual_reference": None if pair["actual"] is None else deepcopy(pair["actual"]["reference"]),
                        "comparison": None})
        market = None
        if plan["market"] is not None:
            try:
                market = _market(plan["market"], scoped, registry, cutoff)
            except (RecordError, MarketInputError) as exc:
                if _fatal(exc):
                    raise
                market = {"status": "unavailable", "reason_code": str(exc),
                    "before": deepcopy(plan["market"]["before"]), "after": deepcopy(plan["market"]["after"]),
                    "scope": None, "rows": [], "page": None, "whole_rows_sha256": None,
                    "denominator": "original-scope-unreadable-never-assumed-empty",
                    "source_requests": 0, "writes": 0}
        counts = dict(sorted(Counter(row["status"] for row in rows).items()))
        result = {"schema": "outcome-review-v1", "plan": plan, "plan_sha256": digest(encoded(plan)),
            "as_of": plan["as_of"], "economic": {"rows": rows, "scope": {"declared_forecasts": len(rows),
                "outcomes": counts, "declared_event_keys": len({r["event_key"] for r in rows if "event_key" in r}),
                "denominator": "declared-pairs-not-all-forecasts-in-research",
                "independent_sample_count": None}}, "market": market,
            "source_requests": 0, "writes": 0, "research_executed": False, "investment_authority": "none",
            "original_judgment_modified": False, "automatic_method_change": False,
            "limitations": ["EXACT_RETAINED_BYTES_DO_NOT_AUTHENTICATE_OR_ECONOMICALLY_APPROVE_SOURCES",
                "DIFFERENCES_AND_RANGE_POSITION_ARE_NOT_PROBABILITY_OR_AI_METHOD_SCORES",
                "DATE_ONLY_DOCUMENTS_DO_NOT_ESTABLISH_PRECISE_PUBLICATION_OR_EX_ANTE_CAPTURE",
                "RAW_CLOSE_CHANGE_IS_NOT_TOTAL_RETURN_OR_PROOF_OF_STOCK_SELECTION_EFFECTIVENESS",
                "EXISTING_ADJACENT_RESEARCH_REVIEW_AND_HUMAN_ACCEPTANCE_REMAIN_SEPARATE"]}
        result["view_sha256"] = digest(encoded(result))
        require(len(encoded(result)) <= MAX_RESULT_BYTES, "OUTCOME_RESULT_BOUND")
    return result
