"""真实 Store、内存 Git 对象和十页 peer 列表的离线 daily 预算回归。"""
from contextlib import contextmanager
from copy import copy
from dataclasses import replace
import socket

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces import daily_market_cli as cli
from test_daily_market import Host, quota_headers
from test_daily_master_cycle import MasterHost
from test_ftshare_on_demand_runtime import NativeSnapshot, NativeStoreModel
from test_news_read_budget import transport


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("禁止真实网络")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def native_daily(monkeypatch, *, remaining=5000, initial=0, snapshot_requests=None):
    model = NativeStoreModel()
    model.native.run.update(path=cli.WORKFLOW, name=cli.TITLE)
    model.native.ci.update(total_count=1)
    model.native.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    selected = {"operation": "update", "code_sha": model.native.main["object"]["sha"],
        "run_id": 8001, "run_attempt": 1, "event": "workflow_dispatch"}
    apis = []
    def api_factory(token):
        api = transport(model)
        wire = api._opener.open
        def observed(request, timeout):
            response = wire(request, timeout)
            response.headers = quota_headers(remaining=remaining, limit=5000,
                resource="graphql" if request.method == "POST" else "core", reset=2000000000)
            return response
        api._opener.open = observed
        api._requests = initial
        apis.append(api)
        return api
    original_api = cli.GitHubAPI
    class BudgetAPI(original_api):
        def __init__(self, token):
            configured = api_factory(token)
            self.__dict__.update(configured.__dict__)
            apis[-1] = self
    monkeypatch.setattr(cli, "GitHubAPI", BudgetAPI)
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation:
        replace(model.binding, write_enabled=operation == "sync"))
    class BoundedSnapshot(NativeSnapshot):
        @contextmanager
        def market_read_scope(self):
            if self.depth or not getattr(model, "pad_promotion_scopes", False):
                with super().market_read_scope() as reader:
                    yield reader
                return
            before = self.api._requests
            with super().market_read_scope() as reader:
                yield reader
            used = self.api._requests - before
            assert used <= 36
            # 内存字节读取省掉了真实快照路径的请求；最坏预算回归补满同一API至36。
            for _ in range(36 - used):
                self.api.request("GET", cli.PREFIX)
            if snapshot_requests is not None:
                snapshot_requests.append(self.api._requests - before)
    snapshot = NativeSnapshot if snapshot_requests is None else BoundedSnapshot
    monkeypatch.setattr(cli, "GitMarketReader", lambda binding, api: snapshot(model, api))
    opened = cli.remote_units("synthetic-daily-only", selected)
    return model, selected, apis, opened


def seeded_host(model, master=False, master_rows=3):
    fresh = lambda: VersionedStore(copy(model.git))
    storage = model.git, VersionedStore(model.git), fresh
    if master:
        return MasterHost(storage=storage, master_rows=master_rows)
    return Host(storage=storage)


def options(host, selected, opened):
    return dict(open_unit=opened, guard=lambda api: cli.collection_guard(api, selected),
        read_guard=lambda api: cli.check_private(api, selected), sleep=lambda _: None)


@pytest.mark.parametrize("settlement_remaining", [None, 330])
def test_calendar_real_store_ten_pages_settles_beyond_240(monkeypatch, settlement_remaining):
    model, selected, apis, opened = native_daily(monkeypatch)
    host = seeded_host(model)
    after_http, before_http = [], []
    original = host.calendar
    def client():
        value = original()
        request = value.request_registered
        def http(*args, **kwargs):
            before_http.append(apis[-1]._requests)
            result = request(*args, **kwargs)
            after_http.append(apis[-1])
            if settlement_remaining is not None and len(after_http) == 2:
                apis[-1].rate_limits.observe(quota_headers(remaining=settlement_remaining,
                    limit=5000, reset=2000000000))
                assert not apis[-1].rate_limits.admits(512 - apis[-1]._requests)
            return result
        value.request_registered = http
        return value
    def stop_prices(**kwargs):
        return {"receipt": {"status": "partial", "reason_code": None, "source_requests": 0}}
    result = host.run(**options(host, selected, opened), calendar_client_factory=client,
        execute_prices=stop_prices)
    assert result["receipt"]["calendar_source_requests"] == 2, result["receipt"]
    assert result["receipt"]["status"] == "partial", result
    from test_market_relay_inputs import REGISTRY
    current = daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])
    assert all(row["status"] == "complete" for row in current["cycle"]["calendars"].values())
    assert len(before_http) == 2 and all(count < 240 for count in before_http)
    assert after_http and any(api._requests > 240 for api in after_http)
    assert all(api._requests <= 512 for api in apis)
    lists = [path for method, path, _ in model.requests if "?status=in_progress&" in path]
    assert lists and len(lists) % 10 == 0
    for offset in range(0, len(lists), 10):
        assert [int(path.rsplit("=", 1)[1]) for path in lists[offset:offset + 10]] == list(range(1, 11))


@pytest.mark.parametrize("reserve", [cli.CALENDAR_UNIT_REQUESTS, cli.PRICE_UNIT_REQUESTS,
    cli.MASTER_UNIT_REQUESTS])
def test_full_unit_budget_rejected_before_source_or_reservation(monkeypatch, reserve):
    model, selected, apis, opened = native_daily(monkeypatch)
    store, fresh, api = opened(True)
    before = model.git.head()
    api._requests = 200
    with pytest.raises(RecordError, match="STORE_SETTLEMENT_BUDGET"):
        cli._source_admit(api, lambda api: cli.collection_guard(api, selected), reserve)
    assert model.git.head() == before and model.writes == 0


def test_settlement_uses_real_remaining_not_source_window(monkeypatch):
    model, selected, apis, opened = native_daily(monkeypatch)
    store, fresh, api = opened(True)
    api._requests = 260
    api.rate_limits.observe(quota_headers(remaining=200, limit=5000, reset=2000000000))
    assert not api.rate_limits.admits(512 - api._requests)
    cli.collection_guard(api, selected)
    before = model.git.head()
    store.backend.commit({"submissions/synthetic-settlement.json": b"{}"}, expected=before)
    assert model.writes == 1 and 240 < api._requests < 512
    with pytest.raises(RecordError, match="STORE_ADMISSION"):
        cli._source_admit(api, lambda api: cli.collection_guard(api, selected), 1)


@pytest.mark.parametrize("case", ["peer", "cas", "quota"])
def test_settlement_keeps_live_peer_native_quota_and_original_cas(monkeypatch, case):
    model, selected, apis, opened = native_daily(monkeypatch)
    store, fresh, api = opened(True)
    before = model.git.head()
    api._requests = 260
    if case == "peer":
        model.native.peers = [{"id": 9001, "status": "in_progress",
            "path": ".github/workflows/ashare-data.yml"}]
    elif case == "cas":
        model.git.commit({"submissions/concurrent.json": b"{}"}, expected=before)
    else:
        api.rate_limits.observe(quota_headers(remaining=20, limit=5000, reset=2000000000))
    with pytest.raises(RecordError):
        store.backend.commit({"submissions/synthetic-denied.json": b"{}"}, expected=before)
    assert model.writes == 0


@pytest.mark.parametrize("response_remaining,master_rows", [(None, 3), (None, 501), (230, 3)])
def test_master_real_store_ten_pages_checkpoint_then_zero_http_promotion(monkeypatch, response_remaining, master_rows):
    from decision_kernel.application import daily_master_refresh as masters
    model, selected, apis, opened = native_daily(monkeypatch)
    host = seeded_host(model, master=True, master_rows=master_rows)
    units, http_counts, settlements = [], [], []
    original = host.master
    def provider():
        client = original()
        request = client.request_registered
        def http(*args, **kwargs):
            http_counts.append(apis[-1]._requests)
            value = request(*args, **kwargs)
            if response_remaining is not None:
                apis[-1].rate_limits.observe(quota_headers(remaining=response_remaining,
                    limit=5000, reset=2000000000))
            return value
        client.request_registered = http
        return client
    def execute(**kwargs):
        admitted = []
        source = kwargs["source_guard"]
        def measured(phase):
            source(phase)
            admitted.append((phase, apis[-1]._requests))
        settlement = kwargs["settlement_guard"]
        def settled(phase):
            settlement(phase)
            settlements.append((phase, apis[-1], apis[-1]._requests))
        value = masters.run_master_unit(**{**kwargs, "source_guard": measured,
            "settlement_guard": settled})
        if value["status"] == "complete":
            phase, api, before = settlements[-1]
            assert phase == "promotion" and api is apis[-1]
            cost = api._requests - before
            assert 0 < cost <= cli.MASTER_PROMOTION_REQUESTS
        for phase, before in admitted:
            bound = cli.MASTER_RESERVATION_REQUESTS if phase == "reservation" else cli.MASTER_SETTLEMENT_REQUESTS
            assert apis[-1]._requests - before <= bound
        units.append((value, apis[-1]._requests))
        return value
    result = host.run(**options(host, selected, opened), master_client_factory=provider,
        execute_master_unit=execute)
    if response_remaining is None:
        assert result["receipt"]["status"] == "complete", result["receipt"]
        sources = 3 if master_rows == 501 else 2
        expected_counts = [1, 1, 0, 1, 0] if master_rows == 501 else [1, 0, 1, 0]
        assert result["receipt"]["master_source_requests"] == sources
        assert [value["source_requests"] for value, _ in units] == expected_counts
        assert [value["status"] for value, _ in units] == [
            "partial" if count else "complete" for count in expected_counts]
    else:
        # 原响应已保管；剩余native额度不够开后续单元时正常停，不重发来源。
        assert result["receipt"]["reason_code"] == "DAILY_MARKET_STORE_RATE_LIMIT"
        assert len(units) == 1 and units[0][0]["status"] == "partial"
        assert units[0][0]["source_requests"] == 1
    assert all(value["checkpoint_reference"] is not None for value, _ in units)
    assert all(count < 240 for count in http_counts)
    assert any(count > 240 for _, count in units)
    assert all(api._requests <= 512 for api in apis)


@pytest.mark.parametrize("boundary", ["requests", "quota"])
@pytest.mark.parametrize("spare", [-1, 0])
def test_master_promotion_reserves_independent_restore_scope(monkeypatch, boundary, spare):
    from decision_kernel.application import daily_master_refresh as masters
    snapshots = []
    model, selected, apis, opened = native_daily(monkeypatch, snapshot_requests=snapshots)
    host = seeded_host(model, master=True)
    promotions, costs, restore_costs, outcomes = [], [], [], []
    retain = masters.retain_market_query
    from decision_kernel.application import market_collection_custody as custody
    restore = custody.restore_market_checkpoint
    def measured(*args, **kwargs):
        before = apis[-1]._requests
        promotions.append(before)
        def restored(*args, **kwargs):
            start = apis[-1]._requests
            value = restore(*args, **kwargs)
            restore_costs.append(apis[-1]._requests - start)
            return value
        model.pad_promotion_scopes = True
        try:
            with monkeypatch.context() as patch:
                patch.setattr(custody, "restore_market_checkpoint", restored)
                value = retain(*args, **kwargs)
        finally:
            model.pad_promotion_scopes = False
        costs.append(apis[-1]._requests - before)
        return value
    monkeypatch.setattr(masters, "retain_market_query", measured)
    assert cli.MASTER_PROMOTION_REQUESTS == cli.CUSTODY_REQUESTS + 36 + 25
    def execute(**kwargs):
        settlement = kwargs["settlement_guard"]
        def limited(phase):
            if phase == "promotion":
                api = apis[-1]
                reserve = cli.GUARD_REQUESTS + cli.MASTER_PROMOTION_REQUESTS
                if boundary == "requests":
                    api._requests = 512 - reserve - spare
                else:
                    reset = api.rate_limits.receipt()["resources"]["core"]["reset"] + 1
                    api.rate_limits.observe(quota_headers(remaining=reserve + spare,
                        limit=5000, reset=reset))
            settlement(phase)
        value = masters.run_master_unit(**{**kwargs, "settlement_guard": limited})
        outcomes.append(value)
        if value["source_requests"] == 0:
            assert value["checkpoint_reference"] is not None
            if spare < 0:
                assert value["status"] == "partial" and value["reason_code"] == "RELAY_STORAGE_BUDGET"
                assert not promotions and model.writes > 0
            else:
                assert value["status"] == "complete" and value["reference"] is not None, (value, costs, snapshots)
            if boundary == "quota":
                # 后续单元的新窗口必须由合成原生 headers 明确证明，而非换 API 清零。
                reset = apis[-1].rate_limits.receipt()["resources"]["core"]["reset"] + 1
                apis[-1].rate_limits.observe(quota_headers(remaining=5000,
                    limit=5000, reset=reset))
        return value
    result = host.run(**options(host, selected, opened), execute_master_unit=execute)
    assert all(api._requests <= 512 for api in apis)
    assert cli.ADMISSION_REQUESTS == 240
    if spare < 0:
        assert result["receipt"]["master_source_requests"] == 1
        assert result["receipt"]["status"] == "partial" and not costs
        row = host.cycle()["cycle"]["masters"]["SSE"]
        assert row["status"] == "in-flight" and row["reserved_queries"] == 1
        from decision_kernel.application.market_collection_reading import latest_market_reference
        from decision_kernel.application.market_collection_custody import restore_market_checkpoint
        from test_market_relay_inputs import REGISTRY
        reference = latest_market_reference(host.fixture["fresh"](), REGISTRY,
            checkpoint_id=masters.checkpoint_id(host.cycle()["cycle"], "SSE"))
        assert restore_market_checkpoint(reference, REGISTRY,
            host.fixture["fresh"]())["state"]["control"]["state"] == "complete"
        resumed = host.run(**options(host, selected, opened))
        assert resumed["receipt"]["status"] == "complete", resumed["receipt"]
        assert resumed["receipt"]["master_source_requests"] == 1
    else:
        assert result["receipt"]["status"] == "complete", (result["receipt"], outcomes, costs, snapshots)
        assert result["receipt"]["master_source_requests"] == 2
    assert len(costs) == 2
    assert all(cli.CUSTODY_REQUESTS < cost <= cli.MASTER_PROMOTION_REQUESTS for cost in costs), costs
    assert restore_costs == [36, 36]
    assert snapshots and set(snapshots) == {36}
    print({"promotion_requests": costs, "independent_restore_requests": restore_costs,
        "promotion_bound": cli.MASTER_PROMOTION_REQUESTS})


@pytest.mark.parametrize("phase", ["reservation", "http"])
def test_master_low_budget_stops_before_source(monkeypatch, phase):
    from decision_kernel.application import daily_master_refresh as masters
    model, selected, apis, opened = native_daily(monkeypatch)
    host = seeded_host(model, master=True)
    def execute(**kwargs):
        original = kwargs["source_guard"]
        def limited(current):
            if current == phase:
                apis[-1]._requests = 230
            original(current)
        return masters.run_master_unit(**{**kwargs, "source_guard": limited})
    result = host.run(**options(host, selected, opened), execute_master_unit=execute)
    assert result["receipt"]["master_source_requests"] == 0
    assert all(client.requests == 0 for client in host.master_clients)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    saved = host.cycle()["cycle"]["masters"]["SSE"]
    if phase == "reservation":
        from decision_kernel.application.market_collection_reading import latest_market_reference
        from test_market_relay_inputs import REGISTRY
        assert latest_market_reference(host.fixture["fresh"](), REGISTRY,
            checkpoint_id=masters.checkpoint_id(host.cycle()["cycle"], "SSE")) is None
    assert saved["status"] == "in-flight" and saved["reserved_queries"] == 1


@pytest.mark.parametrize("initial", [0, 80])
def test_daily_price_real_loop_completes_or_stops_with_zero_source(monkeypatch, initial):
    model, selected, apis, opened = native_daily(monkeypatch, initial=initial)
    host = seeded_host(model)
    def stop_prices(**kwargs):
        return {"receipt": {"status": "partial", "reason_code": None, "source_requests": 0}}
    seeded = host.run(execute_prices=stop_prices)
    assert seeded["receipt"]["cycle_status"] == "market-pending"
    before_http = []
    original = host.price
    def provider():
        client = original()
        request = client.candlesticks
        def http(*args, **kwargs):
            before_http.append(apis[-1]._requests)
            return request(*args, **kwargs)
        client.candlesticks = http
        return client
    result = host.run(**options(host, selected, opened), price_client_factory=provider)
    receipt = result["receipt"]
    if initial == 0:
        assert receipt["status"] == "complete", receipt
    if receipt["status"] == "complete":
        assert receipt["price_source_requests"] == host.approved["max_price_requests"]
    else:
        assert receipt["price_source_requests"] == 0, receipt
        assert receipt["reason_code"] in {"DAILY_MARKET_STORE_SETTLEMENT_BUDGET",
            "DAILY_MARKET_STORE_ADMISSION", "FTSHARE_STORE_ADMISSION_BUDGET"}, receipt
    assert all(api._requests <= 512 for api in apis)
    assert sum(client.requests for client in host.price_clients) == receipt["price_source_requests"]
    assert len(before_http) == receipt["price_source_requests"] and all(count < 240 for count in before_http)
    if before_http:
        assert any(api._requests > 240 for api in apis)
