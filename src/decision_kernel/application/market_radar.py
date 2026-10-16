"""Three independent market surfaces and a bounded, read-only consumer.

The declared universe is a denominator, not proof of full-market discovery.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decision_kernel.observations.market_daily import (
    MarketInputError, benchmark_context, daily_context, observe_daily, require_text, trade_day,
)

KINDS = ("sector", "concept", "stock")


def _identities(values: object, code: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise MarketInputError(code)
    output = [require_text(value, code) for value in values]
    if len(set(output)) != len(output):
        raise MarketInputError(code)
    return output


def _members(value: object) -> dict[str, list[str]]:
    if not isinstance(value, Mapping):
        raise MarketInputError("MEMBERSHIP_INVALID")
    checked = {require_text(key, "MEMBERSHIP_INVALID"): sorted(_identities(members, "MEMBERSHIP_INVALID"))
               for key, members in value.items()}
    return dict(sorted(checked.items()))


def observe_market_surface(*, kind: str, as_of: str,
                           entities: Mapping[str, Sequence[Mapping[str, object]]],
                           declared_entities: Sequence[str], market_sessions: Sequence[str],
                           calendar_version: str, adjustment: str, source_version: str,
                           universe_version: str, coverage: str, market: str, currency: str,
                           benchmark: Mapping[str, object] | None = None,
                           membership: Mapping[str, Sequence[str]] | None = None,
                           membership_version: str | None = None, membership_as_of: str | None = None,
                           horizons: tuple[int, ...] = (5, 20, 60)) -> dict:
    """Keep every declared entity, including missing or malformed price inputs.

    Shared-context errors fail the call. Entity-data errors are isolated with
    explicit reasons. Neither sector nor concept outcomes gate the stock path.
    """
    if kind not in KINDS:
        raise MarketInputError("KIND_INVALID")
    daily_context(as_of=as_of, market_sessions=market_sessions, calendar_version=calendar_version,
                  adjustment=adjustment, source_version=source_version, market=market,
                  currency=currency, horizons=horizons)
    benchmark_context(benchmark)
    require_text(universe_version, "UNIVERSE_VERSION_REQUIRED")
    declared = sorted(_identities(declared_entities, "DECLARED_UNIVERSE_INVALID"))
    declared_set = set(declared)
    if not isinstance(entities, Mapping):
        raise MarketInputError("ENTITIES_INVALID")
    if any(require_text(k, "ENTITIES_INVALID") not in declared_set for k in entities):
        raise MarketInputError("UNDECLARED_ENTITY")
    if coverage not in ("full-declared-universe", "partial-universe", "watchlist"):
        raise MarketInputError("COVERAGE_INVALID")
    if kind != "stock" and coverage == "watchlist":
        raise MarketInputError("CLASSIFICATION_COVERAGE_INVALID")
    members = None
    if membership is None:
        if membership_version is not None or membership_as_of is not None:
            raise MarketInputError("MEMBERSHIP_CONTEXT_WITHOUT_MEMBERS")
    else:
        if kind == "stock":
            raise MarketInputError("STOCK_MEMBERSHIP_NOT_APPLICABLE")
        require_text(membership_version, "MEMBERSHIP_VERSION_REQUIRED")
        if trade_day(membership_as_of) > trade_day(as_of):
            raise MarketInputError("FUTURE_MEMBERSHIP")
        members = _members(membership)
        if any(k not in declared_set for k in members):
            raise MarketInputError("UNDECLARED_MEMBERSHIP_GROUP")
    entries = []
    for entity in declared:
        try:
            entry = observe_daily(entity_id=entity, series=entities.get(entity, []), as_of=as_of,
                                  market_sessions=market_sessions, calendar_version=calendar_version,
                                  adjustment=adjustment, source_version=source_version, market=market,
                                  currency=currency, benchmark=benchmark, horizons=horizons)
        except MarketInputError as exc:
            entry = {"entity_id": entity, "as_of": as_of, "status": "invalid-data",
                     "reason_code": str(exc),
                     "horizons": [{"sessions": h, "status": "invalid-data"} for h in horizons]}
        entry["input_status"] = "supplied" if entity in entities else "not-supplied"
        entries.append(entry)
    observed = sum(e["status"] in ("observed", "partial-history") for e in entries)
    incomplete = sum(e["status"] != "observed" for e in entries)
    counts = []
    for index, horizon in enumerate(horizons):
        windows = [e["horizons"][index] for e in entries]
        absolute = sum(w["status"] == "observed" for w in windows)
        relative = sum(w.get("relative_status") == "observed" for w in windows)
        counts.append({"sessions": horizon, "scope_count": len(declared),
                       "observed_count": absolute, "missing_count": len(declared) - absolute,
                       "relative_observed_count": relative, "relative_missing_count": len(declared) - relative})
    return {"kind": kind, "as_of": as_of, "market": market, "currency": currency,
            "adjustment": adjustment, "calendar_version": calendar_version,
            "source_version": source_version, "universe_version": universe_version, "coverage": coverage,
            "scope_count": len(declared), "received_count": len(entities),
            "observed_count": observed, "missing_count": len(declared) - observed,
            "incomplete_count": incomplete, "horizon_counts": counts,
            "status": "empty-declared-universe" if not declared else "data-gaps" if incomplete else "observed",
            "membership": members, "membership_version": membership_version, "membership_as_of": membership_as_of,
            "membership_semantics": "declared-snapshot-not-historical-exposure",
            "entries": entries, "discovery_claim": "not-established-by-supplied-universe"}


def membership_overlap(left: Mapping[str, Sequence[str]], right: Mapping[str, Sequence[str]]) -> list[dict]:
    """Literal security-ID intersection only, never inferred economic exposure.

    Validate both sides first, even when the other side is empty. Snapshots and
    their dates remain the caller's responsibility; this function has no PIT claim.
    """
    checked_left, checked_right = _members(left), _members(right)
    output = []
    for name_a, a in checked_left.items():
        for name_b, b in checked_right.items():
            overlap = sorted(set(a) & set(b))
            if overlap:
                output.append({"left": name_a, "right": name_b, "members": overlap,
                               "overlap_count": len(overlap)})
    return output


def read_market_surface(payload: Mapping[str, object], *, offset: int = 0, limit: int = 50,
                        entity_id: str | None = None) -> dict:
    """Compute and page one explicit surface; preserve unpaged denominators."""
    required = {"kind", "as_of", "entities", "declared_entities", "market_sessions", "calendar_version",
                "adjustment", "source_version", "universe_version", "coverage", "market", "currency"}
    optional = {"benchmark", "membership", "membership_version", "membership_as_of", "horizons"}
    if not isinstance(payload, Mapping) or not required <= set(payload) or set(payload) - required - optional:
        raise MarketInputError("SURFACE_PAYLOAD_INVALID")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise MarketInputError("PAGE_INVALID")
    if entity_id is not None:
        require_text(entity_id, "ENTITY_REQUIRED")
    params = dict(payload)
    if "horizons" in params:
        if not isinstance(params["horizons"], (list, tuple)):
            raise MarketInputError("HORIZON_INVALID")
        params["horizons"] = tuple(params["horizons"])
    surface = observe_market_surface(**params)
    selected = [e for e in surface["entries"] if entity_id is None or e["entity_id"] == entity_id]
    surface["entries"] = selected[offset:offset + limit]
    surface["page"] = {"offset": offset, "limit": limit, "selected_count": len(selected),
                       "next_offset": offset + limit if offset + limit < len(selected) else None,
                       "entity_id": entity_id}
    return surface
