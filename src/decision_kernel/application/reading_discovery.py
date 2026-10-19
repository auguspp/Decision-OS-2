"""Bounded state discovery projection in the existing exact-reference room."""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.domain.records import encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

KINDS = ("sector", "concept", "stock")
MAX_DISCOVERY_PANEL_BYTES = 768 * 1024


def discovery_plan(value):
    from decision_kernel.application.market_discovery import plan_input
    require(isinstance(value, dict) and set(value) <= {
        "reference", "build", "kind", "offset", "limit", "entity_id"}
        and ("reference" in value) != ("build" in value), "ROOM_DISCOVERY_PLAN")
    result = ({"reference": asdict(artifact_reference(value["reference"]))}
              if "reference" in value else {"build": plan_input(value["build"])})
    kind, offset, limit = value.get("kind", "stock"), value.get("offset", 0), value.get("limit", 20)
    entity = value.get("entity_id")
    require(isinstance(kind, str) and kind in KINDS, "ROOM_DISCOVERY_KIND")
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
            "ROOM_DISCOVERY_PAGE")
    require(entity is None or isinstance(entity, str) and 0 < len(entity) <= 128
            and entity.isprintable() and entity == entity.strip(), "ROOM_DISCOVERY_ENTITY")
    return {**result, "kind": kind, "offset": offset, "limit": limit, "entity_id": entity}


def project_discovery(snapshot, plan):
    """Filter only presentation; all three source scopes and unknown counts survive."""
    summaries = {}
    for kind in KINDS:
        surface = snapshot["surfaces"][kind]
        summaries[kind] = {key: deepcopy(value) for key, value in surface.items()
                           if key not in ("states", "gaps")}
        counts = {}
        for state in surface.get("states", []):
            name = state["state"]
            counts[name] = counts.get(name, 0) + 1
        summaries[kind]["state_counts"] = counts
        summaries[kind]["gap_count"] = len(surface.get("gaps", []))
    surface = snapshot["surfaces"][plan["kind"]]
    states = surface.get("states", [])
    selected = states if plan["entity_id"] is None else [
        row for row in states if row["entity_id"] == plan["entity_id"]]
    rows = selected[plan["offset"]:plan["offset"] + plan["limit"]]
    gaps = surface.get("gaps", [])
    if plan["entity_id"] is not None:
        gaps = [row for row in gaps if row["entity_id"] == plan["entity_id"]]
    event_ids = {row[key] for row in rows for key in ("origin_event", "last_event") if row.get(key)}
    events = {key: deepcopy(snapshot["events"][key]) for key in sorted(event_ids)}
    proof_ids = {event[key] for event in events.values() for key in ("current_proof", "previous_proof")
                 if event.get(key)}
    proofs = {key: deepcopy(snapshot["proofs"][key]) for key in sorted(proof_ids)}
    return {"schema": "market-discovery-reading-v1", "as_of": snapshot["as_of"],
            "policy": snapshot["policy"], "view_sha256": snapshot["view_sha256"],
            "verification": snapshot.get("verification", "computed-from-retained-sources"),
            "surfaces": summaries, "kind": plan["kind"], "states": deepcopy(rows),
            "events": events, "proofs": proofs, "gaps": deepcopy(gaps[:50]),
            "gap_display_count": min(len(gaps), 50), "gap_query_count": len(gaps),
            "page": {"offset": plan["offset"], "limit": plan["limit"],
                     "selected_count": len(selected), "returned": len(rows),
                     "next_offset": plan["offset"] + plan["limit"]
                     if plan["offset"] + plan["limit"] < len(selected) else None},
            "persistence": "saved-exact-version" if "reference" in plan else "not-written",
            "source_requests": 0, "research_executed": False, "investment_authority": "none"}


def open_discovery(store, value, *, registry=None):
    from decision_kernel.application.market_discovery import MarketDiscovery
    plan = discovery_plan(value)
    reader = MarketDiscovery(store, registry=registry)
    snapshot = reader.read(plan["reference"]) if "reference" in plan else reader.build(plan["build"])
    view = project_discovery(snapshot, plan)
    require(len(encoded(view)) <= MAX_DISCOVERY_PANEL_BYTES, "ROOM_DISCOVERY_DISPLAY_BOUND")
    next_offset = view["page"]["next_offset"]
    return {"reference": deepcopy(plan.get("reference")), "plan": plan, "status": "readable",
            "view": view, "next_plan": {**deepcopy(plan), "offset": next_offset}
            if next_offset is not None else None,
            "partial": any(s.get("status") != "readable" or s.get("unknown_count", 0) > 0
                           for s in view["surfaces"].values())}
