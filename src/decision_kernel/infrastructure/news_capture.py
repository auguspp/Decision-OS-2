"""Finite news capture; preserve safe receipts and never refetch successful peers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPException
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT as RELAY_ROOT
from decision_kernel.observations.news_windows import (
    NEWSNOW_SOURCES, RELAY_APIS, combine, newsnow_window, relay_window, verify_reading,
)

NEWSNOW_ROOT = "http://127.0.0.1:4444/api/s"
NEWS_SOURCES = frozenset(("sina", "wallstreetcn", "10jqka", "eastmoney",
                         "yuncaijing", "fenghuang", "jinrongjie", "cls", "yicai"))
MAJOR_SOURCES = frozenset(("cls", "新浪财经", "华尔街见闻", "财联社", "新华网", "凤凰财经",
                          "同花顺", "中证网", "财新网", "第一财经"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        return None


def _now():
    return datetime.now(timezone.utc).isoformat()


def _code(exc):
    value = str(exc) if isinstance(exc, RecordError) else "NEWS_TRANSPORT_UNAVAILABLE"
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", value) else "NEWS_REJECTED"


def _shape(raw):
    """Only structure counts; never raw error messages, headlines or credentials."""
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"object": False}
        out = {"object": True, "top_field_count": len(data)}
        rows = data.get("items")
        if isinstance(data.get("data"), dict):
            rows = data["data"].get("items")
            fields = data["data"].get("fields")
            if isinstance(fields, list):
                out["field_count"] = len(fields)
        if isinstance(rows, list):
            out["item_count"] = len(rows)
        return out
    except (ValueError, UnicodeError, RecursionError):
        return {"valid_json": False}


def _attach(reading, receipts):
    require(len(reading["source_outcomes"]) == len(receipts), "NEWS_RECEIPT_COUNT")
    for outcome, receipt in zip(reading["source_outcomes"], receipts):
        outcome["capture"] = receipt
    reading.pop("reading_sha256")
    reading["reading_sha256"] = sha256(encoded(reading)).hexdigest()
    return verify_reading(reading)


def fetch_newsnow(source: str, *, opener=None) -> bytes:
    require(source in NEWSNOW_SOURCES, "NEWSNOW_SOURCE_NOT_ALLOWED")
    url = NEWSNOW_ROOT + "?" + urlencode({"id": source, "latest": ""})
    request = Request(url, method="GET", headers={
        "Accept": "application/json", "Accept-Encoding": "identity",
        "User-Agent": "DecisionOS2-NewsNow/0.1",
    })
    opener = opener if opener is not None else build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            require(response.status == 200 and response.geturl() == url,
                    "NEWSNOW_HTTP_RESPONSE_INVALID")
            length = response.headers.get("Content-Length")
            require(length is None or (length.isdigit() and int(length) <= 1024 * 1024),
                    "NEWSNOW_HTTP_BODY_LIMIT")
            require(response.headers.get("Content-Encoding", "identity").lower() in ("", "identity"),
                    "NEWSNOW_ENCODING_INVALID")
            raw = response.read(1024 * 1024 + 1)
            require(0 < len(raw) <= 1024 * 1024
                    and (length is None or len(raw) == int(length)), "NEWSNOW_HTTP_BODY_LIMIT")
            return raw
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise RecordError("NEWSNOW_HTTP_" + str(code)) from None
    except RecordError:
        raise
    except (OSError, ValueError, HTTPException):
        raise RecordError("NEWSNOW_HTTP_UNAVAILABLE") from None


def capture_newsnow(*, observed_at=None, fetcher=fetch_newsnow, sources=NEWSNOW_SOURCES, clock=_now):
    require(isinstance(sources, (tuple, list)) and sources
            and all(isinstance(s, str) and s in NEWSNOW_SOURCES for s in sources)
            and len(set(sources)) == len(sources), "NEWSNOW_SOURCE_SELECTION_INVALID")
    windows, receipts = [], []
    for source in sources:
        raw = None
        start = observed_at if observed_at is not None else clock()
        receipt = {"schema": "news-capture-receipt-v1", "requested_at": start,
                   "request": {"family": "newsnow", "source": source},
                   "source_body_retained": False, "response_sha256": None}
        try:
            raw = fetcher(source)
            finish = observed_at if observed_at is not None else clock()
            receipt.update(received_at=finish, response_sha256=sha256(raw).hexdigest(),
                           response_bytes=len(raw), http_status=200)
            windows.append(newsnow_window(raw, source, observed_at=finish))
            receipt["status"] = "normalized"
        except (RecordError, OSError, HTTPException, TypeError, KeyError, OverflowError) as exc:
            receipt.update(status="unavailable-or-rejected", error_code=_code(exc),
                           finished_at=observed_at if observed_at is not None else clock(),
                           safe_structure=_shape(raw))
            windows.append({"status": "source-unavailable", "source_id": source})
        receipts.append(receipt)
    return _attach(combine(windows), receipts)


def relay_news_parameters(api: str, *, source=None, start=None, end=None, date=None):
    require(api in RELAY_APIS, "RELAY_NEWS_API_NOT_REVIEWED")
    if api == "cctv_news":
        require(source in (None, "") and start is None and end is None
                and isinstance(date, str) and len(date) == 8 and date.isascii() and date.isdigit(),
                "RELAY_NEWS_DATE_REQUIRED")
        try:
            datetime.strptime(date, "%Y%m%d")
        except ValueError:
            raise RecordError("RELAY_NEWS_DATE_INVALID") from None
        return {"date": date}
    require(date is None and isinstance(source, str), "RELAY_NEWS_SOURCE_REQUIRED")
    require(source in (NEWS_SOURCES if api == "news" else MAJOR_SOURCES),
            "RELAY_NEWS_SOURCE_NOT_REVIEWED")
    require(isinstance(start, str) and isinstance(end, str),
            "RELAY_NEWS_EXPLICIT_WINDOW_REQUIRED")
    try:
        first = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        last = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        raise RecordError("RELAY_NEWS_EXPLICIT_WINDOW_INVALID") from None
    require(first < last <= first + timedelta(days=1), "RELAY_NEWS_WINDOW_BOUND")
    return {"src": source, "start_date": start, "end_date": end}


def capture_relay_news(api, *, source=None, start=None, end=None, date=None,
                       observed_at=None, transport=None, clock=_now):
    parameters = relay_news_parameters(api, source=source, start=start, end=end, date=date)
    transport = transport if transport is not None else RelayHTTP()
    from decision_kernel.observations.news_windows import MAX_RESPONSE
    url = RELAY_ROOT + "/" + api + "?" + urlencode(parameters)
    name = source if source is not None else "cctv"
    receipt = {"schema": "news-capture-receipt-v1", "request": {"family": "relay", "api": api,
               "parameters": parameters}, "source_body_retained": False, "response_sha256": None,
               "requested_at": observed_at if observed_at is not None else clock()}
    raw = None
    try:
        transport._get_json(url, max_bytes=MAX_RESPONSE, agent="DecisionOS2-News/0.1")
        raw = transport.last_response_bytes
        require(isinstance(raw, bytes) and raw, "RELAY_NEWS_WIRE_MISSING")
        finish = observed_at if observed_at is not None else clock()
        receipt.update(received_at=finish, response_bytes=len(raw), http_status=200,
                       response_sha256=sha256(raw).hexdigest())
        window = relay_window(raw, api, name, observed_at=finish, request=parameters)
        receipt["status"] = "normalized"
    except (RecordError, OSError, HTTPException, TypeError, KeyError, OverflowError) as exc:
        receipt.update(status="unavailable-or-rejected", error_code=_code(exc),
                       finished_at=observed_at if observed_at is not None else clock(),
                       safe_structure=_shape(raw))
        window = {"status": "source-unavailable", "source_id": "relay:" + api + ":" + name}
    return _attach(combine([window]), [receipt])
