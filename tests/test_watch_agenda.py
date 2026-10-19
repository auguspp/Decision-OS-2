"""Retained conditions enter the original Agenda without changing its choices."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.research_agenda import ResearchAgenda, SCHEMA, WATCH_SCHEMA, plan_input
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application import watch_evidence
from decision_kernel.domain.odds_watch import CHECK_RECORD
from decision_kernel.domain.records import RecordError, RevisionInput, StoreUnavailable, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, price_payload
from test_odds_watch import case, request, watch_payload
from test_watch_activity import activate, apply, change, query
from test_watch_conditions import numeric, watch_v2, v2_input

AS_OF = "2026-01-03T06:00:00Z"


def selected(reference, check_ref, kind="check", question_id=None):
    return {"reference": asdict(reference) if not isinstance(reference, dict) else deepcopy(reference),
            "question_id": question_id, "kind": kind, "check_ref": deepcopy(check_ref)}


def check(case, *, at="2026-01-03T04:00:00Z", price="100", previous=None, key="check"):
    store, _, _, service, watch = case
    payload = request(store, watch, at=at, price=price_payload(price=price) if price else None, previous=previous)
    return service.save(authorized("check", payload), submission_id=key)["reference"]


def retain_agenda(store, result, name="agenda"):
    return asdict(ResearchArtifacts(store).put(name, ArtifactInput.from_dict(result["artifact"])))


def agenda_plan(reference, evidence, **kwargs):
    return {"references": [asdict(reference) if not isinstance(reference, dict) else reference],
            "as_of": AS_OF, "watch_evidence": evidence, **kwargs}


def test_real_git_saved_check_agenda_and_independent_read_keep_original_choice(case, tmp_path):
    store, research, _, service, _ = case
    ref = check(case)
    payload = agenda_plan(research, [selected(research, ref)])
    before = store.backend.head()
    built = ResearchAgenda(store).build(payload)
    row = built["cards"][0]["watch_evidence"][0]
    assert store.backend.head() == before
    assert built["document"]["schema"] == WATCH_SCHEMA and built["document"]["state"] == []
    assert row["result"] == service.read_check(ref, include_current_use=False)["result"]
    assert row["result"]["status"] == "first-trigger" and row["status"] == "readable"
    assert built["cards"][0]["agenda"][0]["selection"] is None
    assert built["execution"] == "not-started" and built["notification"] == "not-sent"
    saved = retain_agenda(store, built)
    store.submit_revision("r2", RevisionInput("case", "r2", "full", "Later evidence", "Synthetic", "synthetic",
                                            predecessor=research, unresolved=("Not assessed",)))
    fresh = VersionedStore(GitStore(tmp_path / "watch.git"))
    head = fresh.backend.head()
    reopened = ResearchAgenda(fresh).open(saved)
    assert reopened["watch_evidence"] == built["watch_evidence"]
    assert "current_use" not in row and "current_activity" not in row
    assert reopened["cards"][0]["reference"] == asdict(research) and fresh.backend.head() == head


def test_v2_multiple_condition_results_and_boundaries_are_the_retained_ones(case):
    store, research, spec, service, _ = case
    watch = service.freeze(authorized("watch", watch_v2(spec, conditions=[
        numeric("near", "100"), numeric("lower", "90")])), submission_id="v2-watch")["reference"]
    ref = service.save(authorized("check", request(store, watch, price=v2_input(price_payload(price="95")))),
                       submission_id="v2-check")["reference"]
    result = ResearchAgenda(store).build(agenda_plan(research, [selected(research, ref)]))
    row = result["cards"][0]["watch_evidence"][0]
    assert row["definition"]["schema"] == "odds-watch-v2"
    assert row["result"] == service.read_check(ref, include_current_use=False)["result"]
    assert [c["condition_id"] for c in row["result"]["condition_results"]] == ["near", "lower"]
    boundary = row["result"]["next_boundaries"][0]
    assert boundary["below"]["value"] == "90" and boundary["above"]["value"] == "100"


def test_activity_check_and_lifecycle_event_do_not_refresh_or_invent_a_check(case):
    store, research, _, _, watch = case
    activity, _, active = activate(case)
    ref = activity.save_check(authorized("check", query(store, watch, active, price=price_payload())),
                              submission_id="managed-check")["reference"]
    closed = apply(activity, change(watch, ref, "retire", "2026-01-03T04:03:00Z"), "retire")
    choices = [selected(research, ref, "activity"), selected(research, closed, "activity")]
    view = ResearchAgenda(store).build(agenda_plan(research, choices))
    one, two = view["watch_evidence"]
    assert one["result"]["status"] == "first-trigger" and one["activity"]["state"] == "active"
    assert two["result"] is None and two["activity"]["state"] == "retired"
    assert two["activity"]["summary"]["checks"] == 1
    assert all("current_activity" not in row for row in view["watch_evidence"])


def test_inherited_evidence_is_exact_unselected_and_explicit_empty_exits(case):
    store, research, _, _, _ = case
    first = ResearchAgenda(store).build(agenda_plan(research, [selected(research, check(case))]))
    baseline = retain_agenda(store, first)
    newer = store.submit_revision("r2", RevisionInput("case", "r2", "full", "Later evidence", "Synthetic", "synthetic",
                                                     predecessor=research, unresolved=("Not assessed",)))
    built = ResearchAgenda(store).build({"references": [asdict(newer)], "as_of": AS_OF, "baseline": baseline})
    assert built["cards"][0]["watch_evidence"] == []
    assert built["unselected_watch_evidence"] == first["watch_evidence"]
    assert built["document"]["plan"]["watch_evidence"] == first["document"]["plan"]["watch_evidence"]
    cleared = ResearchAgenda(store).build({**built["document"]["plan"], "watch_evidence": []})
    assert cleared["watch_evidence"] == [] and cleared["document"]["schema"] == WATCH_SCHEMA
    assert ResearchAgenda(store).open(baseline)["watch_evidence"] == first["watch_evidence"]


@pytest.mark.parametrize("failure", ["wrong-research", "invented-question", "invented-question-missing-check", "wrong-kind"])
def test_mismatched_research_question_or_record_kind_is_rejected(case, failure):
    store, research, _, _, _ = case
    row = selected(research, check(case))
    if failure == "wrong-research":
        other = store.submit_revision("other", RevisionInput("other", "r1", "full", "Other", "Synthetic", "synthetic",
                                                              unresolved=("Synthetic unsourced fixture",)))
        row["reference"] = asdict(other)
    elif failure.startswith("invented-question"):
        row["question_id"] = "invented"
        if failure.endswith("missing-check"):
            row["check_ref"]["path"] = row["check_ref"]["path"].replace("odds-watch-check-", "missing-")
    else:
        row["kind"] = "activity"
    with pytest.raises(RecordError):
        ResearchAgenda(store).build(agenda_plan(research, [row]))


def test_missing_and_later_checks_remain_unknown_and_do_not_hide_research(case):
    store, research, _, _, _ = case
    ref = check(case)
    missing = {**ref, "path": ref["path"].replace("odds-watch-check-", "missing-")}
    result = ResearchAgenda(store).build(agenda_plan(research, [selected(research, missing)]))
    assert result["status"] == "partial" and result["cards"][0]["status"] == "readable"
    assert result["watch_evidence"][0]["status"] == "missing" and "result" not in result["watch_evidence"][0]
    later = ResearchAgenda(store).build(agenda_plan(research, [selected(research, ref)],
                                                  as_of="2026-01-03T03:59:59Z"))
    assert later["watch_evidence"][0]["status"] == "after-as-of"
    assert "result" not in later["watch_evidence"][0]


def test_unknown_retained_check_is_readable_but_never_false(case):
    store, research, _, _, _ = case
    ref = check(case, price=None)
    row = ResearchAgenda(store).build(agenda_plan(research, [selected(research, ref)]))["watch_evidence"][0]
    assert row["status"] == "readable" and row["result"]["status"] == "cannot-determine"
    assert row["result"]["condition_met"] is None


def test_forged_resealed_check_cannot_become_verified_agenda_evidence(case):
    store, research, _, service, _ = case
    original = service.read_check(check(case), include_current_use=False)["result"]
    original["condition_met"] = False
    ref = ConditionalOdds(store)._put("forged", CHECK_RECORD, original, "Synthetic tamper regression")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"):
        ResearchAgenda(store).build(agenda_plan(research, [selected(research, asdict(ref))]))


def test_operation_budget_or_permission_failure_stops_before_another_reader(case, monkeypatch):
    store, research, _, _, _ = case
    ref = check(case)
    other = {**ref, "path": ref["path"].replace("odds-watch-check-", "other-")}
    calls = []
    def fail(*args, **kwargs):
        calls.append(args)
        raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
    monkeypatch.setattr(OddsWatch, "read_check", fail)
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        ResearchAgenda(store).build(agenda_plan(research, [selected(research, ref), selected(research, other)]))
    assert len(calls) == 1


def test_item_bound_retains_exact_reopen_request(case, monkeypatch):
    store, research, _, _, _ = case
    ref = check(case)
    monkeypatch.setattr(watch_evidence, "MAX_ITEM_BYTES", 1)
    row = ResearchAgenda(store).build(agenda_plan(research, [selected(research, ref)]))["watch_evidence"][0]
    assert row["status"] == "not-included" and row["selection"]["check_ref"] == ref
    assert row["open_request"]["payload"]["reference"] == ref and "result" not in row


@pytest.mark.parametrize("failure", ["duplicate", "alias", "bound", "shape", "kind", "question"])
def test_selection_shape_identity_and_bounds(case, failure):
    _, research, _, _, _ = case
    row = selected(research, check(case))
    rows = [deepcopy(row)]
    if failure == "duplicate": rows += [row]
    elif failure == "alias":
        alias = deepcopy(row); alias["check_ref"]["commit"] = "f" * 40; rows += [alias]
    elif failure == "bound": rows *= 9
    elif failure == "shape": rows[0]["result"] = {"status": "first-trigger"}
    elif failure == "kind": rows[0]["kind"] = "notify"
    else: rows[0]["question_id"] = "../question"
    with pytest.raises(RecordError):
        plan_input(agenda_plan(research, rows))


def test_public_export_helper_covers_hidden_baseline_and_omitted_brief_rows(case):
    store, research, _, _, _ = case
    built = ResearchAgenda(store).build(agenda_plan(research, [selected(research, check(case))]))
    for value in (built, built["document"], built["document"]["plan"], built["cards"][0],
                  {"rows": [{"context": built["cards"][0]}]},
                  {"rows": [{"watch_evidence": [{"selection": built["watch_evidence"][0]["selection"]}]}]},
                  {"watch_evidence_adoption": [{"status": "not-included"}]}):
        assert watch_evidence.export_required(value)
    assert not watch_evidence.export_required({"watch_evidence": []})
    assert not watch_evidence.export_required({"source": {"watch_evidence": ["untrusted text"]}})


def test_legacy_agenda_plan_document_and_cards_keep_original_shape(case):
    store, research, _, _, _ = case
    built = ResearchAgenda(store).build({"references": [asdict(research)], "as_of": AS_OF})
    assert built["document"] == {"schema": SCHEMA, "state": [], "plan": {
        "references": [asdict(research)], "as_of": AS_OF, "changes": [], "baseline": None}}
    assert "watch_evidence" not in built and "watch_evidence" not in built["cards"][0]
    assert not watch_evidence.export_required(built)
