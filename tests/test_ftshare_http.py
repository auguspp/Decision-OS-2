"""The fixed FTShare transport's credential, wire and single-attempt boundary."""
from base64 import b64encode
from email.message import Message
import hashlib
from http.client import IncompleteRead
from io import BytesIO
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure import ftshare_http as http
from decision_kernel.interfaces.ftshare_check_cli import date_bounds


KEY = "synthetic-ftshare-key-only-12345678"
SINCE, UNTIL = date_bounds("20261016", "20261016")
SYMBOLS = ["600000.SH", "000001.SZ"]


def headers(**values):
    result = Message()
    for name, value in values.items():
        result[name.replace("_", "-")] = value
    return result


class Reply:
    def __init__(self, raw=b'{"code":200,"message":"success","data":[]}', *,
                 response_headers=None, status=200, url=None, failure=None):
        self.raw, self.status, self.url = raw, status, url
        self.headers = headers(Content_Type="application/json") if response_headers is None else response_headers
        self.failure = failure
        self.reads = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, bound):
        self.reads.append(bound)
        if self.failure:
            raise self.failure
        return self.raw[:bound]


class Opener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        assert self.outcomes, "unexpected automatic retry"
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome.url is None:
            outcome.url = request.full_url
        return outcome


class ErrorBody(BytesIO):
    def __init__(self, raw, *, failure=None):
        super().__init__(raw)
        self.reads, self.failure = [], failure

    def read(self, bound):
        self.reads.append(bound)
        if self.failure is not None:
            raise self.failure
        return super().read(bound)


def setup(reply=None, **kwargs):
    opener = Opener(Reply() if reply is None else reply)
    return http.FTShareHTTP(credential=KEY, opener=opener, **kwargs), opener


def get_daily(client, **changes):
    return client.candlesticks_batch(**{"symbols": SYMBOLS, "since": SINCE,
        "until": UNTIL, "adjust_kind": "none", **changes})


def assert_no_source_retained(client):
    assert client.last_response_bytes is None
    assert client.last_response_meta is None
    assert KEY not in json.dumps(client.last_error_meta)


@pytest.mark.parametrize("adjust_kind", ["none", "forward"])
def test_daily_request_uses_fixed_origin_repeated_symbols_and_header_only_credential(monkeypatch, adjust_kind):
    monkeypatch.setenv("FTSHARE_BASE_URL", "https://untrusted.invalid/gateway")
    client, opener = setup()
    get_daily(client, adjust_kind=adjust_kind)
    request, timeout = opener.calls[0]
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == (
        "https", "market.ft.tech", "/gateway/api/v2/market/data/stock-candlesticks/batch")
    assert parse_qs(url.query) == {"symbols": sorted(SYMBOLS), "interval_unit": ["day"],
        "adjust_kind": [adjust_kind], "since_ts_millis": [str(SINCE)], "until_ts_millis": [str(UNTIL)]}
    assert request.method == "GET" and request.data is None and timeout == 90
    sent = {name.lower(): value for name, value in request.header_items()}
    assert sent["x-client-name"] == "ft-claw"
    assert sent["user-agent"] == "DecisionOS2-FTShareDaily/0.1"
    assert sent["ftshare_api_key"] == KEY and sent["accept-encoding"] == "identity"
    assert sent["content-type"] == "application/json" and sent["accept"] == "application/json"
    assert KEY not in request.full_url and "authorization" not in sent
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("adjust_kind", ["none", "forward"])
def test_single_stock_route_requests_one_complete_daily_range_without_limit(monkeypatch, adjust_kind):
    monkeypatch.setenv("FTSHARE_BASE_URL", "https://untrusted.invalid/gateway")
    client, opener = setup()
    client.candlesticks("600000.SH", since=SINCE, until=UNTIL, adjust_kind=adjust_kind)
    request, timeout = opener.calls[0]
    url = urlsplit(request.full_url)
    assert (url.scheme, url.netloc, url.path) == (
        "https", "market.ft.tech", "/gateway/api/v1/market/data/stock-candlesticks")
    assert parse_qs(url.query) == {"symbol": ["600000.SH"], "interval_unit": ["day"],
        "adjust_kind": [adjust_kind], "since_ts_millis": [str(SINCE)], "until_ts_millis": [str(UNTIL)]}
    sent = {name.lower(): value for name, value in request.header_items()}
    assert sent["ftshare_api_key"] == KEY and sent["x-client-name"] == "ft-claw"
    assert sent["accept-encoding"] == "identity" and sent["content-type"] == "application/json"
    assert KEY not in request.full_url and request.method == "GET" and timeout == 90
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("changes", [{"symbol": "all"}, {"symbol": SYMBOLS},
    {"symbol": "600000.SH&limit=1"}, {"adjust_kind": "backward"}, {"since": SINCE + 1}])
def test_invalid_single_stock_request_is_refused_before_source_access(changes):
    client, opener = setup()
    with pytest.raises(ValueError, match="FTSHARE"):
        client.candlesticks(**{"symbol": "600000.SH", "since": SINCE, "until": UNTIL,
            "adjust_kind": "none", **changes})
    assert client.requests == 0 and opener.calls == []


def test_default_opener_ignores_environment_proxy_and_rejects_redirects(monkeypatch):
    captured = []
    fake = Opener()
    def build(*handlers):
        captured.extend(handlers)
        return fake
    monkeypatch.setattr(http, "build_opener", build)
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")
    client = http.FTShareHTTP(credential=KEY)
    assert next(h for h in captured if isinstance(h, ProxyHandler)).proxies == {}
    redirect = next(h for h in captured if isinstance(h, HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.invalid") is None
    assert client.requests == 0 and fake.calls == []


def test_factor_dates_and_pagination_are_bounded_without_implicit_followup():
    client, opener = setup()
    client.stock_adjust_factor("000001.SZ", since=SINCE, until=UNTIL, page=1, page_size=1000)
    parsed = urlsplit(opener.calls[0][0].full_url)
    assert parsed.path == "/gateway/api/v1/market/data/stock-adjust-factor"
    assert parse_qs(parsed.query) == {"symbol": ["000001.SZ"], "start_date": ["2026-10-16"],
        "end_date": ["2026-10-16"], "page": ["1"], "page_size": ["1000"]}
    for bad in ({"page": 2}, {"page_size": 1001}, {"page": True}):
        with pytest.raises(RecordError, match="FTSHARE_FACTOR_PAGE_BOUND"):
            client.stock_adjust_factor("000001.SZ", **{"since": SINCE, "until": UNTIL, **bad})
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("credential", [None, "", "short", "key with spaces", "key\nheader", "秘密cannot-ascii"])
def test_bad_credential_refused_before_any_network(credential, monkeypatch):
    monkeypatch.delenv(http.SECRET_ENV, raising=False)
    opener = Opener()
    client = http.FTShareHTTP(credential=credential, opener=opener)
    with pytest.raises(RecordError, match="FTSHARE_CREDENTIAL_REQUIRED"):
        get_daily(client)
    assert client.requests == 0 and opener.calls == []


def test_arbitrary_endpoint_or_unbounded_timeout_is_not_supported():
    client, opener = setup()
    with pytest.raises(RecordError, match="FTSHARE_ENDPOINT_NOT_ALLOWED"):
        client._get("https://untrusted.invalid/collect", {})
    for value in (0, 91, True, 90.0):
        with pytest.raises(RecordError, match="FTSHARE_TIMEOUT_INVALID"):
            http.FTShareHTTP(credential=KEY, opener=opener, request_timeout_seconds=value)
    assert client.requests == 0 and opener.calls == []


def test_decimal_tokens_keep_all_digits_and_original_body_is_not_reencoded():
    raw = (b'{\n  "code":200, "data":{"close":12.12345678901234567890123456789,'
           b'"factor":1.234567890123456789e-20,"volume":123456789012345678901}\n}\n')
    reply = Reply(raw)
    client, opener = setup(reply)
    data = get_daily(client)["data"]
    assert data["close"] == "12.12345678901234567890123456789"
    assert data["factor"] == "1.234567890123456789e-20"
    assert data["volume"] == 123456789012345678901
    assert client.last_response_bytes == raw
    assert client.last_response_meta["wire_sha256"] == hashlib.sha256(raw).hexdigest()
    assert client.last_response_meta["wire_bytes"] == len(raw)
    assert reply.reads == [http.MAX_BODY + 1] and reply.closed
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("raw", [
    b'{"code":200,"code":401}',
    b'{"data":{"close":"12.3","clo\\u0073e":"99"}}',
    b'{"code":NaN}', b'{"code":Infinity}', b'{"code":-Infinity}',
    b'{"code":200,', b'\xff',
])
def test_ambiguous_or_invalid_json_is_never_retained_as_source(raw):
    client, opener = setup(Reply(raw))
    with pytest.raises(RecordError, match="FTSHARE_JSON_INVALID"):
        get_daily(client)
    assert client.requests == len(opener.calls) == 1
    assert_no_source_retained(client)


@pytest.mark.parametrize("kind", ["raw", "escaped-value", "escaped-key"])
def test_reflected_credential_is_rejected_even_in_nested_escaped_strings(kind):
    escaped = "".join(f"\\u{ord(c):04x}" for c in KEY)
    if kind == "raw":
        raw = json.dumps({"data": {"nested": [KEY]}}).encode()
    elif kind == "escaped-value":
        raw = ('{"data":{"nested":["prefix-' + escaped + '-suffix"]}}').encode()
    else:
        raw = ('{"data":{"' + escaped + '":"some value"}}').encode()
    if kind != "raw":
        assert KEY.encode() not in raw  # The decoded scan is necessary.
    client, opener = setup(Reply(raw))
    with pytest.raises(RecordError, match="FTSHARE_SECRET_REFLECTED") as exc:
        get_daily(client)
    assert KEY not in str(exc.value) and client.requests == len(opener.calls) == 1
    assert_no_source_retained(client)


def test_header_diagnostics_preserve_retry_seconds_and_remain_only_snapshots():
    reply = Reply(response_headers=headers(Retry_After="90", X_RateLimit_Remaining="17",
        X_RateLimit_IP_Remaining="0", X_Request_ID="safe-request-1", X_Cache="HIT",
        Set_Cookie=KEY, Authorization=KEY))
    client, opener = setup(reply)
    get_daily(client)
    meta = client.last_response_meta
    assert client.last_retry_after == ("valid", 90)
    assert meta["retry_after_seconds"] == 90
    assert meta["rate_limits"] == {"key_remaining": {"state": "valid", "remaining": 17},
                                  "ip_remaining": {"state": "valid", "remaining": 0}}
    assert meta["x_request_id"] == "safe-request-1" and meta["x_cache"] == "HIT"
    assert KEY not in json.dumps(meta) and "set-cookie" not in json.dumps(meta).lower()
    assert client.requests == len(opener.calls) == 1


def test_duplicate_or_reflected_retry_and_rate_headers_are_not_zero_wait_or_zero_quota():
    ambiguous = headers(Retry_After="90", X_RateLimit_Remaining="10",
                        X_RateLimit_IP_Remaining=KEY, X_Request_ID=KEY, X_Cache=KEY)
    ambiguous["Retry-After"] = "120"
    ambiguous["X-RateLimit-Remaining"] = "9"
    client, opener = setup(Reply(response_headers=ambiguous))
    get_daily(client)
    meta = client.last_response_meta
    assert client.last_retry_after == ("invalid", None)
    assert meta["rate_limits"] == {"key_remaining": {"state": "invalid", "remaining": None},
        "ip_remaining": {"state": "withheld", "remaining": None}}
    assert meta["x_request_id"] is None and meta["x_cache"] is None
    assert KEY not in json.dumps(meta) and client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("status,classification", [(401, "auth-or-entitlement"),
    (403, "auth-or-entitlement"), (429, "rate-limit"), (503, "http-error")])
def test_http_rejections_read_only_bounded_diagnostics_and_never_retry(status, classification):
    body = ErrorBody(KEY.encode())
    error = HTTPError("https://provider.invalid/" + KEY, status, KEY,
        headers(Retry_After="90", X_RateLimit_Remaining="0", X_Request_ID=KEY), body)
    client, opener = setup(error)
    with pytest.raises(RecordError, match="FTSHARE_HTTP_" + str(status)) as exc:
        get_daily(client)
    assert KEY not in str(exc.value) and body.closed
    assert client.last_error_meta["classification"] == classification
    assert client.last_error_meta["retry_after_seconds"] == 90
    assert client.last_error_meta["rate_limits"]["key_remaining"]["remaining"] == 0
    assert client.last_error_meta["x_request_id"] is None
    assert body.reads == [http.MAX_ERROR_BODY + 1]
    assert client.last_error_meta["error_diagnostics"]["observed_body_bytes"] == len(KEY)
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("code,expected,category", [
    (403, 403, "access-denied"), ("401", 401, "authentication"),
    ("PERMISSION_DENIED", "PERMISSION_DENIED", "access-denied"),
    ("INVALID_API_KEY", "INVALID_API_KEY", "authentication"),
    (10001, 10001, "unclassified"), (200, 200, "unclassified"),
    ("ALICE_SMITH", None, "unclassified"), (13800138000, None, "unclassified"),
    (True, None, "unclassified"), ("alice@example.invalid", None, "unclassified"),
])
def test_error_business_code_is_safe_metadata_and_cannot_change_http_status(code, expected, category):
    prose = "Private account Alice Smith, alice@example.invalid, phone 13800138000"
    raw = json.dumps({"code": code, "message": prose, "data": {"account": prose}}).encode()
    body = ErrorBody(raw)
    error = HTTPError("https://provider.invalid", 403, prose,
        headers(Content_Type="application/json; charset=utf-8"), body)
    client, opener = setup(error)
    with pytest.raises(RecordError, match="^FTSHARE_HTTP_403$") as exc:
        client.candlesticks("600000.SH", since=SINCE, until=UNTIL)
    diagnostics = client.last_error_meta["error_diagnostics"]
    assert diagnostics == {"content_type": "application/json", "observed_body_bytes": len(raw),
        "truncated": False, "business_code": expected, "category": category}
    retained = json.dumps(client.last_error_meta) + str(exc.value)
    assert all(private not in retained for private in (prose, "Alice", "ALICE_SMITH", "alice@", "13800138000"))
    assert body.reads == [http.MAX_ERROR_BODY + 1] and body.closed
    assert client.last_error_meta["http_status"] == 403
    assert client.last_error_meta["classification"] == "auth-or-entitlement"
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("encoding", ["literal", "unicode", "unicode-key", "percent", "double-percent", "html", "base64"])
def test_error_credential_reflection_is_withheld_including_encoded_values_and_keys(encoding):
    encoded_key = {"literal": KEY, "unicode": "".join(f"\\u{ord(c):04x}" for c in KEY),
        "unicode-key": "".join(f"\\u{ord(c):04x}" for c in KEY),
        "percent": "".join(f"%{ord(c):02X}" for c in KEY),
        "double-percent": "".join(f"%25{ord(c):02X}" for c in KEY),
        "html": "".join(f"&#{ord(c)};" for c in KEY),
        "base64": b64encode(KEY.encode()).decode()}[encoding]
    payload = {"code": 403, "message": {KEY: "private"} if encoding == "unicode-key" else KEY}
    raw = json.dumps(payload).replace(KEY, encoded_key).encode()
    body = ErrorBody(raw)
    error = HTTPError("https://provider.invalid", 403, KEY, headers(Content_Type="application/json"), body)
    client, opener = setup(error)
    with pytest.raises(RecordError, match="^FTSHARE_HTTP_403$"):
        get_daily(client)
    diagnostics = client.last_error_meta["error_diagnostics"]
    assert diagnostics["category"] == "secret-reflected" and diagnostics["business_code"] is None
    assert encoded_key not in json.dumps(client.last_error_meta)
    assert_no_source_retained(client)
    assert body.closed and body.reads == [http.MAX_ERROR_BODY + 1]
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("raw,mime,category,code", [
    (b'{"code":403,"message":"private"}' + b" " * 10000, "application/json", "body-truncated", None),
    (b'{"code":403}', "text/html", "unsupported-content-type", None),
    (b"<html>private@example.invalid</html>", "application/json", "non-json", None),
    (b"private@example.invalid", "text/plain", "non-json", None),
    (b'{"code":403}', "text/plain", "access-denied", 403),
    (b'{"code":403}', "text/json", "access-denied", 403),
    (b'{"code":403,"code":401}', "application/json", "non-json", None),
    (b'{"code":NaN}', "application/json", "non-json", None),
    (b'\xff', "application/json", "non-json", None),
    (b"", "application/json", "empty-body", None),
])
def test_untrusted_error_format_and_size_never_expose_prose_or_parse_a_partial_body(raw, mime, category, code):
    body = ErrorBody(raw)
    error = HTTPError("https://provider.invalid", 403, "private@example.invalid",
        headers(Content_Type=mime, Content_Length=str(len(raw))), body)
    client, opener = setup(error)
    with pytest.raises(RecordError, match="^FTSHARE_HTTP_403$"):
        get_daily(client)
    diagnostics = client.last_error_meta["error_diagnostics"]
    assert diagnostics["category"] == category and diagnostics["business_code"] == code
    assert diagnostics["observed_body_bytes"] == min(len(raw), http.MAX_ERROR_BODY + 1)
    assert diagnostics["truncated"] is (len(raw) > http.MAX_ERROR_BODY)
    assert diagnostics["content_type"] == (mime if mime in http.ERROR_MIMES else None)
    assert "private" not in json.dumps(client.last_error_meta)
    assert body.closed and body.reads == [http.MAX_ERROR_BODY + 1]
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError(KEY), IncompleteRead(KEY.encode(), 100)])
def test_error_body_read_failure_preserves_the_original_http_rejection(failure):
    body = ErrorBody(b"", failure=failure)
    error = HTTPError("https://provider.invalid", 403, KEY, headers(Content_Type="application/json"), body)
    client, opener = setup(error)
    with pytest.raises(RecordError, match="^FTSHARE_HTTP_403$"):
        get_daily(client)
    assert client.last_error_meta["error_diagnostics"] == {"content_type": "application/json",
        "observed_body_bytes": None, "truncated": None, "business_code": None, "category": "body-unavailable"}
    assert client.last_error_meta["http_status"] == 403
    assert body.closed and body.reads == [http.MAX_ERROR_BODY + 1]
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1


def test_encoded_credential_in_safe_looking_header_is_withheld():
    reflected = b64encode(KEY.encode()).decode().rstrip("=")
    client, _ = setup(Reply(response_headers=headers(X_Request_ID=reflected, X_Cache=reflected)))
    get_daily(client)
    assert client.last_response_meta["x_request_id"] is None
    assert client.last_response_meta["x_cache"] is None
    assert reflected not in json.dumps(client.last_response_meta)


@pytest.mark.parametrize("failure,classification", [
    (TimeoutError(KEY), "timeout"),
    (URLError(TimeoutError(KEY)), "timeout"),
    (IncompleteRead(KEY.encode(), 100), "incomplete-read"),
])
@pytest.mark.parametrize("stage", ["open", "body"])
def test_timeout_or_partial_body_stops_at_one_request_and_keeps_safe_headers(failure, classification, stage):
    outcome = failure if stage == "open" else Reply(failure=failure,
        response_headers=headers(Retry_After="90", X_RateLimit_Remaining="3", X_Request_ID=KEY))
    client, opener = setup(outcome)
    with pytest.raises(RecordError, match="FTSHARE_HTTP_UNAVAILABLE"):
        get_daily(client)
    meta = client.last_error_meta
    assert meta["classification"] == classification
    if stage == "body":
        assert meta["http_status"] == 200 and meta["retry_after_seconds"] == 90
        assert meta["rate_limits"]["key_remaining"]["remaining"] == 3
        assert outcome.closed
    else:
        assert meta["http_status"] is None
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1


@pytest.mark.parametrize("reply,reason,read_count", [
    (Reply(status=302), "HTTP_RESPONSE_INVALID", 0),
    (Reply(url="https://elsewhere.invalid/collect"), "HTTP_RESPONSE_INVALID", 0),
    (Reply(response_headers=headers(Content_Encoding="gzip")), "CONTENT_ENCODING_INVALID", 0),
    (Reply(response_headers=headers(Content_Type="text/html")), "CONTENT_TYPE_INVALID", 0),
    (Reply(response_headers=headers(Content_Length=str(http.MAX_BODY + 1))), "RESPONSE_BYTES_INVALID", 0),
    (Reply(response_headers=headers(Content_Length="-1")), "RESPONSE_BYTES_INVALID", 0),
    (Reply(response_headers=headers(Content_Length="999")), "RESPONSE_BYTES_INVALID", 1),
    (Reply(b""), "RESPONSE_BYTES_INVALID", 1),
    (Reply(b" " * (http.MAX_BODY + 1)), "RESPONSE_BYTES_INVALID", 1),
])
def test_changed_response_identity_encoding_or_size_is_not_a_retryable_partial(reply, reason, read_count):
    client, opener = setup(reply)
    with pytest.raises(RecordError, match="FTSHARE_" + reason):
        get_daily(client)
    assert reply.closed and len(reply.reads) == read_count
    assert all(bound == http.MAX_BODY + 1 for bound in reply.reads)
    assert_no_source_retained(client)
    assert client.requests == len(opener.calls) == 1

