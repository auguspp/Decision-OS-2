"""Daily business controls over real source custody; all providers are synthetic."""
from copy import copy, deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.ftshare_collection import (
    begin_ftshare_slot, latest_ftshare_reference, restore_ftshare_collection,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import daily_market_cli as cli
from test_ftshare_collection import (
    _retain, plan_single, settle_single, single_capture, single_collection_fixture,
)
from test_ftshare_collection_runtime import NativeAPI
from test_market_collection_inputs import CollectionOpener
from test_market_relay_inputs import REGISTRY
from test_market_snapshot import local_fetch, native

NOW = "2026-10-19T12:10:00+00:00"


class Host:
    def __init__(self, storage=None):
        self.fixture = single_collection_fixture(storage=storage)
        f = self.fixture
        initial = _retain(f["single"], f["store"], f["fresh"])
        _, final = settle_single(f, f["single"], initial)
        self.approved = {"schema": daily.POLICY_SCHEMA, "enabled": True,
            "schedule_utc": "35 10 * * 1-5", "not_before_local": "18:35", "lookback_days": 119,
            "max_calendar_requests": 2, "max_price_requests": 4,
            "symbols": ["000001.SZ", "600000.SH"], "seed_reference": final["reference"]}
        self.now, self.calendar_clients, self.price_clients, self.openings = NOW, [], [], []
        self.calendar_error = self.price_error = self.calendar_edit = None
        self.capacity = lambda reader: {"admit_work_unit": True}

    def open(self, write):
        self.openings.append(write)
        return self.fixture["store"], self.fixture["fresh"], None

    def calendar(self):
        opener = CollectionOpener(count=3, through=self.now[:10].replace("-", ""))
        for venue in daily.VENUES:
            for row in opener.rows["trade_cal", venue]:
                if row["cal_date"] in {"20261001", "20261002", "20261005", "20261006"}:
                    row["is_open"] = 0
                if self.calendar_edit:
                    self.calendar_edit(venue, row)
        if self.calendar_error:
            def failed(request, timeout):
                raise self.calendar_error
            opener.open = failed
        client = RelayHTTP(opener=opener, credential="synthetic-calendar-only", request_timeout_seconds=90)
        self.calendar_clients.append(client)
        return client

    def price(self):
        host = self
        class Client:
            requests = 0
            last_response_meta = last_error_meta = last_response_bytes = None
            def validate_credential(self):
                return "synthetic-price-only"
            def candlesticks(self, symbol, *, since, until, adjust_kind):
                item = daily.latest_cycle(host.fixture["fresh"]())
                ref = latest_ftshare_reference(host.fixture["fresh"](), REGISTRY,
                    collection_id="ftshare-cn-eod", generation=item["cycle"]["market_generation"])
                state = restore_ftshare_collection(ref, REGISTRY, host.fixture["fresh"]())["collection"]
                slot = adjust_kind + ":" + symbol
                assert state["units"][slot]["status"] == "in-flight"
                assert state["plan"]["source_budget"] == host.approved["max_price_requests"]
                self.requests += 1
                if host.price_error:
                    self.last_error_meta = {"http_status": 403, "classification": "auth-or-entitlement"}
                    raise host.price_error
                capture = single_capture(state, slot, acquired_at=host.now)
                self.last_response_bytes = capture["body_utf8"].encode()
                self.last_response_meta = {"http_status": 200, "rate_limits": {}}
                return json.loads(self.last_response_bytes)
        client = Client()
        self.price_clients.append(client)
        return client

    def run(self, **kwargs):
        options = dict(approved=self.approved, registry=REGISTRY, open_unit=self.open,
            clock=lambda: self.now, calendar_client_factory=self.calendar, price_client_factory=self.price,
            capacity=self.capacity, monotonic=lambda: 10, sleep=lambda seconds: None)
        return cli.run_daily(**{**options, **kwargs})


def test_daily_updates_new_session_and_same_day_does_not_replay_any_source():
    host = Host()
    first = host.run()
    receipt = first["receipt"]
    assert receipt["status"] == "complete", receipt
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"], receipt["source_requests"]) == (2, 4, 6)
    assert receipt["health"]["as_of"] == "2026-10-19"
    assert receipt["health"]["scope_count"] == 5
    assert receipt["health"]["raw"]["missing_count"] == 3
    assert [row["observed_count"] for row in receipt["health"]["raw"]["horizon_counts"]] == [2, 2, 2]
    assert receipt["health"]["freshness"]["completed_day_through"] == "2026-10-19"
    assert receipt["health"]["freshness"]["status"] == "unknown"  # Full declared coverage remains partial.
    assert first["control"]["market_reference"] != host.approved["seed_reference"]
    head = host.fixture["backend"].head()
    second = host.run()
    assert second["receipt"]["status"] == "already-complete"
    assert second["receipt"]["source_requests"] == 0 and second["control"] == first["control"]
    assert host.fixture["backend"].head() == head
    assert len(host.calendar_clients) == len(host.price_clients) == 1


def test_next_day_uses_new_qualified_session_and_keeps_prior_allowance():
    host = Host()
    first = host.run()
    assert first["receipt"]["status"] == "complete", first
    host.now = "2026-10-20T12:10:00+00:00"
    second = host.run()
    assert second["receipt"]["status"] == "complete", second
    restored = restore_ftshare_collection(second["control"]["market_reference"], REGISTRY, host.fixture["fresh"]())
    plan = restored["collection"]["plan"]
    assert plan["base_reference"] == first["control"]["market_reference"]
    assert plan["prior_generation_reserved_requests"] == 5  # One previous finite call plus yesterday's four.
    assert plan["source_budget"] == 4 and restored["collection"]["reserved_requests"] == 4
    assert restored["daily_generation_advance"] is True


def test_real_calendar_closed_day_consumes_no_price_requests():
    host = Host()
    host.now = "2026-10-19T12:10:00+00:00"
    host.calendar_edit = lambda venue, row: row.update(is_open=0) if row["cal_date"] == "20261019" else None
    result = host.run()
    assert result["receipt"]["status"] == "no-new-session", result
    assert result["receipt"]["expected_completed_session"] == "20261016"
    assert result["receipt"]["calendar_source_requests"] == 2
    assert result["receipt"]["price_source_requests"] == 0 and host.price_clients == []
    assert result["control"]["market_reference"] == host.approved["seed_reference"]


@pytest.mark.parametrize("kind", ["calendar-failure", "calendar-history-change", "price-refusal"])
def test_source_failure_is_retained_across_new_runs_and_new_dates(kind):
    host = Host()
    if kind == "calendar-failure":
        host.calendar_error = RecordError("RELAY_AUTH_OR_ENTITLEMENT")
    elif kind == "calendar-history-change":
        host.calendar_edit = lambda venue, row: row.update(is_open=1) if row["cal_date"] == "20261001" else None
    else:
        host.price_error = RecordError("FTSHARE_HTTP_403")
    first = host.run()
    assert first["receipt"]["status"] not in {"complete", "read", "already-complete"}, first
    cycle = daily.latest_cycle(host.fixture["fresh"]())["cycle"]
    assert cycle["status"] == "blocked"
    counts = len(host.calendar_clients), len(host.price_clients)
    head = host.fixture["backend"].head()
    host.now = "2026-10-20T12:10:00+00:00"
    host.calendar_error = host.calendar_edit = host.price_error = None
    second = host.run()
    assert second["receipt"]["status"] == "source-held", second
    assert second["receipt"]["source_requests"] == 0
    assert (len(host.calendar_clients), len(host.price_clients)) == counts
    assert host.fixture["backend"].head() == head


def test_unknown_calendar_reservation_does_not_make_another_request(monkeypatch):
    host = Host()
    original = host.fixture["store"].put_source
    def lost(submission, source):
        value = original(submission, source)
        if source.source_id.endswith("-r001"):
            raise UnknownWrite("SYNTHETIC_RESERVATION_RESPONSE_LOST")
        return value
    monkeypatch.setattr(host.fixture["store"], "put_source", lost)
    first = host.run()
    assert first["receipt"]["status"] == "persistence-unconfirmed"
    assert host.calendar_clients == host.price_clients == []
    monkeypatch.setattr(host.fixture["store"], "put_source", original)
    host.now = "2026-10-20T12:10:00+00:00"
    second = host.run()
    assert second["receipt"]["status"] == "persistence-unconfirmed"
    assert second["receipt"]["reason_code"] == "DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED"
    assert second["receipt"]["source_requests"] == 0
    assert host.calendar_clients == host.price_clients == []


@pytest.mark.parametrize("mode", ["held", "failed", "in-flight"])
def test_original_ftshare_failure_or_unfinished_slot_blocks_before_calendars(mode):
    host = Host()
    f = host.fixture
    successor = plan_single(f, base=host.approved["seed_reference"], generation="review-required",
        targets=[{"symbol": "600001.SH", "adjust_kind": "none"}], source_budget=1)
    initial = _retain(successor, f["store"], f["fresh"])
    if mode == "in-flight":
        started = begin_ftshare_slot(successor, "none:600001.SH", at=NOW)
        root = _retain(started, f["store"], f["fresh"], initial["reference"])
    else:
        _, root = settle_single(f, successor, initial, reason_code="FTSHARE_HTTP_403" if mode == "held" else "FTSHARE_HTTP_500",
            source_hold="FTSHARE_HTTP_403" if mode == "held" else None)
    host.approved["seed_reference"] = root["reference"]
    result = host.run()
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_BASE_REQUIRES_REVIEW"
    assert result["receipt"]["source_requests"] == 0
    assert host.calendar_clients == host.price_clients == []


def test_before_close_and_paused_policy_have_no_source_calls_or_business_writes():
    host = Host()
    head = host.fixture["backend"].head()
    host.now = "2026-10-19T06:00:00+00:00"
    result = host.run()
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_BEFORE_CLOSE"
    assert result["receipt"]["status"] == "not-due"
    host.approved["enabled"] = False
    assert host.run()["receipt"]["status"] == "paused"
    assert host.calendar_clients == host.price_clients == []
    assert host.fixture["backend"].head() == head


def test_status_never_requests_writing_or_source_credentials():
    host = Host()
    result = host.run(operation="status")
    assert result["receipt"]["status"] == "read", result
    assert not any(host.openings)
    assert host.calendar_clients == host.price_clients == []
    assert result["control"]["market_reference"] == host.approved["seed_reference"]


def test_stored_daily_records_and_price_reader_restore_in_independent_git(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "records.git")
    host = Host(storage=(backend, VersionedStore(backend), lambda: VersionedStore(GitStore(backend.root))))
    result = host.run()
    assert result["receipt"]["status"] == "complete", result
    reader = VersionedStore(GitStore(backend.root))
    resolved = daily.daily_market_reference(reader, REGISTRY, host.approved)
    assert resolved["reference"] == result["control"]["market_reference"]
    assert resolved["cycle"]["status"] == "complete"
    assert host.run(operation="status")["receipt"]["source_requests"] == 0


def test_daily_native_reads_pin_once_per_contract_and_keep_final_live_check(native, monkeypatch):
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader
    from test_ftshare_collection import _new_calendars
    from test_news_read_budget import transport

    model, store = native
    readonly = replace(model.binding, write_enabled=False)
    # Seed only synthetic original records through the existing local backend.
    # The counted consumers below independently validate the native Git mirror.
    fresh = lambda: VersionedStore(copy(model.git))
    host = Host(storage=(model.git, store, fresh))
    api, fetches = transport(model), []
    backend = GitMarketReader(readonly, api, fetch=local_fetch(model, fetches))
    reader = VersionedStore(backend)
    before = model.git.head()
    prepared = daily.initial_cycle(host.approved, reader, REGISTRY, at=NOW)
    assert prepared["existing"] is None and len(fetches) == 1
    assert fetches[0]["head"] == before and model.git.head() == before
    assert backend._depth == 0 and backend._reader is None
    assert api._requests == len(model.requests) < 32

    # Source and control fixtures still use the original custody methods. Each
    # write's before/after reader stays independently instantiated.
    item = daily.retain_cycle(prepared["candidate"], store, fresh_reader=fresh, previous=None)
    calendars = _new_calendars(store, fresh, through="20261019")
    for venue in daily.VENUES:
        rows = deepcopy(item["cycle"]["calendars"])
        rows[venue].update(status="in-flight", reserved_requests=1)
        item = daily.retain_cycle(daily.next_state(item, at=NOW, calendars=rows),
            store, fresh_reader=fresh, previous=item)
        rows[venue].update(status="complete", reference=calendars[venue])
        item = daily.retain_cycle(daily.next_state(item, at=NOW, calendars=rows),
            store, fresh_reader=fresh, previous=item)
    prior_calls, prior_fetches = api._requests, len(fetches)
    session = daily.completed_session(item["cycle"], REGISTRY, reader)
    assert session["new_session"] and session["price_end"] == "20261019"
    assert len(fetches) == prior_fetches + 1 and api._requests - prior_calls < 32

    prior_calls, prior_fetches = api._requests, len(fetches)
    resolved = daily.daily_market_reference(reader, REGISTRY, host.approved)
    assert resolved["reference"] == host.approved["seed_reference"]
    assert resolved["cycle_reference"] == item["reference"]
    assert len(fetches) == prior_fetches + 1 and api._requests - prior_calls < 32
    assert all(call["api"] is api and not call["destination"].exists() for call in fetches)
    assert model.writes == 0 and backend._depth == 0 and backend._reader is None

    original = daily.latest_ftshare_reference
    def revoked(*args, **kwargs):
        value = original(*args, **kwargs)
        model.meta["private"] = False
        return value
    monkeypatch.setattr(daily, "latest_ftshare_reference", revoked)
    released = []
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        released.append(daily.daily_market_reference(reader, REGISTRY, host.approved))
    assert not released and backend._depth == 0 and backend._reader is None


def test_policy_is_bounded_to_declared_symbols_and_no_hidden_budget():
    host = Host()
    for change in ({"max_price_requests": 10448}, {"max_calendar_requests": 3},
                   {"lookback_days": 120}, {"symbols": host.approved["symbols"] * 2}):
        with pytest.raises(RecordError):
            daily.policy({**host.approved, **change})


def native_context():
    event = {"inputs": {"operation": "update", "code_sha": "a" * 40}}
    env = {"GITHUB_REPOSITORY": cli.REPO, "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": cli.REPO + "/" + cli.WORKFLOW + "@refs/heads/main",
        "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "8001", "GITHUB_RUN_ATTEMPT": "1"}
    return event, env


def test_schedule_uses_its_own_native_identity_not_the_old_manual_one_shot():
    host = Host()
    event, env = native_context()
    selected = cli.settings(event, env, host.approved)
    api = NativeAPI()
    api.run.update(path=cli.WORKFLOW, name=cli.TITLE, created_at=NOW)
    api.ci.update(total_count=1)
    api.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    assert cli.native_ready(api, selected)["main_ci_run"] == 6001
    env["GITHUB_EVENT_NAME"] = "schedule"
    selected = cli.settings({"schedule": "35 10 * * 1-5"}, env, host.approved)
    with pytest.raises(RecordError, match="NATIVE_RUN"):
        cli.native_ready(api, selected)
    api.run["event"] = "schedule"
    assert cli.native_ready(api, selected)["native_run_created_at"] == NOW
    api.ci["workflow_runs"].append({**api.ci["workflow_runs"][0], "id": 6002, "conclusion": "failure"})
    api.ci["total_count"] = 2
    with pytest.raises(RecordError, match="MAIN_CI"):
        cli.native_ready(api, selected)


@pytest.mark.parametrize("change", [{"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_REF": "refs/heads/other"},
    {"GITHUB_REPOSITORY": "other/repo"}, {"GITHUB_EVENT_NAME": "repository_dispatch"},
    {"GITHUB_WORKFLOW_REF": cli.REPO + "/.github/workflows/ashare-data.yml@refs/heads/main"}])
def test_wrong_runtime_never_reuses_the_daily_grant(change):
    host = Host()
    event, env = native_context()
    with pytest.raises(RecordError):
        cli.settings(event, {**env, **change}, host.approved)
