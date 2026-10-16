"""Source-declared stock references and end-anchored factor-adjusted reading.

No network, custody write, issuer matching, corporate-action event inference or
historical PIT certification. Generic capture validation belongs to #12's reader.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Context, localcontext

from decision_kernel.application.market_radar import read_market_surface
from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, MarketRelayError, _calendar, _daily, _day, _lineage, _range,
    _require, _rows, _security, _sessions, _view,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import digest, encoded
from decision_kernel.observations.market_daily import MarketInputError, _price

LIST_STATUSES = ("L", "D", "P", "G")
MASTER_FIELDS = {"ts_code", "symbol", "name", "exchange", "curr_type", "list_status",
                 "list_date", "delist_date"}
FACTOR_FIELDS = {"ts_code", "trade_date", "adj_factor"}
NORMALIZATION = "raw-close-times-date-factor-divided-by-explicit-end-factor-v1"


def qualified_security_master(results: Mapping[str, dict], *, registry: RelayRegistry,
                              exchange: str) -> dict:
    """Read entire supplied exchange/status slices, not a historical universe.

    Missing status slices are explicit; any malformed supplied slice is refused.
    Codes are source security identities, not permanent IDs or issuer mappings.
    """
    _require(isinstance(exchange, str) and exchange in EXCHANGES, "EXCHANGE_UNSUPPORTED")
    _require(isinstance(results, Mapping) and bool(results)
             and all(isinstance(k, str) and k in LIST_STATUSES for k in results), "MASTER_SCOPE")
    securities, lineage, counts = {}, {}, {}
    for status in LIST_STATUSES:
        if status not in results:
            continue
        view = _view(results[status], registry, "stock_basic",
                     {"exchange": exchange, "list_status": status}, MASTER_FIELDS)
        lineage[status] = _lineage(view)
        counts[status] = view["row_count"]
        for row in _rows(view):
            code = _security(row["ts_code"])
            _require(code.endswith("." + EXCHANGES[exchange]) and row["exchange"] == exchange
                     and row["symbol"] == code[:6] and row["list_status"] == status,
                     "MASTER_IDENTITY")
            _require(row["curr_type"] == "CNY", "MASTER_CURRENCY_UNSUPPORTED")
            name = row["name"]
            _require(isinstance(name, str) and 0 < len(name) <= 256
                     and name == name.strip() and name.isprintable(), "MASTER_NAME")
            listed = None if row["list_date"] in (None, "") else _day(row["list_date"])
            delisted = None if row["delist_date"] in (None, "") else _day(row["delist_date"])
            _require(status == "G" or listed is not None, "MASTER_LIST_DATE_REQUIRED")
            _require((status == "D" and delisted is not None and listed <= delisted)
                     or (status != "D" and delisted is None), "MASTER_DELIST_DATE")
            _require(code not in securities, "MASTER_DUPLICATE_SECURITY")
            securities[code] = {"ts_code": code, "symbol": row["symbol"], "name": name,
                "exchange": exchange, "currency": "CNY", "source_list_status": status,
                "list_date": listed, "delist_date": delisted}
    return {"securities": dict(sorted(securities.items())), "scope_count": len(securities),
        "supplied_statuses": [s for s in LIST_STATUSES if s in results],
        "not_supplied_statuses": [s for s in LIST_STATUSES if s not in results],
        "status_counts": counts, "lineage": lineage,
        "version": digest(encoded({"securities": securities, "lineage": lineage})),
        "scope_semantics": "all-returned-rows-in-supplied-status-slices-not-full-market",
        "time_semantics": "retrieval-snapshots-not-atomic-or-historical-membership",
        "identity_semantics": "source-security-code-not-issuer-or-permanent-security-id"}


def _factors(view: dict, code: str, allowed: set[str]) -> list[dict]:
    found = {}
    for row in _rows(view):
        day = _day(row["trade_date"])
        _require(row["ts_code"] == code and day in allowed and day not in found, "FACTOR_ROW")
        try:
            value = _price(row["adj_factor"])
        except MarketInputError:
            raise MarketRelayError("MARKET_RELAY_FACTOR_VALUE") from None
        found[day] = str(value)
    return [{"trade_date": day, "adj_factor": found[day]} for day in sorted(found)]


def qualified_adj_factors(result: dict, *, registry: RelayRegistry, ts_code: str,
                          start: str, end: str, calendar: Sequence[str]) -> list[dict]:
    """Positive factors at exact sessions; no filling, monotonicity or event inference."""
    lo, hi = _range(start, end)
    code = _security(ts_code)
    allowed = _sessions(calendar, lo, hi)
    view = _view(result, registry, "adj_factor", {"ts_code": code,
                 "start_date": start, "end_date": end}, FACTOR_FIELDS)
    return _factors(view, code, allowed)


def _within_listing(day: str, reference: dict) -> bool:
    # A delisting date is not evidence of trading on that date. This reader
    # conservatively excludes it. P is suspended listing, not a daily halt feed.
    return (reference["source_list_status"] != "G" and reference["list_date"] is not None
            and reference["list_date"] <= day
            and (reference["delist_date"] is None or day < reference["delist_date"]))


def _adjust(prices: list[dict], factors: list[dict], sessions: list[str], end: str) -> tuple[list, dict]:
    values = {row["trade_date"]: _price(row["adj_factor"]) for row in factors}
    price_days = {row["trade_date"] for row in prices}
    details = {"anchor_date": end, "anchor_factor": str(values[end]) if end in values else None,
        "missing_factor_sessions": [d for d in sessions if d not in values],
        "price_dates_without_factor": sorted(price_days - values.keys()),
        "factor_dates_without_price": sorted(values.keys() - price_days),
        "factor_change_dates": [b for a, b in zip(sessions, sessions[1:])
                                if a in values and b in values and values[a] != values[b]]}
    if end not in values:
        return [], {**details, "status": "missing-anchor-factor"}
    output = []
    try:
        with localcontext(Context(prec=34)):
            for row in prices:
                day = row["trade_date"]
                if day in values:
                    value = _price(row["close"]) * values[day] / values[end]
                    output.append({"trade_date": day, "close": str(_price(value))})
    except MarketInputError:
        raise MarketRelayError("MARKET_RELAY_ADJUSTED_CLOSE_RANGE") from None
    status = "factor-gaps" if details["price_dates_without_factor"] else "computed" if output else "no-prices"
    return output, {**details, "status": status}


def read_security_stock_surface(*, calendar_result: dict, master_results: Mapping[str, dict],
                                stock_results: Mapping[str, dict], factor_results: Mapping[str, dict],
                                registry: RelayRegistry, exchange: str, start: str, end: str,
                                horizons: tuple[int, ...] = (5, 20, 60),
                                offset: int = 0, limit: int = 50,
                                entity_id: str | None = None) -> dict:
    """Read companion raw/qfq surfaces with one master-derived, undropped scope.

    Bad master/calendar data refuses the shared scope. Bad stock/factor data is
    isolated to the affected security and convention. No candidate policy runs.
    """
    lo, hi = _range(start, end)
    master = qualified_security_master(master_results, registry=registry, exchange=exchange)
    references = master["securities"]
    declared = list(references)
    for results in (stock_results, factor_results):
        _require(isinstance(results, Mapping) and set(results) <= set(declared), "UNDECLARED_STOCK")
    if entity_id is not None:
        _security(entity_id)
    calendar_view = _view(calendar_result, registry, "trade_cal",
        {"exchange": exchange, "start_date": start, "end_date": end}, {"exchange", "cal_date", "is_open"})
    sessions = _calendar(calendar_view, exchange, lo, hi)
    _require(hi in sessions, "END_NOT_MARKET_SESSION")
    allowed = set(sessions)
    raw, adjusted, price_sources, factor_sources, qualifications = {}, {}, {}, {}, {}
    for code in declared:
        reference = references[code]
        state = {"price": {"status": "not-supplied"}, "factor": {"status": "not-supplied"},
                 "adjustment": {"status": "inputs-unavailable"}}
        scope = {"ts_code": code, "start_date": start, "end_date": end}
        if code in stock_results:
            try:
                view = _view(stock_results[code], registry, "daily", scope, {"ts_code", "trade_date", "close"})
                series = _daily(view, code, allowed)
                _require(all(_within_listing(row["trade_date"], reference) for row in series), "PRICE_OUTSIDE_LISTING")
                raw[code] = series
                price_sources[code] = _lineage(view)
                state["price"] = {"status": "checked", "row_count": len(series)}
            except MarketRelayError as exc:
                state["price"] = {"status": "rejected", "reason_code": str(exc)}
        factors = None
        if code in factor_results:
            try:
                view = _view(factor_results[code], registry, "adj_factor", scope, FACTOR_FIELDS)
                factors = _factors(view, code, allowed)
                factor_sources[code] = _lineage(view)
                state["factor"] = {"status": "checked", "row_count": len(factors)}
            except MarketRelayError as exc:
                state["factor"] = {"status": "rejected", "reason_code": str(exc)}
        if code in raw and factors is not None:
            try:
                series, details = _adjust(raw[code], factors, sessions, hi)
                state["adjustment"] = details
                if details["anchor_factor"] is not None:
                    adjusted[code] = series
            except MarketRelayError as exc:
                state["adjustment"] = {"status": "rejected", "reason_code": str(exc)}
        state["within_reported_listing_at_end"] = _within_listing(hi, reference)
        qualifications[code] = state
    calendar_source = _lineage(calendar_view)
    common = {"kind": "stock", "as_of": hi, "declared_entities": declared, "market_sessions": sessions,
        "calendar_version": calendar_source["view_sha256"], "universe_version": master["version"],
        "coverage": "partial-universe", "market": exchange, "currency": "CNY", "horizons": horizons}
    surfaces = {}
    for convention, entities, sources in (("raw", raw, price_sources),
            ("qfq", adjusted, {"prices": price_sources, "factors": factor_sources,
                               "normalization": NORMALIZATION, "anchor_date": hi})):
        surface = read_market_surface({**common, "adjustment": convention, "entities": entities,
            "source_version": digest(encoded(sources))}, offset=offset, limit=limit, entity_id=entity_id)
        for entry in surface["entries"]:
            code = entry["entity_id"]
            entry["security_reference"] = references[code]
            entry["qualification"] = qualifications[code]
            entry["price_lineage"] = price_sources.get(code)
            entry["factor_lineage"] = factor_sources.get(code) if convention == "qfq" else None
        surfaces[convention] = surface
    counts = {kind: {status: sum(q[kind]["status"] == status for q in qualifications.values())
                    for status in ("checked", "rejected", "not-supplied")} for kind in ("price", "factor")}
    counts["adjustment"] = {status: sum(q["adjustment"]["status"] == status for q in qualifications.values())
                            for status in ("computed", "factor-gaps", "missing-anchor-factor", "no-prices",
                                           "rejected", "inputs-unavailable")}
    return {"schema": "relay-security-stock-view-v1", "scope_count": len(declared),
        "master": {k: v for k, v in master.items() if k != "securities"},
        "calendar_lineage": calendar_source, "qualification_counts": counts,
        "normalization": NORMALIZATION, **surfaces, "qualification_limits": [
            "SOURCE_DECLARED_REFERENCE_NOT_ISSUER_OR_PERMANENT_ID_MAPPING",
            "STATUS_SNAPSHOTS_NOT_ATOMIC_HISTORICAL_UNIVERSE_OR_SURVIVORSHIP_FREE",
            "LISTING_INTERVAL_CHECK_NOT_DAILY_TRADING_OR_SUSPENSION_CERTIFICATION",
            "FACTOR_CHANGES_NOT_IDENTIFIED_CORPORATE_ACTION_EVENTS",
            "END_ANCHORED_FACTOR_RATIO_NOT_INDEPENDENT_TOTAL_RETURN_OR_PIT_CERTIFICATION",
            "EXPLICIT_END_NOT_LATEST_COMPLETED_SESSION_CERTIFICATION",
            "VIEW_HASH_NOT_SOURCEREF_OR_PUBLISHER_AUTHENTICATION",
            "NO_FULL_MARKET_DISCOVERY_OR_ECONOMIC_APPROVAL"]}
