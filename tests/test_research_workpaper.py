"""Synthetic method declarations; these checks never certify research quality."""
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.domain.records import BlobRef, RecordError, Selection, SourceRef, digest, encoded
from decision_kernel.domain.research_work import (
    ACTIONABLE_KINDS, WORK_MARKER, QuestionFinding, ResearchChallenge, ResearchProgress,
    ResearchQuestion, ResearchRequest, continue_request, read_workpaper, render_workpaper,
)
from decision_kernel.methods.research import research_method


def request(**changes):
    data = dict(case_id="synthetic", kind="full", mandate="合成经营问题，不是投资委托。",
                questions=(ResearchQuestion("q1", "什么证据可以区分两种解释？"),))
    return ResearchRequest(**{**data, **changes})


def progress(**changes):
    data = dict(body="仅为合成工作底稿。", findings=(QuestionFinding("q1", "answered", "有限回答"),),
                stop="continue", stop_reason="继续分析", limitations=("仅为测试",))
    return ResearchProgress(**{**data, **changes})


def stored(req, work):
    method = research_method(req.kind)
    payload = {"case_id": req.case_id, "kind": req.kind, "mandate": req.mandate,
               "predecessor": asdict(req.predecessor) if req.predecessor else None,
               "sources": [asdict(s) for s in work.sources],
               "method": method["id"] + "@sha256:" + method["sha256"]}
    return json.loads(encoded({"body": render_workpaper(req, work, method), "record": {"input": payload}}))


@pytest.mark.parametrize("kind", ["quick", "full"])
def test_method_is_versioned_deterministic_and_returns_independent_value(kind):
    a = research_method(kind)
    claimed = a.pop("sha256")
    assert digest(encoded(a)) == claimed
    a["steps"].clear()
    b = research_method(kind)
    assert b["steps"] and b["sha256"] == claimed
    assert b["assessment"] == "guidance-only-not-economic-certification"


def test_methods_do_not_collapse_quick_into_full():
    assert research_method("quick")["sha256"] != research_method("full")["sha256"]
    assert "discovery" in {s["topic"] for s in research_method("quick")["steps"]}
    assert "direct" in {s["topic"] for s in research_method("full")["steps"]}


@pytest.mark.parametrize("changes", [{"kind": "decision"}, {"questions": ()},
    {"questions": (ResearchQuestion("q1", "a"), ResearchQuestion("q1", "b"))}, {"predecessor": "latest"}])
def test_request_rejects_invalid_scope(changes):
    with pytest.raises(RecordError):
        request(**changes)


@pytest.mark.parametrize("extra", [{"actor": "human"}, {"write_enabled": True}, {"token": "fake"}, {"case_id": "other"}, {"kind": "full"}])
def test_generated_progress_cannot_select_target_or_grant_permission(extra):
    with pytest.raises(RecordError, match="INVALID_RESEARCH_PROGRESS"):
        ResearchProgress.from_dict({**asdict(progress()), **extra})


@pytest.mark.parametrize("kind", sorted(ACTIONABLE_KINDS))
def test_material_actionable_gap_cannot_be_declared_concluded_but_can_be_saved_as_partial(kind):
    findings = (QuestionFinding("q1", "open", "重要问题尚未回答", remaining_kind=kind, next_action="实际下一步"),)
    assert progress(findings=findings).stop == "continue"
    with pytest.raises(RecordError, match="ACTIONABLE_WORK"):
        progress(findings=findings, stop="concluded", challenge_note="已检查挑战")


@pytest.mark.parametrize("kind", ["future-event", "currently-unidentifiable"])
def test_bounded_conclusion_can_leave_honest_future_unknown(kind):
    findings = (QuestionFinding("q1", "open", "结论限于现有证据", remaining_kind=kind,
                                next_action="若真实新披露能区分解释则重开，不建立任务"),)
    work = progress(findings=findings, stop="concluded", challenge_note="限定结论及替代解释已在正文说明")
    assert work.unresolved(request()) and work.stop == "concluded"


def test_challenge_must_be_accounted_before_declared_conclusion():
    unresolved = ResearchChallenge("q1", "另一种解释", "unresolved", "尚未找到判别证据")
    assert progress(challenges=(unresolved,)).challenges
    with pytest.raises(RecordError, match="UNRESOLVED_CHALLENGE"):
        progress(challenges=(unresolved,), stop="concluded")
    with pytest.raises(RecordError, match="CHALLENGE_ACCOUNTING"):
        progress(stop="concluded")
    assert progress(challenges=(replace(unresolved, disposition="retained-with-reason", response="有据保留"),),
                    stop="concluded").stop == "concluded"


@pytest.mark.parametrize("findings", [(), (QuestionFinding("q2", "answered", "a"),),
    (QuestionFinding("q1", "answered", "a"), QuestionFinding("q1", "answered", "b"))])
def test_every_original_question_accounted_exactly_once(findings):
    with pytest.raises(RecordError):
        progress(findings=findings).validate_for(request())


@pytest.mark.parametrize("remaining,next_action", [(None, "a"), ("future-event", ""), ("invented", "a")])
def test_open_question_requires_real_remaining_category_and_next_action(remaining, next_action):
    with pytest.raises(RecordError):
        QuestionFinding("q1", "open", "unknown", remaining_kind=remaining, next_action=next_action)


@pytest.mark.parametrize("indices", [(True,), (-1,), (64,), (0, 0), ("0",)])
def test_evidence_indices_are_strict(indices):
    with pytest.raises(RecordError):
        QuestionFinding("q1", "answered", "a", evidence=indices)


def test_out_of_range_source_index_is_not_a_citation():
    with pytest.raises(RecordError, match="OUT_OF_BOUNDS"):
        progress(findings=(QuestionFinding("q1", "answered", "a", evidence=(0,)),))


def test_all_strings_and_size_are_bounded():
    with pytest.raises(RecordError):
        progress(body="x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(RecordError):
        progress(body="body" + WORK_MARKER)
    with pytest.raises(RecordError):
        progress(stop_reason="")


def test_json_roundtrip_uses_existing_revision_body_and_preserves_exact_sources():
    source = SourceRef(BlobRef("example/data", "a" * 40, "source-views/x/" + "b" * 64 + "/manifest.json", "c" * 64),
                       Selection("rows", 1, 2, ("value",)))
    req = request()
    work = progress(sources=(source,), findings=(QuestionFinding("q1", "answered", "a", evidence=(0,)),))
    assert ResearchRequest.from_dict(json.loads(encoded(asdict(req)))) == req
    assert ResearchProgress.from_dict(json.loads(encoded(asdict(work)))) == work
    research = stored(req, work)
    restored = read_workpaper(research)
    assert restored == (req, work)
    ref = BlobRef("example/data", "d" * 40, "research/cases/synthetic/revisions/r1/record.json", "e" * 64)
    next_request = continue_request(research, ref)
    assert next_request.predecessor == ref and next_request.questions == req.questions
    assert next_request.mandate == req.mandate


@pytest.mark.parametrize("field,value", [("kind", "quick"), ("mandate", "silently changed"), ("sources", []), ("method", "different")])
def test_workpaper_record_disagreement_is_rejected(field, value):
    source = SourceRef(BlobRef("example/data", "a" * 40, "source-views/x/" + "b" * 64 + "/manifest.json", "c" * 64))
    doc = stored(request(), progress(sources=(source,)))
    doc["record"]["input"][field] = value
    with pytest.raises(RecordError, match="WORKPAPER_INVALID"):
        read_workpaper(doc)


@pytest.mark.parametrize("ending", ["{not-json}\n```\n", '{"schema":"first","schema":"second"}\n```\n', "{}", "[]\n```\n"])
def test_corrupt_workpaper_is_not_silently_downgraded_to_plain_report(ending):
    with pytest.raises(RecordError, match="WORKPAPER_INVALID"):
        read_workpaper({"body": "original" + WORK_MARKER + ending, "record": {"input": {}}})


def test_plain_existing_reports_remain_readable_without_invented_questions():
    plain = {"body": "A normal existing report", "record": {"input": {}}}
    assert read_workpaper(plain) is None
    with pytest.raises(RecordError, match="QUESTIONS_NOT_RECORDED"):
        continue_request(plain, BlobRef("example/data", "a" * 40, "record.json", "b" * 64))


def test_unresolved_summary_never_drops_work_to_fit_existing_record_item_limit():
    req = request(questions=tuple(ResearchQuestion(f"q{n}", f"问题{n}") for n in range(32)))
    findings = tuple(QuestionFinding(q.question_id, "open", "unknown", remaining_kind="interrupted", next_action="resume")
                     for q in req.questions)
    work = progress(findings=findings, limitations=tuple(f"limitation-{n}" for n in range(64)), stop="blocked")
    work.validate_for(req)
    assert len(work.unresolved(req)) == 34
    assert len(read_workpaper(stored(req, work))[1].limitations) == 64
