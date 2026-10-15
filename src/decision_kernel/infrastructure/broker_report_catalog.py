"""Explicit Eastmoney broker-report directory HTTP transport, without PDF fetching.

Fixed /list and /jg mappings from the earlier reviewed legacy broker discovery.
No source fallback, cookies, redirects, credentials, proxy or retry orchestration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, require
from decision_kernel.observations.broker_report_catalog import (
    ReportScope, PAGE_SIZE, normalize_catalog, request_spec,
)

MAX_BODY = 512 * 1024


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EastmoneyReportTransport:
    """Bound to reviewed provider endpoints and validated scope, not arbitrary URLs."""

    def __init__(self, *, opener=None):
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}), _RejectRedirect())
        self.requests = 0

    def __call__(self, spec: tuple[str, dict[str, str]]) -> object:
        url, params = spec
        require(url in ("https://reportapi.eastmoney.com/report/list",
                        "https://reportapi.eastmoney.com/report/jg")
                and isinstance(params, dict) and set(params) >= {"qType", "pageNo", "pageSize", "beginTime", "endTime"}
                and params["pageSize"] == str(PAGE_SIZE)
                and all(isinstance(k, str) and isinstance(v, str) and len(v) <= 256 for k,v in params.items()),
                "BROKER_REQUEST_NOT_ALLOWED")
        self.requests += 1
        require(self.requests <= 4, "BROKER_REQUEST_BUDGET")
        exact_url = url + "?" + urlencode(params)
        req = Request(exact_url, method="GET", headers={
            "Accept": "application/json", "Accept-Encoding": "identity",
            "User-Agent": "DecisionOS2-report-directory/0.1",
        })
        try:
            with self._opener.open(req, timeout=20) as response:
                require(response.status == 200 and response.geturl() == exact_url,
                        "BROKER_HTTP_RESPONSE_NOT_TRUSTED")
                mime = response.headers.get("Content-Type", "application/json").split(";")[0].strip().lower()
                require(mime in ("application/json", "text/json", "text/plain")
                        and response.headers.get("Content-Encoding", "identity").lower() in ("", "identity"),
                        "BROKER_HTTP_CONTENT_INVALID")
                raw = response.read(MAX_BODY+1)
                require(0 < len(raw) <= MAX_BODY, "BROKER_HTTP_SIZE_INVALID")
        except HTTPError as exc:
            raise RecordError("BROKER_HTTP_" + str(exc.code)) from None
        except (OSError, URLError, TimeoutError) as exc:
            raise RecordError("BROKER_HTTP_UNAVAILABLE") from None
        try:
            def unique(pairs):
                out = {}
                for key,v in pairs:
                    if key in out:
                        raise ValueError("duplicate key")
                    out[key] = v
                return out
            return json.loads(raw, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RecordError("BROKER_HTTP_JSON_INVALID") from None


def fetch_catalog(scope: ReportScope, *, transport=None):
    """Only an explicitly requested source-window. Reject partial pages; no automatic retry."""
    require(isinstance(scope, ReportScope), "BROKER_SCOPE_INVALID")
    own_transport = transport if transport is not None else EastmoneyReportTransport()
    pages=[]
    for i in range(1,scope.max_pages+1):
        page=own_transport(request_spec(scope,i))
        require(isinstance(page,dict) and type(page.get("hits")) is int and page["hits"]>=0,
                "BROKER_COUNTER_INVALID")
        pages.append(page)
        implied=max(1,(page["hits"]+PAGE_SIZE-1)//PAGE_SIZE)
        require(implied<=scope.max_pages,"BROKER_PAGE_BUDGET_EXCEEDED")
        if i>=implied:
            break
    return normalize_catalog(scope,pages=pages,captured_at=datetime.now(timezone.utc).isoformat(),
                             source_mode="https-live")
