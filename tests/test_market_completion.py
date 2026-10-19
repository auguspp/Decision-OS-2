"""Explicit same-day qualification without changing retained legacy contracts."""
from copy import deepcopy

import pytest

from decision_kernel.application.ftshare_collection import (
    _capture_completion, begin_ftshare_slot, finish_ftshare_slot, restore_ftshare_collection,
    retain_ftshare_single,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_completion import EOD_COMPLETION_POLICY, completed_close_clock
from decision_kernel.domain.records import RecordError, encoded
from test_ftshare_collection import (
    _new_calendars, _retain, plan_single, settle_single, single_capture, single_collection_fixture,
)
from test_market_relay_inputs import REGISTRY


def test_1835_boundary_is_explicit_and_legacy_remains_next_day():
    with pytest.raises(RecordError, match="FTSHARE_COMPLETION_BEFORE_END"):
        completed_close_clock("2026-10-19T10:34:59Z", "2026-10-19", EOD_COMPLETION_POLICY)
    assert completed_close_clock("2026-10-19T10:35:00Z", "2026-10-19", EOD_COMPLETION_POLICY)[1] == "2026-10-19"
    assert completed_close_clock("2026-10-19T18:35:00+08:00", "2026-10-19", EOD_COMPLETION_POLICY)[1] == "2026-10-19"
    with pytest.raises(ValueError, match="COLLECTION_READ_AS_OF_BEFORE_END"):
        completed_close_clock("2026-10-19T23:59:59+08:00", "2026-10-19")
    assert completed_close_clock("2026-10-20T00:00:00+08:00", "2026-10-19")[1] == "2026-10-19"
    assert completed_close_clock(None, "2026-10-19", EOD_COMPLETION_POLICY) == (None, None)
    with pytest.raises(RecordError, match="FTSHARE_COMPLETION_CLOCK"):
        completed_close_clock("2026-10-19T18:35:00", "2026-10-19", EOD_COMPLETION_POLICY)


def test_explicit_plan_restores_policy_and_old_exact_plan_remains_unchanged():
    f = single_collection_fixture()
    initial = _retain(f["single"], f["store"], f["fresh"])
    finished, base = settle_single(f, f["single"], initial)
    old = encoded(restore_ftshare_collection(base["reference"], REGISTRY, f["fresh"]())["collection"])
    calendars = _new_calendars(f["store"], f["fresh"], through="20261019")
    args = dict(base=base["reference"], generation="g20261019-closed", end="20261019",
        created_at="2026-10-19T12:03:00+00:00", calendar_references=calendars,
        targets=[{"symbol": "000001.SZ", "adjust_kind": "none"}])
    with pytest.raises(ValueError, match="COLLECTION_READ_AS_OF_BEFORE_END"):
        plan_single(f, **args)
    current = plan_single(f, **args, completion_policy=EOD_COMPLETION_POLICY)
    initial = _retain(current, f["store"], f["fresh"])
    _, final = settle_single(f, current, initial)
    restored = restore_ftshare_collection(final["reference"], REGISTRY, f["fresh"]())
    assert restored["collection"]["plan"]["completion_policy"] == EOD_COMPLETION_POLICY
    read = read_ftshare_collection_version(final["reference"], REGISTRY, f["fresh"](),
        read_as_of="2026-10-19T12:10:00+00:00")
    assert read["view"]["freshness"]["completed_day_through"] == "2026-10-19"
    assert read["view"]["freshness"]["completion_policy"] == EOD_COMPLETION_POLICY
    assert encoded(restore_ftshare_collection(base["reference"], REGISTRY, f["fresh"]())["collection"]) == old
    assert "completion_policy" not in finished["plan"]


def test_early_acquisition_never_becomes_qualified_when_reader_clock_moves_forward():
    f = single_collection_fixture()
    calendars = _new_calendars(f["store"], f["fresh"], through="20261019")
    current = plan_single(f, generation="g20261019-early", end="20261019",
        created_at="2026-10-19T12:03:00+00:00", calendar_references=calendars,
        completion_policy=EOD_COMPLETION_POLICY)
    initial = _retain(current, f["store"], f["fresh"])
    slot = current["plan"]["new_slots"][0]
    started = begin_ftshare_slot(current, slot, at="2026-10-19T12:03:00+00:00")
    reserved = _retain(started, f["store"], f["fresh"], initial["reference"])
    early = single_capture(started, slot, acquired_at="2026-10-19T10:34:59+00:00")
    raw = retain_ftshare_single(early, f["store"], fresh_reader=f["fresh"], custody_authorized=True)
    candidate = finish_ftshare_slot(started, slot, at="2026-10-19T12:10:00+00:00", reference=raw["reference"])
    with pytest.raises(RecordError, match="FTSHARE_COMPLETION_BEFORE_END"):
        _retain(candidate, f["store"], f["fresh"], reserved["reference"])
    with pytest.raises(RecordError, match="FTSHARE_COMPLETION_BEFORE_END"):
        _capture_completion(early, EOD_COMPLETION_POLICY)
    later_clock_does_not_change_original = deepcopy(early)
    _capture_completion(later_clock_does_not_change_original, None)  # Legacy interpretation is unchanged.
