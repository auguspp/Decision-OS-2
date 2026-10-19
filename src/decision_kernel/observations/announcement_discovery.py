"""Unseeded announcement metadata. A title is a lead, never verified earnings.

Reuse CNINFO's reviewed per-row identity, time, HTML and locator contracts.
Partial pagination remains useful but cannot become a complete market scan.
"""
from __future__ import annotations

from datetime import date, datetime
import re

from decision_kernel.domain.records import RecordError, checked_text, digest, encoded, require
from decision_kernel.observations.cninfo_catalog import PAGE_SIZE, MAX_PAGES, _pdf_url, _source_timestamp, _stock_code, _title

SCHEMA = "announcement-discovery-v1"
TERMS = ("业绩", "报告", "合同", "中标", "投资", "风险", "更正")


def window(start, end):
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (TypeError, ValueError):
        raise RecordError("DISCOVERY_WINDOW_INVALID") from None
    require(first.isoformat() == start and last.isoformat() == end and 0 <= (last-first).days <= 3,
            "DISCOVERY_WINDOW_INVALID")
    return first, last


def observed_clock(value):
    try:
        at = datetime.fromisoformat(value)
        require("T" in value and at.tzinfo is not None, "DISCOVERY_CLOCK_INVALID")
        return at
    except (TypeError, ValueError):
        raise RecordError("DISCOVERY_CLOCK_INVALID") from None


def query_form(term, start, end, page):
    window(start, end)
    require(term in TERMS and type(page) is int and 1 <= page <= MAX_PAGES, "DISCOVERY_QUERY_INVALID")
    return dict(pageSize=str(PAGE_SIZE), pageNum=str(page), column="", tabName="fulltext", plate="",
                stock="", searchkey=term, secid="", category="", trade="", seDate=start+"~"+end,
                sortName="", sortType="", isHLtitle="true")


def normalize_row(raw, start, end):
    require(isinstance(raw, dict), "DISCOVERY_ROW_INVALID")
    code = _stock_code(raw.get("secCode"))
    ann_id = str(raw.get("announcementId", ""))
    org = raw.get("orgId")
    require(re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", ann_id) is not None and
            isinstance(org, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,96}", org) is not None,
            "DISCOVERY_ID_INVALID")
    first, last = window(start, end)
    row = {"announcement_id": ann_id, "stock_code": code, "org_id": org,
           "title": _title(raw.get("announcementTitle")),
           "published_at_claim": _source_timestamp(raw.get("announcementTime"), first, last),
           "original_locator": _pdf_url(raw.get("adjunctUrl")),
           "issuer_name": checked_text(raw.get("secName", code), limit=1024)}
    row["version_id"] = digest(encoded(row))
    return row


def normalize_query(term, pages, *, start, end, error=None):
    require(term in TERMS and isinstance(pages, list) and len(pages) <= MAX_PAGES, "DISCOVERY_PAGES_INVALID")
    window(start, end)
    result = {"term": term, "stock_filter": None, "advertised_total": None, "pages_read": len(pages),
              "rows_read": 0, "rejected_rows": 0, "duplicate_rows": 0, "entries": [],
              "complete_query": False, "gaps": [], "status": "unavailable"}
    seen = set()
    for number, page in enumerate(pages, 1):
        require(isinstance(page, dict), "DISCOVERY_PAGE_INVALID")
        total, rows = page.get("totalAnnouncement"), page.get("announcements")
        require(type(total) is int and total >= 0, "DISCOVERY_TOTAL_INVALID")
        # CNINFO may represent a genuinely empty page as null; only total=0 permits it.
        if rows is None and total == 0:
            rows = []
        require(isinstance(rows, list) and len(rows) <= PAGE_SIZE, "DISCOVERY_PAGE_INVALID")
        if result["advertised_total"] is None:
            result["advertised_total"] = total
        elif result["advertised_total"] != total:
            result["gaps"].append("TOTAL_CHANGED_DURING_PAGING")
        if len(rows) != min(PAGE_SIZE, max(0, total - (number-1)*PAGE_SIZE)):
            result["gaps"].append("SHORT_OR_OVERLAPPING_PAGE")
        result["rows_read"] += len(rows)
        for raw in rows:
            try:
                row = normalize_row(raw, start, end)
            except RecordError:
                result["rejected_rows"] += 1
                continue
            if row["version_id"] in seen:
                result["duplicate_rows"] += 1
            else:
                seen.add(row["version_id"])
                result["entries"].append(row)
    if error:
        result["gaps"].append(error)
    if result["rejected_rows"]:
        result["gaps"].append("INVALID_ROWS_RETAINED_AS_GAP")
    if result["duplicate_rows"]:
        result["gaps"].append("DUPLICATE_ROWS_WITHIN_QUERY")
    total = result["advertised_total"]
    if total is not None and result["rows_read"] != total:
        result["gaps"].append("PAGINATION_INCOMPLETE")
    result["gaps"] = list(dict.fromkeys(result["gaps"]))
    result["complete_query"] = total is not None and not result["gaps"]
    result["status"] = "complete" if result["complete_query"] else "partial" if pages else "unavailable"
    return result


def assemble(queries, *, start, end, acquired_at):
    window(start, end); observed_clock(acquired_at)
    require(isinstance(queries, list) and [q["term"] for q in queries] == list(TERMS), "DISCOVERY_QUERY_SET_INVALID")
    entries = {}
    for query in queries:
        for row in query["entries"]:
            item = entries.setdefault(row["version_id"], {**row, "matched_terms": []})
            item["matched_terms"].append(query["term"])
    # Sorting is presentation only; no profit, investment score, or ticker whitelist.
    ordered = sorted(entries.values(), key=lambda r: (r["published_at_claim"], r["version_id"]), reverse=True)
    return {"schema": SCHEMA, "start_date": start, "end_date": end, "acquired_at": acquired_at,
            "source": "cninfo-title-search", "terms": list(TERMS), "stock_filter": None,
            "entries": ordered, "queries": [{k: v for k, v in q.items() if k != "entries"} for q in queries],
            "complete_market_coverage": False, "all_declared_queries_complete": all(q["complete_query"] for q in queries),
            "unique_versions": len(ordered), "originals_read": False, "research_executed": False,
            "meaning": "KEYWORD_DIRECTORY_LEADS_NOT_VERIFIED_EVENTS_OR_FULL_MARKET",
            "limitations": ["Keyword selection can miss announcements; no exhaustive recall claim.",
                "Publication is the directory's timestamp claim, not independently verified publication.",
                "Multiple search terms share one upstream; matched terms are not independent evidence."]}


def compare_versions(current, previous=None):
    """A new capture clock is not a new event. A changed version is not new profit."""
    require(current.get("schema") == SCHEMA, "DISCOVERY_SCHEMA_INVALID")
    require(previous is None or previous.get("schema") == SCHEMA, "DISCOVERY_SCHEMA_INVALID")
    known = {r["version_id"] for r in previous["entries"]} if previous else set()
    identities = {(r["stock_code"], r["announcement_id"]) for r in previous["entries"]} if previous else set()
    return [{**row, "increment": "baseline-unknown" if previous is None else
             "same-version" if row["version_id"] in known else
             "changed-metadata-version" if (row["stock_code"], row["announcement_id"]) in identities else
             "new-to-compared-window"} for row in current["entries"]]
