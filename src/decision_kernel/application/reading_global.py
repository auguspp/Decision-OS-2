"""Two retained global-context families in the existing read-only room.

The original custody and family readers own all qualification and arithmetic.
This consumer restores their complete bounded models once, then projects a
separate navigation page. It has no collector, clock substitution or writer.
"""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from decision_kernel.application.global_index_inputs import US_INDEX_CODES, US_INDEX_READING_SCHEMA
from decision_kernel.application.market_custody import (
    READING_SCHEMA, _read_manifest, market_read_scope, read_stored_market,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.shibor_inputs import SHIBOR_READING_SCHEMA, SHIBOR_TENORS
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, Selection, SourceRef, encoded, require
from decision_kernel.observations.market_daily import MarketInputError

MAX_GLOBAL_BYTES = 2 * 1024 * 1024
FAMILIES = {
    "us_indices": (US_INDEX_READING_SCHEMA, "relay-us-index-context-v1", US_INDEX_CODES),
    "shibor": (SHIBOR_READING_SCHEMA, "relay-shibor-context-v1", SHIBOR_TENORS),
}


def global_plan(value):
    require(isinstance(value, dict) and bool(value) and set(value) <= FAMILIES.keys(),
            "ROOM_GLOBAL_PLAN_INVALID")
    result = {}
    for name in FAMILIES:
        if name not in value:
            continue
        item = value[name]
        require(isinstance(item, dict) and "reference" in item and set(item) <= {
            "reference", "offset", "limit", "entity_id"}, "ROOM_GLOBAL_FAMILY_PLAN_INVALID")
        ref = SourceRef.from_dict(item["reference"])
        require(ref.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
        offset, limit, entity = item.get("offset", 0), item.get("limit", 20), item.get("entity_id")
        require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
                "ROOM_GLOBAL_PAGE_INVALID")
        require(entity is None or isinstance(entity, str) and entity in FAMILIES[name][2],
                "ROOM_GLOBAL_ENTITY_INVALID")
        result[name] = {"reference": json.loads(encoded(asdict(ref))),
                        "offset": offset, "limit": limit, "entity_id": entity}
    return result


from decision_kernel.application.market_reading_scope import SourceReads as _SourceReads, operation_failure


def _partial(name, restored):
    view = restored["view"]
    if restored["status"] != "restored" or view is None:
        return True
    if name == "us_indices":
        return any(entry["source_qualification"]["status"] != "checked"
                   or entry["observation"] is None or entry["observation"]["status"] != "observed"
                   for entry in view["entries"])
    return (any(entry["end_quote"]["status"] != "observed"
                or any(item["status"] != "observed" for item in entry["comparisons"])
                for entry in view["entries"])
            or any(item["status"] != "observed" for item in view["curve_spreads"])
            or any(counts["missing-value"] or counts["invalid-value"]
                   for counts in view["received_cell_counts"].values()))


def _open_family(reader, name, plan, registry):
    ref = SourceRef.from_dict(plan["reference"])
    _, root = _read_manifest(reader, ref, READING_SCHEMA)
    require(isinstance(root.get("reading"), dict)
            and root["reading"].get("schema") == FAMILIES[name][0], "ROOM_GLOBAL_FAMILY_ROOT_REQUIRED")
    # At most four supported indices or eight tenors: this original full result
    # is deliberately independent of the room's separate navigation projection.
    restored = read_stored_market(ref, registry, reader, offset=0, limit=100)
    result = {"reference": deepcopy(plan["reference"]), "plan": deepcopy(plan),
              "open_request": {"operation": "read-source", "payload": deepcopy(plan["reference"])},
              "status": "readable", "view": restored, "page": None, "next_plan": None,
              "partial": _partial(name, restored)}
    view = restored["view"]
    if view is None:
        return result
    require(view.get("schema") == FAMILIES[name][1], "ROOM_GLOBAL_VIEW_INVALID")
    declared = view["declared_indices" if name == "us_indices" else "declared_tenors"]
    require(view["scope_count"] == len(view["entries"]) == len(declared)
            and view["pagination"]["next_offset"] is None, "ROOM_GLOBAL_FULL_SCOPE_REQUIRED")
    require(plan["entity_id"] is None or plan["entity_id"] in declared, "ROOM_GLOBAL_ENTITY_NOT_DECLARED")
    selected = [e for e in view["entries"] if plan["entity_id"] is None or e["entity_id"] == plan["entity_id"]]
    entries = selected[plan["offset"]:plan["offset"] + plan["limit"]]
    next_offset = plan["offset"] + len(entries) if plan["offset"] + len(entries) < len(selected) else None
    result["page"] = {"entries": deepcopy(entries), "entity_id": plan["entity_id"],
                      "matched_count": len(selected), "offset": plan["offset"], "limit": plan["limit"],
                      "returned": len(entries), "next_offset": next_offset}
    if next_offset is not None:
        result["next_plan"] = {**deepcopy(plan), "offset": next_offset}
    return result


def open_global(store, value, *, registry=None):
    """Restore only supplied exact roots; local gaps never authorize fallback."""
    from decision_kernel.application.reading_room import _failure

    plan = global_plan(value)
    registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
    with market_read_scope(store) as scoped:
        backend = _SourceReads(scoped.backend)
        try:
            reader, families = VersionedStore(backend), {}
            for name, item in plan.items():
                try:
                    family = _open_family(reader, name, item, registry)
                except (RecordError, MarketInputError, KeyError, TypeError) as error:
                    if operation_failure(error):
                        raise
                    family = {"reference": deepcopy(item["reference"]), "plan": deepcopy(item),
                              "open_request": {"operation": "read-source", "payload": deepcopy(item["reference"])},
                              "next_plan": None, **_failure(error)}
                backend.check()
                families[name] = family
            result = {"schema": "reading-global-v1", "plan": plan, "families": families,
                      "status": "partial" if any(f["status"] != "readable" or f.get("partial", False)
                                                   for f in families.values()) else "readable",
                      "coverage": "specified-retained-US-indices-and-Shibor-not-six-family-global-coverage",
                      "source_requests": 0, "writes": 0,
                      "freshness": "original-explicit-source-dates-not-latest-update-certification",
                      "research_assessment": "not-performed", "human_acceptance": "not-established"}
            if len(encoded(result)) > MAX_GLOBAL_BYTES:
                result = {"schema": "reading-global-v1", "plan": plan, "status": "not-included",
                          "error": "ROOM_GLOBAL_DISPLAY_BOUND"}
        finally:
            backend.active = False
    return result
