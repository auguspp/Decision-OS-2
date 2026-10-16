"""US slice of global context over existing Relay captures; no network or writer."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from decision_kernel.application.market_relay_inputs import (
    MarketRelayError, _daily, _day, _lineage, _range, _require, _rows, _view,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import digest, encoded
from decision_kernel.observations.us_index_daily import index_context, observe_us_index

US_INDEX_CODES = ("DJI", "IXIC", "RUT", "SPX")
INDEX_FIELDS = {"ts_code", "trade_date", "close"}
US_CALENDAR_FIELDS = {"cal_date", "is_open"}


def _bounds(start: str, end: str) -> tuple[str, str]:
    lo, hi = _range(start, end)
    _require((date.fromisoformat(hi) - date.fromisoformat(lo)).days <= 3660,
             "US_INDEX_RANGE_TOO_LARGE")
    return lo, hi


def _us_calendar(result: dict, registry: RelayRegistry, start: str, end: str) -> tuple[list[str], dict]:
    lo, hi = _bounds(start, end)
    view = _view(result, registry, "us_tradecal", {"start_date": start, "end_date": end},
                 US_CALENDAR_FIELDS)
    days = {}
    for row in _rows(view):
        day, flag = _day(row["cal_date"]), row["is_open"]
        _require(lo <= day <= hi and day not in days and type(flag) in (str, int)
                 and flag in ("0", "1", 0, 1), "US_CALENDAR_ROW")
        days[day] = str(flag) == "1"
    _require(bool(days), "US_CALENDAR_EMPTY")
    _require(len(days) == (date.fromisoformat(hi) - date.fromisoformat(lo)).days + 1,
             "US_CALENDAR_GAP")
    return [day for day in sorted(days) if days[day]], view


def qualified_us_trade_calendar(result: dict, *, registry: RelayRegistry,
                                start: str, end: str) -> list[str]:
    """Require all open and closed dates from us_tradecal, not an A-share calendar."""
    return _us_calendar(result, registry, start, end)[0]


def read_us_index_context(*, calendar_result: dict, index_results: Mapping[str, dict],
                          registry: RelayRegistry, declared_indices: Sequence[str],
                          start: str, end: str, horizons: tuple[int, ...] = (5, 20, 60),
                          offset: int = 0, limit: int = 50,
                          entity_id: str | None = None) -> dict:
    """Read every declared supported index, isolating bad or missing index captures.

    This is a bounded declared US context, never the entire world or stock-candidate
    universe. The shared calendar must qualify before any window is computed.
    Qualification counts and the content version are independent of pagination.
    """
    _, hi = _bounds(start, end)
    _require(isinstance(declared_indices, (list, tuple)) and len(declared_indices) <= len(US_INDEX_CODES)
             and all(isinstance(code, str) and code in US_INDEX_CODES for code in declared_indices)
             and len(set(declared_indices)) == len(declared_indices), "US_INDEX_SCOPE")
    declared = sorted(declared_indices)
    _require(isinstance(index_results, Mapping) and set(index_results) <= set(declared),
             "UNDECLARED_US_INDEX")
    _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
             "US_INDEX_PAGE")
    _require(entity_id is None or isinstance(entity_id, str) and entity_id in declared,
             "US_INDEX_NOT_DECLARED")
    sessions, calendar_view = _us_calendar(calendar_result, registry, start, end)
    _require(hi in sessions, "US_INDEX_END_NOT_MARKET_SESSION")
    index_context(as_of=hi, market_sessions=sessions, horizons=horizons)
    allowed, entries = set(sessions), []
    for code in declared:
        entry = {"entity_id": code, "source_qualification": {"status": "not-supplied"},
                 "source_lineage": None, "observation": None}
        if code in index_results:
            try:
                view = _view(index_results[code], registry, "index_global",
                    {"ts_code": code, "start_date": start, "end_date": end}, INDEX_FIELDS)
                series = _daily(view, code, allowed)
                entry["observation"] = observe_us_index(entity_id=code, series=series,
                    as_of=hi, market_sessions=sessions, horizons=horizons)
                entry["source_lineage"] = _lineage(view)
                entry["source_qualification"] = {
                    "status": "checked" if series else "checked-empty", "row_count": len(series)}
            except MarketRelayError as exc:
                entry["source_qualification"] = {"status": "rejected", "reason_code": str(exc)}
        entries.append(entry)
    counts = {status: sum(e["source_qualification"]["status"] == status for e in entries)
              for status in ("checked", "checked-empty", "rejected", "not-supplied")}
    result = {"schema": "relay-us-index-context-v1", "family": "indices", "market": "US",
        "as_of": hi, "start": _day(start), "unit": "index-points", "currency": None,
        "scope_semantics": "explicit-supported-US-index-scope-not-world-coverage-or-stock-candidates",
        "scope_count": len(declared), "declared_indices": declared,
        "observed_count": sum(e["observation"] is not None and e["observation"]["status"]
                              in ("observed", "partial-history") for e in entries),
        "source_qualification_counts": counts, "market_sessions": sessions,
        "calendar_lineage": _lineage(calendar_view),
        "qualification_limits": [
            "US_DJI_IXIC_RUT_SPX_ONLY_OTHER_GLOBAL_FAMILIES_NOT_IMPLEMENTED_HERE",
            "SOURCE_US_MARKET_DATE_NOT_CROSS_MARKET_SIMULTANEOUS_INSTANT",
            "EXPLICIT_END_NOT_LATEST_COMPLETED_SESSION_CERTIFICATION",
            "INDEX_LEVEL_RATIO_NOT_MONETARY_FX_ADJUSTED_OR_CERTIFIED_TOTAL_RETURN",
            "SOURCE_DECLARATION_NOT_INDEPENDENT_INDEX_COMPOSITION_OR_METHOD_VERIFICATION",
            "CAPTURE_VIEW_HASH_NOT_SOURCEREF_OR_PUBLICATION_PERMISSION",
            "NO_SECURITY_MASTER_THS_DEPENDENCY_RESEARCH_OR_ECONOMIC_APPROVAL"]}
    result["view_version"] = digest(encoded({**result, "horizons": horizons, "entries": entries}))
    selected = entries if entity_id is None else [e for e in entries if e["entity_id"] == entity_id]
    page = selected[offset:offset + limit]
    result.update(entries=page, matched_count=len(selected),
                  pagination={"offset": offset, "limit": limit, "returned": len(page),
                              "next_offset": offset + len(page) if offset + len(page) < len(selected) else None})
    return result
