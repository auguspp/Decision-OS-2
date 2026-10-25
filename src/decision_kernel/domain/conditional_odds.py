"""Restricted, probability-free valuation outputs, not an expression evaluator.

Arithmetic extracted from decision-kernel@cb3bbc87 calculation.py (_normalized,
80-digit HALF_EVEN context) and conditional_odds.py (_calculate_world). Old
ResearchSnapshot/HumanPriceContext identities are deliberately not emulated.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import re

from decision_kernel.domain.records import (
    BlobRef, RecordError, SourceRef, checked_text, encoded, identifier, require,
)

VERSION = "conditional-odds-v1"
RETAINED_VERSION = "conditional-odds-retained-close-v1"
CONTEXT = Context(prec=80, rounding=ROUND_HALF_EVEN)
ZERO, ONE = Decimal("0"), Decimal("1")
METHODS = {"current-fair-value": "equity-value-distance-v1",
           "future-exit": "nominal-holding-return-v1"}
NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
BASE_LIMITATIONS = [
    "DECLARED_WORLDS_NOT_COMPLETE_DOWNSIDE_OR_PROBABILITIES",
    "NO_WEIGHTED_RETURN_WIN_PROBABILITY_OR_INVESTMENT_RECOMMENDATION",
    "NO_MARKET_QUALIFICATION_OR_HUMAN_ACCEPTANCE",
    "CAPITAL_STRUCTURE_CURRENCY_AND_ECONOMIC_ASSUMPTIONS_ARE_DECLARATIONS",
    "NO_FEES_TAX_FX_DISCOUNTING_ANNUALIZATION_OR_CASH_REINVESTMENT",
]


def shape(value, keys, optional=()):
    require(isinstance(value, dict) and set(keys) <= set(value) <= set(keys) | set(optional),
            "ODDS_INPUT_SHAPE")


def number(value):
    # Avoid binary-float ingestion, non-finite values and unbounded exponents.
    require(isinstance(value, str) and len(value) <= 96 and NUMBER.fullmatch(value) is not None,
            "ODDS_DECIMAL_STRING_REQUIRED")
    result = Decimal(value)
    require(len(result.as_tuple().digits) <= 48 and -32 <= result.adjusted() <= 32,
            "ODDS_DECIMAL_RANGE")
    return result


def _normalized(value):
    if value == ZERO:
        return ZERO
    return value.normalize()


def decimal_text(value):
    with localcontext(CONTEXT):
        return format(_normalized(value), "f")


def day(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None,
            "ODDS_DATE_REQUIRED")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RecordError("ODDS_DATE_REQUIRED") from None


def timestamp(value):
    checked_text(value, limit=80)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None and stamp.utcoffset() is not None,
                "ODDS_AWARE_PRICE_TIME_REQUIRED")
        return stamp
    except ValueError:
        raise RecordError("ODDS_AWARE_PRICE_TIME_REQUIRED") from None


def security(value):
    shape(value, {"code", "share_class"})
    require(isinstance(value["code"], str) and
            re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,31}", value["code"]) is not None,
            "ODDS_SECURITY_REQUIRED")
    checked_text(value["share_class"], limit=64)


def _currency(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Z]{3}", value) is not None,
            "ODDS_CURRENCY_REQUIRED")


def _strings(values, *, allow_empty=False):
    require(isinstance(values, list) and (allow_empty or values) and len(values) <= 32,
            "ODDS_DECLARATIONS_REQUIRED")
    for value in values:
        checked_text(value, limit=4096)


def _basis(value):
    require(value in ("per-share", "total-equity"), "ODDS_EQUITY_BASIS_REQUIRED")


def validate_spec(payload):
    """Detach input and check declared mechanics; does not approve a valuation."""
    shape(payload, {"schema", "spec_id", "research_ref", "security", "currency", "value_kind",
        "valuation_date", "target_date", "share_count", "capital_structure", "method",
        "cash_policy", "worlds", "probability_status", "allowed_changes", "limitations"})
    require(payload["schema"] == "valuation-spec-v1", "ODDS_SPEC_VERSION_UNSUPPORTED")
    require(len(encoded(payload)) <= 256 * 1024, "ODDS_SPEC_SIZE_LIMIT")
    spec = deepcopy(payload)
    identifier(spec["spec_id"])
    shape(spec["research_ref"], {"repository", "commit", "path", "sha256"})
    ref = BlobRef(**spec["research_ref"])
    require(re.fullmatch(r"research/cases/[a-z0-9][a-z0-9_-]{0,95}/revisions/"
                        r"[a-z0-9][a-z0-9_-]{0,95}/record\.json", ref.path) is not None,
            "ODDS_EXACT_RESEARCH_REQUIRED")
    security(spec["security"])
    _currency(spec["currency"])
    value_kind = spec["value_kind"]
    require(isinstance(value_kind, str) and value_kind in METHODS
            and spec["method"] == METHODS[value_kind], "ODDS_METHOD_NOT_SUPPORTED")
    valuation = day(spec["valuation_date"])
    require(number(spec["share_count"]) > ZERO, "ODDS_POSITIVE_SHARES_REQUIRED")
    shape(spec["capital_structure"], {"as_of", "assumption"})
    require(day(spec["capital_structure"]["as_of"]) <= valuation, "ODDS_CAPITAL_DATE_AFTER_VALUATION")
    checked_text(spec["capital_structure"]["assumption"], limit=4096)
    require(spec["probability_status"] == "not-established", "ODDS_PROBABILITY_POLICY_NOT_SUPPORTED")
    require(spec["allowed_changes"] == ["price-context"], "ODDS_PRICE_ONLY_REQUIRED")
    _strings(spec["limitations"])
    if value_kind == "current-fair-value":
        require(spec["target_date"] is None and spec["cash_policy"] == "included-in-fair-value",
                "ODDS_FAIR_VALUE_IS_NOT_FUTURE_PAYOFF")
        target = valuation
    else:
        target = day(spec["target_date"])
        require(target > valuation, "ODDS_TARGET_MUST_FOLLOW_VALUATION")
        require(spec["cash_policy"] == "terminal-excludes-distributions",
                "ODDS_CASH_DOUBLE_COUNT_RISK")
    worlds = spec["worlds"]
    require(isinstance(worlds, list) and 1 <= len(worlds) <= 12, "ODDS_WORLD_LIMIT")
    ids, names = set(), set()
    for world in worlds:
        shape(world, {"id", "name", "value", "basis", "conditions", "basis_note",
                      "source_indices", "distributions"})
        identifier(world["id"])
        checked_text(world["name"], limit=256)
        require(world["id"] not in ids and world["name"] not in names, "ODDS_DUPLICATE_WORLD")
        ids.add(world["id"]); names.add(world["name"])
        require(number(world["value"]) >= ZERO, "ODDS_NEGATIVE_EQUITY_VALUE")
        _basis(world["basis"])
        _strings(world["conditions"])
        checked_text(world["basis_note"], limit=4096)
        indices = world["source_indices"]
        require(isinstance(indices, list) and len(indices) <= 64
                and all(type(i) is int and 0 <= i < 64 for i in indices)
                and len(set(indices)) == len(indices), "ODDS_SOURCE_INDEX")
        flows = world["distributions"]
        require(isinstance(flows, list) and len(flows) <= 64, "ODDS_CASHFLOW_LIMIT")
        require(value_kind == "future-exit" or not flows, "ODDS_FAIR_VALUE_IS_NOT_FUTURE_PAYOFF")
        flow_ids = set()
        for flow in flows:
            shape(flow, {"id", "date", "amount", "basis", "currency", "kind"})
            identifier(flow["id"])
            require(flow["id"] not in flow_ids, "ODDS_DUPLICATE_CASHFLOW")
            flow_ids.add(flow["id"])
            require(valuation < day(flow["date"]) <= target, "ODDS_CASHFLOW_OUTSIDE_HORIZON")
            require(number(flow["amount"]) >= ZERO, "ODDS_CAPITAL_CALL_REQUIRES_NEW_SPEC")
            _basis(flow["basis"])
            require(flow["currency"] == spec["currency"], "ODDS_FX_NOT_SUPPORTED")
            require(flow["kind"] in ("dividend", "capital-return"), "ODDS_CASHFLOW_TYPE_UNSUPPORTED")
    return spec


def validate_price(payload, spec):
    shape(payload, {"schema", "price_id", "kind", "security", "currency", "share_count",
                    "price", "as_of", "note", "evidence"})
    require(payload["schema"] == "odds-price-v1", "ODDS_PRICE_VERSION_UNSUPPORTED")
    price = deepcopy(payload)
    identifier(price["price_id"])
    require(price["kind"] in ("declared-reference", "relay-observation"), "ODDS_PRICE_KIND_NOT_SUPPORTED")
    security(price["security"])
    require(price["security"] == spec["security"], "ODDS_SECURITY_SHARE_CLASS_MISMATCH")
    require(price["currency"] == spec["currency"], "ODDS_FX_NOT_SUPPORTED")
    require(number(price["share_count"]) == number(spec["share_count"]), "ODDS_CAPITAL_CHANGE_REQUIRES_NEW_SPEC")
    require(number(price["price"]) > ZERO, "ODDS_POSITIVE_PRICE_REQUIRED")
    price_day = timestamp(price["as_of"]).date()
    checked_text(price["note"], limit=4096)
    evidence = price["evidence"]
    require(price["kind"] != "relay-observation" or evidence is not None, "ODDS_RELAY_EVIDENCE_REQUIRED")
    if evidence is not None:
        shape(evidence, {"source", "value_locator", "security_locator", "date_locator"})
        SourceRef.from_dict(evidence["source"])
        for key in ("value_locator", "security_locator", "date_locator"):
            locator = evidence[key]
            require(isinstance(locator, dict) and set(locator) in
                    ({"row", "column"}, {"start", "end"}), "ODDS_PRICE_LOCATOR_REQUIRED")
    if spec["value_kind"] == "future-exit":
        require(price_day < day(spec["target_date"]), "ODDS_TARGET_NOT_AFTER_PRICE")
        require(all(day(flow["date"]) > price_day for world in spec["worlds"]
                    for flow in world["distributions"]), "ODDS_PAST_DISTRIBUTION_REQUIRES_NEW_SPEC")
    return price


def calculate(spec, price):
    """Pure declared-world calculation. Storage and evidence validation are upstream."""
    spec = validate_spec(spec)
    price = validate_price(price, spec)
    return _result(spec, price)


def validate_close_policy(payload):
    """Explicit dated-close tolerances; no default investment or market policy."""
    shape(payload, {"policy_id", "as_of", "rationale", "max_quote_age_days",
                    "max_master_age_days", "max_capital_age_days"})
    identifier(payload["policy_id"])
    timestamp(payload["as_of"])
    checked_text(payload["rationale"], limit=4096)
    for key in ("max_quote_age_days", "max_master_age_days", "max_capital_age_days"):
        require(type(payload[key]) is int and 1 <= payload[key] <= 36525, "ODDS_POLICY_DAY_BOUND")
    return deepcopy(payload)


def calculate_retained(spec, price):
    """Same conditional arithmetic after an existing retained-close adapter.

    This pure function cannot certify custody or market freshness. Its distinct
    output version leaves the original declared-price result/replay unchanged.
    """
    spec = validate_spec(spec)
    shape(price, {"schema", "price_id", "kind", "security", "currency", "share_count",
                  "price", "as_of", "timestamp_semantics", "share_count_qualification"})
    require(price["schema"] == "odds-qualified-close-v1"
            and price["kind"] in ("retained-ftshare-raw-close", "retained-relay-raw-close"),
            "ODDS_PRICE_KIND_NOT_SUPPORTED")
    identifier(price["price_id"])
    require(number(price["price"]) > ZERO and price["security"] == spec["security"]
        and price["currency"] == spec["currency"] and number(price["share_count"]) == number(spec["share_count"]),
        "ODDS_QUALIFIED_PRICE_MISMATCH")
    price_day = timestamp(price["as_of"]).date()
    if spec["value_kind"] == "future-exit":
        require(price_day < day(spec["target_date"]), "ODDS_TARGET_NOT_AFTER_PRICE")
        require(all(day(flow["date"]) > price_day for world in spec["worlds"]
                    for flow in world["distributions"]), "ODDS_PAST_DISTRIBUTION_REQUIRES_NEW_SPEC")
    return _result(spec, deepcopy(price), retained=True)


def _result(spec, price, *, retained=False):
    rows = world_results(spec, number(price["price"]))
    limitations = list(BASE_LIMITATIONS)
    if retained:
        limitations = ["RAW_DATED_CLOSE_NOT_EXECUTABLE_QUOTE_LATEST_FEED_OR_HUMAN_ACCEPTANCE"
            if item == "NO_MARKET_QUALIFICATION_OR_HUMAN_ACCEPTANCE" else item for item in limitations]
    limitations += spec["limitations"]
    price_day = timestamp(price["as_of"]).date()
    if price_day < day(spec["valuation_date"]):
        limitations.append("PRE_VALUATION_REFERENCE_NOT_POINT_IN_TIME_ANALYSIS")
    elif price_day != day(spec["valuation_date"]):
        limitations.append("NEW_PRICE_DOES_NOT_REFRESH_VALUATION_OR_RESEARCH")
    return {"schema": RETAINED_VERSION if retained else VERSION, "method": spec["method"], "value_kind": spec["value_kind"],
            "valuation_date": spec["valuation_date"], "target_date": spec["target_date"],
            "price": price, "world_results": rows, "probability_status": "not-established",
            "weighted_aggregate": None, "market_qualification": "application-validation-required" if retained else "not-established",
            "human_acceptance": "not-confirmed", "investment_authority": "none",
            "is_annualized": False, "is_discounted": False,
            "decimal_precision": 80, "decimal_rounding": "ROUND_HALF_EVEN",
            "limitations": limitations}


def world_results(spec, entry):
    """Internal shared arithmetic over validated inputs; no qualification claim."""
    rows = []
    with localcontext(CONTEXT):
        shares = number(spec["share_count"])
        for world in sorted(spec["worlds"], key=lambda w: (w["name"], w["id"])):
            value = number(world["value"])
            terminal = _normalized(value if world["basis"] == "per-share" else value / shares)
            row = {"world_id": world["id"], "world_name": world["name"],
                   "equity_value_per_share": decimal_text(terminal)}
            if spec["value_kind"] == "current-fair-value":
                row["value_to_price_distance"] = decimal_text(terminal / entry - ONE)
            else:
                distributions = []
                for flow in sorted(world["distributions"], key=lambda f: (f["date"], f["id"])):
                    amount = number(flow["amount"])
                    amount = _normalized(amount if flow["basis"] == "per-share" else amount / shares)
                    distributions.append({"id": flow["id"], "date": flow["date"], "kind": flow["kind"],
                                          "amount_per_share": decimal_text(amount)})
                # Canonical old conditional arithmetic; no inferred probability or annualization.
                total_distributions = _normalized(sum((Decimal(f["amount_per_share"]) for f in distributions), ZERO))
                total_payoff = _normalized(terminal + total_distributions)
                holding_period_return = _normalized((total_payoff / entry) - ONE)
                row.update(distributions=distributions,
                           total_distributions_per_share=decimal_text(total_distributions),
                           total_payoff_per_share=decimal_text(total_payoff),
                           undiscounted_holding_period_return=decimal_text(holding_period_return))
            rows.append(row)
    return rows
