"""Delayed native slots keep real clocks and the original durable source budget."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application import daily_market as daily
from decision_kernel.application.daily_clock import (
    CLOCK_SCHEMA, CURRENT_MAIN, EXTERNAL_CLOCK, EXTERNAL_CRON, EXTERNAL_RUN_TITLE,
)
from decision_kernel.application.ftshare_collection import restore_ftshare_collection
from decision_kernel.application.ftshare_single import restore_single
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.interfaces import daily_market_cli as cli
from decision_kernel.interfaces.daily_reading_cli import read_update_receipt
from test_daily_market import Host, NOW, native_context
from test_ftshare_collection_runtime import NativeAPI
from test_market_relay_inputs import REGISTRY


@pytest.fixture
def approved():
    return json.loads(cli.POLICY_FILE.read_text())


class WindowHost(Host):
    """Keep Host's custody and provider fixtures, respecting calendar query bounds.

    The existing opener supplies every day through the actual execution date.
    Delayed callers request an earlier end, so this transport returns only the
    requested portion without changing the actual capture or execution clock.
    """
    def calendar(self):
        client = super().calendar()
        opener, respond = client._opener, client._opener.open
        original = deepcopy(opener.rows)

        def bounded(request, timeout):
            params = {key: values[0] for key, values in parse_qs(urlsplit(request.full_url).query).items()}
            key = ("trade_cal", params["exchange"])
            opener.rows[key] = [row for row in original[key]
                if params["start_date"] <= row["cal_date"] <= params["end_date"]]
            return respond(request, timeout)

        opener.open = bounded
        return client


@pytest.mark.parametrize("at,business_date,scheduled_for,delay", [
    ("2026-10-19T17:07:33Z", "20261019", "2026-10-19T18:35:00+08:00", 23553.0),
    ("2026-10-20T01:07:33+08:00", "20261019", "2026-10-19T18:35:00+08:00", 23553.0),
    ("2026-10-19T10:35:00Z", "20261019", "2026-10-19T18:35:00+08:00", 0.0),
    ("2026-10-20T10:34:59.999999Z", "20261019", "2026-10-19T18:35:00+08:00", 86399.999999),
    ("2026-10-20T10:35:00Z", "20261020", "2026-10-20T18:35:00+08:00", 0.0),
    ("2026-10-23T23:30:00Z", "20261023", "2026-10-23T18:35:00+08:00", 46500.0),
    ("2026-10-24T10:34:59.999999Z", "20261023", "2026-10-23T18:35:00+08:00", 86399.999999),
])
def test_late_slot_has_an_exact_weekday_cutoff_and_keeps_native_timestamp(
        approved, at, business_date, scheduled_for, delay):
    admitted = daily.admission_slot(approved, at=at, allow_late=True)
    assert admitted == {"mode": "latest-elapsed-schedule", "admitted_at": at,
        "business_date": business_date, "scheduled_for": scheduled_for,
        "delay_seconds": pytest.approx(delay, abs=0.000001), "max_delay_seconds": 86400}
    assert type(admitted["delay_seconds"]) is float


@pytest.mark.parametrize("at", [
    "2026-10-24T10:35:00Z",  # Exactly 24 hours after Friday's slot is not admissible.
    "2026-10-25T04:00:00Z",
    "2026-10-26T02:00:00Z",
    "2026-10-26T10:34:59Z",
])
def test_weekend_and_monday_morning_do_not_reopen_an_older_weekday_slot(approved, at):
    with pytest.raises(RecordError, match="DAILY_MARKET_NO_RECENT_SCHEDULE"):
        daily.admission_slot(approved, at=at, allow_late=True)


def test_ordinary_admission_keeps_its_same_day_cutoff(approved):
    with pytest.raises(RecordError, match="DAILY_MARKET_BEFORE_CLOSE"):
        daily.admission_slot(approved, at="2026-10-20T10:34:59.999999Z")
    admitted = daily.admission_slot(approved, at="2026-10-20T10:35:00Z")
    assert admitted["mode"] == "same-local-day" and admitted["business_date"] == "20261020"
    assert admitted["delay_seconds"] == 0 and admitted["max_delay_seconds"] is None


@pytest.mark.parametrize("override", [
    {"business_date": "20261016"}, {"admission_at": "2026-10-19T17:07:33Z"},
])
def test_explicit_recovery_cannot_take_a_caller_chosen_day_or_native_clock(approved, override):
    event, env = native_context()
    event["inputs"].update(operation="recover-late", **override)
    with pytest.raises(RecordError, match="DAILY_MARKET_INPUT"):
        cli.settings(event, env, approved)


def native_runtime(tmp_path, monkeypatch, approved, *, invocation, admitted_at, actual_at):
    event, env = native_context()
    if invocation == "schedule":
        event = {"schedule": approved["schedule_utc"]}
        env["GITHUB_EVENT_NAME"] = "schedule"
    else:
        event["inputs"]["operation"] = invocation
    if invocation == EXTERNAL_CLOCK:
        from decision_kernel.interfaces import daily_visibility_cli as visibility
        event["inputs"]["code_sha"] = CURRENT_MAIN
        clock_file = tmp_path / "clock.json"
        clock_file.write_bytes(encoded({"schema": CLOCK_SCHEMA, "primary": EXTERNAL_CRON}))
        monkeypatch.setattr(visibility, "CLOCK_POLICY_FILE", clock_file)
    env.update(RUNNER_TEMP=str(tmp_path), DECISION_OS_STORE_TOKEN="synthetic-daily-slot-only")
    config = tmp_path / "policy.json"
    config.write_bytes(encoded(approved))
    monkeypatch.setattr(cli, "POLICY_FILE", config)
    actual = datetime.fromisoformat(actual_at.replace("Z", "+00:00"))

    class ActualClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return actual.astimezone(tz or timezone.utc)

    monkeypatch.setattr(cli, "datetime", ActualClock)
    api = NativeAPI()
    api.run.update(path=cli.WORKFLOW, name=cli.TITLE, event=env["GITHUB_EVENT_NAME"], created_at=admitted_at)
    if invocation == EXTERNAL_CLOCK:
        api.run["display_title"] = EXTERNAL_RUN_TITLE
    api.ci.update(total_count=1)
    api.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    return event, env, api


@pytest.mark.parametrize("invocation", ["schedule", "recover-late", "update", EXTERNAL_CLOCK])
def test_native_admission_is_frozen_across_next_cutoff_and_only_explicit_late_modes_collect(
        tmp_path, monkeypatch, invocation):
    host = WindowHost()
    admitted = "2026-10-19T17:07:33Z"  # The actual delayed native creation observed in #10.
    host.now = "2026-10-20T10:36:00+00:00"  # Queue crosses the next slot, but remains under 24 hours.
    event, env, api = native_runtime(tmp_path, monkeypatch, host.approved,
        invocation=invocation, admitted_at=admitted, actual_at=host.now)
    before = host.fixture["backend"].head()
    calls = []

    def execute(**options):
        calls.append(options)
        return host.run(operation=options["operation"], admission_at=options["admission_at"],
            allow_late=options["allow_late"], progress=options["progress"])

    out = tmp_path / "daily"
    assert cli.run_runtime(event, env, out, api=api, execute=execute) == 0
    selected = cli.settings(event, env, host.approved, clock=cli.load_clock_policy())
    receipt = read_update_receipt(out, selected)
    assert selected["invocation"] == invocation and selected["operation"] == "update"
    assert len(calls) == 1 and calls[0]["admission_at"] == "2026-10-19T17:07:33+00:00"
    assert calls[0]["allow_late"] is (invocation != "update")
    if invocation == "update":
        assert receipt["status"] == "not-due" and receipt["reason_code"] == "DAILY_MARKET_BEFORE_CLOSE"
        assert receipt["source_requests"] == 0 and host.calendar_clients == host.price_clients == []
        assert host.fixture["backend"].head() == before
        assert daily.latest_cycle(host.fixture["fresh"]()) is None
        return

    assert receipt["status"] == "complete", receipt
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"]) == (2, 4)
    assert receipt["admission"]["business_date"] == receipt["cycle_date"] == "20261019"
    assert receipt["admission"]["admitted_at"] == calls[0]["admission_at"]
    assert receipt["started_at"] == receipt["finished_at"] == host.now
    assert receipt["expected_completed_session"] == receipt["calendar_checked_through"] == "20261019"
    assert receipt["health"]["as_of"] == "2026-10-19"
    assert receipt["health"]["freshness"]["status"] == "unknown"
    assert receipt["long_term_uptime_certified"] is False
    item = daily.latest_cycle(host.fixture["fresh"]())
    cycle = item["cycle"]
    assert cycle["date"] == cycle["calendar_end"] == "20261019"
    assert cycle["created_at"] == cycle["updated_at"] == host.now
    assert cycle["market_generation"] == "daily-20261019"
    restored = restore_ftshare_collection(cycle["market_reference"], REGISTRY, host.fixture["fresh"]())
    plan = restored["collection"]["plan"]
    assert plan["created_at"] == host.now and plan["end"] == "20261019"
    assert plan["prior_generation_reserved_requests"] == 1
    assert restored["collection"]["reserved_requests"] == plan["source_budget"] == 4
    for venue in daily.VENUES:
        assert restored["context"]["calendar_results"][venue]["use"]["parameters"]["end_date"] == "20261019"
    for slot in plan["new_slots"]:
        captured = restore_single(restored["collection"]["units"][slot]["reference"],
            host.fixture["fresh"](), request=plan["requests"][slot])
        assert captured["acquired_at"] == host.now
        assert max(row["trade_date"] for rows in captured["normalized"]["series"].values()
                   for row in rows) == "2026-10-19"
    if invocation in {"recover-late", EXTERNAL_CLOCK}:
        # The normal consumer must not accept a receipt relabelled as an ordinary update.
        (out / "result.json").write_bytes(encoded({**receipt, "invocation": "update"}))
        with pytest.raises(RecordError, match="DAILY_READING_RUNTIME_MISMATCH"):
            read_update_receipt(out, selected)


def test_native_queue_age_cannot_be_reset_by_late_recovery(tmp_path, monkeypatch, approved):
    event, env, api = native_runtime(tmp_path, monkeypatch, approved, invocation="recover-late",
        admitted_at="2026-10-19T17:07:33Z", actual_at="2026-10-20T17:07:33Z")
    out = tmp_path / "expired"
    with pytest.raises(RecordError, match="DAILY_MARKET_NATIVE_CLOCK"):
        cli.run_runtime(event, env, out, api=api,
            execute=lambda **options: pytest.fail("an expired native run cannot enter a source cycle"))
    assert not out.exists()


@pytest.mark.parametrize("at,admitted,reason", [
    ("2026-10-19T17:07:32Z", "2026-10-19T17:07:33Z", "DAILY_MARKET_CLOCK_ROLLBACK"),
    ("2026-10-19T17:07:33Z", "", "DAILY_MARKET_CLOCK"),
])
def test_cycle_creation_requires_a_valid_native_clock_and_cannot_precede_it(at, admitted, reason):
    host = WindowHost()
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match=reason):
        daily.initial_cycle(host.approved, host.fixture["fresh"](), REGISTRY,
            at=at, admission_at=admitted, allow_late=True)
    assert host.fixture["backend"].head() == before


def test_no_recent_slot_is_not_due_without_source_calls_or_cycle_writes():
    host = WindowHost()
    host.now = "2026-10-25T04:00:00+00:00"
    before = host.fixture["backend"].head()
    result = host.run(allow_late=True, admission_at=host.now)
    assert result["receipt"]["status"] == "not-due"
    assert result["receipt"]["reason_code"] == "DAILY_MARKET_NO_RECENT_SCHEDULE"
    assert result["receipt"]["source_requests"] == 0
    assert host.calendar_clients == host.price_clients == []
    assert host.fixture["backend"].head() == before
    assert daily.latest_cycle(host.fixture["fresh"]()) is None


def test_late_invocation_of_a_completed_business_day_does_not_replay_sources():
    host = WindowHost()
    first = host.run()
    assert first["receipt"]["status"] == "complete", first
    before = host.fixture["backend"].head()
    host.now = "2026-10-20T02:00:00+00:00"
    second = host.run(allow_late=True, admission_at=host.now)
    assert second["receipt"]["status"] == "already-complete"
    assert second["receipt"]["admission"]["business_date"] == "20261019"
    assert second["receipt"]["source_requests"] == 0 and second["control"] == first["control"]
    assert len(host.calendar_clients) == len(host.price_clients) == 1
    assert host.fixture["backend"].head() == before


def test_late_recovery_continues_only_the_unreserved_part_of_an_older_pending_cycle():
    host = WindowHost()
    admissions = []

    def capacity(reader):
        admissions.append(reader)
        return {"admit_work_unit": len(admissions) < 3}

    host.capacity = capacity
    first = host.run()
    assert first["receipt"]["reason_code"] == "DAILY_MARKET_STORAGE_CAPACITY"
    assert first["receipt"]["calendar_source_requests"] == 1
    prior = daily.latest_cycle(host.fixture["fresh"]())
    assert prior["cycle"]["status"] == "calendar-pending"
    assert prior["cycle"]["calendars"]["SSE"]["status"] == "complete"
    assert prior["cycle"]["calendars"]["SZSE"]["status"] == "pending"
    host.capacity = lambda reader: {"admit_work_unit": True}
    host.now = "2026-10-20T17:07:33+00:00"
    second = host.run(allow_late=True, admission_at=host.now)
    assert second["receipt"]["status"] == "complete", second
    assert second["receipt"]["admission"]["business_date"] == "20261020"
    assert second["receipt"]["cycle_date"] == "20261019"
    assert (second["receipt"]["calendar_source_requests"], second["receipt"]["price_source_requests"]) == (1, 4)
    current = daily.latest_cycle(host.fixture["fresh"]())["cycle"]
    assert current["calendars"]["SSE"] == prior["cycle"]["calendars"]["SSE"]
    assert current["created_at"] == NOW and current["updated_at"] == host.now
    assert current["calendar_end"] == "20261019"
    assert sum(row["reserved_requests"] for row in current["calendars"].values()) == 2
    assert sum(client.requests for client in host.calendar_clients) == 2


@pytest.mark.parametrize("mode", ["calendar-failed", "price-held", "calendar-in-flight"])
def test_late_recovery_preserves_old_blocked_or_unknown_cycle_and_all_reservations(monkeypatch, mode):
    host = WindowHost()
    original = host.fixture["store"].put_source
    if mode == "calendar-failed":
        host.calendar_error = RecordError("RELAY_AUTH_OR_ENTITLEMENT")
    elif mode == "price-held":
        host.price_error = RecordError("FTSHARE_HTTP_403")
    else:
        def lost(submission, source, **kwargs):
            value = original(submission, source, **kwargs)
            if source.source_id.endswith("-r001"):
                raise UnknownWrite("SYNTHETIC_RESERVATION_RESPONSE_LOST")
            return value
        monkeypatch.setattr(host.fixture["store"], "put_source", lost)
    first = host.run()
    assert first["receipt"]["status"] not in {"complete", "read", "already-complete"}
    previous = daily.latest_cycle(host.fixture["fresh"]())
    before = host.fixture["backend"].head()
    clients = len(host.calendar_clients), len(host.price_clients)
    monkeypatch.setattr(host.fixture["store"], "put_source", original)
    host.calendar_error = host.price_error = None
    host.now = "2026-10-20T17:07:33+00:00"
    repeated = host.run(allow_late=True, admission_at=host.now)
    expected = "persistence-unconfirmed" if mode == "calendar-in-flight" else "source-held"
    assert repeated["receipt"]["status"] == expected, repeated
    assert repeated["receipt"]["admission"]["business_date"] == "20261020"
    assert repeated["receipt"]["cycle_date"] == "20261019"
    assert repeated["receipt"]["source_requests"] == 0
    assert (len(host.calendar_clients), len(host.price_clients)) == clients
    assert daily.latest_cycle(host.fixture["fresh"]()) == previous
    assert host.fixture["backend"].head() == before
    if mode == "price-held":
        current = restore_ftshare_collection(repeated["control"]["market_reference"],
            REGISTRY, host.fixture["fresh"]())["collection"]
        assert current["reserved_requests"] == 1
        assert current["source_hold"] == {"reason_code": "FTSHARE_HTTP_403", "set_at": NOW}
    elif mode == "calendar-in-flight":
        assert repeated["receipt"]["reason_code"] == "DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED"
        assert previous["cycle"]["calendars"]["SSE"]["reserved_requests"] == 1


def test_late_closed_business_day_does_not_select_the_current_unclosed_open_day():
    host = WindowHost()
    host.now = "2026-10-20T02:00:00+00:00"
    host.calendar_edit = lambda venue, row: row.update(is_open=0) if row["cal_date"] == "20261019" else None
    result = host.run(allow_late=True, admission_at=host.now)
    receipt = result["receipt"]
    assert receipt["status"] == "no-new-session", result
    assert receipt["admission"]["business_date"] == receipt["calendar_checked_through"] == "20261019"
    assert receipt["expected_completed_session"] == "20261016"
    assert receipt["calendar_source_requests"] == 2 and receipt["price_source_requests"] == 0
    assert host.price_clients == []
    assert result["control"]["market_reference"] == host.approved["seed_reference"]
    assert daily.latest_cycle(host.fixture["fresh"]())["cycle"]["calendar_end"] == "20261019"
