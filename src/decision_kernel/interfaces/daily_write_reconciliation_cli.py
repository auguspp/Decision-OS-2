"""Native proof for one reviewed daily write reconciliation, with no source I/O.

Only the original immutable run, archive and five commits are checked. The
historical cycle reader uses retained source effects and never depends on the
temporary Actions archive remaining available after this admission.
"""
from __future__ import annotations

from io import BytesIO
import stat
import zipfile

from decision_kernel.application.daily_write_reconciliation import proof
from decision_kernel.application.relay_registry import _json
from decision_kernel.domain.records import RecordError, digest, require
from decision_kernel.infrastructure.research_receipts import download_archive

REPOSITORY = "auguspp/Decision-OS-2"
REPOSITORY_ID = 1420041744
PREFIX = "/repos/" + REPOSITORY
WORKFLOW = ".github/workflows/daily-market.yml"
FILES = frozenset({"control.json", "result.json", "discovery-result.json",
    "reading-room.html", "reading-room.json", "reading-summary.md"})
MAX_ARCHIVE = 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024


def verify_original_archive(raw, grant):
    """Read only original JSON; no downloaded code or provider response runs."""
    grant = proof(grant)
    original = grant["native_run"]
    require(isinstance(raw, bytes) and len(raw) == original["size_bytes"] <= MAX_ARCHIVE
        and digest(raw) == original["sha256"], "DAILY_WRITE_RECONCILIATION_ARCHIVE_DIGEST")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            infos = archive.infolist()
            require(len(infos) == len(FILES) and {info.filename for info in infos} == FILES
                and all(not info.is_dir() and not info.flag_bits & 1
                    and not stat.S_ISLNK(info.external_attr >> 16)
                    and info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    for info in infos)
                and sum(info.file_size for info in infos) <= MAX_EXPANDED,
                "DAILY_WRITE_RECONCILIATION_ARCHIVE_MEMBERS")
            require(archive.testzip() is None, "DAILY_WRITE_RECONCILIATION_ARCHIVE_CRC")
            result, control = (_json(archive.read(name)) for name in ("result.json", "control.json"))
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
        raise RecordError("DAILY_WRITE_RECONCILIATION_ARCHIVE_INVALID") from None
    expected = {"schema": "daily-market-runtime-v1", "operation": "update",
        "invocation": "repair-calendar", "run_id": original["run_id"],
        "run_attempt": original["run_attempt"], "code_sha": original["code_sha"],
        "status": "persistence-unconfirmed", "reason_code": "REMOTE_WRITE_REQUIRES_RECONCILIATION",
        "source_requests": 1, "calendar_source_requests": 0, "price_source_requests": 1,
        "price_source_budget": 12, "calendar_source_budget": 3, "calendar_reserved_requests": 3,
        "cycle_date": "20261019", "cycle_status": "blocked", "expected_completed_session": "20261019"}
    require(isinstance(result, dict) and all(type(result.get(key)) is type(value)
        and result[key] == value for key, value in expected.items()),
        "DAILY_WRITE_RECONCILIATION_ORIGINAL_RESULT")
    require(isinstance(control, dict) and control.get("schema") == "daily-market-reading-control-v1"
        and control.get("cycle_reference") == grant["original_reference"]
        and control.get("market_reference") == grant["settled_market_reference"]
        and control.get("generation") == "daily-20261019"
        and control.get("frontier_blocking_reference") == grant["settled_market_reference"]
        and control.get("frontier_successor_references") == []
        and control.get("frontier_reason_code") == "FTSHARE_SINGLE_BASE_NOT_TERMINAL",
        "DAILY_WRITE_RECONCILIATION_ORIGINAL_CONTROL")
    return {"original_status": result["status"], "original_reason_code": result["reason_code"],
        "original_source_requests": 1}


def verify_native_reconciliation(api, grant, *, token=None, archive_loader=None):
    """Check original native identities before the only new control transition."""
    grant = proof(grant)
    original = grant["native_run"]
    run_id, attempt = original["run_id"], original["run_attempt"]
    run = api.request("GET", PREFIX + f"/actions/runs/{run_id}/attempts/{attempt}")
    require(isinstance(run, dict) and type(run.get("id")) is int and run["id"] == run_id
        and type(run.get("run_attempt")) is int and run["run_attempt"] == attempt
        and run.get("head_sha") == original["code_sha"] and run.get("head_branch") == "main"
        and run.get("path") == WORKFLOW and run.get("name") == "decision-os-daily-market"
        and run.get("event") == "workflow_dispatch" and run.get("status") == "completed"
        and run.get("conclusion") == "failure"
        and all(isinstance(run.get(key), dict) and type(run[key].get("id")) is int
            and run[key]["id"] == REPOSITORY_ID for key in ("repository", "head_repository")),
        "DAILY_WRITE_RECONCILIATION_NATIVE_RUN")
    meta = api.request("GET", PREFIX + "/actions/artifacts/" + str(original["artifact_id"]))
    require(isinstance(meta, dict) and type(meta.get("id")) is int
        and meta["id"] == original["artifact_id"] and meta.get("expired") is False
        and meta.get("name") == f"dos2-daily-market-{run_id}-{attempt}"
        and type(meta.get("size_in_bytes")) is int and meta["size_in_bytes"] == original["size_bytes"]
        and meta.get("digest") == "sha256:" + original["sha256"]
        and isinstance(meta.get("workflow_run"), dict)
        and all(meta["workflow_run"].get(key) == value for key, value in {
            "id": run_id, "repository_id": REPOSITORY_ID, "head_repository_id": REPOSITORY_ID,
            "head_sha": original["code_sha"], "head_branch": "main"}.items()),
        "DAILY_WRITE_RECONCILIATION_NATIVE_ARTIFACT")
    jobs = api.request("GET", PREFIX + f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    require(isinstance(jobs, dict) and type(jobs.get("total_count")) is int
        and jobs["total_count"] == 3 and isinstance(jobs.get("jobs"), list) and len(jobs["jobs"]) == 3
        and all(isinstance(job, dict) and type(job.get("run_id")) is int and job["run_id"] == run_id
            and type(job.get("run_attempt")) is int and job["run_attempt"] == attempt
            and job.get("head_sha") == original["code_sha"] and job.get("status") == "completed"
            and isinstance(job.get("name"), str)
            for job in jobs["jobs"]), "DAILY_WRITE_RECONCILIATION_NATIVE_JOBS")
    by_name = {job.get("name"): job for job in jobs["jobs"]}
    require(set(by_name) == {"visibility", "daily", "status"}
        and all(by_name[name].get("conclusion") == conclusion for name, conclusion in {
            "visibility": "success", "daily": "failure", "status": "skipped"}.items()),
        "DAILY_WRITE_RECONCILIATION_NATIVE_JOBS")
    steps = by_name["daily"].get("steps")
    required_steps = {
        "Advance at most two calendar requests and twelve FTShare whole-range requests": "failure",
        "Build daily reading from saved Brief, research and the actual market attempt": "success",
        "Recheck native visibility and publication authority before releasing exact references": "success",
        "Retain the authorized daily status and exact consumer input": "success"}
    require(isinstance(steps, list) and all(isinstance(step, dict) for step in steps),
        "DAILY_WRITE_RECONCILIATION_NATIVE_STEPS")
    for name, conclusion in required_steps.items():
        matches = [step for step in steps if step.get("name") == name]
        require(len(matches) == 1 and matches[0].get("status") == "completed"
            and matches[0].get("conclusion") == conclusion, "DAILY_WRITE_RECONCILIATION_NATIVE_STEPS")
    for item in grant["submissions"]:
        sha = item["reference"]["manifest"]["commit"]
        commit = api.request("GET", PREFIX + "/git/commits/" + sha)
        require(isinstance(commit, dict) and commit.get("sha") == sha
            and isinstance(commit.get("parents"), list) and len(commit["parents"]) == 1
            and isinstance(commit["parents"][0], dict)
            and commit["parents"][0].get("sha") == item["expected_head"],
            "DAILY_WRITE_RECONCILIATION_NATIVE_PARENT")
    loader = archive_loader or (lambda repo, artifact_id: download_archive(repo, artifact_id, token=token))
    checked = verify_original_archive(loader(REPOSITORY, original["artifact_id"]), grant)
    return {"schema": "daily-write-reconciliation-native-v1", "status": "verified",
        **original, **checked, "original_conclusion": "failure", "verified_submission_count": 5,
        "source_requests": 0, "records_write": "not-requested"}
