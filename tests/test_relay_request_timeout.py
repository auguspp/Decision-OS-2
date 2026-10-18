"""Socket windows and the original finite checkpoint budget, with no source I/O."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from decision_kernel.application.relay_capture_checkpoint import CheckpointStateError
from decision_kernel.application.research_sources import capture_registered, validate_plan
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from test_relay_capture_checkpoint import Clock, empty, run as old_capture
from test_research_sources import Opener, body, busy, plan, registry, request


class TimedOpener(Opener):
    def __init__(self, values):
        super().__init__(values)
        self.timeouts = []

    def open(self, req, timeout):
        self.timeouts.append(timeout)
        return super().open(req, timeout)


def run_capture(*values, saved=None, use=None, seconds=0, budget=600):
    use = use or validate_plan(plan(), registry())["requests"][0]
    opener = TimedOpener(values)
    client = RelayHTTP(opener=opener, credential="synthetic-timeout-key",
                       request_timeout_seconds=90)
    clock, states = Clock(seconds), []
    session = RetryingRelay(client, sleep=clock.sleep, monotonic=clock.monotonic,
                           clock=clock.now, deadline=seconds + budget)
    capture = capture_registered(use, registry(), session, max_pages=2, checkpoint=saved,
                                 checkpoint_writer=lambda value: states.append(deepcopy(value)))
    return capture, states, client, opener, clock


@pytest.mark.parametrize("timeout", [30, 60, 90])
def test_transport_and_retry_share_one_read_only_socket_window(timeout):
    opener = TimedOpener([body()])
    client = RelayHTTP(opener=opener, credential="synthetic-timeout-key",
                       request_timeout_seconds=timeout)
    session = RetryingRelay(client, monotonic=lambda: 0, deadline=timeout)
    use = validate_plan(plan(), registry())["requests"][0]
    assert session.run(lambda: client.request_registered(registry(), use["api"], use["parameters"])) == body()
    assert opener.timeouts == [timeout]
    assert session.request_timeout_seconds == client.request_timeout_seconds == timeout
    with pytest.raises(AttributeError):
        client.request_timeout_seconds = 60
    with pytest.raises(AttributeError):
        session.request_timeout_seconds = 60


@pytest.mark.parametrize("value", [True, False, None, "90", 90.0, 29, 91])
def test_invalid_transport_or_present_client_window_is_rejected(value):
    opener = TimedOpener([])
    with pytest.raises(RecordError, match="RELAY_REQUEST_TIMEOUT_INVALID"):
        RelayHTTP(opener=opener, request_timeout_seconds=value)
    with pytest.raises(RecordError, match="RELAY_REQUEST_TIMEOUT_INVALID"):
        RetryingRelay(SimpleNamespace(request_timeout_seconds=value))
    assert opener.calls == []


def test_legacy_admission_uses_an_explicit_client_window():
    client, calls = SimpleNamespace(request_timeout_seconds=90), []
    session = RetryingRelay(client, monotonic=lambda: 0, deadline=89)
    with pytest.raises(RecordError, match="RELAY_RUN_DEADLINE"):
        session.run(lambda: calls.append("request") or body())
    assert calls == [] and session.last_attempts == []
    session = RetryingRelay(client, monotonic=lambda: 0, deadline=90)
    assert session.run(lambda: calls.append("request") or body()) == body()
    assert calls == ["request"]


def test_omitted_transport_window_and_older_fake_client_keep_thirty_seconds():
    opener = TimedOpener([body()])
    client = RelayHTTP(opener=opener, credential="synthetic-timeout-key")
    use = validate_plan(plan(), registry())["requests"][0]
    RetryingRelay(client, monotonic=lambda: 0, deadline=30).run(
        lambda: client.request_registered(registry(), use["api"], use["parameters"]))
    assert client.request_timeout_seconds == 30 and opener.timeouts == [30]
    calls = []
    session = RetryingRelay(SimpleNamespace(), monotonic=lambda: 0, deadline=29)
    with pytest.raises(RecordError, match="RELAY_RUN_DEADLINE"):
        session.run(lambda: calls.append("request") or body())
    assert session.request_timeout_seconds == 30 and calls == []
    session = RetryingRelay(SimpleNamespace(), monotonic=lambda: 0, deadline=30)
    assert session.run(lambda: calls.append("request") or body()) == body()
    assert calls == ["request"]


def test_checkpoint_admission_reserves_the_actual_ninety_second_socket_window():
    paused, saved, client, opener, _ = run_capture(budget=89)
    assert not paused["complete"] and paused["error"] == "RELAY_RUN_DEADLINE"
    assert client.requests == 0 and opener.timeouts == []
    assert saved[-1]["control"]["retry"]["attempts"] == []
    completed, _, client, opener, _ = run_capture(body(), saved=saved[-1], budget=90)
    assert completed["complete"] and client.requests == 1 and opener.timeouts == [90]


@pytest.mark.parametrize("budget, expected_calls, waits", [(119, 1, []), (120, 2, [30])])
def test_retry_decision_keeps_thirty_second_backoff_and_reserves_ninety(budget, expected_calls, waits):
    opener, clock = TimedOpener([busy(504), body()]), Clock()
    client = RelayHTTP(opener=opener, credential="synthetic-timeout-key",
                       request_timeout_seconds=90)
    session = RetryingRelay(client, sleep=clock.sleep, monotonic=clock.monotonic,
                           clock=clock.now, deadline=budget)
    use = validate_plan(plan(), registry())["requests"][0]
    call = lambda: session.run(lambda: client.request_registered(registry(), use["api"], use["parameters"]))
    if expected_calls == 1:
        with pytest.raises(RecordError, match="RELAY_HTTP_504"):
            call()
        assert session.last_retry_decision == {"reason": "run-deadline", "required_wait_seconds": 30}
    else:
        assert call() == body()
    assert client.requests == expected_calls and opener.timeouts == [90] * expected_calls
    assert clock.waits == waits


@pytest.mark.parametrize("budget, expected_calls, waits", [(109, 0, []), (110, 1, [20])])
def test_older_pending_wait_resumes_only_with_wait_plus_actual_request_window(budget, expected_calls, waits):
    _, old, _, _, _ = old_capture(busy(503), budget=40, max_pages=2)
    original = deepcopy(old[-1])
    capture, states, client, opener, clock = run_capture(
        body(), saved=original, seconds=10, budget=budget)
    assert client.requests == expected_calls and opener.timeouts == [90] * expected_calls
    assert clock.waits == waits and original == old[-1]
    control = states[-1]["control"]
    assert control["policy"] == {"max_attempts": 4, "max_wait_seconds": 180}
    assert control["retry"]["wait_budget_seconds"] == 30
    if expected_calls:
        assert capture["complete"]
        attempts = capture["pages"][0]["attempts"]
        assert [a["attempt"] for a in attempts] == [1, 2]
        assert attempts[0]["checkpoint_wait_seconds"] == 30
        assert attempts[0]["completed_wait_seconds"] == 20
    else:
        assert capture["error"] == "RELAY_RUN_DEADLINE"
        assert control["retry"]["pending_wait_seconds"] == 30
        assert control["retry"]["attempts"] == original["control"]["retry"]["attempts"]


@pytest.mark.parametrize("saved_at", [510, 511])
def test_persisted_reservation_must_leave_the_full_socket_window(saved_at):
    use = validate_plan(plan(), registry())["requests"][0]
    opener, clock, states = TimedOpener([body()]), Clock(), []
    client = RelayHTTP(opener=opener, credential="synthetic-timeout-key",
                       request_timeout_seconds=90)
    session = RetryingRelay(client, sleep=clock.sleep, monotonic=clock.monotonic,
                           clock=clock.now, deadline=600)

    def writer(value):
        states.append(deepcopy(value))
        if value["control"]["retry"]["phase"] == "in-flight":
            clock.seconds = saved_at

    call = lambda: capture_registered(use, registry(), session, max_pages=2, checkpoint_writer=writer)
    if saved_at == 511:
        with pytest.raises(CheckpointStateError, match="RELAY_CHECKPOINT_ADMISSION_EXPIRED"):
            call()
        assert client.requests == 0 and opener.timeouts == []
        assert len(states) == 1 and states[0]["control"]["retry"]["phase"] == "in-flight"
        assert states[0]["control"]["retry"]["reserved_attempt"] == 1
        with pytest.raises(RecordError, match="INFLIGHT"):
            run_capture(saved=states[0], seconds=700)
    else:
        assert call()["complete"] and client.requests == 1 and opener.timeouts == [90]


def test_new_socket_window_cannot_refill_old_attempts_or_restart_terminal():
    _, old, first, _, _ = old_capture(*(busy(504) for _ in range(3)), budget=100, max_pages=2)
    original = deepcopy(old[-1])
    capture, states, client, opener, clock = run_capture(busy(504), saved=original, seconds=60)
    assert first.requests == 3 and client.requests == 1 and opener.timeouts == [90]
    assert clock.waits == [30] and original == old[-1]
    assert [a["attempt"] for a in capture["attempts"]] == [1, 2, 3, 4]
    assert [a["completed_wait_seconds"] for a in capture["attempts"]] == [30, 30, 30, 0]
    control = states[-1]["control"]
    assert control["state"] == "terminal" and control["retry"]["decision"]["reason"] == "attempt-limit"
    assert control["policy"] == {"max_attempts": 4, "max_wait_seconds": 180}
    assert control["retry"]["wait_budget_seconds"] == 90
    final, writes, later, opener, _ = run_capture(saved=states[-1], seconds=1000)
    assert final == capture and later.requests == 0 and opener.timeouts == [] and writes == []


def test_longer_socket_window_resumes_next_page_without_repeating_a_saved_success():
    use = validate_plan(plan(request(limit=1)), registry())["requests"][0]
    _, old, first, _, _ = old_capture(TimeoutError("synthetic"), body(), use=use, max_pages=2,
                                      pause=lambda op: "STORAGE_BUDGET" if len(op.calls) == 2 else None)
    original = deepcopy(old[-1])
    assert first.requests == 2 and original["control"]["state"] == "paused"
    assert original["control"]["page_number"] == 2 and original["control"]["retry"] is None
    page = deepcopy(original["capture"]["pages"][0])
    assert [a["attempt"] for a in page["attempts"]] == [1, 2]
    capture, _, client, opener, _ = run_capture(empty(), saved=original, use=use, seconds=100)
    assert capture["complete"] and client.requests == 1 and opener.timeouts == [90]
    assert "offset=1" in opener.calls[0].full_url
    assert capture["pages"][0] == page and original == old[-1]


def test_collection_uses_one_ninety_second_default_for_all_queries(monkeypatch):
    import decision_kernel.application.market_collection as collection
    from test_market_collection_inputs import CollectionOpener
    from test_market_collection_sync import END, memory, plan as collection_plan, run as sync

    class CollectionWire(CollectionOpener):
        def __init__(self):
            super().__init__(start=END, end=END)
            self.timeouts = []

        def open(self, req, timeout):
            self.timeouts.append(timeout)
            return super().open(req, timeout)

    opener, clients = CollectionWire(), []

    def client_factory(**kwargs):
        client = RelayHTTP(opener=opener, credential="synthetic-timeout-key", **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(collection, "RelayHTTP", client_factory)
    _, store, fresh = memory()
    result = sync(collection_plan(start=END), store, fresh)
    assert result["status"] == "complete"
    assert len(clients) == 1 and {api for api, _ in opener.calls} == {
        "trade_cal", "stock_basic", "daily", "adj_factor", "suspend_d"}
    assert opener.timeouts == [90] * len(opener.calls)
