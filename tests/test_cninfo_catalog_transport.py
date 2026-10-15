"""Only modeled HTTP responses; never claim live CNINFO provider acceptance."""
import json
from datetime import datetime
from urllib.error import HTTPError

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.cninfo_catalog import (
    ANNOUNCEMENTS, ORG_SEARCH, MAX_RESPONSE, OfficialCatalogTransport, fetch_catalog,
)
from test_cninfo_catalog import CODE, ORG, START, END, row


class FakeResponse:
    status = 200
    def __init__(self, url, body=b'{}', headers=None):
        self.url, self.body, self.headers = url, body, headers or {"Content-Type":"application/json"}
    def geturl(self): return self.url
    def read(self, limit): return self.body[:limit]
    def __enter__(self): return self
    def __exit__(self, *args): return None


class FakeOpener:
    def __init__(self, fake): self.fake, self.calls = fake, []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.fake, BaseException):
            raise self.fake
        return self.fake


def test_live_capture_uses_old_cninfo_org_and_page_contract_without_provider_fallback():
    calls = []
    def request(url, form):
        calls.append((url, form))
        if url == ORG_SEARCH:
            return [{"code":CODE,"orgId":ORG}]
        assert url == ANNOUNCEMENTS and form["stock"] == CODE + "," + ORG
        assert form["seDate"] == START + "~" + END and form["tabName"] == "fulltext"
        if form["pageNum"] == "1":
            return {"totalAnnouncement":31, "announcements":[row(str(i)) for i in range(100,130)]}
        return {"totalAnnouncement":31, "announcements":[row("130")]}
    cat = fetch_catalog(stock_code=CODE, start_date=START, end_date=END, transport=request)
    assert cat.source_mode == "https-live" and cat.total == 31
    assert cat.announcements[-1].page_number == 2 and len(calls) == 3
    assert all(uri.startswith("https://www.cninfo.com.cn/") for uri,_ in calls)


def test_live_query_failure_does_not_turn_into_empty_result_or_fallback():
    calls = []
    def fail(url, form):
        calls.append(url)
        if url == ORG_SEARCH: return [{"code":CODE,"orgId":ORG}]
        raise RecordError("CNINFO_HTTP_403")
    with pytest.raises(RecordError, match="CNINFO_HTTP_403"):
        fetch_catalog(stock_code=CODE, start_date=START, end_date=END, transport=fail)
    assert calls == [ORG_SEARCH, ANNOUNCEMENTS]


def test_live_noncomplete_source_stops_after_page_budget_and_no_report():
    def request(url, form):
        if url == ORG_SEARCH: return [{"code":CODE,"orgId":ORG}]
        return {"totalAnnouncement":150, "announcements":[]}
    with pytest.raises(RecordError, match="CNINFO_TOTAL_OR_PAGE_LIMIT"):
        fetch_catalog(stock_code=CODE, start_date=START, end_date=END, transport=request)


def test_untrusted_url_oversize_and_redirect_are_rejected_in_transport():
    opener = FakeOpener(FakeResponse(ORG_SEARCH, json.dumps([{"code": CODE,"orgId":ORG}]).encode()))
    transport = OfficialCatalogTransport(opener=opener)
    assert transport(ORG_SEARCH, {"keyWord":CODE}) == [{"code":CODE,"orgId":ORG}]
    assert opener.calls[0][0].method == "POST" and opener.calls[0][1] == 15
    assert opener.calls[0][0].get_header("Accept-encoding") == "identity"
    with pytest.raises(RecordError, match="CNINFO_REQUEST_NOT_ALLOWED"):
        transport("https://evil.example/", {"keyWord":CODE})
    opener.fake = FakeResponse("https://evil.example/", b"{}")
    with pytest.raises(RecordError, match="CNINFO_RESPONSE_NOT_TRUSTED"):
        transport(ORG_SEARCH, {"keyWord":CODE})
    opener.fake = FakeResponse(ORG_SEARCH, b"a" * (MAX_RESPONSE + 1))
    with pytest.raises(RecordError, match="CNINFO_RESPONSE_TOO_LARGE"):
        transport(ORG_SEARCH, {"keyWord":CODE})


def test_malformed_response_and_explicit_denial_keep_bounded_errors():
    opener = FakeOpener(FakeResponse(ORG_SEARCH, b'{"x":1,"x":2}'))
    http = OfficialCatalogTransport(opener=opener)
    with pytest.raises(RecordError, match="CNINFO_JSON_INVALID"):
        http(ORG_SEARCH, {})
    opener.fake = HTTPError(ORG_SEARCH, 403, "sensitive upstream body", {}, None)
    with pytest.raises(RecordError, match="CNINFO_HTTP_403"):
        http(ORG_SEARCH, {})
