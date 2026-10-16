"""Strict market qualification of existing registered Relay capture envelopes.

Pure adapter: no network, credentials, storage, corporate-action adjustment, or
universe discovery. A completed generic page is not proof of field semantics.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from decision_kernel.observations.market_daily import MarketInputError


class MarketRelayError(MarketInputError):
    """Stable refusal code for unqualified market input."""


def _day(value):
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise MarketRelayError("MARKET_RELAY_DATE_INVALID")
    try:
        result = date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise MarketRelayError("MARKET_RELAY_DATE_INVALID") from None
    return result.isoformat()


def _capture(capture, api, required):
    if (not isinstance(capture, Mapping) or capture.get("api") != api
            or capture.get("complete") is not True or capture.get("error") is not None
            or not isinstance(capture.get("pages"), list) or not capture["pages"]):
        raise MarketRelayError("MARKET_RELAY_CAPTURE_INCOMPLETE")
    params = capture.get("parameters")
    if not isinstance(params, Mapping) or not isinstance(params.get("fields"), str):
        raise MarketRelayError("MARKET_RELAY_QUERY_INVALID")
    requested = params["fields"].split(",")
    if not required <= set(requested) or len(requested) != len(set(requested)):
        raise MarketRelayError("MARKET_RELAY_FIELDS_NOT_REQUESTED")
    offset_text = params.get("offset", "0")
    if not isinstance(offset_text, str) or not offset_text.isascii() or not offset_text.isdigit():
        raise MarketRelayError("MARKET_RELAY_QUERY_INVALID")
    offset = int(offset_text)
    results = []
    for page in capture["pages"]:
        table = page.get("table") if isinstance(page, Mapping) else None
        if not isinstance(table, Mapping):
            raise MarketRelayError("MARKET_RELAY_TABLE_INVALID")
        fields, items = table.get("fields"), table.get("items")
        if (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
                or len(set(fields)) != len(fields) or not required <= set(fields)
                or not isinstance(items, list) or type(page.get("offset")) is not int
                or page["offset"] != offset):
            raise MarketRelayError("MARKET_RELAY_TABLE_INVALID")
        for values in items:
            if not isinstance(values, list) or len(values) != len(fields):
                raise MarketRelayError("MARKET_RELAY_TABLE_INVALID")
            results.append(dict(zip(fields, values)))
        offset += len(items)
    return results


def qualified_trade_calendar(capture, *, exchange, start, end):
    """Require the declared complete daily exchange calendar, including closed days."""
    lo, hi = _day(start), _day(end)
    if not isinstance(exchange, str) or not exchange or lo > hi:
        raise MarketRelayError("MARKET_RELAY_CALENDAR_SCOPE")
    params = capture.get("parameters", {}) if isinstance(capture, Mapping) else {}
    if any(params.get(k) != v for k, v in
           (("exchange", exchange), ("start_date", start), ("end_date", end))):
        raise MarketRelayError("MARKET_RELAY_CALENDAR_SCOPE")
    rows = _capture(capture, "trade_cal", {"exchange", "cal_date", "is_open"})
    days = {}
    for row in rows:
        if row["exchange"] != exchange or type(row["is_open"]) not in (str, int) or row["is_open"] not in ("0", "1", 0, 1):
            raise MarketRelayError("MARKET_RELAY_CALENDAR_ROW")
        day = _day(row["cal_date"])
        if not lo <= day <= hi or day in days:
            raise MarketRelayError("MARKET_RELAY_CALENDAR_ROW")
        days[day] = str(row["is_open"]) == "1"
    if not days:
        raise MarketRelayError("MARKET_RELAY_CALENDAR_EMPTY")
    current, last = date.fromisoformat(lo), date.fromisoformat(hi)
    while current <= last:
        if current.isoformat() not in days:
            raise MarketRelayError("MARKET_RELAY_CALENDAR_GAP")
        current += timedelta(days=1)
    return [day for day in sorted(days) if days[day]]


def qualified_daily(capture, *, ts_code, start, end, calendar):
    """Project raw daily closes only, with explicit missing sessions."""
    lo, hi = _day(start), _day(end)
    if not isinstance(ts_code, str) or not ts_code or lo > hi:
        raise MarketRelayError("MARKET_RELAY_DAILY_SCOPE")
    if (not isinstance(calendar, list) or len(calendar) != len(set(calendar))
            or any(not isinstance(d, str) or len(d) != 10 for d in calendar)):
        raise MarketRelayError("MARKET_RELAY_DAILY_SCOPE")
    params = capture.get("parameters", {}) if isinstance(capture, Mapping) else {}
    if any(params.get(k) != v for k, v in
           (("ts_code", ts_code), ("start_date", start), ("end_date", end))):
        raise MarketRelayError("MARKET_RELAY_DAILY_SCOPE")
    rows = _capture(capture, "daily", {"ts_code", "trade_date", "close"})
    allowed = set(calendar)
    results, seen = [], set()
    for row in rows:
        day = _day(row["trade_date"])
        if row["ts_code"] != ts_code or not lo <= day <= hi or day not in allowed or day in seen:
            raise MarketRelayError("MARKET_RELAY_DAILY_ROW")
        seen.add(day)
        raw = row["close"]
        if type(raw) not in (str, int, float) or len(str(raw)) > 128:
            raise MarketRelayError("MARKET_RELAY_DAILY_CLOSE")
        try:
            number = Decimal(str(raw))
        except InvalidOperation:
            raise MarketRelayError("MARKET_RELAY_DAILY_CLOSE") from None
        if not number.is_finite() or number <= 0 or not -99 <= number.adjusted() <= 99:
            raise MarketRelayError("MARKET_RELAY_DAILY_CLOSE")
        results.append({"trade_date": day, "close": str(number)})
    return sorted(results, key=lambda row: row["trade_date"])
