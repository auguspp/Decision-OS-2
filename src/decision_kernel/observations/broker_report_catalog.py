"""Broker-report *index*, not document content or a forecast consensus.

Narrowly adapted from old Kernel broker_report_discovery.py@cb3bbc87:
fixed Eastmoney kind mapping, native report ID/version, date and page counters.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from hashlib import sha256
import json
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import SourceInput, RecordError, checked_text, require, source_time

KINDS = {
    "industry": ("list", "1", "zw_industry", None),
    "strategy": ("jg", "2", "zw_strategy", "002001002"),
    "macro": ("jg", "3", "zw_macresearch", "002001001"),
    "morning": ("jg", "4", "zw_macresearch", "002003001"),
}
PAGE_SIZE = 50
MAX_PAGES = 4
SHANGHAI = ZoneInfo("Asia/Shanghai")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_PUBLISH = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)?\Z")
_INFO = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


@dataclass(frozen=True)
class ReportScope:
    kind: str
    begin: str
    end: str
    max_pages: int = MAX_PAGES
    industry_code: str | None = None

    def __post_init__(self):
        require(self.kind in KINDS, "BROKER_KIND_INVALID")
        require(isinstance(self.begin, str) and isinstance(self.end, str)
                and bool(_DATE.fullmatch(self.begin)) and bool(_DATE.fullmatch(self.end)), "BROKER_DATE_INVALID")
        try:
            start, finish = date.fromisoformat(self.begin), date.fromisoformat(self.end)
        except ValueError as exc:
            raise RecordError("BROKER_DATE_INVALID") from exc
        require(start.isoformat() == self.begin and finish.isoformat() == self.end
                and 0 <= (finish - start).days <= 92, "BROKER_WINDOW_INVALID")
        require(type(self.max_pages) is int and 1 <= self.max_pages <= MAX_PAGES, "BROKER_PAGE_BUDGET")
        require(self.industry_code is None or self.kind == "industry"
                and isinstance(self.industry_code, str)
                and re.fullmatch(r"[0-9]{1,12}", self.industry_code) is not None,
                "BROKER_INDUSTRY_CODE_INVALID")


def request_spec(scope: ReportScope, page: int) -> tuple[str, dict[str, str]]:
    require(type(page) is int and 1 <= page <= scope.max_pages, "BROKER_PAGE_INVALID")
    route, qtype, _, _ = KINDS[scope.kind]
    params = {"pageSize": str(PAGE_SIZE), "pageNo": str(page), "beginTime": scope.begin,
              "endTime": scope.end, "qType": qtype, "fields": "", "industry": "*",
              "rating": "*", "ratingChange": "*", "orgCode": "", "rcode": ""}
    if scope.kind == "industry":
        params["industryCode"] = scope.industry_code or "*"
    return "https://reportapi.eastmoney.com/report/" + route, params


def _field(row: dict, key: str, limit: int = 2000) -> str | None:
    value = row.get(key)
    if value is None or value == "":
        return None
    require(isinstance(value, str) and 0 < len(value) <= limit and
            not any(ord(c) < 32 or ord(c) == 127 for c in value), "BROKER_FIELD_INVALID")
    return value


def _serialized(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise RecordError("BROKER_ROW_ENCODING_INVALID") from exc


@dataclass(frozen=True)
class ReportListing:
    report_key: str
    version_hash: str
    identity_kind: str
    identity_value: str
    title: str
    institution: str | None
    institution_code: str | None
    industry: str | None
    publication_raw: str
    publication_date: str
    type_status: str
    rating: str | None
    detail_url: str
    page_numbers: tuple[int, ...]


@dataclass(frozen=True)
class BrokerCatalog:
    scope: ReportScope
    captured_at: str
    source_mode: str
    provider_rows: int
    provider_pages: int
    duplicate_rows: int
    reports: tuple[ReportListing, ...]
    coverage: str = "REQUEST_WINDOW_COMPLETE_NOT_FULL_MARKET"


def _listing(row: object, scope: ReportScope, page_number: int, captured_day: date) -> ReportListing:
    require(isinstance(row, dict), "BROKER_ROW_INVALID")
    published = _field(row, "publishDate", 40)
    require(published is not None and bool(_PUBLISH.fullmatch(published)), "BROKER_PUBLICATION_DATE_INVALID")
    try:
        moment = datetime.fromisoformat(published.replace(" ", "T"))
    except ValueError as exc:
        raise RecordError("BROKER_PUBLICATION_DATE_INVALID") from exc
    require(scope.begin <= moment.date().isoformat() <= scope.end
            and moment.date() <= captured_day, "BROKER_PUBLICATION_OUTSIDE_WINDOW")
    info, token = _field(row, "infoCode", 80), _field(row, "encodeUrl", 1024)
    require(info is not None or token is not None, "BROKER_REPORT_ID_REQUIRED")
    require(info is None or bool(_INFO.fullmatch(info)), "BROKER_REPORT_ID_INVALID")
    if token is not None:
        require(len(token) <= 512 and not any(c in token for c in "?&#"), "BROKER_ENCODED_URL_INVALID")
    title = _field(row, "title", 1024)
    require(title is not None, "BROKER_TITLE_REQUIRED")
    column, prefix = _field(row, "column", 80), KINDS[scope.kind][3]
    status = ("REQUEST_ONLY_NOT_INDEPENDENTLY_VERIFIED" if prefix is None or column is None else
              "COLUMN_MATCH" if column.startswith(prefix) else "COLUMN_CONFLICT")
    if scope.industry_code is not None:
        require(row.get("industryCode") == scope.industry_code, "BROKER_INDUSTRY_FILTER_MISMATCH")
    ident_kind, ident_value = ("infoCode", info) if info else ("encodeUrl", token)
    report_key = sha256(_serialized(["EASTMONEY", ident_kind, ident_value])).hexdigest()
    row_hash = sha256(_serialized(row)).hexdigest()
    _, _, detail_route, _ = KINDS[scope.kind]
    detail = ("https://data.eastmoney.com/report/" + detail_route + ".jshtml?" +
              urlencode({"encodeUrl": token} if token else {"infocode": info}))
    return ReportListing(report_key, row_hash, ident_kind, ident_value, title,
                         _field(row, "orgSName", 200), _field(row, "orgCode", 80),
                         _field(row, "industryName", 200), published, published[:10], status,
                         _field(row, "ratingName", 100), detail, (page_number,))


def normalize_catalog(scope: ReportScope, *, pages: object, captured_at: str,
                      source_mode: str) -> BrokerCatalog:
    """Require every declared provider page before a complete catalog is saved.

    A response/page budget gap is an explicit failure, never zero research reports.
    """
    require(isinstance(scope, ReportScope), "BROKER_SCOPE_INVALID")
    source_time(captured_at)
    require(isinstance(captured_at, str) and "T" in captured_at and
            datetime.fromisoformat(captured_at.replace("Z", "+00:00")).utcoffset() is not None,
            "BROKER_CAPTURE_CLOCK_INVALID")
    capture_day = datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(SHANGHAI).date()
    require(date.fromisoformat(scope.end) <= capture_day, "BROKER_FUTURE_WINDOW")
    require(source_mode in ("offline-unverified", "https-live"), "BROKER_SOURCE_MODE_INVALID")
    require(isinstance(pages, list) and 1 <= len(pages) <= scope.max_pages, "BROKER_PAGES_INVALID")
    total: int | None = None
    provider_pages: int | None = None
    unique: dict[tuple[str, str], ReportListing] = {}
    raw_count = 0
    for number, page in enumerate(pages, start=1):
        require(isinstance(page, dict), "BROKER_PAGE_INVALID")
        rows = page.get("data")
        count, listed_pages, actual_number = page.get("hits"), page.get("TotalPage"), page.get("pageNo")
        require(page.get("success", True) is True and
                (page.get("code") is None or type(page["code"]) is int and page["code"] in (0, 200)),
                "BROKER_PROVIDER_FAILURE")
        require(type(count) is int and count >= 0 and type(listed_pages) is int and
                listed_pages >= 0 and type(actual_number) is int and actual_number == number,
                "BROKER_COUNTER_INVALID")
        expected_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
        require(listed_pages == (0 if count == 0 else expected_pages) or
                (count == 0 and listed_pages == 1), "BROKER_COUNTER_MISMATCH")
        require(expected_pages <= scope.max_pages, "BROKER_PAGE_BUDGET_EXCEEDED")
        require(total is None or (total == count and provider_pages == expected_pages), "BROKER_TOTAL_DRIFT")
        require(isinstance(rows, list) and len(rows) == min(PAGE_SIZE, max(0, count - (number-1)*PAGE_SIZE)),
                "BROKER_PAGE_INCOMPLETE")
        total, provider_pages = count, expected_pages
        for row in rows:
            item = _listing(row, scope, number, capture_day)
            raw_count += 1
            key = (item.report_key, item.version_hash)
            if key in unique:
                prev = unique[key]
                unique[key] = replace(prev, page_numbers=(*prev.page_numbers, number))
            else:
                unique[key] = item
    require(len(pages) == provider_pages and raw_count == total, "BROKER_CATALOG_INCOMPLETE")
    ordered = tuple(sorted(unique.values(), key=lambda x: (x.publication_date,x.report_key,x.version_hash),reverse=True))
    return BrokerCatalog(scope, captured_at, source_mode, total, provider_pages,
                         raw_count-len(ordered), ordered)


def source_for(report: ReportListing, catalog: BrokerCatalog) -> SourceInput:
    """Index locator only. Does not assert fetched/read broker PDF or analyst consensus."""
    require(report in catalog.reports, "BROKER_REPORT_OUTSIDE_CATALOG")
    require(report.type_status != "COLUMN_CONFLICT", "BROKER_TYPE_CONFLICT")
    source_id = "em-report-" + report.report_key[:28]
    limitations = (
        "BROKER_REPORT_DISCOVERY_ONLY: detail page has not been fetched; PDF body unavailable.",
        "SOURCE_MODE: " + catalog.source_mode,
        "REPORT_ID_KIND: " + report.identity_kind + "; REPORT_ID: " + report.identity_value,
        "REPORT_ROW_VERSION: " + report.version_hash,
        "PUBLICATION_RAW: " + report.publication_raw + "; time zone UNKNOWN",
        "REQUESTED_KIND: " + catalog.scope.kind + "; QUALIFICATION: " + report.type_status,
        "SOURCE_PAGE_NUMBERS: " + ",".join(map(str, report.page_numbers)),
        "CATALOG_WINDOW: " + catalog.scope.begin + "/" + catalog.scope.end,
        "ORIGINAL_PDF_NOT_READ; ANALYST_ESTIMATE_NOT_VERIFIED; HISTORICAL_AVAILABLE_AT_UNKNOWN",
    )
    return SourceInput(source_id=source_id, kind="content", title=report.title,
                       origin=report.detail_url, text=None, custody="external", entities=(),
                       published_at=report.publication_date, acquired_at=catalog.captured_at,
                       limitations=limitations)
