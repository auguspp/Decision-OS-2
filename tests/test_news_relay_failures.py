"""The news adapter preserves the existing transport's redacted failure facts."""
from copy import deepcopy
from email.message import Message
from io import BytesIO
import json
import socket
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.application.news_rolling_index import rolling_index, read_index
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.news_capture import capture_relay_news
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import news_cli
from decision_kernel.observations.news_windows import verify_reading

KEY = "synthetic-news-test-key"
AT = "2026-10-16T08:00:00+00:00"


class Reply:
    status = 200
    headers = {"Content-Type": "application/json"}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def geturl(self): return self.url
    def read(self, n):
        return encoded({"code": 0, "api_name": "major_news", "data": {
            "fields": ["title", "pub_time", "src_site"], "items": []}})[:n]


class Opener:
    def __init__(self, *events): self.events, self.calls = list(events), []
    def open(self, request, timeout):
        self.calls.append(request.full_url)
        event = self.events.pop(0)
        if isinstance(event, BaseException): raise event
        event.url = request.full_url
        return event


def error(status, business=None, *, text=None, headers=None):
    h = Message()
    for name, value in (headers or {}).items(): h[name] = value
    raw = encoded({"error": business, "message": text or "synthetic prose never retained"})
    return HTTPError("https://pcd.mobcvb.cn/tushare/pro/major_news", status,
                     "synthetic exception prose", h, BytesIO(raw))


def capture(client):
    return capture_relay_news("major_news", source="财联社", start="2026-10-15 09:00:00",
                              end="2026-10-15 10:00:00", observed_at=AT, transport=client)


@pytest.mark.parametrize("status,business,category", [
    (503, None, "http-unclassified"),
    (503, "data_source_unavailable", "source-unavailable"),
    (503, "upstream_pool_exhausted", "temporary-upstream"),
    (400, "invalid_params", "request-rejected"),
    (400, "data_source_unavailable", "source-unavailable"),
    (403, "forbidden", "auth-or-entitlement"),
    (429, "rate_limited", "rate-limit"),
])
def test_shared_http_failure_is_preserved_without_new_attempts(status, business, category):
    opener = Opener(error(status, business, headers={"Retry-After": "900"}))
    client = RelayHTTP(opener=opener, credential=KEY)
    reading = capture(client)
    receipt = reading["source_outcomes"][0]["capture"]
    assert client.requests == len(opener.calls) == 1
    assert reading["observations"] == [] and reading["source_outcomes"][0]["status"] == "unavailable"
    assert receipt["error_code"] == "RELAY_HTTP_" + str(status)
    assert receipt["failure"] == client.last_error_meta
    assert receipt["failure"]["classification"] == category
    assert receipt["failure"]["business_error"] == business
    assert receipt["failure"]["retry_after_seconds"] == 900
    assert receipt["source_body_retained"] is False and receipt["response_sha256"] is None
    saved = deepcopy(receipt["failure"])
    client.last_error_meta["classification"] = "changed-in-client"
    assert receipt["failure"] == saved
    assert "synthetic prose" not in encoded(reading).decode() and KEY not in encoded(reading).decode()
    assert verify_reading(reading) == reading


def test_reflected_secret_and_arbitrary_error_prose_remain_withheld():
    opener = Opener(error(400, "invalid_params", text=KEY,
                          headers={"X-Request-ID": KEY, "Retry-After": KEY}))
    client = RelayHTTP(opener=opener, credential=KEY)
    value = capture(client)
    failure = value["source_outcomes"][0]["capture"]["failure"]
    assert failure["error_body_state"] == "withheld" and failure["business_error"] is None
    assert failure["x_request_id"] is None and failure["retry_after_state"] == "withheld"
    assert KEY not in encoded(value).decode()


def test_typed_transport_failure_is_not_guessed_from_http_status():
    client = RelayHTTP(opener=Opener(URLError(socket.gaierror(socket.EAI_AGAIN, "synthetic"))),
                       credential=KEY)
    receipt = capture(client)["source_outcomes"][0]["capture"]
    assert receipt["error_code"] == "RELAY_HTTP_UNAVAILABLE"
    assert receipt["failure"]["classification"] == "dns-temporary"
    assert receipt["failure"]["http_status"] is None and client.requests == 1


def test_reused_transport_success_clears_old_failure_without_rewriting_old_receipt():
    client = RelayHTTP(opener=Opener(error(503, "upstream_pool_exhausted"), Reply()), credential=KEY)
    failed = capture(client)
    again = capture(client)
    assert client.requests == 2 and client.last_error_meta is None
    assert "failure" not in again["source_outcomes"][0]["capture"]
    assert again["source_outcomes"][0]["status"] == "captured"
    assert failed["source_outcomes"][0]["capture"]["failure"]["business_error"] == "upstream_pool_exhausted"
    verify_reading(failed)


def test_client_without_diagnostic_contract_keeps_original_failure_shape():
    class Client:
        def _get_json(self, *args, **kwargs): raise RecordError("RELAY_CREDENTIAL_REQUIRED")
    value = capture(Client())
    receipt = value["source_outcomes"][0]["capture"]
    assert "failure" not in receipt and receipt["error_code"] == "RELAY_CREDENTIAL_REQUIRED"
    verify_reading(value)


def test_failure_survives_zero_filter_source_custody_and_exact_rolling_read(tmp_path, capsys):
    value = capture(RelayHTTP(opener=Opener(error(400, "invalid_params")), credential=KEY))
    path = tmp_path / "window.json"
    path.write_bytes(encoded(value))
    assert news_cli.main(["read", "--window", str(path), "--source", "not-selected"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["source_outcomes"] == value["source_outcomes"]
    assert page["source_outcomes_scope"] == "whole-input-reading"
    root = tmp_path / "news.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    ref = store.put_source("failure", SourceInput(source_id="failure", kind="content",
        title="Synthetic Relay failure", origin="synthetic://news", text=encoded(value).decode(), custody="provided"))
    cold = VersionedStore(GitStore(root))
    assert json.loads(cold.read_source(ref)["data"]) == value
    index = rolling_index(cold.read_source, [ref], as_of="2026-10-16T09:00:00+00:00")
    result = read_index(cold.read_source, index, source="not-selected")
    assert result["rows"] == [] and result["coverage"]["failed_source_windows"] == 1
    assert result["source_outcomes"][0]["outcome"] == value["source_outcomes"][0]
