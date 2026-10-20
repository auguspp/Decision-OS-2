"""Explicit record-only Watch lifecycle, separate from condition truth."""
from copy import deepcopy

from decision_kernel.domain.conditional_odds import shape, timestamp
from decision_kernel.domain.records import SHA, checked_text, require
from decision_kernel.domain.research_artifacts import artifact_reference

ACTIVITY_RECORD = "odds-watch-activity-record-v1"
ACTIVITY_PREFIX = "research/watch-activities/"
MAX_EVENTS = 128  # Full replay is bounded; no trusted/truncated prefix summaries.


def validate_change(payload):
    keys = {"watch_ref", "expected_event", "action", "at", "reason", "seed_check"}
    versioned = isinstance(payload, dict) and "history_version" in payload
    shape(payload, keys | ({"history_version"} if versioned else set()))
    artifact_reference(payload["watch_ref"])
    if payload["expected_event"] is not None: artifact_reference(payload["expected_event"])
    if versioned:
        from decision_kernel.domain.watch_history import HISTORY_VERSION
        require(payload["history_version"] == HISTORY_VERSION and payload["action"] in ("register", "migrate"),
                "WATCH_ACTIVITY_HISTORY_REGISTRATION_REQUIRED")
        require(payload["action"] != "migrate" or payload["expected_event"] is not None,
                "WATCH_ACTIVITY_NOT_REGISTERED")
    else:
        require(payload["action"] in ("register", "pause", "resume", "retire"), "WATCH_ACTIVITY_ACTION_UNSUPPORTED")
    timestamp(payload["at"]); checked_text(payload["reason"], limit=4096)
    if payload["seed_check"] is not None:
        artifact_reference(payload["seed_check"])
        require(payload["action"] == "register", "WATCH_ACTIVITY_SEED_ONLY_AT_REGISTRATION")
    return deepcopy(payload)


def validate_check(payload):
    shape(payload, {"watch_ref", "expected_event", "checked_at", "records_snapshot", "input", "review_reasons"})
    artifact_reference(payload["watch_ref"]); artifact_reference(payload["expected_event"])
    timestamp(payload["checked_at"])
    require(isinstance(payload["records_snapshot"], str) and SHA.fullmatch(payload["records_snapshot"]),
            "WATCH_EXACT_RECORDS_SNAPSHOT_REQUIRED")
    require(payload["input"] is None or isinstance(payload["input"], dict), "WATCH_INPUT_INVALID")
    reasons = payload["review_reasons"]
    require(isinstance(reasons, list) and len(reasons) <= 16, "WATCH_REVIEW_REASONS_INVALID")
    for reason in reasons: checked_text(reason, limit=4096)
    return deepcopy(payload)


def blank_summary(seeded=False):
    return {"events": 0, "checks": 0, "performed": 0, "first_triggers": 0, "continued_triggers": 0,
            "not_met": 0, "cannot_determine": 0, "review_required": 0, "ended": 0, "not_due": 0,
            "last_check_at": None, "last_check_status": None, "seed_control_state_retained": seeded,
            "coverage": "managed-events-only; seed-history-not-added-to-counts"}


def summarize(before, result):
    summary = deepcopy(before)
    summary["events"] += 1
    if result is not None:
        summary["checks"] += 1
        summary["performed"] += int(result["check_performed"])
        key = {"first-trigger": "first_triggers", "continued-trigger": "continued_triggers",
               "not-met": "not_met", "cannot-determine": "cannot_determine", "review-required": "review_required",
               "ended": "ended", "not-due": "not_due"}.get(result["status"])
        if key: summary[key] += 1
        summary["last_check_at"] = result["request"]["checked_at"]
        summary["last_check_status"] = result["status"]
        if result["schema"] in ("odds-watch-check-v2", "odds-watch-check-v3"):
            # Group truth and each declared condition have distinct histories.
            # Seeded standalone checks carry control state, never these counts.
            previous = {row["condition_id"]: row for row in summary.get("condition_summaries", [])}
            rows = []
            for condition in result["condition_results"]:
                identity = condition["condition_id"]
                row = previous.get(identity, {"condition_id": identity, "checks": 0, "performed": 0,
                    "first_triggers": 0, "continued_triggers": 0, "not_met": 0, "cannot_determine": 0,
                    "review_required": 0, "ended": 0, "not_due": 0, "last_check_at": None,
                    "last_check_status": None})
                row["checks"] += 1
                row["performed"] += int(condition["check_performed"])
                counter = {"first-trigger": "first_triggers", "continued-trigger": "continued_triggers",
                    "not-met": "not_met", "cannot-determine": "cannot_determine", "review-required": "review_required",
                    "ended": "ended", "not-due": "not_due"}.get(condition["status"])
                if counter:
                    row[counter] += 1
                row["last_check_at"] = result["request"]["checked_at"]
                row["last_check_status"] = condition["status"]
                rows.append(row)
            summary["condition_summaries"] = rows
            summary["condition_coverage"] = "each-declared-condition; managed-checks-only; seed-counts-excluded"
    return summary
