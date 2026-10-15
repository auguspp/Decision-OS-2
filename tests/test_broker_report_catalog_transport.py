"""Fixed HTTPS acquisition lifecycle; no actual broker provider traffic in tests."""
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.broker_report_catalog import EastmoneyReportTransport, fetch_catalog
from decision_kernel.observations.broker_report_catalog import ReportScope, request_spec


class Reply(io.BytesIO):
    def __init__(self,content,url,status=200,headers=None):
        super().__init__(content)
        self.status,self.url,self.headers=status,url,headers or {"Content-Type":"application/json"}
    def geturl(self):return self.url


class Opener:
    def __init__(self,response):self.response=response;self.calls=[]
    def open(self,req,timeout):
        self.calls.append((req,timeout))
        if isinstance(self.response,BaseException):raise self.response
        return self.response


def content(rows=None,total=0,number=1):
    return json.dumps({"success":True,"code":0,"hits":total,"TotalPage":(total+49)//50,
                       "pageNo":number,"data":rows or []}).encode()


def scope():return ReportScope("industry","2026-10-01","2026-10-12")


def test_native_exact_https_request_lifecycle():
    spec=request_spec(scope(),1)
    from urllib.parse import urlencode
    url=spec[0]+"?"+urlencode(spec[1])
    opener=Opener(Reply(content(),url))
    transport=EastmoneyReportTransport(opener=opener)
    result=transport(spec)
    assert result["hits"]==0 and transport.requests==1
    request,timeout=opener.calls[0]
    assert request.full_url==url and request.get_method()=="GET" and timeout==20
    assert "Authorization" not in request.headers and "Cookie" not in request.headers


@pytest.mark.parametrize("code,error",[(401,"BROKER_HTTP_401"),(403,"BROKER_HTTP_403"),
                                      (429,"BROKER_HTTP_429"),(503,"BROKER_HTTP_503")])
def test_source_denial_is_not_empty_or_retry(code,error):
    opener=Opener(HTTPError("",code,"upstream private",{},io.BytesIO(b"private")))
    with pytest.raises(RecordError,match=error):EastmoneyReportTransport(opener=opener)(request_spec(scope(),1))
    assert len(opener.calls)==1


@pytest.mark.parametrize("raw,error",[(b"not-json","BROKER_HTTP_JSON_INVALID"),
                                    (b'{"x":1,"x":2}',"BROKER_HTTP_JSON_INVALID"),
                                    (b'{"x":NaN}',"BROKER_HTTP_JSON_INVALID"),
                                    (b" "*(512*1024+1),"BROKER_HTTP_SIZE_INVALID")])
def test_response_failures_stay_failures(raw,error):
    spec=request_spec(scope(),1)
    from urllib.parse import urlencode
    url=spec[0]+"?"+urlencode(spec[1])
    with pytest.raises(RecordError,match=error):
        EastmoneyReportTransport(opener=Opener(Reply(raw,url)))(spec)


def test_wrong_response_url_or_html_refused():
    spec=request_spec(scope(),1)
    with pytest.raises(RecordError,match="BROKER_HTTP_RESPONSE_NOT_TRUSTED"):
        EastmoneyReportTransport(opener=Opener(Reply(content(),"https://evil.example/")))(spec)
    from urllib.parse import urlencode
    url=spec[0]+"?"+urlencode(spec[1])
    with pytest.raises(RecordError,match="BROKER_HTTP_CONTENT_INVALID"):
        EastmoneyReportTransport(opener=Opener(Reply(content(),url,headers={"Content-Type":"text/html"})))(spec)


def test_arbitrary_host_blocked_before_http():
    opener=Opener(Reply(content(),"https://evil.example"))
    with pytest.raises(RecordError,match="BROKER_REQUEST_NOT_ALLOWED"):
        EastmoneyReportTransport(opener=opener)(("https://evil.example",{"pageSize":"50","pageNo":"1","qType":"1","beginTime":"2026-10-01","endTime":"2026-10-12"}))
    assert not opener.calls


def test_fetch_catalog_finite_successful_empty_and_late_page_failure():
    count=0
    def transport(spec):
        nonlocal count
        count+=1
        assert spec[0]=="https://reportapi.eastmoney.com/report/list"
        return json.loads(content())
    c=fetch_catalog(scope(),transport=transport)
    assert count==1 and c.provider_rows==0
    assert c.source_mode=="https-live"


def test_fetch_catalog_cannot_hide_insufficient_page_budget():
    def fetch(spec):
        return {"hits":51,"TotalPage":2,"pageNo":1,"data":[{"a":1}]*50}
    with pytest.raises(RecordError,match="BROKER_PAGE_BUDGET_EXCEEDED"):
        fetch_catalog(ReportScope("industry","2026-10-01","2026-10-12",max_pages=1),transport=fetch)


def test_transport_unavailable_preserves_reason_without_retry():
    opener=Opener(URLError("private uri"))
    with pytest.raises(RecordError,match="BROKER_HTTP_UNAVAILABLE"):
        EastmoneyReportTransport(opener=opener)(request_spec(scope(),1))
    assert len(opener.calls)==1
