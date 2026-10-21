"""Read one pinned daily policy and interpret native schedule metadata only.

The producer owns admission. This observer neither repairs a missing trigger nor
certifies source acquisition, saved content, or the policy of an older commit.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime

from decision_kernel.application.daily_clock import (
    CLOCK_PATH, CLOCK_PROBE, EXTERNAL_CLOCK, EXTERNAL_RUN_TITLE, PROBE_RUN_TITLE, clock_policy,
)
from decision_kernel.application.daily_market import admission_slot, policy
from decision_kernel.domain.records import RecordError, SHA, digest, require
from decision_kernel.infrastructure.github_store import _json

WORKFLOW = "daily-market.yml"
POLICY_PATH = "config/daily-market.json"
SCHEDULE_QUERY = "/runs?branch=main&event=schedule&per_page=20"
DISPATCH_QUERY = "/runs?branch=main&event=workflow_dispatch&per_page=20"
MAX_POLICY_BYTES = 8192


def _read_config(get, main, path, error):
    value = get(f"/contents/{path}?ref={main}")
    require(isinstance(value, dict) and value.get("type") == "file"
            and value.get("path") == path and value.get("encoding") == "base64"
            and type(value.get("size")) is int and 0 < value["size"] <= MAX_POLICY_BYTES
            and isinstance(value.get("sha"), str) and SHA.fullmatch(value["sha"])
            and isinstance(value.get("content"), str)
            and len(value["content"]) <= 2 * MAX_POLICY_BYTES, error + "_FILE")
    try:
        raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
    except ValueError:
        raise RecordError(error + "_ENCODING") from None
    require(len(raw) == value["size"], error + "_SIZE")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    require(blob == value["sha"], error + "_BLOB")
    return raw, {"main": main, "path": path, "git_blob": blob,
                 "sha256": digest(raw), "size": len(raw),
                 "interpretation": "pinned-current-main-policy-not-historical-policy-proof"}


def read_policy(get, main):
    """One fixed Contents GET; retain identity/digests, never the seed reference."""
    raw, proof = _read_config(get, main, POLICY_PATH, "HEALTH_DAILY_POLICY")
    approved = policy(_json(raw))
    proof.update(enabled=approved["enabled"], schedule_utc=approved["schedule_utc"],
                 not_before_local=approved["not_before_local"])
    return approved, proof


def read_clock_policy(get, main):
    raw, proof = _read_config(get, main, CLOCK_PATH, "HEALTH_DAILY_CLOCK_POLICY")
    approved = clock_policy(_json(raw))
    return {**proof, "primary": approved["primary"]}


def clock_entry(row):
    """Retain only a fixed entry label, never raw titles, actors or provenance."""
    title = row.get("display_title")
    return (EXTERNAL_CLOCK if title == EXTERNAL_RUN_TITLE else
            CLOCK_PROBE if title == PROBE_RUN_TITLE else "unclassified")


def _clock(value):
    # The caller has already validated all native clocks and the evaluation clock.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _slot(approved, at):
    try:
        return admission_slot(approved, at=at, allow_late=True)
    except OverflowError:
        raise RecordError("HEALTH_SCHEDULE_CLOCK_RANGE") from None
    except RecordError as error:
        if str(error) != "DAILY_MARKET_NO_RECENT_SCHEDULE":
            raise
        return None


def _slot_key(value):
    return value["scheduled_for"] if value else None


def describe(approved, proof, rows, *, total, limit, as_of, observed_from,
             stable, workflow_state, default_branch, explicit_clock, entry=None):
    """Describe a bounded observed slot, using created_at exactly like production."""
    now = _clock(as_of)
    require(_clock(observed_from) <= now, "HEALTH_SCHEDULE_CLOCK_REVERSED")
    require(all(_clock(row[key]) <= now for row in rows
                for key in ("created_at", "run_started_at", "updated_at")), "HEALTH_FUTURE_SCHEDULE_RUN")
    expected = _slot(approved, as_of)
    first_slot = _slot(approved, observed_from)
    recent = sorted(rows, key=lambda row: (_clock(row["run_started_at"]), row["id"]), reverse=True)
    interpreted = [{"run": row, "admission_slot": _slot(approved, row["created_at"]),
                    "first_attempt_visible": row["run_attempt"] == 1} for row in recent
                   if entry is None or row.get("clock_entry") == entry]
    matches = [item for item in interpreted if expected is not None
               and _slot_key(item["admission_slot"]) == _slot_key(expected)]
    complete = total == len(rows)
    covered = (complete or bool(rows) and expected is not None
               and min(_clock(row["created_at"]) for row in rows) < _clock(expected["scheduled_for"]))
    changed_clock = _slot_key(first_slot) != _slot_key(expected)
    report = {"status": "unknown", "as_of": as_of, "observation_started_at": observed_from,
              "clock_basis": "explicit-as-of" if explicit_clock else "inspection-clock",
              "policy": proof, "workflow_state": workflow_state,
              "default_branch": "main" if default_branch == "main" else "not-confirmed-main",
              "expected_slot": expected, "observation_slot_changed": changed_clock,
              "recent_scope": {"limit": limit, "returned": len(rows), "total": total,
                               "complete_history": complete, "expected_slot_covered": covered if expected else None},
              "current_slot_runs": matches,
              "latest": interpreted[0] if interpreted else None,
              "last_visible_success": next((item for item in interpreted
                                            if item["run"]["conclusion"] == "success"), None),
              "last_visible_non_success": next((item for item in interpreted
                    if item["run"]["status"] == "completed" and item["run"]["conclusion"] != "success"), None),
              "run_slot_clock": "native-created-at", "root_cause": "not-established",
              "schedule_payload": "not-inspected", "jobs_and_artifacts": "not-inspected-for-this-observation",
              "business_result": "not-inspected", "historical_gaps_resolved": False}
    if not stable or changed_clock:
        report["status"] = "changed-during-inspection"
    elif default_branch != "main":
        report["error"] = "HEALTH_DEFAULT_BRANCH_NOT_MAIN"
    elif workflow_state != "active":
        report["status"] = "disabled"
    elif not approved["enabled"]:
        report["status"] = "policy-paused"
    elif expected is None:
        report["status"] = "no-recent-slot"
    elif not covered:
        report["error"] = "HEALTH_SCHEDULE_WINDOW_INCOMPLETE"
    elif not matches:
        report["status"] = "not-observed"
    elif any(not item["first_attempt_visible"] for item in matches):
        report["error"] = "HEALTH_SCHEDULE_FIRST_ATTEMPT_NOT_VISIBLE"
    elif any(item["run"]["status"] != "completed" for item in matches):
        report["status"] = "in-progress"
    else:
        latest = matches[0]["run"]
        report["status"] = ("native-success" if latest["conclusion"] == "success"
                            else "native-non-success")
    return report


def describe_external(approved, proof, rows, *, clock_proof, detail, **options):
    """A named dispatch entry is observable; its external origin is not proven."""
    report = describe(approved, proof, rows, entry=EXTERNAL_CLOCK, **options)
    report.update(entry_marker=EXTERNAL_CLOCK, clock_policy=clock_proof,
                  natural_origin="unverified", provider_configuration="not-inspected",
                  entry_identification="fixed-run-name-not-payload-or-provider-proof",
                  matched_entries=sum(row.get("clock_entry") == EXTERNAL_CLOCK for row in rows),
                  latest_detail=detail, dispatch_payload="not-inspected")
    report.pop("schedule_payload")
    probes = sorted((row for row in rows if row.get("clock_entry") == CLOCK_PROBE),
                    key=lambda row: (_clock(row["run_started_at"]), row["id"]), reverse=True)
    report["latest_probe"] = probes[0] if probes else None
    report["jobs_and_artifacts"] = "latest-external-entry-metadata-only" if detail else "not-inspected"
    if detail and detail["artifact"]:
        require(_clock(detail["artifact"]["created_at"]) <= _clock(options["as_of"]),
                "HEALTH_FUTURE_EXTERNAL_ARTIFACT")
    report["native_entry_status"] = report["status"]
    if report["status"] == "native-success":
        selected = report["current_slot_runs"][0]["run"]
        if selected["head_sha"] != clock_proof["main"]:
            report["status"] = "policy-at-run-unverified"
        elif detail is None or detail["run"] != selected:
            report.update(status="unknown", error="HEALTH_EXTERNAL_CURRENT_RUN_NOT_INSPECTED")
        elif detail["status"] != "inspected":
            report["status"] = "changed-during-inspection"
        elif detail["job_state"] != "complete":
            report["status"] = "in-progress"
        else:
            executed = [job for job in detail["jobs"] if job["name"] == "daily"
                        and job["conclusion"] != "skipped"]
            artifact = detail["artifact"]
            if len(executed) != 1 or executed[0]["conclusion"] != "success":
                report["status"] = "daily-not-successful"
            elif artifact is None or artifact["expired"] or _clock(artifact["expires_at"]) <= _clock(options["as_of"]):
                report["status"] = "delivery-metadata-unavailable"
            else:
                report["status"] = "daily-job-success"
    return report
