"""Book projection of revisions already opened by the original Reading Room.

No store, source reader, history traversal or new research truth lives here.
The existing workpaper parser owns validation; prose remains the original body.
"""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.domain.research_work import read_workpaper

SCHEMA = "research-book-v1"


def project_research(research, *, max_bytes):
    """Use the already verified research dict, without reading any dependency."""
    payload, body = research["record"]["input"], research["body"]
    identity = {key: deepcopy(payload[key]) for key in ("case_id", "revision_id", "kind")}
    result = {"reference": deepcopy(research["reference"]), **identity,
        "status": "readable", "mandate": payload["mandate"],
        "stored_at": research["record"]["stored_at"],
        "stored_at_basis": "storage-time-not-research-fact-cutoff",
        "body_sha256": digest(body.encode("utf-8")), "predecessor": deepcopy(payload["predecessor"]),
        "recorded_unresolved": deepcopy(payload["unresolved"]),
        "sources": deepcopy(payload["sources"]), "source_reading": "references-only-no-additional-source-read",
        "standard_company_id": None, "research_fact_cutoff": None, "strongest_challenge": None,
        "not_established": ["standard-company-id", "research-fact-cutoff", "strongest-challenge-ranking"],
        "challenge_order": "original-author-order-not-strength-or-priority",
        "assessment": "author-declarations-not-independent-review-or-human-acceptance"}
    lines = body.splitlines(keepends=True)
    result["review_lines"] = {"total": len(lines), "initial_line": next((i for i, line in enumerate(lines, 1)
        if line.strip() and len(line.encode("utf-8")) <= 8192), None),
        "selection_basis": "first-eligible-literal-line-not-a-judgment-or-semantic-match"}
    try:
        work = read_workpaper(research)
    except RecordError:
        result.update(status="partial", workpaper_status="invalid", workpaper=None,
                      error="RESEARCH_BOOK_WORKPAPER_INVALID_USE_ORIGINAL")
    else:
        result.update(workpaper_status="validated" if work else "not-recorded", workpaper=None)
        if work is not None:
            request, progress = work
            result["workpaper"] = {"questions": [asdict(q) for q in request.questions],
                "scope_change_reason": request.scope_change_reason,
                "findings": [asdict(f) for f in progress.findings],
                "challenges": [asdict(c) for c in progress.challenges],
                "stop": progress.stop, "stop_reason": progress.stop_reason,
                "limitations": list(progress.limitations), "source_changes": progress.source_changes,
                "challenge_note": progress.challenge_note}
    if len(encoded(result)) > max_bytes:
        return {"reference": deepcopy(research["reference"]), **identity,
                "status": "not-included", "error": "RESEARCH_BOOK_PANEL_BOUND_USE_ORIGINAL"}
    return result


def _same(a, b):
    return a is not None and b is not None and encoded(a) == encoded(b)


def book_panel(catalogue, readings, projections, agenda, brief, *, max_bytes):
    """Keep the Catalogue denominator and every returned failed/explicit row.

    Projection capacity is separate from the pre-existing body budget. A whole
    Book card can be omitted; its exact original route and the old body remain.
    """
    scope = {key: deepcopy(catalogue[key]) for key in (
        "repository", "snapshot", "version_scope", "order", "total_cases", "offset", "returned",
        "remaining", "status", "coverage", "next_cursor") if key in catalogue}
    entries, candidates = [], []
    pairs = [(card, next((r for r in readings if _same(r["reference"], card.get("reference"))), None))
             for card in catalogue["items"]]
    pairs.extend((None, r) for r in readings if r["origin"] == "explicit-reference")
    for card, reading in pairs:
        owner = reading or card
        ref = owner.get("reference")
        stub = {"reference": deepcopy(ref), "case_id": owner.get("case_id", (card or {}).get("case_id")),
            "origin": reading["origin"] if reading else "catalogue-page",
            "status": "not-included", "error": "RESEARCH_BOOK_PANEL_BOUND_USE_ORIGINAL"}
        entry = {**stub, "status": owner["status"], "error": owner.get("error")}
        if reading and reading["status"] == "readable":
            projection = next((p for p in projections if _same(p["reference"], ref)), None)
            if projection is not None:
                entry = {**stub, **deepcopy(projection)}
            else:
                entry.update(status="not-included", error="RESEARCH_BOOK_PROJECTION_NOT_SUPPLIED")
        entry["brief_relation"] = deepcopy(reading.get("brief_relation")) if reading else {"status": "unknown"}
        cards = agenda.get("view", {}).get("cards", []) if agenda["status"] == "readable" else []
        entry["agenda_card_index"] = next((i for i, row in enumerate(cards) if _same(row.get("reference"), ref)), None)
        entries.append(stub)
        candidates.append(entry)
    result = {"schema": SCHEMA, "status": "readable", "catalogue": scope, "entries": entries,
        "explicit_versions": sum(r["origin"] == "explicit-reference" for r in readings),
        "coverage": "one-original-catalogue-page-plus-explicit-versions",
        "filter_scope": "this-page-only-not-global-search", "brief_reference": deepcopy(brief["reference"]),
        "brief_scope": "this-named-brief-only-not-all-adoption-history",
        "source_requests": 0, "extra_revision_reads": 0, "writes": 0,
        "human_acceptance": "not-established", "execution": "not-started"}
    if len(encoded(result)) > max_bytes:
        return {"schema": SCHEMA, "status": "not-included", "error": "RESEARCH_BOOK_PANEL_BOUND_USE_ORIGINAL"}
    for index, entry in enumerate(candidates):
        original = entries[index]
        entries[index] = entry
        if len(encoded(result)) > max_bytes:
            entries[index] = original
    if any(e["status"] != "readable" for e in entries):
        result["status"] = "partial"
    return result


def attach_book(result, projections, *, max_panel_bytes, max_view_bytes, render_model):
    """Never evict an original body/panel to fit an additional Book projection.

    Use the actual pure page renderer for its escaped HTML bound, not a second
    estimate of serialization expansion. Only its HTML capacity error omits
    Book; other errors still propagate. The final JSON and HTML share this model.
    At an already-full legacy view even a small omission marker may not fit.
    In that case the old model remains intact and the renderer explicitly says
    that no Book projection was included, retaining its original read actions.
    """
    book = book_panel(result["catalogue"], result["readings"], projections,
                      result["agenda"], result["brief"], max_bytes=max_panel_bytes)
    omitted = {"schema": SCHEMA, "status": "not-included", "error": "RESEARCH_BOOK_VIEW_BOUND_USE_ORIGINAL"}
    for candidate in (book, omitted):
        combined = {**result, "research_book": candidate}
        if len(encoded(combined)) <= max_view_bytes:
            try:
                render_model(combined)
            except RecordError as error:
                if str(error) != "ROOM_HTML_BOUND":
                    raise
                omitted["error"] = "RESEARCH_BOOK_HTML_BOUND_USE_ORIGINAL"
                continue
            result["research_book"] = candidate
            return
