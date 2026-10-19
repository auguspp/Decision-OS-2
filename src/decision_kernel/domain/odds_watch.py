"""Finite, explicit Watch conditions; no scheduler, probability inference or action."""
from copy import deepcopy

from decision_kernel.domain.conditional_odds import number, shape, timestamp, validate_close_policy
from decision_kernel.domain.records import BlobRef, checked_text, encoded, identifier, require
from decision_kernel.domain.research_artifacts import artifact_reference

WATCH_RECORD = "frozen-odds-watch-record-v1"
CHECK_RECORD = "odds-watch-check-record-v1"
WORLD_METRICS = {"value_to_price_distance", "undiscounted_holding_period_return"}
AGGREGATE_METRICS = {"expected_holding_period_return", "positive_return_scenario_probability"}
MAX_HISTORY = 32  # Explicit bounded replay, not a permanent monitoring service.


def validate_watch(payload):
    shape(payload, {"schema", "watch_id", "basis", "condition", "activation", "window",
                    "data_policy", "effect", "limitations"})
    require(payload["schema"] == "odds-watch-v1", "WATCH_VERSION_UNSUPPORTED")
    require(len(encoded(payload)) <= 64 * 1024, "WATCH_SIZE_LIMIT")
    w = deepcopy(payload)
    identifier(w["watch_id"])
    shape(w["basis"], {"kind", "reference"})
    mode = w["basis"]["kind"]
    require(mode in ("research", "conditional", "probability"), "WATCH_BASIS_UNSUPPORTED")
    if mode == "research":
        shape(w["basis"]["reference"], {"repository", "commit", "path", "sha256"})
        BlobRef(**w["basis"]["reference"])
    else:
        artifact_reference(w["basis"]["reference"])
    c = w["condition"]
    shape(c, {"metric", "world_id", "comparison", "threshold", "basis_note"})
    checked_text(c["basis_note"], limit=4096)
    checked_text(c["metric"], limit=64); checked_text(c["comparison"], limit=32)
    if mode == "research":
        require(c["metric"] == "research_version" and c["comparison"] == "changed"
                and c["threshold"] is None and c["world_id"] is None, "WATCH_RESEARCH_CONDITION_ONLY")
    else:
        allowed = {"price"} | WORLD_METRICS | (AGGREGATE_METRICS if mode == "probability" else set())
        require(c["metric"] in allowed and c["comparison"] in ("lte", "gte"), "WATCH_METRIC_UNSUPPORTED")
        value = number(c["threshold"])
        if c["metric"] == "price": require(value > 0, "WATCH_POSITIVE_PRICE_THRESHOLD")
        if c["metric"] == "positive_return_scenario_probability":
            require(0 <= value <= 1, "WATCH_PROBABILITY_THRESHOLD_RANGE")
        if c["metric"] in WORLD_METRICS: identifier(c["world_id"])
        else: require(c["world_id"] is None, "WATCH_UNEXPECTED_WORLD")
    a = w["activation"]
    shape(a, {"enabled", "as_of", "basis_note"})
    require(type(a["enabled"]) is bool, "WATCH_EXPLICIT_ACTIVATION_REQUIRED")
    timestamp(a["as_of"]); checked_text(a["basis_note"], limit=4096)
    win = w["window"]
    shape(win, {"starts_at", "ends_at", "minimum_interval_seconds", "end_after_trigger"})
    require(timestamp(a["as_of"]) <= timestamp(win["starts_at"]) < timestamp(win["ends_at"]),
            "WATCH_WINDOW_INVALID")
    require(type(win["minimum_interval_seconds"]) is int and 0 <= win["minimum_interval_seconds"] <= 31536000
            and type(win["end_after_trigger"]) is bool, "WATCH_WINDOW_INVALID")
    if mode == "conditional":
        p = w["data_policy"]
        if isinstance(p, dict) and p.get("price_kind") == "retained-ftshare-raw-close":
            shape(p, {"price_kind", "market_policy"})
            validate_close_policy(p["market_policy"])
            require(timestamp(p["market_policy"]["as_of"]) <= timestamp(a["as_of"]),
                    "WATCH_POLICY_AFTER_ACTIVATION")
        else:
            shape(p, {"price_kind", "max_price_age_days", "max_capital_age_days"})
            require(p["price_kind"] in ("declared-reference", "relay-observation"), "WATCH_PRICE_REQUIREMENT_UNSUPPORTED")
            for key in ("max_price_age_days", "max_capital_age_days"):
                require(type(p[key]) is int and 1 <= p[key] <= 36525, "WATCH_AGE_POLICY_REQUIRED")
    else:
        require(w["data_policy"] is None, "WATCH_UNEXPECTED_DATA_POLICY")
    require(w["effect"] == "record-only", "WATCH_EFFECT_NOT_AUTHORIZED")
    require(isinstance(w["limitations"], list) and 1 <= len(w["limitations"]) <= 32, "WATCH_LIMITATIONS_REQUIRED")
    for text in w["limitations"]: checked_text(text, limit=4096)
    return w


def validate_request(payload):
    shape(payload, {"watch_ref", "checked_at", "records_snapshot", "previous_check", "input", "review_reasons"})
    artifact_reference(payload["watch_ref"])
    timestamp(payload["checked_at"])
    import re
    require(isinstance(payload["records_snapshot"], str) and re.fullmatch(r"[0-9a-f]{40}", payload["records_snapshot"]),
            "WATCH_EXACT_RECORDS_SNAPSHOT_REQUIRED")
    if payload["previous_check"] is not None: artifact_reference(payload["previous_check"])
    require(payload["input"] is None or isinstance(payload["input"], dict), "WATCH_INPUT_INVALID")
    reasons = payload["review_reasons"]
    require(isinstance(reasons, list) and len(reasons) <= 16, "WATCH_REVIEW_REASONS_INVALID")
    for reason in reasons: checked_text(reason, limit=4096)
    return deepcopy(payload)


def gate(watch, request, previous):
    """Not due is not a check; an end date is not evidence that an event occurred."""
    at = timestamp(request["checked_at"])
    require(at >= timestamp(watch["activation"]["as_of"]), "WATCH_CHECK_PRECEDES_ACTIVATION")
    if previous is not None:
        require(at > timestamp(previous["request"]["checked_at"]), "WATCH_CHECK_CLOCK_NOT_ADVANCING")
    win = watch["window"]
    if not watch["activation"]["enabled"]: return "not-enabled"
    if at >= timestamp(win["ends_at"]) or (win["end_after_trigger"] and previous and previous["ever_triggered"]):
        return "ended"
    if at < timestamp(win["starts_at"]): return "not-due"
    last = previous["last_attempt_at"] if previous else None
    if last and (at - timestamp(last)).total_seconds() < win["minimum_interval_seconds"]: return "not-due"
    return None


def project(watch, request, observation, previous):
    status = observation["status"]
    met = observation["condition_met"]
    last_match = previous["last_decisive_match"] if previous else None
    gap = previous["gap_since_decisive"] if previous else False
    if met is True:
        status = "continued-trigger" if last_match is True else "first-trigger"
    elif met is False:
        status = "not-met"
    checked = observation["status"] not in ("not-enabled", "not-due", "ended")
    return {"schema": "odds-watch-check-v1", "request": request, "observation": observation,
        "status": status, "condition_met": met, "check_performed": checked,
        "trigger_continuity": "intervening-checks-unknown" if met is True and gap else "supplied-check-chain-only",
        "last_decisive_match": met if met is not None else last_match,
        "gap_since_decisive": False if met is not None else gap or checked,
        "ever_triggered": bool(met is True or (previous and previous["ever_triggered"])),
        "last_attempt_at": request["checked_at"] if checked else previous["last_attempt_at"] if previous else None,
        "effects": {"mode": "record-only", "notification_sent": False, "research_started": False,
                    "investment_authority": "none", "scheduler_created": False},
        "limitations": watch["limitations"] + [
            "ACTIVATION_IS_A_CALLER_DECLARATION_NOT_AUTHENTICATED_HUMAN_ACCEPTANCE",
            "CHECK_SNAPSHOT_AND_TIME_ARE_EXPLICIT_NOT_HISTORICAL_PIT",
            "CURRENT_POINTER_EQUALITY_DOES_NOT_PROVE_ECONOMIC_ASSUMPTIONS_UNCHANGED",
            "PREDECESSOR_CHAIN_IS_NOT_GLOBAL_EXACTLY_ONCE_NOTIFICATION",
            "READBACK_REPLAYS_RECORDED_INPUTS_NOT_CURRENT_MARKET_OR_CURRENT_RESEARCH"]}
