"""Bounded outcome arithmetic; differences do not approve the original judgment.

Narrow adaptation of decision-kernel@cb3bbc87 runtime/operating_outcomes.py
compare_pair. Source custody and dates belong to the application; Decimal parsing
and the isolated 80-digit context reuse the current numerical-audit primitives.
"""
from decimal import Decimal, localcontext

from decision_kernel.domain.records import checked_text, require
from decision_kernel.domain.research_numbers import arithmetic_context, number

BASIS_FIELDS = ("subject", "metric", "period", "period_start", "period_end",
                "period_kind", "basis", "currency", "unit", "share_basis")
HORIZONS = (5, 20)


def basis(value):
    require(isinstance(value, dict) and set(value) == set(BASIS_FIELDS), "OUTCOME_BASIS_SHAPE")
    for field in BASIS_FIELDS:
        checked_text(value[field], limit=256)
    return value


def compare_values(forecast, actual, *, lower=None, upper=None):
    """Exact same-scale values; no implicit units, FX, pooled scores or epsilon."""
    f, a = number(forecast), number(actual)
    with localcontext(arithmetic_context()):
        difference = a - f
        result = {"difference": format(difference, "f"),
            "absolute_difference": format(abs(difference), "f"),
            "relative_to_absolute_forecast": format(difference / abs(f), "f") if f else None,
            "relative_denominator_status": "absolute-forecast" if f else "zero-forecast-undefined",
            "range_position": None, "actual_minus_upper": None}
        if lower is not None or upper is not None:
            require(lower is not None and upper is not None, "OUTCOME_RANGE_ENDPOINTS_REQUIRED")
            lo, hi = number(lower), number(upper)
            require(lo <= hi and f == (lo + hi) / Decimal(2), "OUTCOME_RANGE_MIDPOINT")
            result.update(forecast_lower=format(lo, "f"), forecast_upper=format(hi, "f"),
                range_position="below" if a < lo else "above" if a > hi else "within",
                actual_minus_upper=format(a - hi, "f"))
        return result


def horizon_change(original, subsequent, anchor, horizon):
    """Count complete qualified market sessions, including missing price sessions.

The caller supplies the existing source owner's entity cores, not a list of
user-entered bars. The endpoint never moves to the next available quotation.
"""
    require(horizon in HORIZONS and type(horizon) is int, "OUTCOME_HORIZON")
    result = {"horizon_sessions": horizon, "anchor_date": anchor, "target_date": None,
        "status": "source-needed", "reason_code": None, "difference": None,
        "return_pct": None, "anchor_close": None, "target_close": None,
        "missing_sessions": [], "maturity_basis": "supplied-complete-trading-calendar",
        "total_return": None, "execution_return": None, "investment_effectiveness": None}
    if subsequent is None:
        return {**result, "reason_code": "OUTCOME_LATER_ENTITY_UNAVAILABLE"}
    if original["contract"] != subsequent["contract"]:
        return {**result, "status": "incomparable", "reason_code": "OUTCOME_MARKET_CONTRACT_CHANGED"}
    old_days, days = original["sessions"], subsequent["sessions"]
    if anchor not in old_days or anchor not in days:
        return {**result, "reason_code": "OUTCOME_ANCHOR_CALENDAR_UNAVAILABLE"}
    overlap = [d for d in days if old_days[0] <= d <= anchor]
    if overlap != [d for d in old_days if days[0] <= d <= anchor]:
        return {**result, "status": "incomparable", "reason_code": "OUTCOME_CALENDAR_REVISED"}
    old_prices, prices = original["prices"], subsequent["prices"]
    if any(old_prices.get(d) != prices.get(d) for d in overlap):
        return {**result, "status": "incomparable", "reason_code": "OUTCOME_ORIGINAL_PRICES_REVISED"}
    index = days.index(anchor)
    result["calendar_through"] = days[-1]
    if len(days) <= index + horizon:
        return {**result, "status": "not-mature-in-retained-window",
            "reason_code": "OUTCOME_HORIZON_NOT_IN_RETAINED_CALENDAR",
            "observed_subsequent_sessions": len(days) - index - 1,
            "maturity_at_later_calendar_dates": "unknown-needs-later-retained-calendar"}
    window = days[index:index + horizon + 1]
    result["target_date"] = window[-1]
    result["missing_sessions"] = [d for d in window if d not in prices]
    if anchor not in old_prices or result["missing_sessions"]:
        return {**result, "reason_code": "OUTCOME_MISSING_PRICE_WINDOW"}
    first, last = number(old_prices[anchor]), number(prices[window[-1]])
    if first <= 0 or last <= 0:
        return {**result, "status": "incomparable", "reason_code": "OUTCOME_NONPOSITIVE_CLOSE"}
    with localcontext(arithmetic_context()):
        result.update(status="observed", anchor_close=format(first, "f"), target_close=format(last, "f"),
            difference=format(last - first, "f"), return_pct=format((last / first - 1) * 100, "f"))
    return result
