"""A read-only composition of Catalogue, Agenda and exact saved Brief readers.

No new index, artifact writer, current pointer or research runner. The review
clock is not a historical research cutoff; each catalogue page pins its own
records snapshot. Optional components fail locally, never as an empty success.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, StoreUnavailable,
    digest, encoded, require,
)

MAX_BODY_BYTES = 1024 * 1024
MAX_BODIES_BYTES = 2 * 1024 * 1024
MAX_PANEL_BYTES = 768 * 1024
MAX_VIEW_BYTES = 6 * 1024 * 1024


def plan_input(value):
    from decision_kernel.application.research_agenda import clock, research_reference
    from decision_kernel.domain.research_artifacts import artifact_reference

    require(isinstance(value, dict) and "as_of" in value and set(value) <= {
        "as_of", "catalogue", "reference", "agenda", "brief", "materials", "review"}, "ROOM_PLAN_INVALID")
    clock(value["as_of"])
    catalogue = value.get("catalogue", {})
    require(isinstance(catalogue, dict) and set(catalogue) <= {"cursor", "limit"},
            "ROOM_CATALOGUE_INVALID")
    limit = catalogue.get("limit", 4)
    require(type(limit) is int and 1 <= limit <= 4, "ROOM_PAGE_LIMIT")
    result = {"as_of": value["as_of"], "catalogue": {**deepcopy(catalogue), "limit": limit},
              "reference": research_reference(value["reference"]) if value.get("reference") is not None else None}
    for name in ("agenda", "brief"):
        result[name] = asdict(artifact_reference(value[name])) if value.get(name) is not None else None
    if value.get("materials") is not None:
        from decision_kernel.application.reading_materials import material_plan
        result["materials"] = material_plan(value["materials"])
    if value.get("review") is not None:
        from decision_kernel.application.research_review import review_plan
        result["review"] = review_plan(value["review"])
    return result


def _failure(error):
    if isinstance(error, MissingObject):
        return {"status": "missing", "error": "ROOM_OBJECT_MISSING"}
    if isinstance(error, StoreUnavailable):
        return {"status": "unavailable", "error": "ROOM_OBJECT_UNAVAILABLE"}
    if isinstance(error, IntegrityError):
        return {"status": "invalid", "error": "ROOM_OBJECT_INTEGRITY_FAILED"}
    return {"status": "invalid", "error": "ROOM_OBJECT_INVALID"}


def _panel(read, reference):
    try:
        view = read()
        if len(encoded(view)) > MAX_PANEL_BYTES:
            return {"reference": reference, "status": "not-included", "error": "ROOM_PANEL_BOUND"}
        return {"reference": reference, "status": "readable", "view": view}
    except (RecordError, KeyError, TypeError) as error:
        return {"reference": reference, **_failure(error)}


def brief_relation(reference, brief):
    """Join exact references only; never infer adoption from a company/case match."""
    if brief["status"] != "readable":
        return {"status": "not-supplied" if brief["status"] == "not-supplied" else "unknown"}
    section = brief["view"].get("research")
    if section is None:
        return {"status": "research-not-tracked-in-this-brief"}
    for row in section["rows"]:
        if row["item"]["reference"] != reference:
            continue
        included = next((i for i in section["included"] if i["version_id"] == row["version_id"]), None)
        return {"status": "included" if included else "considered-not-included",
                "classification": included["classification"] if included else None,
                "reading_status_at_delivery": row["status"],
                "excerpt_range": {k: row["item"][k] for k in ("start", "end")}}
    return {"status": "not-considered-in-this-brief"}


class ReadingRoom:
    def __init__(self, store, *, source_reader=None):
        self.store = store
        self.source_reader = source_reader or store.read_source

    def _catalogue(self, args):
        from decision_kernel.application.research_catalogue import ResearchCatalogue
        return ResearchCatalogue(self.store).page(**args)

    def _agenda(self, references, plan):
        from decision_kernel.application.research_agenda import ResearchAgenda
        view = ResearchAgenda(self.store).build({"references": references, "as_of": plan["as_of"],
                                                "baseline": plan["agenda"], "changes": []})
        # A generated projection is not a new saved agenda or an invitation to write.
        view.pop("artifact", None)
        return view

    def _brief(self, reference):
        from decision_kernel.application.brief_delivery import open_edition
        from decision_kernel.application.research_artifacts import ResearchArtifacts
        doc, _ = open_edition(self.source_reader, ResearchArtifacts(self.store), reference)
        return doc

    def _research(self, reference):
        research = self.store.read_revision(BlobRef(**reference))
        require(research["reference"] == reference, "ROOM_RESEARCH_REFERENCE_MISMATCH")
        payload = research["record"]["input"]
        body = research["body"]
        return {"case_id": payload["case_id"], "revision_id": payload["revision_id"],
                "kind": payload["kind"], "body": body, "body_sha256": digest(body.encode("utf-8")),
                "stored_at": research["record"]["stored_at"], "predecessor": payload["predecessor"]}

    def build(self, value, *, feedback_export=True, material_export=True):
        plan = plan_input(value)
        require(plan["agenda"] is None or feedback_export, "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED")
        require(not (plan.get("materials") or plan.get("review")) or material_export, "ROOM_PUBLIC_MATERIAL_NOT_CONFIRMED")
        # A repository/cursor failure must not masquerade as an empty catalogue.
        catalogue = self._catalogue(plan["catalogue"])
        refs = [c["reference"] for c in catalogue["items"] if c["status"] == "readable"]
        if plan["reference"] is not None and plan["reference"] not in refs:
            refs.append(plan["reference"])
        agenda = _panel(lambda: self._agenda(refs, plan), plan["agenda"])
        brief = {"reference": None, "status": "not-supplied"}
        if plan["brief"] is not None:
            brief = _panel(lambda: self._brief(plan["brief"]), plan["brief"])
            if brief["status"] == "readable":
                doc = brief["view"]
                if not feedback_export and (doc["attention_state"] or
                        (doc.get("research") or {}).get("agenda_reference")):
                    # Do not put previously saved notes in the host's public output.
                    brief = {"reference": plan["brief"], "status": "permission-required",
                             "error": "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED"}
        readings, used = [], 0
        for ref in refs:
            row = {"reference": deepcopy(ref),
                   "origin": "catalogue-page" if any(c.get("reference") == ref for c in catalogue["items"])
                             else "explicit-reference",
                   "open_request": {"operation": "resume", "payload": {"reference": deepcopy(ref)}}}
            try:
                detail = self._research(ref)
                size = len(encoded(detail))
                if len(detail["body"].encode("utf-8")) > MAX_BODY_BYTES or used + size > MAX_BODIES_BYTES:
                    row.update(status="not-included", error="ROOM_BODY_BOUND_USE_EXACT_READER")
                else:
                    row.update(status="readable", **detail)
                    used += size
            except (RecordError, KeyError, TypeError) as error:
                row.update(_failure(error))
            row["brief_relation"] = brief_relation(ref, brief)
            readings.append(row)
        next_plan = None
        if catalogue["next_cursor"] is not None:
            next_plan = {**deepcopy(plan), "catalogue": {
                "cursor": deepcopy(catalogue["next_cursor"]), "limit": plan["catalogue"]["limit"]}}
        result = {"schema": "reading-room-v1", "plan": plan, "catalogue": catalogue,
                  "readings": readings, "agenda": agenda, "brief": brief, "next_plan": next_plan,
                  "coverage": "one-catalogue-page-plus-explicit-reference-and-specified-artifacts",
                  "time_basis": "as-of-is-review-clock-not-research-cutoff-or-PIT",
                  "human_selection": "not-recorded-by-reading", "human_acceptance": "not-established",
                  "research_assessment": "not-performed", "execution": "not-started",
                  "notification": "not-sent", "persistence": "not-written"}
        partial = catalogue["status"] == "partial-page" or agenda["status"] != "readable" or any(
            r["status"] != "readable" for r in readings) or brief["status"] not in ("readable", "not-supplied")
        if agenda["status"] == "readable" and agenda["view"]["status"] == "partial":
            partial = True
        if plan.get("materials"):
            from decision_kernel.application.reading_materials import open_materials
            try:
                view = open_materials(self.store, plan["materials"])
                result["materials"] = {"reference": plan["materials"]["reference"], "status": "readable", "view": view}
                partial = partial or view["status"] == "partial"
            except (RecordError, KeyError, TypeError) as error:
                failure = ({"status": "not-included", "error": "ROOM_MATERIAL_DISPLAY_BOUND"}
                           if isinstance(error, RecordError) and str(error) == "ROOM_MATERIAL_DISPLAY_BOUND"
                           else _failure(error))
                result["materials"] = {"reference": plan["materials"]["reference"], **failure}
                partial = True
        if plan.get("review"):
            from decision_kernel.application.research_review import open_review
            try:
                view = open_review(self.store, plan["review"], agenda=agenda, brief=brief)
                review = {"reference": plan["review"]["after"]["reference"], "status": "readable", "view": view}
            except (RecordError, KeyError, TypeError) as error:
                failure = ({"status": "not-included", "error": "REVIEW_DISPLAY_BOUND"}
                           if isinstance(error, RecordError) and str(error) == "REVIEW_DISPLAY_BOUND" else _failure(error))
                review = {"reference": plan["review"]["after"]["reference"], **failure}
            result["review"] = review
            partial = partial or review["status"] != "readable" or review.get("view", {}).get("status") == "partial"
        result["status"] = "partial" if partial else "readable"
        raw = encoded(result)
        require(len(raw) <= MAX_VIEW_BYTES, "ROOM_VIEW_BOUND")
        # Final mutable access check after every component and before any export.
        self.store.backend.head()
        return json.loads(raw)  # Workpaper tuple/list normalization, no byte rewriting.
