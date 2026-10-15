"""Explicit public research I/O on an authenticated host; never a model or scheduler."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, Conflict, MAX_BYTES, MissingObject, RecordError, SHA, SourceInput,
    SourceRef, UnknownWrite, digest, encoded, identifier, require,
)
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.methods.research import research_method

WRITE_OPERATIONS = {"put-source", "checkpoint"}
RECONCILE_OPERATIONS = {"reconcile-source", "reconcile-checkpoint"}
READ_OPERATIONS = {"prepare", "resume", "read-source"} | RECONCILE_OPERATIONS
MAX_PAYLOAD_BYTES = 60 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
WORKFLOW = ".github/workflows/research-runtime.yml"


def _object(raw: bytes) -> dict:
    def unique(pairs):
        out = {}
        for key, value in pairs:
            require(key not in out, "DUPLICATE_JSON_KEY")
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(isinstance(value, dict), "JSON_OBJECT_REQUIRED")
        encoded(value)  # Reject non-finite exponent results and invalid UTF-8, too.
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("INVALID_COMMAND_JSON") from None


def _file(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "INPUT_TOO_LARGE")
    return _object(raw)


def parse_inputs(inputs: dict) -> dict:
    """No caller-selected repo, branch, credentials, output path or executable fields."""
    require(isinstance(inputs, dict) and {"operation", "payload", "code_sha"} <= set(inputs)
            and set(inputs) <= {"operation", "payload", "code_sha", "submission_id", "expected_head"},
            "INVALID_DISPATCH_INPUTS")
    require(all(isinstance(v, str) for v in inputs.values()), "STRING_DISPATCH_INPUTS_REQUIRED")
    operation = inputs["operation"]
    require(operation in WRITE_OPERATIONS | READ_OPERATIONS, "UNSUPPORTED_RESEARCH_OPERATION")
    require(bool(SHA.fullmatch(inputs["code_sha"])), "EXPECTED_CODE_SHA_REQUIRED")
    try:
        raw = inputs["payload"].encode("utf-8")
    except UnicodeError:
        raise RecordError("INVALID_COMMAND_UTF8") from None
    require(len(raw) <= MAX_PAYLOAD_BYTES, "DISPATCH_PAYLOAD_TOO_LARGE")
    payload = _object(raw)
    require(len(encoded(payload)) <= MAX_PAYLOAD_BYTES, "DISPATCH_PAYLOAD_TOO_LARGE")
    submission = inputs.get("submission_id", "")
    expected = inputs.get("expected_head", "")
    if operation in WRITE_OPERATIONS | RECONCILE_OPERATIONS:
        identifier(submission)
    else:
        require(not submission and not expected, "UNEXPECTED_WRITE_FIELDS")
    if operation in WRITE_OPERATIONS:
        require(bool(SHA.fullmatch(expected)), "EXPECTED_RECORDS_HEAD_REQUIRED")
    elif expected:
        require(bool(SHA.fullmatch(expected)), "INVALID_EXPECTED_HEAD")
    return {"operation": operation, "payload": payload, "code_sha": inputs["code_sha"],
            "submission_id": submission, "expected_head": expected}


def _perform(store: VersionedStore, command: dict) -> dict:
    operation, payload = command["operation"], command["payload"]
    if operation in ("put-source", "reconcile-source"):
        ref = store.put_source(command["submission_id"], SourceInput.from_dict(payload))
        return {"reference": asdict(ref)}
    if operation in ("checkpoint", "reconcile-checkpoint"):
        require(set(payload) == {"request", "progress", "revision_id", "method_sha256"},
                "INVALID_CHECKPOINT_PAYLOAD")
        request = ResearchRequest.from_dict(payload["request"])
        require(payload["method_sha256"] == research_method(request.kind)["sha256"],
                "RESEARCH_METHOD_CHANGED_REPREPARE")
        return ResearchWorkflow(store).checkpoint(
            request, ResearchProgress.from_dict(payload["progress"]),
            submission_id=command["submission_id"], revision_id=payload["revision_id"])
    if operation == "read-source":
        return store.read_source(SourceRef.from_dict(payload))
    selectors = {"request", "case_id", "reference"} if operation == "prepare" else {"case_id", "reference", "cursor"}
    require(set(payload) <= selectors | {"source_limit"} and len(set(payload) & selectors) == 1,
            "ONE_RESEARCH_SELECTOR_REQUIRED")
    args = dict(payload)
    if "reference" in args:
        args["reference"] = BlobRef(**args["reference"])
    if "request" in args:
        args["request"] = ResearchRequest.from_dict(args["request"])
    if operation == "prepare":
        return ResearchWorkflow(store).prepare(**args)
    return ResearchReader(store).resume(**args)


class _CommandStore(GitHubStore):
    """Only adds operation boundaries; GitHubStore still owns CAS and byte validation."""
    def __init__(self, binding, api, command):
        self.planned_head = command["expected_head"]
        self.reconcile_only = command["operation"] in RECONCILE_OPERATIONS
        self.commit_started, self.published = False, None
        super().__init__(binding, api)

    def commit(self, files, *, expected):
        if self.reconcile_only:
            # Existing put/checkpoint can replay, but an absent intent must never write.
            raise MissingObject("SUBMISSION_NOT_FOUND_NO_WRITE")
        require(self.binding.write_enabled, "REMOTE_READ_ONLY")
        if expected != self.planned_head:
            raise Conflict("PLANNED_HEAD_CHANGED")
        self.commit_started = True
        self.published = super().commit(files, expected=expected)
        return self.published


def run_command(binding: GitHubBinding, *, api: GitHubAPI, inputs: dict,
                code_sha: str, output: Path, run_id: str = "", attempt: str = "") -> dict:
    """Caller authentication/mandate is the host's job, never a field in source text."""
    command = parse_inputs(inputs)
    require(command["code_sha"] == code_sha, "RUNTIME_CODE_CHANGED")
    require(binding.visibility == "public" and not binding.write_enabled,
            "PUBLIC_READ_ONLY_CONFIG_REQUIRED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    operation = command["operation"]
    report = {"schema": "research-operation-v1", "operation": operation,
              "code_sha": code_sha, "run_id": run_id, "run_attempt": attempt,
              "repository": binding.repository, "branch": binding.branch,
              "submission_id": command["submission_id"], "expected_head": command["expected_head"],
              "payload_sha256": digest(encoded(command["payload"])),
              "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "started", "write": "not-attempted" if operation in WRITE_OPERATIONS else "not-requested",
              "research_executed": False, "research_assessment": "not-performed",
              "human_acceptance": "not-confirmed", "execution_authority": "not-granted-by-command"}

    def persist():
        raw = encoded(report)
        require(len(raw) <= MAX_RESULT_BYTES, "COMMAND_RESULT_TOO_LARGE")
        (output / "report.json").write_bytes(raw)

    backend = None
    try:
        # Preserve exact original input before any possible write. All input must be
        # authorized for public disclosure; artifacts are transport, not a new store.
        (output / "inputs.json").write_bytes(encoded(inputs))
        persist()
        backend = _CommandStore(replace(binding, write_enabled=operation in WRITE_OPERATIONS), api, command)
        report["observed_head"] = backend.head()
        persist()
        result = _perform(VersionedStore(backend), command)
        report.update(result=result, status="completed")
        if operation in WRITE_OPERATIONS:
            report["write"] = "published" if backend.published else "replayed"
        elif operation in RECONCILE_OPERATIONS:
            report["write"] = "reconciled-without-write"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        persist()  # A verified store receipt and a retained report are distinct facts.
        return report
    except (RecordError, OSError, TypeError, KeyError) as error:
        uncertain = isinstance(error, UnknownWrite) or (
            backend is not None and backend.commit_started and not isinstance(error, Conflict))
        report.pop("result", None)
        report.update(status="unknown-write" if uncertain else "failed",
                      write="unconfirmed" if uncertain else report["write"],
                      error=str(error) if isinstance(error, RecordError) else "COMMAND_LOCAL_OR_SHAPE_FAILURE",
                      finished_at=datetime.now(timezone.utc).isoformat())
        try:
            persist()
        except (OSError, RecordError):
            pass  # Keep original inputs. Do not turn artifact I/O failure into success.
        if uncertain:
            raise UnknownWrite("RESEARCH_WRITE_REQUIRES_READONLY_RECONCILIATION") from None
        if isinstance(error, RecordError):
            raise
        raise RecordError("COMMAND_LOCAL_OR_SHAPE_FAILURE") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit public research I/O; no AI execution or schedules")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == "refs/heads/main", "MANUAL_MAIN_RUNTIME_REQUIRED")
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        require(os.environ.get("GITHUB_WORKFLOW_REF") == repository + "/" + WORKFLOW + "@refs/heads/main",
                "RESEARCH_WORKFLOW_REQUIRED")
        event = _file(Path(os.environ.get("GITHUB_EVENT_PATH", "")))
        inputs = event.get("inputs")
        command = parse_inputs(inputs)
        require(command["code_sha"] == os.environ.get("GITHUB_SHA"), "RUNTIME_CODE_CHANGED")
        mode = "write" if command["operation"] in WRITE_OPERATIONS else "read"
        require(os.environ.get("DOS2_RESEARCH_MODE") == mode, "RUNTIME_JOB_PERMISSION_MISMATCH")
        binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json")))
        require(binding.repository == repository and str(binding.repository_id) == os.environ.get("GITHUB_REPOSITORY_ID"),
                "RUNTIME_REPOSITORY_MISMATCH")
        require(binding.visibility == "public" and not binding.write_enabled,
                "PUBLIC_READ_ONLY_CONFIG_REQUIRED")
        report = run_command(binding, api=GitHubAPI.from_environment(), inputs=inputs,
                             code_sha=command["code_sha"], output=args.out,
                             run_id=os.environ.get("GITHUB_RUN_ID", ""),
                             attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""))
        # Reports hold the actual data. Never echo submitted source/research text to logs.
        print(json.dumps({key: report[key] for key in ("status", "operation", "write", "payload_sha256")}, sort_keys=True))
        return 0
    except (RecordError, OSError, UnicodeError, TypeError, KeyError) as error:
        code = str(error) if isinstance(error, RecordError) else "RUNTIME_INPUT_OR_OUTPUT_FAILURE"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
