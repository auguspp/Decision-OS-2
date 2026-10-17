"""Wire-boundary tests use synthetic responses; no production credential or network."""
from http.client import IncompleteRead
from email.message import Message
import io
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite
from decision_kernel.infrastructure import github_store as remote


class Response(io.BytesIO):
    status = 200
    def __init__(self, raw=b'{}', url=remote.API_ROOT + "/repos/example/store", *, headers=None):
        super().__init__(raw)
        self.url, self.headers = url, headers
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, result):
        self.result, self.calls = result, []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_only_dedicated_token_environment_is_used(monkeypatch):
    monkeypatch.delenv("DECISION_OS_STORE_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "engineering-must-not-be-used")
    monkeypatch.setenv("GITHUB_TOKEN", "also-not-used")
    with pytest.raises(RecordError, match="STORE_TOKEN_REQUIRED"):
        remote.GitHubAPI.from_environment()
    monkeypatch.setenv("DECISION_OS_STORE_TOKEN", "synthetic-store-token")
    api = remote.GitHubAPI.from_environment()
    api._opener = Opener(Response())
    assert api.request("GET", "/repos/example/store") == {}
    request, timeout = api._opener.calls[0]
    assert request.full_url == remote.API_ROOT + "/repos/example/store"
    assert request.get_header("Authorization") == "Bearer synthetic-store-token"
    assert timeout == 30
    assert "synthetic-store-token" not in repr(api)


def test_post_only_explicit_graphql_payload():
    api = remote.GitHubAPI("synthetic")
    api._opener = Opener(Response(url=remote.API_ROOT + "/graphql"))
    payload = {"query": remote.COMMIT_MUTATION, "variables": {"input": {"expectedHeadOid": "a" * 40}}}
    assert api.request("POST", "/graphql", payload) == {}
    request, _ = api._opener.calls[0]
    assert json.loads(request.data) == payload
    assert request.method == "POST"


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "https://evil.test/repos/a/b", None), ("GET", "//evil.test/repos/a/b", None),
    ("GET", "/repos/example/store\nX: injection", None), ("GET", "/repos/x#fragment", None),
    ("GET", "/user", None), ("GET", "/repos/example/store", {}),
    ("POST", "/repos/example/store/contents/x", {}), ("DELETE", "/repos/example/store", None),
    ("POST", "/graphql", None),
])
def test_arbitrary_host_or_operation_never_sent(method, path, payload):
    api = remote.GitHubAPI("synthetic")
    api._opener = Opener(Response())
    with pytest.raises(RecordError):
        api.request(method, path, payload)
    assert not api._opener.calls


@pytest.mark.parametrize("code,expected", [(401, "REMOTE_NOT_AUTHORIZED"), (403, "REMOTE_NOT_AUTHORIZED"),
                                            (404, "REMOTE_NOT_FOUND_OR_NOT_AUTHORIZED"), (429, "REMOTE_RATE_LIMIT"),
                                            (500, "REMOTE_HTTP_FAILURE"), (302, "REMOTE_HTTP_FAILURE")])
def test_get_errors_do_not_disclose_bodies_or_fall_back(code, expected):
    api = remote.GitHubAPI("synthetic-secret")
    api._opener = Opener(HTTPError("https://sensitive.invalid", code, "private error body", {}, io.BytesIO(b"private")))
    with pytest.raises(StoreUnavailable, match=expected) as caught:
        api.request("GET", "/repos/example/store")
    assert str(caught.value) == expected and len(api._opener.calls) == 1


@pytest.mark.parametrize("failure", [URLError("secret network error"), TimeoutError("private"),
                                    IncompleteRead(b"private"), HTTPError("url", 500, "secret", {}, None)])
def test_ambiguous_post_never_retried(failure):
    api = remote.GitHubAPI("synthetic-secret")
    api._opener = Opener(failure)
    with pytest.raises(UnknownWrite, match="REMOTE_WRITE_UNCONFIRMED") as caught:
        api.request("POST", "/graphql", {"query": "synthetic"})
    assert str(caught.value) == "REMOTE_WRITE_UNCONFIRMED" and len(api._opener.calls) == 1


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'not-json', b'\xff'])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_invalid_json_read_fails_and_mutation_remains_uncertain(raw, method):
    path = "/graphql" if method == "POST" else "/repos/example/store"
    api = remote.GitHubAPI("synthetic")
    api._opener = Opener(Response(raw, remote.API_ROOT + path))
    with pytest.raises(UnknownWrite if method == "POST" else StoreUnavailable):
        api.request(method, path, {} if method == "POST" else None)


def test_limits_are_explicit_not_partial_success(monkeypatch):
    monkeypatch.setattr(remote, "MAX_RESPONSE", 8)
    api = remote.GitHubAPI("synthetic")
    api._opener = Opener(Response(b" " * 9))
    with pytest.raises(StoreUnavailable, match="REMOTE_RESPONSE_UNAVAILABLE"):
        api.request("GET", "/repos/example/store")
    api._requests = remote.MAX_REQUESTS
    before = len(api._opener.calls)
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        api.request("POST", "/graphql", {})
    assert len(api._opener.calls) == before


def test_proxy_environment_and_redirects_not_used(monkeypatch):
    captured = []
    monkeypatch.setenv("HTTPS_PROXY", "https://not-used.invalid")
    monkeypatch.setattr(remote, "build_opener", lambda *handlers: captured.extend(handlers))
    remote.GitHubAPI("synthetic")
    proxy, redirect = captured
    assert proxy.proxies == {}
    assert redirect.redirect_request(Request(remote.API_ROOT), None, 302, "", {}, "https://evil.test") is None


def test_success_response_from_other_url_is_not_trusted():
    api = remote.GitHubAPI("synthetic")
    api._opener = Opener(Response(url="https://evil.test/anything"))
    with pytest.raises(StoreUnavailable):
        api.request("GET", "/repos/example/store")


def quota_headers(*, resource="core", remaining=999, limit=1000, reset=2000000000, extra=None):
    # HTTPMessage/Message lookup is case-insensitive, as on urllib responses.
    headers = Message()
    for key, value in {"X-RateLimit-Resource": resource, "X-RateLimit-Remaining": remaining,
                       "X-RateLimit-Limit": limit, "X-RateLimit-Reset": reset, **(extra or {})}.items():
        headers[key] = str(value)
    return headers


def test_native_quota_same_window_never_refills_and_only_new_header_advances_reset():
    limits = remote.GitHubRateLimits()
    assert not limits.admits(0)  # Unknown does not mean unused.
    limits.observe(quota_headers(remaining=2, reset=1))
    limits.debit("core")
    limits.observe(quota_headers(remaining=999, reset=1))
    assert limits.receipt()["resources"]["core"]["remaining"] == 1
    limits.debit("core")
    limits.debit("core")
    assert not limits.admits(1)  # A reset timestamp in the past is not a refill.
    limits.observe(quota_headers(remaining=900, reset=2))
    assert limits.admits(900)
    limits.observe(quota_headers(remaining=1000, reset=1))
    assert limits.receipt()["resources"]["core"] == {"limit": 1000, "remaining": 900, "reset": 2}


@pytest.mark.parametrize("headers", [
    None, {}, {"x-ratelimit-remaining": "1000"},
    {"x-ratelimit-resource": "search", "x-ratelimit-remaining": "999", "x-ratelimit-limit": "1000", "x-ratelimit-reset": "2"},
    *({"x-ratelimit-resource": "core", "x-ratelimit-remaining": "999", "x-ratelimit-limit": "1000",
       "x-ratelimit-reset": "2", key: value} for key, value in (
        ("x-ratelimit-remaining", "1001"), ("x-ratelimit-remaining", "-1"),
        ("x-ratelimit-remaining", "999\nprivate"), ("x-ratelimit-remaining", 999),
        ("x-ratelimit-limit", "0"), ("x-ratelimit-limit", "1000000000000"),
        ("x-ratelimit-reset", "0"), ("x-ratelimit-reset", "not-a-time"))),
])
def test_missing_or_invalid_native_headers_cannot_create_or_restore_allowance(headers):
    limits = remote.GitHubRateLimits()
    assert limits.observe(headers) is None
    assert limits.receipt() == {"observed": False, "throttled": False, "resources": {}}
    assert not limits.admits(0)
    limits.observe(quota_headers(remaining=0, reset=1))
    before = limits.receipt()
    assert limits.observe(headers) is None
    assert limits.receipt() == before and not limits.admits(1)


def test_successful_wire_headers_account_for_core_and_graphql_separately_and_stay_private():
    api = remote.GitHubAPI("synthetic-secret-token")
    api._opener = Opener(Response(headers=quota_headers(remaining=8, extra={
        "Authorization": "synthetic-secret-token", "X-Private-Repository": "private-owner/hidden",
        "X-Private-Body": "supplier rows must not be retained"})))
    api.request("GET", "/repos/example/store")
    api._opener = Opener(Response(url=remote.API_ROOT + "/graphql",
        headers=quota_headers(resource="graphql", remaining=29, limit=5000)))
    api.request("POST", "/graphql", {"query": "synthetic"})
    api._opener = Opener(Response())
    api.request("GET", "/repos/example/store")  # Missing headers still debit known core usage.
    receipt = api.rate_limits.receipt()
    assert receipt == {"observed": True, "throttled": False, "resources": {
        "core": {"limit": 1000, "remaining": 7, "reset": 2000000000},
        "graphql": {"limit": 5000, "remaining": 29, "reset": 2000000000}}}
    api.rate_limits.debit("graphql")
    api.rate_limits.debit("search")
    assert api.rate_limits.receipt()["resources"]["core"]["remaining"] == 7
    assert api.rate_limits.receipt()["resources"]["graphql"]["remaining"] == 28
    assert api.rate_limits.admits(7) and not api.rate_limits.admits(8)
    receipt["resources"]["core"]["remaining"] = 1000
    assert not api.rate_limits.admits(8)  # Exported receipts cannot change admission.
    text = json.dumps(api.rate_limits.receipt())
    assert all(value not in text for value in ("synthetic-secret-token", "private-owner/hidden", "supplier rows"))


@pytest.mark.parametrize("code,headers,expected", [
    (403, quota_headers(remaining=0), "REMOTE_RATE_LIMIT"),
    (403, quota_headers(resource="graphql", remaining=0), "REMOTE_RATE_LIMIT"),
    (403, quota_headers(remaining=100, extra={"Retry-After": "30"}), "REMOTE_RATE_LIMIT"),
    (403, quota_headers(remaining=100), "REMOTE_NOT_AUTHORIZED"),
    (403, {"x-ratelimit-remaining": "0"}, "REMOTE_NOT_AUTHORIZED"),
    (403, {"retry-after": "private-invalid-value"}, "REMOTE_NOT_AUTHORIZED"),
    (401, quota_headers(remaining=0), "REMOTE_NOT_AUTHORIZED"),
    (429, {}, "REMOTE_RATE_LIMIT"),
])
def test_native_rate_evidence_distinguishes_denial_without_body_parsing_or_retry(code, headers, expected):
    api = remote.GitHubAPI("synthetic-secret")
    api._opener = Opener(HTTPError("https://private.invalid", code, "private error", headers,
        io.BytesIO(b'{"message":"private rate limit or authentication prose"}')))
    with pytest.raises(StoreUnavailable) as caught:
        api.request("GET", "/repos/example/store")
    assert str(caught.value) == expected and len(api._opener.calls) == api._requests == 1
    assert api.rate_limits.throttled is (expected == "REMOTE_RATE_LIMIT")
    assert "private" not in json.dumps(api.rate_limits.receipt())


def test_post_rate_limit_keeps_ambiguous_write_and_sticky_stop():
    api = remote.GitHubAPI("synthetic-secret")
    api._opener = Opener(HTTPError("https://private.invalid", 429, "private", quota_headers(remaining=0),
        io.BytesIO(b"private source rows")))
    with pytest.raises(UnknownWrite, match="^REMOTE_WRITE_UNCONFIRMED$"):
        api.request("POST", "/graphql", {"query": "synthetic"})
    assert api.rate_limits.throttled and len(api._opener.calls) == api._requests == 1
    api.rate_limits.observe(quota_headers(remaining=1000, reset=2000000001))
    assert not api.rate_limits.admits(1)  # No new window authorizes an automatic write retry.
