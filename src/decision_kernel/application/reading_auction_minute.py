"""Exact minute roots in the normal Room, with whole-cohort qualification."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from decision_kernel.application.auction_minute_custody import read_minute_reading
from decision_kernel.application.market_reading_scope import operation_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError, Selection, SourceRef, encoded, require
from decision_kernel.observations.auction_minute import VIEW_SCHEMA
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.opening_auction import CODE

MAX_MINUTE_BYTES = 2 * 1024 * 1024


def minute_plan_input(value):
    require(isinstance(value, dict) and "reference" in value
            and set(value) <= {"reference", "offset", "limit", "entity_id"}, "ROOM_MINUTE_PLAN_INVALID")
    ref = SourceRef.from_dict(value["reference"])
    require(ref.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    offset, limit, entity = value.get("offset", 0), value.get("limit", 20), value.get("entity_id")
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50, "ROOM_MINUTE_PAGE_INVALID")
    require(entity is None or isinstance(entity, str) and CODE.fullmatch(entity), "ROOM_MINUTE_ENTITY_INVALID")
    return {"reference": json.loads(encoded(asdict(ref))), "offset": offset, "limit": limit, "entity_id": entity}


def open_auction_minute(store, value, *, registry=None):
    from decision_kernel.application.reading_room import _failure
    plan = minute_plan_input(value)
    registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
    result = {"schema": "reading-auction-minute-v1", "plan": plan, "reference": plan["reference"],
        "open_request": {"operation": "read-source", "payload": deepcopy(plan["reference"])},
        "source_requests": 0, "writes": 0, "next_plan": None}
    try:
        # The owned reader completes its final original scope check before this
        # consumer projects a page or releases any output.
        restored = read_minute_reading(plan["reference"], registry, store, offset=0, limit=100)
        view = restored["view"]
        result.update(view=restored, page=None, status="partial")
        if view is not None:
            require(view.get("schema") == VIEW_SCHEMA and view["cohort_denominator"] == len(view["cohort_rows"]),
                    "ROOM_MINUTE_FULL_SCOPE_REQUIRED")
            rows = [row for row in view["cohort_rows"] if plan["entity_id"] is None or row["entity_id"] == plan["entity_id"]]
            page = rows[plan["offset"]:plan["offset"] + plan["limit"]]
            next_offset = plan["offset"] + len(page) if plan["offset"] + len(page) < len(rows) else None
            result["page"] = {"entries": deepcopy(page), "matched_count": len(rows), "offset": plan["offset"],
                "limit": plan["limit"], "entity_id": plan["entity_id"], "returned": len(page), "next_offset": next_offset}
            if next_offset is not None:
                result["next_plan"] = {**deepcopy(plan), "offset": next_offset}
            result["status"] = "partial" if restored["status"] != "restored" or view["coverage_gaps"] else "readable"
    except (RecordError, MarketInputError, KeyError, TypeError) as error:
        if operation_failure(error):
            raise
        result.update(_failure(error))
    if len(encoded(result)) > MAX_MINUTE_BYTES:
        return {"schema": "reading-auction-minute-v1", "plan": plan, "reference": plan["reference"],
                "status": "not-included", "error": "ROOM_MINUTE_DISPLAY_BOUND"}
    return result
