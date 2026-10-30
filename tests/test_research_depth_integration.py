"""Real Git/installed research consumers; modeled GitHub transport, not live research."""
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, Conflict, MissingObject, RecordError, RevisionInput, encoded
from decision_kernel.domain.research_work import WORK_MARKER, ResearchProgress, ResearchRequest
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.methods.research import EXTERNAL_METHOD_ID, is_external_method, research_method
from decision_kernel.methods.full_v2 import full_method as full_v2
from decision_kernel.methods.external_full_v1 import external_full_method as external_v1
from decision_kernel.methods.research_v1 import research_method as legacy_method
from test_research_runtime import host, invoke, source, work, request


def legacy_checkpoint(store, payload, submission):
    req = ResearchRequest.from_dict(payload["request"])
    progress = ResearchProgress.from_dict(payload["progress"])
    method = research_method(req.kind, sha256=payload["method_sha256"])
    # Freeze the old wire shape rather than asking today's renderer to invent
    # its own historical fixture. Source references still come from real Git.
    old_request = {"case_id": req.case_id, "kind": req.kind, "mandate": req.mandate,
        "questions": [asdict(question) for question in req.questions],
        "predecessor": asdict(req.predecessor) if req.predecessor else None,
        "scope_change_reason": req.scope_change_reason}
    old_progress = {name: asdict(progress)[name] for name in (
        "findings", "stop", "stop_reason", "sources", "challenges", "limitations",
        "source_changes", "challenge_note")}
    note = {"schema": "research-workpaper-v1", "request": old_request, "progress": old_progress,
            "method": {"id": method["id"], "sha256": method["sha256"]}}
    body = progress.body + WORK_MARKER + encoded(note).decode("utf-8") + "```\n"
    return store.submit_revision(submission, RevisionInput(
        req.case_id, payload["revision_id"], req.kind, body,
        req.mandate, method["id"] + "@sha256:" + method["sha256"],
        progress.sources, req.predecessor, progress.unresolved(req)))


def setup_source(host, tmp_path):
    return invoke(host, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]


def test_prepare_integration_returns_real_saved_work_and_continuation_without_writing(host, tmp_path):
    ref = setup_source(host, tmp_path)
    payload = work(ref)
    payload["progress"]["stop"] = "blocked"
    payload["progress"]["stop_reason"] = "合成：没有公开保存授权不等于不能分析"
    revision = invoke(host, tmp_path / "save", "checkpoint", payload, "first")["result"]["reference"]
    before, count = host.git.head(), host.writes
    prepared = invoke(host, tmp_path / "prepare", "prepare", {"case_id": "example"})["result"]
    assert prepared["method"]["id"] == "decision-os-research-full-v3"
    assert prepared["request"]["predecessor"] == revision
    assert prepared["continuation"]["actions"][0]["action"] == "analyse-available-evidence"
    assert prepared["continuation"]["source_coverage"]["remaining"] == 0
    assert not prepared["continuation"]["source_coverage"]["limitations"]
    assert prepared["continuation"]["review_flags"] == ["BLOCKED_LABEL_DOES_NOT_ESTABLISH_RESEARCH_EXHAUSTION"]
    assert host.git.head() == before and host.writes == count


@pytest.mark.parametrize("old_method", [legacy_method("full"), full_v2(), external_v1()],
                         ids=["full-v1", "full-v2", "external-v1"])
def test_method_upgrade_reconciles_old_intent_after_branch_and_case_advance(host, tmp_path, old_method):
    ref = setup_source(host, tmp_path)
    old = work(ref, method_sha256=old_method["sha256"])
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    original = legacy_checkpoint(store, old, "legacy")
    prepared = invoke(host, tmp_path / "prepare", "prepare", {"case_id": "example"})["result"]
    assert prepared["method"]["id"] == (EXTERNAL_METHOD_ID if is_external_method(old_method)
                                         else "decision-os-research-full-v3")
    if is_external_method(old_method):
        assert prepared["full_execution"]["schema"] == "external-full-execution-contract-v1"
        prepared["request"]["scope_change_reason"] = "升级外部方法纪律，保留原问题、证据及来源合同"
    newer = work(ref, request=prepared["request"], revision_id="r2",
                 method_sha256=prepared["method"]["sha256"])
    invoke(host, tmp_path / "newer", "checkpoint", newer, "newer")
    before, count = host.git.head(), host.writes
    restored = invoke(host, tmp_path / "reconcile", "reconcile-checkpoint", old, "legacy")
    assert restored["result"]["reference"] == asdict(original)
    assert restored["write"] == "reconciled-without-write"
    assert host.git.head() == before and host.writes == count
    with pytest.raises(RecordError, match="RESEARCH_METHOD_CHANGED_REPREPARE"):
        invoke(host, tmp_path / "obsolete-write", "checkpoint", old, "different")
    assert host.writes == count


@pytest.mark.parametrize("old_method", [legacy_method("full"), full_v2(), external_v1()],
                         ids=["full-v1", "full-v2", "external-v1"])
def test_archived_reconciliation_cannot_create_missing_intent_or_accept_changed_bytes(host, tmp_path, old_method):
    ref = setup_source(host, tmp_path)
    old = work(ref, method_sha256=old_method["sha256"])
    before, count = host.git.head(), host.writes
    with pytest.raises(MissingObject, match="SUBMISSION_NOT_FOUND_NO_WRITE"):
        invoke(host, tmp_path / "missing", "reconcile-checkpoint", old, "missing")
    assert host.git.head() == before and host.writes == count
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    original = legacy_checkpoint(store, old, "legacy")
    old["progress"]["body"] += " changed"
    count = host.writes
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        invoke(host, tmp_path / "changed", "reconcile-checkpoint", old, "legacy")
    assert host.writes == count and store.current("example") == original


def test_pending_challenge_cannot_disappear_from_full_successor(host, tmp_path):
    ref = setup_source(host, tmp_path)
    first = work(ref)
    challenge = {"question_id": "economics", "argument": "重复计算经营现金中的营运资金？",
                 "disposition": "unresolved", "response": "需要补算", "evidence": [0]}
    first["progress"]["challenges"] = [challenge]
    original = invoke(host, tmp_path / "first", "checkpoint", first, "first")["result"]["reference"]
    next_work = work(ref, request=request(predecessor=original), revision_id="r2")
    count = host.writes
    with pytest.raises(RecordError, match="UNRESOLVED_CHALLENGE_MUST_BE_ADDRESSED"):
        invoke(host, tmp_path / "drop", "checkpoint", next_work, "second")
    assert host.writes == count
    next_work["progress"]["challenges"] = [{**challenge, "disposition": "changed", "response": "核算后撤掉重复扣减"}]
    second = invoke(host, tmp_path / "respond", "checkpoint", next_work, "second")
    assert second["result"]["reference"] != original
    restored = invoke(host, tmp_path / "restore", "prepare", {"case_id": "example"})["result"]
    assert restored["continuation"]["unresolved_challenges"] == []
    assert restored["previous_context"]["research"]["record"]["input"]["predecessor"] == original


@pytest.mark.parametrize("old_method", [full_v2(), external_v1()], ids=["full-v2", "external-v1"])
def test_archived_challenge_survives_current_method_upgrade(host, tmp_path, old_method):
    ref = setup_source(host, tmp_path)
    old = work(ref, method_sha256=old_method["sha256"])
    challenge = {"question_id": "economics", "argument": "原核心盈利情景为何被删除？",
                 "disposition": "unresolved", "response": "仍需有据处置", "evidence": [0]}
    old["progress"]["challenges"] = [challenge]
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    original = legacy_checkpoint(store, old, "prior")
    prepared = invoke(host, tmp_path / "prepare", "prepare", {"case_id": "example"})["result"]
    new = work(ref, request=prepared["request"], revision_id="r2",
               method_sha256=prepared["method"]["sha256"])
    head, count = host.git.head(), host.writes
    if is_external_method(old_method):
        with pytest.raises(RecordError, match="EXPLICIT_SCOPE_CHANGE_REQUIRED"):
            invoke(host, tmp_path / "no-reason", "checkpoint", new, "next")
        new["request"]["scope_change_reason"] = "更新外部方法版本，原研究责任不变"
    with pytest.raises(RecordError, match="UNRESOLVED_CHALLENGE_MUST_BE_ADDRESSED"):
        invoke(host, tmp_path / "drop", "checkpoint", new, "next")
    assert host.git.head() == head and host.writes == count
    new["progress"]["challenges"] = [{**challenge, "disposition": "changed", "response": "补回遗漏情景并限定结论"}]
    saved = invoke(host, tmp_path / "respond", "checkpoint", new, "next")
    assert saved["result"]["reference"] != asdict(original)
    assert store.read_revision(original)["record"]["input"]["method"] == old_method["id"] + "@sha256:" + old_method["sha256"]


def test_pre_upgrade_accepted_history_is_readable_despite_new_challenge_policy(host, tmp_path):
    # The old policy allowed dropping a challenge. Do NOT rewrite accepted history
    # or reject a read-only receipt when fixing the policy for new v2 submissions.
    ref = setup_source(host, tmp_path)
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    old = work(ref, method_sha256=legacy_method("full")["sha256"])
    old["progress"]["challenges"] = [{"question_id": "economics", "argument": "未解挑战",
                                     "disposition": "unresolved", "response": "待核"}]
    first = legacy_checkpoint(store, old, "old-first")
    second_payload = work(ref, request=request(predecessor=asdict(first)), revision_id="r2",
                          method_sha256=legacy_method("full")["sha256"])
    second = legacy_checkpoint(store, second_payload, "old-second")
    count = host.writes
    replay = invoke(host, tmp_path / "old-replay", "reconcile-checkpoint", second_payload, "old-second")
    assert replay["result"]["reference"] == asdict(second) and host.writes == count
