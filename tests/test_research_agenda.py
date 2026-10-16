"""Agenda reduction and bounded reads; actual Git/host exercised in integration."""
import base64
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import research_agenda as agenda
from decision_kernel.domain.records import MissingObject, RecordError, encoded, digest
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.interfaces.agenda_commands import preflight

AT = "2026-10-16T10:00:00+08:00"
REF = {"repository": "synthetic", "commit": "a" * 40,
       "path": "research/cases/example/revisions/r1/record.json", "sha256": "1" * 64}
NEW = {**REF, "commit": "b" * 40, "path": REF["path"].replace("/r1/", "/r2/"), "sha256": "2" * 64}
WINDOW = {"variable": "经营现金流", "event": "下一份报告，仅为复核计划", "opens_at": "2026-10-17T09:00:00+08:00",
          "closes_at": "2026-10-18T09:00:00+08:00", "required_evidence": "取得原报表及对应期间",
          "end_condition": "复核后显式重开或结束；不是日程到了即发生"}


def selection(**changes):
    return {"reference": REF, "question_id": "economics", "action": "defer", "reason": "合成明确选择",
            "review": WINDOW, **changes}


def plan(**changes):
    return {"references": [REF], "as_of": AT, **changes}


def card(reference):
    return {"reference": reference, "status": "readable", "case_id": "example", "revision_id": "r1",
            "mandate": "原有限委托", "questions": [{"question_id": "economics", "text": "原问题"}],
            "recorded_unresolved": ["原缺口"], "continuation": {
                "actions": [{"question_id": "economics", "action": "analyse-available-evidence"}],
                "unresolved_challenges": [{"argument": "原未解反证"}], "declared_stop": "continue"}}


class ModelAgenda(agenda.ResearchAgenda):
    """Models only artifact reads/cards; never presented as a real store receipt."""
    def __init__(self):
        super().__init__(None)
        self.documents, self.cards, self.reads = {}, {}, []

    def _card(self, reference):
        return deepcopy(self.cards.get(encoded(reference), card(reference)))

    def _artifact(self, reference):
        self.reads.append(reference)
        try:
            return deepcopy(self.documents[encoded(reference)])
        except KeyError:
            raise MissingObject("MODEL_MISSING") from None

    def retain(self, built):
        raw = encoded(built["document"])
        artifact = ArtifactInput.from_bytes("model-" + digest(raw), "agenda.json", "application/json", raw)
        ref = {"repository": "synthetic", "commit": "f" * 40, "path": artifact.path(), "sha256": digest(raw)}
        self.documents[encoded(ref)] = {"reference": ref, "metadata": artifact.metadata(),
                                        "bytes_verified": True, "data_base64": artifact.data_base64}
        return ref


def test_read_never_creates_human_choice_or_execution():
    result = ModelAgenda().build(plan())
    assert result["document"]["state"] == [] and result["document"]["plan"]["changes"] == []
    assert result["cards"][0]["agenda"][0]["selection"] is None
    assert result["execution"] == "not-started" and result["notification"] == "not-sent"
    assert result["human_acceptance"] == "not-established-by-agenda" and result["persistence"] == "not-written"
    assert ArtifactInput.from_dict(result["artifact"]).raw() == encoded(result["document"])


def test_exact_version_defer_does_not_hide_new_revision_or_unselected_state():
    owner = ModelAgenda()
    first = owner.build(plan(changes=[selection()]))
    baseline = owner.retain(first)
    second = owner.build(plan(references=[NEW], baseline=baseline))
    assert second["cards"][0]["agenda"][0]["selection"] is None
    assert second["unselected_attention"] == first["document"]["state"]
    restored = owner.open(baseline)
    assert restored["document"] == first["document"] and restored["persistence"] == "stored-document-read"
    assert restored["cards"][0]["continuation"]["unresolved_challenges"] == [{"argument": "原未解反证"}]


@pytest.mark.parametrize("at,status", [
    ("2026-10-17T00:59:59Z", "not-due"), ("2026-10-17T01:00:00Z", "due-for-review"),
    ("2026-10-18T01:00:00Z", "due-for-review"), ("2026-10-18T01:00:01Z", "overdue-for-review"),
])
def test_review_window_inclusive_cross_timezone_not_event_proof(at, status):
    owner = ModelAgenda()
    ref = owner.retain(owner.build(plan(changes=[selection()])))
    result = owner.open(ref, as_of=at)
    row = result["cards"][0]["agenda"][0]
    assert row["review_status"] == status
    assert result["execution"] == "not-started" and result["research_assessment"] == "not-performed"


def test_question_selection_overrides_only_same_version_whole_revision():
    result = ModelAgenda().build(plan(changes=[selection(question_id=None, action="dismiss", review=None),
                                              selection(action="focus")]))
    assert result["cards"][0]["agenda"][0]["attention_status"] == "focus"


def test_reset_retains_explicit_history_without_mutating_research():
    owner = ModelAgenda()
    first = owner.build(plan(changes=[selection()]))
    baseline = owner.retain(first)
    reset = owner.build(plan(baseline=baseline, changes=[selection(action="reset", review=None)]))
    assert reset["document"]["state"] == []
    assert reset["document"]["plan"]["baseline"] == baseline and reset["document"]["plan"]["changes"]
    assert reset["cards"][0]["agenda"][0]["attention_status"] == "declared-followup"
    assert first["document"]["state"][0]["action"] == "defer"


def test_feedback_is_claim_not_changed_finding_or_accepted_decision():
    note = {"observation": "合成单位可能有错", "implication": "请求原作者更正", "outcome": "revision-requested"}
    result = ModelAgenda().build(plan(changes=[selection(action="focus", feedback=note)]))
    assert result["document"]["state"][0]["feedback"] == note
    assert result["cards"][0]["continuation"]["declared_stop"] == "continue"
    assert result["feedback_assessment"] == "submitted-claims-not-verified"


def test_missing_sibling_is_not_empty_agenda_or_discarded_selection():
    owner = ModelAgenda()
    owner.cards[encoded(NEW)] = {"reference": NEW, "status": "missing", "error": "MODEL_MISSING"}
    result = owner.build(plan(references=[REF, NEW]))
    assert result["status"] == "partial" and len(result["cards"]) == 2
    assert result["cards"][0]["status"] == "readable" and result["market_quiet"] == "not-assessed"
    with pytest.raises(RecordError, match="UNREADABLE"):
        owner.build(plan(references=[NEW], changes=[selection(reference=NEW)]))


def test_unstructured_report_preserves_unresolved_without_inventing_question_ids():
    owner = ModelAgenda()
    owner.cards[encoded(REF)] = {**card(REF), "questions": [], "continuation": None}
    result = owner.build(plan(changes=[selection(question_id=None)]))
    assert result["cards"][0]["recorded_unresolved"] == ["原缺口"]
    assert result["cards"][0]["agenda"][0]["question_id"] is None
    with pytest.raises(RecordError, match="QUESTION_NOT_IN_ORIGINAL"):
        owner.build(plan(changes=[selection()]))


@pytest.mark.parametrize("change", [
    {"references": [REF, REF]}, {"references": [REF] * 9}, {"references": "case"},
    {"as_of": "2026-10-16"}, {"as_of": "2026-10-16T10:00:00"}, {"as_of": None},
    {"changes": [selection(), selection()]}, {"changes": [selection()] * 33},
    {"baseline": {}}, {"baseline": "main"}, {"token": "not-allowed"},
])
def test_plan_rejects_invalid_or_unbounded_inputs(change):
    with pytest.raises(RecordError):
        agenda.plan_input(plan(**change))


@pytest.mark.parametrize("change", [
    {"action": "accept"}, {"action": "start-full"}, {"action": True}, {"question_id": "../q"},
    {"reference": NEW}, {"review": {**WINDOW, "closes_at": AT}},
    {"review": {**WINDOW, "variable": "", "event": ""}},
    {"review": {**WINDOW, "required_evidence": ""}},
    {"action": "dismiss"}, {"reason": ""}, {"role": "human"},
    {"feedback": {"observation": "a", "implication": "b", "outcome": "accepted"}},
])
def test_choice_has_no_permission_role_decision_or_foreign_scope(change):
    with pytest.raises(RecordError):
        agenda.plan_input(plan(changes=[selection(**change)]))


def test_missing_baseline_cannot_silently_reset_state():
    owner = ModelAgenda()
    ref = owner.retain(owner.build(plan(changes=[selection()])))
    del owner.documents[encoded(ref)]
    with pytest.raises(MissingObject):
        owner.build(plan(baseline=ref))


def test_tampered_state_even_with_new_content_bytes_is_rejected():
    owner = ModelAgenda()
    forged = owner.build(plan(changes=[selection()]))
    forged["document"]["state"][0]["action"] = "focus"
    ref = owner.retain(forged)
    with pytest.raises(RecordError, match="TRANSITION_MISMATCH"):
        owner.open(ref)


def test_reopen_checks_question_identity_again_not_just_document_hash():
    owner = ModelAgenda()
    forged = owner.build(plan(changes=[selection()]))
    forged["document"]["state"][0]["question_id"] = "invented"
    forged["document"]["plan"]["changes"][0]["question_id"] = "invented"
    with pytest.raises(RecordError, match="SAVED_QUESTION"):
        owner.open(owner.retain(forged))


def test_actual_predecessor_only_no_unbounded_history_walk():
    owner = ModelAgenda()
    ref = None
    for _ in range(6):
        ref = owner.retain(owner.build(plan(baseline=ref)))
    owner.reads.clear()
    result = owner.open(ref)
    assert len(owner.reads) == 2 and result["history_verification"] == "current-step-only"


def test_open_clock_cannot_run_backwards():
    owner = ModelAgenda()
    ref = owner.retain(owner.build(plan()))
    with pytest.raises(RecordError, match="READ_BEFORE"):
        owner.open(ref, as_of="2026-10-16T01:59:59Z")
    with pytest.raises(RecordError, match="BASELINE_AFTER"):
        owner.build(plan(baseline=ref, as_of="2026-10-15T00:00:00Z"))


def test_empty_selection_and_document_limit_are_explicit(monkeypatch):
    owner = ModelAgenda()
    result = owner.build(plan(references=[]))
    assert result["status"] == "no-selected-research" and result["market_quiet"] == "not-assessed"
    monkeypatch.setattr(agenda, "MAX_DOCUMENT_BYTES", 10)
    with pytest.raises(RecordError, match="DOCUMENT_BOUND"):
        owner.build(plan())


@pytest.mark.parametrize("consent", [False, "true", 1, None])
def test_public_artifacts_need_independent_actual_boolean_feedback_consent(consent):
    with pytest.raises(RecordError, match="CONSENT|FEEDBACK"):
        preflight("agenda", {"plan": plan(changes=[selection()]), "public_feedback_custody_confirmed": consent})


def test_public_read_only_plan_without_new_feedback_is_allowed():
    preflight("agenda", {"plan": plan()})
    preflight("agenda", {"plan": plan(changes=[selection()]), "public_feedback_custody_confirmed": True})
    with pytest.raises(RecordError, match="FUTURE"):
        preflight("agenda", {"plan": plan(as_of="9999-01-01T00:00:00Z")})


def test_missing_research_does_not_trap_existing_selection_forever():
    owner = ModelAgenda()
    ref = owner.retain(owner.build(plan(changes=[selection()])))
    owner.cards[encoded(REF)] = {"reference": REF, "status": "unavailable", "error": "MODEL_DENIED"}
    result = owner.build(plan(baseline=ref, changes=[selection(action="reset", review=None)]))
    assert result["document"]["state"] == [] and result["status"] == "partial"
    assert result["execution"] == "not-started"


def test_display_bound_preserves_reference_instead_of_truncating_research(monkeypatch):
    monkeypatch.setattr(agenda, "MAX_VIEW_BYTES", 100)
    result = ModelAgenda().build(plan())
    assert result["status"] == "partial"
    assert result["cards"] == [{"reference": REF, "status": "not-included",
                                "error": "AGENDA_VIEW_BOUND_USE_EXACT_RESEARCH_READER"}]


def test_accumulated_state_bound_never_silently_drops_old_selections():
    previous = [{**agenda.choice(selection(question_id=f"q{i}")), "recorded_at": AT} for i in range(128)]
    with pytest.raises(RecordError, match="STATE_BOUND"):
        agenda.reduce_state(previous, agenda.plan_input(plan(changes=[selection()])))
