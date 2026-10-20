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
    SINGLE_PLAN_SCHEMA, _single_sources, _single_write_frontier, latest_ftshare_reference,
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
MASTER_SCHEMA = "daily-market-cycle-v4"
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
    limitations = LIMITATIONS
    if state["schema"] in {"daily-market-cycle-v2", "daily-market-cycle-v3"}:
        limitations = LIMITATIONS[:2] + ("EXPLICIT_ONE_ADDITIONAL_CALENDAR_REQUEST_ORIGINAL_FAILURE_PRESERVED",) + LIMITATIONS[3:]
    if state["schema"] == "daily-market-cycle-v3":
        limitations += ("EXACT_SETTLED_WRITE_RECONCILIATION_ZERO_ADDITIONAL_SOURCE_REQUESTS",)
    if state["schema"] == MASTER_SCHEMA:
        limitations += ("FULL_CURRENT_L_MASTER_REFRESH_ON_EACH_NEW_COMPLETED_SESSION",
            "MASTER_PAGE_AND_RETRY_BUDGETS_REMAIN_OWNED_BY_ORIGINAL_RELAY_CHECKPOINTS",)
    return SourceInput(source_id=PREFIX + state["date"] + "-r" + str(state["revision"]).zfill(3),
        kind="content", title="每日行情日历与原集合接续 / " + state["date"],
        origin="https://github.com/auguspp/Decision-OS-2", custody="provided",
        text=encoded(state).decode(), limitations=limitations)


def validate_cycle(value):
    fields = {"schema", "date", "revision", "created_at", "updated_at", "predecessor",
        "policy_sha256", "base_market_reference", "calendar_start", "calendar_end",
        "calendars", "price_end", "market_generation", "market_reference", "status", "reason_code"}
    from decision_kernel.application.daily_calendar_repair import CYCLE_SCHEMA, proof
    from decision_kernel.application.daily_write_reconciliation import CYCLE_SCHEMA as RECONCILED_SCHEMA, proof as write_proof
    reconciled = isinstance(value, dict) and value.get("schema") == RECONCILED_SCHEMA
    repaired = isinstance(value, dict) and value.get("schema") in {CYCLE_SCHEMA, RECONCILED_SCHEMA}
    refreshed = isinstance(value, dict) and value.get("schema") == MASTER_SCHEMA
    repair = proof(value.get("calendar_repair")) if repaired else None
    if reconciled:
        write_proof(value.get("write_reconciliation"))
    require(isinstance(value, dict) and set(value) == fields | ({"calendar_repair"} if repaired else set())
        | ({"write_reconciliation"} if reconciled else set())
        | ({"daily_policy", "master_refresh", "masters"} if refreshed else set())
        and value["schema"] == (MASTER_SCHEMA if refreshed else RECONCILED_SCHEMA if reconciled else CYCLE_SCHEMA if repaired else SCHEMA)
        and isinstance(value["date"], str) and re.fullmatch(r"[0-9]{8}", value["date"])
        and type(value["revision"]) is int and (9 if reconciled else 3 if repaired else 0)
            <= value["revision"] <= (11 if refreshed else 10 if reconciled else 8 if repaired else 6)
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
    for venue, row in calendars.items():
        require(isinstance(row, dict) and set(row) == {"status", "reserved_requests", "reference", "reason_code"}
            and row["status"] in {"pending", "in-flight", "complete", "failed"}
            and type(row["reserved_requests"]) is int
            and row["reserved_requests"] == (2 if repaired and venue == repair["venue"] else int(row["status"] != "pending"))
            and not (repaired and venue == repair["venue"] and row["status"] == "pending")
            and (row["reference"] is not None) == (row["status"] == "complete")
            and (row["reason_code"] is not None) == (row["status"] == "failed"), "DAILY_MARKET_CALENDAR_STATE")
        if row["reference"] is not None:
            SourceRef.from_dict(row["reference"])
    require(sum(row["status"] == "in-flight" for row in calendars.values()) <= 1,
        "DAILY_MARKET_ONE_CALENDAR_IN_FLIGHT")
    require(value["status"] in {"calendar-pending", "market-pending", "blocked"} | SUCCESS
        | ({"master-pending"} if refreshed else set())
        and (value["reason_code"] is not None) == (value["status"] == "blocked"), "DAILY_MARKET_STATUS")
    if refreshed:
        _validate_masters(value)
    if reconciled:
        require((value["revision"], value["status"]) in {
            (9, "market-pending"), (10, "complete"), (10, "blocked")},
            "DAILY_WRITE_RECONCILIATION_PHASE")
    for reason in [value["reason_code"], *(row["reason_code"] for row in calendars.values())]:
        require(reason is None or isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", reason),
            "DAILY_MARKET_REASON")
    if value["status"] in SUCCESS | {"market-pending", "master-pending"}:
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


def _frozen_daily_policy(value):
    """Recover the original digest authority without consulting current config."""
    frozen = value["daily_policy"]
    require(isinstance(frozen, dict) and "enabled" not in frozen, "DAILY_MARKET_FROZEN_POLICY")
    checked = policy({**frozen, "enabled": False})
    checked.pop("enabled")
    require(encoded(checked) == encoded(frozen) and digest(encoded(checked)) == value["policy_sha256"],
        "DAILY_MARKET_FROZEN_POLICY")
    return checked


def _validate_masters(value):
    from decision_kernel.application.daily_master_refresh import policy as master_policy
    _frozen_daily_policy(value)
    master_policy(value["master_refresh"])
    masters = value["masters"]
    require(isinstance(masters, dict) and set(masters) == set(VENUES), "DAILY_MASTER_VENUES")
    for row in masters.values():
        require(isinstance(row, dict) and set(row) == {"status", "reserved_queries", "reference",
            "checkpoint_reference", "reason_code"}
            and row["status"] in {"pending", "in-flight", "complete", "failed"}
            and type(row["reserved_queries"]) is int
            and row["reserved_queries"] == int(row["status"] != "pending")
            and (row["reference"] is not None) == (row["status"] == "complete")
            and (row["reason_code"] is not None) == (row["status"] == "failed")
            and (row["checkpoint_reference"] is not None or row["status"] != "complete")
            and (row["checkpoint_reference"] is None or row["status"] in {"complete", "failed"}),
            "DAILY_MASTER_STATE")
        for key in ("reference", "checkpoint_reference"):
            if row[key] is not None:
                require(SourceRef.from_dict(row[key]).selection == Selection(), "DAILY_MASTER_REFERENCE")
        require(row["reason_code"] is None or isinstance(row["reason_code"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", row["reason_code"]), "DAILY_MASTER_REASON")
    require(sum(row["status"] == "in-flight" for row in masters.values()) <= 1,
        "DAILY_MASTER_ONE_QUERY_IN_FLIGHT")
    if value["status"] in {"calendar-pending", "no-new-session"}:
        require(all(row["status"] == "pending" for row in masters.values()), "DAILY_MASTER_PHASE")
    if any(row["status"] != "pending" for row in masters.values()):
        require(all(row["status"] == "complete" for row in value["calendars"].values())
            and isinstance(value["price_end"], str), "DAILY_MASTER_BEFORE_CALENDARS")
    if value["status"] in {"market-pending", "complete"}:
        require(all(row["status"] == "complete" for row in masters.values()), "DAILY_MASTER_INCOMPLETE")


def _verify_master_edges(previous, current, registry, store):
    """Validate a newly completed master once, at its original history edge."""
    if current["schema"] != MASTER_SCHEMA:
        require(previous is None or previous["schema"] != MASTER_SCHEMA,
            "DAILY_MASTER_POLICY_REMOVED")
        return
    from decision_kernel.application.daily_master_refresh import verify_completed_master
    for venue, row in current["masters"].items():
        if row["status"] == "complete" and (previous is None
                or previous.get("masters", {}).get(venue) != row):
            verify_completed_master(current, venue, registry, store)
    if current["status"] == "complete":
        restored = restore_ftshare_collection(current["market_reference"], registry, store)
        collection = restored["collection"]
        plan, approved = collection["plan"], _frozen_daily_policy(current)
        targets = sorted([{"symbol": symbol, "adjust_kind": kind} for symbol in approved["symbols"]
            for kind in ("none", "forward")], key=lambda target: (target["symbol"], target["adjust_kind"]))
        slots = [target["adjust_kind"] + ":" + target["symbol"] for target in targets]
        # Normal FT restoration already reconstructs collection identity,
        # legacy/adoption and inherited owners from this exact base. Bind the
        # newly completed daily work to its frozen authority as well.
        require(plan["schema"] == SINGLE_PLAN_SCHEMA
            and plan["base_reference"] == current["base_market_reference"]
            and plan["generation"] == current["market_generation"]
            and plan["start"] == current["calendar_start"] and plan["end"] == current["price_end"]
            and plan["calendar_end"] == current["calendar_end"]
            and plan.get("completion_policy") == EOD_COMPLETION_POLICY
            and plan["targets"] == targets and plan["new_slots"] == slots
            and plan["source_budget"] == approved["max_price_requests"]
            and collection["reserved_requests"] == approved["max_price_requests"]
            and collection["revision"] == 2 * approved["max_price_requests"]
            and _stamp(plan["created_at"]) >= _stamp(current["created_at"])
            and _stamp(collection["updated_at"]) <= _stamp(current["updated_at"])
            and plan["master_references"] == {v: current["masters"][v]["reference"] for v in VENUES}
            and plan["calendar_references"] == {v: current["calendars"][v]["reference"] for v in VENUES}
            and collection["source_hold"] is None
            and all(row["status"] == "complete" for row in collection["units"].values())
            and all(collection["units"][slot]["request_count"] == 1 for slot in slots),
            "DAILY_MASTER_NOT_ADOPTED")
        # The manifest reader intentionally omits captures. Reuse their original
        # owner to prove each newly claimed raw/forward request against its exact
        # retained source bytes, within the caller's existing read scope.
        _single_sources(plan, {slot: collection["units"][slot] for slot in slots}, store)


def _transition(previous, current):
    if previous["status"] == "blocked" and "write_reconciliation" not in previous and "write_reconciliation" in current:
        from decision_kernel.application.daily_write_reconciliation import transition
        transition(previous, current)
        return
    if previous["status"] == "blocked" and "calendar_repair" not in previous and "calendar_repair" in current:
        from decision_kernel.application.daily_calendar_repair import transition
        transition(previous, current)
        return
    require(previous["status"] not in SUCCESS | {"blocked"}
        and current["revision"] == previous["revision"] + 1,
        "DAILY_MARKET_TERMINAL_OR_REVISION")
    immutable = set(previous) - {"revision", "updated_at", "predecessor", "calendars", "price_end",
        "market_reference", "status", "reason_code"} - ({"masters"} if previous["schema"] == MASTER_SCHEMA else set())
    require(all(previous[key] == current[key] for key in immutable)
        and _stamp(current["updated_at"]) >= _stamp(previous["updated_at"]), "DAILY_MARKET_INTENT_CHANGED")
    if "write_reconciliation" in previous:
        require(previous["revision"] == 9 and current["revision"] == 10
            and current["status"] in {"complete", "blocked"}
            and current["calendars"] == previous["calendars"]
            and current["price_end"] == previous["price_end"]
            and (current["status"] == "complete" or current["market_reference"] == previous["market_reference"]),
            "DAILY_WRITE_RECONCILIATION_PHASE")
    if previous["schema"] == MASTER_SCHEMA:
        changed_masters = [v for v in VENUES if previous["masters"][v] != current["masters"][v]]
        require(len(changed_masters) <= 1, "DAILY_MASTER_TRANSITION")
        if changed_masters:
            before, after = (state["masters"][changed_masters[0]] for state in (previous, current))
            require(previous["status"] == "master-pending"
                and (before["status"], after["status"]) in {
                    ("pending", "in-flight"), ("in-flight", "complete"), ("in-flight", "failed")}
                and current["status"] == ("blocked" if after["status"] == "failed" else "master-pending")
                and all(previous[key] == current[key] for key in ("calendars", "price_end", "market_reference")),
                "DAILY_MASTER_TRANSITION")
            return
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
        if previous["schema"] == MASTER_SCHEMA:
            require(current["status"] in {
                "calendar-pending": {"master-pending", "no-new-session", "blocked"},
                "master-pending": {"market-pending", "blocked"},
                "market-pending": {"complete", "blocked"},
            }.get(previous["status"], set()), "DAILY_MASTER_PHASE_TRANSITION")
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
                if "write_reconciliation" not in previous["cycle"] and "write_reconciliation" in current:
                    from decision_kernel.application.daily_write_reconciliation import verify_original
                    verify_original(previous["cycle"], current["write_reconciliation"],
                        registry=registry, store=reader, snapshot=head)
                if current.get("write_reconciliation") is not None and current["status"] == "complete":
                    from decision_kernel.application.daily_write_reconciliation import verify_completed
                    verify_completed(current, registry=registry, store=reader)
            _verify_master_edges(None if previous is None else previous["cycle"], current, registry, reader)
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
            if "write_reconciliation" not in before["cycle"] and "write_reconciliation" in state:
                from decision_kernel.application.daily_write_reconciliation import (
                    ORIGINAL_PRICE_BUDGET, verify_original,
                )
                grant = state["write_reconciliation"]
                restored = verify_original(before["cycle"], grant, registry=registry,
                    store=reader, snapshot=snapshot)
                plan = restored["collection"]["plan"]
                targets = sorted([{"symbol": symbol, "adjust_kind": kind} for symbol in protected_symbols
                    for kind in ("none", "forward")], key=lambda target: (target["symbol"], target["adjust_kind"]))
                require(len(targets) == ORIGINAL_PRICE_BUDGET and plan["targets"] == targets,
                    "DAILY_MARKET_POLICY_CHANGED")
                require(latest_ftshare_reference(reader, registry, collection_id=plan["collection_id"],
                    generation=plan["generation"]) == grant["settled_market_reference"],
                    "DAILY_WRITE_RECONCILIATION_FT_FRONTIER_CHANGED")
                check_cycle_frontier(state, registry, reader, protected_symbols=protected_symbols)
            if state.get("write_reconciliation") is not None and state["status"] == "complete":
                from decision_kernel.application.daily_write_reconciliation import verify_completed
                verify_completed(state, registry=registry, store=reader)
            if any(before["cycle"]["calendars"][venue]["status"] in {"pending", "failed"}
                    and state["calendars"][venue]["status"] == "in-flight" for venue in VENUES):
                # The authority for one new calendar request and its durable
                # reservation must use the same snapshot and original CAS.
                check_cycle_frontier(state, registry, reader, protected_symbols=protected_symbols)
            if state["schema"] == MASTER_SCHEMA and any(
                    before["cycle"]["masters"][v]["status"] == "pending"
                    and state["masters"][v]["status"] == "in-flight" for v in VENUES):
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
        _verify_master_edges(None if before is None else before["cycle"], state, registry, reader)
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


def initial_cycle(approved, store, registry, *, at, admission_at=None, allow_late=False, master_refresh=None):
    approved = policy(approved)
    require(approved["enabled"], "DAILY_MARKET_PAUSED")
    admitted = admission_slot(approved, at=at if admission_at is None else admission_at,
                              allow_late=allow_late)
    require(_stamp(at) >= _stamp(admitted["admitted_at"]), "DAILY_MARKET_CLOCK_ROLLBACK")
    # The slot chooses only date/window/generation. Never rewrite the real
    # source-intent creation time to impersonate execution on the business day.
    stamp = _stamp(admitted["scheduled_for"]).astimezone(ZONE)
    with market_read_scope(store) as reader:
        result = _initial_cycle(approved, reader, registry, at=at, stamp=stamp, master_refresh=master_refresh)
    return {**result, "admission": admitted}


def _initial_cycle(approved, store, registry, *, at, stamp, master_refresh=None):
    if master_refresh is not None:
        from decision_kernel.application.daily_master_refresh import policy as master_policy
        master_refresh = master_policy(master_refresh)
    previous = latest_cycle(store, registry=registry, protected_symbols=approved["symbols"])
    if previous is not None:
        require(previous["cycle"]["policy_sha256"] == policy_digest(approved), "DAILY_MARKET_POLICY_CHANGED")
        if previous["cycle"]["schema"] == MASTER_SCHEMA:
            require(master_refresh == previous["cycle"]["master_refresh"], "DAILY_MASTER_POLICY_CHANGED")
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
    if master_refresh is not None:
        frozen = deepcopy(approved)
        frozen.pop("enabled")
        state.update(schema=MASTER_SCHEMA, daily_policy=frozen, master_refresh=deepcopy(master_refresh),
            masters={venue: {"status": "pending", "reserved_queries": 0, "reference": None,
                "checkpoint_reference": None, "reason_code": None} for venue in VENUES})
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
