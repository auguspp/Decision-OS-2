"""Narrow manual NewsNow/Relay capture. No scheduling, publication or source writes."""
from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT as RELAY_ROOT
from decision_kernel.observations.news_windows import (
    NEWSNOW_SOURCES, RELAY_APIS, combine, newsnow_window, relay_window,
)

NEWSNOW_ROOT = "http://127.0.0.1:4444/api/s"
NEWS_SOURCES = frozenset(("sina", "wallstreetcn", "10jqka", "eastmoney",
                          "yuncaijing", "fenghuang", "jinrongjie", "cls", "yicai"))
MAJOR_SOURCES = frozenset(("cls", "新浪财经", "华尔街见闻", "财联社", "新华网", "凤凰财经",
                           "同花顺", "中证网", "财新网", "第一财经"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        return None


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
    except RecordError:
        raise
    except (OSError, ValueError):
        raise RecordError("NEWSNOW_HTTP_UNAVAILABLE") from None


def capture_newsnow(*, observed_at, fetcher=fetch_newsnow):
    windows = []
    for source in NEWSNOW_SOURCES:
        try:
            raw = fetcher(source)
            windows.append(newsnow_window(raw, source, observed_at=observed_at))
        except (RecordError, OSError, TimeoutError):
            windows.append({"status": "source-unavailable", "source_id": source,
                            "error": "SOURCE_UNAVAILABLE_OR_REJECTED"})
    return combine(windows)


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
    require(first < last <= first + timedelta(days=1),
            "RELAY_NEWS_WINDOW_BOUND")
    return {"src": source, "start_date": start, "end_date": end}


def capture_relay_news(api, *, source=None, start=None, end=None, date=None,
                       observed_at, transport=None):
    parameters = relay_news_parameters(api, source=source, start=start, end=end, date=date)
    transport = transport if transport is not None else RelayHTTP()
    # Reuse the fixed-host secure transport without expanding #12's currently
    # claimed shared READ_APIS/request contract. Only this whitelist reaches it.
    from decision_kernel.observations.news_windows import MAX_RESPONSE
    url = RELAY_ROOT + "/" + api + "?" + urlencode(parameters)
    body = transport._get_json(url, max_bytes=MAX_RESPONSE,
                               agent="DecisionOS2-News/0.1")
    raw = transport.last_response_bytes
    require(isinstance(raw, bytes) and raw, "RELAY_NEWS_WIRE_MISSING")
    window = relay_window(raw, api, source if source is not None else "cctv",
                          observed_at=observed_at, request=parameters)
    return combine([window])
