"""Explicit probability/required-return policy over the existing conditional engine.

The weighted sums and risk-additive zone classification are narrowly extracted
from decision-kernel@cb3bbc87 calculation.py and odds.py. No probability fitting,
policy defaults, recommendation, or general model execution is performed here.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

from decision_kernel.domain.conditional_odds import (
    CONTEXT, ONE, ZERO, day, decimal_text, number, shape, timestamp, validate_spec, world_results,
)
from decision_kernel.domain.records import checked_text, encoded, identifier, require
from decision_kernel.domain.research_artifacts import artifact_reference

ASSESSMENT_RECORD = "frozen-odds-assessment-record-v1"
RESULT_RECORD = "probability-odds-record-v1"
LEVELS = ("low", "medium", "high", "very-high")
UNCERTAINTIES = ("baseline", "elevated", "high")
ZONES = ("ACCEPTABLE_ODDS", "ATTRACTIVE_ODDS", "EXCEPTIONAL_ODDS")
LIMITATIONS = [
    "PROBABILITIES_AND_WORLD_COMPLETENESS_ARE_RESEARCH_JUDGMENTS_NOT_CERTIFIED_FREQUENCIES",
    "RISK_AND_POLICY_ARE_DECLARED_NOT_HUMAN_ACCEPTANCE_OR_INVESTMENT_AUTHORITY",
    "POSITIVE_RETURN_PROBABILITY_IS_MODEL_CONDITIONAL_NOT_EMPIRICAL_WIN_RATE",
    "SCENARIO_EXTREMES_ARE_NOT_COMPLETE_TAIL_LOSS_BOUNDS",
    "RAW_DATED_CLOSE_NOT_EXECUTABLE_QUOTE_LATEST_FEED_OR_HISTORICAL_PIT",
    "FIXED_CAPITAL_STRUCTURE_NOT_CERTIFIED_BY_PRICE_OR_SECURITY_MASTER",
    "NO_ANNUALIZATION_DISCOUNTING_FEES_TAX_FX_OR_REINVESTMENT",
]


def _indices(value):
    require(isinstance(value, list) and 1 <= len(value) <= 64
            and all(type(i) is int and 0 <= i < 64 for i in value)
            and len(value) == len(set(value)), "ODDS_PROBABILITY_SOURCE_REQUIRED")


def _unit(value):
    amount = number(value)
    require(ZERO <= amount <= ONE, "ODDS_PROBABILITY_RANGE")
    return amount


def validate_policy(value):
    shape(value, {"policy_id", "as_of", "rationale", "minimum_holding_period_days",
                  "maximum_holding_period_days", "max_quote_age_days", "max_master_age_days",
                  "max_capital_age_days", "thresholds", "model_risk_addons", "uncertainty_addons"})
    identifier(value["policy_id"])
    timestamp(value["as_of"])
    checked_text(value["rationale"], limit=4096)
    for key in ("minimum_holding_period_days", "maximum_holding_period_days",
                "max_quote_age_days", "max_master_age_days", "max_capital_age_days"):
        require(type(value[key]) is int and 1 <= value[key] <= 36525, "ODDS_POLICY_DAY_BOUND")
    require(value["minimum_holding_period_days"] <= value["maximum_holding_period_days"],
            "ODDS_POLICY_HORIZON_RANGE")
    thresholds = value["thresholds"]
    require(isinstance(thresholds, list) and len(thresholds) == 3, "ODDS_POLICY_THRESHOLDS")
    returns, probabilities = [], []
    for zone, row in zip(ZONES, thresholds):
        shape(row, {"zone", "minimum_return", "minimum_positive_probability"})
        require(row["zone"] == zone, "ODDS_POLICY_ZONE_ORDER")
        amount = number(row["minimum_return"])
        require(amount >= ZERO, "ODDS_POLICY_NEGATIVE_THRESHOLD")
        returns.append(amount)
        probabilities.append(_unit(row["minimum_positive_probability"]))
    require(all(a < b for a, b in zip(returns, returns[1:]))
            and all(a <= b for a, b in zip(probabilities, probabilities[1:])),
            "ODDS_POLICY_NONMONOTONIC")
    for key, levels in (("model_risk_addons", LEVELS), ("uncertainty_addons", UNCERTAINTIES)):
        shape(value[key], set(levels))
        amounts = [number(value[key][level]) for level in levels]
        require(all(a >= ZERO for a in amounts)
                and all(a <= b for a, b in zip(amounts, amounts[1:])), "ODDS_POLICY_NONMONOTONIC")
    require(number(value["uncertainty_addons"]["baseline"]) == ZERO,
            "ODDS_BASELINE_ADDON_MUST_BE_ZERO")


def validate_assessment(payload, spec=None):
    shape(payload, {"schema", "assessment_id", "spec_ref", "as_of", "author_note", "probabilities",
        "coverage", "model_risk", "uncertainty", "policy", "unresolved_treatment", "limitations"})
    require(payload["schema"] == "odds-assessment-v1", "ODDS_ASSESSMENT_VERSION")
    require(len(encoded(payload)) <= 256 * 1024, "ODDS_ASSESSMENT_SIZE")
    out = deepcopy(payload)
    identifier(out["assessment_id"])
    artifact_reference(out["spec_ref"])
    timestamp(out["as_of"])
    checked_text(out["author_note"], limit=4096)
    validate_policy(out["policy"])
    require(timestamp(out["policy"]["as_of"]) <= timestamp(out["as_of"]), "ODDS_POLICY_AFTER_ASSESSMENT")
    rows = out["probabilities"]
    require(isinstance(rows, list) and 1 <= len(rows) <= 12, "ODDS_PROBABILITY_DISTRIBUTION_REQUIRED")
    ids = []
    with localcontext(CONTEXT):
        total = ZERO
        for row in rows:
            shape(row, {"world_id", "probability", "basis_note", "source_indices"})
            identifier(row["world_id"])
            ids.append(row["world_id"])
            total += _unit(row["probability"])
            checked_text(row["basis_note"], limit=4096)
            _indices(row["source_indices"])
        # Unlike the old 1e-6 tolerance, never silently accept >100% or normalize.
        require(total == ONE, "ODDS_COMPLETE_PROBABILITY_DISTRIBUTION_REQUIRED")
    require(len(ids) == len(set(ids)), "ODDS_DUPLICATE_PROBABILITY_WORLD")
    shape(out["coverage"], {"mutually_exclusive", "exhaustive", "basis_note", "source_indices"})
    require(out["coverage"]["mutually_exclusive"] is True and out["coverage"]["exhaustive"] is True,
            "ODDS_WORLD_COVERAGE_REQUIRED")
    checked_text(out["coverage"]["basis_note"], limit=4096)
    _indices(out["coverage"]["source_indices"])
    for key, levels in (("model_risk", LEVELS), ("uncertainty", UNCERTAINTIES)):
        shape(out[key], {"level", "basis_note", "source_indices"})
        require(out[key]["level"] in levels, "ODDS_RISK_NOT_ESTABLISHED")
        checked_text(out[key]["basis_note"], limit=4096)
        _indices(out[key]["source_indices"])
    treatment = out["unresolved_treatment"]
    require(isinstance(treatment, list) and len(treatment) <= 128, "ODDS_UNRESOLVED_TREATMENT")
    for row in treatment:
        shape(row, {"item", "effect"})
        checked_text(row["item"], limit=4096)
        checked_text(row["effect"], limit=4096)
    require(len({row["item"] for row in treatment}) == len(treatment), "ODDS_UNRESOLVED_TREATMENT")
    require(isinstance(out["limitations"], list) and 1 <= len(out["limitations"]) <= 32,
            "ODDS_LIMITATIONS_REQUIRED")
    for text in out["limitations"]:
        checked_text(text, limit=4096)
    if spec is not None:
        require(spec["value_kind"] == "future-exit", "ODDS_PROBABILITY_REQUIRES_FUTURE_PAYOFF")
        require(set(ids) == {w["id"] for w in spec["worlds"]}, "ODDS_PROBABILITY_WORLD_MISMATCH")
        require(timestamp(out["as_of"]).date() >= day(spec["valuation_date"]),
                "ODDS_ASSESSMENT_PRECEDES_VALUATION")
    return out


def calculate_probability(spec, assessment, price):
    """Internal deterministic projection; application layer owns input qualification."""
    assessment = validate_assessment(assessment, spec)
    spec = validate_spec(spec)
    require(number(price["price"]) > ZERO and price["security"] == spec["security"]
            and price["currency"] == spec["currency"] and number(price["share_count"]) == number(spec["share_count"]),
            "ODDS_QUALIFIED_PRICE_MISMATCH")
    require(timestamp(price["as_of"]).date() < day(spec["target_date"]), "ODDS_TARGET_NOT_AFTER_PRICE")
    require(all(timestamp(price["as_of"]).date() < day(f["date"]) for w in spec["worlds"] for f in w["distributions"]),
            "ODDS_PAST_DISTRIBUTION_REQUIRES_NEW_SPEC")
    conditional_rows = world_results(spec, number(price["price"]))
    holding_days = (day(spec["target_date"]) - timestamp(price["as_of"]).date()).days
    policy = assessment["policy"]
    require(policy["minimum_holding_period_days"] <= holding_days <= policy["maximum_holding_period_days"],
            "ODDS_POLICY_HORIZON_NOT_COVERED")
    probabilities = {r["world_id"]: number(r["probability"]) for r in assessment["probabilities"]}
    rows = [{**row, "probability": decimal_text(probabilities[row["world_id"]])}
            for row in conditional_rows]
    with localcontext(CONTEXT):
        # Same weighted arithmetic as the old calculation.py; zero return is not positive.
        aggregate = {name: decimal_text(sum((probabilities[r["world_id"]] * Decimal(r[field])
            for r in rows), ZERO)) for name, field in (
            ("weighted_terminal_equity_per_share", "equity_value_per_share"),
            ("weighted_distributions_per_share", "total_distributions_per_share"),
            ("weighted_total_payoff_per_share", "total_payoff_per_share"),
            ("expected_holding_period_return", "undiscounted_holding_period_return"))}
        positive = sum((probabilities[r["world_id"]] for r in rows
                        if Decimal(r["undiscounted_holding_period_return"]) > ZERO), ZERO)
        aggregate["positive_return_scenario_probability"] = decimal_text(positive)
        model = number(policy["model_risk_addons"][assessment["model_risk"]["level"]])
        context = number(policy["uncertainty_addons"][assessment["uncertainty"]["level"]])
        thresholds = [{**t, "required_return": decimal_text(number(t["minimum_return"]) + model + context)}
                      for t in policy["thresholds"]]
        zone = "INSUFFICIENT_ODDS"
        for t in reversed(thresholds):
            if (Decimal(aggregate["expected_holding_period_return"]) >= Decimal(t["required_return"])
                    and positive >= number(t["minimum_positive_probability"])):
                zone = t["zone"]
                break
    returns = [Decimal(r["undiscounted_holding_period_return"]) for r in rows]
    return {"schema": "probability-odds-v1", "method": "declared-probability-nominal-return-v1",
        "world_results": rows, "aggregate": aggregate, "holding_period_days": holding_days,
        "minimum_scenario_return": decimal_text(min(returns)), "maximum_scenario_return": decimal_text(max(returns)),
        "adjusted_thresholds": thresholds, "model_risk_addon": decimal_text(model),
        "uncertainty_addon": decimal_text(context), "participation_zone": zone,
        "policy": policy, "probability_status": "explicit-research-assessment-not-empirically-certified",
        "market_qualification": "application-validation-required", "human_acceptance": "not-confirmed",
        "investment_authority": "none", "economic_assessment": "not-performed",
        "is_annualized": False, "is_discounted": False, "decimal_precision": 80,
        "decimal_rounding": "ROUND_HALF_EVEN", "limitations": LIMITATIONS + assessment["limitations"] + spec["limitations"]}
