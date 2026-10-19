"""Authorized daily finite discovery, using existing runtime/CAS/source custody."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import MissingObject, RecordError, SourceInput, SourceRef, encoded, require


def execute(*, binding, api, output, code_sha, day=None, source_capture=None):
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    from decision_kernel.infrastructure.announcement_discovery import capture
    from decision_kernel.interfaces.research_runtime import _CommandStore
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()
    end = today if day is None else date.fromisoformat(day)
    require(end <= today and (today-end).days <= 7, "DISCOVERY_EXECUTION_DATE_BOUND")
    start = end-timedelta(days=3)
    backend = GitHubStore(binding, api)
    require(backend.live_visibility() == "private", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
    head = backend.head()
    intent_id = "announcement-discovery-" + end.strftime("%Y%m%d")
    output.mkdir(parents=False, exist_ok=False)
    journal = "submissions/"+intent_id+".json"
    try:
        prior = json.loads(backend.read(head, journal))
    except MissingObject:
        prior = None
    if prior is not None:
        original = backend.last_change(head, journal)
        ref = SourceRef(backend.reference(original, prior["result_path"]))
        restored = VersionedStore(backend).read_source(ref)
        require(restored["manifest"]["input"]["source_id"] == intent_id, "DISCOVERY_SUBMISSION_IDENTITY")
        (output/"receipt.json").write_bytes(encoded({"status": "already-saved", "reference": asdict(ref), "source_requests": 0}))
        return 0
    history = sorted(p for p in backend.paths(head, "submissions/")
                     if p.startswith("submissions/announcement-discovery-") and p.endswith(".json"))
    if history:
        previous = json.loads(backend.read(head, history[-1]))
        previous_ref = SourceRef(backend.reference(backend.last_change(head, history[-1]), previous["result_path"]))
        previous_data = json.loads(VersionedStore(backend).read_source(previous_ref)["data"])
        if previous_data.get("source_hold") is True:
            (output/"receipt.json").write_bytes(encoded({"status": "source-held", "reference": asdict(previous_ref),
                "source_requests": 0, "reason": "EXPLICIT_REFUSAL_REQUIRES_REVIEW_NOT_NEXT_DAY_RETRY"}))
            return 2
    # Exact original captured metadata survives a CAS/unknown write in the native artifact.
    doc = (source_capture or capture)(start=start.isoformat(), end=end.isoformat())
    (output/"discovery.json").write_bytes(encoded(doc))
    source = SourceInput(source_id=intent_id, kind="content", title="CNINFO 公告主动发现 "+end.isoformat(),
        origin="https://www.cninfo.com.cn/new/disclosure", text=encoded(doc).decode(), custody="excerpt",
        acquired_at=doc["acquired_at"], limitations=(
            "Finite seven-keyword, no-stock-filter directory metadata; not full market coverage or verified financial evidence.",
            "Original PDF not fetched; partial pages, refused queries and stale dates remain visible."))
    payload = asdict(source)
    (output/"source-input.json").write_bytes(encoded(payload))
    require(backend.live_visibility() == "private", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
    command = {"operation": "put-source", "expected_head": head}
    writer = _CommandStore(replace(binding, write_enabled=True), api, command)
    # Reuse the existing source transaction directly: directory metadata can be
    # larger than a ChatGPT workflow-dispatch payload's separate 60-KiB limit.
    (output/"intent.json").write_bytes(encoded({"submission_id": intent_id, "expected_head": head,
        "code_sha": code_sha, "source_input": "source-input.json", "write": "not-attempted"}))
    try:
        ref = VersionedStore(writer).put_source(intent_id, source)
        VersionedStore(GitHubStore(binding, api)).read_source(ref)
    except RecordError as error:
        (output/"receipt.json").write_bytes(encoded({"status": "failed", "error": str(error),
            "write": "reconcile-original-intent" if writer.commit_started else "not-attempted",
            "submission_id": intent_id, "expected_head": head}))
        raise
    require(writer.live_visibility() == "private", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
    (output/"receipt.json").write_bytes(encoded({"status": "saved-and-read", "reference": asdict(ref),
        "all_declared_queries_complete": doc["all_declared_queries_complete"], "unique_versions": doc["unique_versions"],
        "source_requests_bound": 28, "complete_market_coverage": False}))
    return 0 if doc["all_declared_queries_complete"] else 2


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding
        require(os.environ.get("GITHUB_REPOSITORY") == "auguspp/Decision-OS-2"
                and os.environ.get("GITHUB_REF") == "refs/heads/main"
                and os.environ.get("GITHUB_EVENT_NAME") in ("schedule", "workflow_dispatch")
                and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "DISCOVERY_HOST_NOT_ALLOWED")
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        inputs = event.get("inputs", {})
        code = os.environ["GITHUB_SHA"]
        if os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch":
            require(inputs.get("code_sha") == code and inputs.get("capture_confirmed") in (True, "true"),
                    "DISCOVERY_MANUAL_CONFIRMATION_REQUIRED")
        binding = GitHubBinding.from_dict(json.loads(Path("config/store-binding.json").read_text()))
        return execute(binding=binding, api=GitHubAPI.from_environment(), output=args.out, code_sha=code,
                       day=inputs.get("day") or None)
    except (RecordError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error) if isinstance(error, RecordError) else "DISCOVERY_LOCAL_FAILURE"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
