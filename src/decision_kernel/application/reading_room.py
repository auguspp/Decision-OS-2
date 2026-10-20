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
        "as_of", "catalogue", "reference", "agenda", "brief", "materials", "review", "industry", "market", "global_context", "auction", "auction_minute", "auction_next_session", "discovery", "participants", "event_calendar", "decision_preparation", "outcome", "structure"}, "ROOM_PLAN_INVALID")
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
    if value.get("global_context") is not None:
        from decision_kernel.application.reading_global import global_plan
        result["global_context"] = global_plan(value["global_context"])
    if value.get("auction") is not None:
        from decision_kernel.application.reading_auction import auction_plan
        result["auction"] = auction_plan(value["auction"])
    if value.get("auction_minute") is not None:
        from decision_kernel.application.reading_auction_minute import minute_plan_input
        result["auction_minute"] = minute_plan_input(value["auction_minute"])
    if value.get("auction_next_session") is not None:
        from decision_kernel.application.reading_auction_next_session import next_session_plan
        result["auction_next_session"] = next_session_plan(value["auction_next_session"])
    if value.get("discovery") is not None:
        from decision_kernel.application.reading_discovery import discovery_plan
        result["discovery"] = discovery_plan(value["discovery"])
    if value.get("participants") is not None:
        from decision_kernel.application.reading_participant import participant_plan
        result["participants"] = participant_plan(value["participants"])
    if value.get("event_calendar") is not None:
        from decision_kernel.application.event_calendar import calendar_plan
        result["event_calendar"] = calendar_plan(value["event_calendar"])
    if value.get("decision_preparation") is not None:
        from decision_kernel.interfaces.decision_preparation_commands import preflight
        preflight("read-decision-preparation", value["decision_preparation"])
        result["decision_preparation"] = deepcopy(value["decision_preparation"])
    if value.get("outcome") is not None:
        from decision_kernel.application.reading_outcome import outcome_plan
        result["outcome"] = outcome_plan(value["outcome"])
    if value.get("structure") is not None:
        from decision_kernel.application.close_structure import plan_input as structure_plan
        result["structure"] = structure_plan(value["structure"])
        require(clock(result["structure"]["as_of"]) <= clock(value["as_of"]), "ROOM_STRUCTURE_CLOCK_AFTER_READING")
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
        from decision_kernel.application.research_book import project_research
        research = self.store.read_revision(BlobRef(**reference))
        require(research["reference"] == reference, "ROOM_RESEARCH_REFERENCE_MISMATCH")
        payload = research["record"]["input"]
        body = research["body"]
        return {"case_id": payload["case_id"], "revision_id": payload["revision_id"],
                "kind": payload["kind"], "body": body, "body_sha256": digest(body.encode("utf-8")),
                "stored_at": research["record"]["stored_at"], "predecessor": payload["predecessor"],
                "book_projection": project_research(research, max_bytes=MAX_PANEL_BYTES)}

    def build(self, value, *, feedback_export=True, material_export=True, industry_export=True, market_export=True,
              watch_export=True, participant_export=True, decision_export=True, outcome_export=True):
        plan = plan_input(value)
        require(plan["agenda"] is None or feedback_export, "ROOM_FEEDBACK_EXPORT_NOT_CONFIRMED")
        require(not (plan.get("materials") or plan.get("review")) or material_export, "ROOM_PUBLIC_MATERIAL_NOT_CONFIRMED")
        require(not plan.get("industry") or industry_export, "ROOM_PUBLIC_INDUSTRY_NOT_CONFIRMED")
        require(not plan.get("market") or market_export is True, "ROOM_PUBLIC_MARKET_NOT_CONFIRMED")
        require(not plan.get("global_context") or market_export is True, "ROOM_PUBLIC_GLOBAL_NOT_CONFIRMED")
        require(not plan.get("auction") or market_export is True, "ROOM_PUBLIC_AUCTION_NOT_CONFIRMED")
        require(not plan.get("auction_minute") or market_export is True, "ROOM_PUBLIC_MINUTE_NOT_CONFIRMED")
        require(not plan.get("auction_next_session") or market_export is True, "ROOM_PUBLIC_AUCTION_NEXT_NOT_CONFIRMED")
        require(not plan.get("discovery") or market_export is True, "ROOM_PUBLIC_DISCOVERY_NOT_CONFIRMED")
        require(not plan.get("participants") or participant_export is True, "ROOM_PUBLIC_PARTICIPANT_NOT_CONFIRMED")
        require(not plan.get("event_calendar") or industry_export is True, "ROOM_PUBLIC_CALENDAR_NOT_CONFIRMED")
        require(not plan.get("decision_preparation") or decision_export is True, "ROOM_PUBLIC_DECISION_PREPARATION_NOT_CONFIRMED")
        require(not plan.get("outcome") or outcome_export is True, "ROOM_PUBLIC_OUTCOME_NOT_CONFIRMED")
        require(not plan.get("structure") or market_export is True, "ROOM_PUBLIC_STRUCTURE_NOT_CONFIRMED")
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
        readings, projections, used = [], [], 0
        for ref in refs:
            row = {"reference": deepcopy(ref),
                   "origin": "catalogue-page" if any(c.get("reference") == ref for c in catalogue["items"])
                             else "explicit-reference",
                   "open_request": {"operation": "resume", "payload": {"reference": deepcopy(ref)}}}
            try:
                detail = self._research(ref)
                projection = detail.pop("book_projection", None)
                size = len(encoded(detail))
                if len(detail["body"].encode("utf-8")) > MAX_BODY_BYTES or used + size > MAX_BODIES_BYTES:
                    row.update(status="not-included", error="ROOM_BODY_BOUND_USE_EXACT_READER")
                else:
                    row.update(status="readable", **detail)
                    used += size
                    if projection is not None:
                        projections.append(projection)
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
                # Preserve the original industry access, source-scope and whole-read failures.
                if isinstance(error, StoreUnavailable) or str(error) in (
                    "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
                    "OPERATING_SOURCE_SCOPE_BOUND", "READING_SCOPE_CLOSED", "SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED",
                    "INDUSTRY_READING_BOUND", "INDUSTRY_USES_READING_BOUND",
                ):
                    raise
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
            except StoreUnavailable:
                # Preserve the original market readers' operation-wide failure.
                raise
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                market = {"reference": plan["market"]["reference"], "plan": plan["market"],
                          "open_request": {"operation": "read-source", "payload":
                              deepcopy(plan["market"]["reference"])}, **_failure(error)}
            result["market"] = market
            partial = partial or market["status"] != "readable" or market.get("partial", False)
        if plan.get("global_context"):
            from decision_kernel.application.reading_global import open_global
            # The global consumer isolates ordinary family gaps itself. Its
            # original access/scope/budget failures must escape the whole room.
            context = open_global(self.store, plan["global_context"], registry=self.market_registry)
            result["global_context"] = context
            partial = partial or context["status"] != "readable"
        if plan.get("auction"):
            from decision_kernel.application.reading_auction import open_auction
            # Ordinary missing inputs are local inside the source consumer;
            # original access/scope/budget failures stop this entire Room.
            auction = open_auction(self.store, plan["auction"], registry=self.market_registry)
            result["auction"] = auction
            partial = partial or auction["status"] != "readable"
        if plan.get("auction_minute"):
            from decision_kernel.application.reading_auction_minute import open_auction_minute
            minute = open_auction_minute(self.store, plan["auction_minute"], registry=self.market_registry)
            result["auction_minute"] = minute
            partial = partial or minute["status"] != "readable"
        if plan.get("auction_next_session"):
            from decision_kernel.application.reading_auction_next_session import open_next_session
            context = open_next_session(self.store, plan["auction_next_session"], registry=self.market_registry)
            result["auction_next_session"] = context
            partial = partial or context["status"] != "readable"
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
            from decision_kernel.application.participant_reading import operation_failure as participant_operation_failure
            from decision_kernel.application.reading_participant import open_participants
            try:
                participants = open_participants(self.store, plan["participants"], registry=self.market_registry)
            except (RecordError, KeyError, TypeError) as error:
                if participant_operation_failure(error):
                    raise
                failure = ({"status": "not-included", "error": "ROOM_PARTICIPANT_DISPLAY_BOUND"}
                           if isinstance(error, RecordError) and str(error) == "ROOM_PARTICIPANT_DISPLAY_BOUND"
                           else _failure(error))
                participants = {"reference": plan["participants"]["reference"],
                                "plan": plan["participants"], **failure}
            result["participants"] = participants
            partial = partial or participants["status"] != "readable" or participants.get("partial", False)
        if plan.get("event_calendar"):
            from decision_kernel.application.event_calendar import open_calendar, operation_failure
            try:
                calendar = open_calendar(self.store, plan["event_calendar"])
            except (RecordError, KeyError, TypeError) as error:
                if operation_failure(error):
                    raise
                calendar = {"reference": plan["event_calendar"]["reference"],
                            "plan": plan["event_calendar"], **_failure(error)}
            result["event_calendar"] = calendar
            partial = partial or calendar["status"] != "readable" or calendar.get("view", {}).get("status") == "partial"
        if plan.get("decision_preparation"):
            from decision_kernel.interfaces.decision_preparation_commands import perform
            request = {"operation": "read-decision-preparation", "payload": deepcopy(plan["decision_preparation"])}
            reference = plan["decision_preparation"]["reference"]
            try:
                view = perform(self.store, request["operation"], request["payload"], registry=self.market_registry)
                require(len(encoded(view)) <= MAX_PANEL_BYTES, "ROOM_PANEL_BOUND")
                prepared = {"reference": reference, "status": "readable", "view": view}
            except (MissingObject, IntegrityError, KeyError, TypeError) as error:
                # Ordinary missing/corrupt objects remain a visible panel error.
                # Store, privacy, scope and budget RecordErrors escape immediately;
                # transport refusal/size bounds are not ordinary corrupt objects.
                if isinstance(error, IntegrityError) and str(error) in {
                        "UNEXPECTED_REMOTE_RESPONSE", "REMOTE_RESPONSE_TOO_LARGE"}:
                    raise
                prepared = {"reference": reference, **_failure(error)}
            prepared["open_request"] = request
            result["decision_preparation"] = prepared
            partial = partial or prepared["status"] != "readable" or prepared.get("view", {}).get("document", {}).get("status") == "partial"
        if plan.get("outcome"):
            from decision_kernel.application.reading_outcome import open_outcome
            from decision_kernel.application.outcome_review import _fatal
            try:
                outcome = open_outcome(self.store, plan["outcome"], registry=self.market_registry)
            except (RecordError, KeyError, TypeError) as error:
                # Access and source/API budgets terminate the whole export;
                # the component owner has already retained ordinary row gaps.
                if _fatal(error):
                    raise
                failure = ({"status": "not-included", "error": "OUTCOME_RESULT_BOUND"}
                           if isinstance(error, RecordError) and str(error) == "OUTCOME_RESULT_BOUND"
                           else _failure(error))
                outcome = {"plan": plan["outcome"], **failure}
            result["outcome"] = outcome
            partial = partial or outcome["status"] != "readable" or outcome.get("partial", False)
        if plan.get("structure"):
            from decision_kernel.application.close_structure import fatal_read_error, open_structure
            from decision_kernel.observations.market_daily import MarketInputError
            try:
                structure = open_structure(self.store, plan["structure"], registry=self.market_registry)
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                if fatal_read_error(error):
                    raise
                failure = ({"status": "not-included", "error": "STRUCTURE_DISPLAY_BOUND"}
                           if str(error) == "STRUCTURE_DISPLAY_BOUND" else _failure(error))
                structure = {"reference": plan["structure"]["reference"], "plan": plan["structure"],
                    "open_request": {"operation": "read-source", "payload": deepcopy(plan["structure"]["reference"])},
                    **failure}
            result["structure"] = structure
            partial = partial or structure["status"] != "readable" or structure.get("partial", False)
        result["status"] = "partial" if partial else "readable"
        # Validate the original view first. Book has no I/O and a separate
        # display budget; its duplicate structured fields cannot evict bodies.
        require(len(encoded(result)) <= MAX_VIEW_BYTES, "ROOM_VIEW_BOUND")
        from decision_kernel.application.research_book import attach_book
        from decision_kernel.interfaces.reading_page import render_page
        attach_book(result, projections, max_panel_bytes=MAX_PANEL_BYTES,
                    max_view_bytes=MAX_VIEW_BYTES, render_model=render_page)
        raw = encoded(result)
        require(len(raw) <= MAX_VIEW_BYTES, "ROOM_VIEW_BOUND")
        # Final mutable access check after every component and before any export.
        self.store.backend.head()
        return json.loads(raw)  # Workpaper tuple/list normalization, no byte rewriting.
