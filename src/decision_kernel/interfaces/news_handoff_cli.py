"""One completed NewsNow capture through the existing store and exact News Desk.

This host neither collects sources nor schedules work. A second invocation is
read-only reconciliation of the first immutable intent, never a write retry.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode

from decision_kernel.application import news_handoff as handoff
from decision_kernel.application.news_delivery import build_edition, page_model
from decision_kernel.application.news_rolling import clock
from decision_kernel.application.news_rolling_index import (
    index_source, open_index, read_index, rolling_index,
)
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    SHA, RecordError, UnknownWrite, digest, encoded, require,
)
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.infrastructure.research_receipts import download_archive
from decision_kernel.infrastructure.runtime_publication import publication_basis
from decision_kernel.interfaces.news_custody import source_from_archive, verify_native
from decision_kernel.interfaces.news_page import render_page
from decision_kernel.interfaces.news_rolling_cli import _SourceReads, _file
from decision_kernel.interfaces.research_payload import json_object, pack_payload
from decision_kernel.interfaces.research_runtime import run_command
from decision_kernel.observations.news_windows import verify_reading

REPOSITORY = "auguspp/Decision-OS-2"
REPOSITORY_ID = 1420041744
WORKFLOW = ".github/workflows/news-handoff.yml"
PREFIX = "/repos/" + REPOSITORY
NEWSNOW = {"cls", "wallstreetcn", "fastbull", "jin10", "mktnews", "gelonghui", "thepaper"}
SCOPES = ("sources", "brief", "reading")


def settings(event, env):
    require(isinstance(event, dict) and isinstance(env, dict), "NEWS_HANDOFF_HOST_INPUT")
    require(env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
            and env.get("GITHUB_RUN_ATTEMPT") == "1", "NEWS_HANDOFF_MANUAL_MAIN_REQUIRED")
    inputs = event.get("inputs")
    require(isinstance(inputs, dict) and set(inputs) <= {"operation", "code_sha", "plan", "expected_head"}
            and {"operation", "code_sha", "plan"} <= set(inputs), "NEWS_HANDOFF_INPUT_FIELDS")
    operation = inputs["operation"]
    require(operation in ("deliver", "reconcile"), "NEWS_HANDOFF_OPERATION")
    sha, run_id = inputs["code_sha"], env.get("GITHUB_RUN_ID")
    require(isinstance(sha, str) and SHA.fullmatch(sha)
            and sha == env.get("GITHUB_SHA"), "NEWS_HANDOFF_CODE_CHANGED")
    handoff.keys(run_id)
    require(env.get("DOS2_NEWS_HANDOFF_MODE") == ("write" if operation == "deliver" else "read"),
            "NEWS_HANDOFF_JOB_PERMISSION")
    text = inputs["plan"]
    require(isinstance(text, str) and len(text.encode("utf-8")) <= 8192, "NEWS_HANDOFF_PLAN_BOUND")
    plan = json_object(text.encode("utf-8"))
    expected = inputs.get("expected_head", "")
    if operation == "deliver":
        plan = handoff.plan_input(plan)
        require(isinstance(expected, str) and SHA.fullmatch(expected), "NEWS_HANDOFF_EXPECTED_HEAD")
        require(clock(plan["as_of"]) <= datetime.now(timezone.utc), "NEWS_HANDOFF_FUTURE_CUTOFF")
    else:
        require(set(plan) == {"source_run"} and not expected, "NEWS_HANDOFF_RECONCILE_READ_ONLY")
        handoff.keys(plan["source_run"])
    return {"operation": operation, "code_sha": sha, "run_id": run_id,
            "plan": plan, "expected_head": expected}


def native_ready(api, selected):
    """Validate this run and the latest exact main CI; no last-green fallback."""
    from decision_kernel.infrastructure.runtime_publication import require_code_on_main
    # A merge behind this run does not change the code it is executing; only a
    # rewrite that removed its commit from main invalidates the run.
    require_code_on_main(api, selected["code_sha"], "NEWS_HANDOFF_MAIN_MOVED")
    run = api.request("GET", PREFIX + "/actions/runs/" + selected["run_id"])
    require(isinstance(run, dict) and type(run.get("id")) is int
            and run["id"] == int(selected["run_id"])
            and run.get("head_sha") == selected["code_sha"] and run.get("head_branch") == "main"
            and run.get("event") == "workflow_dispatch" and run.get("run_attempt") == 1
            and type(run.get("run_attempt")) is int and run.get("status") == "in_progress"
            and run.get("path") == WORKFLOW and run.get("name") == "decision-os-news-handoff"
            and run.get("repository", {}).get("id") == REPOSITORY_ID
            and run.get("head_repository", {}).get("id") == REPOSITORY_ID, "NEWS_HANDOFF_NATIVE_RUN")
    page = api.request("GET", PREFIX + "/actions/workflows/ci.yml/runs?" + urlencode({
        "branch": "main", "event": "push", "head_sha": selected["code_sha"], "per_page": 5}))
    rows = page.get("workflow_runs") if isinstance(page, dict) else None
    require(isinstance(rows, list) and 0 < len(rows) <= 5
            and type(page.get("total_count")) is int and page["total_count"] == len(rows)
            and all(isinstance(row, dict) and type(row.get("id")) is int for row in rows),
            "NEWS_HANDOFF_MAIN_CI")
    latest = max(rows, key=lambda row: row["id"])
    require(latest.get("head_sha") == selected["code_sha"] and latest.get("head_branch") == "main"
            and latest.get("event") == "push" and latest.get("path") == ".github/workflows/ci.yml"
            and latest.get("name") == "decision-os-ci" and latest.get("status") == "completed"
            and latest.get("conclusion") == "success" and type(latest.get("run_attempt")) is int
            and latest["run_attempt"] == 1
            and latest.get("repository", {}).get("id") == REPOSITORY_ID
            and latest.get("head_repository", {}).get("id") == REPOSITORY_ID, "NEWS_HANDOFF_MAIN_CI")
    return {"main_ci_run": latest["id"], "native_run_created_at": run.get("created_at")}


def _authority(store):
    require(store.backend.live_visibility() == "public", "NEWS_HANDOFF_PUBLIC_DEPLOYMENT_REQUIRED")
    basis = {scope: publication_basis(store.backend.binding, scope) for scope in SCOPES}
    require(all(basis.values()), "NEWS_HANDOFF_PUBLICATION_NOT_CONFIRMED")
    store.backend.head()
    return basis


def _same_source(result, source):
    require(result.get("data") == source.text
            and encoded({**result["manifest"]["input"], "text": result["data"]}) == encoded(asdict(source)),
            "NEWS_HANDOFF_INDEPENDENT_READ_MISMATCH")


def _newsnow_only(source):
    value = verify_reading(json_object(source.text.encode("utf-8")))
    outcomes = value["source_outcomes"]
    require(bool(outcomes) and all(isinstance(row, dict) and row.get("source") in NEWSNOW
            and isinstance(row.get("capture"), dict)
            and row["capture"].get("schema") == "news-capture-receipt-v1"
            and row["capture"].get("request") == {"family": "newsnow", "source": row["source"]}
            for row in outcomes), "NEWS_HANDOFF_NEWSNOW_ONLY")
    sources = {row["source"] for row in outcomes}
    require(len(sources) == len(outcomes) and all(row["provider"] == "newsnow"
            and row["source_id"] in sources for row in value["observations"]),
            "NEWS_HANDOFF_NEWSNOW_ONLY")


def execute(selected, binding, api, output, *, archive_loader=download_archive):
    """One API budget, original writers, and fresh independently validated readers."""
    require(binding.repository == REPOSITORY and binding.repository_id == REPOSITORY_ID
            and binding.branch == "records" and binding.visibility == "public"
            and not binding.write_enabled, "NEWS_HANDOFF_BINDING")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"schema": "dos2-news-handoff-operation-v1", "operation": selected["operation"],
        "code_sha": selected["code_sha"], "run_id": selected["run_id"], "run_attempt": 1,
        "source_run": selected["plan"]["source_run"], "status": "started", "stages": {},
        "source_http_requests": 0, "archive_requests": 0, "archive_downloads": 0, "confirmed_writes": 0,
        "research_executed": False, "notification_sent": False,
        "continuous_capture_verified": False, "complete_news_coverage": False,
        "human_acceptance": "not-confirmed"}
    first_authority = None

    def persist():
        report["storage_api_requests"] = getattr(api, "_requests", None)
        (output / "handoff-report.json").write_bytes(encoded(report))

    def fresh():
        return VersionedStore(GitHubStore(binding, api))

    def gate():
        proof = native_ready(api, selected)
        authority = _authority(fresh())
        require(first_authority is None or authority == first_authority,
                "NEWS_HANDOFF_PUBLICATION_AUTHORITY_CHANGED")
        return proof, authority

    def command(stage, operation, payload, submission="", expected=""):
        # Never issue another business operation after a failed/unknown command.
        if operation == "put-source":
            gate()
        inputs = {"operation": operation, "code_sha": selected["code_sha"],
            "submission_id": submission, "expected_head": expected,
            "payload": pack_payload(encoded(payload))}
        report["active_stage"] = stage
        persist()
        original = run_command(binding, api=api, inputs=inputs, code_sha=selected["code_sha"],
            output=output / stage, run_id=selected["run_id"], attempt="1")
        require(original["status"] == "completed", "NEWS_HANDOFF_STAGE_NOT_CONFIRMED")
        report["stages"][stage] = {"operation": operation, "write": original["write"],
            "reference": original.get("result", {}).get("reference"),
            "payload_sha256": original["payload_sha256"],
            "expected_head": expected}
        report["confirmed_writes"] += int(original["write"] == "published")
        persist()
        return original["result"]

    def read(stage, ref, source):
        value = command(stage, "read-source", ref)
        _same_source(value, source)
        return value

    def incomplete(stage, reason):
        report.update(status="reconciliation-incomplete", missing_stage=stage, reason_code=reason,
            continuation="original-stage-evidence-required-no-automatic-write-or-recapture")
        persist()
        return 2

    try:
        proof, first_authority = gate()
        report.update(native=proof, publication_authorization=first_authority)
        names = handoff.keys(report["source_run"])
        retained = handoff.retained_source(fresh(), names["intent"], names["intent"])
        if selected["operation"] == "deliver":
            require(retained is None, "NEWS_HANDOFF_ALREADY_STARTED_USE_RECONCILE")
            plan = selected["plan"]
            before = fresh().backend.head()
            require(before == selected["expected_head"], "NEWS_HANDOFF_HEAD_CHANGED")
            run = api.request("GET", PREFIX + "/actions/runs/" + plan["source_run"])
            artifact = api.request("GET", PREFIX + "/actions/artifacts/" + plan["artifact_id"])
            selection = {key: plan[key] for key in ("source_run", "artifact_id", "artifact_sha256")}
            verify_native(run, artifact, selection)
            # Existing isolated gh transport: one fixed native ZIP, no source URL,
            # no retry. Metadata and every Store stage keep the same API object.
            report["archive_requests"] = 1
            persist()
            raw = archive_loader(REPOSITORY, int(plan["artifact_id"]))
            report["archive_downloads"] = 1
            source = source_from_archive(raw, run, artifact, selection)
            _newsnow_only(source)
            prior = handoff.retained_source(fresh(), names["batch"], names["batch"])
            if prior is not None:
                require(encoded(asdict(prior["source"])) == encoded(asdict(source)),
                        "NEWS_HANDOFF_PRIOR_BATCH_CONFLICT")
            intent = handoff.make_intent(plan, source, run["head_sha"], selected["expected_head"],
                prior_batch_reference=None if prior is None else prior["reference"],
                handoff_code_sha=selected["code_sha"], handoff_run_id=selected["run_id"])
            intent_input = handoff.intent_source(intent)
            # Native metadata and exact transport original remain reviewable.
            for name, value in (("source-run.json", run), ("source-artifact.json", artifact),
                                ("plan.json", plan)):
                (output / name).write_bytes(encoded(value))
            (output / "source.zip").write_bytes(raw)
            result = command("intent-write", "put-source", asdict(intent_input),
                names["intent"], selected["expected_head"])
            intent_ref = result["reference"]
            read("intent-read", intent_ref, intent_input)
        else:
            if retained is None:
                return incomplete("intent", "NEWS_HANDOFF_INTENT_NOT_FOUND")
            intent_input, intent_ref = retained["source"], retained["reference"]
            intent = handoff.validate_intent(json_object(intent_input.text.encode("utf-8")))
            require(intent["source_run"] == report["source_run"]
                    and encoded(asdict(intent_input)) == encoded(asdict(handoff.intent_source(intent))),
                    "NEWS_HANDOFF_INTENT_SOURCE_MISMATCH")
            plan = {key: intent[key] for key in ("source_run", "artifact_id", "artifact_sha256",
                                                "as_of", "predecessor")}
            (output / "plan.json").write_bytes(encoded(plan))
            command("intent-reconcile", "reconcile-source", asdict(intent_input),
                names["intent"], intent["expected_head"])
            read("intent-read", intent_ref, intent_input)
            prior = handoff.retained_source(fresh(), names["batch"], names["batch"])
            if prior is None:
                return incomplete("batch", "NEWS_HANDOFF_BATCH_NOT_FOUND")
            source = prior["source"]
            _newsnow_only(source)
            require(digest(encoded(asdict(source))) == intent["batch_source_sha256"],
                    "NEWS_HANDOFF_BATCH_CONTENT_CHANGED")
        report.update(intent_reference=intent_ref, original_handoff_code_sha=intent["handoff_code_sha"],
            original_handoff_run_id=intent["handoff_run_id"], as_of=intent["as_of"])
        original_batch_head = intent_ref["manifest"]["commit"]
        batch_operation = "reconcile-source" if selected["operation"] == "reconcile" or prior is not None else "put-source"
        # An already retained custody batch did not originate at this new
        # intent commit. Its original payload/ref is known; do not invent its
        # old expected head. Read-only reconciliation accepts an empty head.
        batch_head = "" if intent["prior_batch_reference"] is not None else original_batch_head
        result = command("batch-reconcile" if batch_operation == "reconcile-source" else "batch-write",
            batch_operation, asdict(source), names["batch"], batch_head)
        batch_ref = result["reference"]
        if intent["prior_batch_reference"] is not None:
            require(encoded(batch_ref) == encoded(intent["prior_batch_reference"]),
                    "NEWS_HANDOFF_PRIOR_BATCH_CHANGED")
        read("batch-read", batch_ref, source)
        report["batch_reference"] = batch_ref

        store = fresh()
        reads = _SourceReads(store.read_source, fail_closed=True)
        try:
            value = rolling_index(reads, [batch_ref], as_of=intent["as_of"],
                                  predecessor=intent["predecessor"])
            reading = read_index(reads, value)
        finally:
            reads.raise_if_terminal()
        # The original rolling builder may report an unavailable predecessor,
        # but a current native permission/quota failure cannot become that gap.
        window_source = index_source(value, names["window"])
        window_head = original_batch_head if intent["prior_batch_reference"] is not None else batch_ref["manifest"]["commit"]
        if selected["operation"] == "reconcile":
            retained_window = handoff.retained_source(fresh(), names["window"], "news-roll-" + names["window"])
            if retained_window is None:
                return incomplete("window", "NEWS_HANDOFF_WINDOW_NOT_FOUND")
            require(encoded(asdict(retained_window["source"])) == encoded(asdict(window_source)),
                    "NEWS_HANDOFF_WINDOW_CONTENT_CHANGED")
        result = command("window-reconcile" if selected["operation"] == "reconcile" else "window-write",
            "reconcile-source" if selected["operation"] == "reconcile" else "put-source",
            asdict(window_source), names["window"], window_head)
        window_ref = result["reference"]
        read("window-read", window_ref, window_source)
        report["window_reference"] = window_ref

        # A separate uncached Store reader reconstructs the saved index before
        # building the exact no-baseline, no-research, no-feedback news page.
        final_store = fresh()
        final_reads = _SourceReads(final_store.read_source, fail_closed=True)
        desk_plan = {"kind": "adhoc", "cutoff": intent["as_of"], "window": window_ref,
                     "baseline": None, "attention": [], "max_items": 12}
        try:
            recovered = open_index(final_reads, window_ref)
            recovered_reading = read_index(final_reads, recovered)
            require(encoded(recovered) == encoded(value) and encoded(recovered_reading) == encoded(reading),
                    "NEWS_HANDOFF_WINDOW_RESTORE_MISMATCH")
            document, logical = build_edition(final_reads, ResearchArtifacts(final_store), desk_plan)
        finally:
            final_reads.raise_if_terminal()
        html = render_page(page_model(document, logical))
        final_reads.raise_if_terminal()
        final_store.backend.head()
        gate()
        # Do not export a partial page on a permission change or failed stage.
        files = {"view.json": encoded(value), "reading.json": encoded(reading),
            "desk-plan.json": encoded(desk_plan), "edition.json": encoded(document),
            "news-brief.md": document["brief_markdown"].encode("utf-8"),
            "news-desk.html": html.encode("utf-8")}
        for name, raw in files.items():
            (output / name).write_bytes(raw)
        report.update(status="completed", active_stage=None,
            observed_versions=len(logical["entries"]), included=len(document["included"]),
            edition_saved=False, edition_sha256=document["edition_sha256"],
            index_sha256=value["index_sha256"], logical_sha256=value["logical_sha256"],
            html_sha256=digest(files["news-desk.html"]), coverage=value["coverage"],
            losses=value["losses"], source_reads=reads.usage(), independent_source_reads=final_reads.usage(),
            binding_rechecked=True, write="not-requested" if selected["operation"] == "reconcile" else "confirmed",
            recovery={"operation": "reconcile", "plan": {"source_run": intent["source_run"]},
                      "expected_head": "", "code_sha": "use-current-inspected-main"})
        persist()
        return 0
    except (RecordError, OSError, KeyError, TypeError, ValueError) as error:
        report.update(status="unknown-write" if isinstance(error, UnknownWrite) else "failed",
            error_code=str(error) if isinstance(error, RecordError) else "NEWS_HANDOFF_LOCAL_OR_INPUT_FAILURE",
            continuation="read-only-reconcile-original-source-run-no-write-retry")
        try:
            persist()
        except (RecordError, OSError):
            pass
        raise
    finally:
        # Logs contain status and locators only, never provider bodies or tokens.
        print(json.dumps({key: report[key] for key in ("status", "operation", "source_run",
            "confirmed_writes", "source_http_requests", "archive_downloads")}, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Completed capture to exact News Desk; no collection or schedules")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        event = _file(Path(env["GITHUB_EVENT_PATH"]), 2 * 1024 * 1024)
        selected = settings(event, env)
        binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json"), 4096))
        api = GitHubAPI.from_environment()
        return execute(selected, binding, api, args.out)
    except (RecordError, OSError, KeyError, TypeError, ValueError, UnicodeError) as error:
        print(json.dumps({"status": "failed", "error_code": str(error) if isinstance(error, RecordError)
                          else "NEWS_HANDOFF_LOCAL_OR_INPUT_FAILURE"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
