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


def test_status_reader_never_calls_collection_guard_with_peer_present():
    host = MasterHost()
    def peer(*args, **kwargs):
        raise Conflict("SOURCE_COLLECTION_PEER_CONFLICT")
    result = host.run(operation="status", guard=peer, read_guard=lambda api: None)
    assert result["receipt"]["status"] == "read" and result["receipt"]["health"]
    assert host.openings == [False, False]
    assert result["control"]["market_reference"] == host.approved["seed_reference"]


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


@pytest.mark.parametrize("case", ["allowed", "peer", "queued", "failed", "budget"])
def test_combined_guard_retains_private_check_and_post_peer_admission(monkeypatch, case):
    api, selected, calls, checks = peer_api(monkeypatch, peer=case in {"peer", "queued"},
        queued=case == "queued", failure=case == "failed", requests=237 if case == "budget" else 0)
    if case in {"peer", "failed", "budget"}:
        reason = {"peer": "PEER_CONFLICT", "failed": "API_UNAVAILABLE", "budget": "STORE_ADMISSION"}[case]
        with pytest.raises(RecordError, match=reason):
            (cli._source_admit(api, lambda api: cli.collection_guard(api, selected), 1)
                if case == "budget" else cli.collection_guard(api, selected))
    else:
        cli.collection_guard(api, selected)
    assert checks == [(api, selected)]
    assert 1 <= len(calls) <= 12 and all(method == "GET" for method, _ in calls)
    assert all("queued" not in path for _, path in calls)
    if case == "budget":
        assert api._requests == cli.ADMISSION_REQUESTS


@pytest.mark.parametrize("before,allowed", [(227, True), (228, False)])
def test_maximum_twelve_get_peer_check_keeps_original_admission_threshold(monkeypatch, before, allowed):
    api, selected, calls, checks = peer_api(monkeypatch, requests=before)
    own = {"id": 8001, "status": "in_progress", "run_attempt": 1,
        "head_sha": selected["code_sha"], "path": cli.WORKFLOW}
    rows = [own] + [{"id": i, "status": "in_progress", "path": ".github/workflows/ci.yml"}
        for i in range(10000, 10999)]
    class Wire:
        def open(self, request, timeout):
            path = request.full_url.removeprefix("https://api.github.com")
            calls.append((request.method, path))
            assert request.method == "GET"
            if "?" in path:
                page = int(path.rsplit("=", 1)[1])
                value = {"total_count": 1000, "workflow_runs": rows[(page - 1) * 100:page * 100]}
            else:
                value = own
            return Response(encoded(value), request.full_url,
                headers=quota_headers(remaining=5000, reset=2000000000, limit=5000))
    api._opener = Wire()
    if allowed:
        cli._source_admit(api, lambda api: cli.collection_guard(api, selected), 1)
    else:
        with pytest.raises(RecordError, match="DAILY_MARKET_STORE_ADMISSION"):
            cli._source_admit(api, lambda api: cli.collection_guard(api, selected), 1)
    assert len(calls) == 12 and api._requests == before + 12
    assert checks == [(api, selected)]


def test_remote_units_keeps_plain_store_and_passes_expected_through(native, monkeypatch):
    model, _ = native
    api, selected, calls, checks = peer_api(monkeypatch)
    peer_wire = api._opener
    class Wire:
        def open(self, request, timeout):
            path = request.full_url.removeprefix("https://api.github.com")
            if path.startswith(cli.PREFIX + "/actions/runs"):
                return peer_wire.open(request, timeout)
            value = model.request(request.method, path, json.loads(request.data) if request.data else None)
            return Response(encoded(value), request.full_url,
                headers=quota_headers(remaining=5000, reset=2000000000, limit=5000))
    api._opener = Wire()
    monkeypatch.setattr(cli, "GitHubAPI", lambda token: api)
    monkeypatch.setattr(cli, "native_ready", lambda *a: None)
    monkeypatch.setattr(cli.DailyQuotaWindows, "wait", lambda *a: True)
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation:
        replace(model.binding, write_enabled=operation == "sync"))
    opened = cli.remote_units("synthetic-proxy-only", selected)
    store, fresh, actual = opened(True)
    # 提交级保护是原条件更新；探测只在来源请求前，开单元与提交都不发在途列表。
    assert actual is api and type(store.backend) is GitHubStore
    assert calls == []
    expected = model.git.head()
    files = {"synthetic": b"body"}
    commits = []
    def commit(backend, actual_files, *, expected):
        commits.append((backend, actual_files, expected))
        return "synthetic-result"
    monkeypatch.setattr(GitHubStore, "commit", commit)
    assert store.backend.commit(files, expected=expected) == "synthetic-result"
    assert commits == [(store.backend, files, expected)]
    assert commits[0][1] is files and calls == []
    readonly, _, _ = opened(False)
    assert type(readonly.backend) is GitHubStore and calls == []
    assert not fresh().backend.binding.write_enabled


@pytest.mark.parametrize("operation", ["status", "release-master"])
def test_runtime_wires_separate_read_guard_and_release_options(tmp_path, monkeypatch, operation):
    host = MasterHost()
    event, env, api = native_runtime(tmp_path, monkeypatch, host.approved,
        invocation=operation, admitted_at=host.now, actual_at=host.now)
    seen = []
    def peers(current, **context):
        seen.append((current, context))
        raise Conflict("SOURCE_COLLECTION_PEER_CONFLICT")
    monkeypatch.setattr(cli, "require_no_peer_collection", peers)
    def execute(**options):
        options["read_guard"](api)
        assert not seen
        if operation == "release-master":
            assert options["release_master"] is True and options["master_refresh"] == masters.POLICY
            # 单元与只读回执都不探测在途；探测只绑在发上游请求前一刻。
            options["guard"](api)
            assert not seen
            with pytest.raises(Conflict, match="PEER_CONFLICT"):
                options["probe"](api)
        return {"receipt": {"status": "read", "source_requests": 0}, "control": {}}
    assert cli.run_runtime(event, env, tmp_path / "runtime", api=api, execute=execute,
        open_unit=host.open) == 0
    assert bool(seen) is (operation == "release-master")


def test_remote_units_commit_keeps_cas_and_peer_blocks_source_admission(native, monkeypatch):
    model, _ = native
    api, selected, calls, checks = peer_api(monkeypatch)
    own = {"id": 8001, "status": "in_progress", "run_attempt": 1,
        "head_sha": selected["code_sha"], "path": cli.WORKFLOW}
    conflict = [False]
    class Wire:
        def open(self, request, timeout):
            path = request.full_url.removeprefix("https://api.github.com")
            if path.startswith(cli.PREFIX + "/actions/runs"):
                rows = [own] + ([{**own, "id": 8002}] if conflict[0] else [])
                value = {"total_count": len(rows), "workflow_runs": rows} if "?" in path else own
            else:
                value = model.request(request.method, path, json.loads(request.data) if request.data else None)
            return Response(encoded(value), request.full_url,
                headers=quota_headers(remaining=5000, reset=2000000000, limit=5000))
    api._opener = Wire()
    monkeypatch.setattr(cli, "GitHubAPI", lambda token: api)
    monkeypatch.setattr(cli, "native_ready", lambda *a: None)
    monkeypatch.setattr(cli.DailyQuotaWindows, "wait", lambda *a: True)
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation:
        replace(model.binding, write_enabled=operation == "sync"))
    store, _, _ = cli.remote_units("synthetic-real-commit-only", selected)(True)
    before = model.git.head()
    files = {"submissions/synthetic-guard.json": b"{}"}
    # 普通提交不做在途探测：成本与提交数脱钩，提交级冲突仍由原条件更新承担。
    after = store.backend.commit(files, expected=before)
    assert after == model.git.head() != before and model.writes == 1
    posts = [payload for method, _, payload in model.requests if method == "POST"]
    assert posts[0]["variables"]["input"]["expectedHeadOid"] == before
    assert calls == [] and checks == []
    # 在途运行只在真正要发上游请求前一刻阻断，且不产生写入。
    conflict[0] = True
    with pytest.raises(Conflict, match="PEER_CONFLICT"):
        cli._source_admit(api, lambda current: None, 1,
            probe=lambda current: cli.collection_guard(current, selected))
    assert model.writes == 1 and model.git.head() == after
    assert checks == [(api, selected)]


@pytest.mark.parametrize("stage", ["calendar", "master", "price"])
def test_daily_existing_http_boundaries_use_combined_guard_without_transport_changes(monkeypatch, stage):
    host = MasterHost()
    selected = {"operation": "update", "run_id": 8001, "code_sha": "a" * 40}
    checks = []
    monkeypatch.setattr(cli, "check_private", lambda api, actual: None)
    def peer(api, **context):
        checks.append(context)
        if stage == "calendar":
            current = daily.latest_cycle(host.fixture["fresh"]())
            blocked = current is not None and any(row["status"] == "in-flight"
                for row in current["cycle"]["calendars"].values())
        elif stage == "master":
            current = host.cycle()
            blocked = current is not None and current["cycle"]["status"] == "master-pending"
        else:
            blocked = bool(host.price_clients)
        if blocked:
            raise Conflict("SOURCE_COLLECTION_PEER_CONFLICT")
    monkeypatch.setattr(cli, "require_no_peer_collection", peer)
    result = host.run(guard=lambda api: cli.collection_guard(api, selected), read_guard=lambda api: None)
    assert result["receipt"]["reason_code"] == "SOURCE_COLLECTION_PEER_CONFLICT", result
    assert result["receipt"]["calendar_source_requests"] == (0 if stage == "calendar" else 2)
    assert result["receipt"]["master_source_requests"] == (2 if stage == "price" else 0)
    assert result["receipt"]["price_source_requests"] == 0
    assert checks and result["control"]["market_reference"] == host.approved["seed_reference"]


def test_publication_adds_only_exact_daily_release_master(grant):
    from decision_kernel.infrastructure import runtime_publication as publication
    selected, env, _ = context("release-master")
    assert publication.native_publication_basis(selected, env=env) == grant[0]["authorization_ref"]
    for operation in ("release", "release-masters", "release-master-extra"):
        assert publication.native_publication_basis({**selected, "operation": operation}, env=env) is None
    selected, env, _ = context("release-master", workflow="ashare-data.yml")
    assert publication.native_publication_basis(selected, env=env) is None
