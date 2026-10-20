"""Source-backed synthetic calendar fixtures; no actual source or research run."""
from copy import deepcopy
from dataclasses import asdict
import json
import re

import pytest

from decision_kernel.application.event_calendar import calendar_plan, read_calendar
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from decision_kernel.observations.event_calendar import acquisition_issues, project_fact, timeline
from decision_kernel.observations.relay_disclosure_date import normalize_disclosure_dates, source_for

AT = "2026-10-20T12:00:00Z"


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "calendar.git"))


def keep(store, source_id, value, *, acquired_at="2026-10-19T09:00:00Z"):
    body = value if isinstance(value, str) else encoded(value).decode()
    source = SourceInput(source_id, "content", "Synthetic retained calendar input", "synthetic://calendar",
                         body, acquired_at=acquired_at)
    return store.put_source(source_id, source)


def fact_value(evidence, **changes):
    return {"event_id": "synthetic-macro-release", "category": "macro", "subject_id": "synthetic-economy",
            "subject_name": "Synthetic economy", "title": "Synthetic monthly release", "period": "2026-09",
            "timezone": "UTC", "status": "confirmed", "announced_at": "2026-10-01",
            "scheduled_for": "2026-10-20", "actual_at": None,
            "evidence": [{"reference": asdict(evidence), "quote": "Synthetic 2026-09 release scheduled 2026-10-20."}],
            **changes}


def prepare(store, *, facts=None, followups=None):
    evidence = keep(store, "calendar-original", "Synthetic 2026-09 release scheduled 2026-10-20.")
    values = facts if facts is not None else [fact_value(evidence)]
    packet = keep(store, "calendar-facts", {"schema": "event-calendar-facts-v1", "events": values})
    root = keep(store, "calendar-root", {"schema": "event-calendar-v1", "scope_note": "Synthetic selected scope only",
            "inputs": [{"input_id": "release", "format": "event-facts", "reference": asdict(packet)}],
            "followups": followups or []})
    return {"reference": asdict(root), "as_of": AT}, evidence, packet


def test_exact_read_fresh_reader_keeps_source_dates_and_does_not_write(store):
    plan, _, _ = prepare(store)
    before = store.backend.head()
    result = read_calendar(store, plan)
    fresh = VersionedStore(GitStore(store.backend.root))
    assert read_calendar(fresh, plan) == result
    assert result["scope"]["distinct_read_events"] == 1
    assert result["events"][0]["state"] == "window-open"
    assert result["events"][0]["current"]["actual_at"] == {"presence": "null", "value": None}
    assert result["source_requests"] == result["writes"] == 0
    assert result["research_executed"] is False and store.backend.head() == before


@pytest.mark.parametrize("at,expected", [
    ("2026-10-19T23:59:59Z", "upcoming"), ("2026-10-20T00:00:00Z", "window-open"),
    ("2026-10-21T00:00:00Z", "due-no-actual-evidence")])
def test_calendar_day_is_not_an_invented_midnight_instant(store, at, expected):
    plan, _, _ = prepare(store)
    assert read_calendar(store, {**plan, "as_of": at})["events"][0]["state"] == expected


def test_filters_preserve_original_scope_failures_and_view_identity(store):
    plan, _, _ = prepare(store)
    whole = read_calendar(store, plan)
    filtered = read_calendar(store, {**plan, "subject_id": "not-in-the-sources"})
    assert filtered["events"] == []
    assert filtered["scope"] == whole["scope"] and filtered["view_version"] == whole["view_version"]


def test_same_day_conflicting_dates_are_not_ordered_by_input_or_capture(store):
    evidence = keep(store, "evidence", "Synthetic 2026-09 release scheduled 2026-10-20.")
    rows = [project_fact(fact_value(evidence)), project_fact(fact_value(evidence, scheduled_for="2026-10-22"))]
    value = timeline(rows, AT)
    assert value["state"] == "version-ordering-unknown" and value["current"] is None
    assert value["version_count"] == 2
    assert value["transitions"][0]["status"] == "ordering-unknown"


def test_consecutive_source_days_support_reschedule_without_rewriting_before(store):
    evidence = keep(store, "evidence", "Synthetic 2026-09 release scheduled 2026-10-20.")
    rows = [project_fact(fact_value(evidence)), project_fact(fact_value(evidence, announced_at="2026-10-02", scheduled_for="2026-10-22"))]
    value = timeline(rows, AT)
    assert value["current"]["scheduled_for"]["value"] == "2026-10-22"
    assert value["versions"][0]["scheduled_for"]["value"] == "2026-10-20"
    assert value["transitions"][0]["status"] == "schedule-changed"


@pytest.mark.parametrize("update,expected", [
    ({"status": "cancelled"}, "source-reported-cancelled"),
    ({"status": "occurred", "actual_at": "2026-10-18"}, "source-reported-occurred"),
    ({"status": "occurred", "actual_at": "2026-10-21"}, "temporal-uncertainty"),
    ({"status": "occurred", "actual_at": "2026-10-20"}, "temporal-uncertainty"),
    ({"scheduled_for": None}, "date-unknown"),
    ({"announced_at": "2026-10-21"}, "temporal-uncertainty")])
def test_source_state_and_temporal_limitations_are_distinct(store, update, expected):
    evidence = keep(store, "evidence", "Synthetic 2026-09 release scheduled 2026-10-20.")
    result = timeline([project_fact(fact_value(evidence, **update))], AT)
    assert result["state"] == expected
    assert result["material_status"] != "read"


def test_existing_relay_excerpt_preserves_missing_modify_and_unparsed_revision(store):
    fields = ["ts_code", "ann_date", "end_date", "pre_date", "actual_date", "modify_date"]
    raw = {"code": 0, "count": 2, "data": {"fields": fields, "items": [
        ["002384.SZ", "20261001", "20260930", "20261020", None, None],
        ["002384.SZ", "20261002", "20260930", "20261022", None, "two appointments in source record"]]}}
    snap = normalize_disclosure_dates(ts_code="002384.SZ", end_date="20260930", raw=raw,
                                      captured_at="2026-10-19T09:00:00Z", source_mode="offline-unverified")
    original = store.put_source("relay-calendar", source_for(snap))
    root = keep(store, "relay-root", {"schema": "event-calendar-v1", "scope_note": "Synthetic selected Relay schedule",
        "inputs": [{"input_id": "disclosure", "format": "relay-disclosure-date", "reference": asdict(original)}], "followups": []})
    view = read_calendar(store, {"reference": asdict(root), "as_of": AT})
    event = view["events"][0]
    assert event["current"]["scheduled_for"]["value"] == "2026-10-22"
    assert event["current"]["revision_record"]["interpretation"] == "unparsed-revision-record"
    assert event["versions"][0]["revision_record"]["presence"] == "null"
    assert "not-issuer-pdf" in event["current"]["date_interpretation"]
    assert event["current"]["source_row_hash"] == snap.rows[1].row_hash


def test_bad_quote_and_missing_input_are_unknown_not_empty_calendar(store, monkeypatch):
    plan, _, packet = prepare(store)
    original = store.read_source
    def broken(ref):
        if ref == packet:
            raise MissingObject("missing")
        return original(ref)
    monkeypatch.setattr(store, "read_source", broken)
    view = read_calendar(store, plan)
    assert view["events"] == [] and view["status"] == "partial"
    assert view["scope"]["unavailable_inputs"] == 1
    assert view["scope"]["unknown_event_count_in_unavailable_inputs"] is None


def test_permission_failure_stops_before_partial_output(store, monkeypatch):
    plan, _, packet = prepare(store)
    original = store.read_source
    def denied(ref):
        if ref == packet:
            raise StoreUnavailable("denied")
        return original(ref)
    monkeypatch.setattr(store, "read_source", denied)
    with pytest.raises(StoreUnavailable):
        read_calendar(store, plan)


def test_followup_opens_exact_existing_research_without_new_approval_or_write(store):
    research = store.submit_revision("calendar-research", RevisionInput("calendar-case", "r1", "quick",
        "Await the synthetic September release before reviewing the mechanism.", "Synthetic original mandate", "synthetic", unresolved=("Outcome unknown",)))
    plan, _, _ = prepare(store, followups=[{"event_id": "synthetic-macro-release", "reference": asdict(research),
        "question_id": None, "quote": "Await the synthetic September release before reviewing the mechanism."}])
    before = store.backend.head(); view = read_calendar(store, plan)
    link = view["events"][0]["followups"][0]
    assert link["status"] == "readable" and link["open_request"]["payload"]["reference"] == asdict(research)
    assert link["recorded_unresolved"] == ["Outcome unknown"] and link["research_review"] == "not-performed"
    assert store.backend.head() == before


def test_room_permission_literal_page_and_original_model_roundtrip(store):
    plan, _, _ = prepare(store)
    room_plan = {"as_of": AT, "event_calendar": plan}
    with pytest.raises(RecordError, match="PUBLIC_CALENDAR"):
        ReadingRoom(store).build(room_plan, industry_export=False)
    with pytest.raises(RecordError, match="PUBLIC_CALENDAR"):
        reading_commands.preflight("reading-room", {"plan": room_plan, "public_research_custody_confirmed": True})
    reading_commands.preflight("reading-room", {"plan": room_plan}, public=False)
    room = ReadingRoom(store).build(room_plan)
    assert room["event_calendar"]["status"] == "readable"
    room["event_calendar"]["view"]["events"][0]["title"] = "</script><img onerror=alert(1)>"
    html = render_page(room)
    assert "</script><img" not in html and 'id="event-calendar-content"' in html
    assert json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)) == room
    plain = ReadingRoom(store).build({"as_of": AT})
    assert "event_calendar" not in plain and 'id="event-calendar-content"' not in render_page(plain)


@pytest.mark.parametrize("change", [{"as_of": "2026-10-20"}, {"subject_id": "invalid space"},
    {"url": "https://example.invalid"}, {"window": {"start": "2026-11-01", "end": "2026-10-01"}}])
def test_invalid_plan_fails_before_source_access(store, change, monkeypatch):
    plan, _, _ = prepare(store)
    monkeypatch.setattr(store, "read_source", lambda _: pytest.fail("source read before validation"))
    with pytest.raises(RecordError):
        read_calendar(store, {**plan, **change})


@pytest.mark.parametrize("second_announcement,state", [
    ("2026-10-02", "date-unknown"), ("2026-10-01", "version-ordering-unknown")])
def test_unknown_current_date_survives_window_even_when_old_date_is_outside(store, second_announcement, state):
    evidence = keep(store, "evidence", "Synthetic 2026-09 release scheduled 2026-10-20.")
    plan, _, _ = prepare(store, facts=[fact_value(evidence),
        fact_value(evidence, announced_at=second_announcement, scheduled_for=None)])
    whole = read_calendar(store, plan)
    filtered = read_calendar(store, {**plan, "window": {"start": "2026-11-01", "end": "2026-11-30"}})
    assert whole["events"][0]["state"] == state
    assert filtered["events"] == whole["events"]
    assert filtered["scope"] == whole["scope"] and filtered["view_version"] == whole["view_version"]


@pytest.mark.parametrize("error", [RecordError("PRIVATE_BINDING_MISMATCH"),
    RecordError("PUBLIC_BINDING_MISMATCH"), RecordError("PRIVATE_UPGRADE_REVERSED"),
    RecordError("OPERATING_SOURCE_SCOPE_BOUND"), StoreUnavailable("REMOTE_REQUEST_LIMIT")])
def test_operation_failures_stop_sibling_reads_and_escape_room(store, monkeypatch, error):
    plan, _, packet = prepare(store)
    second = keep(store, "second-facts", {"schema": "event-calendar-facts-v1", "events": []})
    from decision_kernel.domain.records import SourceRef
    root = json.loads(store.read_source(SourceRef.from_dict(plan["reference"]))["data"])
    root["inputs"].append({"input_id": "second", "format": "event-facts", "reference": asdict(second)})
    plan["reference"] = asdict(keep(store, "two-inputs", root))
    original, calls = store.read_source, []
    def denied(reference):
        calls.append(reference)
        if reference == packet:
            raise error
        return original(reference)
    monkeypatch.setattr(store, "read_source", denied)
    with pytest.raises(type(error), match=str(error)):
        read_calendar(store, plan)
    with pytest.raises(type(error), match=str(error)):
        ReadingRoom(store).build({"as_of": AT, "event_calendar": plan})
    assert second not in calls


def test_scope_exit_failure_is_not_a_partial_room(store, monkeypatch):
    from contextlib import contextmanager
    from decision_kernel.application import event_calendar as app
    plan, _, _ = prepare(store)
    @contextmanager
    def closed_scope(original):
        yield original
        raise RecordError("PRIVATE_UPGRADE_REVERSED")
    monkeypatch.setattr(app, "industry_source_scope", closed_scope)
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        ReadingRoom(store).build({"as_of": AT, "event_calendar": plan})


def research_for(store, case_id="calendar-budget"):
    quote = "Await the synthetic September release."
    revision = store.submit_revision(case_id, RevisionInput(case_id, "r1", "quick",
        quote + "\n" + "Retained original analysis. " * 1000, "Synthetic original mandate", "synthetic",
        unresolved=("Outcome unknown",)))
    return revision, quote


@pytest.mark.parametrize("room_for_second_read", [False, True])
def test_research_reuses_exact_ref_cache_and_shares_total_read_budget(store, monkeypatch, room_for_second_read):
    from decision_kernel.application import event_calendar as app
    from decision_kernel.domain.records import SourceRef
    revision, quote = research_for(store)
    evidence = keep(store, "evidence", "Synthetic 2026-09 release scheduled 2026-10-20.")
    events = [fact_value(evidence), fact_value(evidence, event_id="second-release")]
    links = [{"event_id": event["event_id"], "reference": asdict(revision), "question_id": None, "quote": quote}
             for event in events]
    plan, _, packet = prepare(store, facts=events, followups=links)
    source_bytes = sum(len(encoded(store.read_source(ref))) for ref in
                       (SourceRef.from_dict(plan["reference"]), packet, evidence))
    original = store.read_revision
    research_bytes = len(encoded(original(revision)))
    calls = []
    def counted(reference):
        calls.append(reference)
        return original(reference)
    monkeypatch.setattr(store, "read_revision", counted)
    monkeypatch.setattr(app, "MAX_READ_BYTES", source_bytes + research_bytes)
    result = read_calendar(store, plan)
    assert result["scope"]["read_followups"] == 2 and calls == [revision]
    calls.clear()
    monkeypatch.setattr(app, "MAX_READ_BYTES", source_bytes + research_bytes - 1)
    with pytest.raises(RecordError, match="CALENDAR_READ_BOUND"):
        if room_for_second_read:
            ReadingRoom(store).build({"as_of": AT, "event_calendar": plan})
        else:
            read_calendar(store, plan)
    if not room_for_second_read:
        assert calls == [revision]  # The second followup is never attempted after the cap.


@pytest.mark.parametrize("error", [RecordError("PRIVATE_BINDING_MISMATCH"),
    RecordError("PRIVATE_UPGRADE_REVERSED"), StoreUnavailable("REMOTE_REQUEST_LIMIT")])
def test_research_access_failure_stops_later_followups(store, monkeypatch, error):
    first, quote = research_for(store, "research-first")
    second, _ = research_for(store, "research-second")
    plan, _, _ = prepare(store, followups=[
        {"event_id": "synthetic-macro-release", "reference": asdict(ref), "question_id": None, "quote": quote}
        for ref in (first, second)])
    original, calls = store.read_revision, []
    def denied(reference):
        calls.append(reference)
        if reference == first:
            raise error
        return original(reference)
    monkeypatch.setattr(store, "read_revision", denied)
    with pytest.raises(type(error), match=str(error)):
        read_calendar(store, plan)
    assert calls == [first]


@pytest.mark.parametrize("acquired,actual,issue", [
    ("2026-10-19T09:00:00Z", "2026-10-19T15:00:00Z", "ACTUAL_EVENT_AFTER_SOURCE_ACQUISITION"),
    ("2026-10-19T09:00:00Z", "2026-10-19", "ACTUAL_EVENT_TIME_RELATIVE_TO_SOURCE_ACQUISITION_UNKNOWN"),
    ("2026-10-19", "2026-10-19T15:00:00Z", "ACTUAL_EVENT_TIME_RELATIVE_TO_SOURCE_ACQUISITION_UNKNOWN"),
    (None, "2026-10-19", "ACTUAL_EVENT_SOURCE_ACQUISITION_NOT_RETAINED"),
    ("2026-10-20T00:00:00Z", "2026-10-19", None)])
def test_original_evidence_clock_cannot_be_replaced_by_later_extraction(store, acquired, actual, issue):
    quote = "Synthetic source reports actual occurrence on " + actual + "."
    original = keep(store, "earlier-original", quote, acquired_at=acquired)
    value = fact_value(original, status="occurred", actual_at=actual)
    value["evidence"] = [{"reference": asdict(original), "quote": quote}]
    facts = keep(store, "later-extraction", {"schema": "event-calendar-facts-v1", "events": [value]},
                 acquired_at="2026-10-20T09:00:00Z")
    root = keep(store, "later-calendar", {"schema": "event-calendar-v1", "scope_note": "Synthetic original timing",
        "inputs": [{"input_id": "release", "format": "event-facts", "reference": asdict(facts)}],
        "followups": []}, acquired_at="2026-10-20T09:00:00Z")
    event = read_calendar(store, {"reference": asdict(root), "as_of": AT})["events"][0]
    assert event["current"]["evidence"][0]["source_acquired_at"] == acquired
    if issue:
        assert event["state"] == "temporal-uncertainty" and issue in event["issues"]
        assert issue in event["current"]["evidence"][0]["temporal_issues"]
    else:
        assert event["state"] == "source-reported-occurred" and event["issues"] == []


@pytest.mark.parametrize("acquired,uncertain", [
    ("2026-03-09T03:30:00Z", True), ("2026-03-09T04:00:00Z", False)])
def test_source_day_acquisition_boundary_obeys_declared_timezone_dst(store, acquired, uncertain):
    original = keep(store, "dst-source", "Synthetic daily source date.")
    row = project_fact(fact_value(original, timezone="America/New_York", announced_at=None,
                                  status="occurred", actual_at="2026-03-08"))
    issues = acquisition_issues(row, acquired)
    assert ("ACTUAL_EVENT_TIME_RELATIVE_TO_SOURCE_ACQUISITION_UNKNOWN" in issues) == uncertain


def test_calendar_source_navigation_requests_match_existing_host_receiver(store):
    from decision_kernel.domain.records import SourceRef
    from decision_kernel.interfaces.research_runtime import _perform
    plan, original, facts = prepare(store)
    event = read_calendar(store, plan)["events"][0]
    row = event["current"]
    requests = [row["open_request"], row["evidence"][0]["open_request"]]
    assert [SourceRef.from_dict(request["payload"]) for request in requests] == [facts, original]
    for request in requests:
        result = _perform(store, request)
        assert encoded(result["reference"]) == encoded(request["payload"])
