"""Probability-free arithmetic and exact custody, using synthetic real Git only."""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, localcontext, ROUND_UP
import json

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds, verify_fresh
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.conditional_odds import calculate, validate_spec
from decision_kernel.domain.records import BlobRef, Conflict, RecordError, RevisionInput, SourceInput
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import ROOT


def spec_payload(ref, **changes):
    return {"schema": "valuation-spec-v1", "spec_id": "synthetic-v1", "research_ref": ref,
        "security": {"code": "TEST.SZ", "share_class": "A"}, "currency": "CNY",
        "value_kind": "future-exit", "valuation_date": "2026-01-01", "target_date": "2028-12-31",
        "share_count": "100", "capital_structure": {"as_of": "2026-01-01", "assumption": "Fixed synthetic equity; no new debt or shares"},
        "method": "nominal-holding-return-v1", "cash_policy": "terminal-excludes-distributions",
        "probability_status": "not-established", "allowed_changes": ["price-context"],
        "limitations": ["Synthetic conditions, not a forecast"],
        "worlds": [{"id": "one", "name": "One", "value": "12000", "basis": "total-equity",
            "conditions": ["Synthetic cash outcome"], "basis_note": "Declared assumption, not a disclosed valuation",
            "source_indices": [0], "distributions": [{"id": "d1", "date": "2027-06-01", "amount": "500",
                "basis": "total-equity", "currency": "CNY", "kind": "dividend"}]}], **changes}


def price_payload(**changes):
    return {"schema": "odds-price-v1", "price_id": "p1", "kind": "declared-reference",
        "security": {"code": "TEST.SZ", "share_class": "A"}, "currency": "CNY",
        "share_count": "100", "price": "100", "as_of": "2026-01-02T16:00:00+08:00",
        "note": "Synthetic reference, not a real human quote or qualified market", "evidence": None, **changes}


def authorized(key, value):
    return {key: value, "rights_confirmed": True, "rights_basis": "Synthetic engineering fixture"}


@pytest.fixture
def context(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "odds.git")
    store = VersionedStore(backend)
    source = store.put_source("source", SourceInput("s", "content", "Synthetic source", "synthetic:test", "Evidence not valuation"))
    revision = store.submit_revision("research", RevisionInput("case", "r1", "full", "Synthetic partial research",
        "Synthetic commission", "synthetic-method", (source,), unresolved=("Economic assumptions unverified",)))
    return store, revision


@pytest.fixture
def spec(context):
    return spec_payload(asdict(context[1]))


def test_old_conditional_arithmetic_two_prices_and_independent_decimal_context(spec):
    with localcontext() as ctx:
        ctx.prec = 3; ctx.rounding = ROUND_UP
        first = calculate(spec, price_payload())
        second = calculate(spec, price_payload(price="80", price_id="p2"))
        assert ctx.prec == 3 and ctx.rounding == ROUND_UP
    a, b = first["world_results"][0], second["world_results"][0]
    assert a["equity_value_per_share"] == "120" and a["total_distributions_per_share"] == "5"
    assert a["total_payoff_per_share"] == b["total_payoff_per_share"] == "125"
    assert a["undiscounted_holding_period_return"] == "0.25"
    assert b["undiscounted_holding_period_return"] == "0.5625"
    assert first["weighted_aggregate"] is None and not first["is_annualized"]
    assert first["market_qualification"] == "not-established" and first["investment_authority"] == "none"


def test_present_fair_value_never_becomes_future_return(spec):
    spec.update(value_kind="current-fair-value", method="equity-value-distance-v1", target_date=None, cash_policy="included-in-fair-value")
    spec["worlds"][0]["distributions"] = []
    result = calculate(spec, price_payload())
    row = result["world_results"][0]
    assert row["value_to_price_distance"] == "0.2"
    assert "undiscounted_holding_period_return" not in row and "total_payoff_per_share" not in row
    assert "NEW_PRICE_DOES_NOT_REFRESH_VALUATION_OR_RESEARCH" in result["limitations"]


def test_per_share_and_equity_total_are_equivalent(spec):
    per_share = deepcopy(spec)
    per_share["worlds"][0].update(value="120", basis="per-share")
    per_share["worlds"][0]["distributions"][0].update(amount="5", basis="per-share")
    assert calculate(per_share, price_payload()) == calculate(spec, price_payload())


def test_zero_equity_and_retrospective_limit_are_explicit(spec):
    spec["worlds"][0].update(value="0", distributions=[])
    result = calculate(spec, price_payload(as_of="2025-12-01T08:00:00Z"))
    assert result["world_results"][0]["undiscounted_holding_period_return"] == "-1"
    assert "PRE_VALUATION_REFERENCE_NOT_POINT_IN_TIME_ANALYSIS" in result["limitations"]


@pytest.mark.parametrize("value", [0, 1.2, True, None, "NaN", "Infinity", "1e8", "1,000", "-1", "0", "9"*49, "0."+"0"*33+"1"])
def test_invalid_or_nonpositive_price_is_rejected(spec, value):
    with pytest.raises(RecordError):
        calculate(spec, price_payload(price=value))


@pytest.mark.parametrize("change,code", [
    ({"currency": "USD"}, "FX_NOT_SUPPORTED"),
    ({"security": {"code": "OTHER.SZ", "share_class": "A"}}, "SECURITY_SHARE_CLASS_MISMATCH"),
    ({"security": {"code": "TEST.SZ", "share_class": "H"}}, "SECURITY_SHARE_CLASS_MISMATCH"),
    ({"share_count": "101"}, "CAPITAL_CHANGE_REQUIRES_NEW_SPEC"),
    ({"kind": "qualified-market"}, "PRICE_KIND_NOT_SUPPORTED"),
    ({"kind": "relay-observation"}, "RELAY_EVIDENCE_REQUIRED"),
    ({"as_of": "2028-12-31T08:00:00Z"}, "TARGET_NOT_AFTER_PRICE"),
    ({"as_of": "2027-06-01T08:00:00Z"}, "PAST_DISTRIBUTION_REQUIRES_NEW_SPEC"),
    ({"as_of": "2026-01-01T08:00:00"}, "AWARE_PRICE_TIME_REQUIRED"),
    ({"as_of": "2026-02-30T08:00:00Z"}, "AWARE_PRICE_TIME_REQUIRED"),
])
def test_price_only_cannot_change_other_economic_inputs(spec, change, code):
    with pytest.raises(RecordError, match=code):
        calculate(spec, price_payload(**change))


@pytest.mark.parametrize("field,value", [
    ("probability_status", "equal-weight"), ("probabilities", [1]), ("allowed_changes", ["share_count"]),
    ("method", "eval"), ("target_date", "2025-12-31"), ("share_count", "0"),
    ("cash_policy", "terminal-includes-distributions"), ("value_kind", "enterprise-value"),
])
def test_unsupported_spec_cannot_gain_calculation_qualification(spec, field, value):
    spec[field] = value
    with pytest.raises(RecordError):
        validate_spec(spec)


def test_duplicate_cash_and_cross_currency_are_rejected(spec):
    flow = spec["worlds"][0]["distributions"][0]
    spec["worlds"][0]["distributions"].append(deepcopy(flow))
    with pytest.raises(RecordError, match="DUPLICATE_CASHFLOW"):
        validate_spec(spec)
    spec["worlds"][0]["distributions"].pop()
    flow["currency"] = "USD"
    with pytest.raises(RecordError, match="FX_NOT_SUPPORTED"):
        validate_spec(spec)


def test_no_cash_in_present_value_and_no_negative_capital_disguised_as_dividend(spec):
    spec["worlds"][0]["distributions"][0]["amount"] = "-1"
    with pytest.raises(RecordError, match="CAPITAL_CALL"):
        validate_spec(spec)
    spec["worlds"][0]["distributions"][0]["amount"] = "1"
    spec.update(value_kind="current-fair-value", method="equity-value-distance-v1", target_date=None, cash_policy="included-in-fair-value")
    with pytest.raises(RecordError, match="NOT_FUTURE_PAYOFF"):
        validate_spec(spec)


def test_frozen_spec_and_two_results_do_not_change_research(context, spec):
    store, ref = context
    service = ConditionalOdds(store)
    original_body = store.read_revision(ref)["body"]
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    original_hash = frozen["spec_sha256"]
    first = service.save(authorized("calculation", {"spec_ref": frozen["reference"], "price": price_payload()}), submission_id="result1")
    second = service.save(authorized("calculation", {"spec_ref": frozen["reference"], "price": price_payload(price="80", price_id="p2")}), submission_id="result2")
    fresh = ConditionalOdds(VersionedStore(GitStore(store.backend.root)))
    a, b = fresh.read(first["reference"]), fresh.read(second["reference"])
    assert a["recalculation"] == b["recalculation"] == "matches"
    assert a["result"]["spec_sha256"] == b["result"]["spec_sha256"] == original_hash
    assert a["result"]["world_results"] != b["result"]["world_results"]
    assert store.current("case") == ref and store.read_revision(ref)["body"] == original_body
    assert first["reference"] != second["reference"]
    assert service.freeze(authorized("spec", spec), submission_id="freeze") == frozen
    modified = deepcopy(spec); modified["worlds"][0]["value"] = "20000"
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        service.freeze(authorized("spec", modified), submission_id="freeze")


def test_historical_calculation_readable_but_not_mislabelled_current(context, spec):
    store, ref = context; service = ConditionalOdds(store)
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    saved = service.save(authorized("calculation", {"spec_ref": frozen["reference"], "price": price_payload()}), submission_id="result")
    old = service.read(saved["reference"])
    store.submit_revision("next", RevisionInput("case", "r2", "full", "New analysis", "Synthetic commission",
        "synthetic-method", predecessor=ref, unresolved=("Still not accepted",)))
    new = service.read(saved["reference"])
    assert new["result"] == old["result"]
    assert new["current_use"]["status"] == "historical-research-not-current"


def test_price_source_value_security_and_date_are_checked(context, spec):
    store, _ = context; service = ConditionalOdds(store)
    price_ref = store.put_source("quote", SourceInput("quote", "dataset", "Synthetic Relay quote", ROOT+"/daily",
        "ts_code,trade_date,close\nTEST.SZ,20260102,100\n", custody="excerpt", limitations=("Synthetic only",)))
    price = price_payload(kind="relay-observation", evidence={"source": asdict(price_ref),
        "value_locator": {"row": 1, "column": "close"}, "security_locator": {"row": 1, "column": "ts_code"},
        "date_locator": {"row": 1, "column": "trade_date"}})
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    result = service.compute({"spec_ref": frozen["reference"], "price": price})
    assert result["price_check"]["status"] == "stored-price-security-date-match"
    assert result["market_qualification"] == "not-established"
    for field, value, code in [("price", "99", "SOURCE_PRICE_MISMATCH"), ("as_of", "2026-01-03T00:00:00Z", "SOURCE_PRICE_DATE_MISMATCH")]:
        bad = deepcopy(price); bad[field] = value
        with pytest.raises(RecordError, match=code):
            service.compute({"spec_ref": frozen["reference"], "price": bad})


def test_cited_source_outside_exact_research_and_unconfirmed_publication_refused(context, spec):
    store, _ = context; service = ConditionalOdds(store)
    initial = store.backend.head()
    spec["worlds"][0]["source_indices"] = [1]
    with pytest.raises(RecordError, match="SOURCE_OUTSIDE_FROZEN"):
        service.freeze(authorized("spec", spec), submission_id="bad")
    spec["worlds"][0]["source_indices"] = [0]
    payload = authorized("spec", spec); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="PUBLICATION_RIGHTS_REQUIRED"):
        service.freeze(payload, submission_id="bad")
    assert store.backend.head() == initial


def test_arbitrary_source_instructions_do_not_execute(context, spec, tmp_path):
    store, _ = context; service = ConditionalOdds(store)
    spec["worlds"][0]["basis_note"] = "__import__('os').system('touch SHOULD_NOT_EXIST')"
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    assert service.read_spec(frozen["reference"])["spec"]["worlds"][0]["basis_note"].startswith("__import__")
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


def test_bad_reference_and_fresh_failure_preserve_known_receipt(context, spec):
    store, _ = context; service = ConditionalOdds(store)
    frozen = service.freeze(authorized("spec", spec), submission_id="freeze")
    bad = dict(frozen["reference"], sha256="0"*64)
    with pytest.raises(RecordError):
        service.read_spec(bad)
    def failed():
        raise RecordError("UNAVAILABLE")
    receipt = verify_fresh(frozen, "freeze-valuation", failed)
    assert receipt["reference"] == frozen["reference"] and receipt["readback"]["status"] == "failed"
