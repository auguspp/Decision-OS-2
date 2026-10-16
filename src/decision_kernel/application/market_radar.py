"""Three explicitly independent consumer entrypoints over qualified daily inputs.

This module deliberately never assumes an index list = a concept/sector membership
or a fixed watchlist = an independently discovered full stock universe.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decision_kernel.observations.market_daily import MarketInputError, observe_daily

KINDS = ("sector", "concept", "stock")


def observe_market_surface(
    *,
    kind: str,
    as_of: str,
    entities: Mapping[str, Sequence[Mapping[str, object]]],
    adjustment: str,
    source_version: str,
    universe_version: str,
    coverage: str,
    benchmark: Sequence[Mapping[str, object]] | None = None,
    membership: Mapping[str, Sequence[str]] | None = None,
    horizons: tuple[int, ...] = (1, 5, 20),
) -> dict:
    """Return a scoped browseable surface, not a ranked investment candidate list.

    The stock path has no dependency on sector/concept hits. The caller must
    declare whether input is a full universe or merely a watchlist.
    """
    if kind not in KINDS:
        raise MarketInputError("KIND_INVALID")
    if not isinstance(entities, Mapping) or not all(isinstance(k, str) and k.strip() for k in entities):
        raise MarketInputError("ENTITIES_INVALID")
    if not isinstance(universe_version, str) or not universe_version.strip():
        raise MarketInputError("UNIVERSE_VERSION_REQUIRED")
    if coverage not in ("full-declared-universe", "partial-universe", "watchlist"):
        raise MarketInputError("COVERAGE_INVALID")
    if kind != "stock" and coverage == "watchlist":
        raise MarketInputError("CLASSIFICATION_COVERAGE_INVALID")
    if membership is not None and (not isinstance(membership, Mapping) or
        any(k not in entities or isinstance(v, (str, bytes)) or not isinstance(v, Sequence)
            or any(not isinstance(x, str) or not x.strip() for x in v) or len(set(v)) != len(v)
            for k, v in membership.items())):
        raise MarketInputError("MEMBERSHIP_INVALID")
    entries = [
        observe_daily(entity_id=entity, series=entities[entity], as_of=as_of,
                      adjustment=adjustment, source_version=source_version,
                      benchmark=benchmark, horizons=horizons)
        for entity in sorted(entities)
    ]
    return {
        "kind": kind, "as_of": as_of, "universe_version": universe_version,
        "source_version": source_version, "coverage": coverage,
        "scope_count": len(entities),
        "observed_count": sum(x["status"] == "observed" for x in entries),
        "missing_count": sum(x["status"] != "observed" for x in entries),
        "membership": {k: sorted(v) for k, v in sorted((membership or {}).items())},
        "entries": entries,
        "discovery_claim": "not-established-by-supplied-universe",
    }


def membership_overlap(left: Mapping[str, Sequence[str]], right: Mapping[str, Sequence[str]]) -> list[dict]:
    """Exact symbol overlap only; no company-identity equivalence inferred."""
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        raise MarketInputError("MEMBERSHIP_INVALID")
    output = []
    for name_a, a in sorted(left.items()):
        for name_b, b in sorted(right.items()):
            if not isinstance(a, Sequence) or isinstance(a, (str, bytes)) or not isinstance(b, Sequence) or isinstance(b, (str, bytes)):
                raise MarketInputError("MEMBERSHIP_INVALID")
            sa, sb = set(a), set(b)
            if any(not isinstance(x, str) or not x.strip() for x in sa | sb):
                raise MarketInputError("MEMBERSHIP_INVALID")
            overlap = sorted(sa & sb)
            if overlap:
                output.append({"left": name_a, "right": name_b, "members": overlap,
                               "overlap_count": len(overlap)})
    return output
