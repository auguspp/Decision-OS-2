"""Explicit public research I/O on an authenticated host; never a model or scheduler."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from decision_kernel.interfaces import odds_commands
from decision_kernel.interfaces.research_use_commands import audit_source_use

from decision_kernel.application.numerical_audit import NumericalAudit
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery, validate_delivery, verify_fresh
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference
from decision_kernel.application.research_catalogue import ResearchCatalogue
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
from decision_kernel.interfaces.research_payload import (
    MAX_PAYLOAD_BYTES, decode_payload, json_object as _object,
)

WRITE_OPERATIONS = {"put-source", "checkpoint", "deliver", "put-artifact"} | set(odds_commands.WRITE_COMMANDS)
RECONCILE_OPERATIONS = {"reconcile-source", "reconcile-checkpoint", "reconcile-delivery", "reconcile-artifact"} | set(odds_commands.RECONCILE_COMMANDS)
READ_OPERATIONS = {"audit-source-use", "prepare", "resume", "read-source", "catalogue", "audit-numbers", "verify-delivery", "open-delivery", "read-artifact"} | RECONCILE_OPERATIONS | set(odds_commands.READ_COMMANDS)
MAX_RESULT_BYTES = 8 * 1024 * 1024
WORKFLOW = ".github/workflows/research-runtime.yml"


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
    payload = decode_payload(inputs["payload"])
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
    if operation == "audit-source-use":
        return audit_source_use(store, payload)
    if operation in odds_commands.COMMANDS + odds_commands.RECONCILE_COMMANDS:
        return odds_commands.perform(store, operation, payload, command["submission_id"])
    if operation in ("deliver", "reconcile-delivery"):
        if operation == "deliver":
            require(payload["method_sha256"] == research_method(payload["request"]["kind"])["sha256"],
                    "RESEARCH_METHOD_CHANGED_REPREPARE")
        return ResearchDelivery(store).deliver(payload, submission_id=command["submission_id"])
    if operation in ("verify-delivery", "open-delivery"):
        require(set(payload) <= {"reference", "history_limit"} and "reference" in payload,
                "EXACT_DELIVERY_REFERENCE_REQUIRED")
        delivery, ref = ResearchDelivery(store), BlobRef(**payload["reference"])
        if operation == "open-delivery":
            return delivery.open(ref, history_limit=payload.get("history_limit", 2))
        require(set(payload) == {"reference"}, "INVALID_DELIVERY_VERIFY_INPUT")
        return delivery.verify(ref)
    if operation in ("put-artifact", "reconcile-artifact"):
        ref = ResearchArtifacts(store).put(command["submission_id"], ArtifactInput.from_dict(payload["artifact"]))
        return {"reference": asdict(ref)}
    if operation == "read-artifact":
        return ResearchArtifacts(store).read(artifact_reference(payload))
    if operation == "audit-numbers":
        audit = NumericalAudit(store)
        return audit.saved(BlobRef(**payload["reference"])) if set(payload) == {"reference"} else audit.audit(payload)
    if operation == "catalogue":
        require(set(payload) <= {"cursor", "limit"}, "INVALID_CATALOGUE_PAYLOAD")
        return ResearchCatalogue(store).page(**payload)
    if operation in ("put-source", "reconcile-source"):
        ref = store.put_source(command["submission_id"], SourceInput.from_dict(payload))
        return {"reference": asdict(ref)}
    if operation in ("checkpoint", "reconcile-checkpoint"):
        require(set(payload) == {"request", "progress", "revision_id", "method_sha256"},
                "INVALID_CHECKPOINT_PAYLOAD")
        request = ResearchRequest.from_dict(payload["request"])
        if operation == "checkpoint":
            require(payload["method_sha256"] == research_method(request.kind)["sha256"],
                    "RESEARCH_METHOD_CHANGED_REPREPARE")
        else:
            # Rebuild the original method, not today's method. _CommandStore and
            # the read-only job still forbid a missing intent from publishing.
            research_method(request.kind, sha256=payload["method_sha256"])
        return ResearchWorkflow(store).checkpoint(
            request, ResearchProgress.from_dict(payload["progress"]),
            submission_id=command["submission_id"], revision_id=payload["revision_id"],
            method_sha256=payload["method_sha256"])
    if operation == "read-source":
        return store.read_source(SourceRef.from_dict(payload))
    selectors = {"request", "case_id", "reference"} if operation == "prepare" else {"case_id", "reference", "cursor"}
    extras = {"source_limit"} | ({"evidence_plan", "evidence_cursor", "evidence_limit"} if operation == "prepare" else set())
    require(set(payload) <= selectors | extras and len(set(payload) & selectors) == 1,
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
        self.delivery = command["operation"] == "deliver"
        self.published_stages = []
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
        if self.delivery:
            # Advance only through this call's confirmed native-CAS commits.
            self.planned_head = self.published
            self.published_stages.append(self.published)
            self.commit_started = False
        return self.published


def run_command(binding: GitHubBinding, *, api: GitHubAPI, inputs: dict,
                code_sha: str, output: Path, run_id: str = "", attempt: str = "") -> dict:
    """Caller authentication/mandate is the host's job, never a field in source text."""
    command = parse_inputs(inputs)
    require(command["code_sha"] == code_sha, "RUNTIME_CODE_CHANGED")
    require(binding.visibility == "public" and not binding.write_enabled,
            "PUBLIC_READ_ONLY_CONFIG_REQUIRED")
    # Reject unconfirmed publication before saving inputs into public job artifacts.
    if command["operation"] in ("deliver", "reconcile-delivery"):
        validate_delivery(command["payload"])
    if command["operation"] in ("put-artifact", "reconcile-artifact"):
        payload = command["payload"]
        require(set(payload) == {"artifact", "rights_confirmed", "rights_basis"}
                and payload["rights_confirmed"] is True, "ARTIFACT_RIGHTS_NOT_CONFIRMED")
        require(isinstance(payload["rights_basis"], str) and bool(payload["rights_basis"].strip()),
                "ARTIFACT_RIGHTS_NOT_CONFIRMED")
        ArtifactInput.from_dict(payload["artifact"])
    odds_commands.preflight(command["operation"], command["payload"])
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
        if operation in ("deliver", "reconcile-delivery"):
            result = verify_fresh(result, lambda: VersionedStore(GitHubStore(binding, api)))
        if operation in odds_commands.WRITE_COMMANDS + odds_commands.RECONCILE_COMMANDS:
            result = odds_commands.verify_fresh(result, operation, lambda: VersionedStore(GitHubStore(binding, api)))
        report.update(result=result, status="completed")
        if operation == "audit-source-use" and result["status"] != "usage-demonstrated":
            report["status"] = "source-use-not-demonstrated"
        if operation in odds_commands.WRITE_COMMANDS + odds_commands.RECONCILE_COMMANDS and result["readback"]["status"] != "verified":
            report["status"] = "odds-readback-failed"
        if operation in ("deliver", "reconcile-delivery", "verify-delivery"):
            verification = result.get("delivery_verification", result)
            if verification.get("status") != "verified-declared-inventory":
                report["status"] = "delivery-not-complete"
            report["published_stages"] = backend.published_stages
        if operation == "audit-numbers" and result["status"] not in ("checked", "conditional"):
            report["status"] = "numerical-not-cleared"  # Keep diagnostics; do not present a green audit.
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
        if backend is not None and backend.delivery and backend.published_stages:
            report["published_stages"] = backend.published_stages
            report["write"] = "partial-confirmed"
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
        return 0 if report["status"] == "completed" else 2
    except (RecordError, OSError, UnicodeError, TypeError, KeyError) as error:
        code = str(error) if isinstance(error, RecordError) else "RUNTIME_INPUT_OR_OUTPUT_FAILURE"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
