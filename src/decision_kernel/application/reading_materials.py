"""Exact material navigation over the original Delivery, Reader and Artifact owners.

A retained file is not a source original certification. Predecessor navigation is
not correction acceptance. This composition never executes files or writes data.
"""
from __future__ import annotations

from copy import deepcopy

from decision_kernel.application.research_agenda import research_reference
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery
from decision_kernel.application.research_reader import CURSOR_SCHEMA, ResearchReader
from decision_kernel.domain.records import BlobRef, RecordError, digest, encoded, identifier, require

MAX_SOURCE_DISPLAY_BYTES = 128 * 1024
MAX_MATERIAL_BYTES = 2 * 1024 * 1024


def material_plan(value):
    require(isinstance(value, dict) and "reference" in value and set(value) <= {
        "reference", "attachment_id", "source_cursor", "source_limit", "history_limit"},
        "ROOM_MATERIAL_PLAN_INVALID")
    ref = research_reference(value["reference"])
    attachment = value.get("attachment_id")
    if attachment is not None:
        identifier(attachment)
    cursor = deepcopy(value.get("source_cursor"))
    if cursor is not None:
        require(isinstance(cursor, dict) and set(cursor) == {"schema", "reference", "source_offset"}
                and cursor["schema"] == CURSOR_SCHEMA and cursor["reference"] == ref
                and type(cursor["source_offset"]) is int and cursor["source_offset"] >= 0,
                "ROOM_MATERIAL_CURSOR_MISMATCH")
    result = {"reference": ref, "attachment_id": attachment, "source_cursor": cursor}
    for field, default in (("source_limit", 4), ("history_limit", 2)):
        limit = value.get(field, default)
        require(type(limit) is int and 1 <= limit <= 4, "ROOM_MATERIAL_PAGE_LIMIT")
        result[field] = limit
    return result


def _revision(research):
    payload = research["record"]["input"]
    body = research["body"].encode("utf-8")
    return {"reference": deepcopy(research["reference"]), "case_id": payload["case_id"],
            "revision_id": payload["revision_id"], "kind": payload["kind"],
            "stored_at": research["record"]["stored_at"], "body_sha256": digest(body),
            "body_size": len(body), "predecessor": deepcopy(payload["predecessor"]),
            "open_request": {"operation": "open-delivery", "payload": {
                "reference": deepcopy(research["reference"]), "history_limit": 2}}}


def _sources(context):
    page = deepcopy(context["source_page"])
    for row in page["items"]:
        row["open_request"] = {"operation": "read-source", "payload": deepcopy(row["reference"])}
        row["display_status"] = "included"
        if row["view"] is not None and len(encoded(row["view"])) > MAX_SOURCE_DISPLAY_BYTES:
            # The owner read this exact representation; omit the whole display,
            # retaining original status/selection and recovery. Never truncate data.
            row.update(view=None, display_status="not-included", display_error="ROOM_SOURCE_DISPLAY_BOUND")
    return page


def _attachment(store, inventory, selected):
    if selected is None:
        return {"status": "not-requested"}
    item = next((i for i in inventory.get("items", []) if i["id"] == selected), None)
    if item is None or item.get("kind") != "artifact" or item["status"] != "retained":
        return {"status": "unavailable", "item_id": selected, "error": "ROOM_ATTACHMENT_NOT_RETAINED"}
    if item["readback"] != "verified":
        return {"status": "unavailable", "item_id": selected, "error": "ROOM_ATTACHMENT_NOT_VERIFIED"}
    try:
        view = ResearchArtifacts(store).read(item["reference"])
        require(view["bytes_verified"] is True and view["reference"] == item["reference"]
                and encoded(view["metadata"]) == encoded(item["metadata"]), "ROOM_ATTACHMENT_MISMATCH")
        return {"status": "readable", "item_id": selected, **view}
    except (RecordError, KeyError, TypeError):
        return {"status": "unavailable", "item_id": selected, "error": "ROOM_ATTACHMENT_READ_FAILED"}


def open_materials(store, value):
    """One exact inventory, one source page and a bounded predecessor navigation.

    The public caller must separately confirm material export before this call.
    Artifact selection is an inventory item ID, never an arbitrary blob or path.
    """
    plan = material_plan(value)
    ref = BlobRef(**plan["reference"])
    opened = ResearchDelivery(store).open(ref, history_limit=plan["history_limit"])
    context = opened["context"]
    require(context["research"]["reference"] == plan["reference"], "ROOM_MATERIAL_REFERENCE_MISMATCH")
    if plan["source_cursor"] is not None:
        context = ResearchReader(store).resume(cursor=plan["source_cursor"], source_limit=plan["source_limit"])
    elif plan["source_limit"] != 4:
        context = ResearchReader(store).resume(reference=ref, source_limit=plan["source_limit"])
    inventory = deepcopy(opened["delivery"])
    # Do not expose backend exception prose in an exported page.
    for item in inventory.get("items", []):
        if "error" in item:
            item["error"] = "ROOM_INVENTORY_ITEM_NOT_VERIFIED"
        if item.get("kind") == "artifact" and item.get("readback") == "verified":
            item["open_request"] = {"operation": "read-artifact", "payload": deepcopy(item["reference"])}
    history = []
    for row in opened["history"]:
        if row["status"] == "read":
            history.append({"status": "read", **_revision(row["research"])})
        else:
            history.append({"status": "unavailable", "reference": deepcopy(row["reference"]),
                            "error": "ROOM_PREDECESSOR_UNAVAILABLE"})
    download = _attachment(store, inventory, plan["attachment_id"])
    source_page = _sources(context)
    partial = inventory["status"] in ("partial", "failed") or any(
        r["status"] != "read" for r in history) or any(
        r["status"] not in ("stored-representation", "external-only") or r["display_status"] != "included"
        for r in source_page["items"]) or download["status"] not in ("readable", "not-requested")
    result = {"schema": "reading-materials-v1", "plan": plan,
              "status": "partial" if partial else "readable", "research": _revision(context["research"]),
              "inventory": inventory, "source_page": source_page,
              "next_source_cursor": deepcopy(context["next_cursor"]),
              "history": history, "next_history_reference": deepcopy(opened["next_history_reference"]),
              "history_semantics": opened["history_semantics"], "history_coverage": "this-page-only",
              "attachment": download, "correction_acceptance": "not-established-by-version-navigation",
              "source_authenticity": "not-certified", "persistence": "not-written", "executed": False}
    require(len(encoded(result)) <= MAX_MATERIAL_BYTES, "ROOM_MATERIAL_DISPLAY_BOUND")
    store.backend.head()  # Current access, not a silently updated selected revision.
    return result
