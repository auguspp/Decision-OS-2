"""Bounded native execution/retention inspection, never a scheduler or writer.

A successful Actions run is NOT verified business content, a durable backup,
proof of a configured timer, or permission to restart/cut over another owner.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from decision_kernel.application import daily_schedule_health
from decision_kernel.domain.records import RecordError, StoreUnavailable, SHA, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubBinding

WORKFLOWS = {
    "store-runtime.yml": "dos2-store",
    "research-runtime.yml": "dos2-research",
    "news-rolling.yml": "dos2-news-roll",
    "daily-market.yml": "dos2-daily-market",
}
RUN_LIMIT = 20
DETAIL_LIMIT = 100
# 6 repository/heads + 4*10 host metadata + 3 self + 4 explicit backup
# + 2 independent schedule pages + 1 pinned policy. No source/artifact-body GET.
MAX_READS = 56
MAX_METADATA_BYTES = 8 * 1024 * 1024
ACTIVE = {"queued", "in_progress", "requested", "waiting", "pending"}
CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "action_required", "neutral", "skipped", "stale", "startup_failure"}
RUN_FIELDS = ("id", "run_attempt", "workflow_id", "head_sha", "head_branch", "path", "event", "status", "conclusion", "created_at", "run_started_at", "updated_at")


def clock(value):
    require(isinstance(value, str) and len(value) <= 40, "HEALTH_CLOCK_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError("HEALTH_CLOCK_INVALID") from None
    require(parsed.tzinfo is not None, "HEALTH_CLOCK_INVALID")
    return parsed


def positive(value):
    require(type(value) is int and value > 0, "HEALTH_NATIVE_ID_INVALID")
    return value


def listing(value, key, limit, *, complete=False):
    require(isinstance(value, dict) and type(value.get("total_count")) is int,
            "HEALTH_LIST_INVALID")
    rows = value.get(key)
    require(isinstance(rows, list) and len(rows) <= limit and value["total_count"] >= len(rows),
            "HEALTH_LIST_INVALID")
    require(len(rows) == min(value["total_count"], limit), "HEALTH_LIST_INCOMPLETE")
    require(not complete or value["total_count"] == len(rows), "HEALTH_DETAIL_INCOMPLETE")
    require(all(isinstance(row, dict) for row in rows), "HEALTH_LIST_INVALID")
    ids = [positive(row.get("id")) for row in rows]
    require(len(ids) == len(set(ids)), "HEALTH_DUPLICATE_NATIVE_ID")
    return rows


def run_summary(row, binding, filename, workflow_id):
    require(isinstance(row, dict), "HEALTH_RUN_INVALID")
    result = {key: row.get(key) for key in RUN_FIELDS}
    positive(result["id"]); positive(result["run_attempt"]); positive(result["workflow_id"])
    require(result["workflow_id"] == workflow_id and result["path"] == ".github/workflows/" + filename
            and result["head_branch"] == "main", "HEALTH_RUN_IDENTITY")
    require(isinstance(result["head_sha"], str) and SHA.fullmatch(result["head_sha"]), "HEALTH_RUN_IDENTITY")
    repo = row.get("head_repository") or {}
    require(repo.get("full_name") == binding.repository and repo.get("id") == binding.repository_id,
            "HEALTH_RUN_REPOSITORY")
    require(result["event"] in {"workflow_dispatch", "schedule", "push", "repository_dispatch", "workflow_run"},
            "HEALTH_RUN_EVENT")
    require(result["status"] in ACTIVE | {"completed"}, "HEALTH_RUN_STATUS")
    require((result["status"] == "completed" and result["conclusion"] in CONCLUSIONS)
            or (result["status"] in ACTIVE and result["conclusion"] is None), "HEALTH_RUN_CONCLUSION")
    for key in ("created_at", "run_started_at", "updated_at"):
        clock(result[key])
    require(clock(result["created_at"]) <= clock(result["updated_at"])
            and clock(result["created_at"]) <= clock(result["run_started_at"]), "HEALTH_RUN_CLOCK_ORDER")
    result["url"] = f"https://github.com/{binding.repository}/actions/runs/{result['id']}/attempts/{result['run_attempt']}"
    return result


def _order(row):
    return clock(row["run_started_at"]), row["id"]


def _detail(get, run, binding, filename, workflow_id):
    ident = str(run["id"])
    jobs = listing(get(f"/actions/runs/{ident}/attempts/{run['run_attempt']}/jobs?per_page=100"),
                   "jobs", DETAIL_LIMIT, complete=True)
    summaries = []
    for job in jobs:
        positive(job.get("run_id")); positive(job.get("run_attempt"))
        require(job.get("run_id") == run["id"] and job.get("run_attempt") == run["run_attempt"]
                and job.get("head_sha") == run["head_sha"], "HEALTH_JOB_IDENTITY")
        require(job.get("status") in ACTIVE | {"completed"}, "HEALTH_JOB_STATUS")
        require((job["status"] == "completed" and job.get("conclusion") in CONCLUSIONS)
                or (job["status"] in ACTIVE and job.get("conclusion") is None), "HEALTH_JOB_CONCLUSION")
        name = job.get("name")
        require(isinstance(name, str) and 0 < len(name) <= 200, "HEALTH_JOB_NAME")
        summaries.append({key: job.get(key) for key in ("id", "name", "status", "conclusion")})
    artifacts = listing(get(f"/actions/runs/{ident}/artifacts?per_page=100"), "artifacts", DETAIL_LIMIT, complete=True)
    operation = "not-observed-in-native-metadata"
    artifact_prefix = WORKFLOWS[filename]
    if filename == "store-runtime.yml":
        operations = [j["name"] for j in summaries if j["name"] in {"health", "backup", "retain-guide"}
                      and j["conclusion"] != "skipped"]
        require(len(operations) <= 1, "HEALTH_STORE_OPERATION_AMBIGUOUS")
        operation = operations[0] if operations else "not-executed"
        if operation == "health":
            artifact_prefix = "dos2-health"
    elif filename == daily_schedule_health.WORKFLOW:
        operations = [j["name"] for j in summaries if j["name"] in {"status", "daily"}
                      and j["conclusion"] != "skipped"]
        require(len(operations) <= 1, "HEALTH_DAILY_OPERATION_AMBIGUOUS")
        operation = operations[0] if operations else "not-executed"
    expected_name = f"{artifact_prefix}-{ident}-{run['run_attempt']}"
    matches = [a for a in artifacts if a.get("name") == expected_name]
    require(len(matches) <= 1, "HEALTH_DUPLICATE_ARTIFACT")
    artifact = None
    if matches:
        a = matches[0]; owner = a.get("workflow_run") or {}
        positive(owner.get("id"))
        require(owner.get("id") == run["id"] and owner.get("head_sha") == run["head_sha"], "HEALTH_ARTIFACT_IDENTITY")
        require(type(a.get("expired")) is bool and type(a.get("size_in_bytes")) is int
                and a["size_in_bytes"] > 0, "HEALTH_ARTIFACT_INVALID")
        checksum = a.get("digest")
        require(isinstance(checksum, str) and len(checksum) == 71 and checksum.startswith("sha256:")
                and all(c in '0123456789abcdef' for c in checksum[7:]), "HEALTH_ARTIFACT_DIGEST")
        require(clock(a["created_at"]) < clock(a["expires_at"]), "HEALTH_ARTIFACT_CLOCK")
        artifact = {k: a[k] for k in ("id", "name", "size_in_bytes", "digest", "expired", "created_at", "expires_at")}
    confirmed = run_summary(get(f"/actions/runs/{ident}"), binding, filename, workflow_id)
    # Do not silently adopt a new attempt or a terminal result that changed while reading.
    stable = confirmed == run
    job_state = "complete" if jobs and all(j["status"] == "completed" for j in jobs) else "not-complete"
    if run["conclusion"] == "success":
        require(jobs and all(j["conclusion"] in {"success", "skipped"} for j in jobs)
                and any(j["conclusion"] == "success" for j in jobs), "HEALTH_SUCCESS_JOB_MISMATCH")
    return {"run": run, "status": "inspected" if stable else "changed-during-inspection",
            "jobs": summaries, "job_state": job_state, "artifact": artifact, "observed_operation": operation,
            "artifact_bytes_verified": False, "business_result": "not-inspected"}


def inspect_runtime(binding, api, *, as_of=None, expected_head=None, backup_run_id=None, self_run=None):
    """Use the existing single bounded API. Only fixed repository GET routes exist."""
    require(isinstance(binding, GitHubBinding) and not binding.write_enabled,
            "HEALTH_READ_ONLY_BINDING_REQUIRED")
    if as_of is not None:
        clock(as_of)  # Invalid caller clocks fail before I/O.
    observed_from = as_of or datetime.now(timezone.utc).isoformat()
    require(expected_head is None or isinstance(expected_head, str) and SHA.fullmatch(expected_head),
            "HEALTH_EXPECTED_HEAD_INVALID")
    if backup_run_id is not None:
        positive(backup_run_id)
    if self_run is not None:
        require(isinstance(self_run, dict) and set(self_run) == {"id", "run_attempt"}, "HEALTH_SELF_IDENTITY")
        positive(self_run["id"]); positive(self_run["run_attempt"])
    evidence, used = [], 0
    prefix = "/repos/" + binding.repository
    def get(path):
        nonlocal used
        require(len(evidence) < MAX_READS, "HEALTH_REQUEST_BOUND")
        value = api.request("GET", prefix + path)
        raw = encoded(value); used += len(raw)
        require(used <= MAX_METADATA_BYTES, "HEALTH_METADATA_BOUND")
        evidence.append({"path": path, "sha256": digest(raw)})
        return value
    verified_private = False
    def repository():
        nonlocal verified_private
        value = get("")
        actual_private = value.get("private") if isinstance(value, dict) else None
        require(isinstance(value, dict) and type(value.get("id")) is int
                and value.get("id") == binding.repository_id and value.get("full_name") == binding.repository
                and binding.accepts_privacy(actual_private)
                and not (verified_private and actual_private is False), "HEALTH_REPOSITORY_MISMATCH")
        if binding.private_upgrade_allowed and actual_private is True:
            verified_private = True
        return value.get("default_branch")
    def heads():
        values = {}
        for name in ("main", binding.branch):
            value = get("/git/ref/heads/" + quote(name, safe=""))
            obj = value.get("object") or {}
            require(value.get("ref") == "refs/heads/" + name and obj.get("type") == "commit"
                    and isinstance(obj.get("sha"), str) and SHA.fullmatch(obj["sha"]), "HEALTH_REF_INVALID")
            values[name] = obj["sha"]
        return values
    default_before = repository(); before = heads(); panels = []; store_workflow_id = None; self_verified = False
    schedule_observation = None
    for filename in WORKFLOWS:
        path = "/actions/workflows/" + filename
        panel = {"path": ".github/workflows/" + filename}
        try:
            workflow = get(path); wid = positive(workflow.get("id"))
            require(workflow.get("path") == panel["path"], "HEALTH_WORKFLOW_IDENTITY")
            if filename == "store-runtime.yml":
                store_workflow_id = wid
            state = workflow.get("state")
            require(state in {"active", "deleted", "disabled_manually", "disabled_inactivity", "disabled_fork"},
                    "HEALTH_WORKFLOW_STATE")
            native = get(path + "/runs?branch=main&per_page=20")
            rows = [run_summary(r, binding, filename, wid) for r in listing(native, "workflow_runs", RUN_LIMIT)]
            self_rows = [r for r in rows if self_run and r["id"] == self_run["id"]]
            require(not self_rows or filename == "store-runtime.yml" and len(self_rows) == 1
                    and self_rows[0]["run_attempt"] == self_run["run_attempt"], "HEALTH_SELF_IDENTITY")
            if self_rows:
                own = _detail(get, self_rows[0], binding, filename, wid)
                require(own["observed_operation"] == "health" and own["status"] == "inspected"
                        and self_rows[0]["status"] in ACTIVE,
                        "HEALTH_SELF_NOT_INSPECTION")
                self_verified = True
            recent = sorted([r for r in rows if r not in self_rows], key=_order, reverse=True)
            schedule_before = None
            if filename == daily_schedule_health.WORKFLOW:
                try:
                    approved, policy_proof = daily_schedule_health.read_policy(get, before["main"])
                    def schedule_page():
                        value = get(path + daily_schedule_health.SCHEDULE_QUERY)
                        items = [run_summary(r, binding, filename, wid)
                                 for r in listing(value, "workflow_runs", RUN_LIMIT)]
                        require(all(r["event"] == "schedule" for r in items), "HEALTH_SCHEDULE_EVENT")
                        return value, items
                    schedule_before = schedule_page()
                except StoreUnavailable:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    panel["schedule"] = {"status": "unknown", "error": str(error)
                        if isinstance(error, RecordError) else "HEALTH_SCHEDULE_NATIVE_SHAPE"}
            latest = recent[0] if recent else None
            successful = next((r for r in recent if r["conclusion"] == "success"), None)
            details = {}
            for run in (latest, successful):
                if run and run["id"] not in details:
                    details[run["id"]] = _detail(get, run, binding, filename, wid)
            final_workflow = get(path)
            final_runs = get(path + "/runs?branch=main&per_page=20")
            final_rows = [run_summary(r, binding, filename, wid) for r in listing(final_runs, "workflow_runs", RUN_LIMIT)]
            stable = (final_workflow.get("id") == wid and final_workflow.get("path") == panel["path"]
                      and final_workflow.get("state") == state and native["total_count"] == final_runs["total_count"]
                      and sorted(final_rows, key=_order) == sorted(rows, key=_order)
                      and all(d["status"] == "inspected" for d in details.values()))
            if schedule_before is not None:
                try:
                    schedule_after = schedule_page()
                    ordinary_before = {r["id"]: r for r in rows}
                    ordinary_after = {r["id"]: r for r in final_rows}
                    schedule_stable = (schedule_before[0]["total_count"] == schedule_after[0]["total_count"]
                        and sorted(schedule_before[1], key=_order) == sorted(schedule_after[1], key=_order)
                        and all(r == ordinary_before[r["id"]] for r in schedule_before[1] if r["id"] in ordinary_before)
                        and all(r == ordinary_after[r["id"]] for r in schedule_after[1] if r["id"] in ordinary_after)
                        and final_workflow.get("id") == wid and final_workflow.get("path") == panel["path"]
                        and final_workflow.get("state") == state)
                    schedule_observation = (panel, approved, policy_proof, schedule_before,
                                            schedule_stable, state)
                except StoreUnavailable:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    panel["schedule"] = {"status": "unknown", "error": str(error)
                        if isinstance(error, RecordError) else "HEALTH_SCHEDULE_NATIVE_SHAPE"}
            status = ("changed-during-inspection" if not stable else "disabled" if state != "active"
                      else "not-run" if latest is None else "in-progress" if latest["status"] != "completed"
                      else "metadata-ok" if latest["conclusion"] == "success" else "needs-attention")
            panel.update(workflow_id=wid, workflow_state=state, status=status, excluded_self=self_rows,
                         recent_scope={"limit": RUN_LIMIT, "returned": len(rows), "total": native["total_count"],
                                       "complete_history": native["total_count"] == len(rows)},
                         observed_active=[r for r in recent if r["status"] in ACTIVE],
                         latest=details.get(latest["id"]) if latest else None,
                         last_visible_success=details.get(successful["id"]) if successful else None)
        except StoreUnavailable:
            raise
        except (KeyError, TypeError, ValueError) as error:
            # Invalid native shapes stay local. Transport/permission failures propagate
            # and stop ALL subsequent reads through the existing API contract.
            panel.update(status="unknown", error=str(error) if isinstance(error, RecordError) else "HEALTH_NATIVE_SHAPE")
        panels.append(panel)
    backup = {"status": "not-supplied"}
    if backup_run_id is not None:
        try:
            require(store_workflow_id is not None, "HEALTH_BACKUP_WORKFLOW_UNKNOWN")
            r = run_summary(get(f"/actions/runs/{backup_run_id}"), binding, "store-runtime.yml", store_workflow_id)
            backup = _detail(get, r, binding, "store-runtime.yml", store_workflow_id)
            require(backup["observed_operation"] == "backup" and r["status"] == "completed"
                    and r["conclusion"] == "success", "HEALTH_NOT_SUCCESSFUL_BACKUP")
        except StoreUnavailable:
            raise
        except (KeyError, TypeError, ValueError) as error:
            backup = {"status": "unknown", "error": str(error) if isinstance(error, RecordError) else "HEALTH_NATIVE_SHAPE"}
    after = heads(); default_after = repository()  # Recheck access before any public output.
    evaluated = as_of or datetime.now(timezone.utc).isoformat()
    now = clock(evaluated)
    if schedule_observation is not None:
        panel, approved, policy_proof, (native_schedule, schedule_rows), stable_schedule, state = schedule_observation
        try:
            panel["schedule"] = daily_schedule_health.describe(approved, policy_proof, schedule_rows,
                total=native_schedule["total_count"], limit=RUN_LIMIT, as_of=evaluated,
                observed_from=observed_from, explicit_clock=as_of is not None,
                stable=stable_schedule and before["main"] == after["main"] and default_before == default_after,
                workflow_state=state, default_branch=default_before)
        except (KeyError, TypeError, ValueError) as error:
            panel["schedule"] = {"status": "unknown", "error": str(error)
                if isinstance(error, RecordError) else "HEALTH_SCHEDULE_NATIVE_SHAPE"}
    for panel in [*panels, {"latest": backup if "run" in backup else None, "status": "backup"}]:
        for name in ("latest", "last_visible_success"):
            detail = panel.get(name)
            if not detail:
                continue
            run = detail["run"]
            require(all(clock(run[k]) <= now for k in ("created_at", "run_started_at", "updated_at")), "HEALTH_FUTURE_RUN")
            detail["age_seconds"] = int((now - clock(run["run_started_at"])).total_seconds())
            a = detail["artifact"]
            detail["artifact_state"] = "not-found" if a is None else "expired" if a["expired"] or clock(a["expires_at"]) <= now else "expiring" if (clock(a["expires_at"]) - now).total_seconds() <= 7 * 86400 else "metadata-available"
            if a:
                require(clock(a["created_at"]) <= now, "HEALTH_FUTURE_ARTIFACT")
        if panel["status"] == "metadata-ok" and panel["latest"]["artifact_state"] != "metadata-available":
            panel["status"] = "needs-attention"
        if panel.get("schedule", {}).get("status") == "changed-during-inspection":
            panel["status"] = "changed-during-inspection"
        elif panel["status"] == "metadata-ok" and "schedule" in panel:
            if panel["schedule"]["status"] not in {"native-success", "no-recent-slot"}:
                panel["status"] = "needs-attention"
    if "run" in backup and backup["status"] == "inspected" and backup["artifact_state"] != "metadata-available":
        backup["status"] = "needs-attention"
    changed = before != after or expected_head is not None and before[binding.branch] != expected_head
    changed = changed or self_run is not None and not self_verified
    result = {"schema": "runtime-health-v1", "repository": binding.repository, "as_of": evaluated,
              "status": "needs-attention" if changed or any(p["status"] != "metadata-ok" for p in panels) or backup["status"] not in {"not-supplied", "inspected"} else "metadata-ok",
              "heads_before": before, "heads_after": after, "expected_records_head": expected_head,
              "heads_changed_or_unexpected": changed, "workflows": panels, "specified_backup": backup,
              "excluded_inspection": self_run if self_verified else None, "native_reads": len(evidence),
              "native_metadata_bytes": used, "evidence": evidence,
              "scope": "four-delivery-hosts; latest-attempt-within-recent-20-main-runs; independent-daily-schedule-page; not-global-writer-lock",
              "automatic_schedule": "not-established-by-workflow-active-state",
              "durable_backup": "not-established-by-actions-artifact",
              "source_freshness": "not-inspected", "business_acceptance": "not-inspected",
              "cutover_authorized": False, "dispatches": 0, "writes": 0, "notifications": 0}
    require(len(encoded(result)) <= 256 * 1024, "HEALTH_OUTPUT_BOUND")
    return result
