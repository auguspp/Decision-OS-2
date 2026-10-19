"""Ordered, versioned Watch truth and retained operating evidence on real custody."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_UP, localcontext

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch, verify_fresh
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.odds_watch import CHECK_RECORD, validate_watch
from decision_kernel.domain.records import (
    Conflict, IntegrityError, RecordError, RevisionInput, Selection, SourceInput, SourceRef, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from test_conditional_odds import authorized, price_payload, spec_payload
from test_odds_watch import AT, case, request, save, watch_payload
from test_probability_odds import ctx, calculation, REGISTRY


def numeric(condition_id, threshold="100", comparison="lte", metric="price", world_id=None):
    return {"condition_id": condition_id, "metric": metric, "world_id": world_id,
            "comparison": comparison, "threshold": threshold, "basis_note": "Synthetic declared boundary"}


def interval(condition_id, lower="80", upper="95", lower_inclusive=True, upper_inclusive=False):
    return {"condition_id": condition_id, "metric": "price", "world_id": None, "comparison": "between",
            "lower": lower, "upper": upper, "lower_inclusive": lower_inclusive,
            "upper_inclusive": upper_inclusive, "basis_note": "Synthetic interval, not trading instructions"}


def evidence_condition(condition_id="operating"):
    return {"condition_id": condition_id, "metric": "evidence", "condition_text": "Has the named plant started delivery?",
            "required_evidence": "Issuer disclosure identifying the plant and the delivery period",
            "basis_note": "Researcher must judge whether the disclosed facts meet this condition"}


def watch_v2(reference, kind="conditional", conditions=None, combination="any"):
    value = watch_payload(reference, kind)
    value.pop("condition")
    return {**value, "schema": "odds-watch-v2", "conditions": conditions or [numeric("first"), numeric("second", "90")],
            "combination": combination}


def v2_input(price=None, evidence=None):
    return {"schema": "odds-watch-input-v2", "price": price, "evidence": evidence or []}


def evidence_entry(source, conclusion="met", **changes):
    return {"condition_id": "operating", "observed_at": "2026-01-03T03:40:00Z", "assessed_at": "2026-01-03T03:50:00Z",
            "conclusion": conclusion, "reason": "Synthetic researcher assessment, not verified economics",
            "sources": [asdict(source)] if source else [], **changes}


def freeze(case, conditions=None, combination="any", **changes):
    store, _, spec, service, _ = case
    payload = {**watch_v2(spec, conditions=conditions, combination=combination), **changes}
    ref = service.freeze(authorized("watch", payload), submission_id="v2-watch")["reference"]
    return store, service, ref


def test_ordered_conditions_keep_each_trigger_unknown_gap_and_reentry(case):
    store, service, ref = freeze(case, [numeric("higher"), numeric("lower", "90"), interval("middle")])
    previous = None
    for i, (value, statuses) in enumerate([
        ("110", ["not-met"] * 3), ("100", ["first-trigger", "not-met", "not-met"]),
        ("90", ["continued-trigger", "first-trigger", "first-trigger"]),
        (None, ["cannot-determine"] * 3), ("85", ["continued-trigger"] * 3),
        ("96", ["continued-trigger", "not-met", "not-met"]),
        ("89", ["continued-trigger", "first-trigger", "first-trigger"]),
    ]):
        q = request(store, ref, at=f"2026-01-03T04:{i:02}:00Z", previous=previous,
                    price=v2_input(price_payload(price=value) if value else None))
        previous, result = save(service, q, "step-" + str(i))
        assert [row["condition_id"] for row in result["condition_results"]] == ["higher", "lower", "middle"]
        assert [row["status"] for row in result["condition_results"]] == statuses
        if i == 4:
            assert all(row["trigger_continuity"] == "intervening-checks-unknown" for row in result["condition_results"])
        if i == 6:
            assert result["status"] == "continued-trigger"  # Group truth never became false.
    restored = OddsWatch(VersionedStore(GitStore(store.backend.root))).read_check(previous)
    assert restored["result"] == result and restored["recalculation"] == "matches"


def test_all_conditions_share_one_calculation_and_boundaries_do_not_choose_a_direction(case, monkeypatch):
    conditions = [numeric("low", "90"), numeric("high"), interval("band"),
                  numeric("return", "0.1", "gte", "undiscounted_holding_period_return", "one")]
    store, service, ref = freeze(case, conditions)
    original, calls = ConditionalOdds.compute, []
    def count(self, payload):
        calls.append(payload)
        return original(self, payload)
    monkeypatch.setattr(ConditionalOdds, "compute", count)
    with localcontext() as context:
        context.prec, context.rounding = 3, ROUND_UP
        result = service.check(request(store, ref, price=v2_input(price_payload(price="97"))))
    assert len(calls) == 1
    assert len(result["next_boundaries"]) == 2  # Price and world return remain separate scales.
    bounds = result["next_boundaries"][0]
    assert bounds["below"]["value"] == "95" and bounds["below"]["distance"] == "2"
    assert bounds["above"]["value"] == "100" and bounds["above"]["distance"] == "3"
    assert bounds["at"] is None and bounds["below"]["conditions"][0]["inclusive"] is False
    at_edge = service.check(request(store, ref, price=v2_input(price_payload(price="95"))))
    assert at_edge["next_boundaries"][0]["at"]["conditions"][0]["inclusive"] is False
    assert at_edge["condition_results"][2]["condition_met"] is False


@pytest.mark.parametrize("comparison,value,expected", [("lte", "100", True), ("gte", "100", True),
    ("lte", "100.01", False), ("gte", "99.99", False)])
def test_inclusive_scalar_edges(case, comparison, value, expected):
    store, service, ref = freeze(case, [numeric("edge", comparison=comparison)])
    assert service.check(request(store, ref, price=v2_input(price_payload(price=value))))["condition_met"] is expected


@pytest.mark.parametrize("lower_closed,upper_closed,value,expected", [
    (True, False, "80", True), (False, True, "80", False), (True, True, "95", True), (True, False, "95", False),
])
def test_interval_endpoint_inclusion_is_explicit(case, lower_closed, upper_closed, value, expected):
    store, service, ref = freeze(case, [interval("edge", lower_inclusive=lower_closed, upper_inclusive=upper_closed)])
    assert service.check(request(store, ref, price=v2_input(price_payload(price=value))))["condition_met"] is expected


@pytest.mark.parametrize("mode,price,expected", [("any", "90", True), ("all", "90", None),
    ("any", "110", None), ("all", "110", False)])
def test_combination_preserves_missing_evidence_without_turning_it_false(case, mode, price, expected):
    store, service, ref = freeze(case, [numeric("price"), evidence_condition()], mode)
    result = service.check(request(store, ref, price=v2_input(price_payload(price=price))))
    assert result["condition_met"] is expected
    assert result["condition_results"][1]["condition_met"] is None
    assert result["condition_results"][1]["status"] == "cannot-determine"
    assert result["observation"]["unknown_condition_ids"] == ["operating"]


@pytest.mark.parametrize("combination,ended", [("any", True), ("all", False)])
def test_end_after_trigger_uses_the_frozen_combination(case, combination, ended):
    window = watch_payload(case[2])["window"]; window["end_after_trigger"] = True
    store, service, ref = freeze(case, combination=combination, window=window)
    previous, _ = save(service, request(store, ref, price=v2_input(price_payload(price="95"))), "first")
    result = service.check(request(store, ref, at="2026-01-03T04:01:00Z", previous=previous, price=v2_input(price_payload(price="90"))))
    assert (result["status"] == "ended") is ended
    assert result["status"] == ("ended" if ended else "first-trigger")


@pytest.mark.parametrize("conclusion,expected", [("met", True), ("not-met", False), ("cannot-determine", None)])
def test_operating_judgment_reads_exact_source_and_preserves_assessment_boundary(case, conclusion, expected, monkeypatch):
    store, research, _, service, _ = case
    source = store.put_source("operating", SourceInput("operating", "content", "Synthetic operating fact", "synthetic:operating",
        "Plant A began deliveries in the stated period.\nSeparate unrelated sentence."))
    source = SourceRef(source.manifest, Selection("lines", 1, 1))
    ref = service.freeze(authorized("watch", watch_v2(asdict(research), "research", [evidence_condition()])), submission_id="v2-research")["reference"]
    monkeypatch.setattr(service, "_registry", lambda: pytest.fail("No price/Registry for operating evidence"))
    saved, result = save(service, request(store, ref, price=v2_input(evidence=[evidence_entry(source, conclusion)])), "operating-check")
    assert result["condition_met"] is expected and result["observation"]["calculation"] is None
    evidence = result["condition_results"][0]["evidence"]
    assert SourceRef.from_dict(evidence["source_reads"][0]["reference"]) == source
    assert evidence["source_reads"][0]["selection_sha256"] and evidence["source_reads"][0]["selection_bytes"] > 0
    assert evidence["assessment"] == "submitted-researcher-judgment-not-automated-economic-verification"
    assert service.read_check(saved)["result"] == result


def test_missing_source_does_not_accept_a_submitted_true_or_false(case):
    store, service, ref = freeze(case, [evidence_condition()])
    for conclusion in ("met", "not-met", "review-required"):
        result = service.check(request(store, ref, price=v2_input(evidence=[evidence_entry(None, conclusion)])))
        assert result["condition_met"] is None and result["status"] == "cannot-determine"


def test_external_locator_is_unknown_and_corrupt_exact_reference_is_an_error(case):
    store, service, ref = freeze(case, [evidence_condition()])
    external = store.put_source("external", SourceInput("external", "content", "External", "synthetic:external", None,
        custody="external", limitations=("No retained body",)))
    result = service.check(request(store, ref, price=v2_input(evidence=[evidence_entry(external)])))
    assert result["condition_met"] is None
    entry = evidence_entry(external); entry["sources"][0]["manifest"]["sha256"] = "0" * 64
    with pytest.raises(RecordError): service.check(request(store, ref, price=v2_input(evidence=[entry])))


def test_evidence_stored_after_assessment_stays_unknown(case, monkeypatch):
    store, service, ref = freeze(case, [evidence_condition()])
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T03:55:00Z")
    source = store.put_source("late", SourceInput("late", "content", "Late retained fact", "synthetic:late", "Original fact"))
    result = service.check(request(store, ref, price=v2_input(evidence=[evidence_entry(source)])))
    assert result["condition_results"][0]["reason"] == "WATCH_EVIDENCE_AFTER_ASSESSMENT"
    assert result["condition_met"] is None


@pytest.mark.parametrize("source_time,expected,qualification", [
    ("2026-01-01", True, "date-definitely-before-assessment"),
    ("2026-01-03", None, "date-order-uncertain"),
    ("2026-01-04T00:00:00+08:00", None, "after-assessment"),
    ("0001-01-01", True, "date-definitely-before-assessment"),
    ("9999-12-31", None, "date-definitely-after-assessment"),
])
def test_known_source_clocks_and_date_precision_cannot_backfill_evidence(case, source_time, expected, qualification):
    store, service, ref = freeze(case, [evidence_condition()])
    source = store.put_source("clocked", SourceInput("clocked", "content", "Clocked source", "synthetic:clocked",
        "A declared operating fact.", acquired_at=source_time, published_at=source_time))
    entry = evidence_entry(source)
    result = service.check(request(store, ref, price=v2_input(evidence=[entry])))
    assert result["condition_met"] is expected
    clock = result["condition_results"][0]["evidence"]["source_reads"][0]["clock_qualification"]
    assert clock == {"acquired_at": qualification, "published_at": qualification}
    # Equivalent instants with another UTC offset do not change day qualification.
    entry["assessed_at"] = "2026-01-03T11:50:00+08:00"
    same = service.check(request(store, ref, price=v2_input(evidence=[entry])))
    assert same["condition_met"] is expected
    assert same["condition_results"][0]["evidence"]["source_reads"][0]["clock_qualification"] == clock


def test_later_source_is_not_read_into_an_earlier_records_snapshot(case, monkeypatch):
    store, service, ref = freeze(case, [evidence_condition()])
    before = store.backend.head()
    source = store.put_source("later", SourceInput("later", "content", "Later source", "synthetic:later", "Later retained fact"))
    original, calls = VersionedStore.read_source, []
    def counted(self, reference):
        calls.append(reference.manifest.path)
        return original(self, reference)
    monkeypatch.setattr(VersionedStore, "read_source", counted)
    result = service.check(request(store, ref, snapshot=before, price=v2_input(evidence=[evidence_entry(source)])))
    row = result["condition_results"][0]
    assert row["condition_met"] is None and row["reason"] == "WATCH_EVIDENCE_OUTSIDE_RECORDS_SNAPSHOT"
    assert row["evidence"]["source_reads"][0]["body_read"] is False
    assert source.manifest.path not in calls


def test_manifest_without_its_representation_cannot_backfill_a_snapshot(case, monkeypatch):
    store, service, ref = freeze(case, [evidence_condition()])
    payload = asdict(SourceInput("split-source", "content", "Incomplete snapshot", "synthetic:split", "Retained fact"))
    version = digest(encoded(payload))
    prefix = "source-views/split-source/" + version
    body = payload.pop("text").encode("utf-8")
    path, body_path = prefix + "/manifest.json", prefix + "/content.txt"
    manifest = {"schema": "source-version-v1", "version_id": version, "input": payload,
        "representation": {"path": body_path, "sha256": digest(body), "size": len(body)},
        "stored_at": "2026-01-03T02:30:00Z"}
    incomplete = store.backend.commit({path: encoded(manifest)}, expected=store.backend.head())
    complete = store.backend.commit({body_path: body}, expected=incomplete)
    source = SourceRef(store.backend.reference(complete, path))
    original, calls = VersionedStore.read_source, []
    def counted(self, reference):
        calls.append(reference.manifest.path)
        return original(self, reference)
    monkeypatch.setattr(VersionedStore, "read_source", counted)
    value = v2_input(evidence=[evidence_entry(source)])
    result = service.check(request(store, ref, snapshot=incomplete, price=value))
    row = result["condition_results"][0]
    assert row["condition_met"] is None and row["reason"] == "WATCH_EVIDENCE_OUTSIDE_RECORDS_SNAPSHOT"
    read = row["evidence"]["source_reads"][0]
    assert read["missing_component"] == "representation" and read["body_read"] is False
    assert source.manifest.path not in calls
    assert service.check(request(store, ref, snapshot=complete, price=value))["condition_met"] is True
    assert source.manifest.path in calls  # The original exact source validation still runs.
    changed = store.backend.commit({body_path: b"Changed fact"}, expected=complete)
    with pytest.raises(IntegrityError, match="BLOB_DIGEST_MISMATCH"):
        service.check(request(store, ref, snapshot=changed, price=value))


def test_retained_evidence_challenge_requires_review_without_changing_research_or_price(case):
    store, research, _, _, _ = case
    store, service, ref = freeze(case, [numeric("price"), evidence_condition()])
    source = store.put_source("challenge", SourceInput("challenge", "content", "Synthetic challenge", "synthetic:challenge",
        "Issuer now states the delivery target cannot be met."))
    result = service.check(request(store, ref, price=v2_input(price_payload(), [evidence_entry(source, "review-required")])))
    assert result["status"] == "review-required" and result["condition_met"] is None
    assert result["condition_results"][0]["condition_met"] is True
    assert result["condition_results"][1]["reason"] == "WATCH_EVIDENCE_CHALLENGES_FROZEN_PREMISE"
    assert SourceRef.from_dict(result["condition_results"][1]["evidence"]["source_reads"][0]["reference"]) == source
    assert store.current("case") == research and result["observation"]["calculation"]["price"]["price"] == "100"


@pytest.mark.parametrize("change", ["duplicate", "too-many", "missing-combination", "interval", "empty-interval", "float", "evidence-shape", "foreign-world"])
def test_invalid_frozen_conditions_never_publish(case, change):
    store, _, spec, service, _ = case
    value = watch_v2(spec)
    if change == "duplicate": value["conditions"][1]["condition_id"] = "first"
    elif change == "too-many": value["conditions"] = [numeric("condition-" + str(i)) for i in range(17)]
    elif change == "missing-combination": value.pop("combination")
    elif change == "interval": value["conditions"] = [interval("bad", lower="96", upper="95")]
    elif change == "empty-interval": value["conditions"] = [interval("bad", lower="95", upper="95")]
    elif change == "float": value["conditions"][0]["threshold"] = 100.0
    elif change == "evidence-shape": value["conditions"] = [{**evidence_condition(), "satisfied": True}]
    elif change == "foreign-world": value["conditions"] = [numeric("unknown", "0.1", "gte", "undiscounted_holding_period_return", "absent")]
    head = store.backend.head()
    with pytest.raises(RecordError): service.freeze(authorized("watch", value), submission_id="bad")
    assert store.backend.head() == head


@pytest.mark.parametrize("mutation", ["observed-after-assessed", "assessed-after-check", "assessed-before-activation", "unknown-condition", "duplicate", "legacy-price"])
def test_invalid_evidence_request_never_becomes_a_false_condition(case, mutation):
    store, service, ref = freeze(case, [evidence_condition()])
    data = v2_input(evidence=[evidence_entry(None)])
    entry = data["evidence"][0]
    if mutation == "observed-after-assessed": entry["observed_at"] = "2026-01-03T03:55:00Z"
    elif mutation == "assessed-after-check": entry["assessed_at"] = "2026-01-03T04:05:00Z"
    elif mutation == "assessed-before-activation": entry.update(observed_at="2026-01-03T03:00:00Z", assessed_at="2026-01-03T03:10:00Z")
    elif mutation == "unknown-condition": entry["condition_id"] = "different"
    elif mutation == "duplicate": data["evidence"].append(deepcopy(entry))
    elif mutation == "legacy-price": data = price_payload()
    with pytest.raises(RecordError): service.check(request(store, ref, price=data))


def test_new_research_requires_review_and_old_v2_replays_its_original_inputs(case):
    store, research, _, _, _ = case
    store, service, ref = freeze(case, [numeric("price"), evidence_condition()])
    saved, before = save(service, request(store, ref, price=v2_input(price_payload())), "before")
    store.submit_revision("new", RevisionInput("case", "r2", "full", "New economic premise", "Synthetic", "synthetic",
        predecessor=research, unresolved=("The premise changed",)))
    after = service.check(request(store, ref, at="2026-01-03T04:01:00Z", previous=saved, price=v2_input(price_payload())))
    assert after["status"] == "review-required" and after["condition_met"] is None
    assert all(row["status"] == "review-required" for row in after["condition_results"])
    assert service.read_check(saved)["result"] == before


def test_known_horizon_ends_v2_even_without_a_new_price(case):
    store, service, ref = freeze(case)
    service.clock = lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)
    result = service.check(request(store, ref, at="2028-12-31T04:00:00Z", price=v2_input()))
    assert result["status"] == "ended" and not result["check_performed"]


@pytest.mark.parametrize("damage", ["condition", "boundary", "order", "evidence"])
def test_resealed_results_cannot_forge_condition_state_or_boundaries(case, damage):
    store, service, ref = freeze(case, [numeric("first"), evidence_condition()])
    result = service.check(request(store, ref, price=v2_input(price_payload())))
    if damage == "condition": result["condition_results"][0]["last_decisive_match"] = False
    elif damage == "boundary": result["next_boundaries"][0]["at"]["value"] = "90"
    elif damage == "order": result["condition_results"].reverse()
    else: result["condition_results"][1]["evidence"] = {"conclusion": "met"}
    forged = service.custody._put("forged-v2", CHECK_RECORD, result, "Synthetic adversarial fixture")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"): service.read_check(asdict(forged))


def test_probability_conditions_keep_one_explicit_assessment_and_all_dimensions(ctx):
    store, _, _, _, market = ctx
    inputs = calculation(ctx); service = OddsWatch(store, REGISTRY)
    value = watch_v2(inputs["assessment_ref"], "probability", [numeric("price"),
        numeric("return", "-0.1", "gte", "expected_holding_period_return"),
        numeric("positive", "0.5", "gte", "positive_return_scenario_probability")], "all")
    ref = service.freeze(authorized("watch", value), submission_id="v2-probability")["reference"]
    result = service.check(request(store, ref, price=v2_input(market)))
    assert result["condition_met"] is True and len(result["next_boundaries"]) == 3
    assert result["observation"]["calculation"]["assessment_ref"] == inputs["assessment_ref"]
    assert [row["value"] for row in result["condition_results"]] == ["100", "-0.05", "0.6"]


def test_same_intent_conflicts_and_independent_readback_preserves_receipt(case):
    store, service, ref = freeze(case)
    publication = authorized("check", request(store, ref, price=v2_input(price_payload())))
    receipt = service.save(publication, submission_id="original-v2")
    assert service.save(publication, submission_id="original-v2") == receipt
    fresh = verify_fresh(receipt, "save-odds-watch-check", lambda: VersionedStore(GitStore(store.backend.root)))
    assert fresh["readback"]["status"] == "verified" and fresh["readback"]["new_reader"]
    publication["check"]["input"]["price"]["price"] = "80"
    with pytest.raises(Conflict): service.save(publication, submission_id="original-v2")
