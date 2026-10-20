"""Daily admission follows original FT additions without reopening an old cycle.

These integration tests retain real SourceRefs and restore their original bodies;
the transport data and clocks come from the existing synthetic provider fixtures.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.ftshare_collection import (
    begin_ftshare_slot, restore_ftshare_collection,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces import daily_reading_cli as reading_cli
from test_daily_market import NOW
from test_daily_market_schedule import WindowHost
from test_ftshare_collection import _new_calendars, _retain, plan_single, settle_single
from test_market_relay_inputs import REGISTRY


def current_cycle(host):
    return daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])


def restored(host, reference):
    return restore_ftshare_collection(reference, REGISTRY, host.fixture["fresh"]())["collection"]


def manual(host, base, generation, *, symbol="600001.SH", stage="complete", **changes):
    """Append a genuine existing FT generation, normally just one new owner."""
    f = host.fixture
    prior = restored(host, base)["plan"]
    state = plan_single(f, base=base, generation=generation, created_at=host.now,
        **{"start": prior["start"], "end": prior["end"],
           "completion_policy": prior.get("completion_policy"),
           "targets": [{"symbol": symbol, "adjust_kind": "none"}],
           "source_budget": 1, **changes})
    saved = _retain(state, f["store"], f["fresh"])
    if stage == "pending":
        return state, saved["reference"]
    if stage == "in-flight":
        state = begin_ftshare_slot(state, state["plan"]["new_slots"][0], at=host.now)
        saved = _retain(state, f["store"], f["fresh"], saved["reference"])
    else:
        assert stage in {"complete", "failed", "held"}
        failure = "FTSHARE_HTTP_403" if stage == "held" else "FTSHARE_HTTP_503" if stage == "failed" else None
        state, saved = settle_single(f, state, saved, reason_code=failure,
            source_hold=failure if stage == "held" else None)
    return state, saved["reference"]


def no_daily_sources(host, result, *, head, clients=(0, 0)):
    assert result["receipt"]["source_requests"] == 0, result
    assert (result["receipt"]["calendar_source_requests"], result["receipt"]["price_source_requests"]) == (0, 0)
    assert (len(host.calendar_clients), len(host.price_clients)) == clients
    assert host.fixture["backend"].head() == head


def test_first_daily_inherits_two_retained_additions_with_the_original_scope_and_allowances():
    host = WindowHost(count=5)
    seed = restored(host, host.approved["seed_reference"])
    first, first_ref = manual(host, host.approved["seed_reference"], "manual-first")
    second, second_ref = manual(host, first_ref, "manual-second", symbol="600002.SH")
    before = host.fixture["backend"].head()
    selected = daily.daily_market_reference(host.fixture["fresh"](), REGISTRY, host.approved)
    assert selected["reference"] == second_ref and selected["cycle"] is None
    assert selected["frontier_reason_code"] is selected["frontier_blocking_reference"] is None
    assert selected["frontier_successor_references"] == [first_ref, second_ref]
    assert host.fixture["backend"].head() == before
    for slot, request in seed["plan"]["requests"].items():
        assert second["plan"]["requests"][slot] == request
        assert second["units"][slot] == {**seed["units"][slot], "request_count": 0}
    assert first["plan"]["prior_generation_reserved_requests"] == 1
    assert second["plan"]["prior_generation_reserved_requests"] == 2

    result = host.run()
    assert result["receipt"]["status"] == "complete", result
    assert (result["receipt"]["calendar_source_requests"], result["receipt"]["price_source_requests"]) == (2, 4)
    cycle = current_cycle(host)["cycle"]
    assert cycle["base_market_reference"] == second_ref
    assert cycle["date"] == cycle["calendar_end"] == cycle["price_end"] == "20261019"
    assert cycle["created_at"] == cycle["updated_at"] == NOW
    prices = restored(host, cycle["market_reference"])
    assert prices["plan"]["base_reference"] == second_ref
    assert prices["plan"]["source_budget"] == prices["reserved_requests"] == 4
    assert prices["plan"]["prior_generation_reserved_requests"] == 3
    assert set(prices["plan"]["new_slots"]) == {
        kind + ":" + symbol for kind in ("none", "forward") for symbol in host.approved["symbols"]}
    for slot in ("none:600001.SH", "none:600002.SH"):
        assert prices["plan"]["requests"][slot] == second["plan"]["requests"][slot]
        assert prices["units"][slot] == {**second["units"][slot], "request_count": 0}
    reading = read_ftshare_collection_version(cycle["market_reference"], REGISTRY,
        host.fixture["fresh"](), read_as_of=host.now)
    assert reading["source_requests"] == 0 and reading["view"]["scope_count"] == 7
    assert result["receipt"]["stock_count"] == 2
    assert result["receipt"]["long_term_uptime_certified"] is False


def test_recorded_cross_day_bridge_survives_later_failed_addition_and_exposes_the_actual_tip():
    host = WindowHost(count=5)
    assert host.run()["receipt"]["status"] == "complete"
    previous = current_cycle(host)
    host.now = "2026-10-19T12:20:00+00:00"
    _, bridge_ref = manual(host, previous["cycle"]["market_reference"], "manual-between-days")
    host.now = "2026-10-20T12:10:00+00:00"
    result = host.run()
    assert result["receipt"]["status"] == "complete", result
    assert (result["receipt"]["calendar_source_requests"], result["receipt"]["price_source_requests"]) == (2, 4)
    current = current_cycle(host)
    assert current["cycle"]["base_market_reference"] == bridge_ref
    assert current["cycle"]["date"] == "20261020"
    prices = restored(host, current["cycle"]["market_reference"])
    assert prices["plan"]["prior_generation_reserved_requests"] == 6
    assert prices["reserved_requests"] == 4
    # The recorded unequal bridge cannot be accepted by an unqualified caller.
    with pytest.raises(RecordError, match="DAILY_MARKET_FRONTIER_PROOF_REQUIRED"):
        daily.latest_cycle(host.fixture["fresh"]())

    host.now = "2026-10-20T12:20:00+00:00"
    failed, failed_ref = manual(host, current["cycle"]["market_reference"],
        "manual-failed-after-daily", symbol="600002.SH", stage="failed")
    later, later_ref = manual(host, failed_ref, "manual-after-failed", symbol="600003.SH")
    before = host.fixture["backend"].head()
    clients = len(host.calendar_clients), len(host.price_clients)
    # Exact history ends at the recorded bridge; later descendants are current
    # reading evidence and cannot retroactively change either daily control.
    assert current_cycle(host) == current
    selected = daily.daily_market_reference(host.fixture["fresh"](), REGISTRY, host.approved)
    assert selected["cycle"] == current["cycle"]
    assert selected["reference"] == later_ref
    assert selected["frontier_reason_code"] == "FTSHARE_EXPANSION_FAILED"
    assert selected["frontier_blocking_reference"] == failed_ref
    assert selected["frontier_successor_references"] == [failed_ref, later_ref]
    status = host.run(operation="status")
    no_daily_sources(host, status, head=before, clients=clients)
    assert status["receipt"]["status"] == "blocked"
    assert status["receipt"]["cycle_status"] == "complete"
    assert status["control"]["market_reference"] == later_ref
    assert status["control"]["frontier_blocking_reference"] == failed_ref
    reading = read_ftshare_collection_version(later_ref, REGISTRY, host.fixture["fresh"](), read_as_of=host.now)
    assert reading["selected_reference"] == later_ref and reading["serving"] == "current-partial"
    assert reading["latest_attempt"]["status"] == "partial"
    assert reading["sources"]["none:600002.SH"]["reason_code"] == "FTSHARE_HTTP_503"
    assert later["units"]["none:600002.SH"] == {**failed["units"]["none:600002.SH"], "request_count": 0}
    assert later["plan"]["prior_generation_reserved_requests"] == 11
    assert current_cycle(host) == current
    host.now = "2026-10-21T12:10:00+00:00"
    next_day = host.run()
    no_daily_sources(host, next_day, head=before, clients=clients)
    assert next_day["receipt"]["reason_code"] == "FTSHARE_EXPANSION_FAILED"
    assert next_day["control"]["market_reference"] == later_ref
    assert current_cycle(host) == current


@pytest.mark.parametrize("stage", ["complete", "failed"])
def test_completed_same_day_keeps_its_exact_cycle_and_budget_after_manual_addition(stage, monkeypatch):
    host = WindowHost(count=5)
    first = host.run()
    assert first["receipt"]["status"] == "complete"
    original_receipt = encoded(first["receipt"])
    previous = current_cycle(host)
    host.now = "2026-10-19T12:20:00+00:00"
    _, reference = manual(host, previous["cycle"]["market_reference"], "manual-same-day", stage=stage)
    before = host.fixture["backend"].head()
    clients = len(host.calendar_clients), len(host.price_clients)
    repeated = host.run()
    no_daily_sources(host, repeated, head=before, clients=clients)
    assert current_cycle(host) == previous
    assert repeated["receipt"]["cycle_status"] == "complete"
    assert repeated["receipt"]["status"] == ("already-complete" if stage == "complete" else "blocked")
    assert repeated["control"]["market_reference"] == reference
    assert previous["cycle"]["market_reference"] != reference
    assert restored(host, previous["cycle"]["market_reference"])["reserved_requests"] == 4
    if stage == "failed":
        from decision_kernel.application import reading_market

        class ReaderClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat(host.now).astimezone(tz or timezone.utc)

        # Use the original room, market resolver and exact FT reader. Only the
        # trusted current clock is synthetic, like the provider's capture clock.
        monkeypatch.setattr(reading_market, "datetime", ReaderClock)
        model = reading_cli.build_reading(host.fixture["fresh"](), host.fixture["fresh"](),
            REGISTRY, host.approved, first["receipt"], as_of=host.now)
        exported = json.loads(encoded(model))
        context = exported["daily_update"]
        assert encoded(first["receipt"]) == original_receipt
        assert context["source_receipt"]["status"] == "complete"
        assert context["market_resolution"] == "resolved" and exported["status"] == "partial"
        assert context["market_cycle_reference"] == previous["reference"]
        assert context["frontier_reason_code"] == "FTSHARE_EXPANSION_FAILED"
        assert context["frontier_blocking_reference"] == reference
        assert context["frontier_successor_references"] == [reference]
        assert exported["market"]["reference"] == reference
        assert exported["market"]["status"] == "readable"
        assert exported["market"]["view"]["selected_reference"] == reference
        assert exported["market"]["view"]["serving"] == "current-partial"
        assert exported["market"]["view"]["sources"]["none:600001.SH"]["reason_code"] == "FTSHARE_HTTP_503"
        page = reading_cli.daily_page(model)
        panel = page.split('aria-label="本次更新情况"', 1)[1].split("</section>", 1)[0]
        visible = panel.split("<details>", 1)[0]
        assert "本次限定行情更新完成" in visible
        assert "最新行情仍有未解决限制" in visible and "FTSHARE_EXPANSION_FAILED" in visible
        assert context["source_http_requests"] == 0 and context["records_write"] == "not-requested"
        assert host.fixture["backend"].head() == before
        assert (len(host.calendar_clients), len(host.price_clients)) == clients


@pytest.mark.parametrize("stage,reason,reserved", [
    ("pending", "FTSHARE_SINGLE_BASE_NOT_TERMINAL", 0),
    ("in-flight", "FTSHARE_SINGLE_BASE_NOT_TERMINAL", 1),
    ("failed", "FTSHARE_EXPANSION_FAILED", 1),
    ("held", "FTSHARE_COLLECTION_SOURCE_HOLD", 1),
])
def test_latest_non_complete_manual_generation_stops_before_any_daily_source_or_intent(stage, reason, reserved):
    host = WindowHost(count=5)
    state, reference = manual(host, host.approved["seed_reference"], "manual-blocked", stage=stage)
    before = host.fixture["backend"].head()
    result = host.run()
    no_daily_sources(host, result, head=before)
    assert result["receipt"]["status"] == "blocked" and result["receipt"]["reason_code"] == reason
    assert current_cycle(host) is None
    assert result["control"]["market_reference"] == reference
    assert result["control"]["frontier_reason_code"] == reason
    assert result["control"]["frontier_blocking_reference"] == reference
    assert result["control"]["frontier_successor_references"] == [reference]
    assert restored(host, reference) == state and state["reserved_requests"] == reserved
    reading = read_ftshare_collection_version(reference, REGISTRY, host.fixture["fresh"](), read_as_of=host.now)
    assert reading["serving"] == "current-partial" and reading["source_requests"] == 0
    assert reading["latest_attempt"]["reserved_requests"] == reserved
    source = reading["sources"]["none:600001.SH"]
    assert source["status"] == "not-retained" and source["reference"] is None
    assert source["reason_code"] == state["units"]["none:600001.SH"]["reason_code"]
    if stage == "held":
        assert reading["latest_attempt"]["source_hold"] == {"reason_code": "FTSHARE_HTTP_403", "set_at": NOW}


def test_stale_new_cycle_is_rejected_after_a_real_successor_arrives_before_intent_retention(monkeypatch):
    host = WindowHost(count=5)
    _, first_ref = manual(host, host.approved["seed_reference"], "manual-before-selection")
    original, injected = cli.initial_cycle, {}

    def competing_addition(*args, **kwargs):
        prepared = original(*args, **kwargs)
        assert prepared["candidate"]["base_market_reference"] == first_ref
        _, injected["reference"] = manual(host, first_ref, "manual-during-selection", symbol="600002.SH")
        injected["head"] = host.fixture["backend"].head()
        return prepared

    monkeypatch.setattr(cli, "initial_cycle", competing_addition)
    result = host.run()
    no_daily_sources(host, result, head=injected["head"])
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_STALE_FRONTIER"
    assert current_cycle(host) is None
    assert result["control"]["market_reference"] == injected["reference"]


@pytest.mark.parametrize("arrival", ["before-store-head", "before-backend-cas"])
def test_checked_frontier_is_bound_to_the_same_head_through_intent_cas(monkeypatch, arrival):
    host = WindowHost(count=5)
    _, base_reference = manual(host, host.approved["seed_reference"], "manual-before-cas")
    f, raced = host.fixture, {}
    checked_head = f["backend"].head()
    original_put, original_commit = f["store"].put_source, f["backend"].commit

    def insert_child(expected):
        assert expected == checked_head
        # The competing owner uses the unchanged original retention path.
        monkeypatch.setattr(f["store"], "put_source", original_put)
        monkeypatch.setattr(f["backend"], "commit", original_commit)
        _, raced["reference"] = manual(host, base_reference, "manual-at-cas", symbol="600002.SH")
        raced["head"] = f["backend"].head()

    if arrival == "before-store-head":
        def before_head(submission, source, **kwargs):
            if source.source_id == "daily-market-cycle-20261019-r000":
                insert_child(kwargs.get("expected_head"))
            return original_put(submission, source, **kwargs)
        monkeypatch.setattr(f["store"], "put_source", before_head)
    else:
        def before_cas(files, *, expected):
            if any(path.startswith("source-views/daily-market-cycle-20261019-r000/") for path in files):
                insert_child(expected)
            return original_commit(files, expected=expected)
        monkeypatch.setattr(f["backend"], "commit", before_cas)
    result = host.run()
    no_daily_sources(host, result, head=raced["head"])
    assert result["receipt"]["reason_code"] == "HEAD_CHANGED"
    assert current_cycle(host) is None
    assert result["control"]["market_reference"] == raced["reference"]

    # Refusal leaves no stale r000 to trap a later run in permanent pending.
    resumed = host.run()
    assert resumed["receipt"]["status"] == "complete", resumed
    assert (resumed["receipt"]["calendar_source_requests"], resumed["receipt"]["price_source_requests"]) == (2, 4)
    current = current_cycle(host)["cycle"]
    assert current["base_market_reference"] == raced["reference"]
    prices = restored(host, current["market_reference"])
    assert prices["plan"]["prior_generation_reserved_requests"] == 3
    assert prices["reserved_requests"] == 4


@pytest.mark.parametrize("arrival", ["before-reservation-check", "after-reservation-check"])
def test_calendar_reservation_checks_and_commits_the_same_frontier_before_source(monkeypatch, arrival):
    host = WindowHost(count=5)
    _, base_reference = manual(host, host.approved["seed_reference"], "manual-before-calendar-race")
    raced = {}
    original_retain, original_put = cli.retain_cycle, host.fixture["store"].put_source

    def insert_child():
        raced["cycle"] = current_cycle(host)
        assert raced["cycle"]["cycle"]["revision"] == 0
        monkeypatch.setattr(cli, "retain_cycle", original_retain)
        monkeypatch.setattr(host.fixture["store"], "put_source", original_put)
        _, raced["reference"] = manual(host, base_reference, "manual-at-calendar-reservation", symbol="600002.SH")
        raced["head"] = host.fixture["backend"].head()

    if arrival == "before-reservation-check":
        def before_check(state, store, **kwargs):
            if state["revision"] == 1:
                insert_child()
            return original_retain(state, store, **kwargs)
        monkeypatch.setattr(cli, "retain_cycle", before_check)
    else:
        def after_check(submission, source, **kwargs):
            if source.source_id == "daily-market-cycle-20261019-r001":
                insert_child()
            return original_put(submission, source, **kwargs)
        monkeypatch.setattr(host.fixture["store"], "put_source", after_check)
    result = host.run()
    no_daily_sources(host, result, head=raced["head"])
    assert result["receipt"]["reason_code"] == ("FTSHARE_SINGLE_BASE_ALREADY_HAS_SUCCESSOR"
        if arrival == "before-reservation-check" else "HEAD_CHANGED")
    assert current_cycle(host) == raced["cycle"]
    cycle = raced["cycle"]["cycle"]
    assert cycle["base_market_reference"] == base_reference
    assert all(row["status"] == "pending" and row["reserved_requests"] == 0 for row in cycle["calendars"].values())
    assert result["control"]["market_reference"] == raced["reference"]
    assert result["control"]["frontier_reason_code"] == "DAILY_MARKET_PENDING_FRONTIER_CHANGED"


def test_old_pending_cycle_cannot_rebase_to_an_external_successor_before_calendars():
    host = WindowHost(count=5)
    prepared = daily.initial_cycle(host.approved, host.fixture["fresh"](), REGISTRY, at=host.now)
    old = daily.retain_cycle(prepared["candidate"], host.fixture["store"], registry=REGISTRY,
        fresh_reader=host.fixture["fresh"], previous=None, protected_symbols=host.approved["symbols"])
    _, reference = manual(host, old["cycle"]["base_market_reference"], "manual-after-daily-intent")
    before = host.fixture["backend"].head()
    host.now = "2026-10-20T12:10:00+00:00"
    result = host.run()
    no_daily_sources(host, result, head=before)
    assert result["receipt"]["reason_code"] == "FTSHARE_SINGLE_BASE_ALREADY_HAS_SUCCESSOR"
    assert result["control"]["frontier_reason_code"] == "DAILY_MARKET_PENDING_FRONTIER_CHANGED"
    assert result["control"]["market_reference"] == reference
    assert current_cycle(host) == old
    assert old["cycle"]["date"] == old["cycle"]["calendar_end"] == "20261019"
    assert all(row["reserved_requests"] == 0 for row in old["cycle"]["calendars"].values())


def test_own_completed_ft_with_unfinished_cycle_cannot_cross_a_later_manual_successor():
    host = WindowHost(count=5)

    def interrupted_completion(**options):
        result = cli.run_collection(**options)
        assert result["receipt"]["status"] == "complete"
        raise UnknownWrite("SYNTHETIC_DAILY_FINAL_COMPLETION_UNCONFIRMED")

    first = host.run(execute_prices=interrupted_completion)
    assert first["receipt"]["status"] == "persistence-unconfirmed", first
    old = current_cycle(host)
    assert old["cycle"]["status"] == "market-pending"
    own_reference = first["control"]["market_reference"]
    assert restored(host, own_reference)["reserved_requests"] == 4
    host.now = "2026-10-19T12:20:00+00:00"
    _, reference = manual(host, own_reference, "manual-after-unfinished-cycle")
    before = host.fixture["backend"].head()
    clients = len(host.calendar_clients), len(host.price_clients)
    host.now = "2026-10-20T12:10:00+00:00"
    result = host.run()
    no_daily_sources(host, result, head=before, clients=clients)
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_PENDING_FRONTIER_CHANGED"
    assert result["control"]["market_reference"] == reference
    assert current_cycle(host) == old
    assert restored(host, own_reference)["reserved_requests"] == 4


def test_own_safe_pending_ft_keeps_its_expanded_base_and_only_collects_remaining_owners_next_day():
    host = WindowHost(count=5)
    _, base_reference = manual(host, host.approved["seed_reference"], "manual-before-safe-pending")
    first = host.run(execute_prices=lambda **options: cli.run_collection(**{**options, "max_work_units": 1}))
    assert (first["receipt"]["calendar_source_requests"], first["receipt"]["price_source_requests"]) == (2, 1)
    old = current_cycle(host)["cycle"]
    assert old["status"] == "market-pending" and old["base_market_reference"] == base_reference
    prior = restored(host, first["control"]["market_reference"])
    consumed = [slot for slot in prior["plan"]["new_slots"] if prior["units"][slot]["status"] == "complete"]
    assert len(consumed) == 1 and prior["reserved_requests"] == 1
    assert sum(row["status"] == "pending" for row in prior["units"].values()) == 3
    host.now = "2026-10-20T12:10:00+00:00"
    resumed = host.run()
    assert resumed["receipt"]["status"] == "complete", resumed
    assert (resumed["receipt"]["calendar_source_requests"], resumed["receipt"]["price_source_requests"]) == (0, 3)
    current = current_cycle(host)["cycle"]
    assert current["date"] == current["calendar_end"] == current["price_end"] == "20261019"
    assert current["created_at"] == old["created_at"] == NOW
    assert current["base_market_reference"] == base_reference
    prices = restored(host, current["market_reference"])
    assert prices["plan"] == prior["plan"]
    assert prices["reserved_requests"] == prices["plan"]["source_budget"] == 4
    assert prices["plan"]["prior_generation_reserved_requests"] == 2
    assert prices["units"][consumed[0]] == prior["units"][consumed[0]]
    assert len(host.calendar_clients) == 1 and sum(client.requests for client in host.price_clients) == 4


@pytest.mark.parametrize("change", ["end", "calendars", "old-owner-request", "protected-owner"])
def test_general_ft_changes_do_not_acquire_daily_expansion_authority(change):
    host = WindowHost(count=5)
    options = {}
    if change == "end":
        host.now = "2026-10-20T12:10:00+00:00"
        options.update(end="20261019", calendar_references=_new_calendars(
            host.fixture["store"], host.fixture["fresh"], through="20261019"))
    elif change == "calendars":
        options["calendar_references"] = _new_calendars(host.fixture["store"], host.fixture["fresh"], through="20261018")
    elif change == "old-owner-request":
        options.update(start="20260702", symbol="600000.SH")
    else:
        host.approved["symbols"].append("600001.SH")
        host.approved["max_price_requests"] = 6
    state, reference = manual(host, host.approved["seed_reference"], "manual-general-change", **options)
    assert all(unit["status"] == "complete" for unit in state["units"].values())
    before = host.fixture["backend"].head()
    result = host.run()
    no_daily_sources(host, result, head=before)
    expected = "FTSHARE_EXPANSION_PROTECTED_SYMBOL" if change == "protected-owner" else "FTSHARE_EXPANSION_WINDOW_CHANGED"
    assert result["receipt"]["status"] == "blocked" and result["receipt"]["reason_code"] == expected
    assert current_cycle(host) is None
    assert result["control"]["market_reference"] == reference
    assert result["control"]["frontier_blocking_reference"] == reference
    assert result["control"]["frontier_reason_code"] == expected
    assert restored(host, reference) == state


def test_unknown_intent_write_restores_the_original_expanded_base_without_a_new_date_or_budget(monkeypatch):
    host = WindowHost(count=5)
    _, base_reference = manual(host, host.approved["seed_reference"], "manual-before-lost-intent")
    original = host.fixture["store"].put_source
    attempts = []

    def lost(submission, source, **kwargs):
        value = original(submission, source, **kwargs)
        if source.source_id == "daily-market-cycle-20261019-r000":
            attempts.append({"submission": submission, "source": source, "kwargs": kwargs})
            raise UnknownWrite("SYNTHETIC_DAILY_INTENT_RESPONSE_LOST")
        return value

    monkeypatch.setattr(host.fixture["store"], "put_source", lost)
    first = host.run()
    assert first["receipt"]["status"] == "persistence-unconfirmed", first
    assert first["receipt"]["source_requests"] == 0 and host.calendar_clients == host.price_clients == []
    old = current_cycle(host)
    assert old["cycle"]["revision"] == 0 and old["cycle"]["base_market_reference"] == base_reference
    monkeypatch.setattr(host.fixture["store"], "put_source", original)
    retained_head = host.fixture["backend"].head()
    attempt = attempts[0]
    assert attempt["kwargs"]["expected_head"] != retained_head
    reconciled = original(attempt["submission"], attempt["source"], **attempt["kwargs"])
    assert encoded(asdict(reconciled)) == encoded(old["reference"])
    assert host.fixture["backend"].head() == retained_head
    assert host.calendar_clients == host.price_clients == []
    host.now = "2026-10-20T12:10:00+00:00"
    result = host.run()
    assert result["receipt"]["status"] == "complete", result
    assert (result["receipt"]["calendar_source_requests"], result["receipt"]["price_source_requests"]) == (2, 4)
    current = current_cycle(host)["cycle"]
    assert [attempt["source"].source_id for attempt in attempts] == ["daily-market-cycle-20261019-r000"]
    assert current["date"] == current["calendar_end"] == current["price_end"] == "20261019"
    assert current["base_market_reference"] == base_reference and current["created_at"] == NOW
    prices = restored(host, current["market_reference"])
    assert prices["plan"]["prior_generation_reserved_requests"] == 2
    assert prices["reserved_requests"] == prices["plan"]["source_budget"] == 4


@pytest.mark.parametrize("failure", ["calendar-in-flight", "price-held"])
def test_expanded_base_does_not_reopen_old_daily_holds_or_unknown_reservations(monkeypatch, failure):
    host = WindowHost(count=5)
    _, base_reference = manual(host, host.approved["seed_reference"], "manual-before-failed-cycle")
    original = host.fixture["store"].put_source
    if failure == "price-held":
        host.price_error = RecordError("FTSHARE_HTTP_403")
    else:
        def lost(submission, source, **kwargs):
            reference = original(submission, source, **kwargs)
            if source.source_id.endswith("-r001") and source.source_id.startswith("daily-market-cycle-"):
                raise UnknownWrite("SYNTHETIC_DAILY_RESERVATION_LOST")
            return reference
        monkeypatch.setattr(host.fixture["store"], "put_source", lost)
    first = host.run()
    old = current_cycle(host)
    assert old["cycle"]["base_market_reference"] == base_reference
    before = host.fixture["backend"].head()
    clients = len(host.calendar_clients), len(host.price_clients)
    monkeypatch.setattr(host.fixture["store"], "put_source", original)
    host.price_error = None
    host.now = "2026-10-20T12:10:00+00:00"
    result = host.run()
    no_daily_sources(host, result, head=before, clients=clients)
    assert current_cycle(host) == old
    assert result["receipt"]["cycle_date"] == "20261019"
    if failure == "price-held":
        assert result["receipt"]["status"] == "source-held"
        prices = restored(host, first["control"]["market_reference"])
        assert prices["plan"]["prior_generation_reserved_requests"] == 2
        assert prices["reserved_requests"] == 1
        assert prices["source_hold"] == {"reason_code": "FTSHARE_HTTP_403", "set_at": NOW}
    else:
        assert result["receipt"]["status"] == "persistence-unconfirmed"
        assert old["cycle"]["calendars"]["SSE"]["reserved_requests"] == 1
