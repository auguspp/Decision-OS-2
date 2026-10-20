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
    from decision_kernel.application.brief_discovery import latest_selector
    from decision_kernel.domain.research_artifacts import artifact_reference

    require(isinstance(value, dict) and "as_of" in value and set(value) <= {
        "as_of", "catalogue", "reference", "agenda", "brief", "materials", "review", "industry", "market", "discovery", "participants"}, "ROOM_PLAN_INVALID")
    clock(value["as_of"])
    catalogue = value.get("catalogue", {})
    require(isinstance(catalogue, dict) and set(catalogue) <= {"cursor", "limit"},
            "ROOM_CATALOGUE_INVALID")
    limit = catalogue.get("limit", 4)
    require(type(limit) is int and 1 <= limit <= 4, "ROOM_PAGE_LIMIT")
    result = {"as_of": value["as_of"], "catalogue": {**deepcopy(catalogue), "limit": limit},
              "reference": research_reference(value["reference"]) if value.get("reference") is not None else None}
    for name in ("agenda", "brief"):
        if name == "brief" and latest_selector(value.get(name)):
            result[name] = deepcopy(value[name])
        else:
            result[name] = asdict(artifact_reference(value[name])) if value.get(name) is not None else None
    if value.get("materials") is not None:
        from decision_kernel.application.reading_materials import material_plan
        result["materials"] = material_plan(value["materials"])
    if value.get("review") is not None:
        from decision_kernel.application.research_review import review_plan
        result["review"] = review_plan(value["review"])
    if value.get("industry") is not None:
        from decision_kernel.application.reading_industry import industry_plan
        result["industry"] = industry_plan(value["industry"])
    if value.get("market") is not None:
        from decision_kernel.application.reading_market import market_plan
        result["market"] = market_plan(value["market"])
    if value.get("discovery") is not None:
        from decision_kernel.application.reading_discovery import discovery_plan
        result["discovery"] = discovery_plan(value["discovery"])
    if value.get("participants") is not None:
        from decision_kernel.application.reading_participant import participant_plan
        result["participants"] = participant_plan(value["participants"])
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
    def __init__(self, store, *, source_reader=None, market_registry=None):
        self.store = store
        self.market_registry = market_registry
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

    def _discover_brief(self, snapshot, as_of):
        from decision_kernel.application.brief_discovery import discover_latest
        return discover_latest(self.store, snapshot=snapshot, as_of=as_of)

    def _research(self, reference):
        research = self.store.read_revision(BlobRef(**reference))
        require(research["reference"] == reference, "ROOM_RESEARCH_REFERENCE_MISMATCH")
        payload = research["record"]["input"]
        body = research["body"]
        return {"case_id": payload["case_id"], "revision_id": payload["revision_id"],
                "kind": payload["kind"], "body": body, "body_sha256": digest(body.encode("utf-8")),
                "stored_at": research["record"]["stored_at"], "predecessor": payload["predecessor"]}

    def build(self, value, *, feedback_export=True, material_export=True, industry_export=True, market_export=True,
              watch_export=True, participant_export=True):
        plan = plan_input(value)
        require(plan["agenda"] is None or feedback_export, "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED")
        require(not (plan.get("materials") or plan.get("review")) or material_export, "ROOM_PUBLIC_MATERIAL_NOT_CONFIRMED")
        require(not plan.get("industry") or industry_export, "ROOM_PUBLIC_INDUSTRY_NOT_CONFIRMED")
        require(not plan.get("market") or market_export is True, "ROOM_PUBLIC_MARKET_NOT_CONFIRMED")
        require(not plan.get("discovery") or market_export is True, "ROOM_PUBLIC_DISCOVERY_NOT_CONFIRMED")
        require(not plan.get("participants") or participant_export is True, "ROOM_PUBLIC_PARTICIPANT_NOT_CONFIRMED")
        # A repository/cursor failure must not masquerade as an empty catalogue.
        catalogue = self._catalogue(plan["catalogue"])
        from decision_kernel.application.brief_discovery import cutoff_age, latest_selector
        refresh_plan = {**deepcopy(plan), "catalogue": {"limit": plan["catalogue"]["limit"]}}
        discovery = None
        if latest_selector(plan["brief"]):
            try:
                discovery = self._discover_brief(catalogue["snapshot"], plan["as_of"])
            except (RecordError, KeyError, TypeError) as error:
                discovery = {"snapshot": catalogue["snapshot"], "complete": False,
                             "reference": None, **_failure(error)}
            if discovery["status"] == "selected":
                # All subsequent navigation/replay uses the frozen ArtifactRef.
                # Only the explicit refresh plan resolves the selector again.
                plan["brief"] = deepcopy(discovery["reference"])
        refs = [c["reference"] for c in catalogue["items"] if c["status"] == "readable"]
        if plan["reference"] is not None and plan["reference"] not in refs:
            refs.append(plan["reference"])
        agenda = _panel(lambda: self._agenda(refs, plan), plan["agenda"])
        from decision_kernel.application.watch_evidence import export_required
        if agenda["status"] == "readable" and export_required(agenda["view"]) and not watch_export:
            agenda = {"reference": plan["agenda"], "status": "sources-permission-required",
                      "error": "ROOM_PUBLIC_WATCH_EVIDENCE_NOT_CONFIRMED"}
        brief = {"reference": None, "status": "not-supplied"}
        if latest_selector(plan["brief"]):
            brief = {"reference": None, "status": discovery["status"]}
            if discovery.get("error"):
                brief["error"] = discovery["error"]
        elif plan["brief"] is not None:
            brief = _panel(lambda: self._brief(plan["brief"]), plan["brief"])
            if brief["status"] == "readable":
                doc = brief["view"]
                brief["age"] = cutoff_age(doc["plan"]["cutoff"], plan["as_of"])
                if not feedback_export and (doc["attention_state"] or
                        (doc.get("research") or {}).get("agenda_reference")):
                    # Do not put previously saved notes in the host's public output.
                    brief = {"reference": plan["brief"], "status": "permission-required",
                             "error": "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED"}
                if not industry_export and "industry" in doc:
                    brief = {"reference": plan["brief"], "status": "sources-permission-required",
                             "error": "ROOM_PUBLIC_INDUSTRY_NOT_CONFIRMED"}
                if not watch_export and export_required(doc.get("research") or {}):
                    brief = {"reference": plan["brief"], "status": "sources-permission-required",
                             "error": "ROOM_PUBLIC_WATCH_EVIDENCE_NOT_CONFIRMED"}
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
                  "refresh_plan": refresh_plan,
                  "coverage": "one-catalogue-page-plus-explicit-reference-and-specified-artifacts",
                  "time_basis": "as-of-is-review-clock-not-research-cutoff-or-PIT",
                  "human_selection": "not-recorded-by-reading", "human_acceptance": "not-established",
                  "research_assessment": "not-performed", "execution": "not-started",
                  "notification": "not-sent", "persistence": "not-written"}
        partial = catalogue["status"] == "partial-page" or agenda["status"] != "readable" or any(
            r["status"] != "readable" for r in readings) or brief["status"] not in ("readable", "not-supplied", "no-saved-brief")
        if discovery is not None:
            result["brief_discovery"] = discovery
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
        if plan.get("industry"):
            from decision_kernel.application.reading_industry import open_industry
            try:
                industry = open_industry(self.store, plan["industry"])
            except (RecordError, KeyError, TypeError) as error:
                industry = {"reference": plan["industry"]["reference"], "plan": plan["industry"],
                            "open_request": {"operation": "industry-read", "payload": deepcopy(plan["industry"])},
                            **_failure(error)}
            from decision_kernel.application.industry_adoption import brief_adoption
            industry["brief_adoption"] = brief_adoption(industry, brief)
            partial = partial or industry["brief_adoption"]["status"] in ("unknown", "not-included")
            result["industry"] = industry
            partial = partial or industry["status"] != "readable" or industry.get("view", {}).get("status") == "partial"
        if plan.get("market"):
            from decision_kernel.application.reading_market import open_market
            from decision_kernel.observations.market_daily import MarketInputError
            try:
                market = open_market(self.store, plan["market"], registry=self.market_registry)
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                market = {"reference": plan["market"]["reference"], "plan": plan["market"],
                          "open_request": {"operation": "read-source", "payload": {
                              "reference": deepcopy(plan["market"]["reference"])}}, **_failure(error)}
            result["market"] = market
            partial = partial or market["status"] != "readable" or market.get("partial", False)
        if plan.get("discovery"):
            from decision_kernel.application.reading_discovery import open_discovery
            from decision_kernel.observations.market_daily import MarketInputError
            try:
                discovery_panel = open_discovery(self.store, plan["discovery"], registry=self.market_registry)
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                discovery_panel = {"reference": plan["discovery"].get("reference"),
                                   "plan": plan["discovery"], **_failure(error)}
            result["discovery"] = discovery_panel
            partial = partial or discovery_panel["status"] != "readable" or discovery_panel.get("partial", False)
        if plan.get("participants"):
            from decision_kernel.application.reading_participant import open_participants
            try:
                participants = open_participants(self.store, plan["participants"], registry=self.market_registry)
            except (RecordError, KeyError, TypeError) as error:
                failure = ({"status": "not-included", "error": "ROOM_PARTICIPANT_DISPLAY_BOUND"}
                           if isinstance(error, RecordError) and str(error) == "ROOM_PARTICIPANT_DISPLAY_BOUND"
                           else _failure(error))
                participants = {"reference": plan["participants"]["reference"],
                                "plan": plan["participants"], **failure}
            result["participants"] = participants
            partial = partial or participants["status"] != "readable" or participants.get("partial", False)
        result["status"] = "partial" if partial else "readable"
        raw = encoded(result)
        require(len(raw) <= MAX_VIEW_BYTES, "ROOM_VIEW_BOUND")
        # Final mutable access check after every component and before any export.
        self.store.backend.head()
        return json.loads(raw)  # Workpaper tuple/list normalization, no byte rewriting.
