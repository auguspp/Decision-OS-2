"""A-share custody reading in the existing room, not a price or capture owner."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json
import re

from decision_kernel.application.market_custody import (
    READING_SCHEMA, _read_manifest, read_stored_market,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import Selection, SourceRef, encoded, require

MAX_MARKET_BYTES = 2 * 1024 * 1024


def market_plan(value):
    require(isinstance(value, dict) and "reference" in value and set(value) <= {
        "reference", "offset", "limit", "entity_id"}, "ROOM_MARKET_PLAN_INVALID")
    reference = SourceRef.from_dict(value["reference"])
    require(reference.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    offset, limit, entity = value.get("offset", 0), value.get("limit", 20), value.get("entity_id")
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
            "ROOM_MARKET_PAGE_INVALID")
    require(entity is None or isinstance(entity, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", entity),
            "ROOM_MARKET_SECURITY_INVALID")
    return {"reference": json.loads(encoded(asdict(reference))), "offset": offset,
            "limit": limit, "entity_id": entity}


def open_market(store, value, *, registry=None):
    """Original root/query/page validation and arithmetic; no data rewritten.

    The caller supplies a trusted registry, as in the existing market CLI. The
    native host uses its checkout's registry. Source text cannot select a path.
    """
    plan = market_plan(value)
    ref = SourceRef.from_dict(plan["reference"])
    # Reuse the original manifest verifier, and reject other families BEFORE
    # restoring their queries. Counts-only receipts cannot become market roots.
    _, root = _read_manifest(store, ref, READING_SCHEMA)
    require(isinstance(root.get("reading"), dict) and
            root["reading"].get("schema") == "relay-security-stock-reading-v1",
            "ROOM_MARKET_ASHARE_ROOT_REQUIRED")
    registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
    restored = read_stored_market(ref, registry, store, offset=plan["offset"],
                                 limit=plan["limit"], entity_id=plan["entity_id"])
    result = {"reference": deepcopy(plan["reference"]), "plan": plan,
              "open_request": {"operation": "read-source", "payload": {"reference": deepcopy(plan["reference"])}},
              "status": "readable", "next_plan": None}
    if len(encoded(restored)) > MAX_MARKET_BYTES:
        return {**result, "status": "not-included", "error": "ROOM_MARKET_DISPLAY_BOUND"}
    view = restored["view"]
    if view is not None:
        require(view.get("schema") == "relay-security-stock-view-v1", "ROOM_MARKET_VIEW_INVALID")
        require(view["raw"]["page"] == view["qfq"]["page"], "ROOM_MARKET_PAGE_MISMATCH")
        offset = view["raw"]["page"]["next_offset"]
        if offset is not None:
            result["next_plan"] = {**deepcopy(plan), "offset": offset}
    result["view"] = restored
    # Restoration and declared-horizon completeness are different dimensions.
    result["partial"] = restored["status"] != "restored" or view is None or any(
        view[k]["incomplete_count"] > 0 for k in ("raw", "qfq"))
    return result
