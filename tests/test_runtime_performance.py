"""Optional bounded observations never become source or write-effect evidence."""
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure import runtime_performance as perf
from decision_kernel.infrastructure import ftshare_http, github_store, relay_http
from test_ftshare_http import KEY, Reply, SINCE, UNTIL
from test_research_sources import body, registry, request


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


def test_nested_owners_keep_one_ordered_session_and_inclusive_observations():
    clock = Clock()
    with perf.performance_session(clock=clock):
        with perf.measure_phase("daily.execute"):
            with perf.measure_phase("master.unit"):
                with perf.measure_phase("checkpoint.save"):
                    perf.count_event("github_api_requests")
                    clock.advance(7)
                    with perf.measure_phase("checkpoint.readback"):
                        perf.count_event("github_api_requests")
                        clock.advance(3)
                with perf.performance_session(clock=lambda: pytest.fail("nested session reset")):
                    perf.count_event("relay_requests")
                    with perf.measure_phase("relay.http"):
                        perf.count_event("relay_wire_attempts")
                        clock.advance(2)
                        with perf.measure_phase("relay.body"):
                            clock.advance(1)
                    with perf.measure_phase("relay.json"):
                        clock.advance(1)
        first = perf.performance_receipt()
        frozen = deepcopy(first)
        with perf.measure_phase("ft.unit"):
            perf.count_event("ft_requests")
        final = perf.performance_receipt()
    assert perf.performance_receipt() is None and first == frozen
    assert final["counts"]["relay_requests"] == final["counts"]["relay_wire_attempts"] == 1
    assert final["counts"]["ft_requests"] == 1
    phases = first["phases"]
    assert phases["daily.execute"]["observed_seconds"] == phases["master.unit"]["observed_seconds"] == 14
    assert phases["checkpoint.save"]["observed_seconds"] == 10
    assert phases["checkpoint.readback"]["observed_seconds"] == 3
    assert phases["relay.http"]["observed_seconds"] == 3
    assert phases["relay.body"]["observed_seconds"] == phases["relay.json"]["observed_seconds"] == 1
    assert phases["checkpoint.save"]["count_deltas"]["github_api_requests"] == 2
    assert phases["checkpoint.readback"]["count_deltas"]["github_api_requests"] == 1
    samples = first["samples"]
    assert [row["sequence"] for row in samples] == sorted(row["sequence"] for row in samples)
    unit = next(row for row in samples if row["phase"] == "master.unit")
    assert all(row["unit_sequence"] == unit["sequence"] for row in samples[1:])
    assert unit["count_deltas"]["github_api_requests"] == 2
    assert "not-additive" in first["duration_semantics"]
    assert "not-write-effect" in first["outcome_semantics"]


@pytest.mark.parametrize("bad_clock", [lambda: float("nan"), lambda: float("inf"), lambda: None,
    lambda: (_ for _ in ()).throw(ValueError("private clock error"))])
def test_bad_observation_clock_preserves_the_same_business_exception_and_unknown(bad_clock):
    failure = UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    with perf.performance_session(clock=bad_clock):
        with pytest.raises(UnknownWrite) as caught:
            with perf.measure_phase("store.cas"):
                raise failure
        value = perf.performance_receipt()
    assert caught.value is failure and value["elapsed_seconds"] is None
    assert value["phases"]["store.cas"]["raised"] == 1
    assert value["phases"]["store.cas"]["observed_seconds"] is None
    assert value["phases"]["store.cas"]["p95_sample_seconds"] is None
    assert value["samples"][0]["elapsed_seconds"] is None
    assert value["clock_errors"] > 0 and "private clock error" not in json.dumps(value)


def test_diagnostic_internal_failure_does_not_mask_business_or_keep_a_unit_context(monkeypatch):
    original = perf._Performance.leave
    monkeypatch.setattr(perf._Performance, "leave", lambda *a: (_ for _ in ()).throw(ValueError("private")))
    failure = RecordError("SYNTHETIC_BUSINESS_FAILURE")
    with perf.performance_session(clock=lambda: 0):
        with pytest.raises(RecordError) as caught:
            with perf.measure_phase("master.unit"):
                raise failure
        monkeypatch.setattr(perf._Performance, "leave", original)
        with perf.measure_phase("relay.http"):
            pass
        result = perf.performance_receipt()
    assert caught.value is failure and result["observer_errors"] == 1
    assert result["samples"][-1]["unit_sequence"] is None
    assert "private" not in json.dumps(result)


def test_capacity_and_fixed_labels_bound_all_samples_and_never_collect_arbitrary_text():
    clock = Clock()
    with perf.performance_session(clock=clock):
        for label in ("https://private.invalid/token", ["private header"], {"body": "secret"}):
            with perf.measure_phase(label):
                perf.count_event(label)
        for label in sorted(perf.PHASES):
            for number in range(40):
                with perf.measure_phase(label):
                    for count in perf.COUNTERS:
                        perf.count_event(count)
                    clock.advance(number + 1)
        value = perf.performance_receipt()
    raw = encoded(value)
    assert len(raw) <= perf.MAX_RECEIPT_BYTES == 128 * 1024
    assert len(value["samples"]) <= 512 and value["dropped_ordered_samples"] > 0
    assert value["truncated"] and value["invalid_labels"] == 6
    assert not any(word in raw for word in (b"private", b"header", b"secret", b"https://"))
    for row in value["phases"].values():
        assert row["sample_count"] == 32 and row["samples_truncated"]
        assert row["p50_sample_seconds"] == 16 and row["p95_sample_seconds"] == 31
    assert "return-and-raise-mixed" in value["percentile_semantics"]


def test_optional_receipt_annotation_does_not_exceed_original_limit_or_mutate_input():
    old = {"schema": "daily-market-runtime-v1", "status": "read"}
    assert perf.annotate_performance(old) is old
    with perf.performance_session(clock=lambda: 0):
        full = perf.annotate_performance(old)
        short = perf.annotate_performance(old, max_bytes=256)
        absent = perf.annotate_performance(old, max_bytes=len(encoded(old)))
    assert "performance" not in old and absent is old
    assert full["performance"]["schema"] == "daily-runtime-performance-v1"
    assert short["performance"]["status"] == "omitted-for-receipt-bound"
    assert len(encoded(short)) <= 256


class TimedReply(Reply):
    def __init__(self, clock, raw):
        super().__init__(raw)
        self.clock = clock

    def read(self, bound):
        self.clock.advance(3)
        return super().read(bound)


class TimedOpener:
    def __init__(self, clock, raw):
        self.clock, self.raw, self.calls = clock, raw, []

    def open(self, request, timeout):
        self.calls.append(request)
        self.clock.advance(5)
        response = TimedReply(self.clock, self.raw)
        response.url = request.full_url
        return response


@pytest.mark.parametrize("owner", ["relay", "ft", "github"])
def test_actual_http_body_and_parser_have_separate_monotonic_boundaries(owner, monkeypatch):
    clock = Clock()
    raw = encoded(body()) if owner == "relay" else b'{"code":200,"data":[]}'
    opener = TimedOpener(clock, raw)
    if owner == "relay":
        client = relay_http.RelayHTTP(opener=opener, credential=KEY)
        invoke = lambda: client.request_registered(registry(), "daily", request()["parameters"])
        original = relay_http.json.loads
        # Patch this transport's parser only, not the registry's JSON module.
        monkeypatch.setattr(relay_http, "json", SimpleNamespace(dumps=json.dumps,
            loads=lambda *a, **kw: (clock.advance(2), original(*a, **kw))[1]))
    elif owner == "ft":
        client = ftshare_http.FTShareHTTP(opener=opener, credential=KEY)
        invoke = lambda: client.candlesticks("600000.SH", since=SINCE, until=UNTIL)
        original = ftshare_http._json_payload
        monkeypatch.setattr(ftshare_http, "_json_payload", lambda raw: (clock.advance(2), original(raw))[1])
    else:
        client = github_store.GitHubAPI(KEY)
        client._opener = opener
        invoke = lambda: client.request("GET", "/repos/example/store")
        original = github_store._json
        monkeypatch.setattr(github_store, "_json", lambda raw: (clock.advance(2), original(raw))[1])
    with perf.performance_session(clock=clock):
        with perf.measure_phase("master.unit"):
            with perf.measure_phase("checkpoint.save"):
                clock.advance(100)  # Synchronous custody is outside actual HTTP.
            invoke()
        result = perf.performance_receipt()
    phases = result["phases"]
    wire = "github.get" if owner == "github" else owner + ".http"
    assert phases[wire]["observed_seconds"] == 8
    assert phases[owner + ".body"]["observed_seconds"] == 3
    assert phases[owner + ".json"]["observed_seconds"] == 2
    assert len(opener.calls) == 1
    assert phases["master.unit"]["observed_seconds"] == 110
    if owner == "github":
        assert client._requests == result["counts"]["github_api_requests"] == result["counts"]["github_get_requests"] == 1
    else:
        assert client.requests == result["counts"][owner + "_requests"] == result["counts"][owner + "_wire_attempts"] == 1
    assert KEY not in json.dumps(result) and "example/store" not in json.dumps(result)


@pytest.mark.parametrize("failure", [URLError(TimeoutError("private socket")),
    HTTPError("https://private.invalid", 429, "private body", {}, BytesIO(b"secret"))])
def test_github_post_failure_stays_unknown_and_rejected_budget_never_dispatches(failure):
    api = github_store.GitHubAPI(KEY)
    calls = []
    class Fail:
        def open(self, request, timeout):
            calls.append(request)
            raise failure
    api._opener = Fail()
    with perf.performance_session(clock=lambda: 0):
        with pytest.raises(UnknownWrite, match="REMOTE_WRITE_UNCONFIRMED"):
            api.request("POST", "/graphql", {"query": "synthetic"})
        api._requests = github_store.MAX_REQUESTS
        with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
            api.request("POST", "/graphql", {"query": "synthetic"})
        result = perf.performance_receipt()
    assert len(calls) == 1 and api._requests == github_store.MAX_REQUESTS + 1
    assert result["counts"]["github_api_requests"] == 2
    assert result["counts"]["github_post_requests"] == 1
    assert result["phases"]["github.post"]["raised"] == 1
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)
