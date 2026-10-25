"""Exact retained preparation horizons joined to real successor research only."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from decision_kernel.application import decision_preparation as preparation
from decision_kernel.application import decision_preparation_review as review
from decision_kernel.application.decision_preparation import preparation_artifact
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application import research_review
from decision_kernel.application.research_review import open_review
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, RevisionInput, Selection,
    SourceInput, SourceRef, StoreUnavailable, encoded,
)
from decision_kernel.domain.research_work import read_workpaper, render_workpaper
from decision_kernel.infrastructure.git_store import GitStore
from test_decision_preparation import (
    LATER, NOW, PREPARED, authorized, context as context, horizon,
    plan as preparation_plan, save_revision, service,
)

SOURCE_STORED = "2026-01-31T09:00:00+00:00"
SUCCESSOR_STORED = "2026-02-01T08:00:00+00:00"


def saved_case(context, monkeypatch, *, source_stored=SOURCE_STORED,
               successor_stored=SUCCESSOR_STORED, published="2026-01-30",
               acquired="2026-01-31", omit_challenge=False, extra_evidence=False):
    store, original_source, before = context
    value = preparation_plan(before, question_ids=["q1", "q2"], horizons=[
        horizon(), horizon("long", question_ids=["q2"], label="Different original question",
            window_end="2027-12-31T23:59:59+00:00", review_at="2026-12-01T00:00:00+00:00")])
    receipt = service(store).save(authorized(value), submission_id="original-preparation")
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: source_stored)
    later_source = store.put_source("later-source", SourceInput(
        "later-source", "content", "Synthetic later disclosure", "synthetic:preparation-review",
        "Later retained receipts differ from the original delivery figures.",
        published_at=published, acquired_at=acquired))
    sources = [later_source]
    if extra_evidence:
        extra = store.put_source("later-extra", SourceInput(
            "later-extra", "content", "Synthetic selected evidence", "synthetic:preparation-review-extra",
            "Unselected first line\nSelected later evidence\nUnselected final line\n"))
        sources.append(SourceRef(extra.manifest, Selection("lines", 2, 2)))
    request, progress = read_workpaper(store.read_revision(before))
    request = replace(request, predecessor=before)
    findings = (replace(progress.findings[0], finding="Cash receipts improved; contract limits remain.",
                        evidence=tuple(range(len(sources)))), progress.findings[1])
    progress = replace(progress, body="Later synthetic research.\nA changed conclusion with remaining limitations.\n",
        sources=tuple(sources), findings=findings,
        challenges=() if omit_challenge else progress.challenges,
        source_changes="The later disclosure replaces the original source for this retained draft.")
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: successor_stored)
    # Use the existing Store's format-valid retained records. Omitted challenge
    # history is a readable legacy input, not approval by ResearchWorkflow.
    method = {"id": "synthetic-method", "sha256": "a" * 64}
    after = store.submit_revision("later-research", RevisionInput(
        request.case_id, "r2", request.kind, render_workpaper(request, progress, method),
        request.mandate, method["id"] + "@sha256:" + method["sha256"],
        progress.sources, before, progress.unresolved(request)))
    selected = {"reference": receipt["reference"], "horizon_id": "near", "at": LATER,
        "review": {"before": {"reference": asdict(before), "start": 1, "end": 1},
                   "after": {"reference": asdict(after), "start": 1, "end": 2},
                   "question_id": "q1", "evidence_limit": 2}}
    return SimpleNamespace(store=store, original_source=original_source, before=before, after=after,
        sources=sources, value=value, receipt=receipt, plan=selected)


def open_case(case, **changes):
    return review.open_preparation_review(case.store, {**case.plan, **changes}, clock=lambda: NOW)


def test_real_saved_horizon_and_successor_review_reopen_without_changing_any_original(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    store = case.store
    head, original_input = store.backend.head(), encoded(case.plan)
    retained = {"preparation": ResearchArtifacts(store).read(case.receipt["reference"]),
                "before": store.read_revision(case.before), "after": store.read_revision(case.after)}
    result = open_case(case)
    assert result["schema"] == "decision-preparation-review-v1"
    assert result["reference"] == case.receipt["reference"]
    assert result["horizon_id"] == "near" and result["at"] == LATER
    assert result["preparation"] == service(store).read(case.receipt["reference"], at=LATER)
    assert result["horizon"] == result["preparation"]["projection"]["horizons"][0]
    none = {"reference": None, "status": "not-supplied"}
    comparison = deepcopy(result["review"])
    for side in ("before", "after"):
        for row in comparison[side]["evidence"]["items"]:
            row.pop("cutoff_check")
    assert comparison == open_review(store, case.plan["review"], agenda=none, brief=none)
    actual = result["review"]
    assert actual["revision_relation"] == "exact-direct-successor"
    assert actual["finding_changed"] and actual["selected_text_changed"]
    assert actual["before"]["evidence"]["items"][0]["view"]["data"] == "Delivery facts"
    assert actual["after"]["evidence"]["items"][0]["view"]["data"].startswith("Later retained receipts")
    assert actual["before"]["challenges"][0]["disposition"] == "unresolved"
    assert actual["after"]["challenges"][0]["disposition"] == "unresolved"
    assert result["timing"] == {
        "after_stored_at": SUCCESSOR_STORED,
        "relative_to_prepared_at": "after-preparation-declaration",
        "relative_to_window": "in-window",
        "meaning": "research-retention-time-not-economic-event-time-or-prospective-proof",
        "evidence_cutoff_scope": "read-evidence-only-pending-references-not-time-qualified",
    }
    assert result["preparation"]["document"]["preparation_time_basis"] == "author-declared-not-proof-draft-existed-at-that-time"
    assert result["persistence"] == "not-written" and result["execution"] == "not-started"
    assert actual["human_acceptance"] == "not-established"
    fresh = VersionedStore(GitStore(store.backend.root))
    assert review.open_preparation_review(fresh, case.plan, clock=lambda: NOW) == result
    assert encoded(case.plan) == original_input and store.backend.head() == head
    assert store.current("case") == case.after
    assert ResearchArtifacts(store).read(case.receipt["reference"]) == retained["preparation"]
    assert store.read_revision(case.before) == retained["before"]
    assert store.read_revision(case.after) == retained["after"]


def test_complete_preparation_history_is_kept_when_a_later_draft_removes_another_horizon(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    later = {**case.value, "predecessor": case.receipt["reference"],
             "prepared_at": "2026-01-11T08:00:00+00:00", "horizons": [horizon()],
             "revision_note": "Removed the other horizon from this selection, without resolving it."}
    receipt = service(case.store).save(authorized(later), submission_id="later-preparation")
    result = open_case(case, reference=receipt["reference"])
    history = result["preparation"]["history"]
    assert [row["reference"] for row in history] == [case.receipt["reference"], receipt["reference"]]
    assert history[0]["document"]["input"] == case.value
    assert history[1]["document"]["input"] == later
    assert result["preparation"]["document"]["changes"]["removed_horizons"][0]["horizon_id"] == "long"
    assert result["review"]["before"]["reference"] == asdict(case.before)


@pytest.mark.parametrize("fault,code", [
    ("horizon", "DECISION_PREPARATION_REVIEW_UNKNOWN_HORIZON"),
    ("other-horizon-question", "DECISION_PREPARATION_REVIEW_QUESTION_OUTSIDE_HORIZON"),
    ("before-digest", "DECISION_PREPARATION_REVIEW_RESEARCH_MISMATCH"),
    ("same-revision", "REVIEW_DISTINCT_REVISIONS_REQUIRED"),
    ("skip-successor", "REVIEW_EXACT_DIRECT_SUCCESSOR_REQUIRED"),
    ("other-case", "REVIEW_EXACT_DIRECT_SUCCESSOR_REQUIRED"),
])
def test_only_the_original_horizon_question_and_exact_direct_research_edge_are_allowed(
        context, monkeypatch, fault, code):
    case = saved_case(context, monkeypatch)
    if fault == "horizon":
        case.plan["horizon_id"] = "invented"
    elif fault == "other-horizon-question":
        case.plan["review"]["question_id"] = "q2"
    elif fault == "before-digest":
        case.plan["review"]["before"]["reference"]["sha256"] = "0" * 64
    elif fault == "same-revision":
        case.plan["review"]["after"]["reference"] = asdict(case.before)
    else:
        after = (save_revision(case.store, case.sources[0], "r3", case.after) if fault == "skip-successor"
                 else save_revision(case.store, case.sources[0], case_id="other"))
        case.plan["review"]["after"]["reference"] = asdict(after)
    head = case.store.backend.head()
    with pytest.raises(RecordError, match="^" + code + "$"):
        open_case(case)
    assert case.store.backend.head() == head


@pytest.mark.parametrize("field,value", [
    ("window_end", "2026-10-01T00:00:00+00:00"),
    ("human_confirmation", True), ("economic", []),
])
def test_plan_cannot_replace_the_original_window_or_add_a_verdict(context, monkeypatch, field, value):
    case = saved_case(context, monkeypatch)
    with pytest.raises(RecordError, match="DECISION_PREPARATION_REVIEW_SHAPE"):
        review.preparation_review_plan({**case.plan, field: value})


def test_oversized_plan_is_rejected_before_store_access(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    case.plan["horizon_id"] = "x" * (128 * 1024)
    monkeypatch.setattr(case.store.backend, "head", lambda: pytest.fail("oversized plan reached Store"))
    with pytest.raises(RecordError, match="^DECISION_PREPARATION_REVIEW_INPUT_BOUND$"):
        open_case(case)


@pytest.mark.parametrize("at,window,review_state", [
    ("2026-02-01T09:00:00+00:00", "in-window", "not-due"),
    (LATER, "window-ended", "review-due"),
])
def test_review_can_be_requested_early_without_moving_original_dates_or_resolving_conditions(
        context, monkeypatch, at, window, review_state):
    case = saved_case(context, monkeypatch)
    result = open_case(case, at=at)
    selected = result["horizon"]
    assert selected["window_state"] == window and selected["review_state"] == review_state
    for field in ("window_start", "window_end", "review_at", "mechanism", "expected_change"):
        assert selected[field] == case.value["horizons"][0][field]
    assert selected["mechanism_outcome"] == "not-evaluated"
    assert all(row["outcome"] == "not-evaluated" for row in selected["catalysts"] + selected["invalidation"])


@pytest.mark.parametrize("fault,code", [
    ("future-at", "DECISION_PREPARATION_FUTURE_TIMESTAMP"),
    ("before-preparation", "DECISION_PREPARATION_NOT_YET_RECORDED"),
    ("future-successor", "DECISION_PREPARATION_REVIEW_RESEARCH_AFTER_CUTOFF"),
    ("reversed-successor", "DECISION_PREPARATION_REVIEW_RESEARCH_TIME_ORDER"),
])
def test_future_or_reversed_clocks_cannot_support_the_selected_review(context, monkeypatch, fault, code):
    case = saved_case(context, monkeypatch, successor_stored=(
        "2026-04-21T08:00:00+00:00" if fault == "future-successor" else
        "2025-12-31T08:00:00+00:00" if fault == "reversed-successor" else SUCCESSOR_STORED))
    if fault == "future-at":
        case.plan["at"] = "2027-01-01T00:00:00+00:00"
    elif fault == "before-preparation":
        case.plan["at"] = "2026-01-09T08:00:00+00:00"
    with pytest.raises(RecordError, match="^" + code + "$"):
        open_case(case)


def test_successor_retained_before_declared_preparation_remains_a_retrospective_review(context, monkeypatch):
    case = saved_case(context, monkeypatch, source_stored="2026-01-04T08:00:00+00:00",
        successor_stored="2026-01-05T08:00:00+00:00", published=None, acquired=None)
    result = open_case(case)
    assert result["review"]["after"]["stored_at"] == "2026-01-05T08:00:00+00:00"
    assert result["preparation"]["document"]["input"]["prepared_at"] == PREPARED
    assert result["timing"]["relative_to_prepared_at"] == "no-later-than-preparation-declaration"
    assert result["preparation"]["document"]["preparation_time_basis"] == "author-declared-not-proof-draft-existed-at-that-time"
    assert result["preparation"]["document"]["formal_decision"] == "not-created"


@pytest.mark.parametrize("field,value,code", [
    ("source_stored", "2026-04-21T00:00:00+00:00", "DECISION_PREPARATION_REVIEW_EVIDENCE_AFTER_CUTOFF"),
    ("published", "2026-04-21", "DECISION_PREPARATION_REVIEW_SOURCE_CLOCK_AFTER_CUTOFF"),
    ("acquired", "2026-04-21T00:00:00+00:00", "DECISION_PREPARATION_REVIEW_SOURCE_CLOCK_AFTER_CUTOFF"),
])
def test_later_source_retention_publication_or_acquisition_cannot_backfill_the_cutoff(
        context, monkeypatch, field, value, code):
    case = saved_case(context, monkeypatch, **{field: value})
    with pytest.raises(RecordError, match="^" + code + "$"):
        open_case(case)


@pytest.mark.parametrize("omit_view", [False, True])
def test_unknown_source_clocks_are_preserved_without_borrowing_the_review_clock(context, monkeypatch, omit_view):
    case = saved_case(context, monkeypatch, published=None, acquired=None)
    if omit_view:
        monkeypatch.setattr(research_review, "MAX_EVIDENCE_BYTES", 1)
    result = open_case(case)
    row = result["review"]["after"]["evidence"]["items"][0]
    if omit_view:
        assert row["view"] is None and row["display"] == "not-included"
    else:
        source = row["view"]["manifest"]
        assert source["stored_at"] == SOURCE_STORED
        assert source["input"]["published_at"] is source["input"]["acquired_at"] is None
    assert row["cutoff_check"]["missing_clocks"] == ["acquired_at", "published_at"]
    assert row["cutoff_check"]["at"] == LATER
    assert row["cutoff_check"]["stored_at"] == SOURCE_STORED
    assert row["cutoff_check"]["published_at"] is row["cutoff_check"]["acquired_at"] is None
    assert result["review"]["source_authenticity"] == "not-certified"


def test_omitted_old_challenge_stays_unresolved_and_the_original_horizon_survives(context, monkeypatch):
    case = saved_case(context, monkeypatch, omit_challenge=True)
    result = open_case(case)
    actual = result["review"]
    assert actual["status"] == "partial" and actual["after"]["challenges"] == []
    assert actual["old_unresolved_not_carried_forward"] == actual["before"]["challenges"]
    assert actual["old_unresolved_not_carried_forward"][0]["disposition"] == "unresolved"
    assert actual["challenge_semantics"] == "missing-from-successor-does-not-mean-resolved"
    assert result["horizon"]["mechanism_outcome"] == "not-evaluated"


def test_limited_evidence_keeps_unread_exact_selection_without_reading_it(context, monkeypatch):
    case = saved_case(context, monkeypatch, extra_evidence=True)
    case.plan["review"]["evidence_limit"] = 1
    native_read, reads = VersionedStore.read_source, []

    def observed(store, ref):
        reads.append(ref)
        return native_read(store, ref)

    monkeypatch.setattr(VersionedStore, "read_source", observed)
    actual = open_case(case)["review"]
    evidence = actual["after"]["evidence"]
    assert actual["status"] == "partial"
    assert (evidence["total"], evidence["read"], evidence["remaining"]) == (2, 1, 1)
    assert encoded(evidence["pending"]) == encoded([{"index": 1,
        "open_request": {"operation": "read-source", "payload": asdict(case.sources[1])}}])
    assert case.sources[1] not in reads
    assert reads.count(case.original_source) == 1


@pytest.mark.parametrize("error,status", [
    (MissingObject("synthetic-missing"), "missing"),
    (IntegrityError("synthetic-corruption"), "invalid"),
])
def test_ordinary_successor_evidence_gap_remains_visible_without_erasing_the_original(
        context, monkeypatch, error, status):
    case = saved_case(context, monkeypatch)
    native_read = VersionedStore.read_source

    def unavailable(store, ref):
        if ref == case.sources[0]:
            raise error
        return native_read(store, ref)

    monkeypatch.setattr(VersionedStore, "read_source", unavailable)
    result = open_case(case)
    actual = result["review"]
    assert actual["status"] == "partial"
    assert actual["after"]["evidence"]["items"][0]["status"] == status
    assert actual["before"]["excerpt"]["text"] == "Synthetic research with unresolved operating evidence.\n"
    assert result["preparation"]["replay"] == "matches"


@pytest.mark.parametrize("error", [StoreUnavailable("REMOTE_NOT_AUTHORIZED"),
    IntegrityError("REMOTE_RESPONSE_TOO_LARGE"), RecordError("PRIVATE_UPGRADE_REVERSED")])
def test_native_source_failure_escapes_without_trying_later_evidence(context, monkeypatch, error):
    case = saved_case(context, monkeypatch, extra_evidence=True)
    native_read, reads = VersionedStore.read_source, []

    def denied(store, ref):
        reads.append(ref)
        if ref == case.sources[0]:
            raise error
        return native_read(store, ref)

    monkeypatch.setattr(VersionedStore, "read_source", denied)
    with pytest.raises(RecordError) as failed:
        open_case(case)
    assert failed.value is error
    assert reads[-1] == case.sources[0] and case.sources[1] not in reads


def test_successor_material_shares_the_preparation_read_budget(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    original = service(case.store)
    original.read(case.receipt["reference"], at=LATER)
    monkeypatch.setattr(preparation, "MAX_READ_BYTES", original._read_bytes)
    assert service(case.store).read(case.receipt["reference"], at=LATER)["replay"] == "matches"
    with pytest.raises(RecordError, match="^DECISION_PREPARATION_READ_BOUND$"):
        open_case(case)


def test_combined_result_bound_refuses_without_truncating_or_rewriting_history(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    head, original = case.store.backend.head(), encoded(case.plan)
    expected = open_case(case)
    bound = len(encoded(expected))
    monkeypatch.setattr(review, "MAX_RESULT_BYTES", bound)
    assert open_case(case) == expected
    monkeypatch.setattr(review, "MAX_RESULT_BYTES", bound - 1)
    with pytest.raises(RecordError, match="^DECISION_PREPARATION_REVIEW_RESULT_BOUND$"):
        open_case(case)
    assert case.store.backend.head() == head and encoded(case.plan) == original


def test_self_consistent_but_forged_preparation_cannot_bypass_original_replay(context, monkeypatch):
    case = saved_case(context, monkeypatch)
    document = deepcopy(service(case.store).read(case.receipt["reference"], at=LATER)["document"])
    document["research"]["challenges"][0]["disposition"] = "resolved"
    forged = ResearchArtifacts(case.store).put("forged-preparation",
        preparation_artifact(document, "Synthetic corruption fixture"))
    head = case.store.backend.head()
    with pytest.raises(RecordError, match="^DECISION_PREPARATION_REPLAY_MISMATCH$"):
        open_case(case, reference=asdict(forged))
    assert case.store.backend.head() == head
