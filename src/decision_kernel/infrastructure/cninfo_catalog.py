"""Optional, explicitly invoked, bounded CNINFO announcement-directory acquisition.

Legacy input/form contract: runtime/cninfo_http.py@cb3bbc87. No PDF fetch,
provider fallback, proxy, cookies, retries, scheduling or persistent provider state.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, require
from decision_kernel.observations.cninfo_catalog import PAGE_SIZE, MAX_PAGES, normalize_catalog

ORG_SEARCH = "https://www.cninfo.com.cn/new/information/topSearch/query"
ANNOUNCEMENTS = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
MAX_RESPONSE = 256 * 1024


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OfficialCatalogTransport:
    """Only official fixed HTTPS form endpoints; no caller-supplied arbitrary URLs."""

    def __init__(self, *, opener=None):
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _RejectRedirect())
        self.requests = 0

    def __call__(self, url: str, form: dict[str, str]) -> object:
        require(url in (ORG_SEARCH, ANNOUNCEMENTS) and isinstance(form, dict),
                "CNINFO_REQUEST_NOT_ALLOWED")
        require(all(isinstance(k, str) and isinstance(v, str) and len(v) <= 200
                    for k, v in form.items()), "CNINFO_FORM_INVALID")
        self.requests += 1
        require(self.requests <= MAX_PAGES + 1, "CNINFO_REQUEST_LIMIT")
        request = Request(url, method="POST", data=urlencode(form).encode("ascii"), headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (compatible; DecisionOS2-public-catalog/0.1)",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "X-Requested-With": "XMLHttpRequest",
        })
        try:
            with self._opener.open(request, timeout=15) as response:
                if response.status != 200 or response.geturl() != url:
                    raise RecordError("CNINFO_RESPONSE_NOT_TRUSTED")
                encoding = response.headers.get("Content-Encoding", "identity")
                mime = response.headers.get("Content-Type", "application/json").split(";")[0].strip().lower()
                require(encoding in ("", "identity") and mime in (
                    "application/json", "text/json", "text/plain", "text/javascript"),
                    "CNINFO_RESPONSE_CONTENT_INVALID")
                raw = response.read(MAX_RESPONSE + 1)
                require(len(raw) <= MAX_RESPONSE, "CNINFO_RESPONSE_TOO_LARGE")
        except HTTPError as exc:
            # 403/429/redirect are explicit failures, not empty-page evidence or fallback approval.
            raise RecordError("CNINFO_HTTP_" + str(exc.code)) from None
        except (URLError, OSError, TimeoutError) as exc:
            raise RecordError("CNINFO_TRANSPORT_UNAVAILABLE") from None
        try:
            def unique(pairs):
                out = {}
                for key, val in pairs:
                    if key in out:
                        raise ValueError("duplicate key")
                    out[key] = val
                return out
            return json.loads(raw, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RecordError("CNINFO_JSON_INVALID") from None


def _form(code: str, org: str, start: str, end: str, page: int) -> dict[str, str]:
    return dict(pageSize=str(PAGE_SIZE), pageNum=str(page), column="", tabName="fulltext", plate="",
                stock=f"{code},{org}", searchkey="", secid="", category="", trade="",
                seDate=start + "~" + end, sortName="", sortType="", isHLtitle="true")


def fetch_catalog(*, stock_code: str, start_date: str, end_date: str,
                  transport=None, max_pages: int = MAX_PAGES):
    """Only explicit one-issuer one-window run; never hide partial pages as a finished batch."""
    # Precheck untrusted arguments before contacting the remote endpoint.
    from decision_kernel.observations.cninfo_catalog import _stock_code
    from datetime import date
    _stock_code(stock_code)
    try:
        d1, d2 = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise RecordError("CNINFO_DATE_WINDOW_INVALID") from exc
    require(d1.isoformat() == start_date and d2.isoformat() == end_date and d1 <= d2
            and (d2 - d1).days <= 31, "CNINFO_DATE_WINDOW_INVALID")
    require(type(max_pages) is int and 1 <= max_pages <= MAX_PAGES,
            "CNINFO_PAGE_BUDGET_INVALID")
    query = transport if transport is not None else OfficialCatalogTransport()
    org_rows = query(ORG_SEARCH, {"keyWord": stock_code, "maxNum": "10"})
    from decision_kernel.observations.cninfo_catalog import resolve_org
    org = resolve_org(org_rows, stock_code)
    pages = []
    for page in range(1, max_pages + 1):
        result = query(ANNOUNCEMENTS, _form(stock_code, org, start_date, end_date, page))
        require(isinstance(result, dict) and type(result.get("totalAnnouncement")) is int,
                "CNINFO_PAGE_INVALID")
        total = result["totalAnnouncement"]
        require(0 <= total <= PAGE_SIZE * max_pages, "CNINFO_TOTAL_OR_PAGE_LIMIT")
        pages.append(result)
        if page >= max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE):
            break
    return normalize_catalog(stock_code=stock_code, start_date=start_date, end_date=end_date,
                             captured_at=datetime.now(timezone.utc).isoformat(),
                             org_rows=org_rows, pages=pages, source_mode="https-live",
                             max_pages=max_pages)
