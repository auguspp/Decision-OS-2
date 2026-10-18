"""Durable SH/SZ daily partitions, using the original Relay and versioned store.

Each sync is one finite work unit. Collection versions retain exact successful
queries; execution checkpoints alone decide whether a pending page may resume.
The collection never creates per-security source captures from whole-market rows.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import re
import time

from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _range, qualified_trade_calendar,
)
from decision_kernel.application.market_security_inputs import MASTER_FIELDS, inspect_security_master
from decision_kernel.application.market_session_inputs import QUOTE_FIELDS, session_quotes
from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS, session_suspensions
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MAX_BYTES, RecordError, SourceRef, StoreUnavailable, digest, encoded, identifier, require, source_time,
)
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError

PLAN_SCHEMA = "ashare-collection-plan-v1"
APIS = ("daily", "adj_factor", "suspend_d")
MAX_DAYS = 119
MAX_UNITS = 3 * MAX_DAYS + 4
MAX_QUERIES_PER_WORK_UNIT = 32
WORK_UNIT_SECONDS = 600
SCOPE = {"exchanges": ["SSE", "SZSE"], "list_status": "L"}
CN = timezone(timedelta(hours=8))


def _reason(exc):
    value = str(exc)
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else "ASHARE_OPERATION_FAILED"


def _date(value):
    return date.fromisoformat(_range(value, value)[0])


def completed_through(now):
    """Conservative end-of-day service: at most the previous Shanghai date."""
    require(isinstance(now, datetime) and now.utcoffset() is not None, "ASHARE_CLOCK_REQUIRED")
    return (now.astimezone(CN).date() - timedelta(days=1)).strftime("%Y%m%d")


def _use(collection_id, api, params, fields, size, registry):
    return validate_plan({"schema": "research-source-plan-v1", "case_id": collection_id,
        "requests": [{"stage": "baseline", "purpose": "Durable SH/SZ daily market collection",
            "api": api, "parameters": {**params, "fields": ",".join(sorted(fields)), "limit": str(size)}}]},
        registry)["requests"][0]


def plan_collection(*, collection_id, generation, start, end, calendar_end, registry,
                    refresh_dates=()):
    """Offline fixed selectors; trading dates are filled only by qualified calendars.

    A generation is an explicit dataset revision. Existing dates remain pinned
    unless listed in refresh_dates; current-L reference snapshots refresh when
    a successor generation is opened. It is not historical-universe rebuilding.
    """
    identifier(collection_id); identifier(generation)
    require(isinstance(registry, RelayRegistry), "ASHARE_REGISTRY_REQUIRED")
    lo, hi = _range(start, end)
    _, through = _range(end, calendar_end)
    require((date.fromisoformat(through) - date.fromisoformat(lo)).days < MAX_DAYS,
            "ASHARE_COLLECTION_RANGE")
    require(isinstance(refresh_dates, (list, tuple)) and len(refresh_dates) <= MAX_DAYS
            and len(set(refresh_dates)) == len(refresh_dates), "ASHARE_REFRESH_DATES")
    refresh = sorted(refresh_dates)
    for day in refresh:
        require(date.fromisoformat(lo) <= _date(day) <= date.fromisoformat(hi), "ASHARE_REFRESH_DATES")
    return {"schema": PLAN_SCHEMA, "collection_id": collection_id, "generation": generation,
        "start": start, "end": end, "calendar_end": calendar_end, "scope": deepcopy(SCOPE),
        "refresh_dates": refresh,
        "calendar_queries": {v: _use(collection_id, "trade_cal",
            {"exchange": v, "start_date": start, "end_date": calendar_end},
            {"exchange", "cal_date", "is_open"}, 5000, registry) for v in EXCHANGES},
        "master_queries": {v: _use(collection_id, "stock_basic",
            {"exchange": v, "list_status": "L"}, MASTER_FIELDS, 500, registry) for v in EXCHANGES},
        "sessions": []}


def validate_collection_plan(plan, registry):
    require(isinstance(plan, dict) and set(plan) == {"schema", "collection_id", "generation", "start",
        "end", "calendar_end", "scope", "refresh_dates", "calendar_queries", "master_queries", "sessions"},
        "ASHARE_COLLECTION_PLAN")
    expected = plan_collection(collection_id=plan["collection_id"], generation=plan["generation"],
        start=plan["start"], end=plan["end"], calendar_end=plan["calendar_end"], registry=registry,
        refresh_dates=plan["refresh_dates"])
    sessions = plan["sessions"]
    require(isinstance(sessions, list) and len(sessions) <= MAX_DAYS and sessions == sorted(set(sessions)),
            "ASHARE_COLLECTION_SESSIONS")
    for day in sessions:
        require(_date(plan["start"]) <= _date(day) <= _date(plan["end"]), "ASHARE_COLLECTION_SESSIONS")
    expected["sessions"] = sessions
    require(encoded(expected) == encoded(plan), "ASHARE_COLLECTION_PLAN_CHANGED")
    return deepcopy(plan)


def unit_request(plan, slot, registry):
    """Only these generated slots may select a source query."""
    if slot.startswith("calendar:") and slot[9:] in EXCHANGES:
        return deepcopy(plan["calendar_queries"][slot[9:]]), 2
    if slot.startswith("master:") and slot[7:] in EXCHANGES:
        return deepcopy(plan["master_queries"][slot[7:]]), 20
    parts = slot.split(":")
    require(len(parts) == 2 and parts[0] in plan["sessions"] and parts[1] in APIS, "ASHARE_COLLECTION_SLOT")
    day, api = parts
    fields = SUSPENSION_FIELDS if api == "suspend_d" else QUOTE_FIELDS[api]
    return _use(plan["collection_id"], api, {"trade_date": day}, fields, 5000, registry), 2


def checkpoint_id(plan, slot):
    # Source query identity is stable across resumptions of this generation.
    return "ashare-" + digest(encoded({"collection_id": plan["collection_id"],
        "generation": plan["generation"], "slot": slot}))[:64]


def _pending():
    return {"query": None, "checkpoint": None, "status": "pending", "reason_code": None}


def _slots(plan):
    # Most recent daily reading is useful while older history is still filling.
    return [*("calendar:" + v for v in EXCHANGES), *("master:" + v for v in EXCHANGES),
            *(d + ":" + api for d in reversed(plan["sessions"]) for api in APIS)]


def _qualified(result, use, plan, registry):
    view = inspect_result(result, registry)
    require(view["complete"], "ASHARE_QUERY_INCOMPLETE")
    require(result["use"] == use, "ASHARE_QUERY_CHANGED")
    api, params = use["api"], use["parameters"]
    if api == "trade_cal":
        return qualified_trade_calendar(result, registry=registry, exchange=params["exchange"],
            start=plan["start"], end=plan["calendar_end"])
    if api == "stock_basic":
        return inspect_security_master({"L": result}, registry=registry, exchange=params["exchange"])
    if api == "suspend_d":
        return session_suspensions(result, registry, params["trade_date"])
    return session_quotes(result, registry, api, params["trade_date"])


def validate_collection(collection, registry):
    require(isinstance(collection, dict) and set(collection) == {"plan", "units", "last_qualified", "attempt"},
            "ASHARE_COLLECTION_SHAPE")
    plan = validate_collection_plan(collection["plan"], registry)
    units = collection["units"]
    require(isinstance(units, dict) and set(units) == set(_slots(plan)) and len(units) <= MAX_UNITS,
            "ASHARE_COLLECTION_UNITS")
    for unit in units.values():
        require(isinstance(unit, dict) and set(unit) == {"query", "checkpoint", "status", "reason_code"}
                and unit["status"] in {"pending", "in-progress", "complete", "paused", "failed", "blocked", "unknown-write"},
                "ASHARE_COLLECTION_UNIT")
        require(unit["reason_code"] is None or isinstance(unit["reason_code"], str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", unit["reason_code"]), "ASHARE_COLLECTION_REASON")
        for key in ("query", "checkpoint"):
            if unit[key] is not None:
                SourceRef.from_dict(unit[key])
        require((unit["query"] is not None) == (unit["status"] == "complete"), "ASHARE_COLLECTION_UNIT_STATE")
    if collection["last_qualified"] is not None:
        SourceRef.from_dict(collection["last_qualified"])
    attempt = collection["attempt"]
    require(isinstance(attempt, dict) and set(attempt) == {"id", "status", "reason_code", "started_at", "updated_at", "source_requests"},
            "ASHARE_COLLECTION_ATTEMPT")
    identifier(attempt["id"])
    require(attempt["status"] in {"planned", "in-progress", "complete", "partial", "blocked"}
            and type(attempt["source_requests"]) is int and attempt["source_requests"] >= 0,
            "ASHARE_COLLECTION_ATTEMPT")
    require(attempt["reason_code"] is None or isinstance(attempt["reason_code"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", attempt["reason_code"]), "ASHARE_COLLECTION_REASON")
    source_time(attempt["started_at"]); source_time(attempt["updated_at"])
    return deepcopy(collection)


def collection_summary(collection, reference=None):
    """No source rows, company names, prices or private source locations."""
    counts = {state: sum(u["status"] == state for u in collection["units"].values())
        for state in ("pending", "in-progress", "complete", "paused", "failed", "blocked", "unknown-write")}
    return {"schema": "ashare-collection-receipt-v1", "collection_id": collection["plan"]["collection_id"],
        "generation": collection["plan"]["generation"], "start": collection["plan"]["start"],
        "end": collection["plan"]["end"], "calendar_end": collection["plan"]["calendar_end"],
        "scope": deepcopy(SCOPE), "session_count": len(collection["plan"]["sessions"]),
        "unit_counts": counts, "latest_attempt": deepcopy(collection["attempt"]),
        "all_partitions_retained": counts["complete"] == len(collection["units"]),
        "trading_interval": "has-market-sessions" if collection["plan"]["sessions"] else "no-derived-market-sessions",
        "has_previous_readable_version": collection["last_qualified"] is not None,
        "reference_sha256": digest(encoded(reference)) if reference is not None else None,
        "source_rows_in_receipt": False,
        "limitations": ["SH_SZ_CURRENT_L_NOT_HISTORICAL_OR_ALL_A_SHARE_UNIVERSE",
            "COLLECTION_COMPLETENESS_NOT_PRICE_CORRECTNESS_OR_LONG_TERM_HEALTH",
            "PREVIOUS_SHANGHAI_DATE_END_OF_DAY_SERVICE_NOT_INTRADAY_LIVE"]}


class CollectionSyncInterrupted(RecordError):
    """No more source or store writes after a persistence failure or unknown write."""
    def __init__(self, receipt, reference):
        super().__init__("ASHARE_COLLECTION_PERSISTENCE_UNCONFIRMED")
        self.receipt, self.reference = receipt, reference


class _PersistenceStopped(RuntimeError):
    pass


def _partition_complete(collection):
    return len(collection["units"]) >= 4 and all(
        u["status"] == "complete" for u in collection["units"].values())


def _new_collection(plan, previous, previous_ref, attempt_id, at, registry):
    plan = validate_collection_plan(plan, registry)
    if previous is not None:
        validate_collection(previous, registry)
        old = previous["plan"]
        require(old["collection_id"] == plan["collection_id"], "ASHARE_COLLECTION_CHANGED")
        if old["generation"] == plan["generation"]:
            require({k: v for k, v in plan.items() if k != "sessions"} ==
                    {k: v for k, v in old.items() if k != "sessions"}, "ASHARE_RESUME_PLAN_CHANGED")
            collection = deepcopy(previous)
        else:
            require(not plan["sessions"], "ASHARE_CALENDAR_DERIVATION_REQUIRED")
            require(_date(plan["end"]) >= _date(old["end"])
                    and _date(plan["calendar_end"]) >= _date(old["calendar_end"]),
                    "ASHARE_COLLECTION_DATE_ROLLBACK")
            require(all(u["status"] in {"complete", "failed"}
                        for u in previous["units"].values()), "ASHARE_RECONCILE_ORIGINAL_GENERATION")
            collection = {"plan": plan, "units": {slot: _pending() for slot in _slots(plan)},
                "last_qualified": previous_ref if _partition_complete(previous) else previous["last_qualified"]}
            # Calendars can be reused only with precisely the same range. New
            # generations refresh current-L masters, keeping their old versions.
            for venue in EXCHANGES:
                slot = "calendar:" + venue
                if old["calendar_queries"][venue] == plan["calendar_queries"][venue]:
                    collection["units"][slot] = deepcopy(previous["units"][slot])
                master_slot = "master:" + venue
                if previous["units"][master_slot]["status"] != "complete":
                    collection["units"][master_slot] = deepcopy(previous["units"][master_slot])
    else:
        require(not plan["sessions"], "ASHARE_CALENDAR_DERIVATION_REQUIRED")
        collection = {"plan": plan, "units": {slot: _pending() for slot in _slots(plan)},
            "last_qualified": None}
    collection["attempt"] = {"id": identifier(attempt_id), "status": "planned", "reason_code": None,
        "started_at": at, "updated_at": at, "source_requests": 0}
    return collection


def sync_collection(*, plan, registry, store, fresh_reader, attempt_id, previous_ref=None,
                    custody_authorized=False, public_rights_confirmed=False,
                    access_confirmed=False, max_queries=1, client=None, now=None, clock=None,
                    monotonic=time.monotonic, sleep=time.sleep, pause_requested=None,
                    latest_checkpoint=None, progress=None):
    """Advance one explicit, bounded operation, retaining each successful page.

    fresh_reader must share the original remote transport budget. A caller may
    supply latest_checkpoint(checkpoint_id) to reconcile durable checkpoints
    newer than the collection's last published root. It must return an exact
    SourceRef or None, never an unverified synthetic capture.
    """
    from decision_kernel.application.market_custody import (
        authorize_market_custody, fresh_market_reader, retain_market_query, restore_market_query,
        market_read_scope,
    )
    from decision_kernel.application.market_collection_custody import (
        retain_market_checkpoint, restore_market_checkpoint,
        retain_market_collection, restore_market_collection,
    )
    from decision_kernel.application.market_collection_reading import (
        inspect_market_chain, latest_market_reference, market_cursor_capacity,
        require_checkpoint_descendant, same_reference,
    )
    now = datetime.now(timezone.utc) if now is None else now
    through = completed_through(now)
    require(access_confirmed is True, "ASHARE_SOURCE_ACCESS_REQUIRED")
    require(type(max_queries) is int and 1 <= max_queries <= MAX_QUERIES_PER_WORK_UNIT,
            "ASHARE_WORK_UNIT_QUERY_BOUND")
    plan = validate_collection_plan(plan, registry)
    require(_date(plan["calendar_end"]) <= _date(through), "ASHARE_COMPLETED_DAYS_ONLY")
    authorize_market_custody(store, custody_authorized=custody_authorized,
                             public_rights_confirmed=public_rights_confirmed)
    reader = fresh_market_reader(store, fresh_reader)
    previous_ref = asdict(previous_ref) if isinstance(previous_ref, SourceRef) else previous_ref
    # Restore and reconcile against one independently pinned pre-write view.
    # The scope must close (including its live binding check) before any write.
    with market_read_scope(reader) as grouped:
        previous = None if previous_ref is None else restore_market_collection(previous_ref, registry, grouped)["collection"]
        clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        at = clock(); source_time(at)
        collection = _new_collection(plan, previous, previous_ref, attempt_id, at, registry)
        chain = inspect_market_chain(grouped, registry, collection_id=plan["collection_id"])
        current = chain["reference"]
        if current is not None:
            require(same_reference(previous_ref, current), "ASHARE_COLLECTION_STALE_REFERENCE")
        if previous is not None and previous["plan"]["generation"] != plan["generation"]:
            require(latest_market_reference(grouped, registry, collection_id=plan["collection_id"],
                    generation=plan["generation"]) is None, "ASHARE_GENERATION_ALREADY_EXISTS")
    cursor_capacity = market_cursor_capacity(node_count=chain["node_count"],
                                             unique_body_bytes=chain["unique_body_bytes"])
    require(cursor_capacity["admit_work_unit"], "ASHARE_CURSOR_CAPACITY")
    reference = previous_ref
    permission = {"custody_authorized": custody_authorized, "public_rights_confirmed": public_rights_confirmed}
    source_client = client
    source_before = source_client.requests if source_client is not None else 0
    selected_slot = None
    persistence_error = None
    calendars_checked = False
    execution_error_persisted = True

    def save(action, *args, **kwargs):
        nonlocal persistence_error
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            persistence_error = exc
            raise _PersistenceStopped("ASHARE_PERSISTENCE_UNCONFIRMED") from exc

    def emit():
        collection["attempt"]["updated_at"] = clock()
        if source_client is not None:
            collection["attempt"]["source_requests"] = source_client.requests - source_before
        receipt = collection_summary(collection, reference)
        receipt["cursor_capacity_before_work_unit"] = deepcopy(cursor_capacity)
        if progress is not None:
            progress(deepcopy(receipt))
        return receipt

    def persist_collection():
        nonlocal reference
        validate_collection(collection, registry)
        saved = save(retain_market_collection, collection, registry, store, fresh_reader=fresh_reader,
            predecessor=reference, **permission)
        reference = saved["reference"]
        emit()

    def restore_unit(slot, reader):
        unit = collection["units"][slot]
        require(unit["status"] == "complete" and unit["query"] is not None, "ASHARE_QUERY_NOT_RETAINED")
        result = restore_market_query(unit["query"], registry, reader)["result"]
        use, _ = unit_request(collection["plan"], slot, registry)
        _qualified(result, use, collection["plan"], registry)
        return result

    def derive_sessions():
        nonlocal collection, calendars_checked
        p = collection["plan"]
        if calendars_checked:
            return
        if not all(collection["units"]["calendar:" + v]["status"] == "complete" for v in EXCHANGES):
            return
        dates = set()
        with market_read_scope(fresh_market_reader(store, fresh_reader)) as calendars:
            for venue in EXCHANGES:
                result = restore_unit("calendar:" + venue, calendars)
                dates.update(d.replace("-", "") for d in qualified_trade_calendar(result,
                    registry=registry, exchange=venue, start=p["start"], end=p["calendar_end"])
                    if _date(p["start"]) <= date.fromisoformat(d) <= _date(p["end"]))
        days = sorted(dates)
        require(not p["sessions"] or p["sessions"] == days, "ASHARE_DERIVED_SESSIONS_CHANGED")
        calendars_checked = True
        if p["sessions"] == days:
            return
        p["sessions"] = days
        for slot in _slots(p):
            if slot in collection["units"]:
                continue
            unit = _pending()
            day = slot.split(":")[0]
            if previous is not None and slot in previous["units"] and day not in p["refresh_dates"]:
                candidate = previous["units"][slot]
                old_use, _ = unit_request(previous["plan"], slot, registry)
                new_use, _ = unit_request(p, slot, registry)
                require(old_use == new_use, "ASHARE_REUSE_QUERY_CHANGED")
                unit = deepcopy(candidate)
            elif previous is not None and slot in previous["units"]:
                require(previous["units"][slot]["status"] in {"complete", "pending"},
                        "ASHARE_REFRESH_CANNOT_RESET_FAILED_QUERY")
            collection["units"][slot] = unit

    try:
        # The durable, read-back plan precedes credentials and any Relay request.
        persist_collection()
        derive_sessions()
        collection["attempt"]["status"] = "in-progress"
        session = None
        queries = 0
        index = 0
        while index < len(_slots(collection["plan"])):
            slot = _slots(collection["plan"])[index]
            index += 1
            unit = collection["units"][slot]
            if unit["status"] == "complete":
                continue
            if unit["status"] == "failed":
                # A known local terminal result stays a gap; it cannot consume
                # every later finite unit and starve still-unattempted peers.
                continue
            if unit["status"] in {"blocked", "unknown-write"}:
                collection["attempt"].update(status="blocked", reason_code=unit["reason_code"])
                break
            if queries >= max_queries:
                break
            if slot.startswith("master:"):
                if collection["units"]["calendar:" + slot[7:]]["status"] != "complete":
                    continue
            elif not slot.startswith("calendar:"):
                if not calendars_checked or not any(collection["units"]["master:" + v]["status"] == "complete" for v in EXCHANGES):
                    continue
            selected_slot = slot
            use, max_pages = unit_request(collection["plan"], slot, registry)
            cp_id = checkpoint_id(collection["plan"], slot)
            cp_ref = unit["checkpoint"]
            state = None
            # A fresh scope can see checkpoints published after the initial
            # root; retain no pinned snapshot across the writes below.
            with market_read_scope(reader) as checkpoints:
                if cp_ref is not None:
                    restored = restore_market_checkpoint(cp_ref, registry, checkpoints)
                    cp_id, state = restored["checkpoint_id"], restored["state"]
                newer = latest_market_reference(checkpoints, registry, checkpoint_id=cp_id)
                if latest_checkpoint is not None:
                    offered = latest_checkpoint(cp_id)
                    require_checkpoint_descendant(offered, cp_ref, registry, checkpoints)
                    require(same_reference(offered, newer), "ASHARE_CHECKPOINT_ROLLBACK")
                if newer is not None:
                    require_checkpoint_descendant(newer, cp_ref, registry, checkpoints)
                    restored = restore_market_checkpoint(newer, registry, checkpoints)
                    require(restored["checkpoint_id"] == cp_id, "ASHARE_CHECKPOINT_OWNER_CHANGED")
                    cp_ref, state = restored["reference"], restored["state"]
            unit.update(status="in-progress", reason_code=None, checkpoint=cp_ref)

            def checkpoint_writer(value):
                nonlocal cp_ref, state
                saved = save(retain_market_checkpoint, value, registry, store, checkpoint_id=cp_id,
                    predecessor=cp_ref, fresh_reader=fresh_reader, **permission)
                cp_ref = saved["reference"]
                state = deepcopy(value)
                unit["checkpoint"] = cp_ref
                emit()

            frozen = state is not None and state["control"]["state"] in {"complete", "terminal"}
            if state is not None and not frozen:
                retry = state["control"].get("retry")
                require(not retry or retry["phase"] not in {"in-flight", "response"}, "RELAY_CHECKPOINT_INFLIGHT")
                require(not retry or not retry["attempts"] or
                    (retry["attempts"][-1].get("failure") or {}).get("classification") not in
                    {"auth-or-entitlement", "rate-limit"}, "RELAY_CHECKPOINT_GLOBAL_STOP")
            if session is None and not frozen:
                source_client = source_client or RelayHTTP(request_timeout_seconds=90)
                source_before = source_client.requests
                source_client.validate_credential()
                session = RetryingRelay(source_client, sleep=sleep, monotonic=monotonic,
                    clock=clock, deadline=monotonic() + WORK_UNIT_SECONDS)
            queries += 1
            capture = deepcopy(state["capture"]) if frozen else capture_registered(use, registry, session,
                max_pages=max_pages, checkpoint=state, checkpoint_writer=checkpoint_writer,
                pause_requested=pause_requested)
            result = {"case_id": collection["plan"]["collection_id"], "use": use,
                "status": "captured" if capture["complete"] else "capture-incomplete", "capture": capture}
            if capture["complete"]:
                try:
                    _qualified(result, use, collection["plan"], registry)
                except (RecordError, MarketInputError) as exc:
                    unit.update(status="failed", reason_code=_reason(exc))
                else:
                    saved = save(retain_market_query, result, registry, store, fresh_reader=fresh_reader,
                                 checkpoint_reference=cp_ref, **permission)
                    unit.update(query=saved["reference"], status="complete", reason_code=None)
            else:
                reason = _reason(capture.get("error") or "ASHARE_QUERY_INCOMPLETE")
                paused = (state is not None and state["control"]["state"] == "paused") or reason in {
                    "RELAY_RUN_DEADLINE", "RELAY_WORK_UNIT_COMPLETE", "RELAY_STORAGE_BUDGET"}
                global_stop = (capture.get("failure") or {}).get("classification") in {
                    "auth-or-entitlement", "rate-limit"} or reason == "RELAY_SECRET_REFLECTED"
                unit.update(status="blocked" if global_stop or "INFLIGHT" in reason else "paused" if paused else "failed",
                            reason_code=reason)
                if paused or global_stop:
                    collection["attempt"].update(status="blocked" if global_stop else "partial", reason_code=reason)
                    emit()
                    break
            emit()
            derive_sessions()
        if collection["attempt"]["status"] != "blocked":
            collection["attempt"].update(status="complete" if _partition_complete(collection) else "partial")
            if queries == 0 and not _partition_complete(collection):
                collection["attempt"]["reason_code"] = "ASHARE_NO_PROGRESS"
        emit(); persist_collection()
    except Exception as exc:
        # Persistence exceptions never become retryable Relay errors. Keep the
        # last published collection/checkpoint; the next invocation reconciles it.
        from decision_kernel.application.relay_capture_checkpoint import CheckpointStateError, CheckpointWriteError
        if isinstance(exc, (CheckpointWriteError, _PersistenceStopped)):
            if selected_slot is not None:
                collection["units"][selected_slot].update(query=None, status="unknown-write",
                                                          reason_code="ASHARE_PERSISTENCE_UNCONFIRMED")
            collection["attempt"].update(status="blocked", reason_code="ASHARE_PERSISTENCE_UNCONFIRMED")
            receipt = emit()
            receipt["persistence_error"] = _reason(persistence_error or exc)
            raise CollectionSyncInterrupted(receipt, reference) from exc
        if isinstance(exc, StoreUnavailable) or _reason(exc) in {
            "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "STORE_MARKER_MISMATCH",
            "COMMIT_OUTSIDE_BINDING", "REMOTE_READ_ONLY"}:
            # A store outage is not a terminal source observation. Leave the
            # durable source state available for the next authorized operation.
            raise
        if isinstance(exc, (RecordError, MarketInputError, CheckpointStateError)) and selected_slot is not None:
            reason = _reason(exc)
            collection["units"][selected_slot].update(query=None, status="blocked", reason_code=reason)
            collection["attempt"].update(status="blocked", reason_code=reason)
            # Invalid caller control or missing credentials are errors in this
            # operation, not observations that permanently terminate a source.
            # The saved pending/paused checkpoint remains the restart authority.
            execution_error_persisted = False
            emit()
        else:
            raise
    receipt = collection_summary(collection, reference)
    receipt["execution_error_persisted"] = execution_error_persisted
    receipt["cursor_capacity_before_work_unit"] = cursor_capacity
    return {"status": "complete" if _partition_complete(collection) else "partial", "reference": reference,
            "collection": collection, "receipt": receipt}
