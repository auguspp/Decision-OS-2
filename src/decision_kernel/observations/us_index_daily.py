"""Calendar-bound index levels, not currency prices, total returns or signals."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from decision_kernel.observations.market_daily import (
    MarketInputError, _change, _normalized, require_text, trade_day,
)


def index_context(*, as_of: str, market_sessions: Sequence[str],
                  horizons: tuple[int, ...]) -> tuple[date, ...]:
    """Validate even an empty index scope; no weekday or received-row calendar."""
    if (not isinstance(horizons, tuple) or not 1 <= len(horizons) <= 12
            or any(type(h) is not int or not 1 <= h <= 2520 for h in horizons)
            or len(set(horizons)) != len(horizons)):
        raise MarketInputError("INDEX_HORIZON_INVALID")
    if not isinstance(market_sessions, (list, tuple)):
        raise MarketInputError("INDEX_CALENDAR_INVALID")
    days = tuple(trade_day(day) for day in market_sessions)
    cutoff = trade_day(as_of)
    if not days or days[-1] != cutoff or any(a >= b for a, b in zip(days, days[1:])):
        raise MarketInputError("INDEX_CALENDAR_INVALID")
    return days


def observe_us_index(*, entity_id: str, series: Sequence[Mapping[str, object]],
                     as_of: str, market_sessions: Sequence[str],
                     horizons: tuple[int, ...] = (5, 20, 60)) -> dict:
    """Reuse strict positive levels and Decimal ratios on exact calendar windows.

    Every supplied row must belong to this calendar. Missing intermediate sessions
    invalidate the affected window, not shorter complete windows. No carry-forward,
    nearest-date substitution, currency conversion or cross-index ranking occurs.
    """
    require_text(entity_id, "INDEX_ID_REQUIRED")
    days = index_context(as_of=as_of, market_sessions=market_sessions, horizons=horizons)
    if not isinstance(series, (list, tuple)):
        raise MarketInputError("INDEX_SERIES_INVALID")
    levels = _normalized(series, days)
    if len(levels) != len(series):
        raise MarketInputError("INDEX_SERIES_OUTSIDE_CALENDAR")
    entries = []
    for horizon in horizons:
        item = {"sessions": horizon, "end_date": as_of}
        if len(days) <= horizon:
            item.update(status="insufficient-calendar-history", available_sessions=len(days) - 1)
        else:
            window = days[-horizon - 1:]
            missing = [day.isoformat() for day in window if day not in levels]
            item["baseline_date"] = window[0].isoformat()
            if missing:
                item.update(status="missing-market-sessions", missing_dates=missing)
            else:
                item.update(status="observed", baseline_level=str(levels[window[0]]),
                            end_level=str(levels[window[-1]]),
                            change_pct=str(_change(levels[window[-1]], levels[window[0]])))
        entries.append(item)
    complete = sum(item["status"] == "observed" for item in entries)
    status = ("observed" if complete == len(entries) else "partial-history" if complete
              else "no-observation-at-date" if days[-1] not in levels else "no-usable-window")
    return {"entity_id": entity_id, "as_of": as_of, "status": status,
            "market": "US", "unit": "index-points", "currency": None,
            "return_semantics": "source-index-level-ratio-not-certified-total-return",
            "market_date_semantics": "source-US-date-not-UTC-or-Singapore-date",
            "expected_sessions": len(days), "observed_sessions": len(levels),
            "last_observed_date": max(levels).isoformat() if levels else None,
            "horizons": entries}
