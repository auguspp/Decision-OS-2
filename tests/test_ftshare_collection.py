"""Real Store source chains with synthetic providers; no live traffic or rights claims."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
import subprocess
import sys
import zipfile

import pytest

from decision_kernel.application.ftshare_collection import (
    SINGLE_PLAN_SCHEMA,
    begin_ftshare_slot, finish_ftshare_slot, latest_ftshare_reference,
    plan_ftshare_collection, plan_ftshare_single_collection, restore_ftshare_batch, restore_ftshare_collection,
    retain_ftshare_batch, retain_ftshare_collection, retain_ftshare_single, validate_ftshare_collection,
)
from decision_kernel.application.ftshare_daily import capture_batch
from decision_kernel.application.market_collection import plan_collection
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_custody import retain_market_query
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, encoded
from test_market_collection_inputs import CollectionOpener
from test_market_collection_sync import memory, run
from test_market_relay_inputs import REGISTRY, ROOT_DIR
from test_market_session_coverage import capture as relay_capture

STAMP = "2026-10-18T12:00:00+00:00"
KERNEL_STAMP = "2026-10-19T12:02:00+00:00"


def _setup(*, count=2, quarantine=False, storage=None, quarantine_last=False):
    backend, store, fresh = memory() if storage is None else storage
    original = plan_collection(collection_id="cn-eod", generation="g20261016",
        start="20260701", end="20261016", calendar_end="20261016", registry=REGISTRY)
    opener = CollectionOpener(count=count)
    # A synthetic 72-session, 220-slot legacy inventory, as in the existing tests.
    for venue in ("SSE", "SZSE"):
        for row in opener.rows["trade_cal", venue]:
            if row["cal_date"] in {"20261001", "20261002", "20261005", "20261006"}:
                row["is_open"] = 0
        if quarantine:
            opener.rows["stock_basic", venue][0]["name"] = ""
        if quarantine_last:
            opener.rows["stock_basic", venue][-1]["name"] = ""
    prior = run(original, store, fresh, opener, max_queries=5)
    source = restore_market_collection(prior["reference"], REGISTRY, fresh())["collection"]
    assert len(source["units"]) == 220
    assert source["units"]["20261016:daily"]["status"] == "complete"
    current = plan_ftshare_collection(prior["reference"], REGISTRY, fresh(),
        collection_id="ftshare-cn-eod", generation="g20261016-ftshare-v1", created_at=STAMP)
    return backend, store, fresh, prior["reference"], source, current


def _millis(day, clock):
    stamp = datetime.fromisoformat(day + "T" + clock + "+08:00")
    return (stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


def _capture(collection, slot):
    plan = collection["plan"]
    params = plan["requests"][slot]["parameters"]
    data = []
    for code in params["symbols"]:
        venue = "SSE" if code.endswith(".SH") else "SZSE"
        rows = []
        for day in plan["sessions"][venue]:
            rows.append({"open": "10.00", "high": "10.50", "low": "9.50", "close": "10.00",
                "volume": 1234, "turnover": "12340.00",
                "ts_millis_open": _millis(day, "09:30:00.000"),
                "ts_millis": _millis(day, "15:00:00.000")})
        data.append([code, rows])
    raw = encoded({"code": 200, "message": "success", "data": data})
    return capture_batch(raw, symbols=params["symbols"], since=params["since"], until=params["until"],
                         adjust_kind=params["adjust_kind"], acquired_at=STAMP)


def _retain(collection, store, fresh, previous=None):
    return retain_ftshare_collection(collection, REGISTRY, store, fresh_reader=fresh,
                                    predecessor=previous, custody_authorized=True)


def _settle(collection, slot, reference, store, fresh):
    started = begin_ftshare_slot(collection, slot, at=STAMP)
    reserved = _retain(started, store, fresh, reference)
    captured = _capture(started, slot)
    raw = retain_ftshare_batch(captured, store, fresh_reader=fresh, custody_authorized=True)
    finished = finish_ftshare_slot(started, slot, at=STAMP, reference=raw["reference"])
    return finished, _retain(finished, store, fresh, reserved["reference"]), raw


def _new_calendars(store, fresh, *, through="20261017", changed_history=False,
                   start="20260701", changed_day=None):
    opener = CollectionOpener(start=start, through=through)
    refs = {}
    for venue in ("SSE", "SZSE"):
        for row in opener.rows["trade_cal", venue]:
            if row["cal_date"] in {"20261001", "20261002", "20261005", "20261006"}:
                row["is_open"] = 0
            if changed_history and row["cal_date"] == "20261015" or row["cal_date"] == changed_day:
                row["is_open"] = 0
        result = relay_capture(opener, "trade_cal", {"exchange": venue, "start_date": start,
            "end_date": through}, {"exchange", "cal_date", "is_open"})
        refs[venue] = json.loads(encoded(retain_market_query(result, REGISTRY, store, fresh_reader=fresh,
                                                            custody_authorized=True)["reference"]))
    return refs


def kernel_archive(documents, captures):
    """Synthetic original ZIP, with the same thirteen members as the native study."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in documents.items():
            archive.writestr(name, encoded(value))
        for role, capture in captures.items():
            archive.writestr(role + ".raw.json", capture["body_utf8"].encode())
            archive.writestr(role + ".capture.json", encoded(capture))
    return buffer.getvalue()


def kernel_adoption_fixture(*, storage=None, count=3):
    """Real legacy custody and a synthetic source-free four-response adoption input."""
    from decision_kernel.application.ftshare_adoption import PRIOR_IDENTITY, kernel_check_plan
    from decision_kernel.application.ftshare_collection import _legacy_context
    from decision_kernel.application.ftshare_single import capture_single
    from decision_kernel.application.market_custody import restore_market_query
    from decision_kernel.domain.records import digest

    backend, store, fresh, legacy, old, _ = _setup(count=count, quarantine_last=True, storage=storage)
    context = _legacy_context(legacy, REGISTRY, fresh())
    codes = sorted([code for code in context["eligible_symbols"] if code.endswith(".SH")][:50]
        + [code for code in context["eligible_symbols"] if code.endswith(".SZ")][:50])
    baseline = {"reference": legacy, "master_references": context["master_references"],
        "scope_count": len(context["scope_symbols"]), "symbols": codes,
        "daily_reference": context["baseline_daily_reference"], "daily": context["baseline_daily"]}
    original = restore_market_query(context["baseline_daily_reference"], REGISTRY, fresh())
    verification = {"close": {code: context["baseline_daily_quotes"]["entries"][code]["value"]
        for code in ("000001.SZ", "600000.SH")}, "sessions": context["sessions"],
        "daily_reference": context["baseline_daily_reference"], "page_references": original["page_references"],
        "calendar_references": context["calendar_references"], "scope_count": len(context["scope_symbols"])}
    predecessor = {"identity": deepcopy(PRIOR_IDENTITY), "documents": {"baseline.json": baseline}}
    identity = {"repository": "auguspp/Decision-OS-2", "study": "ftshare-daily-qualification-20261018",
        "route": "kernel-v1", "route_budget": 4, "source_budget": 6,
        "run_id": "67890", "attempt": 1, "code_sha": "c" * 40}
    request = {**identity, "plan": kernel_check_plan(), "predecessor": deepcopy(PRIOR_IDENTITY),
        "predecessor_sha256": digest(encoded(predecessor)), "baseline_sha256": digest(encoded(baseline)),
        "verification_sha256": digest(encoded(verification))}
    captures, rows = {}, []
    for index, spec in enumerate(kernel_check_plan(), 1):
        venue = "SSE" if spec["symbol"].endswith(".SH") else "SZSE"
        value = "8" if spec["adjust_kind"] == "forward" else "10"
        bars = [{"open": value, "high": value, "low": value, "close": value, "volume": 100,
            "turnover": "1000", "ts_millis_open": _millis(day, "09:30:00.000"),
            "ts_millis": _millis(day, "15:00:00.000")} for day in context["sessions"][venue]]
        capture = capture_single(encoded({"code": 0, "data": bars}),
            **{key: val for key, val in spec.items() if key != "id"},
            acquired_at=f"2026-10-19T12:00:0{index}+00:00")
        captures[spec["id"]] = capture
        row = {"id": spec["id"], "status": "qualified-response", "source_requests": 1,
            "raw_file": spec["id"] + ".raw.json", "raw_bytes": capture["wire_bytes"],
            "raw_sha256": capture["wire_sha256"], "request": capture["request"],
            "request_sha256": capture["request_sha256"], "counts": capture["normalized"]["counts"],
            "requested_at": f"2026-10-19T12:00:0{index}+00:00",
            "transport": {"http_status": 200, "wire_bytes": capture["wire_bytes"],
                          "wire_sha256": capture["wire_sha256"]}}
        if spec["adjust_kind"] == "none":
            row["close_matches_baseline"] = True
        if spec["id"] == "raw-sz-repeat":
            row["repeat_equal"] = True
        rows.append(row)
    report = {**identity, "schema": "ftshare-kernel-daily-check-v1", "status": "validated",
        "raw_release": "private-artifact-only", "predecessor": deepcopy(PRIOR_IDENTITY),
        "source_requests": 4, "reserved_requests": 4, "prior_source_requests": 2,
        "cumulative_source_requests": 6, "remaining_source_requests": 0,
        "required_candle_checks": 4, "qualified_candle_checks": 4, "records_written": False,
        "active_source_changed": False, "full_market_coverage_certified": False,
        "long_term_uptime_certified": False, "factor_qualification_granted": False,
        "account_daily_limit_verified": False, "results": rows,
        "started_at": "2026-10-19T12:00:00+00:00", "completed_at": "2026-10-19T12:01:00+00:00"}
    documents = {"baseline.json": baseline, "verification.json": verification,
        "predecessor.json": predecessor, "request.json": request, "result.json": report}
    archive = kernel_archive(documents, captures)
    proof = {"run_id": 67890, "run_attempt": 1, "code_sha": "c" * 40, "artifact_id": 98765,
             "sha256": digest(archive), "size_in_bytes": len(archive)}
    return {"backend": backend, "store": store, "fresh": fresh, "legacy_reference": legacy,
        "old": old, "context": context, "archive": archive, "proof": proof,
        "documents": documents, "captures": captures, "created_at": KERNEL_STAMP,
        "collection_id": "ftshare-cn-eod", "generation": "g20261016"}


def adopt_kernel(fixture, **options):
    from decision_kernel.application.ftshare_collection import adopt_ftshare_kernel
    arguments = {key: fixture[key] for key in ("archive", "proof", "legacy_reference", "store",
                 "collection_id", "generation", "created_at")}
    arguments.update(registry=REGISTRY, fresh_reader=fixture["fresh"], custody_authorized=True)
    return adopt_ftshare_kernel(**{**arguments, **options})


def single_collection_fixture(*, storage=None, count=3):
    fixture = kernel_adoption_fixture(storage=storage, count=count)
    fixture["adopted"] = adopt_kernel(fixture)
    fixture["single"] = plan_single(fixture)
    return fixture


def plan_single(fixture, *, base=None, **options):
    arguments = {"collection_id": fixture["collection_id"], "generation": "g20261016-v1",
        "created_at": fixture["created_at"], "start": "20260701", "end": "20261016",
        "targets": [{"symbol": "600000.SH", "adjust_kind": "forward"}], "source_budget": 1}
    return plan_ftshare_single_collection(base or fixture["adopted"]["reference"], REGISTRY, fixture["fresh"](),
                                         **{**arguments, **options})


def single_capture(collection, slot, *, acquired_at=None):
    from decision_kernel.application.ftshare_single import capture_single
    plan = collection["plan"]
    params = plan["requests"][slot]["parameters"]
    venue = "SSE" if params["symbol"].endswith(".SH") else "SZSE"
    price = "8" if params["adjust_kind"] == "forward" else "10"
    bars = [{"open": price, "high": price, "low": price, "close": price, "volume": 100,
        "turnover": "1000", "ts_millis_open": _millis(day, "09:30:00.000"),
        "ts_millis": _millis(day, "15:00:00.000")} for day in plan["sessions"][venue]]
    return capture_single(encoded({"code": 0, "data": bars}),
        **{key: params[key] for key in ("symbol", "adjust_kind", "since", "until")},
        acquired_at=acquired_at or collection["updated_at"])


def settle_single(fixture, collection, saved, slot=None, *, reason_code=None, source_hold=None):
    slot = slot or collection["plan"]["new_slots"][0]
    at, store, fresh = collection["updated_at"], fixture["store"], fixture["fresh"]
    started = begin_ftshare_slot(collection, slot, at=at)
    reserved = _retain(started, store, fresh, saved["reference"])
    args = {"reason_code": reason_code}
    if reason_code is None:
        retained = retain_ftshare_single(single_capture(started, slot), store, fresh_reader=fresh,
                                        custody_authorized=True)
        args = {"reference": retained["reference"]}
    finished = finish_ftshare_slot(started, slot, at=at, source_hold=source_hold, **args)
    return finished, _retain(finished, store, fresh, reserved["reference"])


def test_plan_reuses_exact_220_slot_legacy_without_reopening_its_allowance():
    backend, store, fresh, old_ref, old, collection = _setup(quarantine=True)
    plan = collection["plan"]
    assert plan["legacy_reference"] == old_ref
    assert plan["legacy"]["unit_count"] == 220
    assert plan["legacy"]["source_requests"] == old["attempt"]["source_requests"]
    assert len(plan["symbols"]) == 4 and len(plan["eligible_symbols"]) == 2
    assert len(plan["quarantined_symbols"]) == 2
    assert plan["source_budget"] == 2 and collection["reserved_requests"] == 0
    assert set(plan["requests"]) == {"none:000", "forward:000"}
    assert all(request["parameters"]["symbols"] == plan["eligible_symbols"]
               for request in plan["requests"].values())
    before = backend.head()
    saved = _retain(collection, store, fresh)
    restored = restore_ftshare_collection(saved["reference"], REGISTRY, fresh())
    assert restored["predecessor"] == old_ref
    assert restored["context"]["scope_symbols"] == plan["symbols"]
    assert restored["context"]["quarantined_symbols"] == plan["quarantined_symbols"]
    assert len(restored["context"]["sessions"]["SSE"]) == 72
    assert restored["context"]["baseline_daily_quotes"]["entries"]["600001.SH"]["value"] == "10"
    assert restored["source_requests"] == 0 and not restored["legacy_allowance_reopened"]
    assert restore_market_collection(old_ref, REGISTRY, fresh())["collection"] == old
    assert backend.head() != before
    assert saved["reference"]["manifest"]["path"].startswith("source-views/ftshare-collection-")


def test_plan_batches_eligible_symbols_in_50s_with_each_whole_history_once():
    _, _, _, _, _, collection = _setup(count=52, quarantine=True)
    plan = collection["plan"]
    assert len(plan["symbols"]) == 54 and len(plan["eligible_symbols"]) == 52
    assert plan["source_budget"] == 4
    for mode in ("none", "forward"):
        requests = [request for slot, request in plan["requests"].items() if slot.startswith(mode + ":")]
        assert [len(request["parameters"]["symbols"]) for request in requests] == [50, 2]
        assert [code for request in requests for code in request["parameters"]["symbols"]] == plan["eligible_symbols"]
        assert all(request["parameters"]["since"] == plan["since"]
                   and request["parameters"]["until"] == plan["until"] for request in requests)


def test_new_exact_calendars_extend_freshness_without_changing_any_price_request_or_legacy():
    from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
    _, store, fresh, old_ref, old, original = _setup()
    calendars = _new_calendars(store, fresh)
    current = plan_ftshare_collection(old_ref, REGISTRY, fresh(), collection_id="ftshare-cn-eod",
        generation="g20261016-ftshare-v1", created_at=STAMP, calendar_references=calendars)
    assert current["plan"]["calendar_end"] == "20261017"
    assert current["plan"]["calendar_references"] == calendars
    assert current["plan"]["legacy_calendar_references"] == original["plan"]["calendar_references"]
    assert current["plan"]["master_references"] == original["plan"]["master_references"]
    assert current["plan"]["legacy"] == original["plan"]["legacy"]
    assert current["plan"]["requests"] == original["plan"]["requests"]
    saved = _retain(current, store, fresh)
    for slot in current["plan"]["requests"]:
        current, saved, _ = _settle(current, slot, saved["reference"], store, fresh)
    restored = restore_ftshare_collection(saved["reference"], REGISTRY, fresh())
    assert restored["context"]["calendar_references"] == calendars
    assert restored["context"]["legacy_calendar_references"] == original["plan"]["calendar_references"]
    assert restore_market_collection(old_ref, REGISTRY, fresh())["collection"] == old
    reading = read_ftshare_collection_version(saved["reference"], REGISTRY, fresh(), read_as_of=STAMP)
    assert reading["view"]["freshness"]["status"] == "current-through-completed-day"
    assert reading["view"]["freshness"]["calendar_through"] == "2026-10-17"
    assert reading["view"]["as_of"] == "2026-10-16"


@pytest.mark.parametrize("case,reason", [
    ("missing-venue", "CALENDAR_REFERENCES"),
    ("wrong-family", "CALENDAR_SCOPE"),
    ("changed-history", "CALENDAR_HISTORY_CHANGED"),
    ("different-end", "CALENDAR_END_MISMATCH"),
])
def test_calendar_override_requires_both_original_complete_scoped_queries(case, reason):
    backend, store, fresh, old_ref, _, original = _setup()
    refs = _new_calendars(store, fresh, changed_history=case == "changed-history")
    if case == "missing-venue":
        refs.pop("SZSE")
    elif case == "wrong-family":
        refs["SSE"] = original["plan"]["master_references"]["SSE"]
    elif case == "different-end":
        refs["SZSE"] = _new_calendars(store, fresh, through="20261018")["SZSE"]
    before = backend.head()
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_" + reason):
        plan_ftshare_collection(old_ref, REGISTRY, fresh(), collection_id="ftshare-cn-eod",
            generation="g20261016-ftshare-v1", created_at=STAMP, calendar_references=refs)
    assert backend.head() == before


def test_actual_source_store_and_normal_reader_restore_both_conventions():
    from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
    from decision_kernel.application.reading_market import open_market
    _, store, fresh, old_ref, old, collection = _setup(quarantine=True)
    saved = _retain(collection, store, fresh)
    refs = []
    for slot in collection["plan"]["requests"]:
        collection, saved, raw = _settle(collection, slot, saved["reference"], store, fresh)
        refs.append(raw["reference"])
    assert collection["reserved_requests"] == 2 and collection["revision"] == 4
    assert latest_ftshare_reference(fresh(), REGISTRY, collection_id=collection["plan"]["collection_id"],
                                   generation=collection["plan"]["generation"]) == saved["reference"]
    reading = read_ftshare_collection_version(saved["reference"], REGISTRY, fresh(), read_as_of=STAMP)
    assert reading["status"] == "restored" and reading["source_requests"] == 0
    view = reading["view"]
    assert view["provider"] == "FTSHARE" and view["scope_count"] == 4
    assert view["raw"]["incomplete_count"] == view["qfq"]["incomplete_count"] == 2
    assert [entry["observed_count"] for entry in view["raw"]["horizon_counts"]] == [2, 2, 2]
    assert [entry["observed_count"] for entry in view["qfq"]["horizon_counts"]] == [2, 2, 2]
    assert view["freshness"]["status"] == "unknown"
    opened = open_market(fresh(), {"reference": saved["reference"]}, registry=REGISTRY, read_as_of=STAMP)
    assert opened["status"] == "readable" and opened["partial"] is True
    assert restore_market_collection(old_ref, REGISTRY, fresh())["collection"] == old
    assert len({ref["manifest"]["path"] for ref in refs}) == 2


def test_raw_body_and_forward_request_cannot_be_substituted_at_settlement():
    backend, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)
    raw = retain_ftshare_batch(_capture(collection, "none:000"), store,
                              fresh_reader=fresh, custody_authorized=True)
    started = begin_ftshare_slot(collection, "forward:000", at=STAMP)
    saved = _retain(started, store, fresh, initial["reference"])
    finished = finish_ftshare_slot(started, "forward:000", at=STAMP, reference=raw["reference"])
    before = backend.head()
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_BATCH_SOURCE_CHANGED"):
        _retain(finished, store, fresh, saved["reference"])
    assert backend.head() == before


def test_failure_and_unknown_request_never_restore_a_fresh_attempt_budget():
    backend, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    inflight = _retain(begun, store, fresh, initial["reference"])
    with pytest.raises(RecordError, match="UNKNOWN_REQUEST_MUST_RECONCILE"):
        begin_ftshare_slot(begun, "forward:000", at=STAMP)
    failed = finish_ftshare_slot(begun, "none:000", at=STAMP, reason_code="FTSHARE_HTTP_503")
    terminal = _retain(failed, store, fresh, inflight["reference"])
    assert terminal["restored"]["collection"]["reserved_requests"] == 1
    with pytest.raises(RecordError, match="SLOT_ALREADY_CONSUMED"):
        begin_ftshare_slot(failed, "none:000", at=STAMP)
    stale = begin_ftshare_slot(collection, "forward:000", at=STAMP)
    before = backend.head()
    with pytest.raises(RecordError, match="STALE_PREDECESSOR"):
        _retain(stale, store, fresh, initial["reference"])
    with pytest.raises(RecordError, match="STALE_PREDECESSOR"):
        _retain(collection, store, fresh)
    assert backend.head() == before


@pytest.mark.parametrize("valid_batch,hold", [
    (False, "FTSHARE_HTTP_403"),
    (True, "FTSHARE_OBSERVED_QUOTA_EXHAUSTED"),
])
def test_provider_hold_survives_exact_restore_and_blocks_unspent_peer_slots(valid_batch, hold):
    backend, store, fresh, old_ref, old, collection = _setup()
    initial = _retain(collection, store, fresh)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    inflight = _retain(begun, store, fresh, initial["reference"])
    args = {"reason_code": "FTSHARE_HTTP_403"}
    raw = None
    if valid_batch:
        raw = retain_ftshare_batch(_capture(begun, "none:000"), store,
                                  fresh_reader=fresh, custody_authorized=True)
        args = {"reference": raw["reference"]}
    held = finish_ftshare_slot(begun, "none:000", at=STAMP, source_hold=hold, **args)
    final = _retain(held, store, fresh, inflight["reference"])
    restored = restore_ftshare_collection(final["reference"], REGISTRY, fresh())["collection"]
    assert restored["source_hold"] == {"reason_code": hold, "set_at": STAMP}
    assert restored["reserved_requests"] == 1 and restored["revision"] == 2
    assert restored["plan"] == collection["plan"]
    assert restored["units"]["forward:000"]["status"] == "pending"
    assert restored["units"]["none:000"]["status"] == ("complete" if valid_batch else "failed")
    if valid_batch:
        assert restore_ftshare_batch(restored["units"]["none:000"]["reference"], fresh(),
            request=restored["plan"]["requests"]["none:000"])["wire_sha256"] == raw["wire_sha256"]
    before = backend.head()
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_SOURCE_HOLD$"):
        begin_ftshare_slot(restored, "forward:000", at="2026-10-19T12:00:00+00:00")
    replay = _retain(restored, store, fresh, inflight["reference"])
    assert replay["reference"] == final["reference"] and backend.head() == before
    assert restore_market_collection(old_ref, REGISTRY, fresh())["collection"] == old


@pytest.mark.parametrize("mutation", ["clear", "replace", "clear-and-begin"])
def test_retained_source_hold_cannot_be_cleared_or_changed_by_a_forged_control(mutation):
    backend, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    inflight = _retain(begun, store, fresh, initial["reference"])
    held = finish_ftshare_slot(begun, "none:000", at=STAMP,
        reason_code="FTSHARE_HTTP_429", source_hold="FTSHARE_HTTP_429")
    final = _retain(held, store, fresh, inflight["reference"])
    forged = deepcopy(held)
    if mutation == "replace":
        forged["source_hold"]["reason_code"] = "FTSHARE_HTTP_401"
    else:
        forged["source_hold"] = None
        if mutation == "clear-and-begin":
            forged = begin_ftshare_slot(forged, "forward:000", at=STAMP)
    validate_ftshare_collection(forged)  # Shape alone cannot authenticate an execution transition.
    before = backend.head()
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_SOURCE_HOLD$"):
        _retain(forged, store, fresh, final["reference"])
    assert backend.head() == before
    assert restore_ftshare_collection(final["reference"], REGISTRY, fresh())["collection"] == held


@pytest.mark.parametrize("hold", ["FTSHARE_HTTP_503", "PRIVATE_PROVIDER_PROSE", "", {}, True])
def test_only_explicit_provider_stop_reasons_are_admitted(hold):
    _, _, _, _, _, collection = _setup()
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_SOURCE_HOLD_INVALID"):
        finish_ftshare_slot(begun, "none:000", at=STAMP, reason_code="FTSHARE_HTTP_503", source_hold=hold)
    assert begun["source_hold"] is None and begun["units"]["none:000"]["status"] == "in-flight"


def test_hold_can_only_be_attached_to_the_single_settlement_clock():
    _, _, _, _, _, collection = _setup()
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    for state in (collection, begun):
        forged = deepcopy(state)
        forged["source_hold"] = {"reason_code": "FTSHARE_HTTP_403", "set_at": STAMP}
        with pytest.raises(RecordError, match="FTSHARE_COLLECTION_SOURCE_HOLD_INVALID"):
            validate_ftshare_collection(forged)
    held = finish_ftshare_slot(begun, "none:000", at=STAMP,
        reason_code="FTSHARE_HTTP_403", source_hold="FTSHARE_HTTP_403")
    held["source_hold"]["set_at"] = "2026-10-19T12:00:00+00:00"
    with pytest.raises(RecordError, match="FTSHARE_COLLECTION_SOURCE_HOLD_INVALID"):
        validate_ftshare_collection(held)


def test_unknown_hold_root_write_reconciles_without_clearing_provider_stop(monkeypatch):
    backend, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    inflight = _retain(begun, store, fresh, initial["reference"])
    held = finish_ftshare_slot(begun, "none:000", at=STAMP,
        reason_code="FTSHARE_HTTP_401", source_hold="FTSHARE_HTTP_401")
    original = backend.commit
    def unknown(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_RESPONSE_LOST")
    monkeypatch.setattr(backend, "commit", unknown)
    with pytest.raises(UnknownWrite):
        _retain(held, store, fresh, inflight["reference"])
    head = backend.head()
    monkeypatch.setattr(backend, "commit", original)
    final = _retain(held, store, fresh, inflight["reference"])
    assert final["restored"]["collection"]["source_hold"] == held["source_hold"]
    assert final["restored"]["collection"]["reserved_requests"] == 1 and backend.head() == head


def test_unknown_root_write_reconciles_same_source_intent_without_second_reservation(monkeypatch):
    backend, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    original_commit = backend.commit

    def committed_but_unknown(files, *, expected):
        original_commit(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_RESPONSE_LOST")

    monkeypatch.setattr(backend, "commit", committed_but_unknown)
    with pytest.raises(UnknownWrite, match="SYNTHETIC_RESPONSE_LOST"):
        _retain(begun, store, fresh, initial["reference"])
    after_write = backend.head()
    monkeypatch.setattr(backend, "commit", original_commit)
    reconciled = _retain(begun, store, fresh, initial["reference"])
    assert backend.head() == after_write
    assert reconciled["restored"]["collection"]["reserved_requests"] == 1
    assert reconciled["restored"]["collection"]["units"]["none:000"]["status"] == "in-flight"


def test_noninitial_retention_does_not_refetch_legacy_or_all_prior_captures(monkeypatch):
    import decision_kernel.application.ftshare_collection as owner
    _, store, fresh, _, _, collection = _setup()
    initial = _retain(collection, store, fresh)

    def no_second_full_restore(*args, **kwargs):
        raise AssertionError("source context must not be reacquired for each control edge")

    monkeypatch.setattr(owner, "_legacy_context", no_second_full_restore)
    monkeypatch.setattr(owner, "_chain", no_second_full_restore)
    begun = begin_ftshare_slot(collection, "none:000", at=STAMP)
    saved = _retain(begun, store, fresh, initial["reference"])
    failed = finish_ftshare_slot(begun, "none:000", at=STAMP, reason_code="FTSHARE_HTTP_503")
    terminal = _retain(failed, store, fresh, saved["reference"])
    assert terminal["restored"]["context_restored"] is False


@pytest.mark.parametrize("mutation", ["legacy", "budget", "request", "counter", "scope"])
def test_tampered_plan_and_reservation_are_rejected_before_writes(mutation):
    backend, store, fresh, _, _, collection = _setup()
    changed = deepcopy(collection)
    if mutation == "legacy":
        changed["plan"]["legacy"]["source_requests"] = 0
    elif mutation == "budget":
        changed["plan"]["source_budget"] += 1
    elif mutation == "request":
        changed["plan"]["requests"]["none:000"]["parameters"]["limit"] = 10
    elif mutation == "counter":
        changed["reserved_requests"] = 1
    else:
        changed["plan"]["symbols"] = changed["plan"]["symbols"][:-1]
    before = backend.head()
    with pytest.raises(RecordError):
        _retain(changed, store, fresh)
    assert backend.head() == before


def test_authorization_and_fresh_reader_are_required_before_any_source_commit():
    backend, store, fresh, _, _, collection = _setup()
    before = backend.head()
    with pytest.raises(RecordError):
        retain_ftshare_collection(collection, REGISTRY, store, fresh_reader=fresh)
    with pytest.raises(RecordError, match="FRESH_READER_REQUIRED"):
        retain_ftshare_collection(collection, REGISTRY, store, fresh_reader=lambda: store,
                                  custody_authorized=True)
    with pytest.raises(RecordError):
        retain_ftshare_batch(_capture(collection, "none:000"), store, fresh_reader=fresh)
    assert backend.head() == before


def test_retained_batch_reads_without_new_source_requests_and_replays_original_intent():
    backend, store, fresh, _, _, collection = _setup()
    capture = _capture(collection, "none:000")
    saved = retain_ftshare_batch(capture, store, fresh_reader=fresh, custody_authorized=True)
    before = backend.head()
    replay = retain_ftshare_batch(capture, store, fresh_reader=fresh, custody_authorized=True)
    assert saved["reference"] == replay["reference"] and backend.head() == before
    restored = restore_ftshare_batch(saved["reference"], fresh(), request=capture["request"])
    assert restored == capture
    source = fresh().read_source(SourceRef.from_dict(saved["reference"]))
    assert source["data"].encode() == capture["body_utf8"].encode()
    assert json.loads(source["data"])["code"] == 200


def test_new_process_reads_native_git_sources_through_normal_market_room_without_credentials(tmp_path):
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    root = tmp_path / "ftshare-synthetic.git"
    backend = GitStore.init_synthetic(root)
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(root))
    _, _, _, _, _, collection = _setup(storage=(backend, store, fresh))
    saved = _retain(collection, store, fresh)
    for slot in collection["plan"]["requests"]:
        collection, saved, _ = _settle(collection, slot, saved["reference"], store, fresh)
    script = """
import json, sys
from decision_kernel.application.reading_market import open_market
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from test_market_relay_inputs import REGISTRY
payload = json.load(sys.stdin)
room = open_market(VersionedStore(GitStore(sys.argv[1])), {'reference': payload['reference']},
                   registry=REGISTRY, read_as_of=payload['clock'])
restored = room['view']
print(json.dumps({'status': room['status'], 'schema': restored['view']['schema'],
                  'provider': restored['view']['provider'], 'source_requests': restored['source_requests'],
                  'raw': [v['observed_count'] for v in restored['view']['raw']['horizon_counts']],
                  'qfq': [v['observed_count'] for v in restored['view']['qfq']['horizon_counts']]}))
"""
    env = {"PATH": os.defpath, "LANG": "C.UTF-8",
           "PYTHONPATH": os.pathsep.join([str(ROOT_DIR / "src"), str(ROOT_DIR / "tests")])}
    process = subprocess.run([sys.executable, "-c", script, str(root)],
        input=encoded({"reference": saved["reference"], "clock": STAMP}).decode(),
        check=True, capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(process.stdout) == {"status": "readable", "schema": "ashare-collection-view-v2",
        "provider": "FTSHARE", "source_requests": 0, "raw": [4, 4, 4], "qfq": [4, 4, 4]}


def test_kernel_adoption_preserves_four_originals_and_archive_with_zero_new_allowance():
    import base64
    from decision_kernel.application.ftshare_adoption import restore_kernel_evidence
    from decision_kernel.application.ftshare_single import restore_single
    f = kernel_adoption_fixture()
    adopted = adopt_kernel(f)
    state, references = adopted["collection"], adopted["capture_references"]
    assert state["revision"] == state["reserved_requests"] == state["plan"]["source_budget"] == 0
    assert set(state["units"]) == {"raw-sz-history", "raw-sh-history", "forward-sz-history"}
    assert all(unit["status"] == "complete" and unit["request_count"] == 0 for unit in state["units"].values())
    assert len(references) == len({encoded(ref) for ref in references.values()}) == 4
    assert adopted["historical_source_requests"] == 4 and adopted["cumulative_source_requests"] == 6
    for role, reference in references.items():
        restored = restore_single(reference, f["fresh"](), request=f["captures"][role]["request"])
        assert restored == f["captures"][role]
    first = SourceRef.from_dict(references["raw-sz-history"])
    repeat = SourceRef.from_dict(references["raw-sz-repeat"])
    a, b = (f["fresh"]().read_source(ref) for ref in (first, repeat))
    assert a["data"] == b["data"] and a["manifest"]["input"]["source_id"] == b["manifest"]["input"]["source_id"]
    assert a["manifest"]["input"]["acquired_at"] != b["manifest"]["input"]["acquired_at"]
    saved = f["fresh"]().read_source(SourceRef.from_dict(adopted["evidence_reference"]))
    assert base64.b64decode(json.loads(saved["data"])["archive_base64"]) == f["archive"]
    evidence = restore_kernel_evidence(adopted["evidence_reference"], f["fresh"]())
    assert evidence["archive"] == f["archive"] and evidence["proof"] == f["proof"]
    assert restore_market_collection(f["legacy_reference"], REGISTRY, f["fresh"]())["collection"] == f["old"]


def test_kernel_adoption_reentry_keeps_original_root_clock_and_source_versions():
    f = kernel_adoption_fixture()
    first = adopt_kernel(f)
    before = f["backend"].head()
    replay = adopt_kernel(f, created_at="2026-10-20T12:00:00+00:00")
    assert replay == first and f["backend"].head() == before
    with pytest.raises(RecordError, match="FTSHARE_ADOPTION_NO_SOURCE_BUDGET"):
        begin_ftshare_slot(first["collection"], "raw-sz-history", at=KERNEL_STAMP)
    assert f["backend"].head() == before


@pytest.mark.parametrize("mutation", ["allowance", "reservation", "extra-owner", "repeat-owner", "old-count"])
def test_kernel_adoption_cannot_turn_evidence_into_new_requests_or_more_coverage(mutation):
    f = kernel_adoption_fixture()
    adopted = adopt_kernel(f)
    broken = deepcopy(adopted["collection"])
    if mutation == "allowance":
        broken["plan"]["source_budget"] = 4
    elif mutation == "reservation":
        broken["units"]["raw-sz-history"]["request_count"] = 1
    elif mutation == "extra-owner":
        broken["plan"]["requests"]["raw-sz-repeat"] = broken["plan"]["requests"]["raw-sz-history"]
    elif mutation == "repeat-owner":
        broken["units"]["raw-sz-history"]["reference"] = broken["plan"]["adoption"]["capture_references"]["raw-sz-repeat"]
    else:
        broken["plan"]["adoption"]["historical_source_requests"] = 0
    before = f["backend"].head()
    with pytest.raises(RecordError):
        _retain(broken, f["store"], f["fresh"])
    assert f["backend"].head() == before


@pytest.mark.parametrize("part", ["raw", "capture", "result", "baseline"])
def test_kernel_archive_or_legacy_mismatch_rejects_before_any_write(part):
    from decision_kernel.application.ftshare_adoption import read_kernel_archive
    from decision_kernel.domain.records import digest
    f = kernel_adoption_fixture()
    captures, documents = deepcopy(f["captures"]), deepcopy(f["documents"])
    if part == "raw":
        captures["raw-sz-history"]["body_utf8"] += " "
    elif part == "capture":
        captures["raw-sh-history"]["request"]["url"] = "https://market.ft.tech/gateway/api/v2/market/data/stock-candlesticks/batch"
    elif part == "result":
        documents["result.json"]["source_requests"] = 0
    else:
        documents["baseline.json"]["scope_count"] -= 1
        documents["predecessor.json"]["documents"]["baseline.json"] = documents["baseline.json"]
        documents["request.json"]["baseline_sha256"] = digest(encoded(documents["baseline.json"]))
        documents["request.json"]["predecessor_sha256"] = digest(encoded(documents["predecessor.json"]))
    archive = kernel_archive(documents, captures)
    proof = {**f["proof"], "sha256": digest(archive), "size_in_bytes": len(archive)}
    if part == "baseline":
        read_kernel_archive(archive, proof)  # Internally coherent is insufficient: original Store must agree.
    before = f["backend"].head()
    with pytest.raises(RecordError):
        adopt_kernel(f, archive=archive, proof=proof)
    assert f["backend"].head() == before


@pytest.mark.parametrize("phase", ["evidence", "root"])
def test_kernel_unknown_write_reconciles_existing_source_intent_without_new_http(monkeypatch, phase):
    f = kernel_adoption_fixture()
    original = f["store"].put_source
    prefix = "ftshare-kernel-evidence-" if phase == "evidence" else "ftshare-collection-"
    def lost(submission_id, source):
        value = original(submission_id, source)
        if source.source_id.startswith(prefix):
            raise UnknownWrite("SYNTHETIC_ADOPTION_RESPONSE_LOST")
        return value
    monkeypatch.setattr(f["store"], "put_source", lost)
    with pytest.raises(UnknownWrite, match="SYNTHETIC_ADOPTION_RESPONSE_LOST"):
        adopt_kernel(f)
    before = f["backend"].head()
    monkeypatch.setattr(f["store"], "put_source", original)
    restored = adopt_kernel(f, created_at="2026-10-20T12:00:00+00:00")
    assert restored["source_requests"] == restored["reserved_requests"] == 0
    assert len(restored["capture_references"]) == 4
    if phase == "root":
        assert f["backend"].head() == before
        assert restored["collection"]["plan"]["created_at"] == KERNEL_STAMP
    paths = f["backend"].paths(f["backend"].head(), "source-views/")
    assert sum(path.endswith("/manifest.json") and path.startswith("source-views/ftshare-") for path in paths) == 6


def test_kernel_adoption_refuses_existing_batch_generation_without_writing():
    f = kernel_adoption_fixture()
    batch = plan_ftshare_collection(f["legacy_reference"], REGISTRY, f["fresh"](),
        collection_id=f["collection_id"], generation=f["generation"], created_at=STAMP)
    _retain(batch, f["store"], f["fresh"])
    before = f["backend"].head()
    with pytest.raises(RecordError, match="FTSHARE_ADOPTION_EXISTING_COLLECTION_CHANGED"):
        adopt_kernel(f)
    assert f["backend"].head() == before


def test_kernel_native_git_new_process_normal_cli_and_room_need_no_source_credentials(tmp_path):
    from decision_kernel.application.reading_market import open_market
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    root = tmp_path / "kernel-adoption.git"
    backend = GitStore.init_synthetic(root)
    storage = (backend, VersionedStore(backend), lambda: VersionedStore(GitStore(root)))
    f = kernel_adoption_fixture(storage=storage)
    adopted = adopt_kernel(f)
    request_path = tmp_path / "exact-reference.json"
    request_path.write_bytes(encoded(adopted["reference"]))
    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": str(ROOT_DIR / "src")}
    process = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli", "read",
        "--path", str(root), "--registry", str(ROOT_DIR / "docs/sources/relay-registry.json"),
        "--input", str(request_path)], capture_output=True, text=True, env=env, check=True, timeout=60)
    reading = json.loads(process.stdout)
    assert reading["source_requests"] == 0 and reading["serving"] == "current-partial"
    assert reading["view"]["scope_count"] == 5
    assert [row["observed_count"] for row in reading["view"]["raw"]["horizon_counts"]] == [2, 2, 2]
    assert [row["observed_count"] for row in reading["view"]["qfq"]["horizon_counts"]] == [1, 1, 1]
    room = open_market(f["fresh"](), adopted["reading_request"], registry=REGISTRY, read_as_of=KERNEL_STAMP)
    assert room["status"] == "readable" and room["partial"] is True
    assert room["view"]["view"]["freshness"]["status"] == "unknown"
    assert len(room["view"]["view"]["source_lineage"]["adoption"]["capture_references"]) == 4


def test_single_plan_and_real_store_inherit_three_owners_and_reserve_only_one_new_call():
    from decision_kernel.application.ftshare_single import restore_single
    f = single_collection_fixture()
    current = f["single"]
    plan = current["plan"]
    assert plan["schema"] == SINGLE_PLAN_SCHEMA and current["schema"] == f["adopted"]["collection"]["schema"]
    assert plan["base_reference"] == f["adopted"]["reference"]
    assert plan["legacy_reference"] == f["legacy_reference"]
    assert plan["new_slots"] == ["forward:600000.SH"] and plan["source_budget"] == 1
    assert current["revision"] == current["reserved_requests"] == plan["prior_generation_reserved_requests"] == 0
    assert plan["adoption"] == f["adopted"]["collection"]["plan"]["adoption"]
    assert len(plan["inherited_units"]) == 3 and len(current["units"]) == 4
    for role in ("raw-sz-history", "raw-sh-history", "forward-sz-history"):
        request = f["captures"][role]["request"]
        params = request["parameters"]
        slot = params["adjust_kind"] + ":" + params["symbol"]
        assert plan["requests"][slot] == request
        assert current["units"][slot] == {"status": "complete", "request_count": 0, "reason_code": None,
                                         "reference": f["adopted"]["capture_references"][role]}
        assert restore_single(current["units"][slot]["reference"], f["fresh"](), request=request) == f["captures"][role]
    initial = _retain(current, f["store"], f["fresh"])
    restored = restore_ftshare_collection(initial["reference"], REGISTRY, f["fresh"]())
    assert restored["predecessor"] == f["adopted"]["reference"] and restored["source_requests"] == 0
    finished, final = settle_single(f, current, initial)
    restored = restore_ftshare_collection(final["reference"], REGISTRY, f["fresh"]())
    assert restored["collection"] == finished
    assert finished["revision"] == 2 and finished["reserved_requests"] == 1
    assert finished["units"]["forward:600000.SH"]["request_count"] == 1
    assert plan["inherited_units"] == {slot: finished["units"][slot] for slot in plan["inherited_units"]}
    assert restore_ftshare_collection(f["adopted"]["reference"], REGISTRY, f["fresh"]())["collection"] == f["adopted"]["collection"]
    assert restore_market_collection(f["legacy_reference"], REGISTRY, f["fresh"]())["collection"] == f["old"]


def test_single_owner_new_whole_range_replaces_old_reference_and_calendar_can_roll():
    f = single_collection_fixture()
    calendars = _new_calendars(f["store"], f["fresh"], start="20260710", through="20261102")
    current = plan_single(f, start="20260710", end="20261102", created_at="2026-11-03T12:00:00+00:00",
        targets=[{"symbol": "000001.SZ", "adjust_kind": "forward"}], calendar_references=calendars)
    plan = current["plan"]
    assert plan["new_slots"] == ["forward:000001.SZ"] and len(plan["requests"]) == 3
    assert set(plan["inherited_units"]) == {"none:000001.SZ", "none:600000.SH"}
    assert current["units"]["forward:000001.SZ"]["reference"] is None
    assert plan["requests"]["none:000001.SZ"] == f["captures"]["raw-sz-history"]["request"]
    initial = _retain(current, f["store"], f["fresh"])
    finished, final = settle_single(f, current, initial)
    restored = restore_ftshare_collection(final["reference"], REGISTRY, f["fresh"]())
    assert restored["context"]["start"] == "20260710" and restored["context"]["end"] == "20261102"
    assert restored["context"]["sessions"]["SZSE"][-1] == "2026-11-02"
    assert restored["context"]["calendar_references"] == calendars
    assert restored["context"]["baseline_daily_reference"] == f["context"]["baseline_daily_reference"]
    assert restored["context"]["legacy"] == f["context"]["legacy"]
    assert finished["units"]["forward:000001.SZ"]["reference"] != f["adopted"]["capture_references"]["forward-sz-history"]
    assert plan["adoption"]["capture_references"]["forward-sz-history"] == f["adopted"]["capture_references"]["forward-sz-history"]


@pytest.mark.parametrize("case,reason", [
    ("missing-new-calendar", "CALENDAR_COVERAGE_REQUIRED"),
    ("calendar-history", "CALENDAR_HISTORY_CHANGED"),
    ("missing-venue", "CALENDAR_REFERENCES"),
    ("future-price-day", "READ_AS_OF_BEFORE_END"),
    ("range-rollback", "RANGE_ROLLBACK"),
    ("too-many-days", "RANGE_BOUND"),
])
def test_single_new_date_requires_actual_complete_overlapping_calendar_and_completed_day(case, reason):
    f = single_collection_fixture()
    options = {"end": "20261019", "created_at": "2026-10-20T12:00:00+00:00"}
    if case != "missing-new-calendar":
        options["calendar_references"] = _new_calendars(f["store"], f["fresh"], through="20261019",
                                                        changed_history=case == "calendar-history")
    if case == "missing-venue":
        options["calendar_references"].pop("SZSE")
    elif case == "future-price-day":
        options["created_at"] = KERNEL_STAMP
    elif case == "range-rollback":
        options["start"] = "20260630"
    elif case == "too-many-days":
        options["end"] = "20261101"
    before = f["backend"].head()
    with pytest.raises((RecordError, ValueError), match=reason):
        plan_single(f, **options)
    assert f["backend"].head() == before


def test_single_new_calendar_must_preserve_newer_prior_dates_beyond_the_original_baseline():
    f = single_collection_fixture()
    calendars = _new_calendars(f["store"], f["fresh"], through="20261019")
    current = plan_single(f, end="20261019", created_at="2026-10-20T12:00:00+00:00",
                          calendar_references=calendars)
    initial = _retain(current, f["store"], f["fresh"])
    _, final = settle_single(f, current, initial)
    bad = _new_calendars(f["store"], f["fresh"], through="20261020", changed_day="20261019")
    before = f["backend"].head()
    with pytest.raises(RecordError, match="CALENDAR_HISTORY_CHANGED"):
        plan_single(f, base=final["reference"], generation="g20261020-v1", end="20261020",
            created_at="2026-10-21T12:00:00+00:00", calendar_references=bad)
    assert f["backend"].head() == before


def test_single_failed_owner_is_retained_without_cost_while_an_unrelated_owner_can_progress():
    f = single_collection_fixture()
    initial = _retain(f["single"], f["store"], f["fresh"])
    failed, terminal = settle_single(f, f["single"], initial, reason_code="FTSHARE_HTTP_503")
    with pytest.raises(RecordError, match="REQUEST_ALREADY_CONSUMED"):
        plan_single(f, base=terminal["reference"], generation="g20261016-v2")
    peer = plan_single(f, base=terminal["reference"], generation="g20261016-v2",
                      targets=[{"symbol": "600001.SH", "adjust_kind": "none"}])
    assert peer["plan"]["prior_generation_reserved_requests"] == 1
    assert peer["revision"] == peer["reserved_requests"] == 0
    assert peer["units"]["forward:600000.SH"] == {
        "status": "failed", "request_count": 0, "reference": None, "reason_code": "FTSHARE_HTTP_503"}
    saved = _retain(peer, f["store"], f["fresh"])
    finished, final = settle_single(f, peer, saved)
    restored = restore_ftshare_collection(final["reference"], REGISTRY, f["fresh"]())
    assert restored["collection"] == finished and finished["reserved_requests"] == 1
    assert finished["units"]["forward:600000.SH"]["reason_code"] == "FTSHARE_HTTP_503"
    assert restored["collection"]["plan"]["adoption"]["cumulative_source_requests"] == 6
    assert restore_ftshare_collection(terminal["reference"], REGISTRY, f["fresh"]())["collection"] == failed
    with pytest.raises(RecordError, match="REQUEST_ALREADY_CONSUMED"):
        plan_single(f, base=final["reference"], generation="g20261016-v3")


@pytest.mark.parametrize("stage", ["pending", "in-flight", "failed", "held"])
def test_single_old_base_cannot_open_a_second_generation_around_unresolved_or_failed_successor(stage):
    f = single_collection_fixture()
    competing = plan_single(f, generation="g20261016-competing")
    current = f["single"]
    saved = _retain(current, f["store"], f["fresh"])
    if stage == "in-flight":
        current = begin_ftshare_slot(current, "forward:600000.SH", at=KERNEL_STAMP)
        saved = _retain(current, f["store"], f["fresh"], saved["reference"])
    elif stage in ("failed", "held"):
        current, saved = settle_single(f, current, saved,
            reason_code="FTSHARE_HTTP_403" if stage == "held" else "FTSHARE_HTTP_503",
            source_hold="FTSHARE_HTTP_403" if stage == "held" else None)
    before = f["backend"].head()
    with pytest.raises(RecordError, match="BASE_ALREADY_HAS_SUCCESSOR"):
        plan_single(f, generation="g20261016-competing")
    with pytest.raises(RecordError, match="BASE_ALREADY_HAS_SUCCESSOR"):
        _retain(competing, f["store"], f["fresh"])
    if stage != "failed":
        with pytest.raises(RecordError, match="BASE_NOT_TERMINAL|SOURCE_HOLD"):
            plan_single(f, base=saved["reference"], generation="g20261016-next",
                        targets=[{"symbol": "600001.SH", "adjust_kind": "none"}])
    assert f["backend"].head() == before
    assert restore_ftshare_collection(f["adopted"]["reference"], REGISTRY, f["fresh"]())["collection"] == f["adopted"]["collection"]


@pytest.mark.parametrize("case", ["genesis", "reservation", "settlement"])
def test_single_unknown_control_write_reconciles_exact_original_state_without_reopening_allowance(monkeypatch, case):
    f = single_collection_fixture()
    current, predecessor = f["single"], None
    if case != "genesis":
        initial = _retain(current, f["store"], f["fresh"])
        predecessor = initial["reference"]
        current = begin_ftshare_slot(current, "forward:600000.SH", at=KERNEL_STAMP)
    if case == "settlement":
        reserved = _retain(current, f["store"], f["fresh"], predecessor)
        predecessor = reserved["reference"]
        capture = retain_ftshare_single(single_capture(current, "forward:600000.SH"), f["store"],
                                        fresh_reader=f["fresh"], custody_authorized=True)
        current = finish_ftshare_slot(current, "forward:600000.SH", at=KERNEL_STAMP,
                                      reference=capture["reference"])
    original = f["backend"].commit
    def unknown(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SYNTHETIC_RESPONSE_LOST")
    monkeypatch.setattr(f["backend"], "commit", unknown)
    with pytest.raises(UnknownWrite):
        _retain(current, f["store"], f["fresh"], predecessor)
    after = f["backend"].head()
    monkeypatch.setattr(f["backend"], "commit", original)
    saved = _retain(current, f["store"], f["fresh"], predecessor)
    assert f["backend"].head() == after
    assert restore_ftshare_collection(saved["reference"], REGISTRY, f["fresh"]())["collection"] == current
    again = _retain(current, f["store"], f["fresh"], predecessor)
    assert again["reference"] == saved["reference"] and f["backend"].head() == after


def test_single_wrong_convention_and_changed_inherited_owners_are_rejected_before_root_write():
    from decision_kernel.application.ftshare_single import FTShareSingleError
    f = single_collection_fixture()
    current = f["single"]
    initial = _retain(current, f["store"], f["fresh"])
    started = begin_ftshare_slot(current, "forward:600000.SH", at=KERNEL_STAMP)
    reserved = _retain(started, f["store"], f["fresh"], initial["reference"])
    forged = finish_ftshare_slot(started, "forward:600000.SH", at=KERNEL_STAMP,
                                 reference=f["adopted"]["capture_references"]["raw-sh-history"])
    before = f["backend"].head()
    with pytest.raises(FTShareSingleError, match="SOURCE_CHANGED"):
        _retain(forged, f["store"], f["fresh"], reserved["reference"])
    changed = deepcopy(started)
    changed["units"]["none:600000.SH"]["reference"] = f["adopted"]["capture_references"]["raw-sz-history"]
    with pytest.raises(RecordError, match="INHERITED_UNIT_CHANGED"):
        validate_ftshare_collection(changed)
    assert f["backend"].head() == before


@pytest.mark.parametrize("case,reason", [
    ("repeat", "REQUEST_ALREADY_CONSUMED"), ("duplicate", "DUPLICATE_TARGET"),
    ("budget", "BUDGET_CHANGED"), ("bool-budget", "BUDGET_CHANGED"),
    ("quarantined", "OUTSIDE_ELIGIBLE_SCOPE"), ("extra-field", "TARGETS"),
    ("new-collection", "BASE_IDENTITY"),
])
def test_single_planning_rejects_replays_and_unbounded_target_substitution(case, reason):
    f = single_collection_fixture()
    options = {}
    if case == "repeat":
        options["targets"] = [{"symbol": "000001.SZ", "adjust_kind": "forward"}]
    elif case == "duplicate":
        options.update(targets=[{"symbol": "600000.SH", "adjust_kind": "forward"}] * 2, source_budget=2)
    elif case in ("budget", "bool-budget"):
        options["source_budget"] = True if case == "bool-budget" else 2
    elif case == "quarantined":
        options["targets"] = [{"symbol": "600002.SH", "adjust_kind": "forward"}]
    elif case == "extra-field":
        options["targets"] = [{"symbol": "600000.SH", "adjust_kind": "forward", "limit": 100}]
    elif case == "new-collection":
        options["collection_id"] = "ftshare-new-budget"
    before = f["backend"].head()
    with pytest.raises(RecordError, match=reason):
        plan_single(f, **options)
    assert f["backend"].head() == before


def test_single_active_slots_and_root_bytes_keep_the_existing_bounds():
    f = single_collection_fixture(count=215)
    targets = [{"symbol": code, "adjust_kind": "none"} for code in f["context"]["eligible_symbols"]
               if code not in ("000001.SZ", "600000.SH")]
    current = plan_single(f, targets=targets[:209], source_budget=209)
    assert len(current["units"]) == 212 and len(current["plan"]["inherited_units"]) == 3
    before = f["backend"].head()
    with pytest.raises(RecordError, match="REQUEST_BOUND"):
        plan_single(f, targets=targets[:210], source_budget=210)
    with pytest.raises(RecordError, match="TARGETS"):
        plan_single(f, targets=targets[:213], source_budget=213)
    current["plan"]["legacy"]["oversized"] = "x" * (512 * 1024)
    with pytest.raises(RecordError, match="STATE_BOUND"):
        validate_ftshare_collection(current)
    assert f["backend"].head() == before


def test_single_source_body_retention_is_idempotent_and_requires_existing_custody_permission():
    f = single_collection_fixture()
    capture = single_capture(f["single"], "forward:600000.SH")
    before = f["backend"].head()
    with pytest.raises(RecordError):
        retain_ftshare_single(capture, f["store"], fresh_reader=f["fresh"])
    assert f["backend"].head() == before
    saved = retain_ftshare_single(capture, f["store"], fresh_reader=f["fresh"], custody_authorized=True)
    after = f["backend"].head()
    again = retain_ftshare_single(capture, f["store"], fresh_reader=f["fresh"], custody_authorized=True)
    assert saved["reference"] == again["reference"] and f["backend"].head() == after
    assert saved["restored"] == capture


def test_old_batch_remains_readable_but_cannot_grant_v1_qualification():
    f = single_collection_fixture()
    batch = plan_ftshare_collection(f["legacy_reference"], REGISTRY, f["fresh"](),
        collection_id=f["collection_id"], generation="g20261016-old-batch", created_at=KERNEL_STAMP)
    saved = _retain(batch, f["store"], f["fresh"])
    assert restore_ftshare_collection(saved["reference"], REGISTRY, f["fresh"]())["collection"] == batch
    before = f["backend"].head()
    with pytest.raises(RecordError, match="RETAINED_V1_QUALIFICATION_REQUIRED"):
        plan_single(f, base=saved["reference"])
    assert f["backend"].head() == before
