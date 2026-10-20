"""Finite close-only ranges and strict turns over a supplied completed calendar.

Source qualification belongs to the existing market readers. These observations
do not infer intraday prices, OHLC fractals, trades or prospective discovery.
"""
from __future__ import annotations

from decision_kernel.observations.market_daily import (
    MarketInputError, _change, _normalized, daily_context, require_text,
)

POLICY = "prior-close-range-strict-three-close-v1"
MAX_WINDOW = 250
MAX_TURN_SESSIONS = 64


def dimensions(windows, turn_sessions):
    if (not isinstance(windows, (list, tuple)) or not 1 <= len(windows) <= 4
            or any(type(n) is not int or not 1 <= n <= MAX_WINDOW for n in windows)
            or len(set(windows)) != len(windows)):
        raise MarketInputError("STRUCTURE_WINDOWS_INVALID")
    if type(turn_sessions) is not int or not 3 <= turn_sessions <= MAX_TURN_SESSIONS:
        raise MarketInputError("STRUCTURE_TURN_SESSIONS_INVALID")
    return tuple(windows), turn_sessions


def _ranges(days, prices, windows):
    result = []
    for n in windows:
        item = {"sessions": n, "end_date": days[-1].isoformat(),
                "comparison": "final-close-versus-previous-N-closes-final-excluded"}
        if len(days) <= n:
            item.update(status="insufficient-calendar-history", available_sessions=len(days) - 1)
        else:
            required = days[-n - 1:]
            missing = [d.isoformat() for d in required if d not in prices]
            item.update(prior_start=required[0].isoformat(), prior_end=required[-2].isoformat())
            if missing:
                item.update(status="missing-market-sessions", missing_dates=missing)
            else:
                previous = [prices[d] for d in required[:-1]]
                low, high, close = min(previous), max(previous), prices[required[-1]]
                position = ("above-range" if close > high else "below-range" if close < low else
                            "at-flat-range" if low == high else "at-high" if close == high else
                            "at-low" if close == low else "inside-range")
                item.update(status="observed", prior_low=str(low), prior_high=str(high),
                    end_close=str(close), position=position,
                    distance_from_high_pct=str(_change(close, high)),
                    distance_from_low_pct=str(_change(close, low)))
        result.append(item)
    return result


def _turns(days, prices, requested):
    window = days[-requested:]
    confirmed, missing, observed = [], set(), 0
    for left, middle, right in zip(window, window[1:], window[2:]):
        absent = {d for d in (left, middle, right) if d not in prices}
        if absent:
            missing.update(absent)
            continue
        observed += 1
        a, b, c = (prices[d] for d in (left, middle, right))
        kind = "high" if b > a and b > c else "low" if b < a and b < c else None
        if kind is not None:
            confirmed.append({"kind": kind, "left_session": left.isoformat(),
                "turn_session": middle.isoformat(), "confirmed_session": right.isoformat(),
                "left_close": str(a), "turn_close": str(b), "right_close": str(c)})
    candidate = {"turn_session": days[-1].isoformat(), "confirmed_session": None, "kind": None}
    if len(days) < 2:
        candidate["status"] = "insufficient-calendar-history"
    elif any(d not in prices for d in days[-2:]):
        candidate.update(status="missing-market-sessions",
                         missing_dates=[d.isoformat() for d in days[-2:] if d not in prices])
    else:
        left, last = (prices[d] for d in days[-2:])
        candidate.update(status="unconfirmed-no-right-close", left_session=days[-2].isoformat(),
            left_close=str(left), turn_close=str(last),
            kind="high" if last > left else "low" if last < left else None)
        if last == left:
            candidate["status"] = "no-strict-candidate"
    total = max(0, len(window) - 2)
    status = ("observed" if len(window) == requested and observed == total else
              "partial-history" if observed else "no-usable-triples")
    return {"status": status, "requested_sessions": requested,
        "available_calendar_sessions": len(window), "window_start": window[0].isoformat(),
        "window_end": window[-1].isoformat(), "possible_triples": total,
        "observed_triples": observed, "missing_triples": total - observed,
        "missing_dates": [d.isoformat() for d in sorted(missing)],
        "left_boundary": "earlier-turns-outside-this-explicit-window",
        "confirmed": confirmed, "candidate": candidate,
        "confirmation_basis": "strict-neighbours-in-declared-completed-session-calendar"}


def observe_close_structure(*, entity_id, series, as_of, market_sessions,
                            calendar_version, adjustment, source_version, market, currency,
                            windows=(5, 20, 60), turn_sessions=20):
    """Every range uses N+1 actual sessions; every turn requires its right close.

    Missing sessions never shift a window or join nonadjacent observations.
    Replaying a longer unchanged prefix preserves already confirmed triples.
    """
    require_text(entity_id, "ENTITY_REQUIRED")
    windows, turn_sessions = dimensions(windows, turn_sessions)
    days = daily_context(as_of=as_of, market_sessions=market_sessions,
        calendar_version=calendar_version, adjustment=adjustment, source_version=source_version,
        market=market, currency=currency, horizons=windows)
    prices = _normalized(series, days)
    ranges, turns = _ranges(days, prices, windows), _turns(days, prices, turn_sessions)
    complete = all(row["status"] == "observed" for row in ranges) and turns["status"] == "observed"
    some = any(row["status"] == "observed" for row in ranges) or turns["observed_triples"] > 0
    return {"policy": POLICY, "entity_id": entity_id, "as_of": as_of,
        "status": "observed" if complete else "partial-history" if some else "no-usable-observation",
        "market": market, "currency": currency, "adjustment": adjustment,
        "calendar_version": calendar_version, "source_version": source_version,
        "ranges": ranges, "turns": turns,
        "knowledge_basis": "retained-source-replay-not-proof-of-prospective-observation"}
