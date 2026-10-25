"""Two retained source adapters through the same probability-free consumers.

All data, companies and rights statements are synthetic. Existing capture and
custody fixtures supply original representations; these tests make no live calls
and do not qualify a new production source or change an existing Watch policy.
"""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD, verify_fresh
from decision_kernel.application.market_custody import retain_market_query
from decision_kernel.application.odds_book import read_book
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, SourceInput, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_conditional_odds import authorized, price_payload
from test_ftshare_odds import CODE as FTSHARE_CODE, EVALUATED as FTSHARE_EVALUATED
from test_ftshare_odds import build_case, conditional_input, watch
from test_market_relay_inputs import REGISTRY
from test_market_session_coverage import SessionOpener, capture
from test_odds_watch import request as watch_request
from test_probability_odds import AS_OF, EVALUATED, build_context, market_payload
from test_reading_odds_book import html_model


def close_policy(as_of=AS_OF):
    return {"policy_id": "synthetic-conditional-close", "as_of": as_of,
        "rationale": "Synthetic dated-close comparison, not current executable prices",
        "max_quote_age_days": 3, "max_master_age_days": 3, "max_capital_age_days": 30}


@pytest.fixture
def relay_case(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now",
                        lambda: "2026-01-03T02:30:00+00:00")
    backend = GitStore.init_synthetic(tmp_path / "retained-conditional.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    _, _, spec, assessment, market = build_context(store, fresh)
    return {"store": store, "fresh": fresh, "spec": spec, "market": market,
        "service": ConditionalOdds(store, REGISTRY), "input": {"spec_ref": assessment["spec_ref"],
            "market": market, "market_policy": close_policy(), "evaluated_at": EVALUATED}}


def same_day_relay(case):
    """Reuse the existing capture/retention owners for the FTShare fixture date."""
    opener = SessionOpener(count=1)
    opener.rows["daily", None] = [{"ts_code": FTSHARE_CODE, "trade_date": "20261016", "close": "10"}]
    dates = {"start_date": "20261016", "end_date": "20261016"}
    refs = {}
    queries = (
        ("calendar", "trade_cal", {"exchange": "SSE", **dates}, {"exchange", "cal_date", "is_open"}),
        ("daily", "daily", {"ts_code": FTSHARE_CODE, **dates}, {"ts_code", "trade_date", "close"}),
        ("master", "stock_basic", {"exchange": "SSE", "list_status": "L"},
            {"ts_code", "symbol", "name", "exchange", "curr_type", "list_status", "list_date", "delist_date"}),
    )
    for name, api, parameters, fields in queries:
        result = capture(opener, api, parameters, fields)
        refs[name + "_ref"] = retain_market_query(result, REGISTRY, case["store"],
            fresh_reader=case["fresh"], custody_authorized=True)["reference"]
    return {"schema": "odds-retained-close-v1", "price_id": "same-day-retained-relay",
        **refs, "exchange": "SSE", "start": "20261016", "end": "20261016"}


def test_same_frozen_spec_and_raw_close_have_equal_arithmetic_with_distinct_source_clocks(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now",
                        lambda: "2026-10-19T12:03:00+00:00")
    backend = GitStore.init_synthetic(tmp_path / "both-retained-sources.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    case = build_case(storage=(backend, store, fresh))
    relay = same_day_relay(case)
    head = backend.head()
    ft = case["conditional"].compute(conditional_input(case))
    other = case["conditional"].compute(conditional_input(case, relay))
    assert ft["spec_ref"] == other["spec_ref"] == case["spec_ref"]
    assert ft["research_ref"] == other["research_ref"] == asdict(case["research_ref"])
    assert ft["spec_sha256"] == other["spec_sha256"]
    assert ft["world_results"] == other["world_results"]
    assert ft["price"]["price"] == other["price"]["price"] == "10"
    assert ft["price"]["kind"] == "retained-ftshare-raw-close"
    assert other["price"]["kind"] == "retained-relay-raw-close"
    assert ft["price"]["as_of"] == "2026-10-16T18:35:00+08:00"
    assert other["price"]["as_of"] == "2026-10-16T23:59:59+08:00"
    assert ft["price"]["timestamp_semantics"] != other["price"]["timestamp_semantics"]
    assert set(ft["market"]["source_references"]) == {"collection", "calendar", "master", "daily"}
    assert set(other["market"]["source_references"]) == {"calendar", "master", "daily"}
    assert ft["market"]["source_references"]["daily"] != other["market"]["source_references"]["daily"]
    for result in (ft, other):
        assert result["market"]["source_requests"] == 0
        assert result["probability_status"] == "not-established" and result["weighted_aggregate"] is None
        assert result["human_acceptance"] == "not-confirmed" and result["investment_authority"] == "none"
        assert "PRICE_CONTEXT_DOES_NOT_ESTABLISH_HISTORICAL_PIT" in result["limitations"]
    assert backend.head() == head

    # Expanding standalone conditional calculation must not change the source
    # explicitly frozen by an existing FTShare Watch.
    service, reference = watch(case)
    head = backend.head()
    out = service.check(watch_request(store, reference, at=FTSHARE_EVALUATED, price=relay))
    assert out["status"] == "cannot-determine" and out["condition_met"] is None
    assert out["observation"]["reason"] == "WATCH_PRICE_QUALIFICATION_MISMATCH"
    assert out["observation"]["calculation"] is None
    assert backend.head() == head


def test_relay_save_independent_replay_and_original_intent_remain_on_existing_store(relay_case):
    case = relay_case
    store, service, calculation = case["store"], case["service"], case["input"]
    original_research = store.current("case")
    first = service.save(authorized("calculation", calculation), submission_id="retained-first")
    assert first["price_evidence"] == "fixed-slice-verified"
    assert verify_fresh(first, "save-odds", case["fresh"], REGISTRY)["readback"]["status"] == "verified"
    initial = ConditionalOdds(case["fresh"](), REGISTRY).read(first["reference"])
    assert initial["recalculation"] == "matches"
    assert initial["result"]["price"]["kind"] == "retained-relay-raw-close"
    head = store.backend.head()
    assert service.save(authorized("calculation", calculation), submission_id="retained-first") == first
    assert store.backend.head() == head

    updated = deepcopy(calculation)
    updated["market"] = market_payload(store, case["fresh"], close="80")
    second = service.save(authorized("calculation", updated), submission_id="retained-second")
    later = ConditionalOdds(case["fresh"](), REGISTRY).read(second["reference"])["result"]
    assert later["spec_sha256"] == initial["result"]["spec_sha256"]
    assert later["price"]["price"] == "80" and initial["result"]["price"]["price"] == "100"
    assert later["world_results"][1]["undiscounted_holding_period_return"] == "0.5625"
    assert ConditionalOdds(case["fresh"](), REGISTRY).read(first["reference"]) == initial
    with pytest.raises(Conflict):
        service.save(authorized("calculation", updated), submission_id="retained-first")
    assert store.current("case") == original_research


def test_book_room_and_exact_open_keep_relay_result_without_requalification(relay_case, monkeypatch):
    case = relay_case
    saved = case["service"].save(authorized("calculation", case["input"]), submission_id="book-relay")
    original = ConditionalOdds(case["fresh"](), REGISTRY).read(saved["reference"])["result"]
    head = case["store"].backend.head()
    query = {"limit": 8, "scan_limit": 32}
    with monkeypatch.context() as patch:
        patch.setattr(ConditionalOdds, "compute", lambda *a, **kw: pytest.fail("Book/Room must not recalculate"))
        book = read_book(case["fresh"](), query)
        result = reading_commands.perform(case["fresh"](), {"plan": {
            "as_of": EVALUATED, "odds_book": query}}, public=False)
    row = next(item for item in book["items"] if item["reference"] == saved["reference"])
    assert row["status"] == "readable" and row["recorded"] == original
    assert row["qualification"] == "stored-declarations-only" and row["current_validity"] == "not-certified"
    assert result["odds_book"]["view"] == book
    assert result["catalogue"]["snapshot"] == book["snapshot"] == head
    assert html_model(render_page(result)) == result
    assert row["open_operation"] == "read-odds"
    assert ConditionalOdds(case["fresh"](), REGISTRY).read(row["open_payload"]["reference"])["result"] == original
    assert case["store"].backend.head() == head


@pytest.mark.parametrize("damage", ["kind", "schema", "unknown-schema", "price-id"])
def test_book_refuses_hash_valid_source_kind_or_input_identity_mismatch(relay_case, damage):
    case = relay_case
    result = case["service"].compute(case["input"])
    if damage == "kind":
        result["price"]["kind"] = "retained-ftshare-raw-close"
    elif damage == "schema":
        result["calculation_input"]["market"]["schema"] = "odds-ftshare-close-v1"
    elif damage == "unknown-schema":
        result["calculation_input"]["market"]["schema"] = "caller-approved-close-v1"
    else:
        result["calculation_input"]["market"]["price_id"] = "other-price"
    bad = asdict(case["service"]._put("mismatched", RESULT_RECORD, result, "Synthetic forged output"))
    row = next(item for item in read_book(case["store"], {"limit": 8, "scan_limit": 32})["items"]
               if item["reference"] == bad)
    assert row["status"] == "invalid" and row["reason"] == "ODDS_BOOK_RESULT_INPUT_MISMATCH"


@pytest.mark.parametrize("change,error", [
    ({"schema": "caller-qualified-close-v1"}, "ODDS_MARKET_ADAPTER_UNSUPPORTED"),
    ({"schema": ["odds-retained-close-v1"]}, "ODDS_MARKET_ADAPTER_UNSUPPORTED"),
    ({"qualified": True}, "ODDS_INPUT_SHAPE"),
    ({"price": "100"}, "ODDS_INPUT_SHAPE"),
    ({"adjust_kind": "forward"}, "ODDS_INPUT_SHAPE"),
])
def test_caller_schema_price_and_qualification_cannot_replace_retained_evidence(relay_case, change, error):
    case = relay_case
    calculation = deepcopy(case["input"])
    calculation["market"].update(change)
    head = case["store"].backend.head()
    with pytest.raises(RecordError, match=error):
        case["service"].save(authorized("calculation", calculation), submission_id="invalid")
    assert case["store"].backend.head() == head


@pytest.mark.parametrize("mode,error", [
    ("stale-quote", "ODDS_CLOSE_DAY_NOT_FINISHED_OR_STALE"),
    ("capture-after-evaluation", "ODDS_MARKET_CAPTURE_AFTER_EVALUATION"),
    ("unfinished-capture", "ODDS_CLOSE_CAPTURE_DAY_NOT_FINISHED"),
    ("future-policy", "ODDS_POLICY_AFTER_EVALUATION"),
    ("foreign-security", "MARKET_RELAY_QUERY_SCOPE"),
    ("adjusted-share-class", "ODDS_MARKET_SHARE_CLASS_UNSUPPORTED"),
])
def test_original_source_time_identity_and_use_qualifications_still_apply(relay_case, mode, error):
    case = relay_case
    calculation = deepcopy(case["input"])
    if mode == "stale-quote":
        calculation["evaluated_at"] = "2026-01-07T04:00:00Z"
    elif mode in ("capture-after-evaluation", "unfinished-capture"):
        captured = "2026-01-03T05:00:00Z" if mode == "capture-after-evaluation" else "2026-01-02T06:00:00Z"
        calculation["market"] = market_payload(case["store"], case["fresh"], captured_at=captured)
    elif mode == "future-policy":
        calculation["market_policy"]["as_of"] = "2026-01-04T00:00:00Z"
    else:
        spec = deepcopy(case["spec"])
        spec["security"] = {"code": "600000.SH" if mode == "foreign-security" else "600519.SH",
                            "share_class": "ordinary-a" if mode == "foreign-security" else "preferred"}
        calculation["spec_ref"] = case["service"].freeze(authorized("spec", spec), submission_id="other-spec")["reference"]
    head = case["store"].backend.head()
    with pytest.raises(RecordError, match=error):
        case["service"].save(authorized("calculation", calculation), submission_id="ineligible")
    assert case["store"].backend.head() == head


def test_source_corruption_or_pasted_qualified_representation_cannot_produce_a_result(relay_case):
    case = relay_case
    forged = case["store"].put_source("pasted-qualification", SourceInput(
        "pasted-market", "content", "Synthetic pasted qualification", "synthetic:caller",
        encoded({"status": "qualified-retained-close-for-declared-as-of", "price": "100"}).decode()))
    for reference in (asdict(forged), {**deepcopy(case["market"]["daily_ref"]), "manifest": {
            **case["market"]["daily_ref"]["manifest"], "sha256": "0" * 64}}):
        calculation = deepcopy(case["input"])
        calculation["market"]["daily_ref"] = reference
        head = case["store"].backend.head()
        with pytest.raises(RecordError):
            case["service"].save(authorized("calculation", calculation), submission_id="bad-source")
        assert case["store"].backend.head() == head


def test_qualified_price_cannot_bypass_market_custody_via_original_price_input(relay_case):
    case = relay_case
    price = price_payload(security=case["spec"]["security"], kind="retained-relay-raw-close")
    head = case["store"].backend.head()
    with pytest.raises(RecordError, match="ODDS_PRICE_KIND_NOT_SUPPORTED"):
        case["service"].compute({"spec_ref": case["input"]["spec_ref"], "price": price})
    assert case["store"].backend.head() == head
