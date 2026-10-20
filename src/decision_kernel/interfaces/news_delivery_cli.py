"""News desk branch of the existing manual news host, using its original artifact writer."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from decision_kernel.application.brief_delivery import build_edition, open_edition, page_model, plan_input
from decision_kernel.application.news_rolling import clock
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import SHA, RecordError, encoded, identifier, require
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.infrastructure.runtime_publication import publication_basis
from decision_kernel.interfaces.news_page import render_page
from decision_kernel.interfaces.research_payload import json_object, pack_payload
from decision_kernel.interfaces.research_runtime import run_command


def _latest_publication(binding, inputs):
    """Apply the existing revocable deployment grant only to publication fields."""
    effective, basis = dict(inputs), {}
    for field, scopes in {
        "public_metadata_custody_confirmed": ("sources", "brief", "reading"),
        "public_feedback_custody_confirmed": ("watch",),
        "public_research_custody_confirmed": ("research",),
        "public_sources_custody_confirmed": ("sources",),
    }.items():
        grants = {scope: publication_basis(binding, scope) for scope in scopes}
        if all(grants.values()):
            effective[field] = "true"
            basis.update(grants)
    require(effective.get("public_metadata_custody_confirmed") == "true",
            "NEWS_DESK_METADATA_PUBLICATION_NOT_CONFIRMED")
    return effective, basis


def _latest_document_rights(document, inputs):
    # Choices may have come from a real baseline, not just this edition's plan.
    require(not document["attention_state"] or inputs.get("public_feedback_custody_confirmed") == "true",
            "NEWS_DESK_FEEDBACK_PUBLICATION_NOT_CONFIRMED")
    research_rights(document["plan"], inputs)
    industry_rights(document["plan"], inputs)
    watch_rights(document, inputs)


def _latest_runs(backend):
    """Native metadata only, using the existing backend's single API budget."""
    from decision_kernel.application.runtime_health import listing, positive, run_summary, clock as run_clock
    filenames = ("news-intake.yml", "rss-intake.yml", "news-custody.yml", "news-rolling.yml")
    panels = []
    for filename in filenames:
        path = "/repos/" + backend.repository + "/actions/workflows/" + filename
        workflow = backend.api.request("GET", path)
        require(isinstance(workflow, dict) and workflow.get("path") == ".github/workflows/" + filename,
                "NEWS_NATIVE_WORKFLOW_IDENTITY")
        wid = positive(workflow.get("id"))
        original = backend.api.request("GET", path + "/runs?branch=main&per_page=20")
        rows = [run_summary(row, backend.binding, filename, wid)
                for row in listing(original, "workflow_runs", 20)]
        rows.sort(key=lambda row: (run_clock(row["run_started_at"]), row["id"]), reverse=True)
        panels.append({"workflow": ".github/workflows/" + filename, "workflow_id": wid,
            "total_runs": original["total_count"], "inspected_runs": len(rows),
            "coverage": "first-20-main-runs-only", "full_run_history": original["total_count"] == len(rows),
            "latest_observed_attempt": rows[0] if rows else None,
            "selection": "latest-observed-start-not-last-success", "business_content_verified": False})
    return {"status": "observed", "scope": "four-fixed-news-workflows-main-only", "workflows": panels,
        "meaning": "execution-metadata-not-capture-custody-or-continuous-production",
        "source_http_requests": 0}


def _latest_code(backend, expected):
    value = backend.api.request("GET", "/repos/" + backend.repository + "/git/ref/heads/main")
    require(isinstance(value, dict) and value.get("ref") == "refs/heads/main"
            and value.get("object", {}).get("type") == "commit"
            and value["object"]["sha"] == expected, "NEWS_ROLL_CODE_CHANGED")


def run_latest_desk(output, inputs):
    """A normal read-only news host consumer, never a new research operation."""
    from decision_kernel.application.news_desk_navigation import build_navigation, plan_input
    from decision_kernel.interfaces.news_rolling_cli import REPOSITORY, _SourceReads, _file
    from decision_kernel.interfaces.news_page import navigation_summary
    require(inputs.get("operation") == "latest-desk" and inputs.get("save") == "false",
            "NEWS_LATEST_READ_ONLY_REQUIRED")
    require(not inputs.get("submission_id") and not inputs.get("expected_head"), "NEWS_DESK_READ_WRITE_FIELDS")
    require(os.environ.get("DOS2_NEWS_MODE") == "read", "NEWS_DESK_JOB_PERMISSION")
    text = inputs.get("plan", "")
    require(isinstance(text, str) and len(text.encode()) <= 4096, "NEWS_NAVIGATION_PLAN_BOUND")
    now = datetime.now(timezone.utc).isoformat()
    plan = plan_input(json_object(text.encode()) if text.strip() else {}, now=now)
    binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json"), 4096))
    require(binding.repository == REPOSITORY and binding.repository_id == 1420041744
            and binding.visibility == "public" and not binding.write_enabled, "NEWS_DESK_BINDING_CHANGED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"schema": "dos2-news-desk-operation-v1", "operation": "latest-desk",
        "code_sha": inputs["code_sha"], "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": 1,
        "status": "started", "write": "not-requested", "source_http_requests": 0,
        "research_executed": False, "notification_sent": False, "human_acceptance": "not-confirmed"}
    export_started = False
    try:
        effective, authority = _latest_publication(binding, inputs)
        api = GitHubAPI.from_environment()
        store = VersionedStore(GitHubStore(binding, api))
        _latest_code(store.backend, inputs["code_sha"])
        reads = _SourceReads(store.read_source, fail_closed=True)
        checked = []
        def authorize(document):
            _latest_document_rights(document, effective)
            checked.append(document)
        try:
            navigation, model = build_navigation(store, reads, plan, now=now, authorize_document=authorize)
        finally:
            # Original rolling may turn a failed batch read into an incomplete
            # index error. Preserve the first native fatal error and do no I/O.
            reads.raise_if_terminal()
        navigation["native_runs"] = _latest_runs(store.backend)
        rendered = datetime.now(timezone.utc).isoformat()
        navigation["rendered_at"] = rendered
        for part in (navigation["display"], navigation["latest_window"], navigation["latest_saved_edition"]):
            if part.get("clocks") is not None:
                part["clocks"]["rendered_at"] = rendered
        if model is None:
            model = {"edition": None, "articles": [], "saved_reference": None, "navigation": navigation}
        html = render_page(model)
        summary = navigation_summary(model)
        files = {"plan.json": encoded(navigation["plan"]), "navigation.json": encoded(navigation),
                 "news-desk.html": html.encode(), "news-summary.md": summary.encode()}
        if model["edition"] is not None:
            document = model["edition"]
            files.update({"edition.json": encoded(document), "news-brief.md": document["brief_markdown"].encode(),
                "quick-candidates.json": encoded({"status": "candidate-not-executed", "research_started": False,
                                                  "candidates": document["quick_candidates"]})})
        reads.raise_if_terminal()
        store.backend.head()
        _latest_code(store.backend, inputs["code_sha"])
        final_effective, final_authority = _latest_publication(binding, inputs)
        require(authority == final_authority, "NEWS_DESK_PUBLICATION_AUTHORITY_CHANGED")
        for document in checked:
            _latest_document_rights(document, final_effective)
        export_started = True
        for name, raw in files.items():
            (output / name).write_bytes(raw)
        report.update(status="completed", content_status=navigation["status"], snapshot=navigation["snapshot"],
            display=navigation["display"], source_reads=reads.usage(), storage_api_requests=api._requests,
            publication_authorization=authority, binding_rechecked=True)
        return 0
    except (RecordError, OSError, KeyError, ValueError, TypeError) as error:
        report.update(status="failed-during-export" if export_started else "failed-no-content-exported",
                      error_code=str(error) if isinstance(error, RecordError)
                      else "NEWS_NAVIGATION_LOCAL_OR_INPUT_FAILURE")
        raise
    finally:
        (output / "desk-report.json").write_bytes(encoded(report))
        print(json.dumps(report, ensure_ascii=False))


def research_rights(plan, inputs):
    if "research" in plan:
        require(inputs.get("public_research_custody_confirmed") == "true",
                "BRIEF_RESEARCH_PUBLICATION_NOT_CONFIRMED")
        require(plan["research"]["agenda"] is None or inputs.get("public_feedback_custody_confirmed") == "true",
                "NEWS_DESK_FEEDBACK_PUBLICATION_NOT_CONFIRMED")


def industry_rights(plan, inputs):
    if "industry" in plan:
        require(inputs.get("public_sources_custody_confirmed") == "true",
                "BRIEF_INDUSTRY_PUBLICATION_NOT_CONFIRMED")


def watch_rights(document, inputs):
    """A retained evidence judgment does not grant permission to export it."""
    from decision_kernel.application.watch_evidence import export_required
    if export_required(document.get("research") or {}):
        require(inputs.get("public_sources_custody_confirmed") == "true"
                and inputs.get("public_feedback_custody_confirmed") == "true",
                "BRIEF_WATCH_PUBLICATION_NOT_CONFIRMED")


def run_desk(output, inputs):
    # The original host checks repository/workflow/main/attempt/code before routing.
    from decision_kernel.interfaces.news_rolling_cli import REPOSITORY, _SourceReads, _file
    operation = inputs["operation"]
    saving = inputs["save"] == "true"
    require(operation in ("desk", "read-desk") and inputs["save"] in ("true", "false"), "NEWS_DESK_OPERATION")
    require(not saving or operation == "desk", "NEWS_DESK_READ_CANNOT_SAVE")
    require(os.environ.get("DOS2_NEWS_MODE") == ("write" if saving else "read"), "NEWS_DESK_JOB_PERMISSION")
    if saving:
        require(inputs.get("public_metadata_custody_confirmed") == "true", "NEWS_DESK_RIGHTS_REQUIRED")
        identifier(inputs.get("submission_id")); identifier("news-edition-" + inputs["submission_id"])
        require(isinstance(inputs.get("expected_head"), str) and SHA.fullmatch(inputs["expected_head"]),
                "NEWS_DESK_EXPECTED_HEAD_REQUIRED")
    else:
        require(not inputs.get("submission_id") and not inputs.get("expected_head"), "NEWS_DESK_READ_WRITE_FIELDS")
    text = inputs["plan"]
    require(isinstance(text, str) and len(text.encode()) <= 60 * 1024, "NEWS_DESK_PLAN_BOUND")
    plan = json_object(text.encode())
    if operation == "desk":
        plan = plan_input(plan)
        require(clock(plan["cutoff"]) <= datetime.now(timezone.utc), "NEWS_DESK_FUTURE_CUTOFF")
        # Workflow artifacts are public too; this check is required even when save=false.
        require(not plan["attention"] or inputs.get("public_feedback_custody_confirmed") == "true",
                "NEWS_DESK_FEEDBACK_PUBLICATION_NOT_CONFIRMED")
        research_rights(plan, inputs)
        industry_rights(plan, inputs)
    else:
        require(set(plan) == {"reference"}, "NEWS_DESK_EXACT_ARTIFACT_REQUIRED")
    binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json"), 4096))
    require(binding.repository == REPOSITORY and binding.repository_id == 1420041744
            and binding.visibility == "public" and not binding.write_enabled, "NEWS_DESK_BINDING_CHANGED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"schema": "dos2-news-desk-operation-v1", "operation": operation,
              "code_sha": inputs["code_sha"], "run_id": os.environ["GITHUB_RUN_ID"],
              "run_attempt": 1, "status": "started", "write": "not-attempted" if saving else "not-requested",
              "research_executed": False, "notification_sent": False, "human_acceptance": "not-confirmed"}
    try:
        (output / "plan.json").write_bytes(encoded(plan))
        api = GitHubAPI.from_environment()
        store = VersionedStore(GitHubStore(binding, api))
        reads = _SourceReads(store.read_source)
        artifacts = ResearchArtifacts(store)
        if operation == "desk":
            doc, logical = build_edition(reads, artifacts, plan)
            saved_ref = None
        else:
            doc, logical = open_edition(reads, artifacts, plan["reference"])
            saved_ref = plan["reference"]
            research_rights(doc["plan"], inputs)
            industry_rights(doc["plan"], inputs)
        watch_rights(doc, inputs)
        store.backend.head()  # Revalidate mutable access before releasing source-derived results.
        (output / "edition.json").write_bytes(encoded(doc))
        (output / "news-brief.md").write_text(doc["brief_markdown"], encoding="utf-8")
        (output / "quick-candidates.json").write_bytes(encoded({"status": "candidate-not-executed",
                              "research_started": False, "candidates": doc["quick_candidates"]}))
        # Preserve readable output even if the original writer later reports a known/unknown conflict.
        (output / "news-desk.html").write_text(render_page(page_model(doc, logical)), encoding="utf-8")
        if saving:
            artifact = ArtifactInput.from_bytes("news-edition-" + inputs["submission_id"], "news-edition.json",
                                                "application/json", encoded(doc))
            payload = {"artifact": asdict(artifact), "rights_confirmed": True,
                       "rights_basis": "Authorized retained public news metadata and original delivery; research excerpts, industry sources and feedback separately confirmed before export."}
            command = {"operation": "put-artifact", "code_sha": inputs["code_sha"],
                       "submission_id": inputs["submission_id"], "expected_head": inputs["expected_head"],
                       "payload": pack_payload(encoded(payload))}
            # Identical original command retained by the common writer for reconcile-artifact.
            receipt = run_command(binding, api=api, inputs=command, code_sha=inputs["code_sha"],
                                  output=output / "result", run_id=os.environ["GITHUB_RUN_ID"], attempt="1")
            report["write"] = receipt["write"]
            if receipt["status"] != "completed":
                report.update(status="save-not-confirmed", original_writer_status=receipt["status"])
                return 2
            saved_ref = receipt["result"]["reference"]
        (output / "news-desk.html").write_text(render_page(page_model(doc, logical, saved_ref)), encoding="utf-8")
        report.update(status="completed", reference=saved_ref, edition_sha256=doc["edition_sha256"],
                      observed_versions=len(logical["entries"]), included=len(doc["included"]),
                      comparison=doc["comparison"], source_reads=reads.usage(),
                      storage_api_requests=getattr(api, "_requests", None))
        if "research" in doc:
            report.update(research_included=len(doc["research"]["included"]),
                          research_omissions=doc["research"]["omissions"],
                          research_coverage=doc["research"]["coverage"])
        if "industry" in doc:
            report.update(industry_included=len(doc["industry"]["included"]),
                          industry_omissions=doc["industry"]["omissions"],
                          industry_coverage=doc["industry"]["coverage"])
        return 0
    except (RecordError, OSError, KeyError, ValueError, TypeError):
        report["status"] = "failed-see-original-receipt"
        raise
    finally:
        (output / "desk-report.json").write_bytes(encoded(report))
        print(json.dumps(report, ensure_ascii=False))
