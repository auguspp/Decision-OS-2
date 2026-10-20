"""Condition evidence is adopted in the existing Brief body, never by proximity."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import brief_delivery as brief, brief_research, news_delivery as news, research_agenda
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.research_agenda import ResearchAgenda
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_evidence import export_required
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, Selection, SourceInput, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_brief_workpaper import structured_revision
from test_conditional_odds import authorized
from test_news_delivery import END, index_ref, plan, retain, store
from test_odds_watch import case, request
from test_watch_agenda import AS_OF, agenda_plan, check, retain_agenda, selected
from test_watch_conditions import evidence_condition, evidence_entry, watch_v2, v2_input


def setup_brief(case, *, choices=None, watch_ref=None, name="agenda"):
    store, research, _, _, _ = case
    ref = watch_ref or check(case)
    p = agenda_plan(research, [selected(research, ref)])
    if choices is not None:
        p["changes"] = choices
    agenda = retain_agenda(store, ResearchAgenda(store).build(p), name)
    item = {"reference": asdict(research), "start": 1, "end": 1}
    return plan(index_ref(store), research={"items": [item], "agenda": agenda}), ref


def build(store, p):
    return brief.build_edition(store.read_source, ResearchArtifacts(store), p)[0]


def test_retained_condition_is_in_same_body_and_reopens_after_research_update(case, tmp_path):
    store, research, _, _, _ = case
    p, ref = setup_brief(case)
    doc = build(store, p)
    section = doc["research"]
    assert doc["schema"] == brief.SCHEMA
    assert section["watch_evidence_adoption"][0]["status"] == "included"
    assert ref["path"] in doc["brief_markdown"] and "首次满足" in doc["brief_markdown"]
    assert "Synthetic research" in doc["brief_markdown"] and "没有执行新检查" in doc["brief_markdown"]
    assert section["rows"][0]["context"]["agenda"][0]["selection"] is None
    saved = retain(store, doc)
    store.submit_revision("r2", RevisionInput("case", "r2", "full", "Changed research", "Synthetic", "synthetic",
                                             predecessor=research, unresolved=("Not adopted automatically",)))
    fresh = VersionedStore(GitStore(tmp_path / "watch.git"))
    head = fresh.backend.head()
    restored, _ = brief.open_edition(fresh.read_source, ResearchArtifacts(fresh), saved)
    assert encoded(restored) == encoded(doc) and fresh.backend.head() == head


def test_new_explicit_check_can_revisit_same_research_without_inventing_focus(case):
    store, research, _, _, _ = case
    p, first_ref = setup_brief(case)
    first = build(store, p); previous = retain(store, first)
    repeated = build(store, {**p, "baseline": asdict(previous)})
    assert repeated["research"]["included"] == []
    assert repeated["research"]["watch_evidence_adoption"][0]["status"] == "not-included"
    second_ref = check(case, at="2026-01-03T04:01:00Z", price=None, previous=first_ref, key="second-check")
    new_plan = agenda_plan(research, [selected(research, second_ref)], baseline=p["research"]["agenda"])
    new_agenda = retain_agenda(store, ResearchAgenda(store).build(new_plan), "second-agenda")
    new_p = deepcopy(p); new_p["baseline"] = asdict(previous); new_p["research"]["agenda"] = new_agenda
    second = build(store, new_p)
    adopted = second["research"]["included"][0]
    assert adopted["classification"] == "repeat-with-explicit-watch-evidence"
    assert adopted["selection_reason"] == "explicit-watch-evidence-not-in-previous-brief"
    assert "无法判断" in second["brief_markdown"]
    assert second["research"]["rows"][0]["context"]["agenda"][0]["selection"] is None
    assert second["research"]["watch_evidence_adoption"][0]["status"] == "included"
    next_saved = retain(store, second, "second-edition")
    again = build(store, {**new_p, "baseline": asdict(next_saved)})
    assert again["research"]["included"] == []


@pytest.mark.parametrize("action", ["defer", "dismiss"])
def test_saved_attention_still_controls_condition_adoption(case, action):
    store, research, _, _, _ = case
    choice = {"reference": asdict(research), "question_id": None, "action": action,
              "reason": "Synthetic explicit attention, not inferred from trigger"}
    p, _ = setup_brief(case, choices=[choice])
    doc = build(store, p)
    assert doc["research"]["included"] == []
    adoption = doc["research"]["watch_evidence_adoption"][0]
    assert adoption["status"] == "not-included" and adoption["reason"] == "research-explicitly-" + (
        "deferred" if action == "defer" else "dismissed")
    assert "首次满足" not in doc["brief_markdown"]
    assert doc["research"]["rows"][0]["context"]["agenda"][0]["selection"]["action"] == action


@pytest.mark.parametrize("limit", ["brief", "agenda"])
def test_context_capacity_keeps_exact_omission_and_export_requirement(case, monkeypatch, limit):
    store, _, _, _, _ = case
    p, ref = setup_brief(case)
    monkeypatch.setattr(brief_research if limit == "brief" else research_agenda,
                        "MAX_ROW_BYTES" if limit == "brief" else "MAX_CARD_BYTES", 1)
    doc = build(store, p)
    section = doc["research"]
    assert section["included"] == [] and section["rows"][0]["status"] == "not-included"
    assert "context" not in section["rows"][0]
    assert section["rows"][0]["watch_evidence"][0]["selection"]["check_ref"] == ref
    assert section["watch_evidence_adoption"][0]["status"] == "not-included"
    assert export_required(section)


def test_missing_check_is_not_adopted_or_converted_to_not_met(case):
    store, research, _, _, _ = case
    original = check(case)
    missing = {**original, "path": original["path"].replace("odds-watch-check-", "missing-")}
    p, _ = setup_brief(case, watch_ref=missing)
    doc = build(store, p)
    assert len(doc["research"]["included"]) == 1  # Independent original research remains useful.
    assert doc["research"]["watch_evidence_adoption"][0]["status"] == "not-included"
    assert "AGENDA_WATCH_EVIDENCE_MISSING" in doc["brief_markdown"]
    assert "首次满足" not in doc["brief_markdown"] and "条件判定：未满足" not in doc["brief_markdown"]


def test_resealed_adopted_condition_cannot_forge_previous_brief(case):
    store, _, _, _, _ = case
    p, _ = setup_brief(case)
    doc = build(store, p)
    doc["research"]["rows"][0]["context"]["watch_evidence"][0]["result"]["status"] = "not-met"
    doc["brief_markdown"] = brief.brief_text(doc)
    news._seal(doc)
    prior = retain(store, doc)
    with pytest.raises(RecordError, match="ORIGINAL_MISMATCH"):
        build(store, {**p, "baseline": asdict(prior)})


@pytest.mark.parametrize("source_at_snapshot", [True, False])
def test_economic_evidence_links_real_original_question_and_keeps_assessment_limits(store, monkeypatch, source_at_snapshot):
    item = structured_revision(store, monkeypatch)
    reference = item["reference"]
    service = OddsWatch(store)
    payload = watch_v2(reference, "research", [evidence_condition()])
    payload["activation"]["as_of"] = payload["window"]["starts_at"] = "2026-10-16T10:30:00Z"
    watch = service.freeze(authorized("watch", payload), submission_id="operating-watch")["reference"]
    snapshot = store.backend.head()
    title, reason = '<script>issuer</script> [plant](synthetic:plant)', '<b>Only the named plant</b>; [claim](synthetic:claim)'
    source = store.put_source("operating-source", SourceInput(
        "operating-source", "content", title, "synthetic:operating", "Synthetic delivery disclosure.\nRemaining uncertainty.\n",
        acquired_at="2026-10-16T09:30:00Z", published_at="2026-10-16T09:00:00Z",
        limitations=("Synthetic excerpt; original not independently verified",)))
    source = SourceRef(source.manifest, Selection("lines", 1, 1))
    evidence = evidence_entry(source, observed_at="2026-10-16T10:40:00Z",
                              assessed_at="2026-10-16T10:45:00Z", reason=reason)
    command = request(store, watch, at="2026-10-16T11:00:00Z", price=v2_input(evidence=[evidence]))
    if not source_at_snapshot:
        command["records_snapshot"] = snapshot
    ref = service.save(authorized("check", command), submission_id="operating-check")["reference"]
    built = ResearchAgenda(store).build(agenda_plan(reference, [selected(reference, ref, question_id="q2")], as_of=END))
    assert built["cards"][0]["watch_evidence"][0]["selection"]["question_id"] == "q2"
    agenda = retain_agenda(store, built)
    doc = build(store, plan(index_ref(store), research={"items": [item], "agenda": agenda}))
    row = doc["research"]["rows"][0]["context"]["watch_evidence"][0]
    assert row["result"]["condition_results"][0]["condition_met"] is (True if source_at_snapshot else None)
    assert row["relation"] == "explicit-question-link-not-an-independent-economic-assessment"
    assert "q2" in doc["brief_markdown"] and "operating" in doc["brief_markdown"]
    body = doc["brief_markdown"]
    assert news._escape_md(reason) in body
    assert title not in body and reason not in body
    assert evidence["observed_at"] in body and evidence["assessed_at"] in body
    assert source.manifest.sha256 in body and source.manifest.commit in body
    assert '"kind":"lines"' in body and '"start":1' in body and '"end":1' in body
    if source_at_snapshot:
        assert news._escape_md(title) in body
        assert "2026-10-16T09:30:00Z" in body and "2026-10-16T09:00:00Z" in body
        assert "2026-10-16T10:00:00Z" in body and "original not independently verified" in body
    else:
        assert news._escape_md(title) not in body and "未读取来源正文" in body
        assert "not-at-records-snapshot" in body and "条件判定：未知" in body
        assert news._escape_md('"body_read":false') in body and "条件判定：未满足" not in body
    assert "不证明原件真实性或证据足以支持判断" in body
    assert doc["research"]["watch_evidence_adoption"][0]["status"] == "included"
    assert doc["research"]["research_executed"] is False
    saved = retain(store, doc)
    assert brief.open_edition(store.read_source, ResearchArtifacts(store), saved)[0] == doc


@pytest.mark.parametrize("action", ["defer", "dismiss"])
def test_original_question_choice_blocks_only_its_selected_evidence(store, monkeypatch, action):
    item = structured_revision(store, monkeypatch)
    reference = item["reference"]
    initial_plan = plan(index_ref(store), research={"items": [item]})
    prior = retain(store, build(store, initial_plan))
    service = OddsWatch(store)
    payload = watch_v2(reference, "research", [evidence_condition()])
    payload["activation"]["as_of"] = payload["window"]["starts_at"] = "2026-10-16T10:30:00Z"
    watch = service.freeze(authorized("watch", payload), submission_id="question-watch")["reference"]
    ref = service.save(authorized("check", request(store, watch, at="2026-10-16T11:00:00Z",
                     price=v2_input())), submission_id="question-check")["reference"]
    p = agenda_plan(reference, [selected(reference, ref, question_id="q2")], as_of=END,
                    changes=[{"reference": reference, "question_id": "q2", "action": action,
                              "reason": "Original question choice"}])
    agenda = retain_agenda(store, ResearchAgenda(store).build(p))
    chosen_plan = {**initial_plan, "research": {"items": [item], "agenda": agenda}}
    first = build(store, chosen_plan)
    assert first["research"]["included"]  # Another original question remains open.
    receipt = first["research"]["watch_evidence_adoption"][0]
    assert receipt["status"] == "not-included"
    assert receipt["reason"] == "question-explicitly-" + ("deferred" if action == "defer" else "dismissed")
    assert "原检查结果：" not in first["brief_markdown"]
    repeated = build(store, {**chosen_plan, "baseline": asdict(prior)})
    assert repeated["research"]["included"] == []  # The new check cannot bypass its question's choice.
    assert repeated["research"]["watch_evidence_adoption"][0]["status"] == "not-included"


def test_legacy_without_watch_has_no_new_fields_or_policy(case):
    store, research, _, _, _ = case
    p = plan(index_ref(store), research={"items": [{"reference": asdict(research), "start": 1, "end": 1}]})
    doc = build(store, p)
    assert "watch_evidence_adoption" not in doc["research"]
    assert "watch_included" not in doc["research"]["baseline"]
    assert "watch_evidence" not in doc["research"]["rows"][0]["context"]
    assert doc["research"]["policy"] == brief_research.POLICY
    saved = retain(store, doc)
    assert brief.open_edition(store.read_source, ResearchArtifacts(store), saved)[0] == doc
