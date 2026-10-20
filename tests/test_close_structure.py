"""Finite observations and exact source qualification; fixtures are synthetic."""
from datetime import date, timedelta
from decimal import Inexact, localcontext

import pytest

from decision_kernel.application.close_structure import CloseStructure, plan_input
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.observations.close_structure import observe_close_structure
from decision_kernel.observations.market_daily import MarketInputError
from test_market_discovery import AS_OF, DAYS, REGISTRY, retain, storage


def observe(values, *, missing=(), windows=(3,), turn_sessions=5, **changes):
    days = [(date(2026, 1, 1) + timedelta(days=n)).isoformat() for n in range(len(values))]
    args = {"entity_id": "synthetic", "series": [{"trade_date": day, "close": value}
        for day, value in zip(days, values) if day not in missing], "as_of": days[-1],
        "market_sessions": days, "calendar_version": "synthetic-calendar", "adjustment": "raw",
        "source_version": "synthetic-close", "market": "synthetic", "currency": "XXX",
        "windows": windows, "turn_sessions": turn_sessions}
    return observe_close_structure(**{**args, **changes})


def plan(reference, **changes):
    return {"schema": "close-structure-plan-v1", "kind": "sector", "reference": reference,
            "as_of": AS_OF, "windows": [3, 5], "turn_sessions": 5, **changes}


@pytest.mark.parametrize("last,position", [(14, "above-range"), (9, "below-range"),
    (13, "at-high"), (10, "at-low"), (12, "inside-range")])
def test_final_close_is_excluded_from_original_N_session_range(last, position):
    result = observe([10, 13, 11, last])
    row = result["ranges"][0]
    assert row["prior_low"] == "10" and row["prior_high"] == "13"
    assert row["end_close"] == str(last) and row["position"] == position
    assert row["prior_start"] == "2026-01-01" and row["prior_end"] == "2026-01-03"
    assert observe([10] * 4)["ranges"][0]["position"] == "at-flat-range"


def test_missing_session_does_not_shift_ranges_or_join_nonadjacent_turns():
    result = observe([10, 15, 11, 16, 12, 17], missing=["2026-01-02"],
                     windows=(2, 5), turn_sessions=6)
    short, long = result["ranges"]
    assert short["status"] == "observed" and short["prior_low"] == "12"
    assert long["status"] == "missing-market-sessions" and long["missing_dates"] == ["2026-01-02"]
    turns = result["turns"]
    assert turns["possible_triples"] == 4 and turns["observed_triples"] == turns["missing_triples"] == 2
    assert [x["turn_session"] for x in turns["confirmed"]] == ["2026-01-04", "2026-01-05"]


def test_confirmation_requires_right_close_and_old_prefix_is_unchanged():
    before = observe([10, 15], windows=(1,), turn_sessions=6)
    assert before["turns"]["confirmed"] == []
    assert before["turns"]["candidate"]["status"] == "unconfirmed-no-right-close"
    assert before["turns"]["candidate"]["confirmed_session"] is None
    first = observe([10, 15, 12], windows=(1,), turn_sessions=6)
    original = encoded(first)
    later = observe([10, 15, 12, 17, 14], windows=(1,), turn_sessions=6)
    assert later["turns"]["confirmed"][0] == first["turns"]["confirmed"][0]
    assert first["turns"]["confirmed"][0]["turn_session"] == "2026-01-02"
    assert first["turns"]["confirmed"][0]["confirmed_session"] == "2026-01-03"
    assert encoded(first) == original
    assert observe([10, 15, 15, 12])["turns"]["confirmed"] == []


def test_future_rows_are_unused_but_validated_and_decimal_context_is_local():
    args = {"as_of": "2026-01-04", "windows": (3,), "turn_sessions": 4}
    normal = observe([7, 11, 13, 17, 1000], **args)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        assert observe([7, 11, 13, 17, 1000], **args) == normal
    assert normal["ranges"][0]["end_close"] == "17"
    assert normal["turns"]["candidate"]["turn_session"] == "2026-01-04"
    duplicate = [{"trade_date": "2026-01-05", "close": 10}] * 2
    with pytest.raises(MarketInputError, match="DUPLICATE_SESSION"):
        observe([7, 11, 13, 17, 1000], series=duplicate, **args)


def test_short_window_uses_qualified_original_prices_despite_old_path_history_reason(storage):
    ref = retain(storage, [10, 11, 12, 13, 14, 15])
    head = storage[0].backend.head()
    first = CloseStructure(storage[0], REGISTRY).read(plan(ref, limit=1))
    row = first["view"]["rows"][0]
    assert row["source_qualification"] == {"status": "eligible", "reason_code": None,
        "original_path_reason": "DISCOVERY_INSUFFICIENT_CALENDAR_HISTORY"}
    assert all(x["status"] == "observed" for x in row["observation"]["ranges"])
    scope = first["view"]["scope"]
    assert scope["scope_count"] == 2 and scope["source_eligible_count"] == scope["complete_count"] == 1
    assert scope["incomplete_count"] == 1 and first["partial"] is True
    second = CloseStructure(storage[1](), REGISTRY).read(first["next_plan"])
    assert second["view"]["scope"] == scope
    assert second["view"]["view_sha256"] == first["view"]["view_sha256"]
    assert second["view"]["rows"][0]["status"] == "source-unavailable"
    selected = CloseStructure(storage[1](), REGISTRY).read(plan(ref, entity_id=row["entity_id"]))
    assert selected["view"]["scope"] == scope and selected["partial"] is True
    assert storage[0].backend.head() == head
    assert first["view"]["source_requests"] == first["view"]["writes"] == 0
    assert first["open_request"]["payload"] == ref  # Original host's read-source payload is the bare SourceRef.


def test_gap_outside_short_window_only_limits_the_affected_range(storage):
    ref = retain(storage, list(range(10, 41)), missing=[DAYS[0]])
    view = CloseStructure(storage[0], REGISTRY).read(plan(ref, windows=[3, 30]))["view"]
    short, long = view["rows"][0]["observation"]["ranges"]
    assert short["status"] == "observed" and long["status"] == "missing-market-sessions"
    assert view["scope"]["window_status_counts"]["3"] == {"observed": 1, "source-unavailable": 1}


@pytest.mark.parametrize("change,expected", [
    ({"values": [10, 11, 12, 13, 14, 16]}, "historical-prices-changed"),
    ({"values": [10, 11, 12, 13, 14, 15, 16]}, "consecutive-session"),
    ({"values": [10, 11, 12, 13, 14, 15, 16, 17]}, "unobserved-session-gap"),
    ({"values": [10, 11, 12, 13, 14, 15], "name": "changed identity"}, "contract-changed"),
])
def test_exact_previous_source_exposes_restatement_without_rewriting_old_ranges(storage, change, expected):
    previous = retain(storage, [10, 11, 12, 13, 14, 15])
    service = CloseStructure(storage[0], REGISTRY)
    before = service.read(plan(previous))
    original = encoded(before)
    change = dict(change)
    now = retain(storage, change.pop("values"), **change)
    current = service.read(plan(now, previous={"reference": previous, "as_of": AS_OF}))
    assert current["view"]["rows"][0]["history"]["status"] == expected
    assert service.read(plan(previous)) == before and encoded(before) == original
    assert current["view"]["previous"]["source_reference"] == previous
    assert current["view"]["rows"][0]["observation"] is not None


def test_wrong_source_family_does_not_become_empty_market_or_fallback(storage):
    ref = retain(storage, [10, 11, 12, 13, 14, 15], kind="concept")
    result = CloseStructure(storage[0], REGISTRY).read(plan(ref))
    assert result["view"]["status"] == "unavailable" and result["view"]["scope"]["scope_count"] is None
    assert result["view"]["scope"]["complete_count"] is None and result["view"]["rows"] == []
    assert result["view"]["scope"]["reason_code"] == "DISCOVERY_THS_ROOT_REQUIRED"
    assert result["partial"] is True
    concept = CloseStructure(storage[0], REGISTRY).read(plan(ref, kind="concept"))
    assert concept["view"]["scope"]["source_eligible_count"] == 1


def test_unavailable_current_or_previous_scope_cannot_claim_departures_or_zero_query(storage):
    good = retain(storage, [10, 11, 12, 13, 14, 15])
    bad = retain(storage, [10, 11, 12, 13, 14, 15], kind="concept")
    service = CloseStructure(storage[0], REGISTRY)
    lost = service.read(plan(bad, previous={"reference": good, "as_of": AS_OF}))["view"]
    assert lost["page"]["selected_count"] is None
    assert lost["previous"]["departed_scope_count"] is None
    prior_unknown = service.read(plan(good, previous={"reference": bad, "as_of": AS_OF}))["view"]
    assert prior_unknown["previous"]["scope_count"] is None
    assert prior_unknown["previous"]["departed_scope_count"] is None
    assert prior_unknown["rows"][0]["history"]["status"] == "source-unavailable"


def test_previous_stock_defaults_to_current_raw_convention(storage):
    ref = retain(storage, [10, 11, 12, 13, 14, 15])
    checked = plan_input(plan(ref, kind="stock", adjustment="raw",
                              previous={"reference": ref, "as_of": AS_OF}))
    assert checked["previous"]["adjustment"] == "raw"


@pytest.mark.parametrize("change", [
    {"windows": [True]}, {"windows": [251]}, {"windows": [3, 3]}, {"turn_sessions": 65},
    {"limit": 0}, {"offset": -1}, {"entity_id": " 600000.SH"}, {"kind": "minute"},
])
def test_invalid_finite_plan_is_rejected_before_source_access(storage, change):
    ref = retain(storage, [10, 11, 12, 13, 14, 15])
    with pytest.raises(RecordError):
        plan_input(plan(ref, **change))


def test_real_retained_ftshare_shape_keeps_master_quarantine_and_original_source_proof():
    from test_ftshare_collection import adopt_kernel, kernel_adoption_fixture
    fixture = kernel_adoption_fixture()
    retained = adopt_kernel(fixture)
    before = fixture["store"].backend.head()
    value = plan(retained["reference"], kind="stock", adjustment="raw", windows=[1], turn_sessions=3)
    result = CloseStructure(fixture["store"], REGISTRY).read(value)
    view = result["view"]
    assert view["scope"]["scope_count"] == len(fixture["context"]["scope_symbols"])
    eligible = [r for r in view["rows"] if r["source_qualification"]["status"] == "eligible"]
    assert {r["entity_id"] for r in eligible} == {"000001.SZ", "600000.SH"}
    assert all(r["source_provenance"][0]["wire_sha256"] for r in eligible)
    assert any(r["source_qualification"]["reason_code"] == "DISCOVERY_REFERENCE_QUARANTINED" for r in view["rows"])
    assert fixture["store"].backend.head() == before


def test_retained_source_hold_cannot_qualify_inherited_diagnostic_prices():
    from test_ftshare_collection import _retain, settle_single, single_collection_fixture
    from decision_kernel.application.market_discovery import MarketDiscovery
    f = single_collection_fixture()
    genesis = _retain(f["single"], f["store"], f["fresh"])
    _, retained = settle_single(f, f["single"], genesis,
        reason_code="FTSHARE_HTTP_429", source_hold="FTSHARE_HTTP_429")
    value = plan(retained["reference"], kind="stock", adjustment="raw", windows=[1], turn_sessions=3)
    original = MarketDiscovery(f["store"], REGISTRY)
    _, core = original._core("stock", {"reference": retained["reference"], "adjustment": "raw"}, AS_OF)
    assert core["source_hold"] and core["entities"]["600000.SH"]["prices"]  # Core retains diagnostics.
    result = CloseStructure(f["store"], REGISTRY).read(value)["view"]
    assert result["scope"]["source_eligible_count"] == result["scope"]["complete_count"] == 0
    assert all(row["observation"] is None for row in result["rows"])
    row = next(r for r in result["rows"] if r["entity_id"] == "600000.SH")
    assert row["source_qualification"]["reason_code"] == "DISCOVERY_SOURCE_HOLD"


def test_retained_capture_after_reading_clock_does_not_release_its_prices():
    from test_ftshare_collection import (_retain, begin_ftshare_slot, finish_ftshare_slot,
        retain_ftshare_single, single_capture, single_collection_fixture)
    f = single_collection_fixture()
    genesis = _retain(f["single"], f["store"], f["fresh"])
    slot = f["single"]["plan"]["new_slots"][0]
    started = begin_ftshare_slot(f["single"], slot, at=f["single"]["updated_at"])
    reserved = _retain(started, f["store"], f["fresh"], genesis["reference"])
    captured = single_capture(started, slot, acquired_at="2026-10-19T12:06:00+00:00")
    held = retain_ftshare_single(captured, f["store"], fresh_reader=f["fresh"], custody_authorized=True)
    finished = finish_ftshare_slot(started, slot, at="2026-10-19T12:06:00+00:00", reference=held["reference"])
    root = _retain(finished, f["store"], f["fresh"], reserved["reference"])
    value = plan(root["reference"], kind="stock", adjustment="forward", windows=[1], turn_sessions=3)
    result = CloseStructure(f["store"], REGISTRY).read(value)["view"]
    row = next(r for r in result["rows"] if r["entity_id"] == "600000.SH")
    assert row["observation"] is None
    assert row["source_qualification"]["reason_code"] == "DISCOVERY_SOURCE_AFTER_OBSERVATION"
    peer = next(r for r in result["rows"] if r["entity_id"] == "000001.SZ")
    assert peer["source_qualification"]["status"] == "eligible" and peer["observation"] is not None
