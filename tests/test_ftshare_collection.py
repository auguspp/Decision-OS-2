"""Real Store source chains with synthetic providers; no live traffic or rights claims."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

import pytest

from decision_kernel.application.ftshare_collection import (
    begin_ftshare_slot, finish_ftshare_slot, latest_ftshare_reference,
    plan_ftshare_collection, restore_ftshare_batch, restore_ftshare_collection,
    retain_ftshare_batch, retain_ftshare_collection, validate_ftshare_collection,
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


def _setup(*, count=2, quarantine=False, storage=None):
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


def _new_calendars(store, fresh, *, through="20261017", changed_history=False):
    opener = CollectionOpener(through=through)
    refs = {}
    for venue in ("SSE", "SZSE"):
        for row in opener.rows["trade_cal", venue]:
            if row["cal_date"] in {"20261001", "20261002", "20261005", "20261006"}:
                row["is_open"] = 0
            if changed_history and row["cal_date"] == "20261015":
                row["is_open"] = 0
        result = relay_capture(opener, "trade_cal", {"exchange": venue, "start_date": "20260701",
            "end_date": through}, {"exchange", "cal_date", "is_open"})
        refs[venue] = json.loads(encoded(retain_market_query(result, REGISTRY, store, fresh_reader=fresh,
                                                            custody_authorized=True)["reference"]))
    return refs


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
