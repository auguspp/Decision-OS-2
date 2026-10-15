"""Explicit storage operation, separate from CI and from Quick/Full execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Conflict, MAX_BYTES, RecordError, SHA, SourceInput, UnknownWrite, digest, encoded, require,
)
from decision_kernel.infrastructure.git_snapshot import export_public_snapshot, verify_bundle
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.store_cli import _load

GUIDE = "docs/engineering/store-runtime.md"


class _PinnedWriter(GitHubStore):
    """User's planned head remains an operation precondition even if application reads advance."""
    def __init__(self, binding, api, expected):
        self.expected, self.published = expected, None
        super().__init__(binding, api)

    def commit(self, files, *, expected):
        if expected != self.expected:
            raise Conflict("PLANNED_HEAD_CHANGED")
        result = super().commit(files, expected=expected)
        self.published = result
        return result


def guide_source(repository: str, code_sha: str, text: str) -> SourceInput:
    require(isinstance(code_sha, str) and bool(SHA.fullmatch(code_sha)), "INVALID_CODE_SHA")
    return SourceInput("store-runtime-guide", "content", "Decision OS 2 存储运行与恢复说明",
                       f"https://github.com/{repository}/blob/{code_sha}/{GUIDE}", text,
                       limitations=("已审核代码版本的公开工程运行说明，不是投资研究或来源采集。",))


def run_operation(binding: GitHubBinding, *, api: GitHubAPI, operation: str,
                  expected: str, code_sha: str, guide: str, output: Path,
                  exporter=export_public_snapshot) -> dict:
    """The trusted host supplies configuration. Payload/role strings are not credentials."""
    require(operation in ("backup", "retain-guide"), "UNSUPPORTED_STORE_OPERATION")
    require(binding.visibility == "public" and not binding.write_enabled, "PUBLIC_READ_ONLY_CONFIG_REQUIRED")
    require(isinstance(expected, str) and bool(SHA.fullmatch(expected)), "EXPECTED_HEAD_REQUIRED")
    require(isinstance(code_sha, str) and bool(SHA.fullmatch(code_sha)), "INVALID_CODE_SHA")
    try:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        raise RecordError("NEW_OUTPUT_DIRECTORY_REQUIRED") from None
    submission = "store-guide-" + code_sha if operation == "retain-guide" else None
    report = {"schema": "store-operation-v1", "operation": operation, "repository": binding.repository,
              "branch": binding.branch, "anchor": binding.anchor, "expected_head": expected,
              "code_sha": code_sha, "submission_id": submission,
              "started_at": datetime.now(timezone.utc).isoformat(), "status": "started",
              "write": "not-requested" if operation == "backup" else "not-attempted",
              "research_executed": False, "production_backup_policy": "not-certified",
              "artifact_retention_days": 30}

    def checkpoint():
        (output / "report.json").write_bytes(encoded(report))

    checkpoint()
    try:
        writer_binding = replace(binding, write_enabled=operation == "retain-guide")
        backend = _PinnedWriter(writer_binding, api, expected)
        before = backend.head()
        report["observed_head"] = before
        if operation == "retain-guide":
            source = guide_source(binding.repository, code_sha, guide)
            report["intent_sha256"] = digest(encoded({"operation": "source", "input": asdict(source)}))
            report["write"] = "attempting"
            checkpoint()  # Sufficient original key/input identity survives an uncertain write.
            reference = VersionedStore(backend).put_source(submission, source)
            report.update(reference=asdict(reference),
                          write="published" if backend.published is not None else "replayed")
            checkpoint()
            # A second call can ONLY reconcile. Its backend cannot perform any mutation.
            reader = VersionedStore(GitHubStore(binding, api))
            repeated = reader.put_source(submission, source)
            require(repeated == reference, "REPLAY_REFERENCE_MISMATCH")
            report["idempotent_readonly_replay"] = True
            snapshot = reference.manifest.commit
        else:
            require(before == expected, "PLANNED_HEAD_CHANGED")
            snapshot = expected
        audit = audit_store(VersionedStore(backend), snapshot)
        report.update(snapshot=snapshot, online_audit=audit, status="references-verified")
        checkpoint()
        bundle = output / "records.bundle"
        restored = exporter(binding, snapshot, bundle)
        require(restored == audit, "RESTORED_REFERENCES_MISMATCH")
        report.update(status="completed", restored_audit=restored,
                      bundle={"name": bundle.name, "size": bundle.stat().st_size,
                              "sha256": digest(bundle.read_bytes()), "self_contained": True},
                      finished_at=datetime.now(timezone.utc).isoformat())
        checkpoint()
        return report
    except (RecordError, OSError) as error:
        report.update(status="unknown-write" if isinstance(error, UnknownWrite) else "failed",
                      error=str(error) if isinstance(error, RecordError) else "LOCAL_ARTIFACT_IO_FAILURE",
                      finished_at=datetime.now(timezone.utc).isoformat())
        if isinstance(error, UnknownWrite):
            report["write"] = "unconfirmed"
        checkpoint()
        if isinstance(error, RecordError):
            raise
        raise RecordError("LOCAL_ARTIFACT_IO_FAILURE") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Explicit storage operations; no research, schedules or notifications")
    commands = parser.add_subparsers(dest="command", required=True)
    operate = commands.add_parser("operate", help="trusted main-only manual Actions runtime")
    operate.add_argument("--operation", choices=("backup", "retain-guide"), required=True)
    operate.add_argument("--expected-head", required=True)
    operate.add_argument("--out", type=Path, required=True)
    restore = commands.add_parser("verify-bundle", help="offline restoration using original immutable references")
    restore.add_argument("--bundle", type=Path, required=True)
    restore.add_argument("--binding", type=Path, required=True)
    restore.add_argument("--snapshot", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-bundle":
            binding = GitHubBinding.from_dict(_load(args.binding))
            result = verify_bundle(args.bundle, binding, args.snapshot)
        else:
            require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                    and os.environ.get("GITHUB_REF") == "refs/heads/main", "MANUAL_MAIN_RUNTIME_REQUIRED")
            binding = GitHubBinding.from_dict(_load(Path("config/store-binding.json")))
            require(binding.repository == os.environ.get("GITHUB_REPOSITORY"), "RUNTIME_REPOSITORY_MISMATCH")
            code_sha = os.environ.get("GITHUB_SHA", "")
            require(bool(SHA.fullmatch(code_sha)), "INVALID_CODE_SHA")
            path = Path(GUIDE)
            require(path.is_file() and not path.is_symlink(), "CHECKED_IN_GUIDE_REQUIRED")
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            require(len(raw) <= MAX_BYTES, "GUIDE_SIZE_LIMIT")
            result = run_operation(binding, api=GitHubAPI.from_environment(), operation=args.operation,
                                   expected=args.expected_head, code_sha=code_sha,
                                   guide=raw.decode("utf-8"), output=args.out)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RecordError, OSError, UnicodeError) as error:
        code = str(error) if isinstance(error, RecordError) else "LOCAL_INPUT_OR_OUTPUT_FAILURE"
        print(json.dumps({"error": code}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
