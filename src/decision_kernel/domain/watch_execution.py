"""Finite, explicit Watch execution and record-only delivery state.

This module owns no clock, queue, sender, credentials or mutable pointer. A
time slot selects an opportunity; it is never a substitute observation time.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta, timezone

from decision_kernel.domain.conditional_odds import shape, timestamp
from decision_kernel.domain.records import RecordError, checked_text, digest, encoded, identifier, require
from decision_kernel.domain.research_artifacts import artifact_reference

PLAN_SCHEMA = "watch-execution-plan-v1"
SLOT_SCHEMA = "watch-execution-slot-v1"
RESULT_SCHEMA = "watch-execution-result-v1"
DELIVERY_SCHEMA = "watch-delivery-event-v1"
MAX_SLOTS = 32
MAX_DELIVERY_EVENTS = 16
MAX_EXECUTION_BYTES = 256 * 1024
NOTICE_POLICY = "first-trigger-per-condition"


def utc(value):
    try:
        return timestamp(value).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("WATCH_EXECUTION_TIME_RANGE") from None


def reference(value):
    return asdict(artifact_reference(value))


def reference_identity(value):
    ref = reference(value)
    return {key: ref[key] for key in ("repository", "path", "sha256")}


def validate_plan(payload):
    shape(payload, {"schema", "plan_id", "watch_ref", "starts_at", "interval_seconds", "slot_count",
                    "late_tolerance_seconds", "notice_policy", "effect", "limitations"})
    require(payload["schema"] == PLAN_SCHEMA, "WATCH_EXECUTION_PLAN_SCHEMA")
    identifier(payload["plan_id"])
    interval, count, tolerance = (payload[k] for k in ("interval_seconds", "slot_count", "late_tolerance_seconds"))
    require(type(interval) is int and 1 <= interval <= 31536000, "WATCH_EXECUTION_INTERVAL_INVALID")
    require(type(count) is int and 1 <= count <= MAX_SLOTS, "WATCH_EXECUTION_SLOT_LIMIT")
    require(type(tolerance) is int and 0 <= tolerance < interval, "WATCH_EXECUTION_TOLERANCE_INVALID")
    require(payload["notice_policy"] == NOTICE_POLICY, "WATCH_EXECUTION_NOTICE_POLICY_REQUIRED")
    require(payload["effect"] == "record-only", "WATCH_EXECUTION_RECORD_ONLY")
    limitations = payload["limitations"]
    require(isinstance(limitations, list) and 1 <= len(limitations) <= 16, "WATCH_EXECUTION_LIMITATIONS_REQUIRED")
    for item in limitations:
        checked_text(item, limit=4096)
    plan = deepcopy(payload)
    plan["watch_ref"], plan["starts_at"] = reference(plan["watch_ref"]), utc(plan["starts_at"])
    # Detect date overflow at validation, before any persistence or selection.
    slot_window(plan, count - 1)
    return plan


def slot_window(plan, index):
    require(type(index) is int and 0 <= index < plan["slot_count"], "WATCH_EXECUTION_SLOT_INDEX_INVALID")
    try:
        due = timestamp(plan["starts_at"]) + timedelta(seconds=index * plan["interval_seconds"])
        deadline = due + timedelta(seconds=plan["late_tolerance_seconds"])
    except OverflowError:
        raise RecordError("WATCH_EXECUTION_TIME_RANGE") from None
    return {"slot_index": index, "due_at": utc(due.isoformat()), "deadline": utc(deadline.isoformat())}


def slot_identity(plan, index):
    slot_window(plan, index)
    return digest(encoded([digest(encoded(plan)), index]))


def submission_keys(key):
    return {"reserve": "watch-slot-" + key, "check": "watch-check-" + key,
            "finish": "watch-result-" + key}


def select_slot(plan, at):
    clock = timestamp(utc(at))
    start = timestamp(plan["starts_at"])
    last = slot_window(plan, plan["slot_count"] - 1)
    if clock < start:
        return {"state": "not-due", "slot": None, "next_due_at": plan["starts_at"]}
    if clock > timestamp(last["deadline"]):
        return {"state": "plan-ended", "slot": None, "next_due_at": None}
    index = int((clock - start).total_seconds() // plan["interval_seconds"])
    slot = slot_window(plan, index)
    due = clock <= timestamp(slot["deadline"])
    return {"state": "due" if due else "missed", "slot": slot,
            "next_due_at": slot_window(plan, index + 1)["due_at"] if index + 1 < plan["slot_count"] else None}


def validate_query(payload):
    shape(payload, {"plan_ref", "at", "input", "review_reasons"})
    require(payload["input"] is None or isinstance(payload["input"], dict), "WATCH_INPUT_INVALID")
    reasons = payload["review_reasons"]
    require(isinstance(reasons, list) and len(reasons) <= 16, "WATCH_REVIEW_REASONS_INVALID")
    for reason in reasons:
        checked_text(reason, limit=4096)
    require(len(encoded(payload)) <= MAX_EXECUTION_BYTES, "WATCH_EXECUTION_INPUT_LIMIT")
    result = deepcopy(payload)
    result["plan_ref"], result["at"] = reference(result["plan_ref"]), utc(result["at"])
    return result


def effects():
    return {"mode": "record-only", "scheduler_created": False, "source_requests": 0,
            "transport_configured": False, "notification_sent": False,
            "user_read_verified": False, "investment_authority": "none"}


def notice_intents(plan, check_ref, event):
    result = event["result"]
    if result["schema"] == "odds-watch-check-v2":
        candidates = [row for row in result["condition_results"] if row["status"] == "first-trigger"]
    else:
        candidates = [{"condition_id": None, "status": result["status"]}] if result["status"] == "first-trigger" else []
    notices = []
    for row in candidates:
        identity = row["condition_id"]
        key = digest(encoded({"watch_key": event["watch_key"], "check": reference_identity(check_ref),
                              "condition_id": identity, "policy": plan["notice_policy"]}))
        notices.append({"notice_id": key, "policy": plan["notice_policy"], "watch_ref": plan["watch_ref"],
                        "watch_key": event["watch_key"], "check_ref": reference(check_ref),
                        "condition_id": identity, "condition_status": row["status"],
                        "group_status": result["status"], "checked_at": result["request"]["checked_at"],
                        "condition": deepcopy(row.get("condition")),
                        "reason": deepcopy(row.get("reason", result.get("reason"))),
                        "evidence": deepcopy(row.get("evidence")),
                        "state": "pending-transport-selection", "destination": None,
                        "limitations": deepcopy(plan["limitations"]), "effects": effects()})
    return notices


def validate_delivery_change(payload):
    shape(payload, {"execution_ref", "notice_id", "expected_event", "action", "at", "receipt_ref", "note"})
    identifier(payload["notice_id"])
    require(len(payload["notice_id"]) == 64 and all(c in "0123456789abcdef" for c in payload["notice_id"]),
            "WATCH_DELIVERY_NOTICE_INVALID")
    require(payload["action"] in ("reserve-attempt", "mark-unknown", "record-receipt", "record-feedback", "cancel"),
            "WATCH_DELIVERY_ACTION_UNSUPPORTED")
    checked_text(payload["note"], limit=4096)
    result = deepcopy(payload)
    result["execution_ref"], result["at"] = reference(result["execution_ref"]), utc(result["at"])
    for key in ("expected_event", "receipt_ref"):
        if result[key] is not None:
            result[key] = reference(result[key])
    require((result["receipt_ref"] is not None) == (result["action"] in ("record-receipt", "record-feedback")),
            "WATCH_DELIVERY_EXACT_RECEIPT_REQUIRED")
    return result


def delivery_state(before, change, receipt):
    """An attempt reservation is not proof a sender ran; claims stay unverified."""
    previous = deepcopy(before) if before else {"status": "pending-transport-selection", "attempt_reserved": False,
        "send_outcome": "not-observed", "receipts": [], "feedback": [], "cancelled": False}
    action = change["action"]
    require(not previous["cancelled"], "WATCH_DELIVERY_CANCELLED")
    if action == "reserve-attempt":
        require(not previous["attempt_reserved"], "WATCH_DELIVERY_RETRY_NOT_ALLOWED")
        previous.update(status="attempt-reserved-no-sender", attempt_reserved=True)
    elif action == "cancel":
        require(not previous["attempt_reserved"], "WATCH_DELIVERY_ATTEMPT_CANNOT_BE_CANCELLED")
        previous.update(status="cancelled-before-attempt", cancelled=True)
    else:
        require(previous["attempt_reserved"], "WATCH_DELIVERY_ATTEMPT_REQUIRED")
        if action == "mark-unknown":
            require(previous["send_outcome"] != "unknown", "WATCH_DELIVERY_ALREADY_UNKNOWN")
            previous.update(status="send-outcome-unknown", send_outcome="unknown")
        else:
            field = "receipts" if action == "record-receipt" else "feedback"
            require(receipt is not None, "WATCH_DELIVERY_EXACT_RECEIPT_REQUIRED")
            require(all(reference_identity(row["reference"]) != reference_identity(receipt["reference"])
                        for row in previous[field]), "WATCH_DELIVERY_DUPLICATE_RECEIPT")
            previous[field].append(deepcopy(receipt))
            previous["status"] = "receipt-recorded-unverified" if field == "receipts" else "feedback-recorded-unverified"
    previous["user_read_verified"] = False
    previous["transport_delivery_verified"] = False
    previous["automatic_retry_allowed"] = False
    return previous
