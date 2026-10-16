"""Shibor date-pair context over existing Relay captures, not price returns.

No network, calendar inference, interpolation, source correction or writer.
Quote validity and a complete capture are separate from economic qualification.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, DecimalException, Inexact, localcontext

from decision_kernel.application.market_relay_inputs import _day, _lineage, _range, _require, _rows, _view
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.domain.research_numbers import arithmetic_context, number

SHIBOR_TENORS = ("on", "1w", "2w", "1m", "3m", "6m", "9m", "1y")
SHIBOR_FIELDS = {"date", *SHIBOR_TENORS}
SHIBOR_READING_SCHEMA = "relay-shibor-reading-v1"
SHIBOR_REQUIRED = {"rate_result", "start", "end", "baseline_dates"}
QUOTE_STATUSES = ("observed", "missing-value", "invalid-value", "date-not-returned")
CHANGE_STATUSES = ("observed", "missing-quote", "invalid-quote", "arithmetic-unavailable")


def _quote(value: object) -> dict:
    """Reuse signed bounded Decimal parsing; never use the positive stock-price parser."""
    if value is None or value == "":
        return {"status": "missing-value", "rate_pct": None}
    if type(value) not in (str, int, float):
        return {"status": "invalid-value", "rate_pct": None}
    text = str(value)
    try:
        # Relay fields are numeric scalars, not prose, unit suffixes or locale strings.
        if text != text.strip() or "," in text:
            raise RecordError("DECIMAL_STRING_REQUIRED")
        parsed = number(text)
    except RecordError:
        return {"status": "invalid-value", "rate_pct": None}
    return {"status": "observed", "rate_pct": str(parsed)}


def _difference(left: dict, right: dict) -> dict:
    """left minus right in percentage points; one percentage point is 100 bp.

    Refuse an inexact extreme input rather than silently round a valid zero.
    Missing/invalid operands remain individually visible at the call site.
    """
    result = {"status": "missing-quote", "change_pp": None, "change_bp": None}
    if "invalid-value" in (left["status"], right["status"]):
        result["status"] = "invalid-quote"
    elif left["status"] == right["status"] == "observed":
        context = arithmetic_context()
        context.traps[Inexact] = True
        try:
            with localcontext(context):
                pp = Decimal(left["rate_pct"]) - Decimal(right["rate_pct"])
                bp = pp * Decimal(100)
        except DecimalException:
            result["status"] = "arithmetic-unavailable"
        else:
            result.update(status="observed", change_pp=str(pp), change_bp=str(bp))
    return result


def read_shibor_context(*, rate_result: dict, registry: RelayRegistry,
                        start: str, end: str, baseline_dates: list[str] | tuple[str, ...],
                        offset: int = 0, limit: int = 50,
                        entity_id: str | None = None) -> dict:
    """Read all eight tenors at explicit source dates, never N received quote rows.

    Dates/duplicates and capture authenticity are shared prerequisites. Missing or
    invalid numeric cells are isolated, with whole-capture counts even outside
    requested date pairs. A returned date is not certification of a publication
    calendar. Missing intermediate dates do not invalidate an endpoint comparison.
    """
    lo, hi = _range(start, end)
    _require((date.fromisoformat(hi) - date.fromisoformat(lo)).days <= 3660,
             "SHIBOR_RANGE_TOO_LARGE")
    _require(isinstance(baseline_dates, (list, tuple)) and 1 <= len(baseline_dates) <= 12,
             "SHIBOR_BASELINES")
    baselines = [_day(value) for value in baseline_dates]
    _require(len(set(baselines)) == len(baselines) and all(lo <= day < hi for day in baselines),
             "SHIBOR_BASELINES")
    baselines.sort()
    _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
             "SHIBOR_PAGE")
    _require(entity_id is None or isinstance(entity_id, str) and entity_id in SHIBOR_TENORS,
             "SHIBOR_TENOR")
    view = _view(rate_result, registry, "shibor", {"start_date": start, "end_date": end}, SHIBOR_FIELDS)
    quotes = {}
    cell_counts = {tenor: {status: 0 for status in QUOTE_STATUSES[:3]} for tenor in SHIBOR_TENORS}
    for row in _rows(view):
        day = _day(row["date"])
        _require(lo <= day <= hi and day not in quotes, "SHIBOR_DATE_ROW")
        quotes[day] = {tenor: _quote(row[tenor]) for tenor in SHIBOR_TENORS}
        for tenor, value in quotes[day].items():
            cell_counts[tenor][value["status"]] += 1

    def at(day, tenor):
        return {"date": day, **quotes.get(day, {}).get(tenor,
            {"status": "date-not-returned", "rate_pct": None})}

    entries = []
    for tenor in SHIBOR_TENORS:
        end_quote = at(hi, tenor)
        comparisons = []
        for baseline in baselines:
            base_quote = at(baseline, tenor)
            comparisons.append({"baseline": base_quote, "end_date": hi,
                "elapsed_calendar_days": (date.fromisoformat(hi) - date.fromisoformat(baseline)).days,
                **_difference(end_quote, base_quote)})
        entries.append({"entity_id": tenor, "end_quote": end_quote, "comparisons": comparisons,
                        "received_cell_counts": cell_counts[tenor]})
    comparison_counts = {day: {status: sum(e["comparisons"][i]["status"] == status for e in entries)
        for status in CHANGE_STATUSES} for i, day in enumerate(baselines)}
    spreads = []
    for day in [*baselines, hi]:
        for long_tenor in ("3m", "1y"):
            long_quote, short_quote = at(day, long_tenor), at(day, "on")
            change = _difference(long_quote, short_quote)
            spreads.append({"date": day, "long_tenor": long_tenor, "short_tenor": "on",
                "long_quote": long_quote, "short_quote": short_quote, "status": change["status"],
                "spread_pp": change["change_pp"], "spread_bp": change["change_bp"]})
    result = {"schema": "relay-shibor-context-v1", "family": "rates", "benchmark": "SHIBOR",
        "currency": "CNY", "quote_unit": "percent", "change_unit": "basis-points",
        "source_timezone": "Asia/Shanghai", "start": lo, "as_of": hi, "baseline_dates": baselines,
        "scope_count": len(SHIBOR_TENORS), "declared_tenors": list(SHIBOR_TENORS),
        "scope_semantics": "eight-Shibor-tenors-not-all-rates-or-security-candidates",
        "comparison_semantics": "explicit-date-endpoints-not-trading-session-windows-or-investment-returns",
        "source_qualification": {"status": "capture-checked" if quotes else "capture-checked-empty",
                                 "row_count": len(quotes), "page_count": view["page_count"]},
        "source_lineage": _lineage(view), "received_dates": sorted(quotes), "received_cell_counts": cell_counts,
        "end_quote_counts": {s: sum(e["end_quote"]["status"] == s for e in entries) for s in QUOTE_STATUSES},
        "comparison_counts": comparison_counts, "curve_spreads": spreads,
        "qualification_limits": [
            "NO_INFERRED_CALENDAR_LATEST_DATE_INTERPOLATION_OR_FORWARD_FILL",
            "MISSING_DATE_NOT_PROVEN_HOLIDAY_OR_SOURCE_FAILURE",
            "RATE_POINTS_NOT_PRICE_RETURNS_BOND_RETURNS_OR_COMPOUNDED_YIELDS",
            "SIGNED_NUMERIC_VALIDITY_NOT_ECONOMIC_APPROVAL_OR_REFERENCE_RATE_VERIFICATION",
            "SOURCE_DATE_NOT_PUBLISHED_AT_OR_RETRIEVED_AT_OR_HISTORICAL_PIT",
            "CAPTURE_HASH_NOT_SOURCEREF_OR_PUBLICATION_PERMISSION",
            "NO_AUTOMATIC_DISCOUNT_RATE_COMPANY_EXPOSURE_RESEARCH_OR_BRIEF_ADOPTION"]}
    result["view_version"] = digest(encoded({**result, "entries": entries}))
    selected = entries if entity_id is None else [e for e in entries if e["entity_id"] == entity_id]
    page = selected[offset:offset + limit]
    result.update(entries=page, matched_count=len(selected), pagination={"offset": offset, "limit": limit,
        "returned": len(page), "next_offset": offset + len(page) if offset + len(page) < len(selected) else None})
    return result
