"""Method identity and continuation behavior; not economic-quality certification."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.research_continuation import continuation_plan
from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, digest, encoded
from decision_kernel.domain.research_work import (
    QuestionFinding, ResearchChallenge, ResearchProgress, ResearchQuestion, ResearchRequest,
    render_workpaper,
)
from decision_kernel.methods.research import main, research_method
from decision_kernel.methods.research_v1 import research_method as legacy_method

LEGACY = {"quick": "c2186dad0da13b8ea6fac3195640991c4b2fbf5c3dc284655dd39d176d45b764",
          "full": "1626dd595adfbc731c674080d54fca04072e0b99b843d0c5e37d3a276f4a0138"}


def request(*ids):
    return ResearchRequest("sample", "full", "合成研究范围", tuple(ResearchQuestion(i, "问题" + i) for i in ids))


def context(req, progress):
    method = legacy_method(req.kind)
    payload = {"case_id": req.case_id, "kind": req.kind, "mandate": req.mandate,
               "predecessor": asdict(req.predecessor) if req.predecessor else None,
               "sources": [asdict(s) for s in progress.sources],
               "method": method["id"] + "@sha256:" + method["sha256"]}
    value = {"research": {"body": render_workpaper(req, progress, method), "record": {"input": payload}},
             "source_page": {"total": len(progress.sources), "offset": 0, "returned": 0,
                             "remaining": len(progress.sources), "items": []}, "next_cursor": None}
    return json.loads(encoded(value))


@pytest.mark.parametrize("kind", ["quick", "full"])
def test_historical_method_bytes_are_preserved_and_can_be_selected(kind):
    old = research_method(kind, sha256=LEGACY[kind])
    assert old == legacy_method(kind) and old["sha256"] == LEGACY[kind]
    assert digest(encoded({k: v for k, v in old.items() if k != "sha256"})) == LEGACY[kind]
    old["steps"].clear()
    assert research_method(kind, sha256=LEGACY[kind])["steps"]


def test_full_upgrade_does_not_change_quick_or_omit_adopted_depth_disciplines():
    full = research_method("full")
    assert full["id"] == "decision-os-research-full-v2" and full["sha256"] != LEGACY["full"]
    assert research_method("quick")["sha256"] == LEGACY["quick"]
    assert {"execution-loop", "economic-architecture", "financial-diagnostics", "contract-and-project-economics",
            "owner-cash-and-capital", "competition-and-reference-class", "substantive-challenge",
            "valuation-and-reverse-closure", "stop-and-delivery"} <= {s["topic"] for s in full["steps"]}
    assert full["assessment"] == "guidance-only-not-economic-certification"
    assert full["execution_owner"] == "authorized-researcher-in-current-session-not-actions-io"
    assert digest(encoded({k: v for k, v in full.items() if k != "sha256"})) == full["sha256"]


@pytest.mark.parametrize("value", ["", "latest", "0" * 64, [], 1, LEGACY["quick"]])
def test_unknown_or_foreign_method_never_silently_selects_latest(value):
    with pytest.raises(RecordError):
        research_method("full", sha256=value)


def test_installed_method_reader_needs_no_store_credentials_or_network(capsys):
    assert main(["--kind", "full"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "decision-os-research-full-v2"
    assert main(["--kind", "full", "--sha256", LEGACY["full"]]) == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == LEGACY["full"]
    assert main(["--kind", "full", "--sha256", "0" * 64]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "UNKNOWN_RESEARCH_METHOD"


def test_new_full_lists_original_questions_without_inventing_prior_work():
    plan = continuation_plan(request("q1", "q2"), None)
    assert [r["question_id"] for r in plan["actions"]] == ["q1", "q2"]
    assert all(r["finding"] is None and r["material"] is None for r in plan["actions"])
    assert plan["execution"] == "not-started" and plan["research_assessment"] == "not-performed"
    assert not plan["retained_findings"] and plan["source_coverage"] is None


@pytest.mark.parametrize("kind,action", [
    ("acquired-unanalysed", "analyse-available-evidence"),
    ("disclosed-unverified", "retrieve-and-verify"),
    ("currently-unidentifiable", "review-search-limit-and-bound-conclusion"),
    ("future-event", "observe-only-after-evidence-arrives"),
    ("interrupted", "verify-actual-interruption-and-remaining-authority"),
])
def test_each_remaining_nature_keeps_its_actual_disposition(kind, action):
    req = request("q1")
    progress = ResearchProgress("合成部分结果", (QuestionFinding("q1", "open", "未完成", remaining_kind=kind,
                                next_action="实际下一行动"),), "blocked", "作者声明中断", limitations=("仅为合成",))
    value = context(req, progress)
    before = deepcopy(value)
    result = continuation_plan(req, value)
    assert result["actions"][0]["action"] == action and result["actions"][0]["next_action"] == "实际下一行动"
    assert value == before and result["permission"] == "not-granted-by-worklist"
    expected_flag = kind in ("acquired-unanalysed", "disclosed-unverified")
    assert ("BLOCKED_LABEL_DOES_NOT_ESTABLISH_RESEARCH_EXHAUSTION" in result["review_flags"]) == expected_flag


def test_retains_answered_work_and_exposes_unresolved_challenges_without_new_registry():
    req = request("done", "open")
    progress = ResearchProgress("合成", (QuestionFinding("done", "answered", "原分析"),
        QuestionFinding("open", "open", "未验证", remaining_kind="disclosed-unverified", next_action="核原始附注")),
        "continue", "继续", challenges=(ResearchChallenge("done", "可能重复计价", "unresolved", "待核算"),),
        limitations=("合成",))
    plan = continuation_plan(req, context(req, progress))
    assert [a["question_id"] for a in plan["actions"]] == ["open"]
    assert plan["retained_findings"][0]["finding"]["finding"] == "原分析"
    assert plan["unresolved_challenges"][0]["argument"] == "可能重复计价"
    added = replace(req, questions=(*req.questions, ResearchQuestion("new", "新增问题")))
    assert continuation_plan(added, context(req, progress))["actions"][1]["finding"] is None


def test_material_work_not_silenced_by_blocked_label_or_minor_future_gap():
    req = request("future", "cash", "minor")
    progress = ResearchProgress("项目公司合成例，不是真实发行人", (
        QuestionFinding("future", "open", "未来效果", remaining_kind="future-event", next_action="等实际事件"),
        QuestionFinding("cash", "open", "附注已取得但没计算", remaining_kind="acquired-unanalysed", next_action="核现金与投入"),
        QuestionFinding("minor", "open", "次要未知", remaining_kind="disclosed-unverified", next_action="按需核验", material=False)),
        "blocked", "未授权公开保存", limitations=("公开保存与研究执行权限分开",))
    result = continuation_plan(req, context(req, progress))
    assert [r["question_id"] for r in result["actions"]] == ["cash", "future", "minor"]
    assert result["declared_stop_reason"] == "未授权公开保存"
    assert result["review_flags"] == ["BLOCKED_LABEL_DOES_NOT_ESTABLISH_RESEARCH_EXHAUSTION"]
    assert result["research_assessment"] == "not-performed"


def test_concluded_declaration_is_not_an_automatic_quality_pass():
    req = request("q1")
    progress = ResearchProgress("合成", (QuestionFinding("q1", "answered", "作者声称回答"),), "concluded", "作者声明",
                                limitations=("仅合成",), challenge_note="自查说明不是真实经济验收")
    result = continuation_plan(req, context(req, progress))
    assert result["research_assessment"] == "not-performed"
    assert result["review_flags"] == ["DECLARED_CONCLUSION_REQUIRES_SUBSTANTIVE_REVIEW"]


def test_partial_source_page_preserves_cursor_and_all_work_items():
    req = request("q1")
    refs = tuple(SourceRef(BlobRef("synthetic", "a" * 40, "source-views/s" + str(i) + "/" + "a" * 64 + "/manifest.json", "b" * 64)) for i in range(2))
    progress = ResearchProgress("合成", (QuestionFinding("q1", "open", "未核", (1,), "disclosed-unverified", "核第二份"),),
                                "continue", "继续", sources=refs)
    value = context(req, progress)
    value["source_page"].update(returned=1, remaining=1, items=[{"index": 0, "status": "external-only",
                              "reference": asdict(refs[0]), "view": None, "error": None}])
    value["next_cursor"] = {"exact": "synthetic-cursor-not-executed"}
    plan = continuation_plan(req, value)
    assert plan["source_coverage"]["remaining"] == 1 and plan["source_coverage"]["next_cursor"] == value["next_cursor"]
    assert plan["actions"][0]["evidence"] == [1]
    assert plan["source_coverage"]["limitations"][0]["status"] == "external-only"


@pytest.mark.parametrize("status", ["missing", "unavailable", "invalid", "not-included", "external-only"])
def test_local_source_limit_never_becomes_global_research_failure(status):
    req = request("q1")
    value = {"research": {"body": "plain existing report"},
             "source_page": {"total": 1, "offset": 0, "returned": 1, "remaining": 0,
                             "items": [{"index": 0, "status": status, "reference": {"id": "not-followed"}, "view": None}]},
             "next_cursor": None}
    plan = continuation_plan(req, value)
    assert plan["actions"][0]["action"] == "investigate-original-question"
    assert plan["source_coverage"]["limitations"][0]["status"] == status
    assert "NO_STRUCTURED_PRIOR_FINDINGS_NO_INFERENCE" in plan["review_flags"]


def test_source_and_next_action_text_never_execute(tmp_path):
    req = request("q1")
    target = tmp_path / "never-created"
    text = "__import__('pathlib').Path(" + repr(str(target)) + ").touch()"
    progress = ResearchProgress("合成", (QuestionFinding("q1", "open", "不可信文本", remaining_kind="interrupted", next_action=text),),
                                "blocked", "仅数据", limitations=("合成",))
    result = continuation_plan(req, context(req, progress))
    assert result["actions"][0]["next_action"] == text and not target.exists()
