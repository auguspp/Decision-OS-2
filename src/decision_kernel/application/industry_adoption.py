"""Pure joins between an original industry panel and an already reopened Brief.

Call only after the room's source/feedback export gates. No I/O, adoption writer,
new selection policy, economic inference or replacement of the reader's result.
"""
from copy import deepcopy

from decision_kernel.application import brief_industry as owner
from decision_kernel.domain.records import RecordError, digest, encoded, require

MAX_ADOPTION_BYTES = 64 * 1024


def _series_ids(panel):
    view, mode = panel["view"], panel["plan"]["view"]
    if mode == "industry":
        values = [s["series_id"] for s in view["series"]]
    elif mode == "vehicle-flows":
        values = [s["series_id"] for s in view["entries"]]
    else:
        require(mode == "operating-cycle", "ROOM_ADOPTION_VIEW")
        values = [m["series_id"] for e in view["entries"] for m in e["metrics"].values()]
    require(len(values) <= 32 and len(values) == len(set(values)), "ROOM_ADOPTION_SERIES_BOUND")
    return values


def _comparison(panel, item, saved):
    """Compare displayed series and roles only, not whole-packet authenticity.

    Reuse the Brief owner's projection. Root metadata here is deliberately not
    checked or borrowed from the saved Brief; only series/relations are compared.
    A company-filtered panel contains only that company's roles, not all roles.
    """
    if saved["status"] != "readable":
        return {"status": "unknown", "reason": "delivery-context-unavailable"}
    try:
        current = owner.project({"view": panel["view"], "stored_at": None,
                                 "root_limitations": [], "root_original_verified": False}, item)
        old = saved["context"]
        company = panel["plan"].get("company_id")
        roles = [r for r in old["relations"] if company is None or r["company"]["entity_id"] == company]
        def ordered(rows):
            return sorted(rows, key=lambda r: r["relation_id"])
        fields = {"series": (current["series"], old["series"]),
                  "displayed_roles": (ordered(current["relations"]), ordered(roles))}
        mismatches = [name for name, (actual, original) in fields.items() if encoded(actual) != encoded(original)]
        return {"status": "differs-from-delivery" if mismatches else "same-displayed-series-and-roles",
                "different_fields": mismatches,
                "scope": "displayed-series-and-filtered-roles-only-not-whole-packet-or-economic-quality",
                "saved_role_count": len(old["relations"]), "compared_role_count": len(roles)}
    except (RecordError, KeyError, TypeError, ValueError):
        return {"status": "unknown", "reason": "display-context-unavailable"}


def brief_adoption(panel, brief):
    """Relate exact returned series to one permission-cleared, saved Brief.

    Same reachable content commits share the existing owner's identity; names,
    another packet, view or series do not. Unknown is not a negative adoption.
    Original panel/view and Brief are never mutated. No source calls are made.
    """
    result = {"schema": "industry-brief-relation-v1", "status": "unknown", "rows": [],
              "brief_reference": deepcopy(brief.get("reference")),
              "scope": "returned-series-and-one-specified-saved-brief-only",
              "human_acceptance": "not-established", "research_assessment": "not-performed"}
    if panel.get("status") != "readable":
        return {**result, "reason": "industry-panel-unavailable"}
    state = brief.get("status")
    if state != "readable":
        return {**result, "status": "not-supplied" if state == "not-supplied" else "unknown",
                "reason": "brief-not-supplied" if state == "not-supplied" else "brief-unavailable-or-not-exportable"}
    if brief.get("reference") is None:
        return {**result, "reason": "saved-brief-reference-required"}
    try:
        doc = brief["view"]
        section = doc.get("industry")
        result.update(brief_body_sha256=digest(doc["brief_markdown"].encode("utf-8")),
                      edition_sha256=doc["edition_sha256"])
        if section is not None:
            require(owner.derive(section["rows"], doc["plan"]["industry"], section["baseline"]) == section,
                    "ROOM_ADOPTION_SECTION_INVALID")
        by_id = {r["version_id"]: r for r in section["rows"]} if section else {}
        included = {r["version_id"]: r for r in section["included"]} if section else {}
        for series_id in _series_ids(panel):
            item = owner.item_input({"reference": panel["reference"], "view": panel["plan"]["view"],
                                     "series_id": series_id})
            key = owner.version_id(item)
            row = {"series_id": series_id, "version_id": key, "item": item}
            if section is None:
                row["status"] = "industry-not-tracked-in-this-brief"
            elif key not in by_id:
                row["status"] = "not-considered-in-this-brief"
            else:
                saved = by_id[key]
                row.update(status="included" if key in included else "considered-not-included",
                           reading_status_at_delivery=saved["status"],
                           displayed_content=_comparison(panel, item, saved))
                if key in included:
                    row.update(classification=included[key]["classification"], omission_reason=None)
                else:
                    # These are the original, validated policy's precedence rules,
                    # not a fresh selection, recommendation or human rejection.
                    reason = ("unreadable-or-outside-cutoff" if saved["status"] != "readable" else
                              "previously-included-same-selection" if key in section["baseline"]["included"] else
                              "brief-capacity")
                    row.update(classification=None, omission_reason=reason)
            result["rows"].append(row)
        result["status"] = "related"
        if len(encoded(result)) > MAX_ADOPTION_BYTES:
            return {"schema": result["schema"], "status": "not-included", "rows": [],
                    "brief_reference": result["brief_reference"], "reason": "ROOM_ADOPTION_DISPLAY_BOUND",
                    "scope": result["scope"], "human_acceptance": "not-established"}
        return result
    except (RecordError, KeyError, TypeError, ValueError):
        return {"schema": result["schema"], "status": "unknown", "rows": [],
                "brief_reference": result["brief_reference"], "reason": "ROOM_ADOPTION_CONTEXT_INVALID",
                "scope": result["scope"], "human_acceptance": "not-established"}
