"""Actual synthetic Git composition; no external research, source network or task execution."""
import argparse
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, Conflict, IntegrityError, RecordError, RevisionInput, SourceInput, UnknownWrite
from decision_kernel.domain.research_work import (
    QuestionFinding, ResearchChallenge, ResearchProgress, ResearchQuestion, ResearchRequest, read_workpaper,
)
from decision_kernel.infrastructure.git_store import GitStore


@pytest.fixture
def system(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "synthetic.git")
    store = VersionedStore(backend)
    ref = store.put_source("source", SourceInput("sample", "content", "合成材料", "synthetic:workflow", "仅合成数据。"))
    return backend, store, ResearchWorkflow(store), ref


def request(**updates):
    values = dict(case_id="case", kind="full", mandate="合成研究委托", questions=(ResearchQuestion("q1", "区分解释"),))
    return ResearchRequest(**{**values, **updates})


def progress(ref, **updates):
    values = dict(body="合成分析：已读引用但经济判断仅是示例。",
                  findings=(QuestionFinding("q1", "open", "还需分析", evidence=(0,),
                             remaining_kind="acquired-unanalysed", next_action="补算已取得数据"),),
                  sources=(ref,), stop="continue", stop_reason="重要分析尚未完成")
    return ResearchProgress(**{**values, **updates})


def save(workflow, req, work, key="one", rev="r1"):
    result = workflow.checkpoint(req, work, submission_id=key, revision_id=rev)
    return BlobRef(**result["reference"])


def test_direct_full_prepares_without_quick_or_any_write(system):
    backend, store, workflow, ref = system
    initial = backend.head()
    prepared = workflow.prepare(request())
    assert prepared["request"]["kind"] == "full" and prepared["previous_context"] is None
    assert prepared["execution_authority"] == "not-granted-by-preparation"
    assert backend.head() == initial and store.current("case") is None
    saved = save(workflow, request(), progress(ref))
    context = ResearchReader(store).resume(reference=saved)
    assert context["source_page"]["items"][0]["view"]["data"] == "仅合成数据。"
    assert context["research"]["record"]["input"]["kind"] == "full"
    assert read_workpaper(context["research"])[0].mandate == "合成研究委托"


def test_saved_work_resumes_same_questions_and_mandate_and_allows_revision(system):
    backend, store, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    prepared = ResearchWorkflow(VersionedStore(GitStore(backend.root))).prepare(case_id="case")
    req = ResearchRequest.from_dict(prepared["request"])
    assert req.predecessor == first and req.questions == request().questions
    second_work = progress(ref, body="合成修订：补算后限定回答", findings=(QuestionFinding("q1", "answered", "已解释", (0,)),),
                           challenges=(ResearchChallenge("q1", "可能是另一种机制", "changed", "改了论证的范围", (0,)),),
                           stop="concluded", stop_reason="原问题在声明范围内已回答")
    second = save(workflow, req, second_work, "two", "r2")
    assert store.current("case") == second
    assert read_workpaper(store.read_revision(first))[1].stop == "continue"
    assert read_workpaper(store.read_revision(second))[1].stop == "concluded"
    assert store.read_revision(second)["record"]["human_acceptance"] == "not-confirmed"


def test_same_intent_replay_survives_current_advancing(system):
    backend, store, workflow, ref = system
    req, work = request(), progress(ref)
    first = save(workflow, req, work)
    next_req = ResearchRequest.from_dict(workflow.prepare(case_id="case")["request"])
    second = save(workflow, next_req, replace(work, body="later"), "two", "r2")
    head = backend.head()
    assert save(workflow, req, work) == first and backend.head() == head
    assert save(workflow, next_req, replace(work, body="later"), "two", "r2") == second
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        save(workflow, req, replace(work, body="different"))


def test_publish_then_lost_receipt_reconciles_without_second_commit(system, monkeypatch):
    backend, store, workflow, ref = system
    commit = backend.commit
    def lose(files, *, expected):
        commit(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_RECEIPT_LOST")
    monkeypatch.setattr(backend, "commit", lose)
    with pytest.raises(UnknownWrite):
        save(workflow, request(), progress(ref))
    published = backend.head()
    monkeypatch.setattr(backend, "commit", commit)
    assert save(workflow, request(), progress(ref)).commit == published
    assert backend.head() == published


def test_stale_predecessor_never_overwrites_new_work(system):
    backend, store, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    req = replace(request(), predecessor=first)
    second = save(workflow, req, progress(ref), "two", "r2")
    with pytest.raises(Conflict, match="STALE_PREDECESSOR"):
        save(workflow, req, progress(ref), "three", "r3")
    assert store.current("case") == second


@pytest.mark.parametrize("change", [{"kind": "quick"}, {"mandate": "smaller mandate"}])
def test_scope_change_must_be_explicit_caller_input(system, change):
    backend, store, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    req = replace(request(), predecessor=first, **change)
    with pytest.raises(RecordError, match="EXPLICIT_SCOPE_CHANGE"):
        save(workflow, req, progress(ref), "two", "r2")
    explicit = replace(req, scope_change_reason="调用方明确选择的新范围；不是进度载荷自行授予")
    assert save(workflow, explicit, progress(ref), "two", "r2")


def test_quick_to_full_is_explicit_not_automatic_and_retains_original_questions(system):
    _, store, workflow, ref = system
    first = save(workflow, request(kind="quick"), progress(ref))
    resumed = workflow.prepare(case_id="case")
    assert resumed["request"]["kind"] == "quick"
    full = replace(ResearchRequest.from_dict(resumed["request"]), kind="full", scope_change_reason="合成显式Full委托")
    second = save(workflow, full, progress(ref), "two", "r2")
    assert store.read_revision(second)["record"]["input"]["predecessor"] == asdict(first)


@pytest.mark.parametrize("questions", [(ResearchQuestion("q1", "rewritten question"),), (ResearchQuestion("q2", "replacement"),)])
def test_original_questions_cannot_disappear_even_with_scope_change_reason(system, questions):
    _, _, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    changed = request(questions=questions, predecessor=first, scope_change_reason="does not permit silent deletion")
    with pytest.raises(RecordError, match="ORIGINAL_QUESTIONS"):
        workflow.prepare(changed)


def test_new_questions_are_added_without_losing_original_and_reframing_has_account(system):
    _, store, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    req = request(predecessor=first, questions=(*request().questions, ResearchQuestion("q2", "证据重构的新问题")))
    work = progress(ref, findings=(QuestionFinding("q1", "reframed", "原解释被证据否定，改查q2", (0,)),
                                   QuestionFinding("q2", "open", "新问题", (0,), "future-event", "下一披露后复核")))
    saved = save(workflow, req, work, "two", "r2")
    assert len(read_workpaper(store.read_revision(saved))[0].questions) == 2


def test_source_drop_requires_explanation_and_preserves_old_revision(system):
    _, store, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    req = replace(request(), predecessor=first)
    partial = ResearchProgress("仅保留有限分析", (QuestionFinding("q1", "open", "未核来源", remaining_kind="interrupted", next_action="恢复读取"),),
                               "blocked", "来源暂不可用", limitations=("当前不能支持相依结论",))
    with pytest.raises(RecordError, match="SOURCE_REMOVAL"):
        save(workflow, req, partial, "two", "r2")
    saved = save(workflow, req, replace(partial, source_changes="不将暂不可读原件当已引用；原精确引用仍保留在前驱"), "two", "r2")
    assert store.read_revision(saved)["record"]["input"]["sources"] == []
    assert store.read_revision(first)["record"]["input"]["sources"]


def test_damaged_source_is_not_saved_as_success(system):
    backend, store, workflow, ref = system
    bad = replace(ref, manifest=replace(ref.manifest, sha256="0" * 64))
    head = backend.head()
    with pytest.raises(IntegrityError):
        save(workflow, request(), progress(bad))
    assert backend.head() == head and store.current("case") is None


def test_plain_existing_report_requires_explicit_question_accounting_without_schema_migration(system):
    _, store, workflow, ref = system
    plain = store.submit_revision("plain", RevisionInput("case", "r1", "full", "normal report", "合成研究委托", "plain", (ref,)))
    with pytest.raises(RecordError, match="QUESTIONS_NOT_RECORDED"):
        workflow.prepare(case_id="case")
    assert workflow.prepare(request(predecessor=plain))["previous_context"]["research"]["body"] == "normal report"
    assert save(workflow, request(predecessor=plain), progress(ref), "two", "r2")


def test_prepare_historical_revision_stays_pinned_and_does_not_claim_current(system):
    _, _, workflow, ref = system
    first = save(workflow, request(), progress(ref))
    save(workflow, request(predecessor=first), replace(progress(ref), body="new"), "two", "r2")
    old = workflow.prepare(reference=first)
    assert old["request"]["predecessor"] == asdict(first)
    assert old["previous_context"]["research"]["reference"] == asdict(first)


def args(*words):
    from decision_kernel.interfaces import store_cli
    parser = argparse.ArgumentParser()
    store_cli.configure(parser.add_subparsers(dest="command"))
    return parser.parse_args(words)


def test_cli_rejects_readonly_before_accessing_credentials(tmp_path, capsys, monkeypatch):
    from decision_kernel.interfaces import store_cli
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({"repository": "example/test", "repository_id": 123, "branch": "records",
                                   "anchor": "a" * 40, "store_id": "test", "visibility": "public"}))
    def forbidden(*a, **k):
        pytest.fail("read-only checkpoint tried to access a credential")
    monkeypatch.setattr(store_cli.GitHubAPI, "from_environment", forbidden)
    code = store_cli.execute(args("store", "--binding", str(binding), "checkpoint-research", "--request", "missing",
                                  "--input", "missing", "--submission-id", "one", "--revision-id", "r1"))
    assert code == 2 and json.loads(capsys.readouterr().err)["error"] == "REMOTE_READ_ONLY"


def test_cli_same_store_prepares_and_checkpoints(system, tmp_path, capsys):
    from decision_kernel.interfaces import store_cli
    backend, store, _, ref = system
    request_file, progress_file = tmp_path / "request.json", tmp_path / "progress.json"
    request_file.write_text(json.dumps(asdict(request()), ensure_ascii=False))
    progress_file.write_text(json.dumps(asdict(progress(ref)), ensure_ascii=False))
    prefix = ("store", "--path", str(backend.root))
    assert store_cli.execute(args(*prefix, "prepare-research", "--request", str(request_file))) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["persistence"] == "not-written"
    assert store_cli.execute(args(*prefix, "checkpoint-research", "--request", str(request_file), "--input", str(progress_file),
                                   "--submission-id", "one", "--revision-id", "r1")) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert store.current("case") == BlobRef(**receipt["reference"])
    assert receipt["human_acceptance"] == "not-confirmed"
