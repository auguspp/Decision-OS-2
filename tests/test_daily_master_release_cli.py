"""daily release 与并发门禁的离线 CLI 接线回归。"""
from copy import deepcopy
from dataclasses import replace
import json
import socket

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application import daily_master_refresh as masters
from decision_kernel.domain.records import Conflict, RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore
from decision_kernel.interfaces import daily_market_cli as cli
from test_daily_market import quota_headers
from test_daily_master_cycle import MasterHost
from test_daily_market_schedule import native_runtime
from test_github_transport import Response
from test_market_relay_inputs import REGISTRY
from test_market_snapshot import native
from test_runtime_publication import context, grant


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("禁止真实网络")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def pending(monkeypatch):
    host = MasterHost()
    retain = masters.retain_market_checkpoint
    def lost(*args, **kwargs):
        retain(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    with monkeypatch.context() as patch:
        patch.setattr(masters, "retain_market_checkpoint", lost)
        result = host.run()
    assert result["receipt"]["status"] == "persistence-unconfirmed", result
    return host, result


def test_release_uses_normal_reader_and_repeat_never_writes_or_collects(monkeypatch):
    host, original = pending(monkeypatch)
    item = host.cycle()
    calls = len(host.master_clients)
    released = host.run(release_master=True)
    receipt = released["receipt"]
    assert receipt["status"] == "released", receipt
    assert receipt["source_requests"] == 0 and receipt["health"]
    row, = receipt["master_releases"]
    assert row["status"] == "released" and row["attempts_charged"] == 1
    assert not row["terminal"] and row["reconciled_at"] == host.now
    assert released["control"] == original["control"]
    assert released["control"]["cycle_reference"] == item["reference"]
    assert released["control"]["generation"] == item["cycle"]["market_generation"]
    assert len(released["control"]["calendar_references"]) == 2
    before = host.fixture["backend"].head()
    def forbidden(*args, **kwargs):
        pytest.fail("重复裁定不能写入或启动来源")
    monkeypatch.setattr(host.fixture["backend"], "commit", forbidden)
    again = host.run(release_master=True, calendar_client_factory=forbidden,
        master_client_factory=forbidden, price_client_factory=forbidden)
    assert again["receipt"]["status"] == "released"
    assert again["receipt"]["master_releases"] == [{**row, "status": "already-released", "attempts_charged": 0}]
    assert again["control"] == released["control"]
    assert host.fixture["backend"].head() == before and len(host.master_clients) == calls


def test_cli_four_unknown_attempts_preserve_terminal_and_idempotent_receipt(monkeypatch):
    host, _ = pending(monkeypatch)
    retain = masters.retain_market_checkpoint
    for attempt in range(1, 5):
        if attempt > 1:
            def lost(*args, **kwargs):
                retain(*args, **kwargs)
                raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
            with monkeypatch.context() as patch:
                patch.setattr(masters, "retain_market_checkpoint", lost)
                result = host.run()
            assert result["receipt"]["status"] == "persistence-unconfirmed", result
        result = host.run(release_master=True)
        row, = result["receipt"]["master_releases"]
        assert result["receipt"]["status"] == "released"
        assert row["recorded_attempts"] == attempt and row["attempts_charged"] == 1
        assert row["terminal"] is (attempt == 4)
        assert result["receipt"]["source_requests"] == 0
    before = host.fixture["backend"].head()
    again = host.run(release_master=True)
    assert again["receipt"]["master_releases"] == [{**row, "status": "already-released", "attempts_charged": 0}]
    assert host.fixture["backend"].head() == before and not host.price_clients
    assert row["reason_code"] == "RELAY_REQUEST_UNCONFIRMED"


@pytest.mark.parametrize("error,status", [(RecordError("SYNTHETIC_RELEASE_REJECTED"), "blocked"),
    (UnknownWrite("REMOTE_WRITE_UNCONFIRMED"), "persistence-unconfirmed")])
def test_release_errors_become_receipts_and_still_consume_normal_reader(monkeypatch, error, status):
    host, original = pending(monkeypatch)
    before = host.fixture["backend"].head()
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(masters, "release_unconfirmed_masters", fail)
    result = host.run(release_master=True)
    assert result["receipt"]["status"] == status
    assert result["receipt"]["reason_code"] == str(error)
    assert result["receipt"]["health"] and result["control"] == original["control"]
    assert result["receipt"]["source_requests"] == 0 and host.fixture["backend"].head() == before


def test_missing_current_has_explicit_receipt_and_seed_reader():
    host = MasterHost()
    before = host.fixture["backend"].head()
    result = host.run(release_master=True)
    assert result["receipt"]["status"] == "blocked"
    assert result["receipt"]["reason_code"] == "DAILY_MASTER_RELEASE_CURRENT_REQUIRED"
    assert result["control"]["market_reference"] == host.approved["seed_reference"]
    assert result["receipt"]["health"] and not host.master_clients
    assert host.fixture["backend"].head() == before


@pytest.mark.parametrize("case,reason", [("policy", "DAILY_MARKET_POLICY_CHANGED"),
    ("master-policy", "DAILY_MASTER_POLICY_CHANGED"), ("legacy", "DAILY_MASTER_PHASE"),
    ("phase", "DAILY_MASTER_PHASE")])
def test_release_rejects_mismatched_policy_schema_and_phase(monkeypatch, case, reason):
    host, _ = pending(monkeypatch)
    item = deepcopy(host.cycle())
    if case == "policy":
        item["cycle"]["policy_sha256"] = "0" * 64
    elif case == "master-policy":
        item["cycle"]["master_refresh"] = {**masters.POLICY, "enabled": False}
    elif case == "legacy":
        item["cycle"]["schema"] = daily.SCHEMA
    else:
        item["cycle"]["status"] = "market-pending"
    monkeypatch.setattr(cli, "latest_cycle", lambda *args, **kwargs: item)
    def forbidden(*args, **kwargs):
        pytest.fail("校验失败不能调用 release API")
    monkeypatch.setattr(masters, "release_unconfirmed_masters", forbidden)
    before = host.fixture["backend"].head()
    result = host.run(release_master=True)
    assert result["receipt"]["reason_code"] == reason
    assert result["receipt"]["source_requests"] == 0 and host.fixture["backend"].head() == before


@pytest.mark.parametrize("case", ["current", "frontier"])
def test_release_injects_clock_guard_and_rechecks_current_frontier(monkeypatch, case):
    host, _ = pending(monkeypatch)
    before = host.fixture["backend"].head()
    latest, frontier = cli.latest_cycle, cli.check_cycle_frontier
    calls = []
    def execute(cycle, registry, store, fresh_reader, *, clock, guard, preflight):
        assert clock() == host.now
        guard()
        if case == "current":
            monkeypatch.setattr(cli, "latest_cycle", lambda *a, **k: None)
        else:
            def changed(*a, **k):
                raise RecordError("DAILY_MARKET_STALE_FRONTIER")
            monkeypatch.setattr(cli, "check_cycle_frontier", changed)
        calls.append(True)
        preflight(fresh_reader())
        pytest.fail("陈旧控制不能通过 preflight")
    monkeypatch.setattr(masters, "release_unconfirmed_masters", execute)
    result = host.run(release_master=True, guard=lambda api: calls.append("guard"))
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_STALE_" + ("CONTROL" if case == "current" else "FRONTIER")
    assert True in calls and "guard" in calls and host.fixture["backend"].head() == before
    monkeypatch.setattr(cli, "latest_cycle", latest)
    monkeypatch.setattr(cli, "check_cycle_frontier", frontier)


def test_release_unit_admission_error_is_reported_and_reader_still_runs(monkeypatch):
    host, original = pending(monkeypatch)
    calls = []
    def opened(write):
        calls.append(write)
        if write:
            raise StoreUnavailable("SYNTHETIC_WRITE_ADMISSION_FAILED")
        return host.open(False)
    result = host.run(release_master=True, open_unit=opened)
    assert result["receipt"]["status"] == "blocked"
    assert result["receipt"]["reason_code"] == "SYNTHETIC_WRITE_ADMISSION_FAILED"
    assert calls == [True, False] and result["control"] == original["control"]
    assert result["receipt"]["health"]


def test_nothing_to_release_retains_complete_reader_control(monkeypatch):
    host, original = pending(monkeypatch)
    monkeypatch.setattr(masters, "release_unconfirmed_masters", lambda *a, **k: [])
    before = host.fixture["backend"].head()
    result = host.run(release_master=True)
    assert result["receipt"]["status"] == "nothing-to-release"
    assert result["receipt"]["master_releases"] == []
    assert result["receipt"]["health"] and result["control"] == original["control"]
    assert host.fixture["backend"].head() == before


def test_paused_release_is_read_only_and_never_opens_collection(monkeypatch):
    host, _ = pending(monkeypatch)
    host.approved["enabled"] = False
    host.openings.clear()
    before = host.fixture["backend"].head()
    def forbidden(*args, **kwargs):
        pytest.fail("暂停策略不能裁定或采集")
    monkeypatch.setattr(masters, "release_unconfirmed_masters", forbidden)
    result = host.run(release_master=True, guard=forbidden, read_guard=lambda api: None)
    assert result["receipt"]["status"] == "paused" and result["receipt"]["source_requests"] == 0
    assert host.openings == [False, False] and host.fixture["backend"].head() == before
    assert result["control"]["cycle_reference"] is not None


@pytest.mark.parametrize("empty", [False, True])
def test_release_reader_failure_is_not_success_with_empty_control(monkeypatch, empty):
    host, original = pending(monkeypatch)
    if empty:
        monkeypatch.setattr(masters, "release_unconfirmed_masters", lambda *a, **k: [])
    def fail(*args, **kwargs):
        raise RecordError("SYNTHETIC_READER_FAILED")
    monkeypatch.setattr(cli, "read_ftshare_collection_version", fail)
    result = host.run(release_master=True)
    assert result["receipt"]["status"] == "reading-unavailable"
    assert result["receipt"]["reading_reason_code"] == "SYNTHETIC_READER_FAILED"
    assert result["control"] == original["control"]


def peer_api(monkeypatch, *, peer=False, queued=False, failure=False, requests=0):
    selected = {"operation": "update", "code_sha": "a" * 40, "run_id": 8001}
    own = {"id": 8001, "status": "in_progress", "run_attempt": 1,
        "head_sha": selected["code_sha"], "path": cli.WORKFLOW}
    calls = []
    api = GitHubAPI("synthetic-peer-only")
    class Wire:
        def open(self, request, timeout):
            path = request.full_url.removeprefix("https://api.github.com")
            calls.append((request.method, path))
            assert request.method == "GET"
            if failure:
                raise OSError("合成失败")
            rows = [own]
            if peer and not queued:
                rows.append({**own, "id": 8002, "path": ".github/workflows/ashare-data.yml"})
            value = {"total_count": len(rows), "workflow_runs": rows} if "?" in path else own
            return Response(encoded(value), request.full_url,
                headers=quota_headers(remaining=5000, reset=2000000000, limit=5000))
    api._opener, api._requests = Wire(), requests
    api.rate_limits.observe(quota_headers(remaining=5000, reset=2000000000, limit=5000))
    checks = []
    monkeypatch.setattr(cli, "check_private", lambda current, actual: checks.append((current, actual)))
    return api, selected, calls, checks


def test_publication_adds_only_exact_daily_release_master(grant):
    from decision_kernel.infrastructure import runtime_publication as publication
    selected, env, _ = context("release-master")
    assert publication.native_publication_basis(selected, env=env) == grant[0]["authorization_ref"]
    for operation in ("release", "release-masters", "release-master-extra"):
        assert publication.native_publication_basis({**selected, "operation": operation}, env=env) is None
    selected, env, _ = context("release-master", workflow="ashare-data.yml")
    assert publication.native_publication_basis(selected, env=env) is None
