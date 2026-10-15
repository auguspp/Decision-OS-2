"""Narrow port of old Kernel CNINFO org/page/date/locator contracts, not its runtime state.

Legacy evidence: adapters/cninfo.py and runtime/cninfo_http.py at
cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8. No original PDF is read here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from html import unescape
import re
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import Entity, RecordError, SourceInput, checked_text, require, source_time

SHANGHAI = ZoneInfo("Asia/Shanghai")
PAGE_SIZE = 30  # CNINFO's reported per-page maximum in the reviewed legacy contract.
MAX_PAGES = 4
_STOCK = re.compile(r"[0-9]{6}\Z")
_ORG = re.compile(r"[a-zA-Z0-9_-]{1,96}\Z")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
_PDF_PATH = re.compile(r"finalpage/[0-9]{4}-[0-9]{2}-[0-9]{2}/[a-zA-Z0-9_.-]+\.pdf\Z", re.I)
_EM_TAG = re.compile(r"</?em(?:\s+[^<>]*)?>", re.I)


@dataclass(frozen=True)
class Announcement:
    announcement_id: str
    stock_code: str
    org_id: str
    title: str
    announcement_type: str | None
    published_at: str  # Complete source timestamp with its original instant, displayed in Shanghai zone.
    pdf_locator: str  # Candidate original only: it has not been retrieved.
    page_number: int


@dataclass(frozen=True)
class Catalog:
    stock_code: str
    org_id: str
    start_date: str
    end_date: str
    captured_at: str
    source_mode: str
    page_size: int
    total: int
    announcements: tuple[Announcement, ...]


def _stock_code(value: str) -> str:
    require(isinstance(value, str) and bool(_STOCK.fullmatch(value)), "CNINFO_STOCK_CODE_REQUIRED")
    return value


def resolve_org(rows: object, code: str) -> str:
    """Never guess the issuer org ID from a ticker. Accept two observed response wrappers."""
    _stock_code(code)
    if isinstance(rows, dict) and set(rows) == {"stockList"}:
        rows = rows["stockList"]
    require(isinstance(rows, list) and 0 < len(rows) <= 100, "CNINFO_ORG_SEARCH_INVALID")
    found = set()
    for row in rows:
        require(isinstance(row, dict), "CNINFO_ORG_ROW_INVALID")
        stock, org = row.get("code"), row.get("orgId")
        require(isinstance(stock, str) and bool(_STOCK.fullmatch(stock)), "CNINFO_ORG_ROW_INVALID")
        require(isinstance(org, str) and bool(_ORG.fullmatch(org)), "CNINFO_ORG_ROW_INVALID")
        if stock == code:
            found.add(org)
    require(len(found) == 1, "CNINFO_ORG_NOT_UNIQUE_OR_MISSING")
    return found.pop()


def _pdf_url(value: object) -> str:
    require(isinstance(value, str) and 0 < len(value) <= 512, "CNINFO_PDF_LOCATOR_INVALID")
    if value.startswith("https://static.cninfo.com.cn/"):
        path = urlsplit(value).path.lstrip("/")
        parts = urlsplit(value)
        require(not parts.query and not parts.fragment and not parts.username and not parts.password,
                "CNINFO_PDF_LOCATOR_INVALID")
    else:
        require(not value.startswith("/") and "://" not in value, "CNINFO_PDF_LOCATOR_INVALID")
        path = value
    require(bool(_PDF_PATH.fullmatch(path)) and ".." not in path, "CNINFO_PDF_LOCATOR_INVALID")
    return "https://static.cninfo.com.cn/" + path


def _title(value: object) -> str:
    require(isinstance(value, str), "CNINFO_TITLE_REQUIRED")
    # Exact old CNINFO's highlighting tags are not document HTML or a PDF body.
    cleaned = unescape(_EM_TAG.sub("", value)).strip()
    require("<" not in cleaned and ">" not in cleaned, "CNINFO_TITLE_MARKUP_INVALID")
    return checked_text(cleaned, limit=4096)


def _source_timestamp(value: object, start: date, end: date) -> str:
    require(type(value) in (int, str) and str(value).isdigit(), "CNINFO_PUBLICATION_TIME_INVALID")
    try:
        number = int(value)
        # Milliseconds, not seconds, and no fabricated midnight for date-only information.
        require(10**11 <= number <= 4 * 10**12, "CNINFO_PUBLICATION_TIME_INVALID")
        observed = datetime.fromtimestamp(number / 1000, tz=SHANGHAI)
    except (ValueError, OverflowError, OSError) as exc:
        raise RecordError("CNINFO_PUBLICATION_TIME_INVALID") from exc
    require(start <= observed.date() <= end, "CNINFO_PUBLICATION_OUTSIDE_WINDOW")
    return observed.isoformat(timespec="milliseconds")


def normalize_catalog(*, stock_code: str, start_date: str, end_date: str,
                      captured_at: str, org_rows: object, pages: object,
                      source_mode: str, max_pages: int = MAX_PAGES) -> Catalog:
    """Reject partial pagination before permitting any record to be saved."""
    code = _stock_code(stock_code)
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise RecordError("CNINFO_DATE_WINDOW_INVALID") from exc
    require(start.isoformat() == start_date and end.isoformat() == end_date and start <= end
            and (end - start).days <= 31, "CNINFO_DATE_WINDOW_INVALID")
    source_time(captured_at)
    require(isinstance(captured_at, str) and "T" in captured_at
            and datetime.fromisoformat(captured_at).utcoffset() is not None,
            "CNINFO_CAPTURE_TIME_REQUIRED")
    require(source_mode in ("offline-unverified", "https-live"), "CNINFO_SOURCE_MODE_INVALID")
    require(type(max_pages) is int and 1 <= max_pages <= MAX_PAGES,
            "CNINFO_PAGE_BUDGET_INVALID")
    org = resolve_org(org_rows, code)
    require(isinstance(pages, list) and 1 <= len(pages) <= max_pages,
            "CNINFO_PAGES_INVALID")
    total = None
    items: list[Announcement] = []
    seen: set[str] = set()
    for page_number, page in enumerate(pages, start=1):
        require(isinstance(page, dict), "CNINFO_PAGE_INVALID")
        count, rows = page.get("totalAnnouncement"), page.get("announcements")
        require(type(count) is int and 0 <= count <= PAGE_SIZE * max_pages,
                "CNINFO_TOTAL_OR_PAGE_LIMIT")
        require(isinstance(rows, list), "CNINFO_PAGE_ROWS_INVALID")
        if total is None:
            total = count
        require(count == total, "CNINFO_TOTAL_CHANGED")
        required_rows = min(PAGE_SIZE, max(0, count - (page_number - 1) * PAGE_SIZE))
        require(len(rows) == required_rows, "CNINFO_PAGE_INCOMPLETE")
        for row in rows:
            require(isinstance(row, dict), "CNINFO_ROW_INVALID")
            ann_id = row.get("announcementId")
            if type(ann_id) is int:
                ann_id = str(ann_id)
            require(isinstance(ann_id, str) and bool(_ID.fullmatch(ann_id)),
                    "CNINFO_ANNOUNCEMENT_ID_INVALID")
            require(ann_id not in seen, "CNINFO_DUPLICATE_ANNOUNCEMENT")
            seen.add(ann_id)
            if row.get("secCode") is not None:
                require(str(row["secCode"]) == code, "CNINFO_FOREIGN_SECURITY")
            if row.get("orgId") is not None:
                require(str(row["orgId"]) == org, "CNINFO_FOREIGN_ORG")
            kind = row.get("announcementTypeName")
            require(kind is None or isinstance(kind, str), "CNINFO_TYPE_INVALID")
            items.append(Announcement(
                announcement_id=ann_id, stock_code=code, org_id=org,
                title=_title(row.get("announcementTitle")),
                announcement_type=checked_text(kind.strip(), limit=256) if kind and kind.strip() else None,
                published_at=_source_timestamp(row.get("announcementTime"), start, end),
                pdf_locator=_pdf_url(row.get("adjunctUrl")), page_number=page_number))
    assert total is not None
    expected_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    require(len(pages) == expected_pages and len(items) == total, "CNINFO_PAGINATION_INCOMPLETE")
    return Catalog(code, org, start_date, end_date, captured_at, source_mode,
                   PAGE_SIZE, total, tuple(items))


def source_for(entry: Announcement, catalog: Catalog) -> SourceInput:
    """Only an official-PDF *locator* is stored, never a pretended PDF or report body."""
    require(entry.stock_code == catalog.stock_code and entry.org_id == catalog.org_id,
            "CNINFO_CATALOG_IDENTITY_MISMATCH")
    source_id = "cninfo-" + entry.stock_code + "-" + sha256(entry.announcement_id.encode("ascii")).hexdigest()[:24]
    limitations = (
        "DISCOVERY_ONLY: CNINFO catalog metadata; original PDF not acquired or verified.",
        "CATALOG_PROVENANCE: " + catalog.source_mode,
        "ANNOUNCEMENT_ID: " + entry.announcement_id,
        "CATALOG_PAGE: " + str(entry.page_number) + "; complete batch " + str(catalog.total),
    )
    if entry.announcement_type:
        limitations += ("ANNOUNCEMENT_TYPE: " + entry.announcement_type,)
    return SourceInput(source_id=source_id, kind="content", title=entry.title,
                       origin=entry.pdf_locator, text=None, custody="external",
                       entities=(Entity("cn-a-" + entry.stock_code, "security", entry.stock_code),),
                       published_at=entry.published_at, acquired_at=catalog.captured_at,
                       limitations=limitations)
