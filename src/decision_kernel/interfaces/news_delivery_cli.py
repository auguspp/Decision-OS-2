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
from decision_kernel.interfaces.news_page import render_page
from decision_kernel.interfaces.research_payload import json_object, pack_payload
from decision_kernel.interfaces.research_runtime import run_command


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
