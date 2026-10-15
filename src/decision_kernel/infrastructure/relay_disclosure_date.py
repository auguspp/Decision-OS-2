"""Single bounded third-party Relay disclosure_date request, no source router.

Uses exact previously reviewed Relay host/headers. Never obtains or logs API key
from the response body, never tries other providers on error.
"""
from __future__ import annotations

from datetime import datetime,timezone
import json
import os
from hashlib import sha256
import re
from http.client import HTTPException
from urllib.error import HTTPError,URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener

from decision_kernel.domain.records import RecordError,require
from decision_kernel.observations.relay_disclosure_date import (
    FIELDS,MAX_ROWS,normalize_disclosure_dates,ticker,report_period,
)

HOST="https://pcd.mobcvb.cn/tushare/pro/disclosure_date"
SECRET_ENV="TUSHARE_PROXY_API_KEY"
MAX_BODY=512*1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,url):return None


class RelayDisclosureTransport:
    """Actual transport requires the authorised runtime secret, not a model guess."""

    def __init__(self,*,opener=None,credential=None):
        self._opener=opener if opener is not None else build_opener(ProxyHandler({}),_NoRedirect())
        self._credential=credential if credential is not None else os.environ.get(SECRET_ENV,"")
        self.requests=0
        self.last_response_meta=None

    def __call__(self,ts_code:str,end_date:str,limit:int)->dict:
        ticker(ts_code);report_period(end_date)
        require(type(limit) is int and 1<=limit<=MAX_ROWS,"RELAY_ROW_BUDGET_INVALID")
        key=self._credential
        require(isinstance(key,str) and 8<=len(key)<=4096 and key.isascii()
                and all(33<=ord(c)<=126 for c in key),"RELAY_CREDENTIAL_REQUIRED")
        require(self.requests==0,"RELAY_ONE_REQUEST_PER_OPERATION")
        self.requests+=1
        parameters={"ts_code":ts_code,"end_date":end_date,"fields":",".join(FIELDS),"limit":str(limit)}
        exact=HOST+"?"+urlencode(parameters)
        request=Request(exact,method="GET",headers={
            "X-API-Key":key,"Accept":"application/json","Accept-Encoding":"identity",
            "User-Agent":"DecisionOS2-RelayDisclosureDate/0.1",
        })
        try:
            with self._opener.open(request,timeout=30) as resp:
                require(resp.status==200 and resp.geturl()==exact,"RELAY_HTTP_RESPONSE_INVALID")
                require(resp.headers.get("Content-Encoding","identity").lower() in ("","identity"),
                        "RELAY_CONTENT_ENCODING_INVALID")
                mime=resp.headers.get("Content-Type","application/json").split(";")[0].strip().lower()
                require(mime in ("application/json","text/json","text/plain"),"RELAY_CONTENT_TYPE_INVALID")
                length = resp.headers.get("Content-Length")
                require(length is None or isinstance(length, str) and len(length) <= 12
                        and length.isascii() and length.isdigit() and int(length) <= MAX_BODY,
                        "RELAY_RESPONSE_BYTES_INVALID")
                raw=resp.read(MAX_BODY+1)
                require(length is None or len(raw) == int(length), "RELAY_RESPONSE_BYTES_INVALID")
                require(0<len(raw)<=MAX_BODY,"RELAY_RESPONSE_BYTES_INVALID")
                def safe_header(name):
                    value=resp.headers.get(name)
                    return (value if isinstance(value,str) and
                            re.fullmatch(r"[A-Za-z0-9._-]{1,128}",value) and key not in value else None)
                metadata={"http_status":200,
                    "x_request_id":safe_header("X-Request-ID"),
                    "x_cache":safe_header("X-Cache"),
                    "wire_sha256":sha256(raw).hexdigest()}
        except HTTPError as exc:
            exc.close()
            raise RecordError("RELAY_HTTP_"+str(exc.code)) from None
        except (URLError,OSError,TimeoutError,HTTPException) as exc:
            raise RecordError("RELAY_HTTP_UNAVAILABLE") from None
        require(key.encode("ascii") not in raw,"RELAY_SECRET_REFLECTED")
        try:
            def unique(pairs):
                result={}
                for k,v in pairs:
                    if k in result:raise ValueError("duplicate")
                    result[k]=v
                return result
            result=json.loads(raw,object_pairs_hook=unique,
                              parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        except (UnicodeError,ValueError,RecursionError) as exc:
            raise RecordError("RELAY_JSON_INVALID") from None
        require(key not in json.dumps(result, ensure_ascii=False), "RELAY_SECRET_REFLECTED")
        self.last_response_meta = metadata
        return result


def fetch_disclosure_dates(*,ts_code:str,end_date:str,limit:int=MAX_ROWS,transport=None):
    """No retry or other provider fallback after an unsuccessful request."""
    ticker(ts_code);report_period(end_date)
    require(type(limit) is int and 1<=limit<=MAX_ROWS,"RELAY_ROW_BUDGET_INVALID")
    fetch=transport if transport is not None else RelayDisclosureTransport()
    response=fetch(ts_code,end_date,limit)
    return normalize_disclosure_dates(ts_code=ts_code,end_date=end_date,raw=response,
                                      captured_at=datetime.now(timezone.utc).isoformat(),
                                      source_mode="live-relay",limit=limit,
                                      response_transport=getattr(fetch,"last_response_meta",None))
