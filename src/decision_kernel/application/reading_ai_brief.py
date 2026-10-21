"""A read-only AI Brief projection on the original Hosted artifact owner.

The author document is never rebuilt. Catalogue owns the page snapshot, Hosted
owns adoption validation, and the native host owns publication at every export.
"""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application import hosted_handoff
from decision_kernel.application.brief_discovery import cutoff_age
from decision_kernel.application.market_reading_scope import operation_failure
from decision_kernel.domain.records import (
    IntegrityError, MissingObject, RecordError, StoreUnavailable, digest, encoded, require,
)
from decision_kernel.domain.research_artifacts import artifact_reference

LATEST = {"select": "latest-ai-brief"}


def selected(plan):
    return isinstance(plan, dict) and plan.get("ai_brief") is not None


def latest_selector(value):
    return isinstance(value, dict) and value == LATEST


def selection(value):
    return deepcopy(LATEST) if latest_selector(value) else asdict(artifact_reference(value))


def bind_plan(plan, catalogue):
    # Even an exact Brief travels with one Catalogue snapshot. An explicit
    # refresh may release the catalogue cursor, never the exact Brief itself.
    plan["catalogue"] = {"limit": plan["catalogue"]["limit"], "cursor": {
        "schema": "research-catalogue-cursor-v1", "repository": catalogue["repository"],
        "snapshot": catalogue["snapshot"], "offset": catalogue["offset"]}}


def _projection(opened):
    doc = opened["document"]
    require(doc["kind"] == "brief", "ROOM_AI_BRIEF_KIND_REQUIRED")
    # The exact reader already checked every full research body. The Room adds
    # only navigation metadata; it does not copy those bodies into a new Brief.
    research = []
    for restored in opened["research"]:
        payload = restored["record"]["input"]
        research.append({"reference": deepcopy(restored["reference"]),
                         "case_id": payload["case_id"], "revision_id": payload["revision_id"],
                         "kind": payload["kind"], "body_sha256": digest(restored["body"].encode("utf-8"))})
    return {"document": deepcopy(doc), "body_sha256": digest(doc["body"].encode("utf-8")),
            "research": research,
            "watch_evidence_adoption": deepcopy(opened.get("watch_evidence_adoption", [])),
            "human_acceptance": opened["human_acceptance"],
            "economic_quality": opened["economic_quality"]}


def read_panel(store, plan, *, snapshot):
    query = plan["ai_brief"]
    reference = None if latest_selector(query) else deepcopy(query)
    panel = {"snapshot": snapshot, "reference": reference,
             "watch_publication_required": False, "persistence": "not-written"}
    if reference is not None:
        panel["open_request"] = {"operation": "read-hosted-handoff", "payload": {"reference": reference}}
    scope = getattr(store.backend, "component_read_scope", None)
    with scope() if callable(scope) else nullcontext():
        try:
            if latest_selector(query):
                found = hosted_handoff.latest(store, kind="brief", as_of=plan["as_of"],
                                              limit=1, snapshot=snapshot, strict=True)
                # Partial discovery may still have opened a real Watch Brief
                # (or a v1 Brief whose exact baseline has Watch). Preserve all
                # actual dependency scopes even when no body is auto-selected.
                panel["watch_publication_required"] = hosted_handoff.publication_required(store, found)
                panel["discovery"] = {key: deepcopy(value) for key, value in found.items() if key != "items"}
                panel["discovery"]["choices"] = [{"reference": deepcopy(item["reference"]),
                    "cutoff": item["document"]["cutoff"],
                    "execution_date": item["document"]["execution_date"]} for item in found["items"]]
                if found["status"] != "readable":
                    # An unreadable candidate may be newer. Do not silently
                    # promote an older good one into a complete latest claim.
                    return {**panel, "status": found["status"]}
                opened = found["items"][0]
                reference = deepcopy(opened["reference"])
                panel["reference"] = reference
                plan["ai_brief"] = deepcopy(reference)
            else:
                opened = hosted_handoff.open_handoff(store, reference)
            view = _projection(opened)
            # This is derived from the validated original document and its
            # exact immediate baseline, never a caller's permission flag.
            panel["watch_publication_required"] = hosted_handoff.publication_required(store, opened)
            return {**panel, "status": "readable", "view": view,
                    "age": cutoff_age(view["document"]["cutoff"], plan["as_of"]),
                    "open_request": {"operation": "read-hosted-handoff", "payload": {"reference": reference}}}
        except (RecordError, KeyError, TypeError, ValueError) as error:
            if operation_failure(error) or str(error) == "PATH_OUTSIDE_STORE":
                raise
            status = ("missing" if isinstance(error, MissingObject) else
                      "unavailable" if isinstance(error, StoreUnavailable) else "invalid")
            code = ("ROOM_AI_BRIEF_MISSING" if isinstance(error, MissingObject) else
                    "ROOM_AI_BRIEF_INTEGRITY_FAILED" if isinstance(error, IntegrityError) else
                    "ROOM_AI_BRIEF_INVALID")
            return {**panel, "status": status, "error": code}


def relation(reference, panel):
    if panel["status"] != "readable":
        return {"status": "unknown"}
    doc = panel["view"]["document"]
    research = [deepcopy(item) for item in doc["adoptions"] if item["reference"] == reference]
    watches = [deepcopy(item) for item in doc.get("watch_adoptions", []) if item["reference"] == reference]
    return {"status": "adopted" if research or watches else "not-adopted-in-this-ai-brief",
            "research_adoptions": research, "watch_adoptions": watches}


def attach_panel(result, panel, *, max_panel_bytes, max_view_bytes, render_model):
    # Omit the entire additional display copy when it cannot fit. The original
    # handoff, exact request, publication dependency and normal Room stay intact.
    omitted = {key: deepcopy(value) for key, value in panel.items() if key not in ("view", "discovery")}
    omitted.update(status="not-included", error="ROOM_AI_BRIEF_PANEL_BOUND_USE_EXACT_READER")
    candidates = (panel, omitted) if len(encoded(panel)) <= max_panel_bytes else (omitted,)
    for candidate in candidates:
        combined = {**result, "ai_brief": candidate,
                    "readings": [{**row, "ai_brief_relation": relation(row["reference"], candidate)}
                                 for row in result["readings"]]}
        if candidate["status"] not in ("readable", "no-saved-handoff"):
            combined["status"] = "partial"
        if len(encoded(combined)) > max_view_bytes:
            omitted["error"] = "ROOM_AI_BRIEF_VIEW_BOUND_USE_EXACT_READER"
            continue
        try:
            render_model(combined)
        except RecordError as error:
            if str(error) != "ROOM_HTML_BOUND":
                raise
            omitted["error"] = "ROOM_AI_BRIEF_HTML_BOUND_USE_EXACT_READER"
            continue
        result.update(ai_brief=candidate, readings=combined["readings"], status=combined["status"])
        return
    raise RecordError("ROOM_AI_BRIEF_VIEW_BOUND")
