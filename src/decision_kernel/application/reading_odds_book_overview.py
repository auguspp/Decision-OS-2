"""An explicit bounded Book overview and one exact reader in the existing Room."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application import odds_book
from decision_kernel.application.market_reading_scope import operation_failure
from decision_kernel.application.odds_book_overview import read_overview, validate_page_limit
from decision_kernel.application.research_agenda import research_reference
from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference

DETAIL_OPERATIONS = frozenset(odds_book.OPERATIONS.values())


def view_plan(value):
    """Only original queries and exact read operations; no supplied page receipts."""
    require(isinstance(value, dict) and set(value) <= {"query", "page_limit", "detail"},
            "ROOM_ODDS_BOOK_VIEW_INVALID")
    result = {"query": odds_book.validate_plan(value.get("query", {})),
              "page_limit": validate_page_limit(value.get("page_limit", 1)), "detail": None}
    detail = value.get("detail")
    if detail is not None:
        require(isinstance(detail, dict) and set(detail) == {"operation", "payload"},
                "ROOM_ODDS_BOOK_DETAIL_INVALID")
        operation, payload = detail["operation"], detail["payload"]
        require(isinstance(operation, str) and operation in DETAIL_OPERATIONS,
                "ROOM_ODDS_BOOK_DETAIL_OPERATION")
        require(isinstance(payload, dict) and set(payload) == {"reference"},
                "ROOM_ODDS_BOOK_DETAIL_REFERENCE")
        reference = (research_reference(payload["reference"]) if operation == "resume"
                     else asdict(artifact_reference(payload["reference"])))
        result["detail"] = {"operation": operation, "payload": {"reference": reference}}
    return result


def bind_view(plan, catalogue):
    query = plan["odds_book_view"]["query"]
    require(query["snapshot"] in (None, catalogue["snapshot"]), "ROOM_ODDS_BOOK_SNAPSHOT_MISMATCH")
    query["snapshot"] = catalogue["snapshot"]
    plan["catalogue"] = {"limit": plan["catalogue"]["limit"], "cursor": {
        "schema": "research-catalogue-cursor-v1", "repository": catalogue["repository"],
        "snapshot": catalogue["snapshot"], "offset": catalogue["offset"]}}


def refresh_view(value):
    from decision_kernel.application.reading_odds_book import refresh_query
    return {**deepcopy(value), "query": refresh_query(value["query"])}


def _detail(store, selection, view, registry):
    if selection is None:
        return None
    require(selection["payload"]["reference"]["repository"] == view["repository"], "FOREIGN_REPOSITORY")
    visible = any(row["status"] == "readable"
        and selection["operation"] == row["open_operation"]
        and selection["payload"] == row["open_payload"]
        for page in view["pages"] for row in page["items"])
    if not visible:
        return {**deepcopy(selection), "status": "invalid", "error": "ROOM_ODDS_BOOK_DETAIL_NOT_IN_VIEW"}
    try:
        if selection["operation"] == "resume":
            from decision_kernel.application.research_reader import ResearchReader
            result = ResearchReader(store).resume(reference=BlobRef(**selection["payload"]["reference"]))
        else:
            from decision_kernel.interfaces.odds_commands import perform
            result = perform(store, selection["operation"], selection["payload"], registry=registry)
        return {**deepcopy(selection), "status": "readable", "result": result}
    except (RecordError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, RecordError) and (operation_failure(error) or str(error) == "PATH_OUTSIDE_STORE"):
            raise
        return {**deepcopy(selection), "status": "missing" if isinstance(error, MissingObject) else "invalid",
                "error": "ROOM_ODDS_BOOK_DETAIL_MISSING" if isinstance(error, MissingObject)
                else "ROOM_ODDS_BOOK_DETAIL_INVALID"}


def read_view(store, value, *, registry=None):
    """All pages and the optional original reader share one component scope/API."""
    scope = getattr(store.backend, "component_read_scope", None)
    with scope() if callable(scope) else nullcontext():
        view = read_overview(store, value["query"], page_limit=value["page_limit"])
        detail = _detail(store, value["detail"], view, registry)
        return view, detail


def attach_view(result, view, detail, *, max_panel_bytes, max_view_bytes, render_model):
    """Admit the complete additional projection without evicting original bodies."""
    value = result["plan"]["odds_book_view"]
    query = deepcopy(value["query"])
    panel = {"status": view["status"], "query": query, "page_limit": value["page_limit"],
             "view": view, "detail": detail,
             "open_request": {"operation": "read-odds-book", "payload": query}}
    if detail is not None and detail["status"] != "readable":
        panel["status"] = "partial"
    if view["next_cursor"] is not None:
        next_plan = deepcopy(result["plan"])
        next_plan["odds_book_view"]["query"]["cursor"] = deepcopy(view["next_cursor"])
        next_plan["odds_book_view"]["detail"] = None
        panel["next_request"] = {"operation": "reading-room", "payload": {"plan": next_plan}}
    omitted = {key: deepcopy(panel[key]) for key in ("query", "page_limit", "open_request")}
    omitted.update(status="not-included", error="ROOM_ODDS_BOOK_OVERVIEW_PANEL_BOUND_USE_ORIGINAL",
                   snapshot=view["snapshot"], scan=deepcopy(view["scan"]), counts=deepcopy(view["counts"]),
                   coverage=view["coverage"])
    if detail is not None:
        omitted["detail"] = {key: deepcopy(detail[key]) for key in ("operation", "payload")}
        omitted["detail"].update(status="not-included", error="ROOM_ODDS_BOOK_DETAIL_DISPLAY_BOUND")
    if "next_request" in panel:
        omitted["next_request"] = deepcopy(panel["next_request"])
    candidates = (panel, omitted) if len(encoded(panel)) <= max_panel_bytes else (omitted,)
    for candidate in candidates:
        combined = {**result, "odds_book_view": candidate}
        if candidate["status"] != "readable":
            combined["status"] = "partial"
        if len(encoded(combined)) > max_view_bytes:
            omitted["error"] = "ROOM_ODDS_BOOK_OVERVIEW_VIEW_BOUND_USE_ORIGINAL"
            continue
        try:
            render_model(combined)
        except RecordError as error:
            if str(error) != "ROOM_HTML_BOUND":
                raise
            omitted["error"] = "ROOM_ODDS_BOOK_OVERVIEW_HTML_BOUND_USE_ORIGINAL"
            continue
        result.update(odds_book_view=candidate, status=combined["status"])
        return
