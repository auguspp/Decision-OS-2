"""Finite runtime units, private receipts and real controlled reading entrypoints.

The synthetic backend explicitly inventories its retained unique blobs. It does
not reinterpret an unknown native size as empty or bypass production admission.
"""
from dataclasses import replace
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection import checkpoint_id
from decision_kernel.application.market_collection_custody import (
    CHECKPOINT_SCHEMA, restore_market_checkpoint, restore_market_collection,
)
from decision_kernel.application.market_collection_reading import latest_market_reference
from decision_kernel.application.market_collection_runtime import run_collection
from decision_kernel.application.market_custody import QUERY_SCHEMA
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubBinding, github_tree_inventory
from decision_kernel.infrastructure import market_snapshot
from decision_kernel.infrastructure.relay_http import RelayHTTP, SECRET_ENV
from decision_kernel.interfaces import market_collection_cli as cli
from test_market_collection_sync import (
    Clock, CollectionOpener, END, NOW, REGISTRY, ROOT_DIR, http_error,
    manifest_bodies, memory, plan, run, source_calls,
)
from test_market_custody import MemoryBackend
from test_github_store import GitAPI
from test_market_snapshot import local_fetch
from test_news_read_budget import transport


PRIVATE_LOCATION = "synthetic-git:PRIVATE_MARKET_LOCATION"
STORE_TOKEN = "SYNTHETIC_PRIVATE_STORE_TOKEN"
SHA = "a" * 40


class InventoriedBackend(MemoryBackend):
    repository = PRIVATE_LOCATION

    def __init__(self, shared, api, *, write=False, host=None):
        super().__init__(shared)
        self.api, self.host = api, host
        self.binding = GitHubBinding("private-owner/private-market-records", 7, "records/private",
            "b" * 40, "private-market-store", write_enabled=write, visibility="private")

    def market_inventory(self):
        snapshot = self.head()
        paths = self.paths(snapshot)
        directories = {"/".join(parts[:n]) for path in paths for parts in [path.split("/")]
                       for n in range(1, len(parts))}
        # This fixture's explicit storage model retains unique byte blobs across
        # all snapshots and projects the tree response from its synthetic origin.
        # The real Git fixture below measures its actual bare repository.
        blobs = {raw for tree in self.shared["trees"].values() for raw in tree.values()}
        files = {path: len(self.shared["trees"][snapshot][path]) for path in paths}
        return {**github_tree_inventory(self.binding, files, directories=directories),
            "native_bytes": sum(map(len, blobs)), "tree_response_applicability": "required"}

    def read(self, commit, path):
        if self.host is not None and self.host.read_error is not None:
            self.host.read_error(self, commit, path)
        return super().read(commit, path)

    def commit(self, files, *, expected):
        assert self.binding.write_enabled
        self.api._requests += 1
        result = super().commit(files, expected=expected)
        if self.host is not None:
            self.host.commits.append((self.api, files))
            if self.host.after_commit is not None:
                self.host.after_commit(self, files)
        return result


class UnitHost:
    def __init__(self):
        self.shared = MemoryBackend().shared
        self.opened, self.fresh_readers, self.commits = [], [], []
        self.after_commit, self.read_error = None, None

    def open(self, write):
        api = SimpleNamespace(_requests=0, unit=len(self.opened) + 1)
        backend = InventoriedBackend(self.shared, api, write=write, host=self)
        self.opened.append({"api": api, "write": write, "head": backend.head()})

        def fresh():
            reader = InventoriedBackend(self.shared, api, host=self)
            self.fresh_readers.append(reader)
            return VersionedStore(reader)

        return VersionedStore(backend), fresh, api

    def reader(self):
        return VersionedStore(InventoriedBackend(self.shared, SimpleNamespace(_requests=0)))


def execute(host, opener=None, *, request=None, operation="sync", clock=None, **kwargs):
    clock = Clock() if clock is None else clock
    options = {"clock": clock.now, "monotonic": clock.monotonic, "sleep": clock.sleep}
    if opener is not None:
        options["client"] = RelayHTTP(opener=opener, credential="synthetic-key-only")
    return run_collection(operation=operation, plan=request or plan(start=END), registry=REGISTRY,
        open_unit=host.open, attempt_id="synthetic-runtime", access_confirmed=True,
        custody_authorized=True, now=NOW, sync_options=options, **kwargs)


def selection(**changes):
    return {"collection_id": "synthetic-ashare", "generation": "g1", "start": END,
            "end": END, "calendar_end": END, **changes}


def runtime_input(tmp_path):
    event = {"inputs": {"operation": "sync", "code_sha": SHA,
        "selection": json.dumps(selection()), "work_units": "7", "access_and_custody_confirmed": True}}
    binding = {"repository": "private-owner/private-market-records", "repository_id": 7,
        "branch": "records/private", "anchor": "b" * 40, "store_id": "private-market-store",
        "visibility": "private", "write_enabled": True}
    env = {"GITHUB_REPOSITORY": "auguspp/Decision-OS-2", "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA, "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "12345", "GITHUB_RUN_ATTEMPT": "1",
        cli.BINDING_ENV: json.dumps(binding), cli.MARKET_TOKEN_ENV: STORE_TOKEN}
    return event, env


def assert_public(value):
    text = encoded(value).decode()
    for private in (PRIVATE_LOCATION, STORE_TOKEN, "private-owner", "private-market-records",
                    "records/private", "private-market-store", "PRIVATE_SOURCE_NAME", "600000.SH",
                    "000001.SZ", "source-views/", '"manifest"', '"wire_sha256"', '"items"', '"entries"'):
        assert private not in text


def test_real_multi_unit_sync_opens_new_api_only_after_published_unit_and_reads_counts():
    host, opener = UnitHost(), CollectionOpener(start=END, end=END)
    result = execute(host, opener, max_work_units=7)
    receipt = result["receipt"]
    assert receipt["status"] == "complete" and receipt["source_requests"] == 7
    assert len(host.opened) == len(receipt["work_units"]) == 7
    assert all(unit["source_requests"] == 1 for unit in receipt["work_units"])
    assert len({id(item["api"]) for item in host.opened}) == 7
    assert all(reader.api is host.opened[reader.api.unit - 1]["api"]
               and reader.binding.write_enabled is False for reader in host.fresh_readers)
    for number, opened in enumerate(host.opened[1:], 1):
        snapshot = MemoryBackend({**host.shared, "head": opened["head"]})
        snapshot.repository = PRIVATE_LOCATION
        reader = VersionedStore(snapshot)
        ref = latest_market_reference(reader, REGISTRY, collection_id="synthetic-ashare")
        collection = restore_market_collection(ref, REGISTRY, reader)["collection"]
        assert collection["attempt"]["id"].endswith("-u" + str(number))
        assert collection["attempt"]["status"] == "partial"
    assert receipt["capacity"]["admit_work_unit"] is True
    assert receipt["health"]["scope_count"] == 4 and receipt["health"]["source_requests"] == 0
    assert result["reading"] is None
    assert_public(receipt)


def test_remote_factory_shares_real_bounded_api_with_each_native_fresh_reader(tmp_path, monkeypatch):
    model = GitAPI(tmp_path / "private-records.git")
    model.binding = replace(model.binding, branch="records")
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository, model.git.binding = model.binding.repository, model.binding
    apis, fetches = [], []

    def original_api_with_synthetic_wire(token):
        assert token == STORE_TOKEN
        api = transport(model)
        apis.append(api)
        return api

    monkeypatch.setattr(cli, "GitHubAPI", original_api_with_synthetic_wire)
    monkeypatch.setattr(market_snapshot, "_fetch_market_branch", local_fetch(model, fetches))
    opener, clock = CollectionOpener(start=END, end=END), Clock()
    result = run_collection(operation="sync", plan=plan(start=END), registry=REGISTRY,
        open_unit=cli._open_remote(model.binding, STORE_TOKEN), attempt_id="native-runtime",
        max_work_units=2, access_confirmed=True, custody_authorized=True, now=NOW,
        sync_options={"client": RelayHTTP(opener=opener, credential="synthetic-key-only"),
                      "clock": clock.now, "monotonic": clock.monotonic, "sleep": clock.sleep})
    assert result["receipt"]["status"] == "partial" and result["receipt"]["source_requests"] == 2
    assert source_calls(opener) == [("trade_cal", "SSE", 0), ("trade_cal", "SZSE", 0)]
    assert len(apis) == 2 and apis[0] is not apis[1]
    assert all(0 < api._requests <= 512 for api in apis)
    assert sum(api._requests for api in apis) == len(model.requests)
    assert {id(item["api"]) for item in fetches} == {id(api) for api in apis}
    assert all(not item["destination"].exists() for item in fetches)
    assert [u["store_api_requests"] for u in result["receipt"]["work_units"]] == [api._requests for api in apis]


def test_status_opens_one_readonly_unit_without_source_credentials_or_mutation(monkeypatch):
    host = UnitHost()
    completed = execute(host, CollectionOpener(start=END, end=END), max_work_units=7)
    assert completed["receipt"]["status"] == "complete"
    before, opened = host.shared["head"], len(host.opened)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    result = execute(host, operation="status")
    assert result["receipt"]["status"] == "read" and result["receipt"]["source_requests"] == 0
    assert result["receipt"]["health"]["scope_count"] == 4
    assert len(host.opened) == opened + 1 and host.opened[-1]["write"] is False
    assert host.shared["head"] == before and result["reading"] is None
    assert_public(result["receipt"])


def test_storage_admission_pauses_same_page_budget_then_next_explicit_unit_resumes_offset():
    host, opener = UnitHost(), CollectionOpener(count=501, start=END, end=END)
    paused = []

    def budget(backend, files):
        for body in manifest_bodies(files, CHECKPOINT_SCHEMA):
            capture = body["capture"]
            if capture["api"] == "stock_basic" and capture["parameters"]["exchange"] == "SSE" \
                    and len(body["pages"]) == 1 and body["control"]["state"] == "ready":
                backend.api._requests = 240
            if body["control"]["state"] == "paused":
                paused.append(body)

    host.after_commit = budget
    result = execute(host, opener, max_work_units=8)
    assert result["receipt"]["status"] == "complete" and len(host.opened) == 8
    assert source_calls(opener)[2:4] == [("stock_basic", "SSE", 0), ("stock_basic", "SSE", 500)]
    assert len(paused) == 1 and paused[0]["control"]["pause_reason"] == "STORAGE_BUDGET"
    assert paused[0]["control"]["max_pages"] == 20
    assert result["receipt"]["work_units"][2]["store_api_requests"] >= 240
    assert host.opened[2]["api"] is not host.opened[3]["api"]
    ref = latest_market_reference(host.reader(), REGISTRY,
        checkpoint_id=checkpoint_id(plan(start=END), "master:SSE"))
    state = restore_market_checkpoint(ref, REGISTRY, host.reader())["state"]
    assert [page["offset"] for page in state["capture"]["pages"]] == [0, 500]
    assert all([a["attempt"] for a in page["attempts"]] == [1] for page in state["capture"]["pages"])


def test_retry_attempt_history_survives_an_explicit_work_unit_deadline():
    host, opener, clock = UnitHost(), CollectionOpener(start=END, end=END), Clock()

    def fail_once(api, params):
        if len(opener.calls) == 1:
            clock.seconds = 550
            raise http_error(503, error="upstream_pool_exhausted", headers={"Retry-After": "50"})

    opener.before = fail_once
    result = execute(host, opener, clock=clock, max_work_units=2)
    assert result["receipt"]["status"] == "partial" and len(host.opened) == 2
    assert source_calls(opener) == [("trade_cal", "SSE", 0), ("trade_cal", "SSE", 0)]
    ref = latest_market_reference(host.reader(), REGISTRY,
        checkpoint_id=checkpoint_id(plan(start=END), "calendar:SSE"))
    state = restore_market_checkpoint(ref, REGISTRY, host.reader())["state"]
    assert [a["attempt"] for a in state["capture"]["pages"][0]["attempts"]] == [1, 2]
    assert clock.waits == [50] and result["receipt"]["source_requests"] == 2


def test_no_progress_stops_before_another_unit_and_reports_the_dependency_gap():
    host, opener = UnitHost(), CollectionOpener(start=END, end=END)

    def invalid_calendar(api, params):
        if api == "trade_cal" and params["exchange"] == "SSE":
            raise http_error(400)

    opener.before = invalid_calendar
    result = execute(host, opener, max_work_units=32)
    assert result["receipt"]["status"] == "blocked"
    assert result["receipt"]["reason_code"] == "ASHARE_NO_PROGRESS"
    assert len(host.opened) == 4 and len(opener.calls) == 3
    assert result["receipt"]["work_units"][-1]["source_requests"] == 0


@pytest.mark.parametrize("failure", ["rate", "auth", "unknown-write", "store-unavailable"])
def test_global_failure_never_opens_a_following_unit(failure):
    host, opener = UnitHost(), CollectionOpener(start=END, end=END)
    if failure in {"rate", "auth"}:
        def denied(api, params):
            raise http_error(429 if failure == "rate" else 403,
                error="rate_limited" if failure == "rate" else "forbidden",
                headers={"Retry-After": "invalid"})
        opener.before = denied
    elif failure == "unknown-write":
        def uncertain(backend, files):
            if list(manifest_bodies(files, QUERY_SCHEMA)):
                raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        host.after_commit = uncertain
    else:
        def unavailable(backend, commit, path):
            if backend.api.unit == 2:
                raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
        host.read_error = unavailable
    if failure == "store-unavailable":
        with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
            execute(host, opener, max_work_units=32)
        assert len(host.opened) == 2 and len(opener.calls) == 1
    else:
        result = execute(host, opener, max_work_units=32)
        assert result["receipt"]["status"] == "blocked"
        assert len(host.opened) == 1 and len(opener.calls) == 1
        assert_public(result["receipt"])


def test_unknown_native_inventory_blocks_without_source_or_a_second_unit():
    backend, store, fresh = memory()
    opened, opener = [], CollectionOpener(start=END, end=END)

    def open_unit(write):
        opened.append(write)
        return store, fresh, None

    result = run_collection(operation="sync", plan=plan(start=END), registry=REGISTRY,
        open_unit=open_unit, attempt_id="capacity-runtime", max_work_units=32,
        access_confirmed=True, custody_authorized=True, now=NOW,
        sync_options={"client": RelayHTTP(opener=opener, credential="synthetic-key-only")})
    assert market_capacity(store)["native_bytes"] is None
    assert result["receipt"]["reason_code"] == "ASHARE_STORAGE_CAPACITY"
    assert opened == [True] and opener.calls == [] and backend.shared["count"] == 0


def test_completed_runtime_sync_checks_same_generation_plan_before_fastpath():
    host = UnitHost()
    complete = execute(host, CollectionOpener(start="20261015", end=END),
                       request=plan(start="20261015", end="20261015"), max_work_units=7)
    assert complete["receipt"]["status"] == "complete"
    later = CollectionOpener(start="20261015", end=END)
    before = host.shared["head"]
    with pytest.raises(RecordError, match="ASHARE_RESUME_PLAN_CHANGED"):
        execute(host, later, request=plan(start="20261015"))
    assert later.calls == [] and host.shared["head"] == before


@pytest.mark.parametrize("change", ["repository", "ref", "event", "sha", "authorization", "units",
                                    "selection", "binding-missing", "public", "token", "readonly"])
def test_runtime_main_configuration_gate_precedes_all_source_and_store_execution(tmp_path, monkeypatch, change):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    if change == "repository": env["GITHUB_REPOSITORY"] = "another/project"
    elif change == "ref": env["GITHUB_REF"] = "refs/heads/feature"
    elif change == "event": env["GITHUB_EVENT_NAME"] = "push"
    elif change == "sha": event["inputs"]["code_sha"] = "f" * 40
    elif change == "authorization": event["inputs"]["access_and_custody_confirmed"] = False
    elif change == "units": event["inputs"]["work_units"] = "33"
    elif change == "selection": event["inputs"]["selection"] = json.dumps({**selection(), "sessions": [END]})
    elif change == "binding-missing": env.pop(cli.BINDING_ENV)
    elif change == "token": env.pop(cli.MARKET_TOKEN_ENV)
    else:
        binding = json.loads(env[cli.BINDING_ENV])
        binding.update({"visibility": "public"} if change == "public" else {"write_enabled": False})
        env[cli.BINDING_ENV] = json.dumps(binding)
    calls = []

    def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("configuration gate invoked execution")

    out = tmp_path / "runtime"
    try:
        code = cli.run_runtime(event, env, out, execute=forbidden)
    except (RecordError, ValueError):
        code = 2
    assert code == 2 and calls == []
    if (out / "result.json").exists():
        receipt = json.loads((out / "result.json").read_text())
        assert receipt["source_requests"] == 0
        assert_public(receipt)


def test_public_runtime_artifact_uses_real_sync_counts_and_no_private_result_members(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    host, opener, clock = UnitHost(), CollectionOpener(start=END, end=END), Clock()
    actual_results = []

    def actual(**kwargs):
        kwargs.update(open_unit=host.open, now=NOW,
            sync_options={"client": RelayHTTP(opener=opener, credential="synthetic-key-only"),
                          "clock": clock.now, "monotonic": clock.monotonic, "sleep": clock.sleep})
        result = run_collection(**kwargs)
        actual_results.append(result)
        return result

    out = tmp_path / "runtime"
    assert cli.run_runtime(event, env, out, execute=actual) == 0
    receipt = json.loads((out / "result.json").read_text())
    assert receipt["status"] == "complete" and receipt["source_requests"] == len(opener.calls) == 7
    assert len(host.opened) == 7 and receipt["health"]["scope_count"] == 4
    assert set(path.name for path in out.iterdir()) == {"result.json"}
    assert PRIVATE_LOCATION in encoded(actual_results[0]["reference"]).decode()
    assert "reference" not in receipt and "reading" not in receipt
    assert_public(receipt)


@pytest.mark.parametrize("after_source", [False, True])
def test_public_runtime_global_error_keeps_completed_source_counts_and_no_private_error_prose(tmp_path, monkeypatch, after_source):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    host, opener, clock = UnitHost(), CollectionOpener(start=END, end=END), Clock()

    def failure(backend, commit, path):
        if backend.api.unit == 2 and (not after_source or len(opener.calls) == 2):
            raise StoreUnavailable("private-owner/private-market-records private failure body")

    host.read_error = failure

    def actual(**kwargs):
        kwargs.update(open_unit=host.open, now=NOW,
            sync_options={"client": RelayHTTP(opener=opener, credential="synthetic-key-only"),
                          "clock": clock.now, "monotonic": clock.monotonic, "sleep": clock.sleep})
        return run_collection(**kwargs)

    out = tmp_path / "runtime"
    assert cli.run_runtime(event, env, out, execute=actual) == 2
    receipt = json.loads((out / "result.json").read_text())
    assert receipt["status"] == "blocked" and receipt["source_requests"] == len(opener.calls) == (2 if after_source else 1)
    assert len(host.opened) == 2
    assert receipt["reason_code"] == ("ASHARE_COLLECTION_PERSISTENCE_UNCONFIRMED" if after_source else "ASHARE_OPERATION_FAILED")
    assert_public(receipt)


def test_cli_existing_output_is_checked_before_any_store_or_source_operation(tmp_path, monkeypatch):
    chosen, output = tmp_path / "selection.json", tmp_path / "result.json"
    chosen.write_bytes(encoded(selection()))
    output.write_text("keep exact existing output")
    calls = []

    def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("existing output reached execution")

    monkeypatch.setattr(cli, "run_collection", forbidden)
    code = cli.main(["sync", "--selection", str(chosen), "--registry", str(ROOT_DIR / cli.REGISTRY),
        "--path", str(tmp_path / "not-opened.git"), "--out", str(output),
        "--access-confirmed", "--custody-authorized"])
    assert code == 2 and calls == [] and output.read_text() == "keep exact existing output"


def test_controlled_cli_real_git_read_in_new_process_needs_no_source_credentials(tmp_path):
    root = tmp_path / "retained.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    fresh = lambda: VersionedStore(GitStore(root))
    complete = run(plan(start=END), store, fresh, CollectionOpener(start=END, end=END))
    assert complete["status"] == "complete" and market_capacity(fresh())["admit_work_unit"]
    chosen, output = tmp_path / "selection.json", tmp_path / "controlled-reading.json"
    chosen.write_bytes(encoded(selection()))
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT_DIR / "src")}
    assert SECRET_ENV not in env and cli.MARKET_TOKEN_ENV not in env
    result = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.market_collection_cli",
        "read", "--selection", str(chosen), "--registry", str(ROOT_DIR / cli.REGISTRY),
        "--path", str(root), "--out", str(output)],
        check=True, capture_output=True, text=True, env=env, timeout=45)
    assert result.stdout == "" and result.stderr == ""
    reading = json.loads(output.read_text())
    assert reading["receipt"]["status"] == "read" and reading["receipt"]["source_requests"] == 0
    assert reading["reading"]["source_requests"] == 0 and reading["reading"]["view"]["scope_count"] == 4
    assert encoded(reading["reference"]) == encoded(complete["reference"])
    assert "600000.SH" in encoded(reading["reading"]).decode()


def test_workflow_exposes_only_explicit_main_operations_and_count_receipt():
    flow = yaml.load((ROOT_DIR / ".github/workflows/ashare-data.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"}
    assert flow["permissions"] == {} and flow["concurrency"]["group"] == "decision-os-relay-source"
    assert flow["concurrency"]["cancel-in-progress"] == "false"
    inputs = flow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["operation"]["options"] == ["status", "sync"]
    assert inputs["access_and_custody_confirmed"]["default"] == "false"
    steps = flow["jobs"]["run"]["steps"]
    sync = next(step for step in steps if step.get("if") == "inputs.operation == 'sync'")
    status = next(step for step in steps if step.get("if") == "inputs.operation == 'status'")
    assert SECRET_ENV in sync["env"] and SECRET_ENV not in status["env"]
    assert cli.MARKET_TOKEN_ENV in sync["env"] and cli.MARKET_TOKEN_ENV in status["env"]
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["path"] == "${{ runner.temp }}/dos2-ashare-data/result.json"
    assert upload["if"] == "always()"
