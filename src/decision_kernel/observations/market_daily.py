"""Calendar-bound daily price observations over explicitly qualified inputs.

No network, price adjustment, corporate-action qualification or trade signal.
Dataset version labels are not custody receipts or proof of source eligibility.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Context, Decimal, InvalidOperation, localcontext


class MarketInputError(ValueError):
    """A stable input error code, never source text or credentials."""


def require_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or not value.isprintable():
        raise MarketInputError(code)
    return value


def trade_day(value: object) -> date:
    if not isinstance(value, str):
        raise MarketInputError("INVALID_TRADE_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise MarketInputError("INVALID_TRADE_DATE") from None
    if parsed.isoformat() != value:
        raise MarketInputError("INVALID_TRADE_DATE")
    return parsed


def _sequence(value: object, code: str) -> Sequence:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise MarketInputError(code)
    return value


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MarketInputError("INVALID_CLOSE")
    text = str(value)
    if len(text) > 128:
        raise MarketInputError("INVALID_CLOSE")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise MarketInputError("INVALID_CLOSE") from None
    if not number.is_finite() or number <= 0 or not -99 <= number.adjusted() <= 99:
        raise MarketInputError("INVALID_CLOSE")
    return number


def daily_context(*, as_of: str, market_sessions: Sequence[str], calendar_version: str,
                  adjustment: str, source_version: str, market: str, currency: str,
                  horizons: tuple[int, ...]) -> tuple[date, ...]:
    """Validate shared context even when a declared universe has no entities."""
    for value, code in ((calendar_version, "CALENDAR_VERSION_REQUIRED"),
                        (source_version, "SOURCE_VERSION_REQUIRED"), (market, "MARKET_REQUIRED")):
        require_text(value, code)
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
        raise MarketInputError("CURRENCY_INVALID")
    if adjustment not in ("raw", "qfq", "hfq"):
        raise MarketInputError("ADJUSTMENT_REQUIRED")
    if (not isinstance(horizons, tuple) or not horizons
            or any(type(h) is not int or h <= 0 for h in horizons)
            or len(set(horizons)) != len(horizons)):
        raise MarketInputError("HORIZON_INVALID")
    days = tuple(trade_day(d) for d in _sequence(market_sessions, "CALENDAR_INVALID"))
    if not days or any(a >= b for a, b in zip(days, days[1:])):
        raise MarketInputError("CALENDAR_INVALID")
    cutoff = trade_day(as_of)
    if cutoff not in days:
        raise MarketInputError("AS_OF_NOT_MARKET_SESSION")
    return tuple(d for d in days if d <= cutoff)


def _normalized(rows: Sequence[Mapping[str, object]], days: tuple[date, ...]) -> dict[date, Decimal]:
    seen: set[date] = set()
    found: dict[date, Decimal] = {}
    sessions = set(days)
    for row in _sequence(rows, "SERIES_INVALID"):
        if not isinstance(row, Mapping) or set(row) != {"trade_date", "close"}:
            raise MarketInputError("SERIES_ROW_INVALID")
        day = trade_day(row["trade_date"])
        close = _price(row["close"])
        if day in seen:
            raise MarketInputError("DUPLICATE_SESSION")
        seen.add(day)  # Includes future rows: never let duplicates evade validation.
        if days[0] <= day <= days[-1]:
            if day not in sessions:
                raise MarketInputError("NON_MARKET_SESSION")
            found[day] = close
    return found


def benchmark_context(benchmark: Mapping[str, object] | None) -> None:
    if benchmark is None:
        return
    fields = {"entity_id", "series", "source_version", "adjustment", "market", "currency"}
    if not isinstance(benchmark, Mapping) or set(benchmark) != fields:
        raise MarketInputError("BENCHMARK_CONTEXT_INVALID")
    for field in ("entity_id", "source_version", "market"):
        require_text(benchmark[field], "BENCHMARK_CONTEXT_INVALID")
    currency = benchmark["currency"]
    if (benchmark["adjustment"] not in ("raw", "qfq", "hfq")
            or not isinstance(currency, str) or len(currency) != 3
            or not currency.isascii() or not currency.isalpha() or not currency.isupper()):
        raise MarketInputError("BENCHMARK_CONTEXT_INVALID")


def _change(end: Decimal, start: Decimal) -> Decimal:
    # A fresh context makes results independent of caller precision and traps.
    with localcontext(Context(prec=34)):
        return (end / start - Decimal(1)) * 100


def observe_daily(*, entity_id: str, series: Sequence[Mapping[str, object]], as_of: str,
                  market_sessions: Sequence[str], calendar_version: str, adjustment: str,
                  source_version: str, market: str, currency: str,
                  horizons: tuple[int, ...] = (5, 20, 60),
                  benchmark: Mapping[str, object] | None = None) -> dict:
    """Use exactly N declared market sessions, never N received price rows.

    Missing any price in a window invalidates that window, not shorter complete
    windows. Future rows are validated but not used. No carry-forward is applied.
    Benchmark failures only restrict relative performance; metadata must be explicit.
    """
    require_text(entity_id, "ENTITY_REQUIRED")
    days = daily_context(as_of=as_of, market_sessions=market_sessions,
                         calendar_version=calendar_version, adjustment=adjustment,
                         source_version=source_version, market=market, currency=currency,
                         horizons=horizons)
    benchmark_context(benchmark)
    primary = _normalized(series, days)
    base: dict[date, Decimal] = {}
    relative_error = "benchmark-not-provided"
    if benchmark is not None:
        if any(benchmark[k] != v for k, v in (("market", market), ("currency", currency),
                                             ("adjustment", adjustment))):
            relative_error = "benchmark-convention-mismatch"
        else:
            try:
                base = _normalized(benchmark["series"], days)
                relative_error = ""
            except MarketInputError:
                relative_error = "benchmark-invalid-data"
    entries = []
    for horizon in horizons:
        entry = {"sessions": horizon, "end_date": as_of}
        if len(days) <= horizon:
            entry.update(status="insufficient-calendar-history", available_sessions=len(days) - 1)
        else:
            window = days[-horizon - 1:]
            missing = [d.isoformat() for d in window if d not in primary]
            entry["baseline_date"] = window[0].isoformat()
            if missing:
                entry.update(status="missing-market-sessions", missing_dates=missing)
            else:
                absolute = _change(primary[window[-1]], primary[window[0]])
                entry.update(status="observed", change_pct=str(absolute))
                if relative_error:
                    entry["relative_status"] = relative_error
                elif any(d not in base for d in window):
                    entry["relative_status"] = "benchmark-session-mismatch"
                else:
                    bench_change = _change(base[window[-1]], base[window[0]])
                    with localcontext(Context(prec=34)):
                        excess = absolute - bench_change
                    entry.update(relative_status="observed", benchmark_change_pct=str(bench_change),
                                 excess_change_pp=str(excess))
        entries.append(entry)
    complete = sum(e["status"] == "observed" for e in entries)
    status = ("observed" if complete == len(entries) else "partial-history" if complete else
              "no-observation-at-date" if days[-1] not in primary else "no-usable-window")
    return {"entity_id": entity_id, "as_of": as_of, "status": status,
            "market": market, "currency": currency, "adjustment": adjustment,
            "return_semantics": "price-ratio-not-total-return", "source_version": source_version,
            "calendar_version": calendar_version,
            "benchmark": None if benchmark is None else {k: v for k, v in benchmark.items() if k != "series"},
            "horizons": entries}
