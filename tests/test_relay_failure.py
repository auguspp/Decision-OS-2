"""Safe failure facts and finite metadata recovery. All traffic is synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
from pathlib import Path
import socket
import ssl
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.application.relay_capture_checkpoint import new_retry_state, validate_retry_state
from decision_kernel.infrastructure.relay_failure import (
    ERROR_BYTES, http_failure, metadata_retry_delay, retry_after, transport_failure,
)
from decision_kernel.infrastructure.relay_http import CAPABILITIES, ROOT, RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.interfaces.relay_capabilities_cli import capture

KEY = "synthetic-secret-123"
SHA = "a" * 40
NOW = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)


class Reply(io.BytesIO):
    def __init__(self, raw=b'{"apis":{"income":{"enabled":true}}}', url=CAPABILITIES):
        super().__init__(raw)
        self.status, self.url = 200, url
        self.headers = {"Content-Type": "application/json"}
    def geturl(self):
        return self.url


class Sequence:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []
    def open(self, request, timeout):
        self.calls.append(request)
        assert timeout == 30
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


class UnreadableReply(Reply):
    def __init__(self, exc, headers):
        super().__init__()
        self.exc = exc
        self.headers.update(headers)

    def read(self, size):
        raise self.exc


def error(status=429, code="rate_limited", headers=None, raw=None):
    body = json.dumps({"error": code, "message": "private diagnostic"}).encode() if raw is None else raw
    return HTTPError(CAPABILITIES, status, "private exception message", headers or {}, io.BytesIO(body))


def run(tmp_path, *responses, retries=2, max_wait_seconds=120, sleep=None):
    opener = Sequence(*responses)
    client = RelayHTTP(opener=opener, credential=KEY)
    waits = []
    result = capture(tmp_path / "out", code_sha=SHA, public_rights_confirmed=True,
                     client=client, env={}, retries=retries, max_wait_seconds=max_wait_seconds,
                     sleep=sleep or waits.append)
    assert KEY not in json.dumps(result) and "private diagnostic" not in json.dumps(result)
    assert json.loads((tmp_path / "out/receipt.json").read_bytes()) == result
    return result, opener, client, waits


@pytest.mark.parametrize("header,state,seconds", [
    (None, "missing", None), ("0", "valid", 0), (" 7 ", "valid", 7),
    ("86400", "valid", 86400), ("-1", "invalid", None), ("1.5", "invalid", None),
    ("3,4", "invalid", None), ("nan", "invalid", None), ("", "invalid", None),
    ("\r\n7", "invalid", None), ("１", "invalid", None), (KEY, "withheld", None),
    ("Fri, 16 Oct 2026 00:01:01 GMT", "valid", 61),
    ("Thu, 15 Oct 2026 23:59:00 GMT", "valid", 0),
    ("Fri, 16 Oct 2026 00:01:01", "invalid", None),
])
def test_retry_after_is_not_guessed_or_shortened(header, state, seconds):
    assert retry_after({"Retry-After": header}, KEY, NOW) == (state, seconds)


def test_duplicate_retry_after_is_ambiguous():
    headers = Message()
    headers.add_header("Retry-After", "10")
    headers.add_header("Retry-After", "60")
    assert retry_after(headers, KEY, NOW) == ("invalid", None)


@pytest.mark.parametrize("status,code,expected", [
    (401, "rate_limited", "auth-or-entitlement"), (403, "forbidden", "auth-or-entitlement"),
    (503, "data_source_unavailable", "source-unavailable"),
    (503, "minute_data_pending", "temporary-upstream"),
    (503, "unknown-message", "http-unclassified"),
    (504, "upstream_timeout", "temporary-upstream"), (502, None, "temporary-gateway"),
    (400, "invalid_params", "request-rejected"), (404, "unknown_api", "request-rejected"),
    (405, "method_not_allowed", "request-rejected"), (429, "ip_rate_limited", "rate-limit"),
])
def test_safe_error_classification_and_close(status, code, expected):
    exc = error(status, code, {"Retry-After": "12", "X-Request-ID": "request-abc", "Set-Cookie": "private"})
    result = http_failure(exc, KEY, now=NOW)
    assert exc.fp.closed and result["classification"] == expected
    assert result["x_request_id"] == "request-abc" and result["retry_after_seconds"] == 12
    assert "private" not in json.dumps(result) and "unknown-message" not in json.dumps(result)


@pytest.mark.parametrize("raw,state", [
    (b'{"error":"rate_limited","secret":"synthetic-secret-12\\u0033"}', "withheld"),
    (b'{"error":"rate_limited","nested":{"password":"synthetic-secret-123"}}', "withheld"),
    (b'{"error":"rate_limited","error":"unauthorized"}', "invalid-json"),
    (b'{"error":"rate_limited","number":NaN}', "invalid-json"),
    (b"<html>private</html>", "invalid-json"), (b"", "empty"),
    (b"x" * (ERROR_BYTES + 1), "oversized"),
])
def test_untrusted_error_body_never_persisted(raw, state):
    result = http_failure(error(raw=raw, headers={"X-Request-ID": KEY}), KEY, now=NOW)
    assert result["error_body_state"] == state and result["business_error"] is None
    assert result["x_request_id"] is None and KEY not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_error_body_read_failure_retains_status_and_wait_header():
    class Broken(io.BytesIO):
        def read(self, n):
            raise TimeoutError("private endpoint")
    exc = HTTPError(CAPABILITIES, 429, "private", {"Retry-After": "12"}, Broken())
    result = http_failure(exc, KEY, now=NOW)
    assert result["http_status"] == 429 and result["retry_after_seconds"] == 12
    assert result["error_body_state"] == "unreadable" and exc.fp.closed


@pytest.mark.parametrize("exc,category", [
    (TimeoutError("private"), "timeout"),
    (URLError(TimeoutError("private")), "timeout"),
    (URLError(socket.gaierror(socket.EAI_AGAIN, "private")), "dns-temporary"),
    (URLError(socket.gaierror(socket.EAI_NONAME, "private")), "dns"),
    (URLError(ssl.SSLCertVerificationError("private")), "tls-certificate"),
    (ssl.SSLError("private"), "tls"), (ConnectionResetError("private"), "connection"),
    (IncompleteRead(b"private"), "incomplete-read"), (URLError("private"), "transport-unclassified"),
])
def test_transport_cause_is_typed_not_stringified(exc, category):
    facts = transport_failure(exc)
    assert facts["classification"] == category and "private" not in json.dumps(facts)


def test_rate_limit_wait_then_success_retains_original_failure(tmp_path):
    result, opener, client, waits = run(tmp_path, error(headers={"Retry-After": "5"}), Reply())
    assert waits == [5] and client.requests == 2 and client.last_error_meta is None
    assert result["status"] == "captured" and result["business_requests"] == 0
    assert result["attempts"][0]["failure"]["classification"] == "rate-limit"
    assert result["attempts"][0]["wait_completed"] and result["attempts"][1]["status"] == "captured"
    assert "error" not in result and result["total_wait_seconds"] == 5
    assert all(req.full_url == CAPABILITIES and req.get_method() == "GET" for req in opener.calls)
    assert (tmp_path / "out/capabilities.raw.json").exists()


@pytest.mark.parametrize("status,code,headers", [
    (429, "rate_limited", {}), (429, "rate_limited", {"Retry-After": "invalid"}),
    (401, "unauthorized", {"Retry-After": "1"}), (403, "forbidden", {}),
    (503, "data_source_unavailable", {"Retry-After": "1"}), (503, None, {}),
])
def test_stop_conditions_do_not_retry(tmp_path, status, code, headers):
    result, opener, _, waits = run(tmp_path, error(status, code, headers))
    assert len(opener.calls) == 1 and not waits and result["status"] == "failed"
    assert result["retry_outcome"] == "not-retryable"


def test_long_retry_after_defers_without_clamping(tmp_path):
    result, opener, _, waits = run(tmp_path, error(headers={"Retry-After": "121"}))
    assert len(opener.calls) == 1 and not waits
    assert result["retry_outcome"] == "wait-budget-exceeded"
    assert result["attempts"][0]["required_wait_seconds"] == 121


def test_total_wait_bound_is_shared_by_attempts(tmp_path):
    result, opener, _, waits = run(tmp_path, error(headers={"Retry-After": "80"}),
                                 error(headers={"Retry-After": "80"}))
    assert waits == [80] and len(opener.calls) == 2
    assert result["retry_outcome"] == "wait-budget-exceeded"


def test_three_attempt_maximum_preserves_all_failures(tmp_path):
    result, opener, _, waits = run(tmp_path, TimeoutError("private"), TimeoutError("private"),
                                 TimeoutError("private"))
    assert waits == [1, 2] and len(opener.calls) == 3
    assert result["retry_outcome"] == "attempts-exhausted" and len(result["attempts"]) == 3


def test_interruption_during_wait_leaves_original_attempt(tmp_path):
    def interrupt(_):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(tmp_path, error(headers={"Retry-After": "10"}), sleep=interrupt)
    saved = json.loads((tmp_path / "out/receipt.json").read_bytes())
    assert saved["attempts"][0]["error"] == "RELAY_HTTP_429"
    assert not saved["attempts"][0].get("wait_completed")
    assert saved["total_wait_seconds"] == 0


@pytest.mark.parametrize("options", [{"retries":3},{"retries":True},{"max_wait_seconds":-1},
                                      {"max_wait_seconds":121},{"max_wait_seconds":True}])
def test_invalid_retry_policy_rejected_before_files_and_requests(tmp_path, options):
    opener = Sequence()
    with pytest.raises(RecordError):
        capture(tmp_path / "out", code_sha=SHA, public_rights_confirmed=True,
                client=RelayHTTP(opener=opener, credential=KEY), env={}, **options)
    assert not opener.calls and not (tmp_path / "out").exists()


def test_business_transport_preserves_error_code_and_single_attempt():
    opener = Sequence(error(headers={"Retry-After": "1"}))
    client = RelayHTTP(opener=opener, credential=KEY)
    with pytest.raises(RecordError, match="^RELAY_HTTP_429$"):
        client.request("income", {"ts_code":"600519.SH","period":"20251231","fields":"ts_code","limit":"5"})
    assert len(opener.calls) == 1 and client.last_response_meta is None
    assert client.last_response_bytes is None and client.last_error_meta["retry_after_seconds"] == 1


def test_success_transport_still_has_exact_original_four_fields():
    params = {"ts_code":"600519.SH","period":"20251231","fields":"ts_code","limit":"5"}
    opener = Sequence(Reply(b'{"code":0,"data":{}}', ROOT + "/income?" + urlencode(params)))
    client = RelayHTTP(opener=opener, credential=KEY)
    assert client.request("income", params)["code"] == 0
    assert set(client.last_response_meta) == {"http_status","x_request_id","x_cache","wire_sha256"}
    assert client.last_error_meta is None


@pytest.mark.parametrize("exc,category", [
    (TimeoutError("private body"), "timeout"),
    (IncompleteRead(b"private partial body"), "incomplete-read"),
    (ConnectionResetError("private body"), "connection"),
])
def test_http200_body_failure_retains_observed_headers_without_a_success(exc, category):
    reply = UnreadableReply(exc, {"Retry-After": "45", "X-Request-ID": "request-body",
        "X-Cache": "MISS", "X-RateLimit-Remaining": "7", "X-RateLimit-IP-Remaining": "0",
        "Set-Cookie": "private cookie"})
    client = RelayHTTP(opener=Sequence(reply), credential=KEY)
    with pytest.raises(RecordError, match="^RELAY_HTTP_UNAVAILABLE$"):
        client.capabilities()
    failure = client.last_error_meta
    assert failure["http_status"] == 200 and failure["classification"] == category
    assert failure["retry_after_seconds"] == 45 and failure["error_body_state"] == "unreadable"
    assert failure["x_request_id"] == "request-body" and failure["x_cache"] == "MISS"
    assert datetime.fromisoformat(failure["received_at"]).utcoffset() is not None
    assert client.last_header_diagnostics["key_remaining"] == {"state": "valid", "remaining": 7}
    assert client.last_header_diagnostics["ip_remaining"] == {"state": "valid", "remaining": 0}
    assert client.last_response_meta is client.last_response_bytes is None and reply.closed
    assert "private" not in json.dumps(failure) and KEY not in json.dumps(failure)


def test_body_failure_headers_do_not_leak_into_the_next_connection_failure():
    first = UnreadableReply(TimeoutError("private"), {"Retry-After": "45", "X-Request-ID": KEY})
    client = RelayHTTP(opener=Sequence(first, TimeoutError("private connection")), credential=KEY)
    with pytest.raises(RecordError):
        client.capabilities()
    assert client.last_error_meta["http_status"] == 200 and client.last_error_meta["x_request_id"] is None
    with pytest.raises(RecordError):
        client.capabilities()
    assert client.last_error_meta == {"http_status": None, "classification": "timeout",
                                    "retry_after_state": "missing", "retry_after_seconds": None}
    assert client.last_header_diagnostics is None and client.requests == 2


@pytest.mark.parametrize("header,reason", [("181", "wait-budget"),
    ("invalid", "retry-after-unusable"), (KEY, "retry-after-unusable")])
def test_body_failure_does_not_replace_an_unusable_or_long_wait_with_thirty_seconds(header, reason):
    client = RelayHTTP(opener=Sequence(UnreadableReply(TimeoutError("private"),
        {"Retry-After": header})), credential=KEY)
    waits, checkpoints = [], []
    session = RetryingRelay(client, sleep=waits.append, clock=lambda: NOW.isoformat())
    with pytest.raises(RecordError, match="^RELAY_HTTP_UNAVAILABLE$"):
        session.run(client.capabilities, continuation=new_retry_state(),
                    checkpoint=lambda value: checkpoints.append(deepcopy(value)))
    saved = validate_retry_state(checkpoints[-1])
    assert saved["phase"] == "failed" and saved["decision"]["reason"] == reason
    assert saved["attempts"][0]["failure"]["http_status"] == 200
    assert saved["wait_budget_seconds"] == 0 and waits == [] and client.requests == 1
    assert KEY not in json.dumps(saved)


def test_body_failure_wait_and_header_facts_resume_through_the_original_checkpoint():
    clock, waits, checkpoints = [NOW], [], []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += timedelta(seconds=seconds)
    client = RelayHTTP(opener=Sequence(UnreadableReply(TimeoutError("private"),
        {"Retry-After": "45"}), Reply()), credential=KEY)
    session = RetryingRelay(client, sleep=sleep, monotonic=lambda: (clock[0] - NOW).total_seconds(),
        clock=lambda: clock[0].isoformat(), deadline=40)
    with pytest.raises(RecordError, match="^RELAY_HTTP_UNAVAILABLE$"):
        session.run(client.capabilities, continuation=new_retry_state(),
                    checkpoint=lambda value: checkpoints.append(deepcopy(value)))
    original = deepcopy(validate_retry_state(checkpoints[-1]))
    assert original["phase"] == "paused" and original["wait_budget_seconds"] == 45
    assert original["attempts"][0]["failure"]["http_status"] == 200
    assert waits == [] and client.requests == 1
    clock[0] += timedelta(seconds=10)
    session = RetryingRelay(client, sleep=sleep, monotonic=lambda: (clock[0] - NOW).total_seconds(),
        clock=lambda: clock[0].isoformat(), deadline=600)
    assert session.run(client.capabilities, continuation=original,
        checkpoint=lambda value: checkpoints.append(deepcopy(value)))["apis"]["income"]["enabled"]
    restored = validate_retry_state(checkpoints[-1])
    assert restored["phase"] == "response" and restored["wait_budget_seconds"] == 45
    assert [a["attempt"] for a in restored["attempts"]] == [1, 2] and waits == [35]
    assert restored["attempts"][0]["completed_wait_seconds"] == 35 and client.requests == 2
    assert original["attempts"][0]["completed_wait_seconds"] == 0


def test_four_body_failures_still_exhaust_the_original_page_and_cannot_resume():
    client = RelayHTTP(opener=Sequence(*(UnreadableReply(TimeoutError("private"), {})
        for _ in range(4))), credential=KEY)
    waits, checkpoints = [], []
    session = RetryingRelay(client, sleep=waits.append, clock=lambda: NOW.isoformat())
    with pytest.raises(RecordError, match="^RELAY_HTTP_UNAVAILABLE$"):
        session.run(client.capabilities, continuation=new_retry_state(),
                    checkpoint=lambda value: checkpoints.append(deepcopy(value)))
    saved = validate_retry_state(checkpoints[-1])
    assert saved["phase"] == "failed" and saved["decision"]["reason"] == "attempt-limit"
    assert waits == [30, 30, 30] and saved["wait_budget_seconds"] == 90
    with pytest.raises(RecordError, match="^RELAY_CHECKPOINT_NOT_RESUMABLE$"):
        session.run(client.capabilities, continuation=saved, checkpoint=lambda _: None)
    assert client.requests == 4


@pytest.mark.parametrize("raw,code", [(b"not-json", "RELAY_JSON_INVALID"),
    (b'{"key":"synthetic-secret-123"}', "RELAY_SECRET_REFLECTED")])
def test_observed_wait_header_does_not_turn_content_rejection_into_a_transport_retry(raw, code):
    reply = Reply(raw)
    reply.headers["Retry-After"] = "45"
    client = RelayHTTP(opener=Sequence(reply), credential=KEY)
    waits = []
    session = RetryingRelay(client, sleep=waits.append, clock=lambda: NOW.isoformat())
    with pytest.raises(RecordError, match="^" + code + "$"):
        session.run(client.capabilities)
    assert client.requests == 1 and waits == [] and client.last_error_meta is None
    assert session.last_retry_decision["reason"] == "not-retryable"


def test_workflow_retries_opt_in_and_secret_only_in_capture_step():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/relay-capabilities.yml").read_text()
    assert "default: '0'" in text and '"$METADATA_RETRIES"' in text
    assert text.count("secrets.TUSHARE_PROXY_API_KEY") == 1
    assert "contents: write" not in text and "schedule:" not in text
