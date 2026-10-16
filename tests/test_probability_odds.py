"""Probability policy, exact-source qualification and deterministic custody regressions."""
from copy import deepcopy
from dataclasses import asdict
from decimal import localcontext, ROUND_UP
import json
import pytest
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.market_custody import retain_market_reading, restore_market_query
from decision_kernel.application.probability_odds import ProbabilityOdds, verify_fresh
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.probability_odds import calculate_probability, RESULT_RECORD
from decision_kernel.domain.records import Conflict, RecordError, RevisionInput, SourceInput, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from test_conditional_odds import authorized, spec_payload
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, SyntheticOpener

AS_OF, EVALUATED, CODE = "2026-01-03T03:00:00+00:00", "2026-01-03T04:00:00+00:00", "600519.SH"


def assessment_payload(ref):
    note = {"basis_note": "Synthetic judgment, not real company forecast", "source_indices": [0]}
    return {"schema": "odds-assessment-v1", "assessment_id": "synthetic-p1", "spec_ref": ref,
        "as_of": AS_OF, "author_note": "Synthetic assessment, not human acceptance",
        "probabilities": [{"world_id": "one", "probability": "0.6", **note}, {"world_id": "down", "probability": "0.4", **note}],
        "coverage": {"mutually_exclusive": True, "exhaustive": True, **note},
        "model_risk": {"level": "medium", **note}, "uncertainty": {"level": "elevated", **note},
        "policy": {"policy_id": "synthetic-policy", "as_of": AS_OF, "rationale": "Synthetic thresholds, no default investment policy",
            "minimum_holding_period_days": 1, "maximum_holding_period_days": 2000,
            "max_quote_age_days": 3, "max_master_age_days": 3, "max_capital_age_days": 30,
            "thresholds": [{"zone": z, "minimum_return": r, "minimum_positive_probability": p} for z, r, p in
                [("ACCEPTABLE_ODDS", "0.05", "0.5"), ("ATTRACTIVE_ODDS", "0.2", "0.6"), ("EXCEPTIONAL_ODDS", "0.4", "0.8")]],
            "model_risk_addons": {"low": "0", "medium": "0.02", "high": "0.05", "very-high": "0.1"},
            "uncertainty_addons": {"baseline": "0", "elevated": "0.03", "high": "0.05"}},
        "unresolved_treatment": [{"item": "Unverified economic parameters", "effect": "All numbers synthetic, no economic use"}],
        "limitations": ["Synthetic fixture only"]}


def market_payload(store, fresh, close="100", captured_at="2026-01-03T02:00:00+00:00"):
    def capture(api, fields, rows, scope):
        request = {"api": api, "stage": "baseline", "purpose": "synthetic exact Odds input",
            "parameters": {**scope, "fields": ",".join(fields), "limit": "100"}}
        return list(capture_plan({"schema": "research-source-plan-v1", "case_id": "synthetic-odds-market", "requests": [request]}, REGISTRY,
            client=RelayHTTP(opener=SyntheticOpener(api, fields, rows), credential="synthetic-key-only"), public_rights_confirmed=True,
            policy=RetryPolicy(max_attempts=1, max_wait_seconds=0), clock=lambda: captured_at))[0]
    dates = {"start_date": "20260102", "end_date": "20260102"}
    payload = {"schema": "relay-security-stock-reading-v1", "exchange": "SSE", "start": "20260102", "end": "20260102", "horizons": [1],
        "calendar_result": capture("trade_cal", ["exchange", "cal_date", "is_open"], [["SSE", "20260102", 1]], {"exchange": "SSE", **dates}),
        "stock_results": {CODE: capture("daily", ["ts_code", "trade_date", "close"], [[CODE, "20260102", close]], {"ts_code": CODE, **dates})},
        "master_results": {"L": capture("stock_basic", ["ts_code", "symbol", "name", "exchange", "curr_type", "list_status", "list_date", "delist_date"],
            [[CODE, CODE[:6], "合成证券", "SSE", "CNY", "L", "20000101", None]], {"exchange": "SSE", "list_status": "L"})}, "factor_results": {}}
    receipt = retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    manifest = json.loads(store.read_source(SourceRef.from_dict(receipt["reference"]))["data"])
    refs = {restore_market_query(ref, REGISTRY, store)["result"]["use"]["api"]: ref for ref in manifest["queries"].values()}
    return {"schema": "odds-retained-close-v1", "price_id": "synthetic-close-" + close.replace('.', '-'),
        "calendar_ref": refs["trade_cal"], "daily_ref": refs["daily"], "master_ref": refs["stock_basic"],
        "exchange": "SSE", "start": "20260102", "end": "20260102"}


def build_context(store, fresh):
    source = store.put_source("source", SourceInput("s", "content", "Synthetic basis", "synthetic:odds", "Synthetic economic basis"))
    revision = store.submit_revision("r", RevisionInput("case", "r1", "full", "Synthetic research", "Synthetic", "synthetic", (source,),
                                                       unresolved=("Unverified economic parameters",)))
    spec = spec_payload(asdict(revision)); spec["security"] = {"code": CODE, "share_class": "ordinary-a"}
    down = deepcopy(spec["worlds"][0]); down.update(id="down", name="Down", value="5000", distributions=[]); spec["worlds"].append(down)
    frozen = ConditionalOdds(store).freeze(authorized("spec", spec), submission_id="spec")
    return store, ProbabilityOdds(store, REGISTRY), spec, assessment_payload(frozen["reference"]), market_payload(store, fresh)


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    backend = MemoryBackend(); store = VersionedStore(backend)
    return build_context(store, lambda: VersionedStore(MemoryBackend(backend.shared)))


def calculation(ctx):
    store, service, spec, assessment, market = ctx
    ref = service.freeze(authorized("assessment", assessment), submission_id="assessment")["reference"]
    return {"assessment_ref": ref, "market": market, "evaluated_at": EVALUATED}


def test_weighted_arithmetic_and_risk_thresholds(ctx):
    _, service, *_ = ctx
    with localcontext() as decimal:
        decimal.prec = 3; decimal.rounding = ROUND_UP
        result = service.compute(calculation(ctx))
        assert decimal.prec == 3 and decimal.rounding == ROUND_UP
    assert result["aggregate"]["weighted_total_payoff_per_share"] == "95"
    assert result["aggregate"]["expected_holding_period_return"] == "-0.05"
    assert result["aggregate"]["positive_return_scenario_probability"] == "0.6"
    assert result["adjusted_thresholds"][0]["required_return"] == "0.1"
    assert result["participation_zone"] == "INSUFFICIENT_ODDS"
    assert result["market"]["source_requests"] == 0 and not result["is_annualized"] and result["investment_authority"] == "none"


@pytest.mark.parametrize("field,value", [("probability", "0.6000001"), ("probability", "0.5999999"), ("probability", "-0.1"),
    ("probability", "1.1"), ("probability", "NaN"), ("probability", 0.6), ("source_indices", []), ("basis_note", ""), ("world_id", "different")])
def test_bad_probabilities_refuse_without_write(ctx, field, value):
    store, service, _, assessment, _ = ctx; assessment["probabilities"][0][field] = value; head = store.backend.head()
    with pytest.raises(RecordError): service.freeze(authorized("assessment", assessment), submission_id="bad")
    assert store.backend.head() == head


@pytest.mark.parametrize("mutation", ["duplicate", "coverage", "risk", "policy", "missing-question", "wrong-source", "past-clock"])
def test_no_missing_qualification(ctx, mutation):
    store, service, _, a, _ = ctx
    if mutation == "duplicate": a["probabilities"][1]["world_id"] = "one"
    elif mutation == "coverage": a["coverage"]["exhaustive"] = False
    elif mutation == "risk": a["model_risk"]["level"] = "not-established"
    elif mutation == "policy": a["policy"]["model_risk_addons"]["low"] = "1"
    elif mutation == "missing-question": a["unresolved_treatment"] = []
    elif mutation == "wrong-source": a["probabilities"][0]["source_indices"] = [1]
    else: a["as_of"] = "2025-12-01T00:00:00Z"
    with pytest.raises(RecordError): service.freeze(authorized("assessment", a), submission_id="bad")


@pytest.mark.parametrize("key,value,code", [("evaluated_at", "2026-01-03T01:00:00Z", "PRECEDES_ASSESSMENT"),
    ("evaluated_at", "2999-01-01T00:00:00Z", "EVALUATION_IN_FUTURE"), ("evaluated_at", "2026-01-07T04:00:00Z", "NOT_FINISHED_OR_STALE"),
    ("market", {"kind": "qualified-market", "price": "100"}, "INPUT_SHAPE")])
def test_temporal_or_self_certified_market_shortcuts_refused(ctx, key, value, code):
    store, service, *_ = ctx; payload = calculation(ctx); payload[key] = value; head = store.backend.head()
    with pytest.raises(RecordError, match=code): service.compute(payload)
    assert store.backend.head() == head


def test_two_source_prices_keep_one_frozen_assessment(ctx):
    store, service, *_ = ctx; payload = calculation(ctx); original = store.current("case")
    first = service.save(authorized("calculation", payload), submission_id="result1")
    second_payload = deepcopy(payload)
    second_payload["market"] = market_payload(store, lambda: VersionedStore(MemoryBackend(store.backend.shared)), close="80")
    second = service.save(authorized("calculation", second_payload), submission_id="result2")
    a, b = service.read(first["reference"]), service.read(second["reference"])
    assert a["result"]["assessment_sha256"] == b["result"]["assessment_sha256"]
    assert b["result"]["aggregate"]["expected_holding_period_return"] == "0.1875"
    assert b["result"]["participation_zone"] == "ACCEPTABLE_ODDS"
    assert a["result"]["policy"] == b["result"]["policy"] and store.current("case") == original
    assert service.save(authorized("calculation", payload), submission_id="result1") == first
    with pytest.raises(Conflict): service.save(authorized("calculation", second_payload), submission_id="result1")


def test_breakeven_not_positive_and_risk_never_changes_probability(ctx):
    _, _, spec, assessment, _ = ctx
    price = {"security": spec["security"], "currency": "CNY", "share_count": "100", "price": "125", "as_of": "2026-01-02T23:59:59+08:00"}
    a = calculate_probability(spec, assessment, price); assert a["aggregate"]["positive_return_scenario_probability"] == "0"
    assessment["model_risk"]["level"] = "very-high"; b = calculate_probability(spec, assessment, price)
    assert a["aggregate"] == b["aggregate"] and a["adjusted_thresholds"] != b["adjusted_thresholds"]


def test_hash_does_not_certify_calculation(ctx):
    _, service, *_ = ctx; out = service.compute(calculation(ctx)); out["aggregate"]["expected_holding_period_return"] = "8"
    ref = service.conditional._put("tampered", RESULT_RECORD, out, "Synthetic tampering test")
    with pytest.raises(RecordError, match="RECALCULATION_MISMATCH"): service.read(asdict(ref))


def test_current_fair_value_is_not_future_probability_return(ctx):
    store, service, spec, a, _ = ctx
    spec.update(value_kind="current-fair-value", method="equity-value-distance-v1", target_date=None, cash_policy="included-in-fair-value")
    for world in spec["worlds"]: world["distributions"] = []
    a["spec_ref"] = ConditionalOdds(store).freeze(authorized("spec", spec), submission_id="current-spec")["reference"]
    with pytest.raises(RecordError, match="REQUIRES_FUTURE_PAYOFF"): service.freeze(authorized("assessment", a), submission_id="current-assessment")


def test_unfinished_day_and_corrupt_source_refuse(ctx):
    store, service, *_ = ctx; payload = calculation(ctx)
    payload["market"] = market_payload(store, lambda: VersionedStore(MemoryBackend(store.backend.shared)), captured_at="2026-01-02T06:00:00Z")
    with pytest.raises(RecordError, match="CAPTURE_DAY_NOT_FINISHED"): service.compute(payload)
    payload["market"]["daily_ref"]["manifest"]["sha256"] = "0" * 64
    with pytest.raises(RecordError): service.compute(payload)


def test_public_rights_and_readback_failure_remain_distinct(ctx):
    _, service, _, assessment, _ = ctx; payload = authorized("assessment", assessment); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="PUBLICATION_RIGHTS"): service.freeze(payload, submission_id="bad")
    frozen = service.freeze(authorized("assessment", assessment), submission_id="assessment")
    def failed(): raise RecordError("UNAVAILABLE")
    out = verify_fresh(frozen, "freeze-odds-assessment", failed)
    assert out["reference"] == frozen["reference"] and out["readback"]["status"] == "failed"


def test_real_git_and_historical_reconstruction(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    backend = GitStore.init_synthetic(tmp_path / "probability.git"); store = VersionedStore(backend)
    context = build_context(store, lambda: VersionedStore(GitStore(backend.root)))
    result = context[1].save(authorized("calculation", calculation(context)), submission_id="saved")
    fresh = ProbabilityOdds(VersionedStore(GitStore(backend.root)), REGISTRY)
    assert fresh.read(result["reference"])["recalculation"] == "matches"
    original = store.current("case")
    store.submit_revision("r2", RevisionInput("case", "r2", "full", "Synthetic changed", "Synthetic", "synthetic", predecessor=original, unresolved=("Synthetic change is not a source",)))
    assert fresh.read(result["reference"])["current_use"]["status"] == "historical-research-not-current"


def test_result_is_detached_canonical_json_and_hint_failure_is_local(ctx, monkeypatch):
    from decision_kernel.domain.records import MissingObject, encoded
    _, service, *_ = ctx
    payload = calculation(ctx)
    result = service.compute(payload)
    assert result == json.loads(encoded(result))
    payload["market"]["price_id"] = "changed-by-caller"
    assert result["calculation_input"]["market"]["price_id"] != "changed-by-caller"
    saved = service.save(authorized("calculation", calculation(ctx)), submission_id="saved-hint")
    def unavailable(_): raise MissingObject("CURRENT_UNAVAILABLE")
    monkeypatch.setattr(service.store, "current", unavailable)
    restored = service.read(saved["reference"])
    assert restored["recalculation"] == "matches" and restored["current_use"]["status"] == "unknown"


@pytest.mark.parametrize("part", ["calendar_ref", "daily_ref", "master_ref"])
def test_wrong_source_family_or_registry_never_becomes_qualified(ctx, part):
    _, service, *_ = ctx
    payload = calculation(ctx)
    other = "master_ref" if part != "master_ref" else "daily_ref"
    payload["market"][part] = payload["market"][other]
    with pytest.raises(RecordError): service.compute(payload)


def test_risk_policy_horizon_and_listing_age_must_be_explicit(ctx):
    _, service, _, assessment, _ = ctx
    assessment["policy"]["maximum_holding_period_days"] = 2
    with pytest.raises(RecordError, match="HORIZON_NOT_COVERED"): service.compute(calculation(ctx))


def test_registry_change_requires_original_registry_not_silent_requalification(ctx, monkeypatch):
    _, service, *_ = ctx
    payload = calculation(ctx)
    monkeypatch.setattr(REGISTRY, "snapshot", {"changed": True})
    with pytest.raises(RecordError, match="REGISTRY_CHANGED"): service.compute(payload)
