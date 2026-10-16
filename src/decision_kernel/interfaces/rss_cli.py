"""Manual RSS capture/save, exact read, and read-only original-intent reconciliation."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import sys

from decision_kernel.application.rss_intake import collect, now, open_capture, read_capture, source_input, verify_capture
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.observations.rss_failure import SCHEMA as FAILURE_SCHEMA
from decision_kernel.domain.records import Conflict, MissingObject, RecordError, SHA, SourceInput, UnknownWrite, encoded, identifier, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.infrastructure.rss_http import fetch_feed
from decision_kernel.interfaces.research_payload import json_object

REPOSITORY = "auguspp/Decision-OS-2"
WORKFLOW = REPOSITORY + "/.github/workflows/rss-intake.yml@refs/heads/main"


def read_json(path, limit=2 * 1024 * 1024):
    with Path(path).open("rb") as handle:
        raw = handle.read(limit + 1)
    require(len(raw) <= limit, "RSS_INPUT_BOUND")
    return json_object(raw)


class _PlannedStore(GitHubStore):
    """Admission only. The existing backend still owns CAS, paths and unknown writes."""
    def __init__(self, binding, api, expected):
        self.planned_head, self.commit_started, self.published = expected, False, None
        super().__init__(binding, api)

    def commit(self, files, *, expected):
        if expected != self.planned_head:
            raise Conflict("RSS_PLANNED_HEAD_CHANGED")
        self.commit_started = True
        self.published = super().commit(files, expected=expected)
        return self.published


def run_native(output):
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_WORKFLOW_REF") == WORKFLOW
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "RSS_MANUAL_HOST_REQUIRED")
    inputs = read_json(os.environ["GITHUB_EVENT_PATH"])["inputs"]
    require(inputs["code_sha"] == os.environ.get("GITHUB_SHA"), "RSS_CODE_CHANGED")
    operation = inputs["operation"]
    require(operation in ("capture", "read") and os.environ.get("DOS2_RSS_MODE") == operation, "RSS_JOB_MISMATCH")
    require(isinstance(inputs["plan"], str) and len(inputs["plan"].encode("utf-8")) <= 60 * 1024, "RSS_PLAN_BOUND")
    plan = json_object(inputs["plan"].encode("utf-8"))
    binding = GitHubBinding.from_dict(read_json("config/store-binding.json", 4096))
    require(binding.repository == REPOSITORY and binding.repository_id == 1420041744
            and binding.visibility == "public" and not binding.write_enabled, "RSS_BINDING_CHANGED")
    if operation == "capture":
        require(inputs.get("public_metadata_custody_confirmed") == "true", "RSS_METADATA_RIGHTS_REQUIRED")
        identifier(inputs.get("submission_id")); identifier("rss-" + inputs["submission_id"])
        require(isinstance(inputs.get("expected_head"), str) and SHA.fullmatch(inputs["expected_head"]), "RSS_EXPECTED_HEAD_REQUIRED")
        if plan.get("retain_original"):
            require(inputs.get("public_original_custody_confirmed") == "true", "RSS_ORIGINAL_RIGHTS_REQUIRED")
    else:
        require(set(plan) == {"reference"} and not inputs.get("submission_id") and not inputs.get("expected_head"), "RSS_READ_PLAN_REQUIRED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"schema": "dos2-rss-operation-v1", "operation": operation, "code_sha": inputs["code_sha"],
              "run_id": os.environ["GITHUB_RUN_ID"], "run_attempt": 1, "started_at": now(),
              "status": "started", "write": "not-attempted" if operation == "capture" else "not-requested"}
    backend = None
    try:
        (output / "inputs.json").write_bytes(encoded(inputs))
        api = GitHubAPI.from_environment()
        if operation == "capture":
            backend = _PlannedStore(replace(binding, write_enabled=True), api, inputs["expected_head"])
            head = backend.head()
            require(head == inputs["expected_head"], "RSS_PLANNED_HEAD_CHANGED")
            try:
                backend.read(head, "submissions/" + inputs["submission_id"] + ".json")
            except MissingObject:
                pass
            else:
                raise RecordError("RSS_SUBMISSION_EXISTS_RECONCILE_WITHOUT_FETCH")
            store = VersionedStore(backend)
            value = collect(store, plan, batch_id="rss-run-" + os.environ["GITHUB_RUN_ID"], fetch=fetch_feed, retain_failure=True)
            if value["schema"] == FAILURE_SCHEMA:
                report.update(source_outcome="unavailable", source_error_code=value["error_code"],
                              failure_stage=value["stage"])
            source = source_input(value, inputs["submission_id"])
            # Freeze the exact original intent BEFORE the only possible native CAS write.
            (output / "source.json").write_bytes(encoded(asdict(source)))
            (output / "capture.json").write_bytes(encoded(value))
            (output / "reading.json").write_bytes(encoded(read_capture(value)))
            ref = store.put_source(inputs["submission_id"], source)
            report.update(reference=asdict(ref), write="published" if backend.published else "replayed")
        else:
            store = VersionedStore(GitHubStore(binding, api))
            value = open_capture(store, plan["reference"])
            (output / "capture.json").write_bytes(encoded(value))
            (output / "reading.json").write_bytes(encoded(read_capture(value)))
            report["reference"] = plan["reference"]
        if value["schema"] == FAILURE_SCHEMA:
            report.update(status="source-failed-receipt-saved" if operation == "capture" else "completed",
                          source_outcome="unavailable", source_error_code=value["error_code"],
                          failure_stage=value["stage"], capture_sha256=value["capture_sha256"],
                          observations=None, original_status=value["original"]["status"],
                          predecessor=value["predecessor"])
            return 2 if operation == "capture" else 0
        report.update(status="completed", capture_sha256=value["capture_sha256"],
                      observations=len(value["observations"]), delta=value["delta"],
                      original_status=value["original"]["status"], predecessor=value["predecessor"])
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        uncertain = isinstance(error, UnknownWrite) or (backend is not None and backend.commit_started and not isinstance(error, Conflict))
        code = str(error) if isinstance(error, RecordError) else "RSS_OPERATION_FAILED"
        report.update(status="unknown-write-reconcile-original" if uncertain else "failed",
                      error_code=code if re.fullmatch(r"[A-Z0-9_]{1,128}", code) else "RSS_OPERATION_FAILED")
        if uncertain:
            report["write"] = "unknown"
        return 2
    finally:
        report["finished_at"] = now()
        (output / "report.json").write_bytes(encoded(report))
        print(json.dumps(report, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.rss_cli")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run"); run.add_argument("--out", type=Path, required=True)
    read = commands.add_parser("read-local"); read.add_argument("--input", type=Path, required=True)
    read.add_argument("--query", default=""); read.add_argument("--offset", type=int, default=0); read.add_argument("--limit", type=int, default=40)
    reconcile = commands.add_parser("reconcile"); reconcile.add_argument("--input", type=Path, required=True)
    reconcile.add_argument("--binding", type=Path, required=True); reconcile.add_argument("--submission-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return run_native(args.out)
        if args.command == "read-local":
            result = read_capture(read_json(args.input), query=args.query, offset=args.offset, limit=args.limit)
        else:
            # Read-only credentials/config: VersionedStore may replay an existing intent,
            # but a missing journal cannot cause a commit. Never fetch RSS here.
            binding = GitHubBinding.from_dict(read_json(args.binding, 4096))
            require(not binding.write_enabled, "RSS_RECONCILE_READ_ONLY_REQUIRED")
            source = SourceInput.from_dict(read_json(args.input))
            verify_capture(json_object(source.text.encode("utf-8")))
            store = VersionedStore(GitHubStore(binding, GitHubAPI.from_environment()))
            result = {"reference": asdict(store.put_source(args.submission_id, source)), "write": "reconciled-without-write"}
        print(json.dumps(result, ensure_ascii=False)); return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        code = str(error) if isinstance(error, RecordError) else "RSS_LOCAL_OR_INPUT_FAILURE"
        print(json.dumps({"status": "failed", "error_code": code if re.fullmatch(r"[A-Z0-9_]{1,128}", code) else "RSS_LOCAL_OR_INPUT_FAILURE"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
