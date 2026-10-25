"""Live execution revocation after source HTTP preserves actual durable ownership."""
from copy import deepcopy
import socket

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.ftshare_collection import latest_ftshare_reference, restore_ftshare_collection
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure import daily_execution as execution
from decision_kernel.infrastructure.git_store import GitStore
from test_daily_execution import Native
from test_daily_market_schedule import WindowHost
from test_market_relay_inputs import REGISTRY


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Recovery tests use only synthetic native/provider responses and local Git")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def cycle(host):
    return daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])


def source_counts(host):
    return (sum(client.requests for client in host.calendar_clients),
        sum(client.requests for client in host.price_clients))


def later_native_run(native, approved, at):
    """Restore the original policy, then admit a distinct native run on new main."""
    head = "e" * 40
    native.advance(head, policy=approved)
    later = Native(pinned=True)
    later.main = deepcopy(native.main)
    later.policies = deepcopy(native.policies)
    later.publications = deepcopy(native.publications)
    later.workflow_sha = head
    later.selected.update(code_sha=head, run_id=8002)
    later.env.update(GITHUB_SHA=head, GITHUB_WORKFLOW_SHA=head, GITHUB_RUN_ID="8002")
    later.run.update(id=8002, head_sha=head, created_at=at)
    later.cis[head] = Native.ci(head, 103)
    request = later.request
    def next_run(method, path, payload=None):
        if path == execution.PREFIX + "/actions/runs/8002":
            assert method == "GET" and payload is None
            later.calls.append((method, path))
            return deepcopy(later.run)
        return request(method, path, payload)
    later.request = next_run
    return later, later.freeze()


@pytest.mark.parametrize("source", ["calendar", "price"])
def test_spent_response_is_not_replayed_after_execution_revocation_and_new_main(tmp_path, source):
    backend = GitStore.init_synthetic(tmp_path / "records.git")
    host = WindowHost(storage=(backend, VersionedStore(backend),
        lambda: VersionedStore(GitStore(backend.root))))
    native = Native(pinned=True)
    selected = native.freeze()
    approved = deepcopy(native.policies[native.workflow_sha])
    spent = {}

    def revoke_after_response(client):
        # The real fixture has returned its synthetic HTTP body. The original
        # reservation is already committed, but no response custody may follow.
        assert client.requests == 1 and client.last_response_bytes and not spent
        spent.update(head=backend.head(), cycle=cycle(host))
        disabled = deepcopy(approved)
        disabled["enabled"] = False
        native.advance(policy=disabled)

    def calendar():
        client = host.calendar()
        request = client.request_registered
        def returned(*args, **kwargs):
            value = request(*args, **kwargs)
            revoke_after_response(client)
            return value
        client.request_registered = returned
        return client

    def price():
        client = host.price()
        request = client.candlesticks
        def returned(*args, **kwargs):
            value = request(*args, **kwargs)
            revoke_after_response(client)
            return value
        client.candlesticks = returned
        return client

    guard = lambda api: execution.require_live_execution(native, selected)
    options = {"calendar_client_factory": calendar} if source == "calendar" else {"price_client_factory": price}
    first = host.run(guard=guard, read_guard=guard, **options)
    receipt = first["receipt"]
    expected = (1, 0) if source == "calendar" else (2, 1)
    assert source_counts(host) == expected
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"]) == expected
    assert receipt["source_requests"] == sum(expected)
    assert receipt["status"] == "blocked" and receipt["reason_code"] == "DAILY_EXECUTION_DISABLED"
    assert receipt["reading_reason_code"] == "DAILY_EXECUTION_DISABLED"
    # An independent Git reader sees precisely the last reservation. No raw
    # response, completion or substituted failure was committed after revocation.
    assert backend.head() == spent["head"] and cycle(host) == spent["cycle"]
    pending = cycle(host)
    if source == "calendar":
        row = pending["cycle"]["calendars"]["SSE"]
        assert row["status"] == "in-flight" and row["reserved_requests"] == 1 and row["reference"] is None
        assert pending["cycle"]["calendars"]["SZSE"]["status"] == "pending"
        assert host.price_clients == []
    else:
        reference = latest_ftshare_reference(host.fixture["fresh"](), REGISTRY,
            collection_id="ftshare-cn-eod", generation=pending["cycle"]["market_generation"])
        collection = restore_ftshare_collection(reference, REGISTRY, host.fixture["fresh"]())["collection"]
        inflight = [row for row in collection["units"].values() if row["status"] == "in-flight"]
        assert collection["reserved_requests"] == 1 and len(inflight) == 1
        assert inflight[0]["request_count"] == 1 and inflight[0]["reference"] is None
        assert receipt["price_execution"]["status"] == "persistence-unconfirmed"
        assert receipt["price_execution"]["next_run_can_resume"] is False

    host.now = "2026-10-20T12:10:00+00:00"
    later, resumed = later_native_run(native, approved, host.now)
    assert resumed["run_id"] != selected["run_id"] and resumed["workflow_sha"] != selected["workflow_sha"]
    assert resumed["code_sha"] == selected["code_sha"]
    for key in ("approval_ref", "policy_sha256"):
        assert resumed["runtime_version"][key] == selected["runtime_version"][key]
    assert execution.require_live_execution(later, resumed)["main_sha"] == later.workflow_sha
    clients = len(host.calendar_clients), len(host.price_clients)
    resumed_guard = lambda api: execution.require_live_execution(later, resumed)
    second = host.run(guard=resumed_guard, read_guard=resumed_guard)
    assert second["receipt"]["source_requests"] == 0 and source_counts(host) == expected
    assert (len(host.calendar_clients), len(host.price_clients)) == clients
    assert backend.head() == spent["head"] and cycle(host) == pending
    assert second["receipt"]["cycle_date"] == pending["cycle"]["date"] == "20261019"
    expected_reason = ("DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED" if source == "calendar"
        else "FTSHARE_IN_FLIGHT_REQUIRES_RECONCILIATION")
    assert second["receipt"]["reason_code"] == expected_reason
    assert second["receipt"]["status"] == ("persistence-unconfirmed" if source == "calendar" else "blocked")
