"""Finite FTShare replacement generation in the existing records Store.

The exact Relay predecessor, its failed slots and its spent allowance remain
unchanged. FTShare owns new one-attempt, whole-history batch slots. Source I/O,
native job identity, secret handling and work-unit capacity stay with the host.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re

from decision_kernel.application.ftshare_adoption import (
    ADOPTION_PLAN_SCHEMA, PRICE_ROLES, ROLES, kernel_price_requests,
    read_kernel_archive, restore_kernel_evidence, source_for_kernel_evidence, validate_adoption_proof,
)
from decision_kernel.application.ftshare_daily import batch_request, capture_batch, source_for_capture
from decision_kernel.application.ftshare_single import restore_single, single_request, source_for_single_capture
from decision_kernel.application.market_collection import effective_unit, unit_request, _qualified
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_custody import (
    _document, _put, _read_source, authorize_market_custody, fresh_market_reader,
    market_read_scope, restore_market_query,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import MAX_STORE_VERSIONS, VersionedStore
from decision_kernel.domain.records import (
    Selection, SourceInput, SourceRef, UnknownWrite, digest, encoded, identifier, require,
)

SCHEMA = "ftshare-collection-custody-v1"
STATE_SCHEMA = "ftshare-collection-state-v1"
PLAN_SCHEMA = "ftshare-collection-plan-v1"
SINGLE_PLAN_SCHEMA = "ftshare-single-collection-plan-v1"
BATCH_SIZE = 50
MAX_REQUESTS = 212
MAX_ROOTS = 2 * MAX_REQUESTS + 1
MAX_ROOT_BYTES = 512 * 1024
MAX_CHAIN_BYTES = MAX_ROOTS * MAX_ROOT_BYTES
VENUES = ("SSE", "SZSE")
SOURCE_HOLD_REASONS = frozenset({
    "FTSHARE_HTTP_401", "FTSHARE_HTTP_403", "FTSHARE_HTTP_429",
    "FTSHARE_OBSERVED_QUOTA_EXHAUSTED", "FTSHARE_WAIT_INSTRUCTION_UNUSABLE",
    "FTSHARE_WAIT_EXCEEDS_CHECK_BOUND",
})
ORIGIN = "https://github.com/auguspp/Decision-OS-2"
LIMITATIONS = (
    "GENERATED_FTSHARE_COLLECTION_CONTROL_NOT_ORIGINAL_PROVIDER_RESPONSE",
    "EXACT_LEGACY_REFERENCE_PRESERVES_OLD_SLOTS_AND_SPENT_SOURCE_ALLOWANCE",
    "ONE_ATTEMPT_PER_BATCH_NO_AUTOMATIC_RETRY_OR_DAILY_GENERATION_ADVANCE",
    "RETAINED_PROVIDER_HOLD_REQUIRES_EXPLICIT_EXTERNAL_REVIEW_NO_AUTOMATIC_UNLOCK",
    "RAW_AND_PROVIDER_FORWARD_WHOLE_REQUESTS_NOT_RELAY_OR_POINT_IN_TIME_FACTORS",
    "SOURCE_RETENTION_REQUIRES_CALLER_AUTHORITY_NOT_PUBLIC_REDISTRIBUTION",
)


def _clock(value):
    require(isinstance(value, str) and 1 <= len(value) <= 80 and "T" in value,
            "FTSHARE_COLLECTION_CLOCK")
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        require(False, "FTSHARE_COLLECTION_CLOCK")
    require(when.utcoffset() is not None, "FTSHARE_COLLECTION_CLOCK")
    return when


def _ref(value, repository):
    reference = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(reference.selection == Selection() and reference.manifest.repository == repository,
            "FTSHARE_COLLECTION_EXACT_LOCAL_REFERENCE")
    return _document(encoded(asdict(reference)))


def _date_bounds(start, end):
    zone = timezone(timedelta(hours=8))
    first = datetime.strptime(start, "%Y%m%d").replace(tzinfo=zone)
    final = datetime.strptime(end, "%Y%m%d").replace(tzinfo=zone) + timedelta(days=1)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((first - epoch) // timedelta(milliseconds=1),
            (final - epoch) // timedelta(milliseconds=1) - 1)


def _legacy_context(legacy_reference, registry, store, *, calendar_references=None):
    reference = _ref(legacy_reference, store.backend.repository)
    restored = restore_market_collection(reference, registry, store)
    collection = restored["collection"]
    plan = collection["plan"]
    # The host supplies the fixed Source10 reference. Its restored meaning must
    # still be the old cn-eod generation, not a caller-authored replacement plan.
    require(plan["collection_id"] == "cn-eod" and plan["generation"] == "g20261016"
            and plan["start"] == "20260701" and plan["end"] == "20261016",
            "FTSHARE_COLLECTION_LEGACY_SCOPE")
    context = {"reference": reference, "masters": {}, "calendars": {}, "sessions": {},
               "master_results": {}, "calendar_results": {},
               "master_references": {}, "calendar_references": {}}
    for family in ("calendar", "master"):
        for venue in VENUES:
            slot = family + ":" + venue
            unit = effective_unit(collection, slot)
            require(unit is not None and unit["status"] == "complete",
                    "FTSHARE_COLLECTION_LEGACY_CONTEXT_REQUIRED")
            query = _ref(unit["query"], store.backend.repository)
            result = restore_market_query(query, registry, store)["result"]
            use, _ = unit_request(plan, slot, registry)
            checked = _qualified(result, use, plan, registry)
            context[family + "_results"][venue] = result
            context[family + "_references"][venue] = query
            context["calendars" if family == "calendar" else "masters"][venue] = checked
    context["legacy_calendar_references"] = deepcopy(context["calendar_references"])
    lo, hi = "2026-07-01", "2026-10-16"
    for venue in VENUES:
        context["sessions"][venue] = [day for day in context["calendars"][venue] if lo <= day <= hi]
        require(context["sessions"][venue] and context["sessions"][venue][-1] == hi,
                "FTSHARE_COLLECTION_END_SESSION_REQUIRED")
    union = sorted({day.replace("-", "") for days in context["sessions"].values() for day in days})
    require(plan["sessions"] == union, "FTSHARE_COLLECTION_LEGACY_SESSIONS_CHANGED")
    master_rows = {code: item for master in context["masters"].values()
                   for code, item in master["securities"].items()}
    require(len(master_rows) == sum(master["scope_count"] for master in context["masters"].values()),
            "FTSHARE_COLLECTION_LEGACY_DUPLICATE_SYMBOL")
    context["scope_symbols"] = sorted(master_rows)
    context["eligible_symbols"] = sorted(code for code, item in master_rows.items() if item is not None)
    context["quarantined_symbols"] = sorted(code for code, item in master_rows.items() if item is None)
    unit = effective_unit(collection, plan["end"] + ":daily")
    require(unit is not None and unit["status"] == "complete",
            "FTSHARE_COLLECTION_LEGACY_DAILY_REQUIRED")
    query = _ref(unit["query"], store.backend.repository)
    result = restore_market_query(query, registry, store)["result"]
    use, _ = unit_request(plan, plan["end"] + ":daily", registry)
    context["baseline_daily_quotes"] = _qualified(result, use, plan, registry)
    context.update(baseline_daily=result, baseline_daily_reference=query)
    counts = {status: sum(unit["status"] == status for unit in collection["units"].values())
              for status in sorted({unit["status"] for unit in collection["units"].values()})}
    context["legacy"] = {"collection_sha256": digest(encoded(collection)),
        "collection_id": plan["collection_id"], "generation": plan["generation"],
        "unit_count": len(collection["units"]), "unit_status_counts": counts,
        "source_requests": collection["attempt"]["source_requests"],
        "repair_present": "repair" in collection}
    context.update(start=plan["start"], end=plan["end"], calendar_end=plan["calendar_end"])
    if calendar_references is not None:
        require(isinstance(calendar_references, dict) and set(calendar_references) == set(VENUES),
                "FTSHARE_COLLECTION_CALENDAR_REFERENCES")
        references = {venue: _ref(calendar_references[venue], store.backend.repository) for venue in VENUES}
        if references != context["calendar_references"]:
            through = set()
            for venue in VENUES:
                result = restore_market_query(references[venue], registry, store)["result"]
                use = result["use"]
                params = use["parameters"]
                require(use["api"] == "trade_cal" and params.get("exchange") == venue
                        and params.get("start_date") == plan["start"]
                        and isinstance(params.get("end_date"), str)
                        and params["end_date"] >= plan["calendar_end"],
                        "FTSHARE_COLLECTION_CALENDAR_SCOPE")
                checked_plan = {**plan, "calendar_end": params["end_date"]}
                # This original source-specific qualifier proves every actual
                # date, including closed days; no weekday inference is used.
                days = _qualified(result, use, checked_plan, registry)
                _date_bounds(plan["start"], params["end_date"])
                require((datetime.strptime(params["end_date"], "%Y%m%d")
                         - datetime.strptime(plan["start"], "%Y%m%d")).days < 119,
                        "FTSHARE_COLLECTION_CALENDAR_RANGE")
                require([day for day in days if lo <= day <= hi] == context["sessions"][venue],
                        "FTSHARE_COLLECTION_CALENDAR_HISTORY_CHANGED")
                through.add(params["end_date"])
                context["calendar_results"][venue] = result
                context["calendar_references"][venue] = references[venue]
                context["calendars"][venue] = days
            require(len(through) == 1, "FTSHARE_COLLECTION_CALENDAR_END_MISMATCH")
            context["calendar_end"] = next(iter(through))
    return context


def _requests(symbols, since, until):
    require(isinstance(symbols, list) and 0 < len(symbols) <= 5300
            and all(isinstance(symbol, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol)
                    for symbol in symbols) and symbols == sorted(set(symbols)),
            "FTSHARE_COLLECTION_SYMBOLS")
    requests = {}
    for index, start in enumerate(range(0, len(symbols), BATCH_SIZE)):
        for kind in ("none", "forward"):
            requests[kind + ":" + f"{index:03d}"] = batch_request(
                symbols=symbols[start:start + BATCH_SIZE], since=since, until=until, adjust_kind=kind)
    require(len(requests) <= MAX_REQUESTS, "FTSHARE_COLLECTION_REQUEST_BOUND")
    return requests


def _plan(context, *, collection_id, generation, created_at):
    identifier(collection_id); identifier(generation); _clock(created_at)
    require(collection_id.startswith("ftshare-"), "FTSHARE_COLLECTION_NAMESPACE_REQUIRED")
    since, until = _date_bounds(context["start"], context["end"])
    requests = _requests(context["eligible_symbols"], since, until)
    return {"schema": PLAN_SCHEMA, "collection_id": collection_id, "generation": generation,
        "created_at": created_at, "legacy_reference": deepcopy(context["reference"]),
        "legacy": deepcopy(context["legacy"]), "start": context["start"], "end": context["end"],
        "calendar_end": context["calendar_end"], "since": since, "until": until,
        "master_references": deepcopy(context["master_references"]),
        "calendar_references": deepcopy(context["calendar_references"]),
        "legacy_calendar_references": deepcopy(context["legacy_calendar_references"]),
        "baseline_daily_reference": deepcopy(context["baseline_daily_reference"]),
        "sessions": deepcopy(context["sessions"]), "symbols": list(context["scope_symbols"]),
        "eligible_symbols": list(context["eligible_symbols"]),
        "quarantined_symbols": list(context["quarantined_symbols"]),
        "batch_size": BATCH_SIZE, "source_budget": len(requests), "requests": requests}


def plan_ftshare_collection(legacy_reference, registry, store, *, collection_id,
                           generation, created_at, calendar_references=None):
    """Derive one finite generation from exact retained reference evidence only."""
    require(isinstance(registry, RelayRegistry), "FTSHARE_COLLECTION_REGISTRY_REQUIRED")
    with market_read_scope(store) as reader:
        context = _legacy_context(legacy_reference, registry, reader,
                                  calendar_references=calendar_references)
        plan = _plan(context, collection_id=collection_id, generation=generation, created_at=created_at)
    return {"schema": STATE_SCHEMA, "plan": plan, "revision": 0,
        "units": {slot: {"status": "pending", "request_count": 0,
                         "reference": None, "reason_code": None} for slot in plan["requests"]},
        "reserved_requests": 0, "source_hold": None, "updated_at": created_at}


def validate_ftshare_collection(collection):
    """Pure shape and finite budget checks; retained reference proof is separate."""
    if isinstance(collection, dict) and isinstance(collection.get("plan"), dict) \
            and collection["plan"].get("schema") == SINGLE_PLAN_SCHEMA:
        return _validate_single_collection(collection)
    if isinstance(collection, dict) and isinstance(collection.get("plan"), dict) \
            and collection["plan"].get("schema") == ADOPTION_PLAN_SCHEMA:
        return _validate_adoption_collection(collection)
    require(isinstance(collection, dict) and set(collection) == {
        "schema", "plan", "revision", "units", "reserved_requests", "source_hold", "updated_at"}
        and collection["schema"] == STATE_SCHEMA, "FTSHARE_COLLECTION_STATE")
    plan = collection["plan"]
    require(isinstance(plan, dict) and set(plan) == {"schema", "collection_id", "generation",
        "created_at", "legacy_reference", "legacy", "start", "end", "calendar_end", "since", "until",
        "master_references", "calendar_references", "legacy_calendar_references",
        "baseline_daily_reference", "sessions", "symbols",
        "eligible_symbols", "quarantined_symbols", "batch_size", "source_budget", "requests"}
        and plan["schema"] == PLAN_SCHEMA, "FTSHARE_COLLECTION_PLAN")
    identifier(plan["collection_id"]); identifier(plan["generation"])
    require(plan["collection_id"].startswith("ftshare-"), "FTSHARE_COLLECTION_NAMESPACE_REQUIRED")
    require(plan["start"] == "20260701" and plan["end"] == "20261016"
            and (plan["since"], plan["until"]) == _date_bounds(plan["start"], plan["end"]),
            "FTSHARE_COLLECTION_FIXED_RANGE")
    require(plan["batch_size"] == BATCH_SIZE and plan["requests"] == _requests(
        plan["eligible_symbols"], plan["since"], plan["until"]), "FTSHARE_COLLECTION_REQUESTS_CHANGED")
    for field in ("symbols", "eligible_symbols", "quarantined_symbols"):
        values = plan[field]
        require(isinstance(values, list) and len(values) <= 10000
                and all(isinstance(value, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value)
                        for value in values) and values == sorted(set(values)), "FTSHARE_COLLECTION_SYMBOLS")
    require(not set(plan["eligible_symbols"]) & set(plan["quarantined_symbols"])
            and set(plan["symbols"]) == set(plan["eligible_symbols"]) | set(plan["quarantined_symbols"]),
            "FTSHARE_COLLECTION_SCOPE_CHANGED")
    require(type(plan["source_budget"]) is int and plan["source_budget"] == len(plan["requests"]),
            "FTSHARE_COLLECTION_BUDGET_CHANGED")
    units = collection["units"]
    require(isinstance(units, dict) and set(units) == set(plan["requests"]), "FTSHARE_COLLECTION_UNITS")
    for unit in units.values():
        require(isinstance(unit, dict) and set(unit) == {"status", "request_count", "reference", "reason_code"}
                and unit["status"] in ("pending", "in-flight", "complete", "failed"), "FTSHARE_COLLECTION_UNIT")
        require(type(unit["request_count"]) is int
                and unit["request_count"] == (0 if unit["status"] == "pending" else 1),
                "FTSHARE_COLLECTION_ONE_ATTEMPT_ONLY")
        require((unit["reference"] is not None) == (unit["status"] == "complete"),
                "FTSHARE_COLLECTION_UNIT_REFERENCE")
        require((unit["reason_code"] is not None) == (unit["status"] == "failed"),
                "FTSHARE_COLLECTION_UNIT_REASON")
        if unit["reference"] is not None:
            SourceRef.from_dict(unit["reference"])
        if unit["reason_code"] is not None:
            require(isinstance(unit["reason_code"], str)
                    and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", unit["reason_code"]),
                    "FTSHARE_COLLECTION_UNIT_REASON")
    require(sum(unit["status"] == "in-flight" for unit in units.values()) <= 1,
            "FTSHARE_COLLECTION_ONE_INFLIGHT_ONLY")
    attempts = sum(unit["request_count"] for unit in units.values())
    settled = sum(unit["status"] in ("complete", "failed") for unit in units.values())
    require(type(collection["reserved_requests"]) is int and collection["reserved_requests"] == attempts
            and attempts <= plan["source_budget"] and type(collection["revision"]) is int
            and collection["revision"] == attempts + settled, "FTSHARE_COLLECTION_BUDGET_CHANGED")
    require(_clock(collection["updated_at"]) >= _clock(plan["created_at"]), "FTSHARE_COLLECTION_CLOCK")
    require(collection["revision"] != 0 or collection["updated_at"] == plan["created_at"],
            "FTSHARE_COLLECTION_INITIAL_STATE")
    hold = collection["source_hold"]
    if hold is not None:
        require(isinstance(hold, dict) and set(hold) == {"reason_code", "set_at"}
                and isinstance(hold["reason_code"], str) and hold["reason_code"] in SOURCE_HOLD_REASONS
                and hold["set_at"] == collection["updated_at"] and settled > 0
                and not any(unit["status"] == "in-flight" for unit in units.values()),
                "FTSHARE_COLLECTION_SOURCE_HOLD_INVALID")
    require(len(encoded(collection)) <= MAX_ROOT_BYTES - 16384, "FTSHARE_COLLECTION_STATE_BOUND")
    return deepcopy(collection)


def begin_ftshare_slot(collection, slot, *, at):
    """Reserve exactly one request; the host retains this state BEFORE source I/O."""
    result = validate_ftshare_collection(collection)
    require(result["plan"]["schema"] in (PLAN_SCHEMA, SINGLE_PLAN_SCHEMA),
            "FTSHARE_ADOPTION_NO_SOURCE_BUDGET")
    require(result["source_hold"] is None, "FTSHARE_COLLECTION_SOURCE_HOLD")
    require(slot in result["units"] and result["units"][slot]["status"] == "pending",
            "FTSHARE_COLLECTION_SLOT_ALREADY_CONSUMED")
    require(not any(unit["status"] == "in-flight" for unit in result["units"].values()),
            "FTSHARE_COLLECTION_UNKNOWN_REQUEST_MUST_RECONCILE")
    require(_clock(at) >= _clock(result["updated_at"]), "FTSHARE_COLLECTION_CLOCK")
    result["units"][slot].update(status="in-flight", request_count=1)
    result.update(reserved_requests=result["reserved_requests"] + 1, revision=result["revision"] + 1,
                  updated_at=at)
    return validate_ftshare_collection(result)


def finish_ftshare_slot(collection, slot, *, at, reference=None, reason_code=None, source_hold=None):
    """Settle once, optionally retaining the host's classified provider stop.

    A successful response may retain valid bars and also exhaust provider quota.
    The hold prevents every later reservation; no automatic clearing path exists.
    """
    result = validate_ftshare_collection(collection)
    require(result["plan"]["schema"] in (PLAN_SCHEMA, SINGLE_PLAN_SCHEMA),
            "FTSHARE_ADOPTION_NO_SOURCE_BUDGET")
    require(result["source_hold"] is None, "FTSHARE_COLLECTION_SOURCE_HOLD")
    require(source_hold is None or isinstance(source_hold, str) and source_hold in SOURCE_HOLD_REASONS,
            "FTSHARE_COLLECTION_SOURCE_HOLD_INVALID")
    require(slot in result["units"] and result["units"][slot]["status"] == "in-flight",
            "FTSHARE_COLLECTION_SLOT_NOT_INFLIGHT")
    require((reference is None) != (reason_code is None), "FTSHARE_COLLECTION_OUTCOME_REQUIRED")
    require(_clock(at) >= _clock(result["updated_at"]), "FTSHARE_COLLECTION_CLOCK")
    if isinstance(reference, SourceRef):
        reference = _document(encoded(asdict(reference)))
    result["units"][slot].update(status="complete" if reference is not None else "failed",
                                reference=deepcopy(reference), reason_code=reason_code)
    result.update(revision=result["revision"] + 1, updated_at=at,
                  source_hold=None if source_hold is None else {"reason_code": source_hold, "set_at": at})
    return validate_ftshare_collection(result)


def _transition(previous, current):
    require(previous["plan"]["schema"] != ADOPTION_PLAN_SCHEMA, "FTSHARE_ADOPTION_IMMUTABLE")
    require(previous["source_hold"] is None, "FTSHARE_COLLECTION_SOURCE_HOLD")
    require(previous["plan"] == current["plan"], "FTSHARE_COLLECTION_PLAN_CHANGED")
    require(_clock(current["updated_at"]) >= _clock(previous["updated_at"]), "FTSHARE_COLLECTION_CLOCK")
    changed = [slot for slot in previous["units"] if previous["units"][slot] != current["units"][slot]]
    require(len(changed) == 1 and current["revision"] == previous["revision"] + 1,
            "FTSHARE_COLLECTION_TRANSITION")
    slot = changed[0]
    require(previous["plan"]["schema"] != SINGLE_PLAN_SCHEMA
            or slot in previous["plan"]["new_slots"], "FTSHARE_SINGLE_INHERITED_UNIT_CHANGED")
    old, new = previous["units"][slot], current["units"][slot]
    require((old["status"], new["status"]) in {
        ("pending", "in-flight"), ("in-flight", "complete"), ("in-flight", "failed")},
        "FTSHARE_COLLECTION_SLOT_IMMUTABLE")
    if new["status"] == "in-flight":
        require(current["source_hold"] is None, "FTSHARE_COLLECTION_SOURCE_HOLD_INVALID")
        require(not any(unit["status"] == "in-flight" for unit in previous["units"].values()),
                "FTSHARE_COLLECTION_UNKNOWN_REQUEST_MUST_RECONCILE")


def _source_id(collection_id, generation, revision=None):
    identifier(collection_id); identifier(generation)
    prefix = "ftshare-collection-" + digest(encoded([collection_id, generation]))[:32] + "-"
    if revision is None:
        return prefix
    require(type(revision) is int and 0 <= revision < MAX_ROOTS, "FTSHARE_COLLECTION_REVISION")
    return prefix + f"r{revision:03d}"


def _source(content):
    plan = content["collection"]["plan"]
    body = encoded(content)
    require(len(body) <= MAX_ROOT_BYTES, "FTSHARE_COLLECTION_ROOT_BOUND")
    adopted = plan["schema"] == ADOPTION_PLAN_SCHEMA
    single = plan["schema"] == SINGLE_PLAN_SCHEMA
    return SourceInput(source_id=_source_id(plan["collection_id"], plan["generation"],
                                           content["collection"]["revision"]),
        kind="content", title="FTShare retained Kernel v1 daily adoption" if adopted
            else "FTShare finite single-stock daily collection" if single else "FTShare finite daily collection",
        origin=ORIGIN, text=body.decode(), custody="provided", limitations=(
            "GENERATED_FTSHARE_ADOPTION_CONTROL_NOT_ORIGINAL_PROVIDER_RESPONSE",
            "FOUR_ORIGINAL_CAPTURES_THREE_PRICE_SERIES_FULL_LEGACY_SCOPE_RETAINED",
            "ZERO_NEW_SOURCE_REQUESTS_NO_BATCH_QUALIFICATION_OR_FUTURE_ALLOWANCE",
            "EXACT_ORIGINAL_STUDY_ARCHIVE_RESTORED_WITHOUT_ACTIONS_ACCESS",
            "EXACT_LEGACY_REFERENCE_PRESERVES_OLD_SLOTS_AND_SPENT_SOURCE_ALLOWANCE",
            "RAW_AND_PROVIDER_FORWARD_WHOLE_REQUESTS_NOT_RELAY_OR_POINT_IN_TIME_FACTORS",
            "SOURCE_RETENTION_REQUIRES_CALLER_AUTHORITY_NOT_PUBLIC_REDISTRIBUTION",
        ) if adopted else (
            "GENERATED_FTSHARE_COLLECTION_CONTROL_NOT_ORIGINAL_PROVIDER_RESPONSE",
            "EXACT_FTSHARE_PREDECESSOR_AND_RETAINED_V1_QUALIFICATION",
            "INHERITED_PRICE_OWNERS_AND_FAILURES_SPEND_NO_NEW_SOURCE_ALLOWANCE",
            "ONE_ATTEMPT_PER_NEW_SINGLE_REQUEST_NO_REPLAY_BY_GENERATION_CHANGE",
            "RETAINED_PROVIDER_HOLD_REQUIRES_EXPLICIT_EXTERNAL_REVIEW_NO_AUTOMATIC_UNLOCK",
            "RAW_AND_PROVIDER_FORWARD_WHOLE_REQUESTS_NOT_RELAY_OR_POINT_IN_TIME_FACTORS",
            "SOURCE_RETENTION_REQUIRES_CALLER_AUTHORITY_NOT_PUBLIC_REDISTRIBUTION",
        ) if single else LIMITATIONS)


def _manifest(reference, registry, store):
    ref = _ref(reference, store.backend.repository)
    _, stored = _read_source(store, ref)
    content = _document(stored["data"].encode())
    require(isinstance(content, dict) and set(content) == {"schema", "registry", "predecessor", "collection"}
            and content["schema"] == SCHEMA and content["registry"] == registry.snapshot,
            "FTSHARE_COLLECTION_MANIFEST")
    checked = validate_ftshare_collection(content["collection"])
    _ref(content["predecessor"], store.backend.repository)
    _ref(checked["plan"]["legacy_reference"], store.backend.repository)
    require(encoded({**stored["manifest"]["input"], "text": stored["data"]}) == encoded(asdict(_source(content))),
            "FTSHARE_COLLECTION_MANIFEST_SOURCE_CHANGED")
    return ref, {**content, "collection": checked}


def _inventory(store, collection_id, generation):
    """The immutable ordinal namespace exposes forks before another reservation.

    This is an index over ordinary SourceInput paths, not a mutable pointer or
    another store. A missing or duplicated ordinal never yields a guessed tip.
    """
    prefix = "source-views/" + _source_id(collection_id, generation)
    snapshot = store.backend.head()
    paths = [path for path in store.backend.paths(snapshot, "source-views/")
             if path.startswith(prefix) and path.endswith("/manifest.json")]
    require(len(paths) <= MAX_ROOTS, "FTSHARE_COLLECTION_CHAIN_BOUND")
    if not paths:
        return snapshot, []
    numbered = {}
    for path in paths:
        match = re.fullmatch(re.escape(prefix) + r"r([0-9]{3})/[a-f0-9]{64}/manifest\.json", path)
        require(match is not None, "FTSHARE_COLLECTION_CHAIN_PATH")
        number = int(match[1])
        require(number < MAX_ROOTS and number not in numbered, "FTSHARE_COLLECTION_CHAIN_AMBIGUOUS")
        numbered[number] = path
    require(set(numbered) == set(range(max(numbered) + 1)), "FTSHARE_COLLECTION_CHAIN_GAP")
    return snapshot, [numbered[number] for number in range(len(numbered))]


def _tip(store, registry, collection_id, generation):
    snapshot, paths = _inventory(store, collection_id, generation)
    if not paths:
        return None, None
    path = paths[-1]
    original = store.backend.last_change(snapshot, path)
    ref = SourceRef(store.backend.reference(original, path))
    exact, body = _manifest(ref, registry, store)
    plan = body["collection"]["plan"]
    require(body["collection"]["revision"] == len(paths) - 1
            and plan["collection_id"] == collection_id and plan["generation"] == generation,
            "FTSHARE_COLLECTION_CHAIN_PLAN_CHANGED")
    return exact, body


def _chain(store, registry, collection_id, generation):
    """Full restore proves every exact edge once, using one pinned read scope."""
    snapshot, paths = _inventory(store, collection_id, generation)
    if not paths:
        return None, None
    nodes, plan, total = {}, None, 0
    for path in paths:
        original = store.backend.last_change(snapshot, path)
        ref = SourceRef(store.backend.reference(original, path))
        exact, body = _manifest(ref, registry, store)
        total += len(encoded(body))
        require(total <= MAX_CHAIN_BYTES, "FTSHARE_COLLECTION_CHAIN_BOUND")
        current = body["collection"]
        if plan is None:
            plan = current["plan"]
        require(current["plan"] == plan and plan["collection_id"] == collection_id
                and plan["generation"] == generation, "FTSHARE_COLLECTION_CHAIN_PLAN_CHANGED")
        current["plan"] = plan  # Cache one shared immutable plan, not N duplicated symbol inventories.
        nodes[path] = {"reference": exact, "parent": body["predecessor"], "collection": current}
    parents = set()
    initial = 0
    for node in nodes.values():
        current = node["collection"]
        if current["revision"] == 0:
            require(node["parent"] == _genesis_parent(plan), "FTSHARE_COLLECTION_LEGACY_PREDECESSOR_CHANGED")
            initial += 1
            continue
        parent_path = SourceRef.from_dict(node["parent"]).manifest.path
        require(parent_path in nodes and nodes[parent_path]["reference"] == node["parent"],
                "FTSHARE_COLLECTION_PREDECESSOR_NOT_ORIGINAL")
        _transition(nodes[parent_path]["collection"], current)
        parents.add(parent_path)
    tips = set(nodes) - parents
    require(initial == 1 and len(tips) == 1, "FTSHARE_COLLECTION_CHAIN_AMBIGUOUS")
    tip = nodes[next(iter(tips))]
    require(tip["collection"]["revision"] + 1 == len(nodes), "FTSHARE_COLLECTION_CHAIN_AMBIGUOUS")
    return tip["reference"], tip["collection"]


def latest_ftshare_reference(store, registry, *, collection_id, generation):
    """Locate the original current root; normal restore proves its whole chain."""
    with market_read_scope(store) as reader:
        return _tip(reader, registry, collection_id, generation)[0]


def restore_ftshare_batch(reference, store, *, request):
    """Authenticate the exact original SourceInput and requalify its UTF-8 body."""
    with market_read_scope(store) as reader:
        ref = _ref(reference, reader.backend.repository)
        _, stored = _read_source(reader, ref)
        require(isinstance(request, dict) and isinstance(request.get("parameters"), dict),
                "FTSHARE_COLLECTION_BATCH_REQUEST")
        params = request["parameters"]
        require(set(params) == {"symbols", "interval_unit", "adjust_kind", "since", "until"},
                "FTSHARE_COLLECTION_BATCH_REQUEST")
        capture = capture_batch(stored["data"].encode(), symbols=params["symbols"], since=params["since"],
            until=params["until"], adjust_kind=params["adjust_kind"],
            acquired_at=stored["manifest"]["input"]["acquired_at"])
        require(capture["request"] == request, "FTSHARE_COLLECTION_BATCH_REQUEST_CHANGED")
        expected = source_for_capture(capture)
        require(encoded({**stored["manifest"]["input"], "text": stored["data"]}) == encoded(asdict(expected)),
                "FTSHARE_COLLECTION_BATCH_SOURCE_CHANGED")
        return capture


def retain_ftshare_batch(capture, store, *, fresh_reader, custody_authorized=False,
                        public_rights_confirmed=False):
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    source = source_for_capture(capture)
    reader = fresh_market_reader(store, fresh_reader)
    reference = _put(store, source)
    restored = restore_ftshare_batch(reference, reader, request=capture["request"])
    require(encoded(restored) == encoded(capture), "FTSHARE_COLLECTION_BATCH_READBACK_CHANGED")
    return {"status": "stored-and-independently-restored-ftshare-batch",
        "reference": _ref(reference, store.backend.repository), "source_id": source.source_id,
        "wire_sha256": capture["wire_sha256"], "restored": restored}


def restore_ftshare_collection(reference, registry, store):
    """Restore its fixed legacy context; individual price slots load on demand."""
    require(isinstance(registry, RelayRegistry), "FTSHARE_COLLECTION_REGISTRY_REQUIRED")
    with market_read_scope(store) as reader:
        ref, content = _manifest(reference, registry, reader)
        collection = content["collection"]
        plan = collection["plan"]
        if plan["schema"] == SINGLE_PLAN_SCHEMA:
            return _restore_single_collection(ref, content, registry, reader)
        context = _legacy_context(plan["legacy_reference"], registry, reader,
                                  calendar_references=plan["calendar_references"])
        if plan["schema"] == ADOPTION_PLAN_SCHEMA:
            _restore_adoption_inputs(plan, context, registry, reader)
            expected = _adoption_plan(context, plan["adoption"], collection_id=plan["collection_id"],
                generation=plan["generation"], created_at=plan["created_at"])
        else:
            expected = _plan(context, collection_id=plan["collection_id"], generation=plan["generation"],
                             created_at=plan["created_at"])
        require(plan == expected, "FTSHARE_COLLECTION_RESTORED_CONTEXT_CHANGED")
        # Prove this exact version is on the original single finite chain. A
        # newer tip does not substitute its data for the requested old version.
        _, tip = _chain(reader, registry, plan["collection_id"], plan["generation"])
        require(tip is not None and tip["revision"] >= collection["revision"],
                "FTSHARE_COLLECTION_CHAIN_REQUIRED")
        path = SourceRef.from_dict(ref).manifest.path
        original = reader.backend.last_change(reader.backend.head(), path)
        require(_ref(SourceRef(reader.backend.reference(original, path)), reader.backend.repository) == ref,
                "FTSHARE_COLLECTION_REFERENCE_NOT_ORIGINAL")
        for unit in collection["units"].values():
            if unit["reference"] is not None:
                _ref(unit["reference"], reader.backend.repository)
        return {"status": "restored-ftshare-collection-manifest", "reference": ref,
            "predecessor": content["predecessor"], "collection": collection, "context": context,
            "source_requests": 0, "batches_restored": False,
            "legacy_allowance_reopened": False, "daily_generation_advance": False}


def retain_ftshare_collection(collection, registry, store, *, fresh_reader, predecessor=None,
                             custody_authorized=False, public_rights_confirmed=False):
    """Retain one state edge with independent proof; unknown writes stop the host.

    Reinvoking the exact state reconciles its original manifest. Neither a stale
    predecessor nor a restarted pending state creates another request allowance.
    """
    visibility = authorize_market_custody(store, custody_authorized=custody_authorized,
                                         public_rights_confirmed=public_rights_confirmed)
    require(visibility in ("private", "synthetic"), "FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED")
    checked = validate_ftshare_collection(collection)
    plan = checked["plan"]
    reader = fresh_market_reader(store, fresh_reader)
    with market_read_scope(reader) as grouped:
        previous_ref, previous_body = _tip(grouped, registry, plan["collection_id"], plan["generation"])
        previous = None if previous_body is None else previous_body["collection"]
        if previous is not None and previous == checked:
            return {"status": "stored-and-independently-restored-ftshare-collection",
                "reference": previous_ref,
                "source_id": _source_id(plan["collection_id"], plan["generation"], checked["revision"]),
                "restored": {"reference": previous_ref, "collection": previous,
                             "context_restored": False, "batches_restored": False}}
        if previous is None:
            require((predecessor is None or plan["schema"] == SINGLE_PLAN_SCHEMA
                     and _ref(predecessor, store.backend.repository) == plan["base_reference"])
                    and checked["revision"] == 0
                    and checked["updated_at"] == plan["created_at"], "FTSHARE_COLLECTION_INITIAL_STATE")
            if plan["schema"] == SINGLE_PLAN_SCHEMA:
                base = restore_ftshare_collection(plan["base_reference"], registry, grouped)
                _single_write_frontier(base, registry, grouped, generation=plan["generation"])
                context = _single_context(base, registry, grouped, start=plan["start"], end=plan["end"],
                                          calendar_references=plan["calendar_references"])
                expected = _single_plan(base, context, collection_id=plan["collection_id"],
                    generation=plan["generation"], created_at=plan["created_at"],
                    targets=plan["targets"], source_budget=plan["source_budget"])
                _single_inherited_sources(expected, grouped)
            else:
                context = _legacy_context(plan["legacy_reference"], registry, grouped,
                                          calendar_references=plan["calendar_references"])
            if plan["schema"] == ADOPTION_PLAN_SCHEMA:
                _restore_adoption_inputs(plan, context, registry, grouped)
                expected = _adoption_plan(context, plan["adoption"], collection_id=plan["collection_id"],
                    generation=plan["generation"], created_at=plan["created_at"])
            elif plan["schema"] != SINGLE_PLAN_SCHEMA:
                expected = _plan(context, collection_id=plan["collection_id"], generation=plan["generation"],
                                 created_at=plan["created_at"])
            require(plan == expected, "FTSHARE_COLLECTION_RESTORED_CONTEXT_CHANGED")
            parent = _ref(_genesis_parent(plan), store.backend.repository)
        else:
            require(predecessor is not None and _ref(predecessor, store.backend.repository) == previous_ref,
                    "FTSHARE_COLLECTION_STALE_PREDECESSOR")
            _transition(previous, checked)
            parent = previous_ref
            for slot, unit in checked["units"].items():
                if unit["status"] == "complete" and previous["units"][slot]["status"] != "complete":
                    loader = restore_single if plan["schema"] == SINGLE_PLAN_SCHEMA else restore_ftshare_batch
                    loader(unit["reference"], grouped, request=plan["requests"][slot])
    content = {"schema": SCHEMA, "registry": deepcopy(registry.snapshot),
               "predecessor": parent, "collection": checked}
    source = _source(content)
    reference = _put(store, source)
    # Verify the new control with a genuinely fresh Store. Re-fetching the same
    # old masters/calendars and all settled batches here would multiply source
    # restoration cost by every state edge. The ordinary reader proves them.
    with market_read_scope(fresh_market_reader(store, fresh_reader)) as grouped:
        exact, restored_body = _manifest(reference, registry, grouped)
        require(encoded(restored_body) == encoded(content), "FTSHARE_COLLECTION_READBACK_CHANGED")
        if checked["revision"]:
            _, original_previous = _manifest(parent, registry, grouped)
            _transition(original_previous["collection"], restored_body["collection"])
        else:
            require(restored_body["predecessor"] == _genesis_parent(plan),
                    "FTSHARE_COLLECTION_LEGACY_PREDECESSOR_CHANGED")
    restored = {"reference": exact, "collection": restored_body["collection"],
                "context_restored": False, "batches_restored": False}
    return {"status": "stored-and-independently-restored-ftshare-collection",
        "reference": _ref(reference, store.backend.repository), "source_id": source.source_id,
        "restored": restored}


def _adoption_plan(context, adoption, *, collection_id, generation, created_at):
    plan = _plan(context, collection_id=collection_id, generation=generation, created_at=created_at)
    plan.update(schema=ADOPTION_PLAN_SCHEMA, batch_size=None, source_budget=0,
                requests=kernel_price_requests(), adoption=deepcopy(adoption))
    return plan


def _validate_adoption_collection(collection):
    """An immutable retained-input genesis never reserves a new source request."""
    require(set(collection) == {"schema", "plan", "revision", "units", "reserved_requests", "source_hold",
        "updated_at"} and collection["schema"] == STATE_SCHEMA, "FTSHARE_ADOPTION_STATE")
    plan = collection["plan"]
    require(set(plan) == {"schema", "collection_id", "generation", "created_at", "legacy_reference", "legacy",
        "start", "end", "calendar_end", "since", "until", "master_references", "calendar_references",
        "legacy_calendar_references", "baseline_daily_reference", "sessions", "symbols", "eligible_symbols",
        "quarantined_symbols", "batch_size", "source_budget", "requests", "adoption"}, "FTSHARE_ADOPTION_PLAN")
    identifier(plan["collection_id"]); identifier(plan["generation"])
    require(plan["collection_id"].startswith("ftshare-") and plan["start"] == "20260701"
        and plan["end"] == "20261016" and (plan["since"], plan["until"]) == _date_bounds(plan["start"], plan["end"])
        and plan["batch_size"] is None and type(plan["source_budget"]) is int and plan["source_budget"] == 0
        and plan["requests"] == kernel_price_requests(), "FTSHARE_ADOPTION_REQUESTS")
    for field in ("symbols", "eligible_symbols", "quarantined_symbols"):
        values = plan[field]
        require(isinstance(values, list) and len(values) <= 10000 and values == sorted(set(values))
            and all(isinstance(value, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value) for value in values),
            "FTSHARE_ADOPTION_SCOPE")
    require(not set(plan["eligible_symbols"]) & set(plan["quarantined_symbols"])
        and set(plan["symbols"]) == set(plan["eligible_symbols"]) | set(plan["quarantined_symbols"])
        and {"000001.SZ", "600000.SH"} <= set(plan["eligible_symbols"]), "FTSHARE_ADOPTION_SCOPE")
    evidence = plan["adoption"]
    require(isinstance(evidence, dict) and set(evidence) == {"proof", "evidence_reference", "capture_references",
        "historical_source_requests", "prior_source_requests", "cumulative_source_requests"}, "FTSHARE_ADOPTION_EVIDENCE")
    validate_adoption_proof(evidence["proof"])
    require(encoded({key: evidence[key] for key in ("historical_source_requests", "prior_source_requests",
        "cumulative_source_requests")}) == encoded({"historical_source_requests": 4,
        "prior_source_requests": 2, "cumulative_source_requests": 6}), "FTSHARE_ADOPTION_HISTORICAL_SPENDING")
    SourceRef.from_dict(evidence["evidence_reference"])
    references = evidence["capture_references"]
    require(isinstance(references, dict) and set(references) == set(ROLES)
        and len({encoded(reference) for reference in references.values()}) == 4, "FTSHARE_ADOPTION_CAPTURE_REFERENCES")
    for reference in references.values():
        SourceRef.from_dict(reference)
    expected_units = {role: {"status": "complete", "request_count": 0, "reference": references[role],
                            "reason_code": None} for role in PRICE_ROLES}
    require(encoded(collection["units"]) == encoded(expected_units)
        and type(collection["revision"]) is int and collection["revision"] == 0
        and type(collection["reserved_requests"]) is int and collection["reserved_requests"] == 0
        and collection["source_hold"] is None, "FTSHARE_ADOPTION_ZERO_REQUEST_GENESIS")
    _clock(plan["created_at"])
    require(collection["updated_at"] == plan["created_at"]
        and len(encoded(collection)) <= MAX_ROOT_BYTES - 16384, "FTSHARE_ADOPTION_STATE_BOUND")
    return deepcopy(collection)


def _verify_adoption_context(checked, context, registry, store):
    """Recreate the study's calendar and decimal baseline from original Store inputs."""
    selected = context["eligible_symbols"]
    require({"000001.SZ", "600000.SH"} <= set(selected), "FTSHARE_ADOPTION_REQUIRED_SECURITIES")
    codes = sorted([code for code in selected if code.endswith(".SH")][:50]
        + [code for code in selected if code.endswith(".SZ")][:50])
    baseline = {"reference": context["reference"], "master_references": context["master_references"],
        "scope_count": len(context["scope_symbols"]), "symbols": codes,
        "daily_reference": context["baseline_daily_reference"], "daily": context["baseline_daily"]}
    require(encoded(checked["documents"]["baseline.json"]) == encoded(baseline),
            "FTSHARE_ADOPTION_BASELINE_CHANGED")
    original = restore_market_query(context["baseline_daily_reference"], registry, store)
    prices = {}
    for ref in original["page_references"]:
        _, stored = _read_source(store, ref)
        page = json.loads(stored["data"], parse_float=str)["page"]["table"]
        for values in page["items"]:
            row = dict(zip(page["fields"], values, strict=True))
            code = row["ts_code"]
            if code in {"000001.SZ", "600000.SH"}:
                require(code not in prices and row["trade_date"] == "20261016",
                        "FTSHARE_ADOPTION_BASELINE_PRICE_IDENTITY")
                value = Decimal(str(row["close"]))
                require(value.is_finite() and value > 0, "FTSHARE_ADOPTION_BASELINE_PRICE")
                prices[code] = str(value)
    require(set(prices) == {"000001.SZ", "600000.SH"}, "FTSHARE_ADOPTION_BASELINE_PRICES_REQUIRED")
    verification = {"close": prices, "sessions": context["sessions"],
        "daily_reference": context["baseline_daily_reference"], "page_references": original["page_references"],
        "calendar_references": context["calendar_references"], "scope_count": len(context["scope_symbols"])}
    require(encoded(checked["documents"]["verification.json"]) == encoded(verification),
            "FTSHARE_ADOPTION_VERIFICATION_CHANGED")


def _restore_adoption_inputs(plan, context, registry, store):
    evidence = plan["adoption"]
    checked = restore_kernel_evidence(evidence["evidence_reference"], store)
    require(checked["proof"] == evidence["proof"], "FTSHARE_ADOPTION_PROOF_CHANGED")
    _verify_adoption_context(checked, context, registry, store)
    for role in ROLES:
        original = checked["captures"][role]
        restored = restore_single(evidence["capture_references"][role], store, request=original["request"])
        require(encoded(restored) == encoded(original), "FTSHARE_ADOPTION_CAPTURE_CHANGED")
    return checked


def adopt_ftshare_kernel(archive, proof, legacy_reference, registry, store, *, collection_id,
                        generation, created_at, fresh_reader, custody_authorized=False,
                        public_rights_confirmed=False):
    """Adopt four existing originals and three price owners; issue no source HTTP.

    The existing Store submission journal owns write reconciliation. Re-entry
    returns the original genesis (including its original creation clock), or
    completes the same deterministic inputs if no root was published yet.
    """
    visibility = authorize_market_custody(store, custody_authorized=custody_authorized,
                                         public_rights_confirmed=public_rights_confirmed)
    require(visibility in ("private", "synthetic"), "FTSHARE_COLLECTION_PRIVATE_CUSTODY_REQUIRED")
    require(isinstance(registry, RelayRegistry), "FTSHARE_COLLECTION_REGISTRY_REQUIRED")
    identifier(collection_id); identifier(generation); _clock(created_at)
    checked = read_kernel_archive(archive, proof)
    require(collection_id.startswith("ftshare-")
        and _clock(created_at) >= _clock(checked["documents"]["result.json"]["completed_at"]),
        "FTSHARE_ADOPTION_CREATION_CONTEXT")

    def result(reference, collection):
        evidence = collection["plan"]["adoption"]
        return {"status": "stored-and-independently-restored-ftshare-adoption", "reference": deepcopy(reference),
            "collection": deepcopy(collection), "evidence_reference": deepcopy(evidence["evidence_reference"]),
            "capture_references": deepcopy(evidence["capture_references"]), "source_requests": 0,
            "source_budget": 0, "reserved_requests": 0, "historical_source_requests": 4,
            "cumulative_source_requests": 6,
            "reading_request": {"reference": deepcopy(reference), "offset": 0, "limit": 50}}

    with market_read_scope(fresh_market_reader(store, fresh_reader)) as reader:
        reference = latest_ftshare_reference(reader, registry, collection_id=collection_id, generation=generation)
        if reference is not None:
            restored = restore_ftshare_collection(reference, registry, reader)
            existing = restored["collection"]
            require(existing["plan"]["schema"] == ADOPTION_PLAN_SCHEMA
                and existing["plan"]["legacy_reference"] == _ref(legacy_reference, store.backend.repository)
                and existing["plan"]["adoption"]["proof"] == checked["proof"],
                "FTSHARE_ADOPTION_EXISTING_COLLECTION_CHANGED")
            return result(reference, existing)
        context = _legacy_context(legacy_reference, registry, reader)
        _verify_adoption_context(checked, context, registry, reader)
    try:
        evidence_ref = _put(store, source_for_kernel_evidence(archive, proof))
        restored_evidence = restore_kernel_evidence(evidence_ref, fresh_market_reader(store, fresh_reader))
        require(restored_evidence["archive"] == archive, "FTSHARE_ADOPTION_ARCHIVE_READBACK_CHANGED")
        references = {}
        for role in ROLES:
            capture = checked["captures"][role]
            reference = _put(store, source_for_single_capture(capture))
            restored = restore_single(reference, fresh_market_reader(store, fresh_reader), request=capture["request"])
            require(encoded(restored) == encoded(capture), "FTSHARE_ADOPTION_CAPTURE_READBACK_CHANGED")
            references[role] = _ref(reference, store.backend.repository)
        adoption = {"proof": checked["proof"], "evidence_reference": _ref(evidence_ref, store.backend.repository),
            "capture_references": references, "historical_source_requests": 4,
            "prior_source_requests": 2, "cumulative_source_requests": 6}
        plan = _adoption_plan(context, adoption, collection_id=collection_id, generation=generation, created_at=created_at)
        collection = {"schema": STATE_SCHEMA, "plan": plan, "revision": 0,
            "units": {role: {"status": "complete", "request_count": 0, "reference": references[role],
                             "reason_code": None} for role in PRICE_ROLES},
            "reserved_requests": 0, "source_hold": None, "updated_at": created_at}
        saved = retain_ftshare_collection(collection, registry, store, fresh_reader=fresh_reader,
            custody_authorized=True, public_rights_confirmed=False)
        restored = restore_ftshare_collection(saved["reference"], registry, fresh_market_reader(store, fresh_reader))
        return result(saved["reference"], restored["collection"])
    except UnknownWrite:
        raise
    except Exception as exc:
        raise UnknownWrite("FTSHARE_ADOPTION_PERSISTENCE_UNCONFIRMED") from exc


def _genesis_parent(plan):
    return plan["base_reference"] if plan["schema"] == SINGLE_PLAN_SCHEMA else plan["legacy_reference"]


def _single_period(start, end):
    require(isinstance(start, str) and isinstance(end, str)
            and re.fullmatch(r"[0-9]{8}", start) and re.fullmatch(r"[0-9]{8}", end)
            and start <= end, "FTSHARE_SINGLE_RANGE")
    try:
        since, until = _date_bounds(start, end)
    except ValueError:
        require(False, "FTSHARE_SINGLE_RANGE")
    require(until - since + 1 <= 119 * 86400000, "FTSHARE_SINGLE_RANGE_BOUND")
    return since, until


def _single_targets(targets):
    require(isinstance(targets, list) and 1 <= len(targets) <= MAX_REQUESTS,
            "FTSHARE_SINGLE_TARGETS")
    checked = []
    for target in targets:
        require(isinstance(target, dict) and set(target) == {"symbol", "adjust_kind"}
                and isinstance(target["symbol"], str)
                and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", target["symbol"])
                and isinstance(target["adjust_kind"], str)
                and target["adjust_kind"] in ("none", "forward"), "FTSHARE_SINGLE_TARGETS")
        checked.append(deepcopy(target))
    keys = [(target["symbol"], target["adjust_kind"]) for target in checked]
    require(len(set(keys)) == len(keys), "FTSHARE_SINGLE_DUPLICATE_TARGET")
    return sorted(checked, key=lambda target: (target["symbol"], target["adjust_kind"]))


def _single_base_ready(base):
    previous = base["collection"]
    require(previous["plan"]["schema"] in (ADOPTION_PLAN_SCHEMA, SINGLE_PLAN_SCHEMA),
            "FTSHARE_SINGLE_RETAINED_V1_QUALIFICATION_REQUIRED")
    require(previous["source_hold"] is None and not any(
        unit["reason_code"] in SOURCE_HOLD_REASONS for unit in previous["units"].values()),
        "FTSHARE_COLLECTION_SOURCE_HOLD")
    require(all(unit["status"] in ("complete", "failed") for unit in previous["units"].values()),
            "FTSHARE_SINGLE_BASE_NOT_TERMINAL")


def _single_context(base, registry, store, *, start, end, calendar_references=None):
    """Use actual bounded calendar queries, preserving the original price baseline."""
    from decision_kernel.application.market_collection_inputs import _fresh_calendar
    _single_base_ready(base)
    _single_period(start, end)
    prior = base["collection"]["plan"]
    require(start >= prior["start"] and end >= prior["end"], "FTSHARE_SINGLE_RANGE_ROLLBACK")
    context = deepcopy(base["context"])
    if calendar_references is None:
        calendar_references = context["calendar_references"]
    require(isinstance(calendar_references, dict) and set(calendar_references) == set(VENUES),
            "FTSHARE_COLLECTION_CALENDAR_REFERENCES")
    references = {venue: _ref(calendar_references[venue], store.backend.repository) for venue in VENUES}
    through = set()
    for venue in VENUES:
        previous = context["calendar_results"][venue]["use"]["parameters"]
        if references[venue] != context["calendar_references"][venue]:
            result = restore_market_query(references[venue], registry, store)["result"]
            fresh = _fresh_calendar(result, registry, venue)
            params = result["use"]["parameters"]
            overlap_start = max(previous["start_date"], params["start_date"])
            overlap_end = min(previous["end_date"], params["end_date"])
            require(overlap_start <= overlap_end and params["end_date"] >= previous["end_date"],
                    "FTSHARE_SINGLE_CALENDAR_CONTINUITY")
            def overlap(days):
                return [day for day in days if overlap_start <= day.replace("-", "") <= overlap_end]
            require(overlap(fresh["sessions"]) == overlap(context["calendars"][venue]),
                    "FTSHARE_COLLECTION_CALENDAR_HISTORY_CHANGED")
            context["calendar_results"][venue] = result
            context["calendar_references"][venue] = references[venue]
            context["calendars"][venue] = fresh["sessions"]
        else:
            params = previous
        require(params["start_date"] <= start <= end <= params["end_date"],
                "FTSHARE_SINGLE_CALENDAR_COVERAGE_REQUIRED")
        through.add(params["end_date"])
        context["sessions"][venue] = [day for day in context["calendars"][venue]
            if start <= day.replace("-", "") <= end]
    require(len(through) == 1, "FTSHARE_COLLECTION_CALENDAR_END_MISMATCH")
    context.update(start=start, end=end, calendar_end=next(iter(through)))
    return context


def _single_plan(base, context, *, collection_id, generation, created_at, targets, source_budget):
    from decision_kernel.application.market_collection_inputs import _reading_clock
    _single_base_ready(base)
    prior = base["collection"]
    require(collection_id == prior["plan"]["collection_id"] and generation != prior["plan"]["generation"],
            "FTSHARE_SINGLE_BASE_IDENTITY")
    require(_clock(created_at) >= _clock(prior["updated_at"]), "FTSHARE_COLLECTION_CLOCK")
    final = datetime.strptime(context["end"], "%Y%m%d").date().isoformat()
    _reading_clock(created_at, final)
    targets = _single_targets(targets)
    require(type(source_budget) is int and source_budget == len(targets), "FTSHARE_COLLECTION_BUDGET_CHANGED")
    require(all(target["symbol"] in context["eligible_symbols"] for target in targets),
            "FTSHARE_SINGLE_TARGET_OUTSIDE_ELIGIBLE_SCOPE")
    requests, inherited = {}, {}
    for prior_slot, request in prior["plan"]["requests"].items():
        params = request["parameters"]
        slot = params["adjust_kind"] + ":" + params["symbol"]
        require(slot not in requests, "FTSHARE_SINGLE_DUPLICATE_OWNER")
        requests[slot] = deepcopy(request)
        inherited[slot] = {**deepcopy(prior["units"][prior_slot]), "request_count": 0}
    since, until = _single_period(context["start"], context["end"])
    new_slots = []
    for target in targets:
        slot = target["adjust_kind"] + ":" + target["symbol"]
        request = single_request(**target, since=since, until=until)
        require(slot not in requests or requests[slot] != request, "FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED")
        requests[slot] = request
        inherited.pop(slot, None)
        new_slots.append(slot)
    require(len(requests) <= MAX_REQUESTS, "FTSHARE_COLLECTION_REQUEST_BOUND")
    plan = _plan(context, collection_id=collection_id, generation=generation, created_at=created_at)
    plan.update(schema=SINGLE_PLAN_SCHEMA, batch_size=None, source_budget=source_budget,
        requests=requests, base_reference=deepcopy(base["reference"]), targets=targets,
        new_slots=new_slots, inherited_units=inherited, adoption=deepcopy(prior["plan"]["adoption"]),
        prior_generation_reserved_requests=(prior["plan"].get("prior_generation_reserved_requests", 0)
                                            + prior["reserved_requests"]))
    return plan


def _single_write_frontier(base, registry, store, *, generation):
    """Existing immutable genesis records are the index; there is no new pointer."""
    _single_base_ready(base)
    plan = base["collection"]["plan"]
    tip, _ = _tip(store, registry, plan["collection_id"], plan["generation"])
    require(tip == base["reference"], "FTSHARE_SINGLE_STALE_BASE")
    snapshot = store.backend.head()
    paths = [path for path in store.backend.paths(snapshot, "source-views/")
        if re.fullmatch(r"source-views/ftshare-collection-[a-f0-9]{32}-r000/[a-f0-9]{64}/manifest\.json", path)]
    require(len(paths) <= MAX_STORE_VERSIONS, "STORE_VERSION_LIMIT_EXCEEDED")
    for path in paths:
        original = store.backend.last_change(snapshot, path)
        _, body = _manifest(SourceRef(store.backend.reference(original, path)), registry, store)
        candidate = body["collection"]["plan"]
        if candidate["schema"] == SINGLE_PLAN_SCHEMA and candidate["collection_id"] == plan["collection_id"] \
                and candidate["base_reference"] == base["reference"]:
            require(candidate["generation"] == generation, "FTSHARE_SINGLE_BASE_ALREADY_HAS_SUCCESSOR")


def _single_inherited_sources(plan, store):
    for slot, unit in plan["inherited_units"].items():
        if unit["status"] == "complete":
            restore_single(unit["reference"], store, request=plan["requests"][slot])


def plan_ftshare_single_collection(base_reference, registry, store, *, collection_id,
                                  generation, created_at, start, end, targets, source_budget,
                                  calendar_references=None):
    """Plan bounded new v1 calls on the exact terminal FTShare frontier; no provider I/O."""
    require(isinstance(registry, RelayRegistry), "FTSHARE_COLLECTION_REGISTRY_REQUIRED")
    with market_read_scope(store) as reader:
        base = restore_ftshare_collection(base_reference, registry, reader)
        _single_write_frontier(base, registry, reader, generation=generation)
        context = _single_context(base, registry, reader, start=start, end=end,
                                  calendar_references=calendar_references)
        plan = _single_plan(base, context, collection_id=collection_id, generation=generation,
            created_at=created_at, targets=targets, source_budget=source_budget)
        _single_inherited_sources(plan, reader)
        units = {**deepcopy(plan["inherited_units"]), **{slot: {"status": "pending", "request_count": 0,
            "reference": None, "reason_code": None} for slot in plan["new_slots"]}}
        return validate_ftshare_collection({"schema": STATE_SCHEMA, "plan": plan, "revision": 0,
            "units": units, "reserved_requests": 0, "source_hold": None, "updated_at": created_at})


def _validate_single_collection(collection):
    require(set(collection) == {"schema", "plan", "revision", "units", "reserved_requests", "source_hold",
        "updated_at"} and collection["schema"] == STATE_SCHEMA, "FTSHARE_SINGLE_STATE")
    plan = collection["plan"]
    require(set(plan) == {"schema", "collection_id", "generation", "created_at", "legacy_reference", "legacy",
        "start", "end", "calendar_end", "since", "until", "master_references", "calendar_references",
        "legacy_calendar_references", "baseline_daily_reference", "sessions", "symbols", "eligible_symbols",
        "quarantined_symbols", "batch_size", "source_budget", "requests", "base_reference", "targets",
        "new_slots", "inherited_units", "adoption", "prior_generation_reserved_requests"}, "FTSHARE_SINGLE_PLAN")
    identifier(plan["collection_id"]); identifier(plan["generation"])
    require(plan["collection_id"].startswith("ftshare-") and plan["batch_size"] is None
        and type(plan["since"]) is int and type(plan["until"]) is int
        and (plan["since"], plan["until"]) == _single_period(plan["start"], plan["end"]),
        "FTSHARE_SINGLE_REQUESTS")
    targets = _single_targets(plan["targets"])
    require(targets == plan["targets"] and plan["new_slots"] == [target["adjust_kind"] + ":" + target["symbol"]
            for target in targets], "FTSHARE_SINGLE_TARGETS_CHANGED")
    require(type(plan["source_budget"]) is int and plan["source_budget"] == len(targets),
            "FTSHARE_COLLECTION_BUDGET_CHANGED")
    require(type(plan["prior_generation_reserved_requests"]) is int
            and plan["prior_generation_reserved_requests"] >= 0, "FTSHARE_SINGLE_PRIOR_RESERVATIONS")
    for field in ("symbols", "eligible_symbols", "quarantined_symbols"):
        values = plan[field]
        require(isinstance(values, list) and len(values) <= 10000
            and all(isinstance(value, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", value) for value in values)
            and values == sorted(set(values)), "FTSHARE_COLLECTION_SYMBOLS")
    require(not set(plan["eligible_symbols"]) & set(plan["quarantined_symbols"])
        and set(plan["symbols"]) == set(plan["eligible_symbols"]) | set(plan["quarantined_symbols"]),
        "FTSHARE_COLLECTION_SCOPE_CHANGED")
    requests, inherited, units = plan["requests"], plan["inherited_units"], collection["units"]
    require(isinstance(requests, dict) and 1 <= len(requests) <= MAX_REQUESTS
        and set(plan["new_slots"]) <= set(requests)
        and isinstance(inherited, dict) and set(inherited) == set(requests) - set(plan["new_slots"])
        and isinstance(units, dict) and set(units) == set(requests), "FTSHARE_SINGLE_UNITS")
    for slot, request in requests.items():
        require(isinstance(request, dict) and isinstance(request.get("parameters"), dict), "FTSHARE_SINGLE_REQUESTS")
        params = request["parameters"]
        require(set(params) == {"symbol", "interval_unit", "adjust_kind", "since", "until"}, "FTSHARE_SINGLE_REQUESTS")
        expected = single_request(symbol=params["symbol"], adjust_kind=params["adjust_kind"],
                                  since=params["since"], until=params["until"])
        require(request == expected and slot == params["adjust_kind"] + ":" + params["symbol"]
            and params["symbol"] in plan["eligible_symbols"], "FTSHARE_SINGLE_REQUESTS")
        if slot in plan["new_slots"]:
            require((params["since"], params["until"]) == (plan["since"], plan["until"]),
                    "FTSHARE_SINGLE_REQUESTS")
        unit = units[slot]
        require(isinstance(unit, dict) and set(unit) == {"status", "request_count", "reference", "reason_code"}
            and unit["status"] in ("pending", "in-flight", "complete", "failed"), "FTSHARE_COLLECTION_UNIT")
        require(type(unit["request_count"]) is int and unit["request_count"] == (
            0 if slot in inherited or unit["status"] == "pending" else 1), "FTSHARE_COLLECTION_ONE_ATTEMPT_ONLY")
        if slot in inherited:
            require(unit["status"] in ("complete", "failed") and encoded(unit) == encoded(inherited[slot]),
                    "FTSHARE_SINGLE_INHERITED_UNIT_CHANGED")
        require((unit["reference"] is not None) == (unit["status"] == "complete"), "FTSHARE_COLLECTION_UNIT_REFERENCE")
        require((unit["reason_code"] is not None) == (unit["status"] == "failed"), "FTSHARE_COLLECTION_UNIT_REASON")
        if unit["reference"] is not None:
            SourceRef.from_dict(unit["reference"])
        if unit["reason_code"] is not None:
            require(isinstance(unit["reason_code"], str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", unit["reason_code"]),
                    "FTSHARE_COLLECTION_UNIT_REASON")
    attempts = sum(units[slot]["request_count"] for slot in plan["new_slots"])
    settled = sum(units[slot]["status"] in ("complete", "failed") for slot in plan["new_slots"])
    require(type(collection["reserved_requests"]) is int and collection["reserved_requests"] == attempts
        and attempts <= plan["source_budget"] and type(collection["revision"]) is int
        and collection["revision"] == attempts + settled, "FTSHARE_COLLECTION_BUDGET_CHANGED")
    require(sum(unit["status"] == "in-flight" for unit in units.values()) <= 1,
            "FTSHARE_COLLECTION_ONE_INFLIGHT_ONLY")
    require(_clock(collection["updated_at"]) >= _clock(plan["created_at"])
        and (collection["revision"] != 0 or collection["updated_at"] == plan["created_at"]), "FTSHARE_COLLECTION_CLOCK")
    hold = collection["source_hold"]
    if hold is not None:
        require(isinstance(hold, dict) and set(hold) == {"reason_code", "set_at"}
            and isinstance(hold["reason_code"], str) and hold["reason_code"] in SOURCE_HOLD_REASONS
            and hold["set_at"] == collection["updated_at"] and settled > 0
            and not any(unit["status"] == "in-flight" for unit in units.values()), "FTSHARE_COLLECTION_SOURCE_HOLD_INVALID")
    SourceRef.from_dict(plan["base_reference"])
    require(isinstance(plan["adoption"], dict) and "proof" in plan["adoption"], "FTSHARE_SINGLE_QUALIFICATION")
    validate_adoption_proof(plan["adoption"]["proof"])
    require(len(encoded(collection)) <= MAX_ROOT_BYTES - 16384, "FTSHARE_COLLECTION_STATE_BOUND")
    return deepcopy(collection)


def _restore_single_collection(reference, content, registry, store):
    """Walk exact generation parents iteratively, then rebuild each plan once."""
    lineage, seen, total = [], set(), 0
    while content["collection"]["plan"]["schema"] == SINGLE_PLAN_SCHEMA:
        key = encoded(reference)
        require(key not in seen, "FTSHARE_SINGLE_LINEAGE_CYCLE")
        seen.add(key)
        total += len(encoded(content))
        require(total <= MAX_CHAIN_BYTES and len(lineage) < MAX_ROOTS, "FTSHARE_COLLECTION_CHAIN_BOUND")
        lineage.append((reference, content))
        reference, content = _manifest(content["collection"]["plan"]["base_reference"], registry, store)
    require(content["collection"]["plan"]["schema"] == ADOPTION_PLAN_SCHEMA,
            "FTSHARE_SINGLE_RETAINED_V1_QUALIFICATION_REQUIRED")
    base = restore_ftshare_collection(reference, registry, store)
    for reference, content in reversed(lineage):
        collection, plan = content["collection"], content["collection"]["plan"]
        context = _single_context(base, registry, store, start=plan["start"], end=plan["end"],
                                  calendar_references=plan["calendar_references"])
        expected = _single_plan(base, context, collection_id=plan["collection_id"], generation=plan["generation"],
            created_at=plan["created_at"], targets=plan["targets"], source_budget=plan["source_budget"])
        require(plan == expected, "FTSHARE_COLLECTION_RESTORED_CONTEXT_CHANGED")
        _, tip = _chain(store, registry, plan["collection_id"], plan["generation"])
        require(tip is not None and tip["revision"] >= collection["revision"], "FTSHARE_COLLECTION_CHAIN_REQUIRED")
        path = SourceRef.from_dict(reference).manifest.path
        original = store.backend.last_change(store.backend.head(), path)
        require(_ref(SourceRef(store.backend.reference(original, path)), store.backend.repository) == reference,
                "FTSHARE_COLLECTION_REFERENCE_NOT_ORIGINAL")
        for unit in collection["units"].values():
            if unit["reference"] is not None:
                _ref(unit["reference"], store.backend.repository)
        base = {"status": "restored-ftshare-collection-manifest", "reference": reference,
            "predecessor": content["predecessor"], "collection": collection, "context": context,
            "source_requests": 0, "batches_restored": False, "legacy_allowance_reopened": False,
            "daily_generation_advance": plan["end"] != base["collection"]["plan"]["end"]}
    return base


def retain_ftshare_single(capture, store, *, fresh_reader, custody_authorized=False,
                         public_rights_confirmed=False):
    authorize_market_custody(store, custody_authorized=custody_authorized,
                            public_rights_confirmed=public_rights_confirmed)
    source = source_for_single_capture(capture)
    reader = fresh_market_reader(store, fresh_reader)
    reference = _put(store, source)
    restored = restore_single(reference, reader, request=capture["request"])
    require(encoded(restored) == encoded(capture), "FTSHARE_SINGLE_READBACK_CHANGED")
    return {"status": "stored-and-independently-restored-ftshare-single", "reference": _ref(reference, store.backend.repository),
            "source_id": source.source_id, "wire_sha256": capture["wire_sha256"], "restored": restored}
