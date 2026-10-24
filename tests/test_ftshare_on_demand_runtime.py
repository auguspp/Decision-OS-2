"""按需入口复用真实执行循环与合成来源；禁止网络。"""
from contextlib import contextmanager
from copy import copy, deepcopy
from dataclasses import replace
import base64
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import pytest

from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubRateLimits, GitHubStore, github_tree_inventory
from test_market_custody import MemoryBackend
from test_ftshare_collection import single_collection_fixture, _retain, settle_single
from test_news_read_budget import transport
from decision_kernel.domain.records import RecordError, UnknownWrite, digest, encoded
from decision_kernel.interfaces import ftshare_collection_cli as cli
from test_ftshare_collection_runtime import (
    Host, SingleProvider, SingleNativeAPI, single_config, single_input,
)
from test_ftshare_on_demand import LATER, STAMP, no_network, ready, request
from test_market_relay_inputs import REGISTRY


class PeerAPI(SingleNativeAPI):
    """Restore the real paged in_progress listing shape for peer-detection cases."""
    def __init__(self):
        super().__init__()
        self.peers = []

    def request(self, method, path, payload=None):
        if "?status=in_progress&per_page=100&page=" in path:
            self.calls.append(path)
            self._requests += 1
            rows = [deepcopy(self.run), *deepcopy(self.peers)]
            page = int(path.rsplit("=", 1)[1])
            return {"total_count": len(rows), "workflow_runs": rows[(page - 1) * 100:page * 100]}
        return super().request(method, path, payload)


class NativeStoreModel:
    """只模拟传输响应；真实GitHubAPI/Store负责预算、CAS及逐字读回。"""
    def __init__(self):
        self.git = MemoryBackend()
        self.binding = GitHubBinding(cli.REPO, 1420041744, "records", self.git.head(), "test-store", True)
        anchor = self.git.commit({"store.json": self.binding.marker()}, expected=self.git.head())
        self.binding = replace(self.binding, anchor=anchor)
        self.git.repository, self.git.binding = cli.REPO, self.binding
        self.native = PeerAPI()
        self.native.peers = [{"id": 9000 + i, "status": "in_progress", "path": ".github/workflows/ci.yml"}
            for i in range(999)]
        self.requests, self.writes, self.before_write = [], 0, None
        self.blobs = {}

    def request(self, method, path, payload=None):
        self.requests.append((method, path, deepcopy(payload)))
        if method == "POST":
            assert path == "/graphql"
            value = payload["variables"]["input"]
            assert value["branch"] == {"id": "REF_synthetic_data_branch"}
            if self.before_write:
                self.before_write()
            files = {row["path"]: base64.b64decode(row["contents"], validate=True)
                for row in value["fileChanges"]["additions"]}
            commit = self.git.commit(files, expected=value["expectedHeadOid"])
            self.writes += 1
            return {"data": {"createCommitOnBranch": {"commit": {"oid": commit}}}}
        if path == cli.PREFIX or path.endswith("/git/ref/heads/main") or "/actions/" in path:
            return self.native.request(method, path, payload)
        suffix = path.removeprefix(cli.PREFIX + "/")
        url = urlsplit(suffix)
        route, query = url.path, parse_qs(url.query)
        if route.startswith("git/ref/heads/"):
            return {"ref": "refs/heads/records", "node_id": "REF_synthetic_data_branch",
                "object": {"type": "commit", "sha": self.git.head()}}
        if route.startswith("compare/"):
            base, head = route[8:].split("...")
            assert int(base, 16) <= int(head, 16)
            return {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": base}}
        if route.startswith("git/commits/"):
            sha = route.rsplit("/", 1)[1]
            self.git.check_commit(sha)
            return {"sha": sha, "tree": {"sha": sha}, "parents": [{"sha": f"{int(sha, 16) - 1:040x}"}]}
        if route.startswith("git/trees/"):
            sha = route.rsplit("/", 1)[1]
            rows = []
            for name, raw in self.git.shared["trees"][sha].items():
                oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                self.blobs[oid] = raw
                rows.append({"path": name, "sha": oid, "size": len(raw), "type": "blob", "mode": "100644"})
            return {"sha": sha, "tree": rows, "truncated": False}
        if route.startswith("git/blobs/"):
            sha = route.rsplit("/", 1)[1]
            raw = self.blobs[sha]
            return {"sha": sha, "size": len(raw), "encoding": "base64", "content": base64.b64encode(raw).decode()}
        if route == "commits":
            return [{"sha": self.git.last_change(query["sha"][0], query["path"][0])}]
        raise AssertionError(path)


class NativeSnapshot(MemoryBackend):
    """仅替换本地 Git 字节读取；入口/出口使用生产 Store 和同一 API 核验。"""
    def __init__(self, model, api):
        super().__init__(model.git.shared)
        self.repository, self.binding, self.api = model.git.repository, replace(model.binding, write_enabled=False), api
        self.depth = 0

    @contextmanager
    def market_read_scope(self):
        if self.depth:
            yield self
            return
        remote = GitHubStore(self.binding, self.api)
        snapshot = remote.head()
        self.depth += 1
        try:
            yield self
            remote.check_commit(snapshot)
        finally:
            self.depth -= 1

    def market_inventory(self):
        files = self.shared["trees"][self.head()]
        return {**github_tree_inventory(self.binding, {path: len(raw) for path, raw in files.items()}),
            "native_bytes": sum(map(len, files.values())), "tree_response_applicability": "required"}

    def commit(self, files, *, expected):
        pytest.fail("快照不得写入")


def native_budget_setup(tmp_path, *, initial=0, native_remaining=5000):
    model = NativeStoreModel()
    fresh = lambda: VersionedStore(copy(model.git))
    fixture = single_collection_fixture(count=5, storage=(model.git, VersionedStore(model.git), fresh))
    saved = _retain(fixture["single"], fixture["store"], fresh)
    _, saved = settle_single(fixture, fixture["single"], saved)
    fixture["base"] = saved["reference"]
    intent = request(fixture)
    event, env = demand_input(tmp_path)
    config = single_config()
    config["kernel_adoption"] = {key: fixture["proof"][key]
        for key in ("run_id", "artifact_id", "sha256", "code_sha")}
    selected = cli.settings(event, env, config)
    api = transport(model)
    wire = api._opener.open
    def observed(request, timeout):
        response = wire(request, timeout)
        response.headers = {"x-ratelimit-resource": "graphql" if request.method == "POST" else "core",
            "x-ratelimit-limit": "5000", "x-ratelimit-remaining": str(native_remaining),
            "x-ratelimit-reset": "2000000000"}
        return response
    api._opener.open = observed
    api._requests = initial
    fresh = lambda: VersionedStore(NativeSnapshot(model, api))
    def opening(write):
        cli.native_ready(api, selected, config)
        return VersionedStore(GitHubStore(model.binding, api)), fresh, api
    generation = "od-" + digest(encoded(intent["id"]))[:40] + "-" + digest(encoded(intent))[:48]
    def latest():
        ref = ft.latest_ftshare_reference(fresh(), REGISTRY, collection_id="ftshare-cn-eod", generation=generation)
        return ft.restore_ftshare_collection(ref, REGISTRY, fresh())
    host = SimpleNamespace(fixture=fixture, now=STAMP, latest=latest)
    provider = SingleProvider(host)
    arguments = dict(operation="ftshare-sync", config=config, registry=REGISTRY,
        open_unit=opening, access_confirmed=True, custody_authorized=True, on_demand=intent,
        protected_symbols=["600000.SH"], client_factory=lambda: provider,
        clock=lambda: STAMP, sleep=lambda _: None, guard=lambda api: cli.collection_guard(api, selected))
    return model, api, host, provider, arguments


class DemandHost(Host):
    def __init__(self, **options):
        super().__init__()
        self.fixture = ready(**options)
        self.shared = self.fixture["backend"].shared
        self.intent = request(self.fixture)
        self.now = STAMP
        self.config = single_config()
        self.config["legacy_reference"] = self.fixture["legacy_reference"]
        self.config["kernel_adoption"] = {key: self.fixture["proof"][key]
            for key in ("run_id", "artifact_id", "sha256", "code_sha")}

    def latest(self):
        generation = "od-" + digest(encoded(self.intent["id"]))[:40] + "-" + digest(encoded(self.intent))[:48]
        ref = ft.latest_ftshare_reference(self.read(), REGISTRY,
            collection_id=self.fixture["collection_id"], generation=generation)
        return None if ref is None else ft.restore_ftshare_collection(ref, REGISTRY, self.read())


def execute(host, provider=None, **options):
    arguments = dict(operation="ftshare-sync", config=host.config, registry=REGISTRY,
        open_unit=host.open, access_confirmed=True, custody_authorized=True,
        on_demand=host.intent, protected_symbols=["600000.SH"], clock=lambda: host.now,
        sleep=lambda _: None,
        client_factory=(lambda: provider) if provider is not None else
            lambda: pytest.fail("不得构造来源客户端"))
    return cli.run_collection(**{**arguments, **options})


@pytest.mark.parametrize("initial,settlement_remaining", [(0, None), (5, None), (0, 230)])
def test_native_store_finishes_one_source_beyond_source_admission_threshold(tmp_path, monkeypatch, initial, settlement_remaining):
    model, api, host, provider, arguments = native_budget_setup(tmp_path, initial=initial)
    before_http, after_response, store_costs = [], [], []
    for name in ("retain_ftshare_collection", "retain_ftshare_single"):
        original_retain = getattr(cli, name)
        def retain(*args, _retain=original_retain, **kwargs):
            before = api._requests
            saved = _retain(*args, **kwargs)
            store_costs.append(api._requests - before)
            return saved
        monkeypatch.setattr(cli, name, retain)
    original = provider.candlesticks
    def request(*args, **kwargs):
        before_http.append(api._requests)
        result = original(*args, **kwargs)
        # HTTP 往返期间的既有消耗可能已越过新来源阈值；结算阶段只核真实硬余量。
        api._requests = max(api._requests, cli.SOURCE_ADMISSION_API_REQUESTS + 20)
        if settlement_remaining is not None:
            api.rate_limits.observe({"x-ratelimit-resource": "core", "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(settlement_remaining), "x-ratelimit-reset": "2000000000"})
            assert not cli._quota(api), "来源后余量无需再满足新来源的全部剩余硬预算"
        return result
    provider.candlesticks = request
    def sleep(_):
        assert model.writes == 3
        after_response.append(api._requests)
    result = cli.run_collection(**{**arguments, "sleep": sleep})
    assert result["receipt"]["status"] == "complete", json.dumps(result["receipt"], ensure_ascii=False, indent=2)
    assert provider.requests == result["receipt"]["source_requests"] == result["receipt"]["reserved_requests"] == 1
    assert len(before_http) == 1 and before_http[0] < cli.SOURCE_ADMISSION_API_REQUESTS
    assert after_response and after_response[0] > cli.SOURCE_ADMISSION_API_REQUESTS
    assert initial + len(model.requests) <= api._requests < cli.MAX_REQUESTS == 512
    assert api._requests - before_http[0] <= cli.ON_DEMAND_SETTLEMENT_REQUESTS
    assert len(store_costs) == 4 and max(store_costs) <= cli.ON_DEMAND_STORE_REQUESTS
    assert not any("?status=in_progress" in path for _, path, _ in model.requests)
    assert host.latest()["collection"]["units"]["none:600001.SH"]["status"] == "complete"
    assert model.writes == 4


# 每个参数都必须真的不足：初始已用逼近硬限，或原生余量不足以覆盖本轮预留。
@pytest.mark.parametrize("initial,native_remaining", [(0, 0), (110, 5000), (240, 5000), (0, 200)])
def test_native_store_low_hard_or_native_budget_refuses_before_source_and_reservation(tmp_path, initial, native_remaining):
    model, api, host, provider, arguments = native_budget_setup(tmp_path, initial=initial, native_remaining=native_remaining)
    result = cli.run_collection(**arguments)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["source_requests"] == provider.requests == model.writes == 0
    assert result["receipt"]["reason_code"] in {"FTSHARE_STORE_ADMISSION_BUDGET", "FTSHARE_STORE_SETTLEMENT_BUDGET"}
    assert api._requests < cli.MAX_REQUESTS


@pytest.mark.parametrize("retained", [False, True])
@pytest.mark.parametrize("change", ["run_id", "artifact_id", "sha256", "code_sha", "missing", "none"])
def test_on_demand_rejects_untrusted_adoption_before_capacity_client_reservation_or_source(monkeypatch, retained, change):
    host = DemandHost()
    if retained:
        assert execute(host, SingleProvider(host))["receipt"]["status"] == "complete"
    if change == "missing":
        del host.config["kernel_adoption"]
    elif change == "none":
        host.config["kernel_adoption"] = None
    else:
        proof = host.config["kernel_adoption"]
        proof[change] = proof[change] + 1 if change in {"run_id", "artifact_id"} else (
            "0" if proof[change][0] != "0" else "1") + proof[change][1:]
    trusted = deepcopy(host.config)
    before, commits = host.read().backend.head(), len(host.commits)
    def forbidden(*args, **kwargs):
        pytest.fail("资格拒绝必须先于容量、客户端、预留、写入及来源调用")
    for name in ("retain_ftshare_collection", "begin_ftshare_slot", "retain_ftshare_single", "capture_single"):
        monkeypatch.setattr(cli, name, forbidden)
    provider = SingleProvider(host)
    monkeypatch.setattr(provider, "validate_credential", forbidden)
    monkeypatch.setattr(provider, "candlesticks", forbidden)
    result = execute(host, provider, capacity=forbidden, client_factory=forbidden, guard=forbidden)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["reason_code"] == "FTSHARE_RUNTIME_KERNEL_QUALIFICATION_CHANGED"
    assert result["receipt"]["source_requests"] == provider.requests == 0
    assert result["receipt"]["reserved_requests"] == int(retained)
    assert host.read().backend.head() == before and len(host.commits) == commits
    assert host.config == trusted


def test_original_loop_keeps_planner_result_clock_frozen_refs_and_original_response(monkeypatch):
    host = DemandHost(completion_policy=EOD_COMPLETION_POLICY)
    trusted = deepcopy(host.config)
    original = cli.plan_ftshare_on_demand
    calls = []
    def plan(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        return result
    monkeypatch.setattr(cli, "plan_ftshare_on_demand", plan)
    monkeypatch.setattr(cli, "plan_ftshare_single_collection", lambda *a, **k: pytest.fail("不得重造计划"))
    saved_collections = []
    retain = cli.retain_ftshare_collection
    def retain_control(collection, *args, **kwargs):
        saved_collections.append(deepcopy(collection))
        return retain(collection, *args, **kwargs)
    monkeypatch.setattr(cli, "retain_ftshare_collection", retain_control)
    provider = SingleProvider(host)
    result = execute(host, provider)
    assert result["receipt"]["status"] == "complete", result["receipt"]
    assert len(calls) == 1 and saved_collections[0] == calls[0]["collection"]
    assert saved_collections[0]["plan"]["created_at"] == STAMP
    assert [value["revision"] for value in saved_collections] == [0, 1, 2]
    assert result["receipt"]["source_requests"] == result["receipt"]["source_budget"] == result["receipt"]["reserved_requests"] == 1
    assert len(host.commits) == 4 and provider.requests == 1
    collection = host.latest()["collection"]
    prior = ft.restore_ftshare_collection(host.fixture["base"], REGISTRY, host.read())["collection"]
    for field in ("start", "end", "calendar_end", "calendar_references", "master_references", "completion_policy", "legacy_reference", "adoption"):
        assert collection["plan"][field] == prior["plan"][field]
    from decision_kernel.application.ftshare_single import restore_single
    slot = collection["plan"]["new_slots"][0]
    captured = restore_single(collection["units"][slot]["reference"], host.read(), request=collection["plan"]["requests"][slot])
    assert captured["body_utf8"].encode() == provider.last_response_bytes
    before, commits = host.read().backend.head(), len(host.commits)
    host.now = LATER
    repeated = execute(host)
    assert repeated["receipt"]["status"] == "already-complete"
    assert repeated["reference"] == result["reference"] and repeated["receipt"]["source_requests"] == 0
    status = execute(host, operation="ftshare-status", access_confirmed=False, custody_authorized=False)
    assert status["receipt"]["status"] == "read" and status["reference"] == result["reference"]
    assert host.read().backend.head() == before and len(host.commits) == commits
    assert host.config == trusted
    assert collection["plan"]["adoption"]["proof"].items() >= trusted["kernel_adoption"].items()
    assert collection["plan"]["generation"] != trusted["generation"]
    assert collection["plan"]["targets"] != trusted["kernel_sync"]["targets"]
    assert collection["plan"].get("completion_policy") != trusted["kernel_sync"].get("completion_policy")


def test_missing_status_is_readonly_without_capacity_guard_or_credentials():
    host = DemandHost()
    result = execute(host, operation="ftshare-status", access_confirmed=False, custody_authorized=False,
        capacity=lambda _: pytest.fail("status不得检查写容量"), guard=lambda _: pytest.fail("status无副作用"))
    assert result["receipt"]["status"] == "not-retained" and result["reference"] is None
    assert result["receipt"]["source_requests"] == 0 and not host.commits


@pytest.mark.parametrize("failure,status", [(403, "source-held"), (429, "source-held"),
    (RecordError("FTSHARE_HTTP_UNAVAILABLE"), "exhausted")])
def test_failed_or_held_intent_never_reissues(failure, status):
    host = DemandHost()
    first = execute(host, SingleProvider(host, fail=failure))
    assert first["receipt"]["status"] == "failed"
    before = host.read().backend.head()
    again = execute(host)
    assert again["receipt"]["status"] == status and again["receipt"]["source_requests"] == 0
    assert again["receipt"]["reserved_requests"] == 1 and host.read().backend.head() == before


def test_unknown_genesis_reconciles_same_clock_and_has_only_one_allowance():
    host = DemandHost()
    def lose_receipt(backend, files):
        if len(host.commits) == 1:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_commit = lose_receipt
    first = execute(host, SingleProvider(host))
    assert first["receipt"]["status"] == "persistence-unconfirmed"
    assert first["receipt"]["source_requests"] == 0
    host.after_commit = None
    original = host.latest()["collection"]
    host.now = LATER
    result = execute(host, SingleProvider(host))
    assert result["receipt"]["status"] == "complete", result["receipt"]
    assert result["receipt"]["source_requests"] == result["receipt"]["reserved_requests"] == 1
    assert host.latest()["collection"]["plan"] == original["plan"]


def test_reentry_keeps_exact_reference_after_another_intent_advances_frontier():
    host = DemandHost()
    first_intent = deepcopy(host.intent)
    first = execute(host, SingleProvider(host))
    host.intent.update(id="second", symbol="600002.SH")
    second = execute(host, SingleProvider(host))
    assert second["receipt"]["status"] == "complete", second["receipt"]
    host.intent = first_intent
    before = host.read().backend.head()
    again = execute(host)
    assert again["receipt"]["status"] == "already-complete"
    assert again["reference"] == first["reference"] and again["receipt"]["source_requests"] == 0
    assert host.read().backend.head() == before


@pytest.mark.parametrize("stop", [2, 3, 4])
def test_unknown_reservation_original_or_settlement_never_reissues(stop):
    host = DemandHost()
    def lose_receipt(backend, files):
        if len(host.commits) == stop:
            raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_commit = lose_receipt
    first = execute(host, SingleProvider(host))
    assert first["receipt"]["status"] == "persistence-unconfirmed", first["receipt"]
    assert first["receipt"]["source_requests"] == (0 if stop == 2 else 1)
    host.after_commit = None
    before = host.read().backend.head()
    again = execute(host)
    assert again["receipt"]["source_requests"] == 0 and again["receipt"]["reserved_requests"] == 1
    assert again["receipt"]["status"] == ("already-complete" if stop == 4 else "blocked")
    assert host.read().backend.head() == before


@pytest.mark.parametrize("change", ["reason", "symbol", "adjust_kind", "id"])
def test_changed_intent_or_renamed_consumed_slot_rejected(change):
    host = DemandHost()
    execute(host, SingleProvider(host))
    host.intent[change] = {"reason": "不同用途", "symbol": "600002.SH", "adjust_kind": "forward", "id": "different"}[change]
    before = host.read().backend.head()
    result = execute(host)
    assert result["receipt"]["reason_code"] == ("FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED" if change == "id" else "FTSHARE_ON_DEMAND_INTENT_CONFLICT")
    assert result["receipt"]["source_requests"] == 0 and host.read().backend.head() == before


@pytest.mark.parametrize("gate", ["capacity", "quota", "guard-quota"])
def test_admission_before_secret_reservation_or_http(gate):
    host = DemandHost()
    options = {}
    if gate == "capacity":
        options["capacity"] = lambda _: {"admit_work_unit": False}
    elif gate == "quota":
        host.initial_api_count = cli.SOURCE_ADMISSION_API_REQUESTS
    else:
        options["guard"] = lambda api: setattr(api, "_requests", cli.SOURCE_ADMISSION_API_REQUESTS)
    result = execute(host, **options)
    assert result["receipt"]["status"] == "blocked", result["receipt"]
    assert result["receipt"]["source_requests"] == 0 and not host.commits


@pytest.mark.parametrize("protection", [None, [], ["600001.SH"]])
def test_runtime_cannot_default_away_protected_symbols(protection):
    host = DemandHost()
    if protection is None or protection == []:
        with pytest.raises(RecordError, match="PROTECTED_SYMBOLS"):
            execute(host, protected_symbols=protection)
    else:
        # 直接走真实首次 reader，不能在 fixture 的准备步骤提前拒绝。
        result = cli.run_collection(operation="ftshare-sync", config=host.config, registry=REGISTRY,
            open_unit=host.open, access_confirmed=True, custody_authorized=True,
            on_demand=host.intent, protected_symbols=protection, clock=lambda: host.now,
            client_factory=lambda: pytest.fail("保护证券不得读取来源secret"))
        assert result["receipt"]["reason_code"] == "FTSHARE_EXPANSION_PROTECTED_SYMBOL"
    assert not host.commits


def native_request():
    return {"schema": cli.REQUEST_SCHEMA, "id": "research-one", "base_reference": deepcopy(cli.BASE_REFERENCE),
        "symbol": "600001.SH", "adjust_kind": "none", "reason": "合成离线测试"}


def demand_input(tmp_path):
    event, env = single_input(tmp_path)
    event["inputs"]["selection"] = json.dumps({"on_demand": native_request()})
    return event, env


@pytest.mark.parametrize("extra", ["source_budget", "start", "end", "created_at", "calendar_references",
    "master_references", "completion_policy", "protected_symbols", "targets"])
def test_settings_rejects_all_caller_scope_budget_clock_protection_overrides(tmp_path, extra):
    event, env = demand_input(tmp_path)
    value = native_request()
    value[extra] = 1
    event["inputs"]["selection"] = json.dumps({"on_demand": value})
    with pytest.raises(RecordError):
        cli.settings(event, env, single_config())


@pytest.mark.parametrize("change", ["mixed", "units", "adoption", "size", "reason", "foreign", "selection"])
def test_settings_strict_mutual_exclusion_size_and_identity(tmp_path, change):
    event, env = demand_input(tmp_path)
    selection = json.loads(event["inputs"]["selection"])
    if change == "mixed":
        selection["collection_id"] = "ftshare-cn-eod"
    elif change == "units":
        event["inputs"]["work_units"] = "2"
    elif change == "adoption":
        event["inputs"]["operation"] = "ftshare-adopt-kernel"
    elif change == "size":
        selection["on_demand"]["reason"] = "x" * 6000
    elif change == "reason":
        selection["on_demand"]["reason"] = "中" * 342
    elif change == "foreign":
        selection["on_demand"]["base_reference"]["manifest"]["repository"] = "other/repo"
    else:
        selection["on_demand"]["base_reference"]["selection"] = {"kind": "lines", "start": 1, "end": 2}
    event["inputs"]["selection"] = json.dumps(selection)
    with pytest.raises(RecordError):
        cli.settings(event, env, single_config())


@pytest.mark.parametrize("operation", ["ftshare-sync", "ftshare-status"])
def test_runtime_settings_to_execute_binds_trusted_policy_and_deferred_secret(tmp_path, monkeypatch, operation):
    event, values = demand_input(tmp_path)
    event["inputs"]["operation"] = operation
    class Environment(dict):
        def get(self, key, *args):
            assert key != "FTSHARE_API_KEY", "execute之前以及status不得读取来源secret"
            return super().get(key, *args)
    env = Environment(values)
    config_path = tmp_path / "ftshare.json"
    config_path.write_bytes(encoded(single_config()))
    monkeypatch.setattr(cli, "CONFIG_FILE", config_path)
    policy = json.loads((Path(__file__).resolve().parents[1] / "config/daily-market.json").read_bytes())
    policy["symbols"] = ["600001.SH"]
    policy["max_price_requests"] = 2
    policy_path = tmp_path / "policy.json"
    policy_path.write_bytes(encoded(policy))
    monkeypatch.setattr(cli, "POLICY_FILE", policy_path)
    def run(**kwargs):
        assert kwargs["on_demand"] == native_request() and kwargs["protected_symbols"] == ["600001.SH"]
        assert kwargs["max_work_units"] == 1 and kwargs["operation"] == operation
        assert (kwargs["client_factory"] is None) == (operation == "ftshare-status")
        assert kwargs["kernel_loader"] is None
        return {"receipt": {"status": "not-retained", "source_requests": 0}, "reference": None}
    assert cli.run_runtime(event, env, tmp_path / "out", execute=run) == 2


@pytest.mark.parametrize("operation,retained", [("ftshare-sync", False), ("ftshare-status", False),
    ("ftshare-status", True)])
def test_real_runtime_dispatch_executes_original_loop_without_eager_secret(tmp_path, monkeypatch, operation, retained):
    from decision_kernel.infrastructure import ftshare_http
    host = DemandHost()
    saved = execute(host, SingleProvider(host)) if retained else None
    event, values = demand_input(tmp_path)
    event["inputs"]["operation"] = operation
    reads = []
    class Environment(dict):
        def get(self, key, *args):
            if key == "FTSHARE_API_KEY":
                assert operation == "ftshare-sync"
                reads.append(key)
            return super().get(key, *args)
    path = tmp_path / "ftshare.json"
    config = single_config()
    config["kernel_adoption"] = deepcopy(host.config["kernel_adoption"])
    path.write_bytes(encoded(config))
    monkeypatch.setattr(cli, "CONFIG_FILE", path)
    monkeypatch.setattr(cli, "POLICY_FILE", Path(__file__).resolve().parents[1] / "config/daily-market.json")
    native = SingleNativeAPI()
    def remote(binding, token, selected, config):
        def opening(write):
            cli.native_ready(native, selected, config)
            store, fresh, _ = host.open(write)
            return store, fresh, native
        return opening
    monkeypatch.setattr(cli, "_open_remote", remote)
    provider = SingleProvider(host)
    def factory(*, credential):
        assert credential == values["FTSHARE_API_KEY"]
        return provider
    monkeypatch.setattr(ftshare_http, "FTShareHTTP", factory)
    def run(**kwargs):
        assert kwargs["on_demand"] == native_request() and kwargs["max_work_units"] == 1
        # 仅替换合成Store的精确定位、registry和时钟，保留真实宿主guard/factory及循环。
        intent = {**kwargs["on_demand"], "base_reference": host.intent["base_reference"],
            "reason": host.intent["reason"]}
        assert intent == host.intent
        return cli.run_collection(**{**kwargs, "on_demand": intent, "registry": REGISTRY,
            "clock": lambda: host.now, "sleep": lambda _: None})
    out = tmp_path / "runtime"
    code = cli.run_runtime(event, Environment(values), out, execute=run)
    receipt = json.loads((out / "result.json").read_bytes())
    assert receipt["status"] == ("complete" if operation == "ftshare-sync" else "read" if retained else "not-retained"), receipt
    assert code == (0 if operation == "ftshare-sync" or retained else 2)
    assert reads == (["FTSHARE_API_KEY"] if operation == "ftshare-sync" else [])
    assert provider.requests == (1 if operation == "ftshare-sync" else 0)
    if saved:
        assert json.loads((out / "control.json").read_bytes())["reference"] == saved["reference"]
    if operation == "ftshare-status":
        assert not any("?status=in_progress" in path for path in native.calls)


def test_runtime_missing_or_invalid_policy_fails_closed_before_execute(tmp_path, monkeypatch):
    event, env = demand_input(tmp_path)
    path = tmp_path / "ftshare.json"
    path.write_bytes(encoded(single_config()))
    monkeypatch.setattr(cli, "CONFIG_FILE", path)
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(cli, "POLICY_FILE", missing)
    with pytest.raises((RecordError, OSError)):
        cli.run_runtime(event, env, tmp_path / "out", execute=lambda **_: pytest.fail("不得执行"))
    missing.write_bytes(encoded({"symbols": []}))
    with pytest.raises(RecordError):
        cli.run_runtime(event, env, tmp_path / "out", execute=lambda **_: pytest.fail("不得执行"))
