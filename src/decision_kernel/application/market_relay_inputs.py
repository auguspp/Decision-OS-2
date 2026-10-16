"""Source-specific raw market reading over the existing registered Relay result.

Generic capture checks stay with research_sources.inspect_result. This module
never requests data, writes custody, adjusts prices, or certifies a stock universe.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import re

from decision_kernel.application.market_radar import read_market_surface
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.observations.market_daily import MarketInputError, _price, trade_day

EXCHANGES = {"SSE": "SH", "SZSE": "SZ"}


class MarketRelayError(MarketInputError):
    """Stable input refusal, never publisher prose or credentials."""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise MarketRelayError("MARKET_RELAY_" + code)


def _day(value: object) -> str:
    _require(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{8}", value)), "DATE_INVALID")
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
    except ValueError:
        raise MarketRelayError("MARKET_RELAY_DATE_INVALID") from None


def _range(start: str, end: str) -> tuple[str, str]:
    lo, hi = _day(start), _day(end)
    _require(lo <= hi, "DATE_RANGE_INVALID")
    return lo, hi


def _security(code: object) -> str:
    _require(isinstance(code, str) and bool(re.fullmatch(r"[0-9]{6}\.(SH|SZ)", code)),
             "SECURITY_INVALID")
    return code


def _view(result: dict, registry: RelayRegistry, api: str, scope: dict, fields: set) -> dict:
    """One shared inspector; only market-specific scope restrictions are added."""
    _require(isinstance(registry, RelayRegistry), "REGISTRY_REQUIRED")
    _require(isinstance(result, dict) and isinstance(result.get("use"), dict), "RESULT_INVALID")
    use = result["use"]
    params = use.get("parameters")
    _require(use.get("api") == api and isinstance(params, dict), "QUERY_INVALID")
    _require(set(scope) | {"fields", "limit"} <= params.keys()
             and params.keys() <= set(scope) | {"fields", "limit", "offset"}
             and all(params.get(k) == v for k, v in scope.items()), "QUERY_SCOPE")
    _require(params.get("offset", "0") == "0", "CAPTURE_MUST_START_AT_ZERO")
    _require(isinstance(params.get("fields"), str)
             and fields <= set(params["fields"].split(",")), "FIELDS_NOT_REQUESTED")
    try:
        view = inspect_result(result, registry)
    except RecordError as exc:
        raise MarketRelayError(str(exc)) from None
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        raise MarketRelayError("MARKET_RELAY_RESULT_INVALID") from None
    _require(view["complete"] and result.get("status") in ("captured", "reused-in-this-batch"),
             "CAPTURE_INCOMPLETE")
    capture = result["capture"]
    _require(capture.get("snapshot") == registry.snapshot
             and capture.get("source_identity") == "THIRD_PARTY_RELAY_NOT_ISSUER",
             "CAPTURE_CONTEXT_CHANGED")
    return view


def _rows(view: dict):
    for page in view["pages"]:
        table = page["table"]
        for row in table["items"]:
            yield dict(zip(table["fields"], row))


def _calendar(view: dict, exchange: str, lo: str, hi: str) -> list[str]:
    days = {}
    for row in _rows(view):
        _require(row["exchange"] == exchange and type(row["is_open"]) in (str, int)
                 and row["is_open"] in ("0", "1", 0, 1), "CALENDAR_ROW")
        day = _day(row["cal_date"])
        _require(lo <= day <= hi and day not in days, "CALENDAR_ROW")
        days[day] = str(row["is_open"]) == "1"
    _require(bool(days), "CALENDAR_EMPTY")
    # All unique dates lie in the interval; cardinality proves no missing day.
    # No date.max + timedelta overflow or unbounded synthetic date-range loop.
    _require(len(days) == (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1,
             "CALENDAR_GAP")
    return [day for day in sorted(days) if days[day]]


def qualified_trade_calendar(result: dict, *, registry: RelayRegistry,
                             exchange: str, start: str, end: str) -> list[str]:
    """Full calendar, including closed days; no weekday or previous-close heuristic."""
    lo, hi = _range(start, end)
    _require(isinstance(exchange, str) and exchange in EXCHANGES, "EXCHANGE_UNSUPPORTED")
    view = _view(result, registry, "trade_cal", {"exchange": exchange, "start_date": start,
                 "end_date": end}, {"exchange", "cal_date", "is_open"})
    return _calendar(view, exchange, lo, hi)


def _sessions(calendar: Sequence[str], lo: str, hi: str) -> set[str]:
    _require(isinstance(calendar, (list, tuple)), "CALENDAR_INVALID")
    try:
        days = [trade_day(day).isoformat() for day in calendar]
    except MarketInputError:
        raise MarketRelayError("MARKET_RELAY_CALENDAR_INVALID") from None
    _require(all(lo <= day <= hi for day in days)
             and all(a < b for a, b in zip(days, days[1:])), "CALENDAR_INVALID")
    return set(days)


def _daily(view: dict, code: str, allowed: set[str]) -> list[dict]:
    results, seen = [], set()
    for row in _rows(view):
        day = _day(row["trade_date"])
        _require(row["ts_code"] == code and day in allowed and day not in seen, "DAILY_ROW")
        seen.add(day)
        try:
            price = _price(row["close"])
        except MarketInputError:
            raise MarketRelayError("MARKET_RELAY_DAILY_CLOSE") from None
        results.append({"trade_date": day, "close": str(price)})
    return sorted(results, key=lambda row: row["trade_date"])


def qualified_daily(result: dict, *, registry: RelayRegistry, ts_code: str,
                    start: str, end: str, calendar: Sequence[str]) -> list[dict]:
    """Raw closes; missing sessions remain missing, including a legitimate empty page."""
    lo, hi = _range(start, end)
    code = _security(ts_code)
    allowed = _sessions(calendar, lo, hi)
    view = _view(result, registry, "daily", {"ts_code": code, "start_date": start,
                 "end_date": end}, {"ts_code", "trade_date", "close"})
    return _daily(view, code, allowed)


def _lineage(view: dict) -> dict:
    return {"query_key": view["query_key"], "view_sha256": digest(encoded(view)),
            "api": view["api"], "parameters": view["parameters"],
            "pages": [{k: page[k] for k in ("offset", "captured_at", "transport")}
                      for page in view["pages"]]}


def read_relay_stock_surface(*, calendar_result: dict, stock_results: Mapping[str, dict],
                            registry: RelayRegistry, exchange: str, start: str, end: str,
                            declared_entities: Sequence[str], coverage: str,
                            horizons: tuple[int, ...] = (5, 20, 60),
                            offset: int = 0, limit: int = 50,
                            entity_id: str | None = None) -> dict:
    """Actual read consumer. Invalid stock data is isolated, never dropped from scope.

    This bounded reader only supports SH/SZ raw daily A-share input. Code syntax
    does not prove listing/issuer identity. End is explicit, not certified latest.
    """
    lo, hi = _range(start, end)
    _require(isinstance(exchange, str) and exchange in EXCHANGES, "EXCHANGE_UNSUPPORTED")
    _require(isinstance(declared_entities, (list, tuple)), "UNIVERSE_INVALID")
    declared = [_security(code) for code in declared_entities]
    _require(len(declared) == len(set(declared))
             and all(code.endswith("." + EXCHANGES[exchange]) for code in declared), "UNIVERSE_INVALID")
    _require(isinstance(stock_results, Mapping) and set(stock_results) <= set(declared), "UNDECLARED_STOCK")
    _require(coverage in ("partial-universe", "watchlist"), "UNIVERSE_NOT_PROVEN")
    if entity_id is not None:
        _security(entity_id)
    calendar_view = _view(calendar_result, registry, "trade_cal",
        {"exchange": exchange, "start_date": start, "end_date": end}, {"exchange", "cal_date", "is_open"})
    sessions = _calendar(calendar_view, exchange, lo, hi)
    _require(hi in sessions, "END_NOT_MARKET_SESSION")
    entities, qualification, sources = {}, {}, {}
    allowed = set(sessions)
    for code in sorted(declared):
        if code not in stock_results:
            qualification[code] = {"status": "not-supplied"}
            continue
        try:
            view = _view(stock_results[code], registry, "daily",
                {"ts_code": code, "start_date": start, "end_date": end}, {"ts_code", "trade_date", "close"})
            entities[code] = _daily(view, code, allowed)
        except MarketRelayError as exc:
            qualification[code] = {"status": "rejected", "reason_code": str(exc)}
        else:
            qualification[code] = {"status": "raw-input-checked"}
            sources[code] = _lineage(view)
    calendar_source = _lineage(calendar_view)
    payload = {"kind": "stock", "as_of": hi, "entities": entities,
        "declared_entities": declared, "market_sessions": sessions,
        "calendar_version": calendar_source["view_sha256"], "adjustment": "raw",
        "source_version": digest(encoded(sources)),
        "universe_version": digest(encoded({"exchange": exchange, "declared": sorted(declared), "coverage": coverage})),
        "coverage": coverage, "market": exchange, "currency": "CNY", "horizons": horizons}
    surface = read_market_surface(payload, offset=offset, limit=limit, entity_id=entity_id)
    for entry in surface["entries"]:
        entry["source_qualification"] = qualification[entry["entity_id"]]
        entry["source_lineage"] = sources.get(entry["entity_id"])
    surface["source_qualification_counts"] = {
        "supplied": len(stock_results), "accepted": len(entities),
        "rejected": sum(v["status"] == "rejected" for v in qualification.values()),
        "not_supplied": len(declared) - len(stock_results)}
    surface["calendar_lineage"] = calendar_source
    surface["qualification_limits"] = [
        "RAW_A_SHARE_PRICE_RATIO_NOT_ADJUSTED_OR_TOTAL_RETURN",
        "SYNTAX_AND_RESPONSE_IDENTITY_NOT_SECURITY_MASTER_OR_ISSUER_MAPPING",
        "EXPLICIT_END_NOT_LATEST_COMPLETED_SESSION_CERTIFICATION",
        "CAPTURE_VIEW_HASH_NOT_SOURCEREF_OR_PUBLISHER_AUTHENTICATION",
        "NO_FULL_UNIVERSE_OR_PIT_OR_ECONOMIC_APPROVAL"]
    return surface
