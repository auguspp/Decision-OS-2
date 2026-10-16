"""Deterministic daily market observation, no network and no trade signal.

Inputs are *qualified* dated closes, not an inferred quote provider. Consumers
must carry the source dataset version separately from an external URL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


class MarketInputError(ValueError):
    pass


def _day(value: str) -> date:
    if not isinstance(value, str):
        raise MarketInputError("INVALID_TRADE_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise MarketInputError("INVALID_TRADE_DATE") from None
    if parsed.isoformat() != value:
        raise MarketInputError("INVALID_TRADE_DATE")
    return parsed


def _price(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise MarketInputError("INVALID_CLOSE")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise MarketInputError("INVALID_CLOSE") from None
    if not number.is_finite() or number <= 0:
        raise MarketInputError("INVALID_CLOSE")
    return number


def observe_daily(
    *,
    entity_id: str,
    series: Sequence[Mapping[str, object]],
    as_of: str,
    horizons: tuple[int, ...] = (1, 5, 20),
    adjustment: str,
    source_version: str,
    benchmark: Sequence[Mapping[str, object]] | None = None,
) -> dict:
    """Compare exactly N observed sessions before as_of (not N calendar days).

    No future observations are used. A benchmark must share exact start/end
    session dates; otherwise relative performance is explicitly unavailable.
    """
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise MarketInputError("ENTITY_REQUIRED")
    if adjustment not in ("raw", "qfq", "hfq"):
        raise MarketInputError("ADJUSTMENT_REQUIRED")
    if not isinstance(source_version, str) or not source_version.strip():
        raise MarketInputError("SOURCE_VERSION_REQUIRED")
    if not isinstance(horizons, tuple) or not horizons or any(type(h) is not int or h <= 0 for h in horizons) or len(set(horizons)) != len(horizons):
        raise MarketInputError("HORIZON_INVALID")
    cutoff = _day(as_of)

    def normalized(rows):
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise MarketInputError("SERIES_INVALID")
        found = {}
        for row in rows:
            if not isinstance(row, Mapping) or set(row) != {"trade_date", "close"}:
                raise MarketInputError("SERIES_ROW_INVALID")
            day = _day(row["trade_date"])
            close = _price(row["close"])
            if day in found:
                raise MarketInputError("DUPLICATE_SESSION")
            if day <= cutoff:
                found[day] = close
        return sorted(found.items())

    primary = normalized(series)
    base = dict(normalized(benchmark)) if benchmark is not None else None
    if not primary or primary[-1][0] != cutoff:
        return {"entity_id": entity_id, "as_of": as_of, "status": "no-observation-at-date",
                "adjustment": adjustment, "source_version": source_version, "horizons": []}
    entries = []
    for horizon in horizons:
        if len(primary) <= horizon:
            entries.append({"sessions": horizon, "status": "insufficient-history", "available_sessions": len(primary) - 1})
            continue
        old_day, old_price = primary[-horizon - 1]
        now_day, now_price = primary[-1]
        absolute = (now_price / old_price - Decimal(1)) * 100
        entry = {"sessions": horizon, "status": "observed", "baseline_date": old_day.isoformat(),
                 "end_date": now_day.isoformat(), "change_pct": str(absolute)}
        if base is None:
            entry["relative_status"] = "benchmark-not-provided"
        elif old_day not in base or now_day not in base:
            entry["relative_status"] = "benchmark-session-mismatch"
        else:
            bench_change = (base[now_day] / base[old_day] - Decimal(1)) * 100
            entry["benchmark_change_pct"] = str(bench_change)
            entry["excess_change_pp"] = str(absolute - bench_change)
            entry["relative_status"] = "observed"
        entries.append(entry)
    return {"entity_id": entity_id, "as_of": as_of, "status": "observed",
            "adjustment": adjustment, "source_version": source_version, "horizons": entries}
