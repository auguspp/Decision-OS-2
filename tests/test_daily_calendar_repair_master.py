"""Finite v4 recovery through the native host, original Git custody and FT owner.

All provider responses and native API facts are synthetic; this does not certify
the live SZSE endpoint or convert recovery into an on-time scheduled run.
"""
from copy import copy, deepcopy
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application import daily_calendar_repair as repair
from decision_kernel.application import daily_market as daily
from decision_kernel.application import daily_master_refresh as masters
from decision_kernel.application.ftshare_collection import restore_ftshare_collection
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces.daily_reading_cli import build_reading, summary_markdown
from test_daily_market import Host
from test_daily_market_schedule import WindowHost, native_runtime
from test_daily_master_cycle import MasterHost
from test_market_collection_inputs import CollectionOpener
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY

AUTHORITY = "https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6353400670"


class RepairMasterHost(MasterHost, WindowHost):
    def __init__(self, *, storage=None, count=3, master_rows=3):
        Host.__init__(self, storage=storage, count=count)
        self.master_clients, self.master_error = [], None
        self.master_opener = CollectionOpener(count=master_rows)
        self.failed_venue = "SZSE"
        self.now = "2026-10-22T10:35:00+00:00"

    def calendar(self):
        client = super().calendar()
        client._opener.fail = ("trade_cal", self.failed_venue)
        return client


def failed(host):
    receipt = host.run()["receipt"]
    assert receipt["status"] == "blocked" and receipt["reason_code"] == "RELAY_HTTP_UNAVAILABLE"
    assert (receipt["calendar_source_requests"], receipt["master_source_requests"],
        receipt["price_source_requests"]) == (2, 0, 0)
    item = host.cycle()
    assert (item["cycle"]["schema"], item["cycle"]["revision"]) == (daily.MASTER_SCHEMA, 4)
    grant = {"schema": repair.PROOF_SCHEMA, "original_reference": deepcopy(item["reference"]),
        "authorization_ref": AUTHORITY, "venue": "SZSE", "additional_requests": 1}
    host.failed_venue = None
    host.now = "2026-10-23T00:00:00+00:00"
    return item, grant


@pytest.fixture(scope="module")
def original_history():
    host = RepairMasterHost()
    item, grant = failed(host)
    return host, item, grant


@pytest.fixture
def original(original_history):
    old, item, grant = original_history
    host = copy(old)
    backend = MemoryBackend(deepcopy(old.fixture["backend"].shared))
    host.fixture = {**old.fixture, "backend": backend, "store": VersionedStore(backend),
        "fresh": lambda: VersionedStore(MemoryBackend(backend.shared))}
    host.approved = deepcopy(old.approved)
    host.calendar_clients, host.master_clients, host.price_clients, host.openings = [], [], [], []
    host.master_opener = CollectionOpener(count=3)
    return host, deepcopy(item), deepcopy(grant)


def test_native_repair_retains_real_git_failure_and_completes_full_masters_and_twelve_prices(tmp_path, monkeypatch):
    root = tmp_path / "records.git"
    backend = GitStore.init_synthetic(root)
    fresh = lambda: VersionedStore(GitStore(root))
    host = RepairMasterHost(storage=(backend, VersionedStore(backend), fresh), count=6, master_rows=501)
    host.approved.update(symbols=sorted(host.fixture["context"]["eligible_symbols"])[:6], max_price_requests=12)
    item, grant = failed(host)
    original_bytes = fresh().read_source(daily.SourceRef.from_dict(item["reference"]))
    sse = deepcopy(item["cycle"]["calendars"]["SSE"])
    grant_file = tmp_path / "repair.json"
    grant_file.write_bytes(encoded({"enabled": True, "repair": grant}))
    monkeypatch.setattr(repair, "POLICY_FILE", grant_file)
    event, env, api = native_runtime(tmp_path, monkeypatch, host.approved,
        invocation="repair-calendar", admitted_at=host.now, actual_at=host.now)
    options_seen = []

    def execute(**options):
        options_seen.append(options)
        # Native policy forwarding is material: omitting master_refresh used to
        # skip the new contract even though a direct test host supplied it.
        return host.run(calendar_repair=options["calendar_repair"],
            master_refresh=options.get("master_refresh"), progress=options["progress"])

    out = tmp_path / "daily"
    assert cli.run_runtime(event, env, out, api=api, execute=execute) == 0
    assert len(options_seen) == 1 and options_seen[0]["master_refresh"] == masters.POLICY
    receipt = json.loads((out / "result.json").read_text())
    control = json.loads((out / "control.json").read_text())
    assert receipt["invocation"] == "repair-calendar" and receipt["cycle_date"] == "20261022"
    assert (receipt["calendar_source_requests"], receipt["master_source_requests"],
        receipt["price_source_requests"]) == (1, 3, 12)
    assert receipt["calendar_reserved_requests"] == receipt["calendar_source_budget"] == 3
    assert receipt["calendar_additional_source_budget"] == 1
    assert receipt["master_reserved_queries"] == 2 and receipt["master_source_budget"] == 160
    final = daily.latest_cycle(fresh(), registry=REGISTRY)
    assert (final["cycle"]["schema"], final["cycle"]["revision"], final["cycle"]["status"]) == (
        daily.MASTER_REPAIR_SCHEMA, 13, "complete")
    assert final["cycle"]["calendars"]["SSE"] == sse
    assert final["cycle"]["calendar_repair"] == grant
    assert final["cycle"]["daily_policy"] == item["cycle"]["daily_policy"]
    assert final["cycle"]["master_refresh"] == item["cycle"]["master_refresh"]
    assert len(encoded(final["cycle"])) <= 12 * 1024
    assert fresh().read_source(daily.SourceRef.from_dict(item["reference"])) == original_bytes
    collection = restore_ftshare_collection(final["cycle"]["market_reference"], REGISTRY, fresh())["collection"]
    assert collection["plan"]["base_reference"] == item["cycle"]["base_market_reference"]
    assert collection["plan"]["generation"] == item["cycle"]["market_generation"]
    assert collection["plan"]["source_budget"] == collection["reserved_requests"] == 12
    assert collection["plan"]["master_references"] == control["master_references"]
    assert collection["plan"]["calendar_references"] == control["calendar_references"]
    assert all(collection["units"][slot]["request_count"] == 1 for slot in collection["plan"]["new_slots"])
    for venue in daily.VENUES:
        masters.verify_completed_master(final["cycle"], venue, REGISTRY, fresh())
    model = build_reading(fresh(), fresh(), REGISTRY, host.approved, receipt, as_of=host.now)
    summary = summary_markdown(model)
    assert "显式追加1次" in summary and "本轮两所完整主表已被行情版本采用" in summary
    before = backend.head()
    again = host.run(calendar_repair=grant)
    assert again["receipt"]["status"] == "already-complete" and again["receipt"]["source_requests"] == 0
    assert backend.head() == before


def test_repaired_cycle_resumes_only_original_stages_and_next_day_uses_plain_v4(original):
    host, item, grant = original
    paused = host.run(calendar_repair=grant, execute_prices=lambda **options: {
        "receipt": {"status": "partial", "source_requests": 0, "reason_code": None}, "reference": None})
    assert paused["receipt"]["status"] == "partial" and host.cycle()["cycle"]["revision"] == 12
    assert host.cycle()["cycle"]["status"] == "market-pending"
    master_refs = deepcopy(paused["control"]["master_references"])
    resumed = host.run(calendar_repair=grant)
    assert resumed["receipt"]["status"] == "complete"
    assert (resumed["receipt"]["calendar_source_requests"], resumed["receipt"]["master_source_requests"],
        resumed["receipt"]["price_source_requests"]) == (0, 0, 4)
    assert resumed["control"]["master_references"] == master_refs
    host.now = "2026-10-23T12:00:00+00:00"
    after = host.run()
    assert after["receipt"]["status"] == "complete", after
    assert host.cycle()["cycle"]["schema"] == daily.MASTER_SCHEMA
    assert host.cycle()["cycle"]["revision"] == 11
    assert after["receipt"]["calendar_source_requests"] == after["receipt"]["calendar_source_budget"] == 2
    assert "calendar_repair" not in host.cycle()["cycle"] and "calendar_repair" not in after["receipt"]


@pytest.mark.parametrize("phase", ["reservation", "calendar-source"])
def test_unknown_repair_write_keeps_reservation_and_cannot_repeat_http(original, monkeypatch, phase):
    host, item, grant = original
    owner = host.fixture["store"] if phase == "reservation" else cli
    attribute = "put_source" if phase == "reservation" else "retain_market_query"
    original_write = getattr(owner, attribute)

    def lost(*args, **kwargs):
        original_write(*args, **kwargs)
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")

    monkeypatch.setattr(owner, attribute, lost)
    first = host.run(calendar_repair=grant)["receipt"]
    assert first["status"] == "persistence-unconfirmed"
    assert first["calendar_source_requests"] == int(phase == "calendar-source")
    assert first["master_source_requests"] == first["price_source_requests"] == 0
    reserved = host.cycle()
    assert reserved["cycle"]["revision"] == 5
    assert reserved["cycle"]["calendars"]["SZSE"]["reserved_requests"] == 2
    assert reserved["cycle"]["calendars"]["SSE"] == item["cycle"]["calendars"]["SSE"]
    monkeypatch.setattr(owner, attribute, original_write)
    before = host.fixture["backend"].head()
    again = host.run(calendar_repair=grant)["receipt"]
    assert again["status"] == "persistence-unconfirmed" and again["source_requests"] == 0
    assert host.cycle() == reserved and host.fixture["backend"].head() == before
    assert not host.master_clients and not host.price_clients


@pytest.mark.parametrize("error", [TimeoutError("synthetic timeout"),
    HTTPError("https://synthetic.invalid/calendar", 403, "synthetic refusal", {}, None)])
def test_spent_v5_repair_failure_stays_terminal_across_calls_and_dates(original, error):
    host, item, grant = original
    host.calendar_error = error
    first = host.run(calendar_repair=grant)["receipt"]
    assert first["status"] == "blocked" and first["calendar_source_requests"] == 1
    assert first["calendar_reserved_requests"] == 3
    assert first["master_source_requests"] == first["price_source_requests"] == 0
    before = host.fixture["backend"].head()
    host.now = "2026-10-23T12:00:00+00:00"
    for options in ({"calendar_repair": grant}, {}):
        again = host.run(**options)["receipt"]
        assert again["status"] == "source-held" and again["source_requests"] == 0
        assert host.fixture["backend"].head() == before


@pytest.mark.parametrize("change", ["downgrade", "remove-masters", "master-budget", "policy", "identity",
    "proof", "other-calendar", "counts", "revision"])
def test_v5_repair_edge_cannot_discard_frozen_authority_or_expand_budget(original, change):
    host, item, grant = original
    state = repair.reserve(item, grant, at=host.now)
    if change == "downgrade":
        state["schema"] = repair.CYCLE_SCHEMA
        for key in ("daily_policy", "master_refresh", "masters"):
            state.pop(key)
    elif change == "remove-masters":
        state.pop("masters")
    elif change == "master-budget":
        state["master_refresh"]["max_attempts_per_page"] += 1
    elif change == "policy":
        state["daily_policy"].update(symbols=["000001.SZ"], max_price_requests=2)
        state["policy_sha256"] = digest(encoded(state["daily_policy"]))
    elif change == "identity":
        state.update(date="20261023", calendar_end="20261023", market_generation="daily-20261023")
    elif change == "proof":
        state["calendar_repair"]["additional_requests"] = 2
    elif change == "other-calendar":
        state["calendars"]["SSE"]["reference"] = deepcopy(item["cycle"]["base_market_reference"])
    elif change == "counts":
        state["calendars"]["SZSE"]["reserved_requests"] = 1
    else:
        state["revision"] = 14
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError):
        daily.retain_cycle(state, host.fixture["store"], registry=REGISTRY,
            fresh_reader=host.fixture["fresh"], previous=item, protected_symbols=host.approved["symbols"])
    assert host.fixture["backend"].head() == before and host.cycle() == item


def test_explicit_repair_cannot_remove_original_master_policy_before_sources(original):
    host, item, grant = original
    before = host.fixture["backend"].head()
    denied = host.run(calendar_repair=grant, master_refresh=None)["receipt"]
    assert denied["status"] == "blocked" and denied["reason_code"] == "DAILY_MASTER_POLICY_CHANGED"
    assert denied["source_requests"] == 0 and host.fixture["backend"].head() == before
    assert host.cycle() == item


def test_deployment_grant_is_the_exact_oct22_szse_failure():
    grant = repair.load_grant()
    assert grant["venue"] == "SZSE" and grant["additional_requests"] == 1
    assert grant["authorization_ref"] == AUTHORITY
    assert grant["original_reference"]["manifest"] == {
        "repository": "auguspp/Decision-OS-2", "commit": "3e81622c99986310e82d7b509bc3f80329002333",
        "path": "source-views/daily-market-cycle-20261022-r004/14fe199b38d45a917d58d901293be4d536f1af1bb6b41395cea628ad30e1b218/manifest.json",
        "sha256": "bd64256c02e275ba50e6d32561f64a95efedeb7316ac68a6a49f5f67327b40a0"}
