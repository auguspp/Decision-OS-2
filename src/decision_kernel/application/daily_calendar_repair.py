"""One explicitly approved calendar repair; never a general source retry policy.

The original failed cycle and its spent request remain in the immutable chain.
A new reservation spends the additional grant before HTTP, so an interrupted
or failed repair cannot acquire a second attempt merely by starting a new run.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import re

from decision_kernel.domain.records import SourceRef, Selection, require

CYCLE_SCHEMA = "daily-market-cycle-v2"
PROOF_SCHEMA = "daily-calendar-repair-v1"
POLICY_FILE = Path("config/daily-calendar-repair.json")
TRANSPORT_CLASSES = frozenset({"tls-certificate", "tls", "dns-temporary", "dns", "timeout",
    "incomplete-read", "connection", "http-protocol", "transport-unclassified", "auth-or-entitlement",
    "rate-limit", "source-unavailable", "request-rejected", "temporary-upstream", "temporary-gateway",
    "http-unclassified"})


def proof(value):
    require(isinstance(value, dict) and set(value) == {
        "schema", "original_reference", "authorization_ref", "venue", "additional_requests"}
        and value["schema"] == PROOF_SCHEMA and isinstance(value["venue"], str)
        and value["venue"] in {"SSE", "SZSE"}
        and type(value["additional_requests"]) is int and value["additional_requests"] == 1,
        "DAILY_CALENDAR_REPAIR_PROOF")
    ref = SourceRef.from_dict(value["original_reference"])
    require(ref.selection == Selection() and isinstance(value["authorization_ref"], str)
        and re.fullmatch(r"https://github\.com/auguspp/Decision-OS-2/issues/10#issuecomment-[1-9][0-9]*",
                         value["authorization_ref"]), "DAILY_CALENDAR_REPAIR_PROOF")
    return deepcopy(value)


def load_grant():
    """Only the reviewed deployment file can start a repair, not dispatch input."""
    from decision_kernel.application.relay_registry import _json
    from decision_kernel.interfaces.ftshare_check_cli import _regular
    value = _json(_regular(POLICY_FILE, 8192))
    require(isinstance(value, dict) and set(value) == {"enabled", "repair"}
        and value["enabled"] is True, "DAILY_CALENDAR_REPAIR_NOT_AUTHORIZED")
    return proof(value["repair"])


def select_repair(item, grant, *, at):
    """Pin the original failure or its one repair, never select a new daily slot."""
    from decision_kernel.application.daily_market import _stamp, validate_cycle
    grant = proof(grant)
    require(isinstance(item, dict), "DAILY_CALENDAR_REPAIR_TARGET_REQUIRED")
    state = validate_cycle(item["cycle"])
    require(_stamp(at) >= _stamp(state["updated_at"]), "DAILY_MARKET_CLOCK_ROLLBACK")
    require(_stamp(at) - _stamp(state["created_at"]) < timedelta(days=7),
        "DAILY_CALENDAR_REPAIR_WINDOW_EXPIRED")
    if state.get("calendar_repair") is not None:
        require(state["calendar_repair"] == grant, "DAILY_CALENDAR_REPAIR_TARGET_CHANGED")
        return False  # Already spent. The normal owner may only resume unused stages.
    require(item["reference"] == grant["original_reference"], "DAILY_CALENDAR_REPAIR_TARGET_CHANGED")
    eligible(state, grant)
    return True


def eligible(state, grant):
    from decision_kernel.application.daily_market import SCHEMA, VENUES
    require(state["schema"] == SCHEMA and state["status"] == "blocked"
        and state["reason_code"] == "RELAY_HTTP_UNAVAILABLE"
        and state["price_end"] is None and state["market_reference"] is None,
        "DAILY_CALENDAR_REPAIR_NOT_TRANSPORT_FAILURE")
    row = state["calendars"][grant["venue"]]
    require(row == {"status": "failed", "reserved_requests": 1, "reference": None,
                    "reason_code": "RELAY_HTTP_UNAVAILABLE"}
        and all(state["calendars"][v]["status"] in {"pending", "complete"}
                for v in VENUES if v != grant["venue"]), "DAILY_CALENDAR_REPAIR_NOT_TRANSPORT_FAILURE")


def reserve(item, grant, *, at):
    from decision_kernel.application.daily_market import next_state
    require(select_repair(item, grant, at=at), "DAILY_CALENDAR_REPAIR_ALREADY_SPENT")
    calendars = deepcopy(item["cycle"]["calendars"])
    calendars[grant["venue"]] = {"status": "in-flight", "reserved_requests": 2,
                                  "reference": None, "reason_code": None}
    return next_state(item, at=at, schema=CYCLE_SCHEMA, calendar_repair=proof(grant),
                      calendars=calendars, status="calendar-pending", reason_code=None)


def transition(previous, current):
    """Historical verification is independent of later grant revocation."""
    from decision_kernel.application.daily_market import _stamp
    grant = proof(current.get("calendar_repair"))
    eligible(previous, grant)
    require(current["schema"] == CYCLE_SCHEMA and current["predecessor"] == grant["original_reference"]
        and current["revision"] == previous["revision"] + 1
        and _stamp(current["updated_at"]) >= _stamp(previous["updated_at"]),
        "DAILY_CALENDAR_REPAIR_EDGE")
    immutable = set(previous) - {"schema", "revision", "predecessor", "updated_at", "calendars", "status", "reason_code"}
    require(all(previous[key] == current[key] for key in immutable), "DAILY_MARKET_INTENT_CHANGED")
    calendars = deepcopy(previous["calendars"])
    calendars[grant["venue"]] = {"status": "in-flight", "reserved_requests": 2,
                                  "reference": None, "reason_code": None}
    require(current["calendars"] == calendars and current["status"] == "calendar-pending"
        and current["reason_code"] is None, "DAILY_CALENDAR_REPAIR_RESERVATION_REQUIRED")


def diagnostic(venue, client, *, elapsed_seconds):
    """Project typed facts only: no raw error prose, body, URL, key or traceback."""
    import math
    meta = getattr(client, "last_error_meta", None)
    meta = meta if isinstance(meta, dict) else {}
    category = meta.get("classification")
    category = category if isinstance(category, str) and category in TRANSPORT_CLASSES else "unclassified"
    status = meta.get("http_status")
    status = status if type(status) is int and 100 <= status <= 599 else None
    elapsed = elapsed_seconds if type(elapsed_seconds) in (int, float) and math.isfinite(elapsed_seconds) \
        and 0 <= elapsed_seconds <= 86400 else None
    timeout = getattr(client, "request_timeout_seconds", None)
    return {"exchange": venue, "classification": category, "http_status": status,
        "elapsed_seconds": None if elapsed is None else round(elapsed, 3),
        "socket_timeout_seconds": timeout if type(timeout) is int and 0 < timeout <= 90 else None,
        "retry_performed": False}
