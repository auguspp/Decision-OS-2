"""Operator main-only runtime, real native Git restore with synthetic GitHub responses."""
from dataclasses import replace
from pathlib import Path
import json
import re

import pytest
import yaml

from decision_kernel.domain.records import Conflict, RecordError, StoreUnavailable, UnknownWrite
from decision_kernel.infrastructure.git_snapshot import bundle_from_bare
from decision_kernel.interfaces.store_runtime import _PinnedWriter, main, run_operation
from test_store_snapshot import public_api, base_public_api

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
GUIDE = "Synthetic recovery instructions, not investment research.\n"


def execute(api, out, *, operation="retain-guide", expected=None, code_sha=SHA, guide=GUIDE):
    binding = replace(api.binding, write_enabled=False)
    def export(binding, snapshot, output):
        return bundle_from_bare(api.git.root, binding, snapshot, output)
    return run_operation(binding, api=api, operation=operation,
                         expected=expected or api.git.head(), code_sha=code_sha,
                         guide=guide, output=out, exporter=export)


def test_published_real_git_backed_transaction_readonly_replay_and_restored_backup(public_api, tmp_path):
    before = public_api.git.head()
    report = execute(public_api, tmp_path / "first", expected=before)
    assert report["status"] == "completed" and report["write"] == "published"
    assert report["idempotent_readonly_replay"] is True and public_api.writes == 1
    assert report["mirror_audit"] == report["restored_audit"]
    assert report["mirror_audit"]["source_versions"] == 1
    assert "online_audit" not in report
    assert report["audit_basis"] == "authenticated-head-pinned-git-mirror-and-offline-full-restore-v1"
    assert report["research_executed"] is False
    second = execute(public_api, tmp_path / "second", expected=before)
    assert second["status"] == "completed" and second["write"] == "replayed"
    assert second["reference"] == report["reference"] and public_api.writes == 1
    assert second["snapshot"] == report["snapshot"]
    readonly = execute(public_api, tmp_path / "third", operation="backup")
    assert readonly["write"] == "not-requested" and public_api.writes == 1


def test_stale_expected_cannot_be_bypassed_by_application_refetch(public_api, tmp_path):
    initial = public_api.git.head()
    public_api.git.commit({"submissions/new.json": b"{}\n"}, expected=initial)
    with pytest.raises(Conflict, match="PLANNED_HEAD_CHANGED"):
        execute(public_api, tmp_path / "stale", expected=initial)
    assert public_api.writes == 0
    report = json.loads((tmp_path / "stale/report.json").read_text())
    assert report["status"] == "failed" and report["submission_id"] == "store-guide-" + SHA


def test_unknown_write_keeps_original_intent_then_exact_retry_reconciles(public_api, tmp_path):
    before = public_api.git.head()
    def lose(_):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    public_api.after_write = lose
    with pytest.raises(UnknownWrite):
        execute(public_api, tmp_path / "unknown", expected=before)
    report = json.loads((tmp_path / "unknown/report.json").read_text())
    assert report["status"] == "unknown-write" and report["write"] == "unconfirmed"
    assert report["submission_id"] == "store-guide-" + SHA and report["expected_head"] == before
    public_api.after_write = None
    recovered = execute(public_api, tmp_path / "recovered", expected=before)
    assert recovered["write"] == "replayed" and public_api.writes == 1
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        execute(public_api, tmp_path / "different", expected=before, guide="not the same intent")


def test_backup_failure_retains_successful_write_reference_without_claiming_restore(public_api, tmp_path):
    def fail(*_):
        raise StoreUnavailable("SYNTHETIC_EXPORT_FAILURE")
    with pytest.raises(StoreUnavailable):
        run_operation(replace(public_api.binding, write_enabled=False), api=public_api,
                      operation="retain-guide", expected=public_api.git.head(), code_sha=SHA,
                      guide=GUIDE, output=tmp_path / "failed", exporter=fail)
    report = json.loads((tmp_path / "failed/report.json").read_text())
    assert report["status"] == "failed" and report["write"] == "published"
    assert report["reference"]["manifest"]["commit"] == public_api.git.head()
    assert "restored_audit" not in report and public_api.writes == 1


def test_concurrent_native_cas_does_not_retry_or_overwrite(public_api, tmp_path):
    def race():
        public_api.git.commit({"submissions/winner.json": b"{}\n"}, expected=public_api.git.head())
    public_api.before_write = race
    with pytest.raises(UnknownWrite):
        execute(public_api, tmp_path / "race")
    assert public_api.writes == 1
    assert public_api.git.read(public_api.git.head(), "submissions/winner.json") == b"{}\n"


@pytest.mark.parametrize("operation,expected", [("delete", "a" * 40), ("retain-guide", "main"), ("backup", "")])
def test_invalid_operations_do_not_write(public_api, tmp_path, operation, expected):
    with pytest.raises(RecordError):
        run_operation(replace(public_api.binding, write_enabled=False), api=public_api,
                      operation=operation, expected=expected, code_sha=SHA, guide=GUIDE, output=tmp_path / "unused")
    assert public_api.writes == 0
    assert not (tmp_path / "unused").exists()


def test_explicit_main_dispatch_required_and_no_token_fallback(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    assert main(["operate", "--operation", "backup", "--expected-head", SHA, "--out", str(tmp_path / "unused")]) == 2
    assert "MANUAL_MAIN_RUNTIME_REQUIRED" in capsys.readouterr().err
    assert not (tmp_path / "unused").exists()


def test_workflow_only_manual_and_split_least_privilege_job_tokens():
    path = ROOT / ".github/workflows/store-runtime.yml"
    text = path.read_text()
    flow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"}
    assert flow["permissions"] == {}
    assert flow["concurrency"]["cancel-in-progress"] == "false"
    for name, job in flow["jobs"].items():
        assert job["permissions"] == {"contents": "write" if name == "retain-guide" else "read"}
        assert "refs/heads/main" in job["if"]
        assert "DECISION_OS_STORE_TOKEN" not in job.get("env", {})
        for step in job["steps"]:
            assert "continue-on-error" not in step
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"
                assert step["with"]["ref"] == "${{ github.sha }}"
            if "run" in step:
                assert "${{ inputs." not in step["run"]
                assert step["env"]["DECISION_OS_STORE_TOKEN"] == "${{ github.token }}"
                assert "GH_TOKEN" not in step["env"] and "GITHUB_TOKEN" not in step["env"]
                assert '"$DOS2_EXPECTED_HEAD"' in step["run"]
    for uses in re.findall(r"uses: ([^\n]+)", text):
        assert uses.startswith("./") or re.fullmatch(r"[A-Za-z0-9_./-]+@[a-f0-9]{40}", uses)


def test_many_real_versions_backup_avoids_remote_per_record_audit(public_api, tmp_path, monkeypatch):
    """Synthetic GitHub fake with a strict API call cap, genuine Git bundle and refs."""
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.domain.records import SourceInput
    from decision_kernel.infrastructure.github_store import GitHubStore

    store = VersionedStore(GitHubStore(public_api.binding, public_api))
    for n in range(10):
        store.put_source(f"bulk-input-{n}", SourceInput(
            f"bulk-source-{n}", "content", f"Synthetic source {n}",
            "synthetic:bulk", f"Synthetic verified data row {n}\n"))
    calls = 0
    previous_writes = public_api.writes
    original = public_api.request

    def limited(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 28:
            raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
        return original(*args, **kwargs)

    monkeypatch.setattr(public_api, "request", limited)
    report = execute(public_api, tmp_path / "many", operation="backup")
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["mirror_audit"] == report["restored_audit"]
    assert report["mirror_audit"]["source_versions"] == 10
    assert calls <= 28 and public_api.writes == previous_writes


def test_bundle_corruption_after_export_cannot_be_reported_as_backup(public_api, tmp_path):
    before = public_api.git.head()
    binding = replace(public_api.binding, write_enabled=False)

    def damaged_export(binding, snapshot, output):
        mirror = bundle_from_bare(public_api.git.root, binding, snapshot, output)
        output.write_bytes(output.read_bytes()[:90])
        return mirror

    with pytest.raises(RecordError):
        run_operation(binding, api=public_api, operation="backup", expected=before,
                      code_sha=SHA, guide=GUIDE, output=tmp_path / "damaged",
                      exporter=damaged_export)
    report = json.loads((tmp_path / "damaged/report.json").read_text())
    assert report["status"] == "failed" and "restored_audit" not in report
    assert report["snapshot"] == before and report["write"] == "not-requested"


def test_backup_preserves_verified_bundle_if_reader_archive_export_fails(public_api, tmp_path, monkeypatch):
    from decision_kernel.infrastructure import reader_archive
    def fail(*_, **__):
        raise RecordError("READER_CODE_HEAD_MISMATCH")
    monkeypatch.setattr(reader_archive, "archive_reader", fail)
    out = tmp_path / "reader-failed"
    binding = replace(public_api.binding, write_enabled=False)
    with pytest.raises(RecordError, match="READER_CODE_HEAD_MISMATCH"):
        run_operation(binding, api=public_api, operation="backup", expected=public_api.git.head(),
                      code_sha=SHA, guide=GUIDE, output=out, code_root=tmp_path,
                      exporter=lambda b, s, o: bundle_from_bare(public_api.git.root, b, s, o))
    report = json.loads((out / "report.json").read_bytes())
    assert report["status"] == "failed" and report["error"] == "READER_CODE_HEAD_MISMATCH"
    assert report["bundle"]["self_contained"] is True and report["restored_audit"]["status"] == "verified"
    assert report["write"] == "not-requested" and public_api.writes == 0
    assert "reader" not in report
