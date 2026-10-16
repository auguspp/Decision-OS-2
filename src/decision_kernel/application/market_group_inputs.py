"""THS catalogue, reported members and index-level reading over retained Relay results.

Retrieval clocks are NOT effective membership dates. No network, source writes,
member-portfolio reconstruction or discovery/issuer/PIT certification.
"""
from __future__ import annotations

from collections.abc import Mapping
import re
import math

from decision_kernel.application.market_radar import membership_overlap, read_market_surface
from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, MarketRelayError, _calendar, _daily, _day, _lineage, _range, _require, _rows, _view,
)
from decision_kernel.application.market_security_inputs import read_security_stock_surface
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.observations.market_daily import MarketInputError

GROUP_TYPES = {"sector": "I", "concept": "N"}
CATALOG_FIELDS = {"ts_code", "name", "count", "exchange", "list_date", "type"}
MEMBER_FIELDS = {"ts_code", "con_code", "con_name"}
LEVEL_FIELDS = {"ts_code", "trade_date", "close"}
GROUP_REQUIRED = {"catalog_result", "member_results", "index_results", "calendar_result", "exchange", "start", "end"}
STOCK_REQUIRED = {"master_results", "factor_results", "stock_results", "calendar_result", "exchange", "start", "end"}


def _index_code(value: object) -> str:
    _require(isinstance(value, str) and bool(re.fullmatch(r"[0-9]{6}\.TI", value)), "GROUP_CODE")
    return value


def _name(value: object) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 256
             and value == value.strip() and value.isprintable(), "GROUP_NAME")
    return value


CLASSIFICATION_CODES = ("GROUP_CODE", "GROUP_DUPLICATE_CODE", "GROUP_EXCHANGE_MISMATCH", "GROUP_TYPE_MISMATCH")


def _classification_checks(row: dict, seen: Mapping, expected_type: str) -> tuple:
    """One set of predicates for qualification and non-publishing diagnostics.

    The caller owns the source slice and seen-code scope. Invalid identifiers
    never participate in duplicate matching; no value is coerced or rewritten.
    """
    try:
        code = _index_code(row.get("ts_code"))
    except MarketRelayError:
        code = None
    return tuple(zip(CLASSIFICATION_CODES, (
        code is not None,
        code is None or code not in seen,
        row.get("exchange") == "A",
        row.get("type") == expected_type,
    )))


def qualified_ths_catalog(result: dict, *, registry: RelayRegistry, kind: str) -> dict:
    """All rows in one explicit A/I or A/N slice; not a historical classification."""
    _require(isinstance(kind, str) and kind in GROUP_TYPES, "GROUP_KIND")
    view = _view(result, registry, "ths_index", {"exchange": "A", "type": GROUP_TYPES[kind]}, CATALOG_FIELDS)
    groups = {}
    namespace = "ths-a-" + kind
    for row in _rows(view):
        for reason, passed in _classification_checks(row, groups, GROUP_TYPES[kind]):
            _require(passed, reason)
        code = row["ts_code"]
        count = row["count"]
        if count in (None, ""):
            count = None
        elif type(count) is float:
            # Relay may serialize integer counts as JSON numbers with a decimal
            # point. Accept the same bounded integer VALUE, never round a fraction.
            _require(math.isfinite(count) and count.is_integer() and 0 <= count <= 999999999,
                     "GROUP_COUNT")
            count = int(count)
        else:
            _require(type(count) in (int, str) and bool(re.fullmatch(r"[0-9]{1,9}", str(count))), "GROUP_COUNT")
            count = int(count)
        groups[code] = {"ts_code": code, "entity_id": namespace + ":" + code,
            "name": _name(row["name"]), "kind": kind, "source_type": GROUP_TYPES[kind],
            "reported_member_count": count,
            "list_date": None if row["list_date"] in (None, "") else _day(row["list_date"])}
    lineage = _lineage(view)
    return {"namespace": namespace, "groups": dict(sorted(groups.items())), "scope_count": len(groups),
        "lineage": lineage, "version": digest(encoded({"namespace": namespace, "lineage": lineage})),
        "effective_at": None, "semantics": "source-classification-slice-not-historical-or-full-market"}


def qualified_ths_members(result: dict, *, registry: RelayRegistry, ts_code: str) -> dict:
    """As-returned rows, not certified current or historical constituents.

    THS documents in_date/out_date/weight as unavailable. Do not manufacture an
    effective date from the capture clock or imply an undated row held all period.
    BJ identifiers may be preserved here without enabling BJ price processing.
    """
    code = _index_code(ts_code)
    view = _view(result, registry, "ths_member", {"ts_code": code}, MEMBER_FIELDS)
    members = {}
    for row in _rows(view):
        member = row["con_code"]
        _require(row["ts_code"] == code and isinstance(member, str)
                 and bool(re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", member))
                 and member not in members, "GROUP_MEMBER_IDENTITY")
        members[member] = _name(row["con_name"])
    lineage = _lineage(view)
    return {"ts_code": code, "members": dict(sorted(members.items())), "row_count": len(members),
        "lineage": lineage, "version": lineage["view_sha256"], "effective_at": None,
        "semantics": "as-returned-rows-not-dated-current-or-historical-membership"}


def _levels(result: dict, registry: RelayRegistry, code: str, start: str, end: str, allowed: set[str]):
    view = _view(result, registry, "ths_daily", {"ts_code": code, "start_date": start, "end_date": end}, LEVEL_FIELDS)
    return _daily(view, code, allowed), _lineage(view)


def read_ths_group_surface(*, catalog_result: dict, member_results: Mapping[str, dict],
                           index_results: Mapping[str, dict], calendar_result: dict,
                           registry: RelayRegistry, kind: str, exchange: str, start: str, end: str,
                           benchmark: dict | None = None, horizons: tuple[int, ...] = (5, 20, 60),
                           offset: int = 0, limit: int = 50, entity_id: str | None = None) -> dict:
    """Read index levels independently of member availability; keep all catalogue rows."""
    lo, hi = _range(start, end)
    catalog = qualified_ths_catalog(catalog_result, registry=registry, kind=kind)
    groups = catalog["groups"]
    _require(isinstance(exchange, str) and exchange in EXCHANGES, "EXCHANGE_UNSUPPORTED")
    for mapping in (member_results, index_results):
        _require(isinstance(mapping, Mapping) and set(mapping) <= set(groups), "UNDECLARED_GROUP")
    calendar_view = _view(calendar_result, registry, "trade_cal", {"exchange": exchange,
        "start_date": start, "end_date": end}, {"exchange", "cal_date", "is_open"})
    sessions = _calendar(calendar_view, exchange, lo, hi)
    _require(hi in sessions, "END_NOT_MARKET_SESSION")
    calendar_source = _lineage(calendar_view)
    allowed = set(sessions)
    levels, sources, snapshots, qualification = {}, {}, {}, {}
    for code, group in groups.items():
        key = group["entity_id"]
        state = {"index": {"status": "not-supplied"}, "members": {"status": "not-supplied"}}
        if code in index_results:
            try:
                levels[key], sources[key] = _levels(index_results[code], registry, code, start, end, allowed)
                state["index"] = {"status": "checked", "row_count": len(levels[key])}
            except MarketRelayError as exc:
                state["index"] = {"status": "rejected", "reason_code": str(exc)}
        if code in member_results:
            try:
                snapshot = qualified_ths_members(member_results[code], registry=registry, ts_code=code)
                snapshots[key] = snapshot
                expected, actual = group["reported_member_count"], snapshot["row_count"]
                state["members"] = {"status": "checked", "row_count": actual,
                    "catalogue_count_comparison": "unknown" if expected is None else
                        "matches" if expected == actual else "differs-between-captures"}
            except MarketRelayError as exc:
                state["members"] = {"status": "rejected", "reason_code": str(exc)}
        qualification[key] = state
    baseline = None
    benchmark_state = {"status": "not-supplied"}
    if benchmark is not None:
        try:
            _require(isinstance(benchmark, dict) and set(benchmark) == {"ts_code", "result"}, "GROUP_BENCHMARK")
            code = _index_code(benchmark["ts_code"])
            series, lineage = _levels(benchmark["result"], registry, code, start, end, allowed)
            baseline = {"entity_id": "ths-a-benchmark:" + code, "series": series,
                "source_version": lineage["view_sha256"], "adjustment": "raw", "market": "THS-A", "currency": "XXX"}
            benchmark_state = {"status": "checked", "lineage": lineage}
        except MarketRelayError as exc:
            benchmark_state = {"status": "rejected", "reason_code": str(exc)}
    surface = read_market_surface({"kind": kind, "as_of": hi, "entities": levels,
        "declared_entities": [g["entity_id"] for g in groups.values()], "market_sessions": sessions,
        "calendar_version": calendar_source["view_sha256"], "adjustment": "raw",
        "source_version": digest(encoded(sources)), "universe_version": catalog["version"],
        "coverage": "partial-universe", "market": "THS-A", "currency": "XXX",
        "horizons": horizons, "benchmark": baseline}, offset=offset, limit=limit, entity_id=entity_id)
    by_id = {g["entity_id"]: g for g in groups.values()}
    for entry in surface["entries"]:
        key = entry["entity_id"]
        entry.update(classification=by_id[key], qualification=qualification[key],
                     index_lineage=sources.get(key), member_snapshot=snapshots.get(key))
    surface.update(catalogue={k: v for k, v in catalog.items() if k != "groups"},
        calendar_lineage=calendar_source, benchmark_qualification=benchmark_state,
        reported_membership_version=digest(encoded({"snapshots": snapshots,
            "qualification": {k: q["members"] for k, q in qualification.items()}})),
        qualification_counts={part: {status: sum(q[part]["status"] == status for q in qualification.values())
            for status in ("checked", "rejected", "not-supplied")} for part in ("index", "members")},
        index_unit="index-points-not-currency", qualification_limits=[
            "THS_A_I_OR_N_CLASSIFICATION_NOT_OTHER_TAXONOMIES_OR_FULL_MARKET",
            "CAPTURE_CLOCK_NOT_EFFECTIVE_MEMBERSHIP_DATE_OR_PIT_CERTIFICATION",
            "AS_RETURNED_MEMBERS_NOT_CERTIFIED_CURRENT_OR_HISTORICAL_CONSTITUENTS",
            "INDEX_LEVEL_RATIO_NOT_RECONSTRUCTED_MEMBER_PORTFOLIO_OR_TOTAL_RETURN",
            "EXPLICIT_CALENDAR_AND_END_NOT_LATEST_COMPLETED_SESSION_CERTIFICATION",
            "EXPLICIT_BENCHMARK_NOT_CERTIFIED_BROAD_MARKET_OR_ECONOMIC_COMPARABILITY",
            "SOURCE_SCOPED_READING_ID_NOT_CANONICAL_ENTITY_OR_ISSUER_ID",
            "VIEW_HASH_NOT_SOURCEREF_OR_PUBLISHER_AUTHENTICATION"])
    return surface


def read_three_market_surfaces(*, surfaces: Mapping[str, dict], registry: RelayRegistry,
                               offset: int = 0, limit: int = 50, entity_id: str | None = None) -> dict:
    """Compose existing stock and group readers, isolating even a failed shared group input.

    Overlap is limited to selected group pages, not an unbounded all-pairs matrix.
    Use the single-surface routes for ID selection. No new collection is performed.
    """
    _require(isinstance(registry, RelayRegistry), "REGISTRY_REQUIRED")
    _require(isinstance(surfaces, Mapping) and set(surfaces) <= {"sector", "concept", "stock"}, "SURFACES_INVALID")
    _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "PAGE_INVALID")
    _require(entity_id is None, "COMBINED_FILTER_USE_SINGLE_SURFACE")
    output = {}
    for kind in ("sector", "concept", "stock"):
        if kind not in surfaces:
            output[kind] = {"status": "not-supplied"}
            continue
        try:
            value = surfaces[kind]
            required = STOCK_REQUIRED if kind == "stock" else GROUP_REQUIRED
            optional = {"horizons"} if kind == "stock" else {"horizons", "benchmark"}
            _require(isinstance(value, dict) and required <= value.keys()
                     and value.keys() <= required | optional, "SURFACE_INPUT_INVALID")
            if kind == "stock":
                view = read_security_stock_surface(**value, registry=registry, offset=offset, limit=limit)
            else:
                view = read_ths_group_surface(**value, registry=registry, kind=kind, offset=offset, limit=limit)
            output[kind] = {"status": "read", "view": view}
        except (MarketInputError, RecordError) as exc:
            output[kind] = {"status": "rejected", "reason_code": str(exc)}
    maps, context = {}, {}
    for kind in ("sector", "concept"):
        if output[kind]["status"] != "read":
            maps[kind] = {}
            context[kind] = {"surface_status": output[kind]["status"], "unknown_page_memberships": None}
            continue
        view = output[kind]["view"]
        entries = view["entries"]
        maps[kind] = {e["entity_id"]: list(e["member_snapshot"]["members"])
                      for e in entries if e["member_snapshot"] is not None}
        context[kind] = {"surface_status": "read", "page": view["page"], "price_as_of": view["as_of"],
            "membership_version": view["reported_membership_version"],
            "unknown_page_memberships": len(entries) - len(maps[kind]), "scope_count": view["scope_count"]}
    overlap = {"status": "literal-page-intersection" if all(maps.values()) else "insufficient-reported-membership",
        "pairs": membership_overlap(maps["sector"], maps["concept"]), "context": context,
        "semantics": "selected-pages-as-returned-security-ids-not-same-time-membership-or-economic-exposure"}
    return {"schema": "relay-three-market-view-v1", "surfaces": output, "membership_overlap": overlap}
