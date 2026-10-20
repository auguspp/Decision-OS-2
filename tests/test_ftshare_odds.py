"""Retained native FTShare sources through real Odds/Watch consumers, offline.

All companies, economic assumptions, probabilities and data are synthetic.
The existing source/collection fixtures own custody; no production records or
provider requests are used to make these assertions.
"""
from copy import copy, deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD
from decision_kernel.application.ftshare_collection import (
    begin_ftshare_slot, finish_ftshare_slot, retain_ftshare_single,
)
from decision_kernel.application.ftshare_single import capture_single
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.probability_odds import ProbabilityOdds
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, RevisionInput, SourceInput, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import odds_commands
from test_conditional_odds import authorized, price_payload, spec_payload
from test_ftshare_collection import (
    _new_calendars, _retain, adopt_kernel, kernel_adoption_fixture, plan_single, single_capture,
)
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH
from test_odds_watch import request as watch_request, watch_payload
from test_probability_odds import assessment_payload

AS_OF = "2026-10-19T12:05:00+00:00"
EVALUATED = "2026-10-19T13:00:00+00:00"
CODE = "600000.SH"


class SyntheticClock(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2030, 1, 1, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def clocks(monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now",
                        lambda: "2026-10-19T12:03:00+00:00")
    for module in ("conditional_odds", "probability_odds", "odds_watch", "watch_activity"):
        monkeypatch.setattr("decision_kernel.application." + module + ".datetime", SyntheticClock)


def build_case(*, storage=None):
    f = kernel_adoption_fixture(storage=storage)
    f["adopted"] = adopt_kernel(f)
    store = f["store"]
    source = store.put_source("odds-source", SourceInput("odds-basis", "content", "Synthetic basis",
        "synthetic:ftshare-odds", "Synthetic economic assumptions only; not an actual company judgment"))
    revision = store.submit_revision("odds-research", RevisionInput("odds-case", "r1", "full",
        "Synthetic frozen research", "Synthetic commission", "synthetic", (source,),
        unresolved=("Unverified economic parameters",)))
    spec = spec_payload(asdict(revision), security={"code": CODE, "share_class": "ordinary-a"},
        valuation_date="2026-10-16", capital_structure={"as_of": "2026-10-01",
            "assumption": "Synthetic fixed shares; source prices do not certify capital structure"})
    spec["worlds"][0]["value"] = "1200"
    spec["worlds"][0]["distributions"][0]["amount"] = "50"
    down = deepcopy(spec["worlds"][0])
    down.update(id="down", name="Down", value="500", distributions=[])
    spec["worlds"].append(down)
    conditional = ConditionalOdds(store, REGISTRY)
    spec_ref = conditional.freeze(authorized("spec", spec), submission_id="ft-spec")["reference"]
    assessment = assessment_payload(spec_ref)
    assessment["as_of"] = assessment["policy"]["as_of"] = AS_OF
    assessment["policy"].update(max_quote_age_days=7, max_master_age_days=30, max_capital_age_days=30)
    probability = ProbabilityOdds(store, REGISTRY)
    assessment_ref = probability.freeze(authorized("assessment", assessment), submission_id="ft-assessment")["reference"]
    policy = {key: assessment["policy"][key] for key in ("policy_id", "as_of", "rationale",
              "max_quote_age_days", "max_master_age_days", "max_capital_age_days")}
    market = {"schema": "odds-ftshare-close-v1", "price_id": "ft-original-close",
        "collection_ref": deepcopy(f["adopted"]["reference"]), "exchange": "SSE", "quote_date": "2026-10-16"}
    f.update(spec=spec, spec_ref=spec_ref, research_ref=revision, conditional=conditional,
        probability=probability, assessment=assessment, assessment_ref=assessment_ref,
        policy=policy, market=market)
    return f


@pytest.fixture
def case():
    return build_case()


@pytest.fixture
def native_case(tmp_path):
    """Saved Watch use proves original activity selection on a real Git graph."""
    backend = GitStore.init_synthetic(tmp_path / "ftshare-watch.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    return build_case(storage=(backend, store, fresh))


def conditional_input(case, market=None):
    return {"spec_ref": case["spec_ref"], "market": deepcopy(market or case["market"]),
        "market_policy": deepcopy(case["policy"]), "evaluated_at": EVALUATED}


def probability_input(case, market=None):
    return {"assessment_ref": case["assessment_ref"], "market": deepcopy(market or case["market"]),
            "evaluated_at": EVALUATED}


def replacement(case, *, price="8", failed=False, acquired_at=None,
                completion_policy=EOD_COMPLETION_POLICY, **plan_options):
    """A different retained native raw query; the original is left immutable."""
    collection = plan_single(case, generation="g-synthetic-price-change",
        end="20261019", calendar_references=_new_calendars(case["store"], case["fresh"], through="20261020"),
        completion_policy=completion_policy,
        targets=[{"symbol": CODE, "adjust_kind": "none"}], **plan_options)
    store, fresh = case["store"], case["fresh"]
    genesis = _retain(collection, store, fresh)
    slot = collection["plan"]["new_slots"][0]
    started = begin_ftshare_slot(collection, slot, at=collection["updated_at"])
    reserved = _retain(started, store, fresh, genesis["reference"])
    if failed:
        args = {"reason_code": "FTSHARE_HTTP_429", "source_hold": "FTSHARE_HTTP_429"}
    else:
        original = single_capture(started, slot)
        body = json.loads(original["body_utf8"])
        for row in body["data"]:
            row.update(open=price, high=price, low=price, close=price)
        params = original["request"]["parameters"]
        captured = capture_single(encoded(body), **{key: params[key] for key in
            ("symbol", "since", "until", "adjust_kind")}, acquired_at=acquired_at or original["acquired_at"])
        source = retain_ftshare_single(captured, store, fresh_reader=fresh, custody_authorized=True)
        args = {"reference": source["reference"]}
    finished = finish_ftshare_slot(started, slot, at=started["updated_at"], **args)
    retained = _retain(finished, store, fresh, reserved["reference"])
    return {**deepcopy(case["market"]), "price_id": "ft-replacement-close",
            "quote_date": "2026-10-19", "collection_ref": retained["reference"]}


def watch(case, kind="conditional", version=1):
    basis = case["spec_ref"] if kind == "conditional" else case["assessment_ref"]
    w = watch_payload(basis, kind)
    w["watch_id"] = "ft-" + kind
    w["activation"]["as_of"] = w["window"]["starts_at"] = AS_OF
    w["condition"]["threshold"] = "9"
    if kind == "conditional":
        w["data_policy"] = {"price_kind": "retained-ftshare-raw-close", "market_policy": deepcopy(case["policy"])}
    if version == 2:
        from test_watch_conditions import interval
        condition = w.pop("condition")
        w.update(schema="odds-watch-v2", combination="all", conditions=[
            {**condition, "condition_id": "lower-price"}, interval("declared-range", "7", "9", True, True)])
    service = OddsWatch(case["store"], REGISTRY)
    ref = service.freeze(authorized("watch", w), submission_id="watch-" + kind)["reference"]
    return service, ref


def test_native_raw_close_reaches_conditional_and_probability_without_full_scope_claim(case):
    before = case["store"].backend.head()
    conditional = case["conditional"].compute(conditional_input(case))
    probability = case["probability"].compute(probability_input(case))
    assert conditional["schema"] == "conditional-odds-retained-close-v1"
    assert conditional["probability_status"] == "not-established" and conditional["weighted_aggregate"] is None
    assert conditional["world_results"][1]["undiscounted_holding_period_return"] == "0.25"
    assert probability["aggregate"]["expected_holding_period_return"] == "-0.05"
    assert conditional["market"] == probability["market"]
    qualified = conditional["market"]
    assert qualified["price"]["kind"] == "retained-ftshare-raw-close" and qualified["price"]["price"] == "10"
    assert qualified["native_request"]["parameters"]["adjust_kind"] == "none"
    assert qualified["native_request"]["url"].endswith("/api/v1/market/data/stock-candlesticks")
    assert qualified["source_query_slot"] == "raw-sh-history"
    assert set(qualified["source_references"]) == {"collection", "calendar", "master", "daily"}
    assert qualified["quote_age_calendar_days"] == 3
    assert qualified["freshness"]["latest_market_close"] == "not-certified"
    assert qualified["source_requests"] == 0 and case["store"].backend.head() == before
    assert conditional["investment_authority"] == probability["investment_authority"] == "none"


def test_price_replacement_preserves_spec_probability_and_original_replay(case):
    original = case["conditional"].save(authorized("calculation", conditional_input(case)), submission_id="ft-result-1")
    second_market = replacement(case)
    second_input = conditional_input(case, second_market)
    second = case["conditional"].save(authorized("calculation", second_input), submission_id="ft-result-2")
    reader = ConditionalOdds(case["fresh"](), REGISTRY)
    a, b = reader.read(original["reference"]), reader.read(second["reference"])
    assert a["recalculation"] == b["recalculation"] == "matches"
    assert a["result"]["price"]["price"] == "10" and b["result"]["price"]["price"] == "8"
    assert a["result"]["spec_sha256"] == b["result"]["spec_sha256"]
    assert b["result"]["world_results"][1]["undiscounted_holding_period_return"] == "0.5625"
    p = case["probability"].compute(probability_input(case, second_market))
    assert p["aggregate"]["expected_holding_period_return"] == "0.1875"
    assert p["assessment_ref"] == case["assessment_ref"] and p["policy"] == case["assessment"]["policy"]
    assert case["store"].current("odds-case") == case["research_ref"]
    assert case["conditional"].save(authorized("calculation", second_input), submission_id="ft-result-2") == second
    with pytest.raises(Conflict):
        case["conditional"].save(authorized("calculation", second_input), submission_id="ft-result-1")


@pytest.mark.parametrize("kind", ["conditional", "probability"])
@pytest.mark.parametrize("version", [1, 2])
def test_existing_watch_consumes_both_prices_and_replays_saved_checks(native_case, kind, version):
    from test_watch_conditions import v2_input
    case = native_case
    service, ref = watch(case, kind, version)
    first_input = watch_request(case["store"], ref, at=EVALUATED,
                                price=v2_input(case["market"]) if version == 2 else case["market"])
    first = service.save(authorized("check", first_input), submission_id="watch-first")
    assert first["status"] == "not-met"
    next_market = replacement(case)
    second_input = watch_request(case["store"], ref, at="2026-10-19T13:01:00+00:00",
                                price=v2_input(next_market) if version == 2 else next_market, previous=first["reference"])
    second = service.save(authorized("check", second_input), submission_id="watch-second")
    assert second["status"] == "first-trigger"
    read = OddsWatch(case["fresh"](), REGISTRY).read_check(second["reference"])
    assert read["recalculation"] == "matches"
    calculation = read["result"]["observation"]["calculation"]
    assert calculation["price"]["kind"] == "retained-ftshare-raw-close" and calculation["price"]["price"] == "8"
    assert calculation["spec_ref"] == case["spec_ref"]
    if version == 2:
        from decision_kernel.application.research_agenda import ResearchAgenda
        assert [c["status"] for c in read["result"]["condition_results"]] == ["first-trigger"] * 2
        agenda = ResearchAgenda(case["fresh"]()).build({"references": [asdict(case["research_ref"])],
            "as_of": "2026-10-19T13:02:00+00:00", "watch_evidence": [{
                "reference": asdict(case["research_ref"]), "question_id": None, "kind": "check",
                "check_ref": second["reference"]}]})
        assert agenda["cards"][0]["watch_evidence"][0]["result"] == read["result"]
    assert not any(read["result"]["effects"][key] for key in
                   ("notification_sent", "research_started", "scheduler_created"))


@pytest.mark.parametrize("change,error", [
    ({"exchange": "SZSE"}, "SECURITY_EXCHANGE_MISMATCH"),
    ({"quote_date": "2026-10-10"}, "NOT_TRADING_DAY"),
    ({"quote_date": "2026-10-20"}, "BEFORE_CUTOFF_OR_STALE"),
    ({"quote_date": "2026-10-18"}, "OUTSIDE_COLLECTION_RANGE"),
    ({"quote_date": "2026-09-30"}, "BEFORE_CUTOFF_OR_STALE"),
    ({"adjust_kind": "forward"}, "INPUT_SHAPE"),
    ({"price": "8"}, "INPUT_SHAPE"),
])
def test_explicit_date_security_and_no_caller_supplied_adjusted_price(case, change, error):
    payload = conditional_input(case)
    payload["market"].update(change)
    if change.get("quote_date") == "2026-10-10":
        payload["market_policy"]["max_quote_age_days"] = 30
    head = case["store"].backend.head()
    with pytest.raises(RecordError, match=error):
        case["conditional"].compute(payload)
    assert case["store"].backend.head() == head


@pytest.mark.parametrize("change,error", [
    ({"max_quote_age_days": 2}, "BEFORE_CUTOFF_OR_STALE"),
    ({"max_master_age_days": 1}, "MASTER_STALE"),
    ({"max_capital_age_days": 1}, "CAPITAL_ASSUMPTION_STALE"),
    ({"max_quote_age_days": True}, "POLICY_DAY_BOUND"),
    ({"as_of": "2026-10-20T00:00:00Z"}, "POLICY_AFTER_EVALUATION"),
])
def test_declared_age_policy_cannot_be_omitted_or_ignored(case, change, error):
    payload = conditional_input(case)
    payload["market_policy"].update(change)
    with pytest.raises(RecordError, match=error):
        case["conditional"].compute(payload)


def test_date_qualified_close_is_not_a_claim_of_current_market_or_historical_pit(case):
    payload = conditional_input(case)
    payload["market"]["quote_date"] = "2026-10-15"
    # A specified old completed trading date may be used inside its declared
    # age limit, even when the collection also contains a later observation.
    result = case["conditional"].compute(payload)
    assert result["price"]["as_of"] == "2026-10-15T18:35:00+08:00"
    assert result["market"]["freshness"]["latest_market_close"] == "not-certified"
    assert "PRICE_CONTEXT_DOES_NOT_ESTABLISH_HISTORICAL_PIT" in result["limitations"]


def test_sources_acquired_later_than_evaluation_never_backfill_a_quote(case):
    payload = conditional_input(case)
    payload["evaluated_at"] = "2026-10-19T11:59:00+00:00"
    payload["market_policy"]["as_of"] = "2026-10-19T11:58:00+00:00"
    with pytest.raises(RecordError, match="CAPTURE_AFTER_EVALUATION"):
        case["conditional"].compute(payload)


def test_post_cutoff_same_day_close_reaches_watch_and_remains_usable_the_next_day(native_case):
    case = native_case
    market = replacement(case, acquired_at="2026-10-19T10:35:00+00:00")
    payload = conditional_input(case, market)
    saved = case["conditional"].save(authorized("calculation", payload), submission_id="post-close")
    value = ConditionalOdds(case["fresh"](), REGISTRY).read(saved["reference"])["result"]
    assert value["market"]["quote_age_calendar_days"] == 0
    assert value["price"]["as_of"] == "2026-10-19T18:35:00+08:00"
    assert value["price"]["timestamp_semantics"] == "conservative-local-post-close-observation-cutoff-not-source-tick"
    service, ref = watch(case)
    checked = service.save(authorized("check", watch_request(case["store"], ref, at=EVALUATED,
                            price=market)), submission_id="same-day-watch")
    assert checked["status"] == "first-trigger"
    following = service.check(watch_request(case["store"], ref, at="2026-10-20T13:00:00+00:00",
                            price=market, previous=checked["reference"]))
    assert following["status"] == "continued-trigger"
    assert following["observation"]["calculation"]["market"]["quote_age_calendar_days"] == 1
    assert service.read_check(checked["reference"])["result"]["observation"]["calculation"]["market"]["quote_age_calendar_days"] == 0


def test_evaluation_before_local_cutoff_cannot_use_same_day_close(case):
    payload = conditional_input(case, replacement(case))
    payload["market_policy"]["as_of"] = "2026-10-19T09:00:00+00:00"
    payload["evaluated_at"] = "2026-10-19T10:34:59+00:00"
    with pytest.raises(RecordError, match="FTSHARE_CLOSE_BEFORE_CUTOFF_OR_STALE"):
        case["conditional"].compute(payload)


def test_pre_cutoff_response_never_becomes_post_close_by_waiting(case):
    # A legacy collection may contain an early response. Its original custody
    # stays readable; the new Odds consumer must never promote it to a close.
    # New-policy collections reject this capture even before collection retention.
    market = replacement(case, acquired_at="2026-10-19T10:34:59+00:00",
        completion_policy=None, created_at="2026-10-20T12:00:00+00:00")
    for evaluated in ("2026-10-20T13:00:00+00:00", "2026-10-21T13:00:00+00:00"):
        payload = conditional_input(case, market)
        payload["evaluated_at"] = evaluated
        with pytest.raises(RecordError, match="FTSHARE_CAPTURE_BEFORE_CLOSE_CUTOFF"):
            case["conditional"].compute(payload)
    service, ref = watch(case)
    checked = service.check(watch_request(case["store"], ref, at="2026-10-20T13:00:00+00:00", price=market))
    assert checked["status"] == "cannot-determine" and checked["condition_met"] is None
    assert checked["observation"]["reason"] == "ODDS_FTSHARE_CAPTURE_BEFORE_CLOSE_CUTOFF"


def test_quarantined_close_is_missing_and_never_filled_from_original(case):
    market = replacement(case, price="0")
    with pytest.raises(RecordError, match="ODDS_EXACT_CLOSE_MISSING"):
        case["conditional"].compute(conditional_input(case, market))
    service, ref = watch(case)
    checked = service.check(watch_request(case["store"], ref, at=EVALUATED, price=market))
    assert checked["status"] == "cannot-determine"


def test_retained_current_fair_value_still_means_distance_not_future_return(case):
    spec = deepcopy(case["spec"])
    spec.update(value_kind="current-fair-value", method="equity-value-distance-v1",
                target_date=None, cash_policy="included-in-fair-value")
    for world in spec["worlds"]:
        world["distributions"] = []
    ref = case["conditional"].freeze(authorized("spec", spec), submission_id="ft-fair-value")["reference"]
    result = case["conditional"].compute({**conditional_input(case), "spec_ref": ref})
    assert result["world_results"][1]["value_to_price_distance"] == "0.2"
    assert "undiscounted_holding_period_return" not in result["world_results"][1]
    assert result["weighted_aggregate"] is None


@pytest.mark.parametrize("kind", ["conditional", "probability"])
def test_held_replacement_never_falls_back_and_original_check_remains_readable(native_case, kind):
    case = native_case
    service, ref = watch(case, kind)
    original = service.save(authorized("check", watch_request(case["store"], ref, at=EVALUATED,
                            price=case["market"])), submission_id="before-hold")
    held = replacement(case, failed=True)
    checked = service.check(watch_request(case["store"], ref, at="2026-10-19T13:01:00+00:00",
                            price=held, previous=original["reference"]))
    assert checked["status"] == "cannot-determine" and checked["condition_met"] is None
    assert checked["observation"]["reason"] == "ODDS_FTSHARE_SOURCE_HOLD"
    assert service.read_check(original["reference"])["result"]["status"] == "not-met"


def test_missing_native_raw_query_is_unknown_and_has_no_declared_or_forward_fallback(case):
    spec = deepcopy(case["spec"])
    spec["security"]["code"] = "600001.SH"  # In the qualified synthetic master, never requested from FTShare.
    case["spec_ref"] = case["conditional"].freeze(authorized("spec", spec), submission_id="unrequested-spec")["reference"]
    service, ref = watch(case)
    checked = service.check(watch_request(case["store"], ref, at=EVALUATED, price=case["market"]))
    assert checked["status"] == "cannot-determine" and checked["condition_met"] is None
    assert checked["observation"]["reason"] == "ODDS_EXACT_CLOSE_MISSING"
    wrong = watch_request(case["store"], ref, at=EVALUATED, price=price_payload(price="1"))
    assert service.check(wrong)["status"] == "cannot-determine"


def test_corrupt_ref_and_tampered_result_fail_instead_of_a_negative_condition(case):
    service, ref = watch(case)
    market = deepcopy(case["market"])
    market["collection_ref"]["manifest"]["sha256"] = "0" * 64
    with pytest.raises(RecordError):
        service.check(watch_request(case["store"], ref, at=EVALUATED, price=market))
    result = case["conditional"].compute(conditional_input(case))
    result["price"]["price"] = "1"
    stored = case["conditional"]._put("tampered-ft-result", RESULT_RECORD, result, "Synthetic adversarial fixture")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"):
        case["conditional"].read(asdict(stored))


def test_matching_registry_is_required_even_when_prices_come_from_ftshare(case, monkeypatch):
    from decision_kernel.application.relay_registry import RelayRegistry
    registry = RelayRegistry(REGISTRY_PATH)
    monkeypatch.setattr(registry, "snapshot", {"changed": True})
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_MANIFEST"):
        ConditionalOdds(case["store"], registry).compute(conditional_input(case))


def test_old_declared_price_result_remains_byte_equal_and_requires_no_registry(case, monkeypatch):
    price = price_payload(security=case["spec"]["security"], price="10", as_of="2026-10-16T16:00:00+08:00")
    original = case["conditional"].compute({"spec_ref": case["spec_ref"], "price": price})
    receipt = case["conditional"].save(authorized("calculation", {"spec_ref": case["spec_ref"], "price": price}), submission_id="old-price")
    monkeypatch.setattr("decision_kernel.application.relay_registry.RelayRegistry",
                        lambda *args: pytest.fail("Old conditional results do not depend on a Registry"))
    restored = ConditionalOdds(case["fresh"]()).read(receipt["reference"])
    assert encoded(restored["result"]) == encoded(original)
    assert original["schema"] == "conditional-odds-v1" and "calculation_input" not in original


def test_normal_odds_commands_save_read_and_verify_the_same_retained_input(case):
    payload = authorized("calculation", conditional_input(case))
    receipt = odds_commands.perform(case["store"], "save-odds", payload, "ft-command", registry=REGISTRY)
    fresh = odds_commands.verify_fresh(receipt, "save-odds", case["fresh"], registry=REGISTRY)
    assert fresh["readback"]["status"] == "verified"
    result = odds_commands.perform(case["fresh"](), "read-odds", {"reference": receipt["reference"]}, registry=REGISTRY)
    assert result["result"]["calculation_input"] == payload["calculation"]
    assert result["result"]["market"]["source_requests"] == 0


def test_registered_watch_uses_the_existing_activity_entry_with_ftshare(native_case):
    from decision_kernel.application.watch_activity import WatchActivity
    from test_watch_activity import change, query
    case = native_case
    old, ref = watch(case)
    service = WatchActivity(case["store"], REGISTRY)
    registered = service.change(authorized("change", change(ref, at="2026-10-19T12:06:00+00:00")),
                                submission_id="ft-register")
    active = service.change(authorized("change", change(ref, registered["reference"], "resume",
                                "2026-10-19T12:07:00+00:00")), submission_id="ft-resume")
    payload = query(case["store"], ref, active["reference"], EVALUATED, case["market"])
    saved = service.save_check(authorized("check", payload), submission_id="ft-managed-check")
    read = WatchActivity(case["fresh"](), REGISTRY).read(saved["reference"])
    assert read["event"]["result"]["status"] == "not-met"
    assert read["event"]["result"]["observation"]["calculation"]["price"]["kind"] == "retained-ftshare-raw-close"
    with pytest.raises(RecordError, match="USE_ACTIVITY_ENTRY"):
        old.save(authorized("check", watch_request(case["store"], ref, at=EVALUATED,
                 price=case["market"])), submission_id="ft-managed-bypass")


def test_original_git_source_chain_independently_reopens_odds_and_watch(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "ftshare-odds.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    case = build_case(storage=(backend, store, fresh))
    saved = case["conditional"].save(authorized("calculation", conditional_input(case)), submission_id="git-ft-result")
    probability = case["probability"].save(authorized("calculation", probability_input(case)), submission_id="git-ft-probability")
    service, ref = watch(case)
    checked = service.save(authorized("check", watch_request(store, ref, at=EVALUATED,
                            price=case["market"])), submission_id="git-ft-watch")
    assert ConditionalOdds(fresh(), REGISTRY).read(saved["reference"])["recalculation"] == "matches"
    assert ProbabilityOdds(fresh(), REGISTRY).read(probability["reference"])["recalculation"] == "matches"
    assert OddsWatch(fresh(), REGISTRY).read_check(checked["reference"])["recalculation"] == "matches"


@pytest.mark.parametrize("kind", ["conditional", "probability", "watch"])
def test_ftshare_save_and_independent_readback_share_the_existing_native_budget(tmp_path, monkeypatch, kind):
    from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
    from test_github_store import GitAPI
    from test_market_snapshot import local_fetch
    from test_news_read_budget import transport
    from test_odds_watch_runtime import native_watch_fetch

    model = GitAPI(tmp_path / "budget.git")
    model.git.repository, model.git.binding = model.binding.repository, model.binding
    local = VersionedStore(model.git)
    # Only the source fixture is seeded locally; hosted consumers below use
    # real GitHubStore, its real finite API budget, and native Git verification.
    case = build_case(storage=(model.git, local, lambda: VersionedStore(copy(model.git))))
    if kind == "watch":
        _, reference = watch(case)
        operation = "save-odds-watch-check"
        payload = authorized("check", watch_request(local, reference, at=EVALUATED, price=case["market"]))
    else:
        operation = "save-odds" if kind == "conditional" else "save-probability-odds"
        payload = authorized("calculation", conditional_input(case) if kind == "conditional" else probability_input(case))
    api, fetches, readers, watch_fetches = transport(model), [], [], []
    monkeypatch.setattr("decision_kernel.infrastructure.market_snapshot._fetch_market_branch", local_fetch(model, fetches))
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch", native_watch_fetch(model, watch_fetches))
    writer = VersionedStore(GitHubStore(model.binding, api))

    def fresh():
        reader = VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), api))
        readers.append(reader)
        return reader

    saved = odds_commands.perform(writer, operation, payload, "native-" + kind, registry=REGISTRY)
    verified = odds_commands.verify_fresh(saved, operation, fresh, registry=REGISTRY)
    assert verified["readback"]["status"] == "verified" and verified["reference"] == saved["reference"]
    assert len(readers) == 1 and readers[0].backend is not writer.backend
    assert readers[0].backend.api is writer.backend.api is api
    assert readers[0].backend.binding.write_enabled is False
    assert len(fetches) == 2 and all(call["api"] is api and not call["destination"].exists() for call in fetches)
    if kind == "watch":
        assert watch_fetches and all(call["api"] is api and not call["destination"].exists() for call in watch_fetches)
    assert api._requests == len(model.requests) <= MAX_REQUESTS == 512
    assert model.writes == 1
