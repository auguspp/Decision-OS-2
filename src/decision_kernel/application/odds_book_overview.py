"""Compose only the original pages actually read in this bounded invocation.

The native Book owns custody, candidate discovery and every stored field. This
adapter keeps its five-field query and pages intact; it has no page-import API,
transport, catalogue, calculation or persistence of its own. The surrounding
Room owns the shared component scope and its operation-wide terminal latch.
"""
from copy import deepcopy

from decision_kernel.application import odds_book
from decision_kernel.domain.records import SHA, StoreUnavailable, require

SCHEMA = "odds-book-overview-v1"
MAX_PAGES = 4


def validate_page_limit(value=1):
    """Pure Room preflight, independent of the original single-page query."""
    require(type(value) is int and 1 <= value <= MAX_PAGES,
            "ODDS_BOOK_OVERVIEW_PAGE_LIMIT")
    return value


def _consistent(condition):
    if not condition:
        # An inconsistent native page is an operation failure, never a partial
        # successful table assembled from the preceding pages.
        raise StoreUnavailable("ODDS_BOOK_OVERVIEW_PAGE_MISMATCH")


def _check_page(page, request, repository, *, previous, seen_paths):
    """Check the composition boundary without repeating native record checks."""
    _consistent(isinstance(page, dict) and page.get("schema") == "odds-book-v1"
                and page.get("repository") == repository
                and isinstance(page.get("snapshot"), str)
                and SHA.fullmatch(page["snapshot"]) is not None
                and page.get("coverage") == "this-page-only")
    _consistent(request["snapshot"] in (None, page["snapshot"]))
    scan = page.get("scan")
    _consistent(isinstance(scan, dict) and set(scan) == {
        "start", "end", "total_candidates", "at_end"})
    _consistent(all(type(scan[key]) is int for key in ("start", "end", "total_candidates"))
                and type(scan["at_end"]) is bool
                and 0 <= scan["start"] <= scan["end"] <= scan["total_candidates"] <= odds_book.MAX_CANDIDATES
                and scan["at_end"] == (scan["end"] == scan["total_candidates"]))
    expected_start = request["cursor"]["offset"] if request["cursor"] is not None else 0
    _consistent(scan["start"] == expected_start
                and scan["end"] - scan["start"] <= request["scan_limit"]
                and (scan["end"] > scan["start"] or scan["at_end"]))
    if previous is not None:
        _consistent(page["snapshot"] == previous["snapshot"]
                    and scan["total_candidates"] == previous["scan"]["total_candidates"]
                    and scan["start"] == previous["scan"]["end"])
    items = page.get("items")
    _consistent(isinstance(items, list)
                and len(items) <= min(request["limit"], scan["end"] - scan["start"]))
    for row in items:
        _consistent(isinstance(row, dict) and isinstance(row.get("path"), str)
                    and row["path"] not in seen_paths
                    and row.get("status") in {"readable", "missing", "invalid", "not-included"})
        seen_paths.add(row["path"])
        if row["status"] == "readable" and request["research_ref"] is not None:
            _consistent(row.get("research_key") == odds_book._key(request["research_ref"]))
    cursor = page.get("next_cursor")
    if scan["at_end"]:
        _consistent(cursor is None)
    else:
        _consistent(isinstance(cursor, dict) and cursor == {
            "schema": odds_book.CURSOR_SCHEMA, "repository": repository,
            "snapshot": page["snapshot"], "offset": scan["end"],
            "research_ref": request["research_ref"]})


def _relations(pages, *, absence_checked):
    # Reuse the native exact-key relationship projection, then change only the
    # labels describing this invocation's wider proven range. Original pages,
    # stored records, prices and original-reference aliases remain untouched.
    groups = deepcopy(odds_book._relations(
        [row for page in pages for row in page["items"]], False))
    for group in groups:
        group["coverage"] = "this-read-range-only"
        if group["saved_odds"] == "present-in-this-page-not-current-certification":
            group["saved_odds"] = "present-in-this-read-not-current-certification"
        else:
            group["saved_odds"] = ("not-found-in-complete-scan-of-known-layouts" if absence_checked
                                   else "not-observed-in-this-read")
        for spec in group["spec_groups"]:
            for result in spec["results"]:
                result["comparison_scope"] = "same-spec-saved-results-in-this-read-not-latest-price"
    return groups


def read_overview(store, query, *, page_limit=1):
    """Read up to four contiguous native pages on the very same store/backend.

    A returned cursor is only a position. A nonzero starting cursor leaves an
    explicit unread prefix even when this invocation reaches the final page.
    Native failures propagate before any aggregate is returned; no later page
    is attempted after a failed call. Caller-supplied receipts are not input.
    """
    count = validate_page_limit(page_limit)
    original = odds_book.validate_plan(query)
    request, pages, seen_paths = deepcopy(original), [], set()
    repository = store.backend.repository
    for _ in range(count):
        page = odds_book.read_book(store, request)
        _check_page(page, request, repository,
                    previous=pages[-1] if pages else None, seen_paths=seen_paths)
        pages.append(page)
        if page["next_cursor"] is None:
            break
        request = {**deepcopy(original), "snapshot": page["snapshot"],
                   "cursor": deepcopy(page["next_cursor"])}
    first, last = pages[0], pages[-1]
    start, end, total = first["scan"]["start"], last["scan"]["end"], first["scan"]["total_candidates"]
    statuses = [row["status"] for page in pages for row in page["items"]]
    failures = sum(status in ("missing", "invalid") for status in statuses)
    omitted = statuses.count("not-included")
    scan_complete = start == 0 and end == total
    content_complete = failures == 0 and omitted == 0
    pinned_query = {**deepcopy(original), "snapshot": first["snapshot"]}
    return {
        "schema": SCHEMA, "repository": first["repository"], "snapshot": first["snapshot"],
        "query": pinned_query, "page_limit": count, "pages": pages,
        "research_relations": _relations(pages, absence_checked=scan_complete and content_complete),
        "scan": {"start": start, "end": end, "total_candidates": total,
                 "at_end": end == total, "complete_from_start": scan_complete,
                 "unread_prefix": start, "unread_tail": total - end},
        "counts": {"pages": len(pages), "scanned_candidates": end - start,
                   "returned_items": len(statuses), "filtered_candidates": end - start - len(statuses),
                   "readable_items": statuses.count("readable"), "failed_items": failures,
                   "omitted_items": omitted},
        "content_complete": content_complete,
        "status": "readable" if scan_complete and content_complete else "partial",
        "coverage": "this-read-range-only", "order": "stored-path-not-priority-or-recency",
        "next_cursor": deepcopy(last["next_cursor"]),
        "semantic_replay": "not-requested", "current_validity": "not-certified",
        "human_acceptance": "not-established-by-read", "investment_authority": "none",
        "persistence": "not-written", "source_acquisition": "not-requested",
    }
