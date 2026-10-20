"""Bind v4 completion to its original policy and real FTShare source owners."""
from copy import copy, deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application.ftshare_single import FTShareSingleError
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.interfaces.ftshare_collection_cli import run_collection
from test_daily_master_cycle import MasterHost
from test_ftshare_collection import _retain, settle_single, single_capture
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY


@pytest.fixture(scope="module")
def pending_history():
    host = MasterHost()
    paused = host.run(execute_prices=lambda **options: {
        "receipt": {"status": "partial", "source_requests": 0, "reason_code": None}, "reference": None})
    assert paused["receipt"]["status"] == "partial", paused
    assert host.cycle()["cycle"]["revision"] == 10
    assert host.cycle()["cycle"]["status"] == "market-pending"
    assert not host.price_clients
    return host


@pytest.fixture
def pending(pending_history):
    host = copy(pending_history)
    backend = MemoryBackend(deepcopy(pending_history.fixture["backend"].shared))
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    host.fixture = {**pending_history.fixture, "backend": backend,
        "store": VersionedStore(backend), "fresh": fresh}
    host.approved = deepcopy(pending_history.approved)
    host.calendar_clients, host.master_clients, host.price_clients, host.openings = [], [], [], []
    return host, host.cycle()


def _plan(host, item, **changes):
    cycle = item["cycle"]
    config = daily.price_configuration(cycle, host.approved, REGISTRY, host.fixture["fresh"]())
    arguments = {**config["kernel_sync"], "collection_id": config["collection_id"],
        "generation": config["generation"], "created_at": host.now,
        "calendar_references": {v: cycle["calendars"][v]["reference"] for v in daily.VENUES},
        "master_references": {v: cycle["masters"][v]["reference"] for v in daily.VENUES}, **changes}
    reference = arguments.pop("base_reference")
    return ft.plan_ftshare_single_collection(reference, REGISTRY, host.fixture["fresh"](), **arguments)


def _complete(host, collection):
    saved = _retain(collection, host.fixture["store"], host.fixture["fresh"])
    for slot in collection["plan"]["new_slots"]:
        collection, saved = settle_single(host.fixture, collection, saved, slot)
    return collection, saved


def _claim(host, item, reference):
    return daily.next_state(item, at=host.now, status="complete", market_reference=reference)


def _retain_daily(host, item, state):
    return daily.retain_cycle(state, host.fixture["store"], registry=REGISTRY,
        fresh_reader=host.fixture["fresh"], previous=item, protected_symbols=host.approved["symbols"])


def _inject_daily(host, state):
    """An adversarial writer bypasses application validation, not the Store."""
    return host.fixture["store"].put_source("synthetic-daily-proof-" + digest(encoded(state))[:32],
        daily._source(state))


def test_complete_policy_authority_replays_without_the_current_config(pending):
    host, item = pending
    frozen = deepcopy(host.approved)
    frozen.pop("enabled")
    assert item["cycle"]["daily_policy"] == frozen
    assert digest(encoded(frozen)) == item["cycle"]["policy_sha256"]
    assert daily.policy_digest({**host.approved, "enabled": False}) == item["cycle"]["policy_sha256"]
    collection, saved = _complete(host, _plan(host, item))
    final = _retain_daily(host, item, _claim(host, item, saved["reference"]))
    assert final["cycle"]["revision"] == 11 and len(encoded(final["cycle"])) <= 12 * 1024
    assert collection["reserved_requests"] == collection["plan"]["source_budget"] == 4
    assert all(collection["units"][slot]["request_count"] == 1 for slot in collection["plan"]["new_slots"])
    before = host.fixture["backend"].head()
    host.approved.update(symbols=["600001.SH"], max_price_requests=2)
    # Historical completion comes from the original immutable policy, even if
    # a caller now protects a different valid symbol or supplies no live policy.
    assert daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"]) == final
    assert daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY) == final
    refused = host.run()
    assert refused["receipt"]["status"] == "blocked"
    assert refused["receipt"]["reason_code"] == "DAILY_MARKET_POLICY_CHANGED"
    assert refused["receipt"]["source_requests"] == 0 and host.fixture["backend"].head() == before


@pytest.mark.parametrize("change", ["missing", "enabled", "symbols", "budget", "seed"])
def test_v4_policy_snapshot_must_be_the_exact_original_digest_preimage(pending, change):
    _, item = pending
    cycle = deepcopy(item["cycle"])
    if change == "missing":
        cycle.pop("daily_policy")
    elif change == "enabled":
        cycle["daily_policy"]["enabled"] = False
    elif change == "symbols":
        cycle["daily_policy"].update(symbols=["000001.SZ"], max_price_requests=2)
    elif change == "budget":
        cycle["daily_policy"]["max_price_requests"] = 2
    else:
        cycle["daily_policy"]["seed_reference"] = cycle["masters"]["SSE"]["reference"]
    with pytest.raises(RecordError):
        daily.validate_cycle(cycle)


def test_recomputed_policy_digest_cannot_rewrite_authority_at_completion(pending):
    host, item = pending
    _, saved = _complete(host, _plan(host, item))
    state = _claim(host, item, saved["reference"])
    state["daily_policy"].update(symbols=["000001.SZ"], max_price_requests=2)
    state["policy_sha256"] = digest(encoded(state["daily_policy"]))
    daily.validate_cycle(state)  # A valid new policy is not the original intent.
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="DAILY_MARKET_INTENT_CHANGED"):
        _retain_daily(host, item, state)
    assert host.fixture["backend"].head() == before
    _inject_daily(host, state)
    with pytest.raises(RecordError, match="DAILY_MARKET_INTENT_CHANGED"):
        daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY)


@pytest.mark.parametrize("change", ["one-slot", "one-mode", "different-symbol", "short-start",
    "old-end", "no-eod-policy", "different-generation"])
def test_valid_but_unapproved_ft_plans_cannot_complete_or_replay_daily(pending, change):
    host, item = pending
    targets = [{"symbol": symbol, "adjust_kind": kind} for symbol in host.approved["symbols"]
        for kind in ("none", "forward")]
    options = {}
    if change == "one-slot":
        options.update(targets=targets[:1], source_budget=1)
    elif change == "one-mode":
        targets = [target for target in targets if target["adjust_kind"] == "none"]
        options.update(targets=targets, source_budget=2)
    elif change == "different-symbol":
        targets[-1] = {"symbol": "600001.SH", "adjust_kind": "forward"}
        options["targets"] = targets
    elif change == "short-start":
        options["start"] = "20260702"
    elif change == "old-end":
        options["end"] = "20261018"
    elif change == "no-eod-policy":
        options["completion_policy"] = None
        host.now = "2026-10-20T12:10:00+00:00"
    else:
        options["generation"] = "daily-20261019-other"
    collection, saved = _complete(host, _plan(host, item, **options))
    restored = ft.restore_ftshare_collection(saved["reference"], REGISTRY, host.fixture["fresh"]())
    assert restored["collection"] == collection
    assert all(row["status"] == "complete" for row in collection["units"].values())
    state = _claim(host, item, saved["reference"])
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="DAILY_MASTER_NOT_ADOPTED"):
        _retain_daily(host, item, state)
    assert host.fixture["backend"].head() == before and host.cycle() == item
    _inject_daily(host, state)
    with pytest.raises(RecordError, match="DAILY_MASTER_NOT_ADOPTED"):
        daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
            protected_symbols=collection["plan"]["eligible_symbols"])


def test_real_host_rejects_a_narrowed_success_receipt_without_claiming_missing_slots(pending):
    host, item = pending
    class Price:
        requests = 0
        last_response_meta = last_error_meta = last_response_bytes = None

        def validate_credential(self):
            return "synthetic-price-only"

        def candlesticks(self, symbol, *, since, until, adjust_kind):
            reference = ft.latest_ftshare_reference(host.fixture["fresh"](), REGISTRY,
                collection_id="ftshare-cn-eod", generation=item["cycle"]["market_generation"])
            state = ft.restore_ftshare_collection(reference, REGISTRY, host.fixture["fresh"]())["collection"]
            slot = adjust_kind + ":" + symbol
            assert state["units"][slot]["status"] == "in-flight"
            self.requests += 1
            capture = single_capture(state, slot, acquired_at=host.now)
            self.last_response_bytes = capture["body_utf8"].encode()
            self.last_response_meta = {"http_status": 200, "rate_limits": {}}
            return json.loads(self.last_response_bytes)

    client = Price()
    def narrowed(**options):
        config = deepcopy(options["config"])
        config["kernel_sync"]["targets"] = config["kernel_sync"]["targets"][:1]
        config["kernel_sync"]["source_budget"] = 1
        return run_collection(**{**options, "config": config})

    result = host.run(execute_prices=narrowed, price_client_factory=lambda: client)
    assert result["receipt"]["status"] == "blocked", result
    assert result["receipt"]["reason_code"] == "DAILY_MASTER_NOT_ADOPTED"
    assert result["receipt"]["price_execution"]["status"] == "complete"
    assert result["receipt"]["price_source_requests"] == result["receipt"]["source_requests"] == client.requests == 1
    assert host.cycle() == item
    assert host.cycle()["cycle"]["daily_policy"]["max_price_requests"] == 4
    before = host.fixture["backend"].head()
    repeated = host.run(price_client_factory=lambda: pytest.fail("altered plan attempted another source"))
    assert repeated["receipt"]["source_requests"] == 0
    assert repeated["receipt"]["reason_code"] == "FTSHARE_RUNTIME_RESUME_CONFIG_CHANGED"
    assert host.fixture["backend"].head() == before


@pytest.mark.parametrize("owner", ["old-period", "other-symbol", "other-mode"])
def test_new_slot_source_ownership_is_verified_beyond_the_ft_control_manifest(pending, owner):
    host, item = pending
    collection = _plan(host, item)
    saved = _retain(collection, host.fixture["store"], host.fixture["fresh"])
    last = collection["plan"]["new_slots"][-1]
    for slot in collection["plan"]["new_slots"][:-1]:
        collection, saved = settle_single(host.fixture, collection, saved, slot)
    if owner == "old-period":
        base = ft.restore_ftshare_collection(item["cycle"]["base_market_reference"],
            REGISTRY, host.fixture["fresh"]())["collection"]
        reference = base["units"][last]["reference"]
    elif owner == "other-symbol":
        reference = collection["units"]["none:000001.SZ"]["reference"]
    else:
        reference = collection["units"]["forward:600000.SH"]["reference"]
    started = ft.begin_ftshare_slot(collection, last, at=host.now)
    reserved = _retain(started, host.fixture["store"], host.fixture["fresh"], saved["reference"])
    finished = ft.finish_ftshare_slot(started, last, at=host.now, reference=reference)
    content = {"schema": ft.SCHEMA, "registry": deepcopy(REGISTRY.snapshot),
        "predecessor": reserved["reference"], "collection": finished}
    # Bypass the FT source-custody check to model a forged immutable control.
    # Its ordinary metadata replay is valid; its claimed source is another owner.
    wrong = host.fixture["store"].put_source("synthetic-wrong-owner-" + owner, ft._source(content))
    wrong = json.loads(encoded(asdict(wrong)))
    assert ft.restore_ftshare_collection(wrong, REGISTRY,
        host.fixture["fresh"]())["collection"] == finished
    state = _claim(host, item, wrong)
    before = host.fixture["backend"].head()
    with pytest.raises(FTShareSingleError, match="FTSHARE_SINGLE_SOURCE_CHANGED"):
        _retain_daily(host, item, state)
    assert host.fixture["backend"].head() == before
    _inject_daily(host, state)
    with pytest.raises(FTShareSingleError, match="FTSHARE_SINGLE_SOURCE_CHANGED"):
        daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY)


@pytest.mark.parametrize("terminal", [False, True])
def test_a_success_receipt_cannot_promote_unspent_or_failed_ft_reservations(pending, terminal):
    host, item = pending
    collection = _plan(host, item)
    saved = _retain(collection, host.fixture["store"], host.fixture["fresh"])
    if terminal:
        collection, saved = settle_single(host.fixture, collection, saved,
            reason_code="FTSHARE_HTTP_403", source_hold="FTSHARE_HTTP_403")
    state = _claim(host, item, saved["reference"])
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="DAILY_MASTER_NOT_ADOPTED"):
        _retain_daily(host, item, state)
    assert host.fixture["backend"].head() == before and host.cycle() == item
