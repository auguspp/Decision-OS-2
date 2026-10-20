"""A bounded daily calendar intent on the existing immutable source store.

FTShare remains the price collection owner. These small source records retain
only the two calendar reservations and the exact collection selected by one
daily invocation; they are neither another price store nor a generic queue.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import re

from decision_kernel.application.ftshare_collection import (
    SINGLE_PLAN_SCHEMA, _single_write_frontier, latest_ftshare_reference,
    resolve_ftshare_expansion_frontier, restore_ftshare_collection,
)
from decision_kernel.application.market_custody import (
    authorize_market_custody, fresh_market_reader, market_read_scope,
    restore_market_query,
)
from decision_kernel.application.market_relay_inputs import qualified_trade_calendar
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
from decision_kernel.application.research_sources import validate_plan
from decision_kernel.domain.records import (
    Selection, SourceInput, SourceRef, digest, encoded, require,
)

SCHEMA = "daily-market-cycle-v1"
POLICY_SCHEMA = "daily-market-policy-v1"
PREFIX = "daily-market-cycle-"
VENUES = ("SSE", "SZSE")
ZONE = timezone(timedelta(hours=8))
MAX_RECORDS = 4096
MAX_CONTROL_BYTES = 16 * 1024 * 1024
MAX_LATE_SECONDS = 24 * 3600
SUCCESS = {"complete", "no-new-session"}
LIMITATIONS = (
    "GENERATED_DAILY_CALENDAR_INTENT_NOT_PROVIDER_RESPONSE",
    "EXACT_EXISTING_RECORDS_SOURCE_REFS_NO_SECOND_PRICE_STORE",
    "TWO_CALENDAR_RESERVATIONS_NO_RETRY_OR_CROSS_DAY_FAILURE_RESET",
    "PRICE_ATTEMPTS_AND_SOURCE_HOLDS_REMAIN_OWNED_BY_FTSHARE_COLLECTION",
    "BOUNDED_STOCK_SCOPE_NOT_COMPLETE_MARKET_OR_LONG_TERM_UPTIME",
)


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        require(False, "DAILY_MARKET_CLOCK")
    require(stamp.tzinfo is not None, "DAILY_MARKET_CLOCK")
    return stamp


def policy(value):
    fields = {"schema", "enabled", "schedule_utc", "not_before_local", "lookback_days",
        "max_calendar_requests", "max_price_requests", "symbols", "seed_reference"}
    require(isinstance(value, dict) and set(value) == fields and value["schema"] == POLICY_SCHEMA
        and type(value["enabled"]) is bool and value["schedule_utc"] == "35 10 * * 1-5"
        and value["not_before_local"] == "18:35" and type(value["lookback_days"]) is int
        and value["lookback_days"] == 119 and type(value["max_calendar_requests"]) is int
        and value["max_calendar_requests"] == 2, "DAILY_MARKET_POLICY")
    symbols = value["symbols"]
    require(isinstance(symbols, list) and 1 <= len(symbols) <= 6
        and all(isinstance(code, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", code) for code in symbols)
        and symbols == sorted(set(symbols)) and type(value["max_price_requests"]) is int
        and value["max_price_requests"] == 2 * len(symbols), "DAILY_MARKET_SCOPE")
    require(SourceRef.from_dict(value["seed_reference"]).selection == Selection(),
        "DAILY_MARKET_SEED_REFERENCE")
    return json.loads(encoded(value))


def policy_digest(value):
    # Pausing never changes an existing source intent or grants a new allowance.
    checked = policy(value)
    checked.pop("enabled")
    return digest(encoded(checked))


def _exact(value, repository):
    ref = SourceRef.from_dict(value)
    require(ref.selection == Selection() and ref.manifest.repository == repository,
        "DAILY_MARKET_REFERENCE")
    return json.loads(encoded(asdict(ref)))


def _source(state):
    return SourceInput(source_id=PREFIX + state["date"] + "-r" + str(state["revision"]).zfill(3),
        kind="content", title="每日行情日历与原集合接续 / " + state["date"],
        origin="https://github.com/auguspp/Decision-OS-2", custody="provided",
        text=encoded(state).decode(), limitations=LIMITATIONS)


def validate_cycle(value):
    fields = {"schema", "date", "revision", "created_at", "updated_at", "predecessor",
        "policy_sha256", "base_market_reference", "calendar_start", "calendar_end",
        "calendars", "price_end", "market_generation", "market_reference", "status", "reason_code"}
    require(isinstance(value, dict) and set(value) == fields and value["schema"] == SCHEMA
        and isinstance(value["date"], str) and re.fullmatch(r"[0-9]{8}", value["date"])
        and type(value["revision"]) is int and 0 <= value["revision"] <= 6
        and value["calendar_end"] == value["date"]
        and value["market_generation"] == "daily-" + value["date"]
        and isinstance(value["policy_sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["policy_sha256"]), "DAILY_MARKET_CYCLE")
    begin = datetime.strptime(value["calendar_start"], "%Y%m%d")
    end = datetime.strptime(value["calendar_end"], "%Y%m%d")
    require(0 <= (end - begin).days < 119 and _stamp(value["updated_at"]) >= _stamp(value["created_at"]),
        "DAILY_MARKET_CYCLE_CLOCK")
    calendars = value["calendars"]
    require(isinstance(calendars, dict) and set(calendars) == set(VENUES), "DAILY_MARKET_CALENDARS")
    for row in calendars.values():
        require(isinstance(row, dict) and set(row) == {"status", "reserved_requests", "reference", "reason_code"}
            and row["status"] in {"pending", "in-flight", "complete", "failed"}
            and type(row["reserved_requests"]) is int
            and row["reserved_requests"] == int(row["status"] != "pending")
            and (row["reference"] is not None) == (row["status"] == "complete")
            and (row["reason_code"] is not None) == (row["status"] == "failed"), "DAILY_MARKET_CALENDAR_STATE")
        if row["reference"] is not None:
            SourceRef.from_dict(row["reference"])
    require(sum(row["status"] == "in-flight" for row in calendars.values()) <= 1,
        "DAILY_MARKET_ONE_CALENDAR_IN_FLIGHT")
    require(value["status"] in {"calendar-pending", "market-pending", "blocked"} | SUCCESS
        and (value["reason_code"] is not None) == (value["status"] == "blocked"), "DAILY_MARKET_STATUS")
    for reason in [value["reason_code"], *(row["reason_code"] for row in calendars.values())]:
        require(reason is None or isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", reason),
            "DAILY_MARKET_REASON")
    if value["status"] in SUCCESS | {"market-pending"}:
        require(all(row["status"] == "complete" for row in calendars.values())
            and isinstance(value["price_end"], str) and re.fullmatch(r"[0-9]{8}", value["price_end"])
            and value["calendar_start"] <= value["price_end"] <= value["date"], "DAILY_MARKET_CALENDAR_INCOMPLETE")
    if value["status"] in SUCCESS:
        require(value["market_reference"] is not None, "DAILY_MARKET_COLLECTION_REQUIRED")
    SourceRef.from_dict(value["base_market_reference"])
    for field in ("predecessor", "market_reference"):
        if value[field] is not None:
            SourceRef.from_dict(value[field])
    require(len(encoded(value)) <= 12 * 1024, "DAILY_MARKET_CONTROL_BOUND")
    return json.loads(encoded(value))


def _transition(previous, current):
    require(previous["status"] not in SUCCESS | {"blocked"}
        and current["revision"] == previous["revision"] + 1,
        "DAILY_MARKET_TERMINAL_OR_REVISION")
    immutable = set(previous) - {"revision", "updated_at", "predecessor", "calendars", "price_end",
        "market_reference", "status", "reason_code"}
    require(all(previous[key] == current[key] for key in immutable)
        and _stamp(current["updated_at"]) >= _stamp(previous["updated_at"]), "DAILY_MARKET_INTENT_CHANGED")
    changed = [venue for venue in VENUES if previous["calendars"][venue] != current["calendars"][venue]]
    require(len(changed) <= 1, "DAILY_MARKET_CALENDAR_TRANSITION")
    if changed:
        before, after = (state["calendars"][changed[0]] for state in (previous, current))
        require((before["status"], after["status"]) in {
            ("pending", "in-flight"), ("in-flight", "complete"), ("in-flight", "failed")},
            "DAILY_MARKET_CALENDAR_TRANSITION")
        require(current["status"] == ("blocked" if after["status"] == "failed" else "calendar-pending")
            and current["price_end"] == previous["price_end"]
            and current["market_reference"] == previous["market_reference"], "DAILY_MARKET_CALENDAR_TRANSITION")
    else:
        require(previous["status"] in {"calendar-pending", "market-pending"}
            and current["status"] in {"market-pending", "blocked"} | SUCCESS,
            "DAILY_MARKET_PHASE_TRANSITION")


def _market_bridge(base_reference, through_reference, registry, store, protected_symbols):
    if base_reference == through_reference:
        return
    require(registry is not None, "DAILY_MARKET_FRONTIER_PROOF_REQUIRED")
    frontier = resolve_ftshare_expansion_frontier(base_reference, registry, store,
        protected_symbols=protected_symbols, through_reference=through_reference)
    require(frontier["reason_code"] is None,
        frontier["reason_code"] or "DAILY_MARKET_BASE_REQUIRES_REVIEW")
    require(frontier["reference"] == through_reference, "DAILY_MARKET_HISTORY_MARKET_EDGE")


def latest_cycle(store, *, registry=None, protected_symbols=()):
    """Replay the small immutable controls; duplicate ordinals never pick a winner."""
    with market_read_scope(store) as reader:
        head = reader.backend.head()
        paths = [path for path in reader.backend.paths(head, "source-views/")
            if path.startswith("source-views/" + PREFIX) and path.endswith("/manifest.json")]
        require(len(paths) <= MAX_RECORDS, "DAILY_MARKET_HISTORY_BOUND")
        rows, total = {}, 0
        for path in paths:
            match = re.fullmatch(r"source-views/" + PREFIX + r"([0-9]{8})-r([0-9]{3})/[a-f0-9]{64}/manifest\.json", path)
            require(match is not None, "DAILY_MARKET_HISTORY_PATH")
            key = (match[1], int(match[2]))
            require(key not in rows, "DAILY_MARKET_HISTORY_FORK")
            original = reader.backend.last_change(head, path)
            ref = SourceRef(reader.backend.reference(original, path))
            stored = reader.read_source(ref)
            total += len(stored["data"].encode())
            require(total <= MAX_CONTROL_BYTES, "DAILY_MARKET_HISTORY_BOUND")
            state = validate_cycle(json.loads(stored["data"]))
            require(key == (state["date"], state["revision"])
                and encoded({**stored["manifest"]["input"], "text": stored["data"]}) == encoded(asdict(_source(state))),
                "DAILY_MARKET_SOURCE_CHANGED")
            rows[key] = {"reference": json.loads(encoded(asdict(ref))), "cycle": state}
        previous = None
        for item in [rows[key] for key in sorted(rows)]:
            current = item["cycle"]
            require(current["predecessor"] == (None if previous is None else previous["reference"]),
                "DAILY_MARKET_HISTORY_EDGE")
            if previous is None or previous["cycle"]["date"] != current["date"]:
                require(current["revision"] == 0 and current["status"] == "calendar-pending"
                    and all(row["status"] == "pending" for row in current["calendars"].values())
                    and (previous is None or previous["cycle"]["status"] in SUCCESS),
                    "DAILY_MARKET_PRIOR_CYCLE_NOT_COMPLETE")
                if previous is not None:
                    # History proves only this recorded bridge. Later children
                    # of its endpoint cannot invalidate an already saved cycle.
                    _market_bridge(previous["cycle"]["market_reference"], current["base_market_reference"],
                        registry, reader, protected_symbols)
            else:
                _transition(previous["cycle"], current)
            previous = item
        return previous


def retain_cycle(state, store, *, registry, fresh_reader, previous, protected_symbols=()):
    authorize_market_custody(store, custody_authorized=True, public_rights_confirmed=False)
    state = validate_cycle(state)
    with market_read_scope(fresh_market_reader(store, fresh_reader)) as reader:
        snapshot = reader.backend.head()
        before = latest_cycle(reader, registry=registry, protected_symbols=protected_symbols)
        require(before == previous and state["predecessor"] == (None if before is None else before["reference"]),
            "DAILY_MARKET_STALE_CONTROL")
        if before is not None and before["cycle"]["date"] == state["date"]:
            _transition(before["cycle"], state)
            if any(before["cycle"]["calendars"][venue]["status"] == "pending"
                    and state["calendars"][venue]["status"] == "in-flight" for venue in VENUES):
                # The authority for one new calendar request and its durable
                # reservation must use the same snapshot and original CAS.
                check_cycle_frontier(state, registry, reader, protected_symbols=protected_symbols)
        else:
            require(state["revision"] == 0 and (before is None or before["cycle"]["status"] in SUCCESS
                and before["cycle"]["date"] < state["date"]), "DAILY_MARKET_PRIOR_CYCLE_NOT_COMPLETE")
            anchor = state["base_market_reference"] if before is None else before["cycle"]["market_reference"]
            # A valid historical bridge is insufficient here: a competing
            # addition may have arrived after initial_cycle chose its base.
            frontier = resolve_ftshare_expansion_frontier(anchor, registry, reader,
                protected_symbols=protected_symbols)
            require(frontier["reason_code"] is None,
                frontier["reason_code"] or "DAILY_MARKET_BASE_REQUIRES_REVIEW")
            require(frontier["reference"] == state["base_market_reference"], "DAILY_MARKET_STALE_FRONTIER")
    source = _source(state)
    reference = store.put_source("daily-market-" + digest(encoded(asdict(source)))[:32], source,
        expected_head=snapshot)
    restored = latest_cycle(fresh_market_reader(store, fresh_reader), registry=registry,
        protected_symbols=protected_symbols)
    expected = {"reference": json.loads(encoded(asdict(reference))), "cycle": state}
    require(restored == expected, "DAILY_MARKET_READBACK_CHANGED")
    return restored


def next_state(item, *, at, **changes):
    value = {**deepcopy(item["cycle"]), "revision": item["cycle"]["revision"] + 1,
        "predecessor": deepcopy(item["reference"]), "updated_at": at, **changes}
    return validate_cycle(value)


def admission_slot(approved, *, at, allow_late=False):
    """Separate one due business date from the actual invocation clock.

    Only the native schedule and the explicit recover-late host use the most
    recent elapsed weekday slot. Exchange calendars still decide whether that
    business date contains a new completed session.
    """
    approved = policy(approved)
    require(type(allow_late) is bool, "DAILY_MARKET_ADMISSION_MODE")
    stamp = _stamp(at).astimezone(ZONE)
    hour, minute = map(int, approved["not_before_local"].split(":"))
    slot = stamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if allow_late:
        if slot > stamp:
            slot -= timedelta(days=1)
        while slot.weekday() >= 5:
            slot -= timedelta(days=1)
        require(0 <= (stamp - slot).total_seconds() < MAX_LATE_SECONDS,
                "DAILY_MARKET_NO_RECENT_SCHEDULE")
    else:
        require(stamp >= slot, "DAILY_MARKET_BEFORE_CLOSE")
    return {"mode": "latest-elapsed-schedule" if allow_late else "same-local-day",
        "admitted_at": at, "business_date": slot.strftime("%Y%m%d"),
        "scheduled_for": slot.isoformat(), "delay_seconds": (stamp - slot).total_seconds(),
        "max_delay_seconds": MAX_LATE_SECONDS if allow_late else None}


def initial_cycle(approved, store, registry, *, at, admission_at=None, allow_late=False):
    approved = policy(approved)
    require(approved["enabled"], "DAILY_MARKET_PAUSED")
    admitted = admission_slot(approved, at=at if admission_at is None else admission_at,
                              allow_late=allow_late)
    require(_stamp(at) >= _stamp(admitted["admitted_at"]), "DAILY_MARKET_CLOCK_ROLLBACK")
    # The slot chooses only date/window/generation. Never rewrite the real
    # source-intent creation time to impersonate execution on the business day.
    stamp = _stamp(admitted["scheduled_for"]).astimezone(ZONE)
    with market_read_scope(store) as reader:
        result = _initial_cycle(approved, reader, registry, at=at, stamp=stamp)
    return {**result, "admission": admitted}


def _initial_cycle(approved, store, registry, *, at, stamp):
    previous = latest_cycle(store, registry=registry, protected_symbols=approved["symbols"])
    if previous is not None:
        require(previous["cycle"]["policy_sha256"] == policy_digest(approved), "DAILY_MARKET_POLICY_CHANGED")
        require(previous["cycle"]["date"] <= stamp.strftime("%Y%m%d"), "DAILY_MARKET_CLOCK_ROLLBACK")
        if previous["cycle"]["date"] == stamp.strftime("%Y%m%d") or previous["cycle"]["status"] not in SUCCESS:
            return {"existing": previous, "candidate": None}
    anchor = approved["seed_reference"] if previous is None else previous["cycle"]["market_reference"]
    frontier = resolve_ftshare_expansion_frontier(anchor, registry, store,
        protected_symbols=approved["symbols"])
    require(frontier["reason_code"] is None,
        frontier["reason_code"] if frontier["successor_references"] else "DAILY_MARKET_BASE_REQUIRES_REVIEW")
    base_ref, base = frontier["reference"], frontier["restored"]
    require(base["collection"]["plan"]["schema"] == SINGLE_PLAN_SCHEMA
        and base["collection"]["source_hold"] is None
        and all(row["status"] == "complete" for row in base["collection"]["units"].values()),
        "DAILY_MARKET_BASE_REQUIRES_REVIEW")
    date = stamp.strftime("%Y%m%d")
    _single_write_frontier(base, registry, store, generation="daily-" + date)
    require(all(code in base["collection"]["plan"]["eligible_symbols"] for code in approved["symbols"]),
        "DAILY_MARKET_SCOPE_NOT_QUALIFIED")
    start = max(base["collection"]["plan"]["start"],
        (stamp.date() - timedelta(days=approved["lookback_days"] - 1)).strftime("%Y%m%d"))
    state = {"schema": SCHEMA, "date": date, "revision": 0, "created_at": at, "updated_at": at,
        "predecessor": None if previous is None else previous["reference"],
        "policy_sha256": policy_digest(approved), "base_market_reference": deepcopy(base_ref),
        "calendar_start": start, "calendar_end": date,
        "calendars": {venue: {"status": "pending", "reserved_requests": 0,
            "reference": None, "reason_code": None} for venue in VENUES},
        "price_end": None, "market_generation": "daily-" + date,
        "market_reference": None, "status": "calendar-pending", "reason_code": None}
    return {"existing": previous, "candidate": validate_cycle(state)}


def calendar_plan(cycle, registry):
    return validate_plan({"schema": "research-source-plan-v1", "case_id": "daily-market-" + cycle["date"],
        "requests": [{"stage": "baseline", "purpose": "Retain " + venue
            + " actual exchange calendar for bounded daily market reading", "api": "trade_cal",
            "parameters": {"exchange": venue, "start_date": cycle["calendar_start"],
                "end_date": cycle["calendar_end"], "fields": "exchange,cal_date,is_open", "limit": "5000"}}
            for venue in VENUES]}, registry)


def completed_session(cycle, registry, store):
    with market_read_scope(store) as reader:
        return _completed_session(cycle, registry, reader)


def _completed_session(cycle, registry, store):
    dates, references = {}, {}
    base = restore_ftshare_collection(cycle["base_market_reference"], registry, store)
    for venue in VENUES:
        row = cycle["calendars"][venue]
        require(row["status"] == "complete", "DAILY_MARKET_CALENDAR_INCOMPLETE")
        references[venue] = _exact(row["reference"], store.backend.repository)
        result = restore_market_query(references[venue], registry, store)["result"]
        days = qualified_trade_calendar(result, registry=registry, exchange=venue,
            start=cycle["calendar_start"], end=cycle["calendar_end"])
        require(bool(days), "DAILY_MARKET_NO_KNOWN_SESSION")
        old = base["context"]["calendar_results"][venue]["use"]["parameters"]
        overlap_start = max(cycle["calendar_start"], old["start_date"])
        overlap_end = min(cycle["calendar_end"], old["end_date"])
        require(overlap_start <= overlap_end and
            [day for day in days if overlap_start <= day.replace("-", "") <= overlap_end] ==
            [day for day in base["context"]["calendars"][venue]
             if overlap_start <= day.replace("-", "") <= overlap_end], "DAILY_MARKET_CALENDAR_HISTORY_CHANGED")
        dates[venue] = days[-1].replace("-", "")
    require(len(set(dates.values())) == 1, "DAILY_MARKET_EXCHANGE_SESSION_MISMATCH")
    end = next(iter(dates.values()))
    require(end >= base["collection"]["plan"]["end"], "DAILY_MARKET_SESSION_ROLLBACK")
    return {"price_end": end, "calendar_references": references,
        "new_session": end > base["collection"]["plan"]["end"]}


def price_configuration(cycle, approved, registry, store):
    require(cycle["status"] == "market-pending" and cycle["policy_sha256"] == policy_digest(approved),
        "DAILY_MARKET_PRICE_PHASE")
    base = check_cycle_frontier(cycle, registry, store, protected_symbols=approved["symbols"])
    proof = base["collection"]["plan"]["adoption"]["proof"]
    return {"schema": "ftshare-daily-runtime-v1", "collection_id": base["collection"]["plan"]["collection_id"],
        "generation": cycle["market_generation"],
        "legacy_reference": deepcopy(base["collection"]["plan"]["legacy_reference"]), "qualification": None,
        "kernel_adoption": {key: proof[key] for key in ("run_id", "artifact_id", "sha256", "code_sha")},
        "kernel_sync": {"base_reference": deepcopy(cycle["base_market_reference"]),
            "start": cycle["calendar_start"], "end": cycle["price_end"],
            "completion_policy": EOD_COMPLETION_POLICY,
            "targets": [{"symbol": symbol, "adjust_kind": kind} for symbol in approved["symbols"]
                for kind in ("none", "forward")], "source_budget": approved["max_price_requests"]}}


def check_cycle_frontier(cycle, registry, store, *, protected_symbols=()):
    """Keep an existing cycle fixed; only its own FT generation may resume."""
    with market_read_scope(store) as reader:
        base = restore_ftshare_collection(cycle["base_market_reference"], registry, reader)
        _single_write_frontier(base, registry, reader, generation=cycle["market_generation"])
        newest = latest_ftshare_reference(reader, registry,
            collection_id=base["collection"]["plan"]["collection_id"], generation=cycle["market_generation"])
        anchor = newest or cycle["base_market_reference"]
        frontier = resolve_ftshare_expansion_frontier(anchor, registry, reader,
            protected_symbols=protected_symbols)
        require(frontier["reference"] == anchor and not frontier["successor_references"],
            "DAILY_MARKET_PENDING_FRONTIER_CHANGED")
        if newest is None:
            require(frontier["reason_code"] is None,
                frontier["reason_code"] or "DAILY_MARKET_BASE_REQUIRES_REVIEW")
        else:
            require(frontier["restored"]["collection"]["plan"]["base_reference"] == cycle["base_market_reference"],
                "DAILY_MARKET_HISTORY_MARKET_EDGE")
            # The existing FT owner decides pending, held, failed and unknown
            # reservations. This check neither resumes nor resets those units.
        return base


def daily_market_reference(store, registry, approved):
    """Resolve latest attempted prices, retaining failure status for human readers."""
    approved = policy(approved)
    with market_read_scope(store) as reader:
        return _daily_market_reference(reader, registry, approved)


def _daily_market_reference(store, registry, approved):
    item = latest_cycle(store, registry=registry, protected_symbols=approved["symbols"])
    cycle = None if item is None else item["cycle"]
    anchor = approved["seed_reference"]
    if cycle is not None:
        require(cycle["policy_sha256"] == policy_digest(approved), "DAILY_MARKET_POLICY_CHANGED")
        base = restore_ftshare_collection(cycle["base_market_reference"], registry, store)
        newest = latest_ftshare_reference(store, registry,
            collection_id=base["collection"]["plan"]["collection_id"], generation=cycle["market_generation"])
        if newest is not None:
            owned = restore_ftshare_collection(newest, registry, store)
            require(owned["collection"]["plan"]["base_reference"] == cycle["base_market_reference"],
                "DAILY_MARKET_HISTORY_MARKET_EDGE")
        anchor = newest or cycle["market_reference"] or cycle["base_market_reference"]
    frontier = resolve_ftshare_expansion_frontier(anchor, registry, store,
        protected_symbols=approved["symbols"])
    reason, blocking = frontier["reason_code"], frontier["blocking_reference"]
    if cycle is not None and cycle["status"] not in SUCCESS and frontier["successor_references"]:
        reason = reason or "DAILY_MARKET_PENDING_FRONTIER_CHANGED"
        blocking = blocking or frontier["successor_references"][0]
    return {"reference": deepcopy(frontier["reference"]), "cycle": deepcopy(cycle),
        "cycle_reference": None if item is None else item["reference"],
        "frontier_reason_code": reason, "frontier_blocking_reference": deepcopy(blocking),
        "frontier_successor_references": deepcopy(frontier["successor_references"])}
