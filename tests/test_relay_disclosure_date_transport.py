"""Synthetic transport responses; never contact Tushare Relay or recover a secret."""
from __future__ import annotations

import io
import json
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_disclosure_date import (
    FIELDS,HOST,RelayDisclosureTransport,fetch_disclosure_dates,
)

TICK="600598.SH"
PERIOD="20260930"


class Reply(io.BytesIO):
    def __init__(self,data,url,status=200,headers=None):
        super().__init__(data)
        self.status,self.url,self.headers=status,url,headers or {"Content-Type":"application/json"}
    def geturl(self):return self.url


class Opener:
    def __init__(self,reply):self.reply=reply;self.requests=[]
    def open(self,request,timeout):
        self.requests.append((request,timeout))
        if isinstance(self.reply,BaseException):raise self.reply
        return self.reply


def response():
    return json.dumps({"api_name":"disclosure_date","code":0,"data":{
        "fields":list(FIELDS),"items":[[TICK,"20260924",PERIOD,"20261029",None,None]]},"count":1}).encode()


def url():return HOST+"?"+urlencode({"ts_code":TICK,"end_date":PERIOD,"fields":",".join(FIELDS),"limit":"100"})


def test_exact_relay_host_and_header_no_body_or_key_echo():
    own=Opener(Reply(response(),url()))
    t=RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")
    value=t(TICK,PERIOD,100)
    assert value["count"]==1
    request,timeout=own.requests[0]
    assert request.full_url==url() and timeout==30 and request.get_method()=="GET"
    assert request.get_header("X-api-key")=="synthetic-secret-123"
    assert "synthetic-secret-123" not in json.dumps(value)
    assert "Authorization" not in request.headers and "Cookie" not in request.headers
    assert t.last_response_meta["http_status"]==200
    assert len(t.last_response_meta["wire_sha256"])==64


def test_secret_absent_does_not_contact_relay(monkeypatch):
    monkeypatch.delenv("TUSHARE_PROXY_API_KEY",raising=False)
    own=Opener(Reply(response(),url()))
    with pytest.raises(RecordError,match="RELAY_CREDENTIAL_REQUIRED"):
        RelayDisclosureTransport(opener=own)(TICK,PERIOD,100)
    assert not own.requests


@pytest.mark.parametrize("status",[401,403,429,500,503])
def test_http_failure_does_not_retry_or_claim_empty(status):
    own=Opener(HTTPError(url(),status,"private",{},io.BytesIO(b"private")))
    t=RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")
    with pytest.raises(RecordError,match="RELAY_HTTP_"+str(status)):
        t(TICK,PERIOD,100)
    assert len(own.requests)==1


def test_wrong_target_redirect_rejected_not_sent_twice():
    own=Opener(Reply(response(),"https://evil.example/hijacked"))
    with pytest.raises(RecordError,match="RELAY_HTTP_RESPONSE_INVALID"):
        RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")(TICK,PERIOD,100)
    assert len(own.requests)==1


@pytest.mark.parametrize("raw,headers,error",[
    (b"not-json",None,"RELAY_JSON_INVALID"),
    (b'{"x":1,"x":2}',None,"RELAY_JSON_INVALID"),
    (b'{"x":NaN}',None,"RELAY_JSON_INVALID"),
    (b" "*(512*1024+1),None,"RELAY_RESPONSE_BYTES_INVALID"),
    (response(),{"Content-Type":"text/html"},"RELAY_CONTENT_TYPE_INVALID"),
    (response(),{"Content-Encoding":"gzip"},"RELAY_CONTENT_ENCODING_INVALID"),
])
def test_untrusted_response_rejected(raw,headers,error):
    own=Opener(Reply(raw,url(),headers=headers))
    with pytest.raises(RecordError,match=error):
        RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")(TICK,PERIOD,100)


def test_replay_is_single_attempt_and_not_a_provider_router():
    own=Opener(URLError("upstream busy"))
    t=RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")
    with pytest.raises(RecordError,match="RELAY_HTTP_UNAVAILABLE"):t(TICK,PERIOD,100)
    assert len(own.requests)==1
    with pytest.raises(RecordError,match="RELAY_ONE_REQUEST_PER_OPERATION"):
        t(TICK,PERIOD,100)


def test_explicit_normalization_wrapper_with_mocked_relay_value():
    called=[]
    def fake(ticker,period,limit):
        called.append((ticker,period,limit));return json.loads(response())
    v=fetch_disclosure_dates(ts_code=TICK,end_date=PERIOD,transport=fake)
    assert called==[(TICK,PERIOD,100)]
    assert v.rows[0].pre_date["value"]=="2026-10-29"
    assert v.source_mode=="live-relay"


def test_live_response_retains_safe_headers_and_wire_identity():
    headers={"Content-Type":"application/json", "X-Request-ID":"request-001", "X-Cache":"MISS"}
    own=Opener(Reply(response(),url(),headers=headers))
    t=RelayDisclosureTransport(opener=own,credential="synthetic-secret-123")
    actual=fetch_disclosure_dates(ts_code=TICK,end_date=PERIOD,transport=t)
    assert actual.response_transport["x_request_id"]=="request-001"
    assert actual.response_transport["x_cache"]=="MISS"
    assert actual.response_transport["wire_sha256"]
    assert actual.parsed_response_sha256
