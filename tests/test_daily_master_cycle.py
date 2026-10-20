"""New daily master ownership through real custody and normal FT consumers.

Providers and clocks are synthetic. Existing cycles retain their old budgets.
"""
from copy import deepcopy
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application import daily_master_refresh as masters
from decision_kernel.application.ftshare_collection import restore_ftshare_collection
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces.daily_reading_cli import build_reading, daily_page, summary_markdown
from test_daily_market import Host
from test_market_collection_inputs import CollectionOpener
from test_market_relay_inputs import REGISTRY


class MasterHost(Host):
    def __init__(self, *, storage=None, master_rows=3):
        super().__init__(storage=storage)
        self.master_clients = []
        self.master_opener = CollectionOpener(count=master_rows)
        self.master_error = None

    def master(self):
        opener = self.master_opener
        if self.master_error is not None:
            error = self.master_error
            class Denied:
                def open(self, request, timeout):
                    raise error
            opener = Denied()
        client = RelayHTTP(opener=opener, credential="synthetic-master-only", request_timeout_seconds=90)
        self.master_clients.append(client)
        return client

    def run(self, **options):
        return super().run(**{"master_refresh": deepcopy(masters.POLICY),
            "master_client_factory": self.master, **options})

    def cycle(self):
        return daily.latest_cycle(self.fixture["fresh"](), registry=REGISTRY,
            protected_symbols=self.approved["symbols"])


def test_full_paged_daily_master_is_adopted_and_independently_read_from_git(tmp_path):
    root = tmp_path / "daily-master.git"
    backend = GitStore.init_synthetic(root)
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(root))
    host = MasterHost(storage=(backend, store, fresh), master_rows=501)
    old = restore_ftshare_collection(host.approved["seed_reference"], REGISTRY, fresh())
    original = encoded(old["collection"])
    result = host.run()
    receipt, control = result["receipt"], result["control"]
    assert receipt["status"] == "complete", receipt
    assert (receipt["calendar_source_requests"], receipt["master_source_requests"],
        receipt["price_source_requests"], receipt["source_requests"]) == (2, 3, 4, 9)
    assert (receipt["master_reserved_queries"], receipt["master_query_budget"],
        receipt["master_page_budget"], receipt["master_source_budget"]) == (2, 2, 40, 160)
    assert [client.requests for client in host.master_clients] == [1, 1, 1]
    assert [(params["exchange"], params.get("offset", "0"))
        for api, params in host.master_opener.calls] == [("SSE", "0"), ("SSE", "500"), ("SZSE", "0")]
    item = host.cycle()
    assert item["cycle"]["schema"] == daily.MASTER_SCHEMA and item["cycle"]["revision"] == 11
    assert len(encoded(item["cycle"])) <= 12 * 1024
    restored = restore_ftshare_collection(control["market_reference"], REGISTRY, fresh())
    assert restored["collection"]["plan"]["master_references"] == control["master_references"]
    assert restored["collection"]["plan"]["master_references"] != old["context"]["master_references"]
    assert restored["collection"]["plan"]["calendar_references"] == control["calendar_references"]
    assert restored["collection"]["plan"]["source_budget"] == 4
    assert encoded(restore_ftshare_collection(host.approved["seed_reference"], REGISTRY, fresh())["collection"]) == original
    for venue in daily.VENUES:
        checked = masters.verify_completed_master(item["cycle"], venue, REGISTRY, fresh())
        assert encoded(checked["reference"]) == encoded(control["master_references"][venue])
    before = backend.head()
    read = read_ftshare_collection_version(control["market_reference"], REGISTRY, fresh(), read_as_of=host.now)
    assert read["source_requests"] == 0 and read["view"]["scope_count"] == 503
    assert read["view"]["master"]["SSE"]["capture_times"] == {"L": [host.now, host.now]}
    assert read["view"]["freshness"]["status"] == "unknown"
    model = build_reading(fresh(), fresh(), REGISTRY, host.approved, receipt, as_of=host.now)
    assert model["market"]["status"] == "readable"
    assert model["daily_update"]["market_cycle_reference"] == control["cycle_reference"]
    assert model["daily_update"]["source_receipt"]["master_source_requests"] == 3
    assert "本轮两所完整主表已被行情版本采用" in summary_markdown(model)
    assert "本轮两所完整主表已被行情版本采用" in daily_page(model)
    again = host.run()
    assert again["receipt"]["status"] == "already-complete"
    assert again["receipt"]["source_requests"] == 0 and again["control"] == control
    assert backend.head() == before


def test_each_new_completed_session_refreshes_with_new_refs_and_original_price_budget():
    host = MasterHost()
    first = host.run()
    assert first["receipt"]["status"] == "complete", first
    old = host.cycle()
    host.now = "2026-10-20T12:10:00+00:00"
    second = host.run()
    assert second["receipt"]["status"] == "complete", second
    assert (second["receipt"]["calendar_source_requests"], second["receipt"]["master_source_requests"],
        second["receipt"]["price_source_requests"]) == (2, 2, 4)
    new = host.cycle()
    assert (new["cycle"]["date"], new["cycle"]["revision"]) == ("20261020", 11)
    assert new["cycle"]["base_market_reference"] == old["cycle"]["market_reference"]
    assert second["control"]["master_references"] != first["control"]["master_references"]
    assert restore_ftshare_collection(old["cycle"]["market_reference"], REGISTRY,
        host.fixture["fresh"]())["collection"]["plan"]["master_references"] == first["control"]["master_references"]
    assert len(host.master_clients) == 4


def test_no_new_session_leaves_master_queries_and_prices_unreserved():
    host = MasterHost()
    host.now = "2026-10-17T12:10:00+00:00"
    result = host.run()
    assert result["receipt"]["status"] == "no-new-session", result
    assert result["receipt"]["source_requests"] == result["receipt"]["calendar_source_requests"] == 2
    assert result["receipt"]["master_source_requests"] == result["receipt"]["price_source_requests"] == 0
    assert host.master_clients == host.price_clients == []
    assert all(row["status"] == "pending" and row["reserved_queries"] == 0
        for row in host.cycle()["cycle"]["masters"].values())


def test_enabling_master_refresh_does_not_reopen_a_completed_legacy_cycle():
    host = MasterHost()
    original = host.run(master_refresh=None)
    assert original["receipt"]["status"] == "complete"
    assert host.cycle()["cycle"]["schema"] == daily.SCHEMA
    before = host.fixture["backend"].head()
    enabled = host.run()
    assert enabled["receipt"]["status"] == "already-complete" and enabled["receipt"]["source_requests"] == 0
    assert host.fixture["backend"].head() == before and host.master_clients == []
    host.now = "2026-10-20T12:10:00+00:00"
    updated = host.run()
    assert updated["receipt"]["status"] == "complete", updated
    assert host.cycle()["cycle"]["schema"] == daily.MASTER_SCHEMA
    assert updated["receipt"]["master_source_requests"] == 2


@pytest.mark.parametrize("status", [403, 429])
def test_source_hold_preserves_original_master_and_blocks_price_and_new_date(status):
    host = MasterHost()
    host.master_error = HTTPError("https://synthetic.invalid/master", status, "synthetic", {}, None)
    first = host.run()
    assert first["receipt"]["status"] == "source-held", first
    assert (first["receipt"]["calendar_source_requests"], first["receipt"]["master_source_requests"],
        first["receipt"]["price_source_requests"]) == (2, 1, 0)
    item = host.cycle()
    assert item["cycle"]["status"] == "blocked" and item["cycle"]["masters"]["SSE"]["status"] == "failed"
    assert item["cycle"]["masters"]["SZSE"]["reserved_queries"] == 0
    assert host.price_clients == []
    before = host.fixture["backend"].head()
    host.now = "2026-10-20T12:10:00+00:00"
    second = host.run()
    assert second["receipt"]["status"] == "source-held" and second["receipt"]["source_requests"] == 0
    assert host.fixture["backend"].head() == before and host.cycle() == item


def test_unknown_inflight_checkpoint_never_creates_a_second_master_source_attempt(monkeypatch):
    host = MasterHost()
    retain = masters.retain_market_checkpoint
    def unknown(*args, **kwargs):
        retain(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    monkeypatch.setattr(masters, "retain_market_checkpoint", unknown)
    first = host.run()
    assert first["receipt"]["status"] == "persistence-unconfirmed", first
    assert first["receipt"]["master_source_requests"] == 0 and host.master_clients[0].requests == 0
    assert host.cycle()["cycle"]["status"] == "master-pending"
    monkeypatch.setattr(masters, "retain_market_checkpoint", retain)
    second = host.run()
    assert second["receipt"]["status"] == "persistence-unconfirmed", second
    assert second["receipt"]["source_requests"] == 0 and len(host.master_clients) == 1
    assert second["receipt"]["master_execution"]["cumulative_source_requests"] is None
    assert host.price_clients == []


def test_complete_checkpoint_promotes_same_unknown_query_without_repeating_its_source(monkeypatch):
    host = MasterHost()
    retain = masters.retain_market_query
    saved = []
    def unknown(*args, **kwargs):
        result = retain(*args, **kwargs)
        saved.append(result["reference"])
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    monkeypatch.setattr(masters, "retain_market_query", unknown)
    first = host.run()
    assert first["receipt"]["status"] == "persistence-unconfirmed", first
    assert first["receipt"]["master_source_requests"] == 1 and first["receipt"]["price_source_requests"] == 0
    monkeypatch.setattr(masters, "retain_market_query", retain)
    second = host.run()
    assert second["receipt"]["status"] == "complete", second
    assert (second["receipt"]["calendar_source_requests"], second["receipt"]["master_source_requests"],
        second["receipt"]["price_source_requests"]) == (0, 1, 4)
    assert encoded(second["control"]["master_references"]["SSE"]) == encoded(saved[0])
    assert [client.requests for client in host.master_clients] == [1, 1]


def test_missing_existing_owner_in_fresh_master_stops_before_price_credentials():
    host = MasterHost()
    host.master_opener.rows["stock_basic", "SSE"] = [row
        for row in host.master_opener.rows["stock_basic", "SSE"] if row["ts_code"] != "600000.SH"]
    result = host.run()
    assert result["receipt"]["status"] == "blocked", result
    assert result["receipt"]["reason_code"] == "FTSHARE_SINGLE_MASTER_OWNER_UNQUALIFIED"
    assert result["receipt"]["master_source_requests"] == 2 and result["receipt"]["price_source_requests"] == 0
    # The original planner rejects before creating any new FT work. Retain the
    # exact pending intent; it cannot substitute later master pages or a date.
    pending = host.cycle()
    assert host.price_clients == [] and pending["cycle"]["status"] == "market-pending"
    before = host.fixture["backend"].head()
    host.now = "2026-10-20T12:10:00+00:00"
    again = host.run()
    assert again["receipt"]["status"] == "blocked"
    assert again["receipt"]["reason_code"] == "FTSHARE_SINGLE_MASTER_OWNER_UNQUALIFIED"
    assert again["receipt"]["source_requests"] == 0 and host.cycle() == pending
    assert host.fixture["backend"].head() == before and host.price_clients == []


def test_master_policy_cannot_be_removed_after_a_v4_cycle():
    host = MasterHost()
    assert host.run()["receipt"]["status"] == "complete"
    before = host.fixture["backend"].head()
    host.now = "2026-10-20T12:10:00+00:00"
    denied = host.run(master_refresh=None)
    assert denied["receipt"]["status"] == "blocked"
    assert denied["receipt"]["reason_code"] == "DAILY_MASTER_POLICY_CHANGED"
    assert denied["receipt"]["source_requests"] == 0 and host.fixture["backend"].head() == before


@pytest.mark.parametrize("field,value", [("max_pages_per_venue", 21), ("max_attempts_per_page", 5),
    ("max_wait_seconds_per_page", 181), ("page_size", True), ("list_status", "D"), ("venues", ["SSE"])])
def test_config_is_exact_and_cannot_enlarge_scope_or_budgets(field, value):
    configured = json.loads(cli.MASTER_POLICY_FILE.read_text())
    assert masters.policy(configured) == masters.POLICY
    configured[field] = value
    with pytest.raises(RecordError, match="DAILY_MASTER_POLICY"):
        masters.policy(configured)
