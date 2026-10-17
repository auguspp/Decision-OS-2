"""A-share custody reading in the existing room, not a price or capture owner."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
import json
import re

from decision_kernel.application.market_custody import (
    READING_SCHEMA, _read_manifest, read_stored_market,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import Selection, SourceRef, encoded, require

MAX_MARKET_BYTES = 2 * 1024 * 1024


def read_market_reference(reference, registry, store, *, offset=0, limit=50,
                          entity_id=None, ashare_only=False, read_as_of=None):
    """Dispatch an exact source root to its original family verifier and reader.

    The generated source owner prefix selects a verifier; it is not proof of
    the source's content. Collection restoration still verifies the whole root,
    registry, plan and every retained query. The legacy reader stays unchanged.
    Only the trusted host may supply the reading clock, never source text.
    """
    ref = reference if isinstance(reference, SourceRef) else SourceRef.from_dict(reference)
    require(ref.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
            "PAGE_INVALID")
    if ref.manifest.path.startswith("source-views/market-collection-"):
        from decision_kernel.application.market_collection_reading import read_collection_version
        from decision_kernel.application.versioned_store import VersionedStore
        from decision_kernel.infrastructure.github_store import GitHubStore
        from decision_kernel.infrastructure.market_snapshot import GitMarketReader

        if isinstance(store.backend, GitHubStore):
            # Preserve the operation's single API budget and exact binding. The
            # source-only snapshot pins one native Git view for all day queries.
            store = VersionedStore(GitMarketReader(replace(store.backend.binding, write_enabled=False),
                                                   store.backend.api))
        clock = datetime.now(timezone.utc).isoformat() if read_as_of is None else read_as_of
        return read_collection_version(ref, registry, store, offset=offset, limit=limit,
                                       entity_id=entity_id, read_as_of=clock)
    if ashare_only:
        # Reject other market families before restoring any of their queries.
        _, root = _read_manifest(store, ref, READING_SCHEMA)
        require(isinstance(root.get("reading"), dict) and
                root["reading"].get("schema") == "relay-security-stock-reading-v1",
                "ROOM_MARKET_ASHARE_ROOT_REQUIRED")
    return read_stored_market(ref, registry, store, offset=offset, limit=limit,
                              entity_id=entity_id)


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


def open_market(store, value, *, registry=None, read_as_of=None):
    """Original root/query/page validation and arithmetic; no data rewritten.

    The caller supplies a trusted registry, as in the existing market CLI. The
    native host uses its checkout's registry. Source text cannot select a path.
    """
    plan = market_plan(value)
    ref = SourceRef.from_dict(plan["reference"])
    registry = registry if registry is not None else RelayRegistry(Path("docs/sources/relay-registry.json"))
    restored = read_market_reference(ref, registry, store, offset=plan["offset"],
        limit=plan["limit"], entity_id=plan["entity_id"], ashare_only=True, read_as_of=read_as_of)
    result = {"reference": deepcopy(plan["reference"]), "plan": plan,
              "open_request": {"operation": "read-source", "payload": {"reference": deepcopy(plan["reference"])}},
              "status": "readable", "next_plan": None}
    if len(encoded(restored)) > MAX_MARKET_BYTES:
        return {**result, "status": "not-included", "error": "ROOM_MARKET_DISPLAY_BOUND"}
    view = restored["view"]
    if view is not None:
        require(view.get("schema") in ("relay-security-stock-view-v1", "relay-ashare-collection-view-v1"),
                "ROOM_MARKET_VIEW_INVALID")
        require(view["raw"]["page"] == view["qfq"]["page"], "ROOM_MARKET_PAGE_MISMATCH")
        offset = view["raw"]["page"]["next_offset"]
        if offset is not None:
            result["next_plan"] = {**deepcopy(plan), "offset": offset}
    result["view"] = restored
    # Restoration and declared-horizon completeness are different dimensions.
    result["partial"] = restored["status"] != "restored" or view is None or any(
        view[k]["incomplete_count"] is None or view[k]["incomplete_count"] > 0
        and view[k].get("status") != "no-market-sessions" for k in ("raw", "qfq"))
    if view is not None and view.get("schema") == "relay-ashare-collection-view-v1":
        result["partial"] = result["partial"] or restored["serving"] != "current-complete" or (
            any(view[k].get("status") != "no-market-sessions" for k in ("raw", "qfq"))
            and view["freshness"]["status"] != "current-through-completed-day")
    return result
