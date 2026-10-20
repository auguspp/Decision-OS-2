"""One original Odds Book page in the existing Room, never another catalogue.

The native reader owns candidate discovery, validation, identity and scope.
This adapter binds the Room snapshot and admits only an optional display copy.
"""
from contextlib import nullcontext
from copy import deepcopy

from decision_kernel.application import odds_book
from decision_kernel.domain.records import RecordError, encoded, require


def selected(plan):
    """An explicit empty query requests a page; missing/null requests no read."""
    return isinstance(plan, dict) and plan.get("odds_book") is not None


def bind_query(plan, catalogue):
    """Keep the two independently owned page denominators at one real snapshot."""
    query = odds_book.validate_plan(plan["odds_book"])
    require(query["snapshot"] in (None, catalogue["snapshot"]), "ROOM_ODDS_BOOK_SNAPSHOT_MISMATCH")
    query["snapshot"] = catalogue["snapshot"]
    plan["odds_book"] = query
    plan["catalogue"] = {"limit": plan["catalogue"]["limit"], "cursor": {
        "schema": "research-catalogue-cursor-v1", "repository": catalogue["repository"],
        "snapshot": catalogue["snapshot"], "offset": catalogue["offset"]}}


def refresh_query(query):
    """An explicit refresh releases both page positions, never the exact filter."""
    return {key: deepcopy(query[key]) for key in ("limit", "scan_limit", "research_ref")}


def read_page(store, query):
    """Delegate once, preserving the reader's original pinned/native dispatch."""
    scope = getattr(store.backend, "component_read_scope", None)
    with scope() if callable(scope) else nullcontext():
        return odds_book.read_book(store, query)


def attach_panel(result, page, *, max_panel_bytes, max_view_bytes, render_model):
    """Keep the complete original page or omit this additional projection only."""
    query = deepcopy(result["plan"]["odds_book"])
    panel = {"status": "partial" if page["status"] == "partial-page" else "readable",
             "query": query, "view": page,
             "open_request": {"operation": "read-odds-book", "payload": query}}
    omitted = {"status": "not-included", "error": "ROOM_ODDS_BOOK_PANEL_BOUND_USE_ORIGINAL",
               "query": query, "snapshot": page["snapshot"], "scan": deepcopy(page["scan"]),
               "coverage": page["coverage"], "open_request": deepcopy(panel["open_request"])}
    candidates = (panel, omitted) if len(encoded(panel)) <= max_panel_bytes else (omitted,)
    for candidate in candidates:
        combined = {**result, "odds_book": candidate}
        if candidate["status"] != "readable":
            combined["status"] = "partial"
        if len(encoded(combined)) > max_view_bytes:
            omitted["error"] = "ROOM_ODDS_BOOK_VIEW_BOUND_USE_ORIGINAL"
            continue
        try:
            render_model(combined)
        except RecordError as error:
            if str(error) != "ROOM_HTML_BOUND":
                raise
            omitted["error"] = "ROOM_ODDS_BOOK_HTML_BOUND_USE_ORIGINAL"
            continue
        result.update(odds_book=candidate, status=combined["status"])
        return
