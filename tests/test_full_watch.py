"""Full admission and exact Odds/question bindings without rewriting old Watch history."""
from copy import deepcopy
from collections import Counter
from dataclasses import asdict

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds, record_artifact
from decision_kernel.application.odds_watch import OddsWatch, _Sources
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.watch_activity import WatchActivity, pointer_path
from decision_kernel.application.watch_execution import WatchExecution
from decision_kernel.application import watch_history as history_store
from decision_kernel.domain.odds_watch import CHECK_RECORD, WATCH_RECORD, validate_watch
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceRef, encoded
from decision_kernel.domain.research_work import ResearchProgress, ResearchRequest
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD
from test_conditional_odds import authorized, price_payload, spec_payload
from test_odds_watch import case, request, save, watch_payload
from test_probability_odds import ctx, calculation, REGISTRY
from test_watch_activity import apply, change, query as activity_query
from test_watch_conditions import evidence_condition, evidence_entry, numeric, v2_input, watch_v2
from test_watch_execution import Clock, plan_payload, query as execution_query


def saved_odds(store, spec, key="full-odds"):
    return ConditionalOdds(store, allow_declared_only=True).save(authorized("calculation", {
        "spec_ref": spec, "price": price_payload()}), submission_id=key)["reference"]


def question_link(question_id="open", condition_id="operating", worlds=None):
    return {"condition_id": condition_id, "question_id": question_id,
            "affected_inputs": ["cash", "world-value"], "world_ids": worlds or [],
            "impact_note": "The unresolved operating fact changes the declared cash and valuation assumptions."}


def full_watch(reference, odds=None, *, kind="conditional", conditions=None, links=None):
    value = watch_v2(reference, kind, conditions=conditions)
    return {**value, "schema": "odds-watch-v3", "tracking": {
        "odds_result": {"kind": kind, "reference": odds} if odds else None,
        "question_links": links or []}}


def structured_full(store):
    source = SourceRef.from_dict(store.read_revision(store.current("case"))["record"]["input"]["sources"][0])
    mandate = ResearchRequest.from_dict({"case_id": "questions", "kind": "full", "mandate": "Synthetic Full work",
        "questions": [{"question_id": identity, "text": "Original question " + identity}
                      for identity in ("open", "challenged", "closed", "immaterial", "immaterial-challenge")]})
    progress = ResearchProgress.from_dict({"body": "Synthetic unfinished Full; no economic certification.",
        "sources": [asdict(source)], "findings": [
            {"question_id": "open", "status": "open", "finding": "Unresolved cash timing", "evidence": [0],
             "remaining_kind": "acquired-unanalysed", "next_action": "Check the original disclosure"},
            {"question_id": "challenged", "status": "answered", "finding": "Provisional response", "evidence": [0]},
            {"question_id": "closed", "status": "answered", "finding": "Completed response", "evidence": [0]},
            {"question_id": "immaterial", "status": "open", "finding": "Minor unresolved fact", "evidence": [0],
             "material": False, "remaining_kind": "acquired-unanalysed", "next_action": "Optional detail"},
            {"question_id": "immaterial-challenge", "status": "answered", "finding": "Minor response",
             "evidence": [0], "material": False}],
        "challenges": [{"question_id": identity, "argument": "Alternative explanation", "disposition": "unresolved",
                        "response": "Still to examine", "evidence": [0]}
                       for identity in ("challenged", "immaterial-challenge")],
        "stop": "continue", "stop_reason": "Important questions remain open"})
    research = ResearchWorkflow(store).checkpoint(mandate, progress,
        submission_id="structured-full", revision_id="r1")["reference"]
    spec = ConditionalOdds(store).freeze(authorized("spec", spec_payload(research)),
        submission_id="structured-spec")["reference"]
    return research, spec, source


def legacy_quick(case, version=1, kind="conditional"):
    """Retain the exact pre-admission format through its original artifact owner."""
    store, research, _, service, _ = case
    source = SourceRef.from_dict(store.read_revision(research)["record"]["input"]["sources"][0])
    quick = store.submit_revision("quick", RevisionInput("quick", "r1", "quick", "Synthetic Quick",
        "Synthetic", "synthetic", (source,)))
    basis = asdict(quick)
    if kind == "conditional":
        basis = ConditionalOdds(store).freeze(authorized("spec", spec_payload(basis)),
            submission_id="quick-spec")["reference"]
    value = watch_payload(basis, kind) if version == 1 else watch_v2(basis, kind, conditions=[
        evidence_condition()] if kind == "research" else [numeric("price")])
    frozen = service._frozen(value, store)
    payload = authorized("watch", value)
    ref = service.custody._put("historical-watch", WATCH_RECORD, frozen, payload["rights_basis"])
    return payload, asdict(ref), frozen


def historical_activity(service, payload, key, kind="change"):
    """Original v1 transaction bytes, before the new Full admission rule existed."""
    head = service.store.backend.head()
    event, segment = service._prepare_plan({"kind": kind, "input": payload[kind]}, head,
                                           _Sources(service.store.backend))
    assert segment is None
    artifact = record_artifact(ACTIVITY_RECORD, event, payload["rights_basis"])
    files = history_store.artifact_files(artifact)
    files[pointer_path(event["watch_key"])] = encoded({"schema": "watch-activity-pointer-v1",
        "watch_key": event["watch_key"], "event_path": artifact.path()})
    return asdict(service.store._save(head, key, {"operation": "watch-activity-" + kind, "input": payload},
                                      artifact.path(), files))


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("kind", ["research", "conditional"])
def test_new_watch_cannot_admit_quick_directly_or_through_a_valuation_spec(case, version, kind):
    store, _, _, service, _ = case
    payload, ref, frozen = legacy_quick(case, version, kind)
    head = store.backend.head()
    with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"):
        service.freeze(payload, submission_id="new-quick-watch")
    assert service.freeze(payload, submission_id="historical-watch")["reference"] == ref
    assert encoded(service.read_watch(ref)) == encoded(frozen)
    assert store.backend.head() == head


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_quick_checks_reconcile_exactly_but_cannot_create_new_checks(case, version):
    store, _, _, service, _ = case
    _, ref, frozen = legacy_quick(case, version)
    price = price_payload() if version == 1 else v2_input(price_payload())
    req = request(store, ref, price=price)
    result = service._check(req)  # Historical projection, before admission existed.
    payload = authorized("check", req)
    saved = asdict(service.custody._put("historical-check", CHECK_RECORD, result, payload["rights_basis"]))
    head = store.backend.head()
    assert service.save(payload, submission_id="historical-check")["reference"] == saved
    assert encoded(service.read_check(saved)["result"]) == encoded(result)
    assert service.read_watch(ref) == frozen
    with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"):
        service.save(payload, submission_id="new-quick-check")
    with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"):
        service.inspect(req)
    with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"):
        service.check(req)
    assert store.backend.head() == head


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_quick_activity_plan_and_cleanup_preserve_original_journals(case, version):
    store, _, _, _, _ = case
    _, ref, _ = legacy_quick(case, version)
    activity = WatchActivity(store)
    registration = authorized("change", change(ref))
    registered = historical_activity(activity, registration, "historical-register")
    resume = authorized("change", change(ref, registered, "resume", "2026-01-03T04:01:00Z"))
    active = historical_activity(activity, resume, "historical-resume")
    price = price_payload() if version == 1 else v2_input(price_payload())
    execution = WatchExecution(store, clock=Clock())
    plan = authorized("plan", plan_payload(ref))
    legacy_plan = execution.records.put(execution._plan(plan["plan"]), plan["rights_basis"], "historical-plan")
    head = store.backend.head()
    assert activity.change(registration, submission_id="historical-register")["reference"] == registered
    assert activity.change(resume, submission_id="historical-resume")["reference"] == active
    assert execution.freeze(plan, submission_id="historical-plan")["reference"] == legacy_plan
    assert execution.read_plan(legacy_plan)["recalculation"] == "matches"
    for operation in (
        lambda: activity.change(registration, submission_id="new-register"),
        lambda: activity.save_check(authorized("check", activity_query(store, ref, active, price=price)),
                                    submission_id="new-check"),
        lambda: execution.freeze(plan, submission_id="new-plan"),
        lambda: execution.preview(execution_query(legacy_plan, price=price)),
        lambda: execution.reserve(authorized("execution", execution_query(legacy_plan, price=price))),
    ):
        with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"): operation()
    assert store.backend.head() == head
    paused = apply(activity, change(ref, active, "pause", "2026-01-03T04:03:00Z"), "cleanup-pause")
    with pytest.raises(RecordError, match="WATCH_FULL_REQUIRED"):
        apply(activity, change(ref, paused, "resume", "2026-01-03T04:04:00Z"), "new-resume")
    retired = apply(activity, change(ref, paused, "retire", "2026-01-03T04:05:00Z"), "cleanup-retire")
    assert activity.read(retired)["event"]["state"] == "retired"
    assert activity.read(active)["event"]["state"] == "active"
    assert activity.change(resume, submission_id="historical-resume")["reference"] == active


def test_v3_price_tracking_retains_the_actual_odds_and_continues_after_trigger(case):
    store, research, spec, service, _ = case
    odds = saved_odds(store, spec)
    value = full_watch(spec, odds, conditions=[numeric("price")])
    ref = service.freeze(authorized("watch", value), submission_id="full-watch")["reference"]
    frozen = service.read_watch(ref)
    assert frozen["schema"] == "frozen-odds-watch-v3" and frozen["research_ref"] == asdict(research)
    assert frozen["watch"]["tracking"]["odds_result"]["reference"] == odds
    previous = None
    for index, (price, status) in enumerate([("100", "first-trigger"), ("90", "continued-trigger"),
            (None, "cannot-determine"), ("95", "continued-trigger"), ("101", "not-met"), ("99", "first-trigger")]):
        previous, result = save(service, request(store, ref, at=f"2026-01-03T04:{index:02}:00Z", previous=previous,
            price=v2_input(price_payload(price=price) if price else None)), "full-check-" + str(index))
        assert result["schema"] == "odds-watch-check-v3" and result["status"] == status
        assert result["condition_results"][0]["status"] == status
        if index == 3: assert result["trigger_continuity"] == "intervening-checks-unknown"
    assert service.read_watch(ref) == frozen
    assert store.current("case") == research


def test_v3_shared_odds_inputs_reuse_original_reads_and_recheck_live_authority(case, monkeypatch):
    store, research, spec, service, _ = case
    value = full_watch(spec, saved_odds(store, spec), conditions=[numeric("price")])
    record = store.read_revision(research)["record"]
    source = SourceRef.from_dict(record["input"]["sources"][0])
    shared_paths = {research.path, record["body"]["path"], source.manifest.path,
                    source.manifest.path.rsplit("/", 1)[0] + "/content.txt"}
    original_resolve, reads = store.backend.resolve, Counter()
    def counted(ref):
        reads[ref.path] += 1
        return original_resolve(ref)
    monkeypatch.setattr(store.backend, "resolve", counted)
    ref = service.freeze(authorized("watch", value), submission_id="memo-full-watch")["reference"]
    assert all(reads[path] == 1 for path in shared_paths)
    reads.clear()
    service.read_watch(ref)
    assert all(reads[path] == 1 for path in shared_paths)
    original_frozen = service._frozen
    def revoke_after_immutable_reads(*args):
        frozen = original_frozen(*args)
        def unavailable(): raise RecordError("SIMULATED_VISIBILITY_CHANGED")
        monkeypatch.setattr(store.backend, "head", unavailable)
        return frozen
    monkeypatch.setattr(service, "_frozen", revoke_after_immutable_reads)
    with pytest.raises(RecordError, match="SIMULATED_VISIBILITY_CHANGED"): service.read_watch(ref)


def test_v3_execution_and_resume_keep_each_condition_history_and_first_trigger_intents(case):
    store, _, spec, service, _ = case
    value = full_watch(spec, saved_odds(store, spec), conditions=[numeric("higher"), numeric("lower", "90")])
    ref = service.freeze(authorized("watch", value), submission_id="managed-full-watch")["reference"]
    activity = WatchActivity(store)
    registered = apply(activity, change(ref), "full-register")
    apply(activity, change(ref, registered, "resume", "2026-01-03T04:01:00Z"), "full-resume")
    clock = Clock()
    execution = WatchExecution(store, clock=clock)
    plan = execution.freeze(authorized("plan", plan_payload(ref)), submission_id="full-plan")["reference"]
    slot = execution.reserve(authorized("execution", execution_query(plan, price=v2_input(price_payload(price="90")))))["reference"]
    first = execution.check({"slot_ref": slot})
    finished = execution.finish({"slot_ref": slot})
    assert [notice["condition_id"] for notice in finished["record"]["notices"]] == ["higher", "lower"]
    assert execution.read_result(finished["reference"])["record"] == finished["record"]
    paused = apply(activity, change(ref, first["reference"], "pause", "2026-01-03T04:03:00Z"), "full-pause")
    resumed = apply(activity, change(ref, paused, "resume", "2026-01-03T04:04:00Z"), "full-resume-again")
    clock.set("2026-01-03T04:05:05Z")
    slot = execution.reserve(authorized("execution", execution_query(plan, at="2026-01-03T04:05:05Z",
        price=v2_input(price_payload(price="85")))))["reference"]
    continued = execution.check({"slot_ref": slot})
    view = activity.read(continued["reference"])
    assert view["event"]["command"]["input"]["expected_event"] == resumed
    rows = view["event"]["result"]["condition_results"]
    assert all(row["status"] == "continued-trigger" and row["trigger_continuity"] == "intervening-checks-unknown" for row in rows)
    assert all(row["checks"] == 2 and row["first_triggers"] == row["continued_triggers"] == 1
               for row in view["summary"]["condition_summaries"])
    assert execution.finish({"slot_ref": slot})["record"]["notices"] == []


@pytest.mark.parametrize("fault", ["missing", "wrong-kind", "spec-not-result", "other-spec", "other-full", "quick"])
def test_v3_requires_a_real_saved_odds_result_from_the_same_full_and_spec(case, fault):
    store, research, spec, service, _ = case
    odds = saved_odds(store, spec)
    value = full_watch(spec, odds, conditions=[numeric("price")])
    expected = "WATCH_FULL_ODDS"
    if fault == "missing": value["tracking"]["odds_result"] = None
    elif fault == "wrong-kind": value["tracking"]["odds_result"]["kind"] = "probability"
    elif fault == "spec-not-result":
        value["tracking"]["odds_result"]["reference"] = spec
        expected = "ODDS_RECORD_INVALID"
    else:
        original = asdict(research)
        if fault in ("other-full", "quick"):
            source = SourceRef.from_dict(store.read_revision(research)["record"]["input"]["sources"][0])
            original = asdict(store.submit_revision("other", RevisionInput("other", "r1",
                "quick" if fault == "quick" else "full", "Other research", "Synthetic", "synthetic", (source,))))
        other = ConditionalOdds(store).freeze(authorized("spec", spec_payload(original, spec_id="different")),
            submission_id="other-spec")["reference"]
        value["tracking"]["odds_result"]["reference"] = saved_odds(store, other, "other-odds")
        if fault == "quick":
            value["basis"]["reference"] = other
            expected = "WATCH_FULL_REQUIRED"
    head = store.backend.head()
    with pytest.raises(RecordError, match=expected):
        service.freeze(authorized("watch", value), submission_id="invalid-full-watch")
    assert store.backend.head() == head


@pytest.mark.parametrize("question,valid", [("open", True), ("challenged", True), ("closed", False),
    ("immaterial", False), ("immaterial-challenge", False), ("unknown", False)])
def test_only_original_material_unresolved_questions_can_bind_evidence(case, question, valid):
    store, _, _, service, _ = case
    research, spec, source = structured_full(store)
    odds = saved_odds(store, spec)
    value = full_watch(spec, odds, conditions=[numeric("price"), evidence_condition()],
        links=[question_link(question, worlds=["one"])])
    head = store.backend.head()
    if not valid:
        with pytest.raises(RecordError, match="WATCH_FULL_(MATERIAL_UNRESOLVED_QUESTION_REQUIRED|QUESTION_NOT_IN_RESEARCH)"):
            service.freeze(authorized("watch", value), submission_id="invalid-question")
        assert store.backend.head() == head
        return
    ref = service.freeze(authorized("watch", value), submission_id="question-watch")["reference"]
    first, result = save(service, request(store, ref, price=v2_input(price_payload(), [evidence_entry(source)])), "question-check")
    assert result["status"] == "first-trigger" and len(result["condition_results"]) == 2
    store.submit_revision("successor", RevisionInput("questions", "r2", "full", "A new Full is not an observed question change",
        "Synthetic", "synthetic", (source,), predecessor=BlobRef(**research)))
    later = service.check(request(store, ref, at="2026-01-03T04:01:00Z", previous=first,
        price=v2_input(price_payload(), [evidence_entry(source)])))
    assert later["status"] == "review-required"
    assert all(row["condition_met"] is None for row in later["condition_results"])
    assert service.read_check(first)["result"] == result


@pytest.mark.parametrize("fault", ["missing-link", "duplicate-link", "numeric-link", "bad-input", "bad-world", "one-shot"])
def test_v3_question_dependencies_are_explicit_and_continuous(case, fault):
    store, _, _, service, _ = case
    _, spec, _ = structured_full(store)
    value = full_watch(spec, saved_odds(store, spec), conditions=[numeric("price"), evidence_condition()],
        links=[question_link(worlds=["one"])])
    if fault == "missing-link": value["tracking"]["question_links"] = []
    elif fault == "duplicate-link": value["tracking"]["question_links"] *= 2
    elif fault == "numeric-link": value["tracking"]["question_links"][0]["condition_id"] = "price"
    elif fault == "bad-input": value["tracking"]["question_links"][0]["affected_inputs"] = ["recommendation"]
    elif fault == "bad-world": value["tracking"]["question_links"][0]["world_ids"] = ["absent"]
    else: value["window"]["end_after_trigger"] = True
    head = store.backend.head()
    with pytest.raises(RecordError, match="WATCH_FULL"):
        service.freeze(authorized("watch", value), submission_id="invalid-dependency")
    assert store.backend.head() == head


def test_research_only_full_questions_need_no_odds_and_cannot_use_a_revision_as_a_question_change(case, monkeypatch):
    store, _, _, service, _ = case
    research, _, source = structured_full(store)
    value = full_watch(research, kind="research", conditions=[evidence_condition()], links=[question_link()])
    monkeypatch.setattr(service, "_registry", lambda: pytest.fail("Research-only questions do not require market data"))
    ref = service.freeze(authorized("watch", value), submission_id="research-only-full")["reference"]
    result = service.check(request(store, ref, price=v2_input(evidence=[evidence_entry(source)])))
    assert result["status"] == "first-trigger" and result["observation"]["calculation"] is None
    value["conditions"] = [{"condition_id": "version", **watch_payload(research, "research")["condition"]}]
    value["tracking"]["question_links"] = []
    with pytest.raises(RecordError, match="WATCH_FULL_QUESTION_CHANGE_REQUIRED"): validate_watch(value)


def test_v3_does_not_invent_questions_for_an_unstructured_full(case):
    store, research, _, service, _ = case
    value = full_watch(asdict(research), kind="research", conditions=[evidence_condition()], links=[question_link()])
    head = store.backend.head()
    with pytest.raises(RecordError, match="WATCH_FULL_QUESTIONS_NOT_RECORDED"):
        service.freeze(authorized("watch", value), submission_id="missing-workpaper")
    assert store.backend.head() == head


def test_probability_watch_restores_the_result_and_requires_the_exact_assessment(ctx):
    store, probability, _, _, _ = ctx
    calculation_input = calculation(ctx)
    odds = probability.save(authorized("calculation", calculation_input), submission_id="probability-result")["reference"]
    service = OddsWatch(store, REGISTRY)
    value = full_watch(calculation_input["assessment_ref"], odds, kind="probability",
        conditions=[numeric("probability", "0.5", "gte", "positive_return_scenario_probability")])
    head = store.backend.head()
    with pytest.raises(RecordError, match="WATCH_FULL_ACTIVATION_PRECEDES_ODDS"):
        service.freeze(authorized("watch", value), submission_id="probability-watch-before-odds")
    assert store.backend.head() == head
    value["activation"]["as_of"] = value["window"]["starts_at"] = "2026-01-03T04:00:00Z"
    ref = service.freeze(authorized("watch", value), submission_id="probability-watch")["reference"]
    result = service.check(request(store, ref, at="2026-01-03T04:01:00Z", price=v2_input(ctx[-1])))
    assert result["status"] == "first-trigger" and result["condition_results"][0]["value"] == "0.6"
    different = deepcopy(ctx[3]); different["assessment_id"] = "other-assessment"
    value["basis"]["reference"] = probability.freeze(authorized("assessment", different),
        submission_id="other-assessment")["reference"]
    head = store.backend.head()
    with pytest.raises(RecordError, match="WATCH_FULL_ODDS_ASSESSMENT_MISMATCH"):
        service.freeze(authorized("watch", value), submission_id="wrong-probability-watch")
    assert store.backend.head() == head
