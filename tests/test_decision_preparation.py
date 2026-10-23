"""Real Git preparation, exact evidence/history and time-vs-outcome boundaries."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime

import pytest

from decision_kernel.application import decision_preparation as module
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.decision_preparation import DecisionPreparation, preparation_artifact, verify_fresh
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.decision_preparation import project_horizons, validate_preparation
from decision_kernel.domain.records import (
    BlobRef, Conflict, MissingObject, RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded,
)
from decision_kernel.domain.research_work import (
    QuestionFinding, ResearchChallenge, ResearchProgress, ResearchQuestion, ResearchRequest, render_workpaper,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import decision_preparation_commands as commands
from decision_kernel.interfaces.reading_decision_page import JS
from test_conditional_odds import authorized as odds_authorized, price_payload, spec_payload

PREPARED = "2026-01-10T08:00:00+00:00"
LATER = "2026-04-20T08:00:00+00:00"
NOW = datetime.fromisoformat("2026-10-19T12:00:00+00:00")


def horizon(horizon_id="near", **changes):
    return {"horizon_id": horizon_id, "label": "Near-term revenue mechanism", "question_ids": ["q1"],
            "mechanism": "Orders must become accepted deliveries to become revenue.",
            "expected_change": "Compare delivered volume and cash receipts with the same reporting scope.",
            "source_indices": [0], "window_start": "2026-01-01T00:00:00+00:00",
            "window_end": "2026-03-31T23:59:59+00:00", "review_at": "2026-04-15T00:00:00+00:00",
            "catalysts": [{"event_id": "release", "description": "Expected issuer disclosure, date is a hypothesis",
                           "expected_at": "2026-04-10T00:00:00+00:00", "source_indices": [0]}],
            "invalidation": [{"condition_id": "returns", "description": "Accepted deliveries are later returned",
                              "source_indices": [0]}], "limitations": ["Synthetic hypotheses, not a company recommendation"],
            **changes}


def plan(reference, **changes):
    return {"schema": "decision-preparation-v1", "preparation_id": "synthetic-preparation",
            "research_ref": asdict(reference), "prepared_at": PREPARED, "predecessor": None,
            "question_ids": ["q1"], "stance": "awaiting-evidence", "rationale": "Economics still needs confirmation.",
            "source_indices": [0], "horizons": [horizon(), horizon("long", label="Capital discipline", window_end="2027-12-31T23:59:59+00:00",
                review_at="2026-12-01T00:00:00+00:00", catalysts=[])],
            "target": {"security": {"code": "TEST.SZ", "share_class": "A"}, "expression": "Ordinary shares; no position size or execution"},
            "alternatives": [
                {"alternative_id": "wait", "choice": "wait", "description": "Wait for cash disclosure",
                 "case_for": "Avoid assuming cash conversion", "case_against": "Evidence may arrive after price moves", "unknowns": ["Disclosure detail"]},
                {"alternative_id": "research", "choice": "continue-research", "description": "Investigate contract terms",
                 "case_for": "Clarify the economic mechanism", "case_against": "Relevant contracts may remain undisclosed", "unknowns": ["Contract access"]}],
            "human_questions": ["Which uncertainty would justify further research?"], "data_gaps": [],
            "major_assumptions": ["Reported deliveries eventually generate collectible cash"],
            "strongest_counterarguments": ["Receivable balances may mask customer financing"],
            "analysis_context": {"valuation_ref": None, "odds": None},
            "revision_note": "Initial synthetic engineering draft", "limitations": ["No human investment decision"], **changes}


def authorized(value):
    return {"preparation": value, "rights_confirmed": True, "rights_basis": "Synthetic engineering fixture"}


def save_revision(store, source, revision_id="r1", predecessor=None, *, case_id="case", **changes):
    request = ResearchRequest(case_id, "full", "Synthetic research mandate",
                              (ResearchQuestion("q1", "How does delivery convert to cash?"),
                               ResearchQuestion("q2", "What could invalidate the thesis?")), predecessor)
    progress = ResearchProgress("Synthetic research with unresolved operating evidence.", (
        QuestionFinding("q1", "answered", "Revenue is not cash collection.", (0,)),
        QuestionFinding("q2", "open", "Contract terms missing", (0,), "currently-unidentifiable", "Check next disclosure")),
        "blocked", "Missing evidence", (source,),
        (ResearchChallenge("q1", "Cash may lag revenue", "unresolved", "Need a matching disclosure", (0,)),),
        ("Synthetic only",))
    progress = replace(progress, **changes)
    method = {"id": "synthetic-method", "sha256": "a" * 64}
    return store.submit_revision("research-" + case_id + "-" + revision_id, RevisionInput(
        case_id, revision_id, "full", render_workpaper(request, progress, method), request.mandate,
        method["id"] + "@sha256:" + method["sha256"], (source,), predecessor, progress.unresolved(request)))


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-01T08:00:00+00:00")
    backend = GitStore.init_synthetic(tmp_path / "decision.git")
    store = VersionedStore(backend)
    source = store.put_source("source", SourceInput("s", "content", "Synthetic disclosure", "synthetic:decision", "Delivery facts"))
    ref = save_revision(store, source)
    return store, source, ref


def service(store):
    return DecisionPreparation(store, clock=lambda: NOW)


def test_preparation_reads_original_questions_challenges_and_source_without_writing(context):
    store, source, ref = context
    before = store.backend.head()
    result = service(store).prepare(plan(ref))
    doc = result["document"]
    assert doc["research"]["questions"][0]["text"] == "How does delivery convert to cash?"
    assert doc["research"]["challenges"][0]["disposition"] == "unresolved"
    assert doc["research"]["unselected_question_ids"] == ["q2"]
    assert encoded(doc["evidence"]["items"][0]["reference"]) == encoded(asdict(source))
    assert doc["evidence"]["items"][0]["availability"] == "stored-representation"
    assert doc["status"] == "partial" and doc["formal_decision"] == "not-created"
    assert doc["human_confirmation"] == "not-established" and doc["investment_authority"] == "none"
    assert result["persistence"] == "not-written" and store.backend.head() == before


def test_preparation_keeps_target_alternatives_human_questions_and_explicit_missing_context(context):
    store, _, ref = context
    value = plan(ref, target={"security": None, "expression": "Research the business before selecting a security"},
                 data_gaps=["Contract terms remain unavailable"])
    doc = service(store).prepare(value)["document"]
    assert doc["input"]["target"] == value["target"]
    assert doc["input"]["alternatives"] == value["alternatives"]
    assert doc["input"]["human_questions"] == value["human_questions"]
    assert doc["input"]["major_assumptions"] == value["major_assumptions"]
    assert doc["input"]["strongest_counterarguments"] == value["strongest_counterarguments"]
    assert doc["analysis_context"]["valuation"] is doc["analysis_context"]["odds"] is None
    assert doc["analysis_context"]["price_context"] is None
    assert doc["analysis_context"]["target_binding"] == "author-selected"
    assert doc["data_gaps"] == ["Contract terms remain unavailable", "valuation-not-established",
                                "odds-not-established", "price-context-not-established", "target-security-not-established"]
    assert doc["status"] == "partial"


@pytest.mark.parametrize("changes,code", [
    ({"alternatives": []}, "ALTERNATIVES_REQUIRED"),
    ({"human_questions": []}, "HUMAN_QUESTIONS_REQUIRED"),
    ({"major_assumptions": []}, "MAJOR_ASSUMPTIONS_REQUIRED"),
    ({"strongest_counterarguments": []}, "STRONGEST_COUNTERARGUMENTS_REQUIRED"),
    ({"target": {"security": None, "expression": ""}}, "INVALID_TEXT"),
])
def test_preparation_requires_substantive_comparison_and_unresolved_human_items(context, changes, code):
    store, _, ref = context
    with pytest.raises(RecordError, match=code):
        service(store).prepare(plan(ref, **changes))
    value = plan(ref)
    value["alternatives"][1]["choice"] = value["alternatives"][0]["choice"]
    with pytest.raises(RecordError, match="DISTINCT_ALTERNATIVES"):
        service(store).prepare(value)


def existing_conditional(store, ref, *, price=None):
    odds = ConditionalOdds(store, allow_declared_only=True, clock=lambda: NOW)
    frozen = odds.freeze(odds_authorized("spec", spec_payload(asdict(ref))), submission_id="existing-spec")
    saved = odds.save(odds_authorized("calculation", {"spec_ref": frozen["reference"],
                     "price": price or price_payload()}), submission_id="existing-odds")
    return frozen, saved, odds.read(saved["reference"])["result"]


def test_original_conditional_artifacts_replay_with_dated_price_and_unqualified_market(context):
    store, _, ref = context
    frozen, odds, original = existing_conditional(store, ref)
    value = plan(ref, analysis_context={"valuation_ref": frozen["reference"],
                                       "odds": {"kind": "conditional", "reference": odds["reference"]}})
    head = store.backend.head()
    doc = service(store).prepare(value)["document"]
    analysis = doc["analysis_context"]
    assert encoded(analysis["odds"]["result"]) == encoded(original)
    assert analysis["valuation"]["reference"] == frozen["reference"]
    assert analysis["price_context"]["price"] == price_payload()
    assert analysis["price_context"]["market_qualification"] == "not-established"
    assert analysis["price_context"]["probability_status"] == "not-established"
    assert analysis["missing"] == ["qualified-market-not-established"]
    assert analysis["quotation_freshness_at_preparation"] == "not-requalified"
    assert analysis["source_requests"] == 0 and store.backend.head() == head
    assert doc["status"] == "partial" and doc["investment_authority"] == "none"
    saved = service(store).save(authorized(value), submission_id="preparation-with-odds")
    fresh = VersionedStore(GitStore(store.backend.root))
    assert service(fresh).read(saved["reference"], at=LATER)["document"] == doc
    assert store.current("case") == ref


def test_valuation_only_context_inherits_its_original_source_indices(context):
    store, _, ref = context
    frozen, _, _ = existing_conditional(store, ref)
    value = plan(ref, analysis_context={"valuation_ref": frozen["reference"], "odds": None},
                 question_ids=[], source_indices=[], horizons=[horizon(question_ids=[], source_indices=[],
                 catalysts=[], invalidation=[{"condition_id": "missing", "description": "Missing disclosed evidence", "source_indices": []}])])
    doc = service(store).prepare(value)["document"]
    assert doc["evidence"]["cited"] == 1
    assert doc["analysis_context"]["odds"] is None
    assert doc["analysis_context"]["missing"] == ["odds-not-established", "price-context-not-established"]


def test_original_analysis_requires_exact_target_research_and_spec(context):
    store, source, ref = context
    frozen, odds, _ = existing_conditional(store, ref)
    selection = {"valuation_ref": None, "odds": {"kind": "conditional", "reference": odds["reference"]}}
    bad = plan(ref, analysis_context=selection, target={"security": {"code": "OTHER.SZ", "share_class": "A"},
                                                     "expression": "Different security"})
    with pytest.raises(RecordError, match="ANALYSIS_TARGET_MISMATCH"):
        service(store).prepare(bad)
    r2 = save_revision(store, source, "r2", ref)
    with pytest.raises(RecordError, match="ANALYSIS_RESEARCH_MISMATCH"):
        service(store).prepare(plan(r2, analysis_context=selection))
    different = spec_payload(asdict(ref), spec_id="alternative-spec")
    other = ConditionalOdds(store).freeze(odds_authorized("spec", different), submission_id="other-spec")
    with pytest.raises(RecordError, match="ANALYSIS_SPEC_MISMATCH"):
        service(store).prepare(plan(ref, analysis_context={**selection, "valuation_ref": other["reference"]}))


def test_future_original_price_cannot_support_earlier_preparation(context):
    store, _, ref = context
    _, odds, _ = existing_conditional(store, ref, price=price_payload(as_of="2026-01-11T16:00:00+08:00"))
    with pytest.raises(RecordError, match="ANALYSIS_AFTER_PREPARATION"):
        service(store).prepare(plan(ref, analysis_context={"valuation_ref": None,
                                  "odds": {"kind": "conditional", "reference": odds["reference"]}}))


def test_original_analysis_access_failure_is_not_downgraded_to_missing_context(context, monkeypatch):
    store, _, ref = context
    frozen, odds, _ = existing_conditional(store, ref)
    calls = []
    def denied(reference):
        calls.append(reference)
        raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
    monkeypatch.setattr(ResearchArtifacts, "read", lambda self, reference: denied(reference))
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        service(store).prepare(plan(ref, analysis_context={"valuation_ref": frozen["reference"],
                                  "odds": {"kind": "conditional", "reference": odds["reference"]}}))
    assert len(calls) == 1


def test_existing_probability_context_keeps_original_assessment_and_requires_matching_registry(tmp_path, monkeypatch):
    from test_market_relay_inputs import REGISTRY
    from test_probability_odds import EVALUATED, build_context
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "probability-preparation.git"))
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    store, probability, spec, assessment, market = build_context(store, fresh)
    frozen = probability.freeze(odds_authorized("assessment", assessment), submission_id="existing-assessment")
    saved = probability.save(odds_authorized("calculation", {"assessment_ref": frozen["reference"],
                             "market": market, "evaluated_at": EVALUATED}), submission_id="existing-probability")
    ref = BlobRef(**spec["research_ref"])
    value = plan(ref, question_ids=[], horizons=[horizon(question_ids=[])],
                 target={"security": spec["security"], "expression": "Synthetic ordinary shares for human consideration"},
                 analysis_context={"valuation_ref": None, "odds": {"kind": "probability", "reference": saved["reference"]}})
    preparation = DecisionPreparation(store, REGISTRY, clock=lambda: NOW)
    doc = preparation.prepare(value)["document"]
    analysis = doc["analysis_context"]
    assert analysis["valuation"]["reference"] == assessment["spec_ref"]
    assert analysis["probability_assessment"]["assessment"] == assessment
    assert analysis["odds"]["result"] == probability.read(saved["reference"])["result"]
    assert analysis["price_context"]["market_qualification"] == "qualified-retained-close-for-declared-as-of"
    assert analysis["price_context"]["probability_status"] == "explicit-research-assessment-not-empirically-certified"
    assert analysis["missing"] == [] and doc["status"] == "partial"
    assert doc["human_confirmation"] == "not-established" and doc["formal_decision"] == "not-created"
    receipt = preparation.save(authorized(value), submission_id="probability-preparation")
    assert verify_fresh(receipt, fresh, REGISTRY)["readback"]["status"] == "verified"
    with pytest.raises(RecordError, match="ODDS_MATCHING_REGISTRY_REQUIRED"):
        service(store).read(receipt["reference"], at=LATER)


def test_dates_produce_review_states_never_catalyst_or_invalidation_outcomes(context):
    store, _, ref = context
    original = plan(ref)
    first = project_horizons(original, PREPARED)
    later = project_horizons(original, LATER)
    assert first[0]["review_state"] == "not-due" and later[0]["review_state"] == "review-due"
    assert later[0]["window_state"] == "window-ended" and later[1]["window_state"] == "in-window"
    assert later[0]["catalysts"][0]["timing"] == "date-reached-needs-evidence"
    assert later[0]["catalysts"][0]["outcome"] == later[0]["invalidation"][0]["outcome"] == "not-evaluated"
    assert later[0]["mechanism_outcome"] == "not-evaluated"
    assert original == plan(ref)


def test_save_history_independent_reader_same_intent_and_current_research_stays_unchanged(context):
    store, source, ref = context
    original_body = store.read_revision(ref)["body"]
    a = service(store).save(authorized(plan(ref)), submission_id="draft1")
    updated = plan(ref, predecessor=a["reference"], prepared_at="2026-01-11T08:00:00+00:00",
                   stance="researching", rationale="Revisit cash conversion", horizons=[horizon()], revision_note="Removed long horizon from this selection")
    b = service(store).save(authorized(updated), submission_id="draft2")
    fresh = VersionedStore(GitStore(store.backend.root))
    view = service(fresh).read(b["reference"], at=LATER)
    assert len(view["history"]) == 2 and view["history"][0]["reference"] == a["reference"]
    assert view["document"]["changes"]["judgment_changed"] is True
    assert view["document"]["changes"]["removed_horizons"][0]["horizon_id"] == "long"
    assert view["history"][0]["document"]["input"]["horizons"][1]["horizon_id"] == "long"
    assert view["replay"] == "matches" and view["current_use"]["status"] == "same-revision"
    assert service(store).save(authorized(updated), submission_id="draft2") == b
    assert store.current("case") == ref and store.read_revision(ref)["body"] == original_body
    paths = store.backend.paths(store.backend.head())
    assert not any("decision" in path and "current" in path for path in paths)
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        service(store).save(authorized({**updated, "stance": "awaiting-evidence"}), submission_id="draft2")


def test_new_research_must_retain_actual_ancestor_and_old_preparation_remains_historical(context):
    store, source, ref = context
    a = service(store).save(authorized(plan(ref)), submission_id="draft1")
    r2 = save_revision(store, source, "r2", ref)
    r3 = save_revision(store, source, "r3", r2)
    b = service(store).save(authorized(plan(r3, predecessor=a["reference"], prepared_at="2026-01-12T08:00:00+00:00")), submission_id="draft2")
    view = service(store).read(b["reference"], at=LATER)
    assert view["document"]["research_lineage_to_previous_preparation"] == [asdict(r3), asdict(r2), asdict(ref)]
    old = service(store).read(a["reference"], at=LATER)
    assert old["current_use"]["status"] == "historical-research-not-current"
    assert old["document"] == view["history"][0]["document"]


def test_unrelated_case_and_reversed_research_ancestry_are_rejected(context):
    store, source, ref = context
    r2 = save_revision(store, source, "r2", ref)
    a = service(store).save(authorized(plan(r2)), submission_id="draft1")
    earlier = plan(ref, predecessor=a["reference"], prepared_at="2026-01-12T08:00:00+00:00")
    with pytest.raises(RecordError, match="RESEARCH_ANCESTRY"):
        service(store).prepare(earlier)
    other = save_revision(store, source, case_id="other")
    with pytest.raises(RecordError, match="CASE_MISMATCH"):
        service(store).prepare({**earlier, "research_ref": asdict(other)})


@pytest.mark.parametrize("change,code", [
    ({"question_ids": ["invented"], "horizons": [horizon(question_ids=["invented"])]}, "UNKNOWN_QUESTION"),
    ({"source_indices": [63]}, "SOURCE_OUTSIDE_RESEARCH"),
    ({"prepared_at": "2025-12-31T00:00:00+00:00"}, "RESEARCH_AFTER_PREPARATION"),
    ({"prepared_at": "2027-01-01T00:00:00+00:00"}, "FUTURE_TIMESTAMP"),
    ({"human_confirmation": True}, "SHAPE"),
    ({"stance": "buy"}, "STANCE"),
    ({"limitations": []}, "LIMITATIONS_REQUIRED"),
    ({"horizons": [horizon(window_end="2025-01-01T00:00:00+00:00")]}, "WINDOW_ORDER"),
    ({"horizons": [horizon(invalidation=[])]}, "INVALIDATION_REQUIRED"),
    ({"horizons": [horizon(review_at="2026-03-01")]}, "AWARE_TIME"),
    ({"horizons": [horizon(), horizon()]}, "DUPLICATE_ID"),
])
def test_unsupported_or_unfounded_inputs_cannot_gain_decision_qualification(context, change, code):
    store, _, ref = context
    with pytest.raises(RecordError, match=code):
        service(store).prepare(plan(ref, **change))


def test_inherited_finding_evidence_is_read_even_when_author_omits_citation_indices(context):
    store, _, ref = context
    value = plan(ref, source_indices=[])
    for row in value["horizons"]:
        row["source_indices"] = []
        for event in row["catalysts"] + row["invalidation"]:
            event["source_indices"] = []
    assert service(store).prepare(value)["document"]["evidence"]["cited"] == 1


def test_external_only_evidence_stays_partial_and_ordinary_research_does_not_invent_questions(context):
    store, _, _ = context
    external = store.put_source("external", SourceInput("x", "content", "External locator", "synthetic:external", None,
        custody="external", limitations=("Original not retained",)))
    ref = store.submit_revision("ordinary", RevisionInput("plain", "r1", "quick", "Ordinary text", "Original question", "method", (external,)))
    value = plan(ref, question_ids=[], horizons=[horizon(question_ids=[])])
    doc = service(store).prepare(value)["document"]
    assert doc["status"] == "partial" and doc["research"]["questions_status"] == "not-recorded"
    assert doc["research"]["questions"] == [] and doc["evidence"]["items"][0]["availability"] == "external-only"
    with pytest.raises(RecordError, match="RECORDED_QUESTIONS"):
        service(store).prepare(plan(ref))


@pytest.mark.parametrize("error", [MissingObject("missing"), StoreUnavailable("denied"), RecordError("GITHUB_OPERATION_BUDGET_EXCEEDED")])
def test_source_read_failures_abort_without_retry_or_fallback(context, monkeypatch, error):
    store, _, ref = context
    calls = []
    def fail(source):
        calls.append(source)
        raise error
    monkeypatch.setattr(store, "read_source", fail)
    with pytest.raises(type(error)) as failure:
        service(store).prepare(plan(ref))
    assert failure.value is error and len(calls) == 1


def test_self_consistent_artifact_with_forged_recomputed_history_is_rejected(context):
    store, _, ref = context
    document = service(store).prepare(plan(ref))["document"]
    document["research"]["challenges"][0]["disposition"] = "changed"
    forged = ResearchArtifacts(store).put("forged", preparation_artifact(document, "Synthetic corruption fixture"))
    with pytest.raises(RecordError, match="REPLAY_MISMATCH"):
        service(store).read(forged, at=LATER)


def test_history_bound_keeps_original_records_and_never_discards_predecessor(context, monkeypatch):
    store, _, ref = context
    monkeypatch.setattr(module, "MAX_CHAIN", 2)
    first = service(store).save(authorized(plan(ref)), submission_id="first")
    second = service(store).save(authorized(plan(ref, predecessor=first["reference"], prepared_at="2026-01-11T08:00:00+00:00")), submission_id="second")
    before = store.backend.head()
    with pytest.raises(RecordError, match="HISTORY_BOUND"):
        service(store).save(authorized(plan(ref, predecessor=second["reference"], prepared_at="2026-01-12T08:00:00+00:00")), submission_id="third")
    assert store.backend.head() == before
    assert len(service(store).read(second["reference"], at=LATER)["history"]) == 2


def test_final_access_check_and_read_bound_fail_without_partial_success(context, monkeypatch):
    store, _, ref = context
    monkeypatch.setattr(module, "MAX_READ_BYTES", 1)
    with pytest.raises(RecordError, match="READ_BOUND"):
        service(store).prepare(plan(ref))
    monkeypatch.setattr(module, "MAX_READ_BYTES", 8 * 1024 * 1024)
    def denied():
        raise StoreUnavailable("denied")
    monkeypatch.setattr(store.backend, "head", denied)
    with pytest.raises(StoreUnavailable):
        service(store).prepare(plan(ref))


def test_reader_failure_preserves_save_receipt_and_commands_have_no_confirmation_operation(context):
    store, _, ref = context
    saved = service(store).save(authorized(plan(ref)), submission_id="saved")
    def failed():
        raise StoreUnavailable("reader denied")
    result = verify_fresh(saved, failed)
    assert result["reference"] == saved["reference"] and result["readback"]["status"] == "failed"
    result = verify_fresh(saved, lambda: VersionedStore(GitStore(store.backend.root)))
    assert result["readback"]["status"] == "verified"
    read = commands.perform(store, "read-decision-preparation", {"reference": saved["reference"], "at": LATER})
    assert read["document"]["formal_decision"] == "not-created"
    with pytest.raises(RecordError, match="OPERATION"):
        commands.perform(store, "confirm-decision", {"reference": saved["reference"]})
    assert "innerHTML" not in JS and "eval(" not in JS


def test_reference_and_evaluation_time_cannot_backdate_or_follow_future_draft(context):
    store, _, ref = context
    saved = service(store).save(authorized(plan(ref)), submission_id="saved")
    with pytest.raises(RecordError, match="NOT_YET_RECORDED"):
        service(store).read(saved["reference"], at="2026-01-09T08:00:00+00:00")
    with pytest.raises(RecordError, match="FUTURE_TIMESTAMP"):
        service(store).read(saved["reference"], at="2027-01-09T08:00:00+00:00")
    with pytest.raises(RecordError):
        service(store).read(None, at=LATER)


@pytest.mark.parametrize("field,stamp", [
    ("acquired_at", "2026-01-11T08:00:00+00:00"),
    ("published_at", "2026-01-11"),
])
def test_future_acquisition_or_publication_cannot_support_earlier_preparation(context, field, stamp):
    store, _, _ = context
    source = store.put_source("future", SourceInput("future", "content", "Synthetic future clock", "synthetic:future", "Facts", **{field: stamp}))
    ref = save_revision(store, source, case_id="future")
    with pytest.raises(RecordError, match="SOURCE_CLOCK_AFTER_PREPARATION"):
        service(store).prepare(plan(ref))


def test_old_preparation_clock_and_identity_cannot_be_reused_for_a_different_successor(context):
    store, _, ref = context
    first = service(store).save(authorized(plan(ref)), submission_id="first")
    with pytest.raises(RecordError, match="TIME_ORDER"):
        service(store).prepare(plan(ref, predecessor=first["reference"]))
    with pytest.raises(RecordError, match="ID_MISMATCH"):
        service(store).prepare(plan(ref, predecessor=first["reference"], preparation_id="other", prepared_at=LATER))
