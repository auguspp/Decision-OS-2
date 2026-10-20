"""Finite, native-gated FTShare sync, adoption and status in the existing records Store."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from decision_kernel.application.ftshare_collection import (
    MAX_REQUESTS as MAX_SOURCE_REQUESTS, PLAN_SCHEMA, SINGLE_PLAN_SCHEMA,
    begin_ftshare_slot, finish_ftshare_slot, latest_ftshare_reference,
    plan_ftshare_collection, plan_ftshare_single_collection, restore_ftshare_collection,
    retain_ftshare_batch, retain_ftshare_collection, retain_ftshare_single,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.ftshare_daily import capture_batch
from decision_kernel.application.ftshare_single import capture_single, single_request
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection_runtime import health_receipt
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    RecordError, Selection, SourceRef, UnknownWrite, digest, encoded, identifier, require,
)
from decision_kernel.infrastructure.github_store import (
    MAX_REQUESTS, GitHubAPI, GitHubRateLimits, GitHubStore,
)
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.research_receipts import download_archive
from decision_kernel.interfaces.ftshare_check_cli import (
    BASE_REFERENCE, PREFIX, REPO, WORKFLOW, _regular, _safe_error, check_private,
)
from decision_kernel.interfaces.market_collection_cli import (
    BINDING_ENV, REGISTRY, _records_runtime_binding, _save_receipt,
)

CONFIG_FILE = Path("config/ftshare-daily.json")
SCHEMA = "ftshare-daily-runtime-v1"
SOURCE_ADMISSION_API_REQUESTS = 240
TOKEN_ENV = "DECISION_OS_STORE_TOKEN"
QUALIFICATION_TITLE = "FTShare daily check continuation 20261018"
KERNEL_TITLE = "FTShare Kernel v1 daily check 20261019"
ADOPTION_TITLE = "FTShare Kernel v1 adoption 20261019"
OPERATIONS = {"ftshare-sync", "ftshare-status", "ftshare-adopt-kernel"}


def configuration(value):
    fields = {"schema", "collection_id", "generation", "legacy_reference", "qualification", "kernel_adoption"}
    require(isinstance(value, dict) and fields <= set(value) <= fields | {"kernel_sync"}
        and value["schema"] == SCHEMA and value["collection_id"] == "ftshare-cn-eod"
        and value["legacy_reference"] == BASE_REFERENCE,
        "FTSHARE_RUNTIME_FIXED_CONFIG")
    for field in ("qualification", "kernel_adoption"):
        proof = value[field]
        require(proof is None or isinstance(proof, dict)
            and set(proof) == {"run_id", "artifact_id", "sha256", "code_sha"}
            and all(type(proof[k]) is int and 0 < proof[k] < 10 ** 15 for k in ("run_id", "artifact_id"))
            and isinstance(proof["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", proof["sha256"])
            and isinstance(proof["code_sha"], str) and re.fullmatch(r"[a-f0-9]{40}", proof["code_sha"]),
            "FTSHARE_RUNTIME_QUALIFICATION_REQUIRED")
    sync = value.get("kernel_sync")
    if sync is None:
        require(value["generation"] == "g20261016", "FTSHARE_RUNTIME_FIXED_CONFIG")
    else:
        sync_fields = {"base_reference", "start", "end", "targets", "source_budget"}
        require(isinstance(sync, dict) and sync_fields <= set(sync) <= sync_fields | {"completion_policy"},
            "FTSHARE_RUNTIME_KERNEL_SYNC_CONFIG")
        if "completion_policy" in sync:
            from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
            require(sync["completion_policy"] == EOD_COMPLETION_POLICY, "FTSHARE_COMPLETION_POLICY")
        reference = SourceRef.from_dict(sync["base_reference"])
        require(reference.selection == Selection() and reference.manifest.repository == REPO,
            "FTSHARE_RUNTIME_KERNEL_SYNC_BASE")
        identifier(value["generation"])
        require(value["generation"] != "g20261016" and value["kernel_adoption"] is not None,
            "FTSHARE_RUNTIME_KERNEL_SYNC_CONFIG")
        _single_requests(sync)
    return deepcopy(value)


def _single_requests(sync):
    """Canonical finite v1 requests; configuration supplies every source parameter."""
    require(all(isinstance(sync.get(k), str) and re.fullmatch(r"[0-9]{8}", sync[k])
        for k in ("start", "end")), "FTSHARE_RUNTIME_KERNEL_SYNC_DATES")
    try:
        zone = timezone(timedelta(hours=8))
        first = datetime.strptime(sync["start"], "%Y%m%d").replace(tzinfo=zone)
        final = datetime.strptime(sync["end"], "%Y%m%d").replace(tzinfo=zone) + timedelta(days=1)
    except (ValueError, OverflowError):
        require(False, "FTSHARE_RUNTIME_KERNEL_SYNC_DATES")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    since, until = ((first - epoch) // timedelta(milliseconds=1),
                    (final - epoch) // timedelta(milliseconds=1) - 1)
    targets = sync.get("targets")
    require(isinstance(targets, list) and 0 < len(targets) <= MAX_SOURCE_REQUESTS
        and type(sync.get("source_budget")) is int and sync["source_budget"] == len(targets),
        "FTSHARE_RUNTIME_KERNEL_SYNC_BUDGET")
    requests = {}
    for target in targets:
        require(isinstance(target, dict) and set(target) == {"symbol", "adjust_kind"},
            "FTSHARE_RUNTIME_KERNEL_SYNC_TARGETS")
        request = single_request(symbol=target["symbol"], since=since, until=until,
            adjust_kind=target["adjust_kind"])
        slot = target["adjust_kind"] + ":" + target["symbol"]
        require(slot not in requests, "FTSHARE_RUNTIME_KERNEL_SYNC_TARGETS")
        requests[slot] = request
    return requests


def settings(event, env, config):
    config = configuration(config)
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1", "FTSHARE_RUNTIME_MANUAL_MAIN")
    data = event.get("inputs") if isinstance(event, dict) else None
    require(isinstance(data, dict) and set(data) <= {"operation", "code_sha", "selection", "work_units",
        "repair_request", "access_and_custody_confirmed", "public_rights_confirmed"}, "FTSHARE_RUNTIME_INPUT")
    operation = data.get("operation")
    require(operation in OPERATIONS and not data.get("repair_request"),
        "FTSHARE_RUNTIME_OPERATION")
    require(operation != "ftshare-adopt-kernel" or config.get("kernel_sync") is None,
        "FTSHARE_RUNTIME_KERNEL_ADOPTION_GENERATION_CLOSED")
    authorized = (data.get("access_and_custody_confirmed") is True
        or data.get("access_and_custody_confirmed") == "true")
    require(operation == "ftshare-status" or authorized, "FTSHARE_RUNTIME_SOURCE_CUSTODY_REQUIRED")
    require(data.get("public_rights_confirmed") is False
        or data.get("public_rights_confirmed") == "false", "FTSHARE_RUNTIME_PRIVATE_ONLY")
    raw = data.get("selection")
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= 6000, "FTSHARE_RUNTIME_FIXED_SELECTION")
    selection = _json(raw.encode())
    require(isinstance(selection, dict) and {"collection_id", "generation"} <= set(selection)
        <= {"collection_id", "generation", "calendar_references"}
        and all(selection[k] == config[k] for k in ("collection_id", "generation")),
        "FTSHARE_RUNTIME_FIXED_SELECTION")
    calendars = None
    if "calendar_references" in selection:
        values = selection["calendar_references"]
        require(isinstance(values, dict) and set(values) == {"SSE", "SZSE"}, "FTSHARE_RUNTIME_CALENDAR_REFERENCES")
        calendars = {}
        for venue, value in values.items():
            ref = SourceRef.from_dict(value)
            require(ref.selection == Selection() and ref.manifest.repository == REPO,
                "FTSHARE_RUNTIME_CALENDAR_REFERENCES")
            calendars[venue] = _json(encoded(asdict(ref)))
    require(operation == "ftshare-sync" or calendars is None, "FTSHARE_RUNTIME_STATUS_CALENDAR_OVERRIDE")
    units = str(data.get("work_units", "1"))
    require(re.fullmatch(r"[1-9][0-9]?", units) and 1 <= int(units) <= 32
        and (operation == "ftshare-sync" or units == "1"), "FTSHARE_RUNTIME_WORK_UNIT_BOUND")
    sha, run_id = data.get("code_sha"), env.get("GITHUB_RUN_ID")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "FTSHARE_RUNTIME_CODE_SHA")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id),
        "FTSHARE_RUNTIME_RUN_ID")
    return {"operation": operation, "code_sha": sha, "run_id": int(run_id), "run_attempt": 1,
        "max_work_units": int(units), "access_confirmed": authorized, "custody_authorized": authorized,
        "calendar_references": calendars}


def native_ready(api, selected, config):
    """Authenticate private main execution and the operation's distinct evidence.

    Batch sync keeps its continuation-artifact gate. A configured single-route
    successor restores its qualification from the retained Store before creating
    a source client; it has no temporary Actions artifact dependency. Adoption
    retains separate authority for its four already acquired v1 responses.
    """
    require(selected.get("operation") in OPERATIONS, "FTSHARE_RUNTIME_OPERATION")
    adopting = selected["operation"] == "ftshare-adopt-kernel"
    single = config.get("kernel_sync") is not None
    require(not adopting or not single, "FTSHARE_RUNTIME_KERNEL_ADOPTION_GENERATION_CLOSED")
    check_private(api, selected)
    run = api.request("GET", PREFIX + "/actions/runs/" + str(selected["run_id"]))
    require(type(run.get("id")) is int and run["id"] == selected["run_id"]
        and run.get("head_sha") == selected["code_sha"] and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch" and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1 and run.get("path") == WORKFLOW
        and run.get("name") == (ADOPTION_TITLE if adopting else "decision-os-ashare-data")
        and (not (adopting or single)
            or run.get("display_title") == (ADOPTION_TITLE if adopting else "decision-os-ashare-data"))
        and run.get("status") == "in_progress"
        and run.get("repository", {}).get("id") == 1420041744
        and run.get("head_repository", {}).get("id") == 1420041744, "FTSHARE_RUNTIME_NATIVE_RUN")
    ci = api.request("GET", PREFIX + "/actions/workflows/ci.yml/runs?" + urlencode({
        "branch": "main", "event": "push", "head_sha": selected["code_sha"], "per_page": 5}))
    rows = ci.get("workflow_runs")
    require(isinstance(rows, list) and 0 < len(rows) <= 5, "FTSHARE_RUNTIME_MAIN_CI")
    latest = rows[0]
    if adopting or single:
        require(type(ci.get("total_count")) is int and ci["total_count"] == len(rows)
            and all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0 for row in rows),
            "FTSHARE_RUNTIME_MAIN_CI")
        latest = max(rows, key=lambda row: row["id"])
        require(type(latest.get("run_attempt")) is int and latest["run_attempt"] == 1
            and latest.get("name") == "decision-os-ci"
            and latest.get("repository", {}).get("id") == 1420041744
            and latest.get("head_repository", {}).get("id") == 1420041744, "FTSHARE_RUNTIME_MAIN_CI")
    require(latest.get("head_sha") == selected["code_sha"] and latest.get("head_branch") == "main"
        and latest.get("event") == "push" and latest.get("path") == ".github/workflows/ci.yml"
        and latest.get("status") == "completed" and latest.get("conclusion") == "success",
        "FTSHARE_RUNTIME_MAIN_CI")
    if selected["operation"] == "ftshare-status":
        return {"main_ci_run": latest["id"], "source_qualification": "not-required-for-status"}
    if adopting:
        return {"main_ci_run": latest["id"], "kernel_adoption": _kernel_native_proof(api, config)}
    if single:
        return {"main_ci_run": latest["id"], "source_qualification": "required-from-retained-kernel-base"}
    proof = config["qualification"]
    require(proof is not None, "FTSHARE_RUNTIME_QUALIFICATION_PENDING")
    study = api.request("GET", PREFIX + "/actions/runs/" + str(proof["run_id"]))
    require(type(study.get("id")) is int and study["id"] == proof["run_id"]
        and study.get("head_sha") == proof["code_sha"] and study.get("head_branch") == "main"
        and study.get("event") == "workflow_dispatch" and type(study.get("run_attempt")) is int
        and study["run_attempt"] == 1 and study.get("path") == WORKFLOW
        and study.get("display_title") == QUALIFICATION_TITLE and study.get("status") == "completed"
        and study.get("conclusion") == "success"
        and study.get("repository", {}).get("id") == 1420041744
        and study.get("head_repository", {}).get("id") == 1420041744, "FTSHARE_RUNTIME_STUDY_IDENTITY")
    artifact = api.request("GET", PREFIX + "/actions/artifacts/" + str(proof["artifact_id"]))
    producer = artifact.get("workflow_run", {})
    require(type(artifact.get("id")) is int and artifact["id"] == proof["artifact_id"]
        and artifact.get("name") == "dos2-ftshare-check-continuation-" + str(proof["run_id"]) + "-1"
        and artifact.get("expired") is False and artifact.get("digest") == "sha256:" + proof["sha256"]
        and producer.get("id") == proof["run_id"] and producer.get("head_sha") == proof["code_sha"]
        and producer.get("head_branch") == "main", "FTSHARE_RUNTIME_STUDY_ARTIFACT")
    return {"main_ci_run": latest["id"], "qualification_run": proof["run_id"],
        "qualification_artifact": proof["artifact_id"], "artifact_digest_verified": True}


def _kernel_native_proof(api, config):
    proof = config["kernel_adoption"]
    require(proof is not None, "FTSHARE_RUNTIME_KERNEL_ADOPTION_PENDING")
    run = api.request("GET", PREFIX + "/actions/runs/" + str(proof["run_id"]))
    require(type(run.get("id")) is int and run["id"] == proof["run_id"]
        and run.get("head_sha") == proof["code_sha"] and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch" and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1 and run.get("path") == WORKFLOW
        and run.get("name") == KERNEL_TITLE and run.get("display_title") == KERNEL_TITLE
        and run.get("status") == "completed" and run.get("conclusion") == "success"
        and run.get("repository", {}).get("id") == 1420041744
        and run.get("head_repository", {}).get("id") == 1420041744, "FTSHARE_RUNTIME_KERNEL_RUN")
    artifact = api.request("GET", PREFIX + "/actions/artifacts/" + str(proof["artifact_id"]))
    producer = artifact.get("workflow_run", {})
    require(type(artifact.get("id")) is int and artifact["id"] == proof["artifact_id"]
        and artifact.get("name") == "dos2-ftshare-check-kernel-" + str(proof["run_id"]) + "-1"
        and artifact.get("expired") is False and artifact.get("digest") == "sha256:" + proof["sha256"]
        and type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= 1024 * 1024
        and type(producer.get("id")) is int and producer["id"] == proof["run_id"]
        and producer.get("head_sha") == proof["code_sha"] and producer.get("head_branch") == "main"
        and producer.get("repository_id") == 1420041744 and producer.get("head_repository_id") == 1420041744,
        "FTSHARE_RUNTIME_KERNEL_ARTIFACT")
    return {**proof, "run_attempt": 1, "size_in_bytes": artifact["size_in_bytes"]}


def load_kernel_archive(api, config, *, archive_loader=download_archive):
    """Obtain the fixed original with the Store token; the application replays it."""
    proof = _kernel_native_proof(api, config)
    raw = archive_loader(REPO, proof["artifact_id"])
    require(type(raw) is bytes and len(raw) == proof["size_in_bytes"]
        and digest(raw) == proof["sha256"], "FTSHARE_RUNTIME_KERNEL_ARCHIVE_DIGEST")
    return {"archive": raw, "proof": proof}


def _open_remote(binding, token, selected, config):
    limits = GitHubRateLimits()
    def open_unit(write):
        require(type(write) is bool and write == (selected["operation"] != "ftshare-status"),
            "FTSHARE_RUNTIME_OPERATION_BINDING")
        require(not write or binding.write_enabled, "REMOTE_READ_ONLY")
        api = GitHubAPI(token)
        api.rate_limits = limits
        native_ready(api, selected, config)
        readonly = replace(binding, write_enabled=False)
        store = VersionedStore(GitHubStore(binding if write else readonly, api))
        return store, lambda: VersionedStore(GitMarketReader(readonly, api)), api
    return open_unit


def _summary(collection):
    units = collection["units"]
    counts = {status: sum(unit["status"] == status for unit in units.values())
              for status in ("pending", "in-flight", "complete", "failed")}
    result = {"source_budget": collection["plan"]["source_budget"],
        "reserved_requests": collection["reserved_requests"], "slot_counts": counts,
        "scope_count": len(collection["plan"]["symbols"]),
        "eligible_count": len(collection["plan"]["eligible_symbols"]),
        "quarantined_count": len(collection["plan"]["quarantined_symbols"]),
        "revision": collection["revision"], "source_hold": deepcopy(collection["source_hold"]),
        "source_hold_requires_external_review": collection["source_hold"] is not None,
        "next_run_can_resume": counts["pending"] > 0 and counts["in-flight"] == 0
            and collection["source_hold"] is None}
    if collection["plan"]["schema"] == SINGLE_PLAN_SCHEMA:
        result.update(source_route="kernel-v1-single", new_slot_counts=_work_slot_counts(collection),
            inherited_slot_count=len(collection["plan"]["inherited_units"]),
            prior_study_source_requests=collection["plan"]["adoption"]["cumulative_source_requests"],
            prior_generation_reserved_requests=collection["plan"]["prior_generation_reserved_requests"])
    return result


def _work_slot_counts(collection):
    plan = collection["plan"]
    slots = plan["new_slots"] if plan["schema"] == SINGLE_PLAN_SCHEMA else collection["units"]
    return {status: sum(collection["units"][slot]["status"] == status for slot in slots)
        for status in ("pending", "in-flight", "complete", "failed")}


def _check_sync_plan(collection, config, calendar_references, master_references=None):
    """A retained generation cannot silently acquire a different configured scope."""
    plan = collection["plan"]
    require(all(plan[key] == config[key] for key in ("collection_id", "generation", "legacy_reference")),
        "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED")
    require(calendar_references is None or plan["calendar_references"] == calendar_references,
        "FTSHARE_RUNTIME_RESUME_CALENDAR_CHANGED")
    require(master_references is None or plan["master_references"] == master_references,
        "FTSHARE_RUNTIME_RESUME_MASTER_CHANGED")
    sync = config.get("kernel_sync")
    if sync is None:
        require(plan["schema"] == PLAN_SCHEMA, "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED")
        return
    require(plan["schema"] == SINGLE_PLAN_SCHEMA and all(plan[key] == sync[key]
        for key in ("base_reference", "start", "end", "source_budget"))
        and plan.get("completion_policy") == sync.get("completion_policy"),
        "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED")
    requests = _single_requests(sync)
    require(len(plan["new_slots"]) == len(requests) and set(plan["new_slots"]) == set(requests)
        and all(plan["requests"][slot] == request for slot, request in requests.items()),
        "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED")
    # The core planner/restore has already independently replayed the permanent
    # original archive and four captures. Match that proven identity to main's
    # admission configuration before the caller can even construct a client.
    approved = config.get("kernel_adoption")
    require(isinstance(approved, dict) and set(approved) == {"run_id", "artifact_id", "sha256", "code_sha"}
        and all(plan["adoption"]["proof"][key] == value for key, value in approved.items()),
        "FTSHARE_RUNTIME_KERNEL_QUALIFICATION_CHANGED")


def _slot_order(collection, slot):
    parameters = collection["plan"]["requests"][slot]["parameters"]
    symbols = (parameters["symbol"],) if "symbol" in parameters else tuple(parameters["symbols"])
    return symbols, parameters["adjust_kind"] != "none"


def _quota(api):
    return api is None or (api._requests < SOURCE_ADMISSION_API_REQUESTS
        and (not isinstance(getattr(api, "rate_limits", None), GitHubRateLimits)
            or api.rate_limits.admits(MAX_REQUESTS - api._requests)))


def _transport(client, elapsed):
    meta = client.last_response_meta or client.last_error_meta or {}
    allowed = ("http_status", "received_at", "retry_after_state", "retry_after_seconds", "rate_limits",
               "wire_bytes", "wire_sha256", "classification", "phase", "exception_type")
    return {"elapsed_seconds": round(max(0, elapsed), 6),
            **{k: deepcopy(meta[k]) for k in allowed if k in meta}}


def _source_instruction(transport):
    """Use observed transport fields, never an arbitrary exception's wording.

    A hold requires external condition review; this host cannot clear it. A short
    valid wait is fulfilled before settling, including the last unit of a run.
    """
    status = transport.get("http_status")
    if type(status) is int and status in (401, 403, 429):
        return "FTSHARE_HTTP_" + str(status), None
    rates = transport.get("rate_limits", {})
    if isinstance(rates, dict):
        for name in ("key_remaining", "ip_remaining"):
            quota = rates.get(name, {})
            if (isinstance(quota, dict) and quota.get("state") == "valid"
                    and type(quota.get("remaining")) is int and quota["remaining"] == 0):
                return "FTSHARE_OBSERVED_QUOTA_EXHAUSTED", None
    state, seconds = transport.get("retry_after_state", "missing"), transport.get("retry_after_seconds")
    if state not in ("missing", "valid"):
        return "FTSHARE_WAIT_INSTRUCTION_UNUSABLE", None
    if state == "valid":
        if type(seconds) is not int or seconds < 0:
            return "FTSHARE_WAIT_INSTRUCTION_UNUSABLE", None
        if seconds > 60:
            return "FTSHARE_WAIT_EXCEEDS_CHECK_BOUND", None
        return None, max(2, seconds)
    return None, 2


def _run_kernel_adoption(*, config, registry, open_unit, kernel_loader,
                         guard, progress, clock, monotonic, capacity):
    """One bounded adoption of existing bytes; there is no source client here."""
    from decision_kernel.application.ftshare_adoption import read_kernel_archive
    from decision_kernel.application.ftshare_collection import adopt_ftshare_kernel
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    guard = guard or (lambda _api: None)
    started = monotonic()
    receipt = {"schema": "ftshare-collection-runtime-receipt-v1", "operation": "ftshare-adopt-kernel",
        "status": "started", "source_requests": 0, "source_budget": 0, "reserved_requests": 0,
        "work_units": [], "reason_code": None, "supplier_rows_in_receipt": False,
        "private_locations_in_receipt": False, "normal_read_required": True,
        "account_daily_limit_verified": False, "long_term_uptime_certified": False,
        "full_market_coverage_certified": False, "factor_qualification_granted": False,
        "started_at": clock()}
    reference = reading_request = api = unit = None
    def report():
        if progress is not None:
            progress(deepcopy(receipt))
    report()
    try:
        require(callable(kernel_loader), "FTSHARE_RUNTIME_KERNEL_ARCHIVE_READER_REQUIRED")
        store, fresh, api = open_unit(True)
        unit = {"number": 1, "source_requests": 0, "status": "admission"}
        receipt["work_units"].append(unit)
        guard(api)
        original = kernel_loader(api)
        read_kernel_archive(original["archive"], original["proof"])
        receipt.update(historical_source_requests=4, prior_source_requests=2, cumulative_source_requests=6)
        with market_read_scope(fresh()) as reader:
            reference = latest_ftshare_reference(reader, registry,
                collection_id=config["collection_id"], generation=config["generation"])
        if reference is None:
            receipt["capacity"] = capacity(fresh())
            require(receipt["capacity"]["admit_work_unit"], "FTSHARE_STORAGE_CAPACITY")
        require(_quota(api), "FTSHARE_STORE_ADMISSION_BUDGET")
        guard(api)
        require(_quota(api), "FTSHARE_STORE_ADMISSION_BUDGET")
        unit["status"] = "retaining"
        report()
        saved = adopt_ftshare_kernel(original["archive"], original["proof"],
            config["legacy_reference"], registry, store, collection_id=config["collection_id"],
            generation=config["generation"], created_at=clock(), fresh_reader=fresh,
            custody_authorized=True, public_rights_confirmed=False)
        require(saved["source_requests"] == saved["source_budget"] == saved["reserved_requests"] == 0
            and saved["historical_source_requests"] == 4 and saved["cumulative_source_requests"] == 6
            and len(saved["capture_references"]) == 4 and len(saved["collection"]["units"]) == 3,
            "FTSHARE_RUNTIME_KERNEL_ADOPTION_RESULT")
        reference, reading_request = saved["reference"], saved["reading_request"]
        unit["status"] = "complete"
        receipt.update(status="complete", retained_response_count=4, unique_series_count=3,
            revision=saved["collection"]["revision"])
    except Exception as exc:
        uncertain = isinstance(exc, UnknownWrite)
        from decision_kernel.infrastructure.github_store import write_failure_diagnostic
        diagnosis = write_failure_diagnostic(exc)
        if diagnosis is not None:
            receipt["write_last_failure"] = diagnosis
        receipt.update(status="persistence-unconfirmed" if uncertain else "blocked", reason_code=_safe_error(exc))
        if uncertain:
            receipt["next_run_can_resume"] = False
        if unit is not None:
            unit["status"] = receipt["status"]
    finally:
        if unit is not None:
            unit["store_api_requests"] = getattr(api, "_requests", None)
        limits = getattr(api, "rate_limits", None)
        if isinstance(limits, GitHubRateLimits):
            receipt["store_rate_limits"] = limits.receipt()
        receipt.update(finished_at=clock(), elapsed_seconds=round(max(0, monotonic() - started), 6))
        report()
    return {"receipt": receipt, "reference": reference, "reading_request": reading_request}


def run_collection(*, operation, config, registry, open_unit, max_work_units=1,
                   access_confirmed=False, custody_authorized=False, client_factory=None,
                   guard=None, progress=None, clock=None, monotonic=time.monotonic,
                   sleep=time.sleep, capacity=market_capacity, calendar_references=None, master_references=None,
                   kernel_loader=None,
                   quota_wait=None):
    """Finite source sync, source-free adoption, or independently restored status."""
    require(operation in OPERATIONS, "FTSHARE_RUNTIME_OPERATION")
    writing = operation != "ftshare-status"
    require(type(max_work_units) is int and 1 <= max_work_units <= 32
        and (operation == "ftshare-sync" or max_work_units == 1), "FTSHARE_RUNTIME_WORK_UNIT_BOUND")
    require(not writing or access_confirmed is True and custody_authorized is True,
        "FTSHARE_RUNTIME_SOURCE_CUSTODY_REQUIRED")
    single = config.get("kernel_sync") is not None
    require(master_references is None or operation == "ftshare-sync" and single,
            "FTSHARE_RUNTIME_MASTER_OVERRIDE_REQUIRES_SINGLE_SYNC")
    if operation == "ftshare-adopt-kernel":
        require(not single, "FTSHARE_RUNTIME_KERNEL_ADOPTION_GENERATION_CLOSED")
        require(calendar_references is None, "FTSHARE_RUNTIME_STATUS_CALENDAR_OVERRIDE")
        return _run_kernel_adoption(config=config, registry=registry, open_unit=open_unit,
            kernel_loader=kernel_loader, guard=guard, progress=progress, clock=clock,
            monotonic=monotonic, capacity=capacity)
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    guard = guard or (lambda _api: None)
    receipt = {"schema": "ftshare-collection-runtime-receipt-v1", "operation": operation,
        "status": "started", "source_requests": 0, "work_units": [], "reason_code": None,
        "supplier_rows_in_receipt": False, "private_locations_in_receipt": False,
        "account_daily_limit_verified": False, "long_term_uptime_certified": False,
        "normal_read_required": writing, "started_at": clock()}
    collection = reference = client = previous_limits = None
    started = monotonic()
    def report():
        if collection is not None:
            receipt.update(_summary(collection))
            receipt["reserved_requests_basis"] = "last-independently-restored-control"
        if receipt["status"] == "persistence-unconfirmed":
            receipt["next_run_can_resume"] = False
        if progress is not None:
            progress(deepcopy(receipt))
    def retain(candidate, store, fresh):
        return retain_ftshare_collection(candidate, registry, store, fresh_reader=fresh,
            predecessor=reference, custody_authorized=True, public_rights_confirmed=False)
    report()
    for number in range(1, max_work_units + 1):
        if previous_limits is not None and not previous_limits.admits(MAX_REQUESTS):
            admitted = False
            if quota_wait is not None:
                try:
                    admitted = quota_wait(api) is True
                finally:
                    # The callback uses the preceding unit's API. Its native
                    # read probes remain part of that original bounded counter,
                    # including when permission or quota refresh fails.
                    unit["store_api_requests"] = getattr(api, "_requests", None)
                    receipt["store_rate_limits"] = previous_limits.receipt()
                    report()
            if not admitted or not previous_limits.admits(MAX_REQUESTS):
                receipt.update(status="paused", reason_code="FTSHARE_STORE_RATE_LIMIT")
                break
        try:
            store, fresh, api = open_unit(writing)
        except Exception as exc:
            receipt.update(status="blocked", reason_code=_safe_error(exc))
            break
        unit = {"number": number, "source_requests": 0, "status": "admission"}
        receipt["work_units"].append(unit)
        try:
            if collection is None:
                with market_read_scope(fresh()) as reader:
                    reference = latest_ftshare_reference(reader, registry,
                        collection_id=config["collection_id"], generation=config["generation"])
                    if not writing:
                        if reference is None:
                            receipt.update(status="not-retained", reason_code="FTSHARE_COLLECTION_NOT_RETAINED")
                        else:
                            reading = read_ftshare_collection_version(reference, registry, reader, read_as_of=clock())
                            held = reading["latest_attempt"]["source_hold"]
                            receipt.update(status="read", health=health_receipt(reading),
                                reserved_requests=reading["latest_attempt"]["reserved_requests"],
                                source_hold=deepcopy(held), source_hold_requires_external_review=held is not None)
                            if held is not None:
                                receipt["next_run_can_resume"] = False
                        unit["status"] = receipt["status"]
                        break
                    if reference is not None:
                        collection = restore_ftshare_collection(reference, registry, reader)["collection"]
                    elif single:
                        sync = config["kernel_sync"]
                        collection = plan_ftshare_single_collection(sync["base_reference"], registry, reader,
                            collection_id=config["collection_id"], generation=config["generation"], created_at=clock(),
                            start=sync["start"], end=sync["end"], targets=sync["targets"],
                            source_budget=sync["source_budget"], calendar_references=calendar_references,
                            master_references=master_references,
                            completion_policy=sync.get("completion_policy"))
                    else:
                        collection = plan_ftshare_collection(config["legacy_reference"], registry, reader,
                            collection_id=config["collection_id"], generation=config["generation"], created_at=clock(),
                            calendar_references=calendar_references)
                    _check_sync_plan(collection, config, calendar_references, master_references)
            summary = _summary(collection)
            if summary["source_hold"] is not None:
                receipt.update(status="source-held", reason_code=summary["source_hold"]["reason_code"])
                break
            if summary["slot_counts"]["in-flight"]:
                receipt.update(status="blocked", reason_code="FTSHARE_IN_FLIGHT_REQUIRES_RECONCILIATION")
                break
            pending = [slot for slot, state in collection["units"].items() if state["status"] == "pending"]
            if not pending:
                failed = _work_slot_counts(collection)["failed"]
                receipt.update(status="already-complete" if not failed else "exhausted",
                    reason_code=None if not failed else "FTSHARE_FAILED_SLOTS_REMAIN")
                break
            receipt["capacity"] = capacity(fresh())
            require(receipt["capacity"]["admit_work_unit"], "FTSHARE_STORAGE_CAPACITY")
            require(_quota(api), "FTSHARE_STORE_ADMISSION_BUDGET")
            if client is None:
                if client_factory is None:
                    from decision_kernel.infrastructure.ftshare_http import FTShareHTTP
                    client_factory = FTShareHTTP
                client = client_factory()
                client.validate_credential()
                require(type(client.requests) is int and client.requests == 0, "FTSHARE_FRESH_CLIENT_REQUIRED")
            guard(api)
            require(_quota(api), "FTSHARE_STORE_ADMISSION_BUDGET")
            if reference is None:
                saved = retain(collection, store, fresh)
                reference, collection = saved["reference"], saved["restored"]["collection"]
            slot = min(pending, key=lambda candidate: _slot_order(collection, candidate))
            unit["slot"] = slot
            candidate = begin_ftshare_slot(collection, slot, at=clock())
            unit["status"] = "reserving"
            report()
            saved = retain(candidate, store, fresh)
            reference, collection = saved["reference"], saved["restored"]["collection"]
            unit["status"] = "in-flight"
            report()
            guard(api)
            require(_quota(api), "FTSHARE_STORE_ADMISSION_BUDGET")
            spec = collection["plan"]["requests"][slot]["parameters"]
            before, request_started, failure, captured = client.requests, monotonic(), None, None
            try:
                if single:
                    client.candlesticks(spec["symbol"], since=spec["since"], until=spec["until"],
                        adjust_kind=spec["adjust_kind"])
                else:
                    client.candlesticks_batch(spec["symbols"], since=spec["since"], until=spec["until"],
                        adjust_kind=spec["adjust_kind"])
                require(isinstance(client.last_response_bytes, bytes), "FTSHARE_ORIGINAL_BODY_REQUIRED")
                if single:
                    captured = capture_single(client.last_response_bytes, symbol=spec["symbol"],
                        since=spec["since"], until=spec["until"], adjust_kind=spec["adjust_kind"], acquired_at=clock())
                else:
                    captured = capture_batch(client.last_response_bytes, symbols=spec["symbols"],
                        since=spec["since"], until=spec["until"], adjust_kind=spec["adjust_kind"], acquired_at=clock())
            except Exception as exc:
                failure = _safe_error(exc)
            finally:
                count = client.requests - before
                require(type(count) is int and 0 <= count <= 1, "FTSHARE_ONE_CALL_PER_SLOT")
                unit.update(source_requests=count, transport=_transport(client, monotonic() - request_started))
                receipt["source_requests"] += count
                report()
            guard(api)
            require(count == 1, "FTSHARE_REQUEST_NOT_CONFIRMED")
            source_hold, delay = _source_instruction(unit["transport"])
            raw_reference = None
            if failure is None:
                retain_capture = retain_ftshare_single if single else retain_ftshare_batch
                raw = retain_capture(captured, store, fresh_reader=fresh,
                    custody_authorized=True, public_rights_confirmed=False)
                raw_reference = raw["reference"]
            if source_hold is None:
                try:
                    sleep(delay)
                except Exception:
                    source_hold = "FTSHARE_WAIT_INSTRUCTION_UNUSABLE"
            guard(api)
            candidate = finish_ftshare_slot(collection, slot, at=clock(), reference=raw_reference,
                reason_code=failure, source_hold=source_hold)
            unit["status"] = "settling"
            saved = retain(candidate, store, fresh)
            reference, collection = saved["reference"], saved["restored"]["collection"]
            unit["status"] = "complete" if failure is None else "failed"
            if failure is not None:
                receipt.update(status="failed", reason_code=failure)
                break
            if source_hold is not None:
                receipt.update(status="source-held", reason_code=source_hold)
                break
            remaining = _work_slot_counts(collection)
            receipt["status"] = "partial" if remaining["pending"] else "exhausted" if remaining["failed"] else "complete"
            if receipt["status"] in {"complete", "exhausted"}:
                break
        except Exception as exc:
            uncertain = isinstance(exc, UnknownWrite) or unit["status"] in {"reserving", "in-flight", "settling"}
            from decision_kernel.infrastructure.github_store import write_failure_diagnostic
            diagnosis = write_failure_diagnostic(exc)
            if diagnosis is not None:
                receipt["write_last_failure"] = diagnosis
                unit["write_last_failure"] = diagnosis
            receipt.update(status="persistence-unconfirmed" if uncertain else "blocked",
                reason_code=_safe_error(exc))
            unit["status"] = receipt["status"]
            break
        finally:
            unit["store_api_requests"] = getattr(api, "_requests", None)
            previous_limits = getattr(api, "rate_limits", None)
            if isinstance(previous_limits, GitHubRateLimits):
                receipt["store_rate_limits"] = previous_limits.receipt()
            else:
                previous_limits = None
            report()
    receipt.update(finished_at=clock(), elapsed_seconds=round(max(0, monotonic() - started), 6))
    report()
    return {"receipt": receipt, "reference": reference}


def run_runtime(event, env, out, *, execute=run_collection):
    config = configuration(_json(_regular(CONFIG_FILE, 65536)))
    selected = settings(event, env, config)
    root = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    out = Path(out)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
        and out.resolve() != root, "FTSHARE_RUNTIME_OUTPUT")
    out.mkdir(parents=False, exist_ok=False)
    receipt_path = out / "result.json"
    context = {k: selected[k] for k in ("operation", "code_sha", "run_id", "run_attempt")}
    _save_receipt(receipt_path, {**context, "status": "configuration-validated", "source_requests": 0})
    try:
        require(not env.get(BINDING_ENV), "FTSHARE_RUNTIME_BINDING_OVERRIDE_FORBIDDEN")
        require(bool(env.get(TOKEN_ENV)), "FTSHARE_STORE_TOKEN_REQUIRED")
        binding = _records_runtime_binding("status" if selected["operation"] == "ftshare-status" else "sync")
        client_factory = None
        if selected["operation"] == "ftshare-sync":
            from decision_kernel.infrastructure.ftshare_http import FTShareHTTP
            client_factory = lambda: FTShareHTTP(credential=env.get("FTSHARE_API_KEY", ""))
        kernel_loader = None
        if selected["operation"] == "ftshare-adopt-kernel":
            kernel_loader = lambda api: load_kernel_archive(api, config,
                archive_loader=lambda repo, artifact_id: download_archive(repo, artifact_id, token=env[TOKEN_ENV]))
        result = execute(operation=selected["operation"], config=config, registry=RelayRegistry(REGISTRY),
            open_unit=_open_remote(binding, env[TOKEN_ENV], selected, config),
            max_work_units=selected["max_work_units"], access_confirmed=selected["access_confirmed"],
            custody_authorized=selected["custody_authorized"],
            calendar_references=selected["calendar_references"],
            client_factory=client_factory, kernel_loader=kernel_loader,
            guard=lambda api: check_private(api, selected),
            progress=lambda value: _save_receipt(receipt_path, {**context, **value}))
        _save_receipt(receipt_path, {**context, **result["receipt"]})
        if result["reference"] is not None:
            # Only the private control artifact carries the exact source locator.
            _save_receipt(out / "control.json", {"schema": "ftshare-reading-control-v1",
                "reference": result["reference"], "reading_request": result.get("reading_request") or {
                    "reference": result["reference"], "offset": 0, "limit": 50}})
        return 0 if result["receipt"]["status"] in {"complete", "already-complete", "read"} else 2
    except Exception as exc:
        saved = _json(_regular(receipt_path, 262144))
        _save_receipt(receipt_path, {**saved, "status": "blocked", "reason_code": _safe_error(exc)})
        return 2
    finally:
        print(_regular(receipt_path, 262144).decode(), end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("runtime",))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return run_runtime(event, dict(os.environ), args.out)
    except (RecordError, ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        print(json.dumps({"error": _safe_error(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
