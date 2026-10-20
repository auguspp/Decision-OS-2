"""Daily master ownership over real Relay capture/custody; all wire data synthetic."""
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application import daily_master_refresh as refresh
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection_custody import retain_market_checkpoint, restore_market_checkpoint
from decision_kernel.application.market_collection_reading import latest_market_reference
from decision_kernel.application.market_custody import restore_market_query
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import API_ROOT, GitHubAPI, GitHubStore, MAX_REQUESTS
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.interfaces.daily_market_cli import _admit
from test_github_transport import Response as GitHubResponse
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY, Response
from test_market_session_coverage import SessionOpener
from test_market_snapshot import local_fetch, native

STAMP = "2026-10-20T12:00:00+00:00"


class Clock:
    def __init__(self):
        self.seconds, self.waits = 0, []

    def now(self):
        return (datetime.fromisoformat(STAMP) + timedelta(seconds=self.seconds)).isoformat()

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        assert 0 < seconds <= 60
        self.waits.append(seconds)
        self.seconds += seconds


def cycle():
    return {"schema": "daily-market-cycle-v4", "date": "20261020", "created_at": STAMP,
        "status": "master-pending", "master_refresh": deepcopy(refresh.POLICY),
        "masters": {venue: {"status": "in-flight" if venue == "SSE" else "pending",
            "reserved_queries": int(venue == "SSE"), "reference": None,
            "checkpoint_reference": None, "reason_code": None} for venue in ("SSE", "SZSE")}}


class Host:
    def __init__(self, backend=None, count=2):
        self.backend = backend or MemoryBackend()
        self.store = VersionedStore(self.backend)
        self.fresh = lambda: VersionedStore(copy(self.backend))
        self.cycle, self.clock, self.clients = cycle(), Clock(), []
        self.opener = SessionOpener(count=count)

    def client(self):
        client = RelayHTTP(opener=self.opener, credential="synthetic-master-only", request_timeout_seconds=90)
        self.clients.append(client)
        return client

    def run(self, **options):
        return refresh.run_master_unit(**{**dict(cycle=self.cycle, venue="SSE", registry=REGISTRY,
            store=self.store, fresh_reader=self.fresh, client_factory=self.client,
            clock=self.clock.now, monotonic=self.clock.monotonic, sleep=self.clock.sleep), **options})

    def checkpoint(self):
        reference = latest_market_reference(self.fresh(), REGISTRY,
            checkpoint_id=refresh.checkpoint_id(self.cycle, "SSE"))
        return restore_market_checkpoint(reference, REGISTRY, self.fresh())

    def complete(self, result):
        self.cycle["masters"]["SSE"].update(status="complete", reference=result["reference"],
            checkpoint_reference=result["checkpoint_reference"])


@pytest.mark.parametrize("change", [
    {"page_size": 1000}, {"max_pages_per_venue": 21}, {"max_attempts_per_page": 5},
    {"max_wait_seconds_per_page": 181}, {"max_attempts_per_page": 4.0},
    {"venues": ["SZSE", "SSE"]}, {"venues": ["SSE"]}, {"list_status": "D"},
    {"trigger": "source-response-instruction"}, {"force": True},
])
def test_policy_is_exact_and_does_not_grant_alternate_scope(change):
    with pytest.raises(RecordError, match="DAILY_MASTER_POLICY"):
        refresh.policy({**refresh.POLICY, **change})
    accepted = refresh.policy(refresh.POLICY)
    accepted["venues"].append("BSE")
    assert refresh.POLICY["venues"] == ["SSE", "SZSE"]


def test_owner_is_one_daily_venue_and_plan_is_complete_current_l():
    value = cycle()
    plan = refresh.master_plan(value, REGISTRY)
    assert plan["case_id"] == "daily-master-20261020"
    assert [row["parameters"]["exchange"] for row in plan["requests"]] == ["SSE", "SZSE"]
    assert all(row["api"] == "stock_basic" and row["parameters"]["list_status"] == "L"
        and row["parameters"]["limit"] == "500"
        and set(row["parameters"]["fields"].split(",")) == refresh.MASTER_FIELDS
        for row in plan["requests"])
    owner = refresh.checkpoint_id(value, "SSE")
    value["created_at"] = "2026-10-21T12:00:00Z"
    assert refresh.checkpoint_id(value, "SSE") == owner
    assert refresh.checkpoint_id(value, "SZSE") != owner


def test_three_pages_use_three_units_and_original_query_uses_same_page_references():
    host = Host(count=1200)
    outcomes = [host.run() for _ in range(3)]
    assert [row["status"] for row in outcomes] == ["partial", "partial", "complete"], outcomes
    assert [row["source_requests"] for row in outcomes] == [1, 1, 1]
    assert [row["page_count"] for row in outcomes] == [1, 2, 3]
    assert [row["cumulative_source_requests"] for row in outcomes] == [1, 2, 3]
    assert [int(params.get("offset", 0)) for _, params in host.opener.calls] == [0, 500, 1000]
    assert all(client.requests == 1 for client in host.clients)
    final = outcomes[-1]
    saved = host.checkpoint()
    restored = restore_market_query(final["reference"], REGISTRY, host.fresh())
    assert saved["state"]["control"]["state"] == "complete"
    assert restored["page_references"] == saved["page_references"]
    assert sum(len(page["table"]["items"]) for page in restored["result"]["capture"]["pages"]) == 1200
    host.complete(final)
    assert refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh()) == restored


def test_checkpoint_reservation_is_durably_saved_before_http():
    host = Host()
    observed = []

    def before(api, params):
        saved = host.checkpoint()["state"]
        observed.append((saved["control"]["state"], saved["control"]["retry"]["phase"],
            saved["control"]["retry"]["reserved_attempt"]))

    host.opener.before = before
    result = host.run()
    assert result["status"] == "complete", result
    assert observed == [("executing", "in-flight", 1)]


def test_lost_query_response_promotes_complete_checkpoint_with_zero_credentials(monkeypatch):
    host = Host()
    retain = refresh.retain_market_query

    def lost(*args, **kwargs):
        retain(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")

    monkeypatch.setattr(refresh, "retain_market_query", lost)
    first = host.run()
    assert first["status"] == "persistence-unconfirmed" and first["source_requests"] == 1
    assert first["cumulative_source_requests"] == 1 and first["reference"] is None
    assert host.checkpoint()["state"]["control"]["state"] == "complete"
    monkeypatch.setattr(refresh, "retain_market_query", retain)
    before = host.backend.head()
    final = host.run(client_factory=lambda: pytest.fail("complete checkpoint acquired credentials"))
    assert final["status"] == "complete" and final["source_requests"] == 0
    assert final["cumulative_source_requests"] == 1 and len(host.opener.calls) == 1
    assert host.backend.head() == before  # Exact original query journal reconciles without another write.
    host.complete(final)
    refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh())


def test_retry_budget_survives_new_clients_without_replaying_accepted_pages():
    host = Host(count=501)
    first = host.run()
    assert first["status"] == "partial" and first["page_count"] == 1
    host.opener.fail, host.opener.once = "stock_basic", True
    second = host.run()
    assert second["status"] == "partial" and second["source_requests"] == 1
    failed = host.checkpoint()["state"]["control"]["retry"]
    assert len(failed["attempts"]) == 1 and failed["wait_budget_seconds"] == 30
    assert host.clock.waits == [30]
    third = host.run()
    assert third["status"] == "complete" and third["cumulative_source_requests"] == 3
    state = host.checkpoint()["state"]
    assert [len(page["attempts"]) for page in state["capture"]["pages"]] == [1, 2]
    assert [int(params.get("offset", 0)) for _, params in host.opener.calls] == [0, 500, 500]


def test_four_attempts_end_original_page_and_no_new_owner_is_created():
    host = Host()
    host.opener.fail = "stock_basic"
    results = [host.run() for _ in range(4)]
    assert [row["status"] for row in results] == ["partial", "partial", "partial", "failed"], results
    assert [row["cumulative_source_requests"] for row in results] == [1, 2, 3, 4]
    state = host.checkpoint()["state"]
    assert state["control"]["state"] == "terminal"
    assert state["control"]["retry"]["decision"]["reason"] == "attempt-limit"
    assert sum(host.clock.waits) == 90
    before = host.backend.head()
    again = host.run(client_factory=lambda: pytest.fail("failed page constructed a fresh client"))
    assert again["status"] == "failed" and again["source_requests"] == 0
    assert again["cumulative_source_requests"] == 4 and host.backend.head() == before


def test_wait_budget_remains_180_across_units_and_sleeps_are_bounded():
    host = Host()

    def busy(api, parameters):
        raise HTTPError("https://example.invalid/never-followed", 504, "PRIVATE_PROSE",
            {"Retry-After": "180"}, BytesIO(b'{"error":"upstream_timeout"}'))

    host.opener.before = busy
    first = host.run()
    assert first["status"] == "partial", first
    assert host.clock.waits == [60, 60, 60]
    second = host.run()
    assert second["status"] == "failed" and second["cumulative_source_requests"] == 2, second
    state = host.checkpoint()["state"]
    assert state["control"]["retry"]["wait_budget_seconds"] == 180
    assert state["control"]["retry"]["decision"]["reason"] == "wait-budget"


@pytest.mark.parametrize("status", [401, 403, 429])
def test_original_permission_or_rate_stop_never_resumes(status):
    host = Host()

    def denied(api, parameters):
        raise HTTPError("https://example.invalid/secret", status, "PRIVATE_DENIAL", {},
            BytesIO(b'{"error":"unauthorized"}' if status == 401 else b'{"error":"forbidden"}'
                if status == 403 else b'{"error":"rate_limited"}'))

    host.opener.before = denied
    first = host.run()
    assert first["status"] == "source-held" and first["source_requests"] == 1, first
    before = host.backend.head()
    second = host.run(client_factory=lambda: pytest.fail("global stop acquired credentials"))
    assert second["status"] == "source-held" and second["source_requests"] == 0
    assert host.backend.head() == before and len(host.opener.calls) == 1
    assert "PRIVATE" not in encoded((first, second)).decode() and "secret" not in encoded((first, second)).decode()


def test_guard_failure_after_http_keeps_actual_count_and_durable_unknown_reservation():
    host = Host()
    occurred = []
    host.opener.before = lambda api, parameters: occurred.append(True)

    def guard():
        if occurred:
            raise StoreUnavailable("PUBLIC_BINDING_MISMATCH")

    first = host.run(guard=guard)
    assert first["status"] == "persistence-unconfirmed" and first["source_requests"] == 1, first
    assert first["reason_code"] == "PUBLIC_BINDING_MISMATCH"
    assert host.checkpoint()["state"]["control"]["retry"]["phase"] == "in-flight"
    second = host.run(client_factory=lambda: pytest.fail("unknown reservation repeated HTTP"))
    assert second["status"] == "persistence-unconfirmed" and second["source_requests"] == 0
    assert second["cumulative_source_requests"] is None and len(host.opener.calls) == 1


def test_validated_terminal_response_preserves_known_capture_failure_without_retry():
    host = Host()
    host.opener.open = lambda request, timeout: Response(request.full_url, {
        "code": 0, "api_name": "stock_basic", "data": {"fields": ["unrequested_field"], "items": [["value"]]}})
    first = host.run()
    assert first["status"] == "failed" and first["source_requests"] == 1, first
    assert first["reason_code"] == "SOURCE_REQUESTED_FIELDS_MISSING"
    state = host.checkpoint()["state"]
    assert state["control"]["state"] == "terminal" and state["control"]["retry"]["phase"] == "response"
    before = host.backend.head()
    second = host.run(client_factory=lambda: pytest.fail("known failed response sent another request"))
    assert second["status"] == "failed" and second["reason_code"] == first["reason_code"]
    assert second["source_requests"] == 0 and second["cumulative_source_requests"] == 1
    assert host.backend.head() == before


def test_preflight_during_original_checkpoint_readback_stops_before_source():
    host = Host()
    scopes = []

    def stale(reader):
        scopes.append(reader.backend.head())
        if len(scopes) == 2:
            raise RecordError("DAILY_MARKET_STALE_CONTROL")

    result = host.run(preflight=stale)
    assert result["status"] == "persistence-unconfirmed" and result["source_requests"] == 0, result
    assert result["reason_code"] == "DAILY_MARKET_STALE_CONTROL" and len(scopes) == 2
    assert not host.opener.calls
    assert host.checkpoint()["state"]["control"]["retry"]["phase"] == "in-flight"
    before = host.backend.head()
    again = host.run(client_factory=lambda: pytest.fail("unconfirmed reservation silently sent HTTP"))
    assert again["status"] == "persistence-unconfirmed" and again["source_requests"] == 0
    assert again["reason_code"] == "RELAY_CHECKPOINT_INFLIGHT"
    assert again["cumulative_source_requests"] is None
    assert host.backend.head() == before and not host.opener.calls


@pytest.mark.parametrize("already_retained", [False, True])
def test_complete_checkpoint_pauses_before_promotion_when_storage_admission_ends(monkeypatch, already_retained):
    host = Host()
    retain = refresh.retain_market_query
    if already_retained:
        def lost(*args, **kwargs):
            retain(*args, **kwargs)
            raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
        monkeypatch.setattr(refresh, "retain_market_query", lost)
        assert host.run()["status"] == "persistence-unconfirmed"
    monkeypatch.setattr(refresh, "retain_market_query", lambda *a, **kw: pytest.fail("promotion crossed admission"))

    def guard():
        if host.opener.calls and host.checkpoint()["state"]["control"]["state"] == "complete":
            raise RecordError("DAILY_MARKET_STORE_ADMISSION")

    factory = (lambda: pytest.fail("complete checkpoint acquired credentials")) if already_retained else host.client
    paused = host.run(guard=guard, client_factory=factory)
    assert paused["status"] == "partial" and paused["reason_code"] == "RELAY_STORAGE_BUDGET", paused
    assert paused["source_requests"] == (0 if already_retained else 1)
    assert paused["cumulative_source_requests"] == 1 and paused["checkpoint_reference"] is not None
    assert paused["reference"] is None and len(host.opener.calls) == 1
    assert host.checkpoint()["state"]["control"]["state"] == "complete"
    monkeypatch.setattr(refresh, "retain_market_query", retain)
    done = host.run(client_factory=lambda: pytest.fail("promotion repeated source"))
    assert done["status"] == "complete" and done["source_requests"] == 0, done
    assert len(host.opener.calls) == 1


def test_new_cycle_creation_time_cannot_adopt_an_older_checkpoint():
    host = Host(count=501)
    assert host.run()["status"] == "partial"
    before = host.backend.head()
    host.clock.seconds += 60
    host.cycle["created_at"] = host.clock.now()
    result = host.run(client_factory=lambda: pytest.fail("old capture reset its owner"))
    assert result["status"] == "failed" and result["reason_code"] == "DAILY_MASTER_OLD_CAPTURE"
    assert result["source_requests"] == 0 and host.backend.head() == before


def test_existing_owner_unreadable_before_initial_proof_has_unknown_cumulative_count():
    host = Host(count=501)
    assert host.run()["source_requests"] == 1
    before = host.backend.head()

    def unavailable():
        raise StoreUnavailable("REMOTE_NOT_AUTHORIZED")

    result = host.run(fresh_reader=unavailable,
        client_factory=lambda: pytest.fail("unreadable original owner obtained credentials"))
    assert result["source_requests"] == 0 and result["cumulative_source_requests"] is None, result
    assert result["reason_code"] == "REMOTE_NOT_AUTHORIZED"
    assert len(host.opener.calls) == 1 and host.backend.head() == before


def test_foreign_checkpoint_is_not_a_new_empty_owner():
    host = Host()
    original = host.run()
    host.cycle["date"] = "20261021"
    host.cycle["masters"]["SSE"]["checkpoint_reference"] = original["checkpoint_reference"]
    before = host.backend.head()
    result = host.run(client_factory=lambda: pytest.fail("foreign checkpoint reset request allowance"))
    assert result["status"] == "failed" and result["source_requests"] == 0
    assert result["reason_code"] == "ASHARE_CHECKPOINT_ROLLBACK" and host.backend.head() == before


def test_venue_query_cannot_replace_another_complete_checkpoints_original_pages():
    host = Host()
    first = host.run()
    host.cycle["masters"]["SZSE"].update(status="in-flight", reserved_queries=1)
    other = host.run(venue="SZSE")
    host.complete(first)
    host.cycle["masters"]["SSE"]["reference"] = other["reference"]
    with pytest.raises(RecordError, match="DAILY_MASTER_QUERY_CHECKPOINT_CHANGED"):
        refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh())


def test_complete_master_reopens_in_independent_real_git(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "records.git")
    host = Host(backend=backend, count=501)
    assert host.run()["status"] == "partial"
    result = host.run()
    assert result["status"] == "complete" and result["cumulative_source_requests"] == 2, result
    host.complete(result)
    reopened = VersionedStore(GitStore(backend.root))
    restored = refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, reopened)
    assert restored["reference"] == result["reference"]
    assert len(restored["page_references"]) == 2


class QuotaWire:
    """Synthetic native responses; the real Git store owns writes and history."""
    def __init__(self, model):
        self.model, self.counts = model, {"core": 0, "graphql": 0}

    def open(self, request, timeout):
        assert timeout == 30
        resource = "graphql" if request.method == "POST" else "core"
        self.counts[resource] += 1
        value = self.model.request(request.method, request.full_url.removeprefix(API_ROOT),
            json.loads(request.data) if request.data else None)
        return GitHubResponse(encoded(value), request.full_url, headers={
            "x-ratelimit-resource": resource, "x-ratelimit-limit": "10000",
            "x-ratelimit-remaining": str(10000 - self.counts[resource]), "x-ratelimit-reset": "2000000000"})


def native_unit(host, model):
    api, fetches, admitted = GitHubAPI("synthetic-native-master"), [], []
    api._opener = QuotaWire(model)
    store = VersionedStore(GitHubStore(model.binding, api))
    fresh = lambda: VersionedStore(GitMarketReader(replace(model.binding, write_enabled=False), api,
        fetch=local_fetch(model, fetches)))
    prefix = "/repos/" + model.binding.repository
    # Account for native start checks, then execute the original capacity and
    # admission gates. This isolates the module, not the complete daily host.
    for _ in range(4):
        api.request("GET", prefix)
    assert market_capacity(fresh())["admit_work_unit"]
    _admit(api)

    def guard():
        api.request("GET", prefix)
        api.request("GET", prefix + "/git/ref/heads/records")
        _admit(api)
        admitted.append(api._requests)

    result = host.run(store=store, fresh_reader=fresh, guard=guard)
    assert 0 < api._requests == sum(api._opener.counts.values()) < MAX_REQUESTS == 512
    assert admitted and max(admitted) < 240
    assert all(call["api"] is api and not call["destination"].exists() for call in fetches)
    return result, api, fetches


def test_native_two_pages_fit_original_admission_and_one_shared_api_per_unit(native):
    model, _ = native
    host = Host(backend=model.git, count=501)
    first, first_api, _ = native_unit(host, model)
    second, second_api, _ = native_unit(host, model)
    assert first["status"] == "partial" and second["status"] == "complete", (first, second)
    assert first["source_requests"] == second["source_requests"] == 1
    assert second["cumulative_source_requests"] == 2 and first_api is not second_api
    assert [int(params.get("offset", 0)) for _, params in host.opener.calls] == [0, 500]


def test_twentieth_page_and_query_reuse_original_page_refs_in_one_native_unit(native):
    model, local_store = native
    host = Host(backend=model.git, count=9501)
    client, states = host.client(), []
    # Seed the already retained nineteen-page prefix through original capture
    # and custody. This does not claim that nineteen initial writes fit a unit.
    refresh.capture_registered(refresh.master_plan(host.cycle, REGISTRY)["requests"][0], REGISTRY,
        RetryingRelay(client, refresh.RETRY, clock=host.clock.now), max_pages=20,
        checkpoint_writer=lambda state: states.append(deepcopy(state)),
        pause_requested=lambda: client.requests >= 19)
    assert len(states[-1]["capture"]["pages"]) == client.requests == 19
    retain_market_checkpoint(states[-1], REGISTRY, local_store,
        checkpoint_id=refresh.checkpoint_id(host.cycle, "SSE"), fresh_reader=host.fresh,
        custody_authorized=True)
    result, _, _ = native_unit(host, model)
    assert result["status"] == "complete" and result["source_requests"] == 1, result
    assert result["page_count"] == result["cumulative_source_requests"] == 20
    assert int(host.opener.calls[-1][1]["offset"]) == 9500 and len(host.opener.calls) == 20
    host.complete(result)
    restored = refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh())
    assert len(restored["page_references"]) == 20


def test_native_daily_master_stage_preserves_budget_after_completed_generation(native, monkeypatch):
    from decision_kernel.infrastructure import market_snapshot
    from decision_kernel.interfaces import daily_market_cli as cli
    from test_daily_master_cycle import MasterHost
    from test_daily_market import native_context
    from test_ftshare_collection_runtime import NativeAPI
    from test_market_collection_inputs import CollectionOpener

    model, _ = native
    model.binding = replace(model.binding, repository=cli.REPO, repository_id=1420041744)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository, model.git.binding = model.binding.repository, model.binding
    local_store = VersionedStore(model.git)
    local_reader = lambda: VersionedStore(copy(model.git))
    host = MasterHost(storage=(model.git, local_store, local_reader))
    assert host.run()["receipt"]["status"] == "complete"
    host.now = "2026-10-20T12:10:00+00:00"
    host.master_opener = CollectionOpener(count=501)
    event, env = native_context()
    selected = cli.settings(event, env, host.approved)
    authority = NativeAPI()
    authority.run.update(path=cli.WORKFLOW, name=cli.TITLE, created_at=host.now)
    authority.ci.update(total_count=1)
    authority.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    model.meta = authority.meta
    original_request = model.request

    def request(method, path, payload=None):
        if path == cli.PREFIX + "/git/ref/heads/main" or "/actions/" in path:
            model.requests.append((method, path, deepcopy(payload)))
            return authority.request(method, path, payload)
        return original_request(method, path, payload)

    model.request = request
    wire, apis, fetches, units, source_admissions = QuotaWire(model), [], [], [], []

    class MeteredAPI(GitHubAPI):
        def __init__(self, token):
            super().__init__(token)
            self._opener = wire
            apis.append(self)

    monkeypatch.setattr(cli, "GitHubAPI", MeteredAPI)
    monkeypatch.setattr(cli, "_records_runtime_binding",
        lambda operation: replace(model.binding, write_enabled=operation == "sync"))
    monkeypatch.setattr(market_snapshot, "_fetch_market_branch", local_fetch(model, fetches))
    provider_open = host.master_opener.open

    def source(request, timeout):
        source_admissions.append(apis[-1]._requests)
        return provider_open(request, timeout)

    host.master_opener.open = source

    def execute(**options):
        api, checks = options["store"].backend.api, []
        assert api is apis[-1] and callable(options["preflight"])
        preflight = options["preflight"]

        def checked(reader):
            before, snapshots = api._requests, len(fetches)
            preflight(reader)
            checks.append(api._requests - before)
            assert len(fetches) == snapshots  # Same fresh scope owns all daily/FT proofs.

        result = refresh.run_master_unit(**{**options, "preflight": checked})
        units.append({"venue": options["venue"], "status": result["status"],
            "source_requests": result["source_requests"], "page_count": result["page_count"],
            "store_requests": api._requests, "preflight_requests": checks})
        return result

    price_boundaries, prior_price_clients = [], len(host.price_clients)

    def pause_prices(**options):
        # The changed owner ends at the original price entry. Complete native
        # prices were measured separately; this regression retains the real old
        # FT/v4 proof without repeating those unchanged price work units.
        price_boundaries.append(deepcopy(options["master_references"]))
        return {"receipt": {"status": "partial", "source_requests": 0, "reason_code": None},
            "reference": None}

    initial_calls = len(model.requests)
    result = host.run(open_unit=cli.remote_units("synthetic-whole-host-master", selected),
        guard=lambda api: cli.check_private(api, selected), capacity=market_capacity,
        execute_master_unit=execute, execute_prices=pause_prices)
    measured = {"unit_requests": [api._requests for api in apis], "master_units": units,
        "before_source": source_admissions, "snapshots": len(fetches)}
    print(json.dumps(measured, sort_keys=True))
    assert result["receipt"]["status"] == "partial", (result["receipt"], measured)
    assert result["receipt"]["master_source_requests"] == 3
    assert result["receipt"]["calendar_source_requests"] == 2
    assert result["receipt"]["price_source_requests"] == 0 and len(host.price_clients) == prior_price_clients
    assert price_boundaries == [result["control"]["master_references"]]
    item = host.cycle()
    assert item["cycle"]["status"] == "market-pending" and item["cycle"]["revision"] == 10
    assert all(row["status"] == "complete" for row in item["cycle"]["masters"].values())
    assert [row["status"] for row in units] == ["partial", "complete", "complete"]
    assert [row["source_requests"] for row in units] == [1, 1, 1]
    assert all(len(row["preflight_requests"]) >= 4 for row in units)
    assert source_admissions and max(source_admissions) < cli.ADMISSION_REQUESTS == 240
    assert all(0 < api._requests <= MAX_REQUESTS == 512 for api in apis)
    assert sum(api._requests for api in apis) == len(model.requests) - initial_calls
    assert all(call["api"] in apis and not call["destination"].exists() for call in fetches)
