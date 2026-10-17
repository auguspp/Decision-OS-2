"""Synthetic market HTTP through the real sync, custody and normal reader.

No provider traffic occurs. These tests prove retained execution and reading
semantics, not supplier availability, historical universe coverage or rights.
"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
import subprocess
import sys
from urllib.error import HTTPError

import pytest

from decision_kernel.application.market_collection import (
    CollectionSyncInterrupted, checkpoint_id, plan_collection, sync_collection,
)
from decision_kernel.application.market_collection_custody import (
    CHECKPOINT_SCHEMA, restore_market_checkpoint, restore_market_collection,
)
from decision_kernel.application.market_collection_reading import (
    latest_market_reference, read_collection_version,
)
from decision_kernel.application.market_custody import QUERY_SCHEMA, restore_market_query
from decision_kernel.application.research_sources import CAPTURE
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP, SECRET_ENV
from test_market_collection_inputs import CollectionOpener
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY, ROOT_DIR


NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
START, END = "20261008", "20261016"


class Clock:
    def __init__(self, seconds=0):
        self.seconds, self.waits = seconds, []

    def now(self):
        return (NOW + timedelta(seconds=self.seconds)).isoformat()

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds


def plan(*, generation="g1", start=START, end=END, through=END, refresh_dates=()):
    return plan_collection(collection_id="synthetic-ashare", generation=generation,
        start=start, end=end, calendar_end=through, refresh_dates=refresh_dates, registry=REGISTRY)


def memory():
    backend = MemoryBackend()
    return backend, VersionedStore(backend), lambda: VersionedStore(MemoryBackend(backend.shared))


def run(plan_value, store, fresh, opener=None, *, previous=None, clock=None, **kwargs):
    clock = Clock() if clock is None else clock
    options = dict(plan=plan_value, registry=REGISTRY, store=store, fresh_reader=fresh,
        attempt_id="sync-attempt", previous_ref=previous, custody_authorized=True,
        access_confirmed=True, max_queries=32, now=NOW, clock=clock.now,
        monotonic=clock.monotonic, sleep=clock.sleep)
    if opener is not None:
        options["client"] = RelayHTTP(opener=opener, credential="synthetic-key-only")
    return sync_collection(**{**options, **kwargs})


def source_calls(opener):
    return [(api, p.get("exchange", p.get("trade_date")), int(p.get("offset", "0")))
            for api, p in opener.calls]


def manifest_bodies(files, schema):
    for path, raw in files.items():
        if path.endswith("/content.txt"):
            value = json.loads(raw)
            if value.get("schema") == schema:
                yield value


def manifest(store, reference):
    return json.loads(store.read_source(SourceRef.from_dict(reference))["data"])


def latest_checkpoint(store):
    return lambda owner: latest_market_reference(store, REGISTRY, checkpoint_id=owner)


def http_error(status, *, error=None, headers=None):
    body = {"error": error} if error is not None else {"message": "synthetic refusal"}
    return HTTPError("https://synthetic.invalid/source", status, "synthetic refusal",
                     headers or {}, BytesIO(encoded(body)))


def test_plan_capture_retention_resume_increment_and_normal_windows():
    backend, store, fresh = memory()
    initial = plan(end="20261015")
    op = CollectionOpener(start=START, end=END)
    seen_reservations = []

    def before_source(api, params):
        slot = ("calendar:" if api == "trade_cal" else "master:") + params["exchange"] \
            if api in ("trade_cal", "stock_basic") else params["trade_date"] + ":" + api
        cp = latest_market_reference(fresh(), REGISTRY, checkpoint_id=checkpoint_id(initial, slot))
        state = restore_market_checkpoint(cp, REGISTRY, fresh())["state"]
        assert state["control"]["retry"]["phase"] == "in-flight"
        assert state["control"]["retry"]["reserved_attempt"] == 1
        assert latest_market_reference(fresh(), REGISTRY,
            collection_id=initial["collection_id"], generation=initial["generation"]) is not None
        seen_reservations.append(slot)

    op.before = before_source
    first = run(initial, store, fresh, op, max_queries=1)
    first_query = first["collection"]["units"]["calendar:SSE"]["query"]
    assert first["status"] == "partial" and source_calls(op) == [("trade_cal", "SSE", 0)]
    # New client and store objects are essential: no in-memory capture is handed over.
    resumed_op = CollectionOpener(start=START, end=END)
    resumed_op.before = before_source
    completed = run(initial, VersionedStore(MemoryBackend(backend.shared)), fresh,
                    resumed_op, previous=first["reference"])
    assert completed["status"] == "complete"
    assert encoded(completed["collection"]["units"]["calendar:SSE"]["query"]) == encoded(first_query)
    assert ("trade_cal", "SSE", 0) not in source_calls(resumed_op)
    assert len(op.calls) + len(resumed_op.calls) == 4 + 3 * 6 == len(seen_reservations)
    assert completed["receipt"]["all_partitions_retained"]

    empty_op = CollectionOpener(start=START, end=END)
    same = run(initial, store, fresh, empty_op, previous=completed["reference"])
    assert same["status"] == "complete" and empty_op.calls == []
    assert same["receipt"]["latest_attempt"]["source_requests"] == 0

    next_op = CollectionOpener(start=START, end=END)
    increment = run(plan(generation="g2"), store, fresh, next_op, previous=same["reference"])
    assert increment["status"] == "complete"
    assert source_calls(next_op) == [("stock_basic", "SSE", 0), ("stock_basic", "SZSE", 0),
        ("daily", END, 0), ("adj_factor", END, 0), ("suspend_d", END, 0)]
    for slot, unit in same["collection"]["units"].items():
        if slot.startswith("2026"):
            assert encoded(increment["collection"]["units"][slot]["query"]) == encoded(unit["query"])
    view = read_collection_version(increment["reference"], REGISTRY, fresh(),
                                   read_as_of=NOW.isoformat())
    assert view["status"] == "restored" and view["serving"] == "current-complete"
    assert view["source_requests"] == 0 and view["view"]["source_requests"] == 0
    assert view["view"]["scope_count"] == 4 and view["view"]["as_of"] == "2026-10-16"
    assert view["view"]["freshness"]["status"] == "current-through-completed-day"
    for kind in ("raw", "qfq"):
        assert view["view"][kind]["observed_count"] == 4
        assert all(entry["horizons"][0]["change_pct"] == "0"
                   for entry in view["view"][kind]["entries"])
    assert len(next_op.calls) == 5  # Reading does not invoke the injected source.
    assert "PRIVATE_SOURCE_NAME" not in encoded(increment["receipt"]).decode()


@pytest.mark.parametrize("pause", ["WORK_UNIT_COMPLETE", "STORAGE_BUDGET"])
def test_two_page_master_keeps_original_offset_and_page_reference(pause):
    _, store, fresh = memory()
    request = plan(start=END)
    op = CollectionOpener(count=501, start=END, end=END)
    paused = run(request, store, fresh, op,
                 pause_requested=lambda: pause if len(op.calls) >= 3 else None)
    assert source_calls(op) == [("trade_cal", "SSE", 0), ("trade_cal", "SZSE", 0),
                               ("stock_basic", "SSE", 0)]
    unit = paused["collection"]["units"]["master:SSE"]
    assert unit["status"] == "paused" and unit["reason_code"] == "RELAY_" + pause
    previous = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fresh())["state"]
    assert previous["control"]["max_pages"] == 20
    assert previous["capture"]["next_parameters"]["offset"] == "500"
    assert len(previous["capture"]["pages"][0]["table"]["items"]) == 500
    first_page_ref = manifest(fresh(), unit["checkpoint"])["pages"][0]["reference"]
    resumed = CollectionOpener(count=501, start=END, end=END)
    finished = run(request, store, fresh, resumed, previous=paused["reference"])
    assert finished["status"] == "complete"
    assert source_calls(resumed)[0] == ("stock_basic", "SSE", 500)
    assert ("stock_basic", "SSE", 0) not in source_calls(resumed)
    retained = finished["collection"]["units"]["master:SSE"]
    full_state = restore_market_checkpoint(retained["checkpoint"], REGISTRY, fresh())["state"]
    assert [p["offset"] for p in full_state["capture"]["pages"]] == [0, 500]
    assert full_state["capture"]["pages"][0] == previous["capture"]["pages"][0]
    query_manifest = manifest(fresh(), retained["query"])
    assert query_manifest["pages"][0]["reference"] == first_page_ref
    restored = restore_market_query(retained["query"], REGISTRY, fresh())["result"]
    assert sum(len(p["table"]["items"]) for p in restored["capture"]["pages"]) == 501


def test_local_terminal_failure_is_retained_without_starving_other_dates():
    _, store, fresh = memory()
    request = plan(start="20261015")
    op = CollectionOpener(start="20261015", end=END)

    def fail_daily(api, params):
        if api == "daily" and params["trade_date"] == END:
            raise http_error(400)

    op.before = fail_daily
    partial = run(request, store, fresh, op, max_queries=5)
    failed = partial["collection"]["units"][END + ":daily"]
    assert failed["status"] == "failed" and failed["reason_code"] == "RELAY_HTTP_400"
    assert len(op.calls) == 5 and partial["receipt"]["unit_counts"]["complete"] == 4
    assert partial["receipt"]["unit_counts"]["pending"] == 5
    original = restore_market_checkpoint(failed["checkpoint"], REGISTRY, fresh())["state"]
    assert original["control"]["state"] == "terminal"
    later = CollectionOpener(start="20261015", end=END)
    resumed = run(request, store, fresh, later, previous=partial["reference"])
    assert source_calls(later) == [("adj_factor", END, 0), ("suspend_d", END, 0),
        ("daily", "20261015", 0), ("adj_factor", "20261015", 0), ("suspend_d", "20261015", 0)]
    assert resumed["collection"]["units"][END + ":daily"] == failed
    assert resumed["receipt"]["unit_counts"]["complete"] == 9
    again = CollectionOpener(start="20261015", end=END)
    unchanged = run(request, store, fresh, again, previous=resumed["reference"])
    assert again.calls == [] and unchanged["collection"]["units"][END + ":daily"] == failed
    reading = read_collection_version(resumed["reference"], REGISTRY, fresh(), read_as_of=NOW.isoformat())
    assert reading["status"] == "partial-restoration" and reading["unavailable_queries"] == 1
    assert reading["view"]["scope_count"] == 4
    assert reading["view"]["freshness"]["status"] == "stale"


def test_partial_new_generation_serves_pinned_previous_values_with_real_staleness():
    _, store, fresh = memory()
    previous_op = CollectionOpener(start=START, end="20261015")
    original = run(plan(end="20261015", through="20261015"), store, fresh, previous_op)
    assert original["status"] == "complete"
    old = read_collection_version(original["reference"], REGISTRY, fresh())
    next_op = CollectionOpener(start=START, end=END)
    partial = run(plan(generation="g2"), store, fresh, next_op,
                  previous=original["reference"], max_queries=2)
    assert source_calls(next_op) == [("trade_cal", "SSE", 0), ("trade_cal", "SZSE", 0)]
    assert partial["status"] == "partial" and partial["receipt"]["has_previous_readable_version"]
    served = read_collection_version(partial["reference"], REGISTRY, fresh(), read_as_of=NOW.isoformat())
    assert served["status"] == "restored" and served["serving"] == "previous-complete"
    assert encoded(served["selected_reference"]) == encoded(original["reference"])
    for part in ("calendars", "masters", "partitions"):
        assert served["view"]["source_lineage"][part] == old["view"]["source_lineage"][part]
    assert set(served["view"]["source_lineage"]["freshness_calendars"]) == {"SSE", "SZSE"}
    assert served["view"]["raw"] == old["view"]["raw"]
    assert served["view"]["qfq"] == old["view"]["qfq"]
    assert served["selected_requested_end"] == "20261015" and served["latest_requested_end"] == END
    assert served["view"]["freshness"]["status"] == "stale"
    assert all(v["current_session_lag"] == 1 for v in served["view"]["by_exchange"].values())
    assert served["latest_attempt"]["status"] == "partial" and served["source_requests"] == 0


def test_global_rate_limit_stops_peers_and_cannot_restart_on_new_session():
    _, store, fresh = memory()
    request = plan(start=END)
    op = CollectionOpener(start=END, end=END)
    op.before = lambda api, params: (_ for _ in ()).throw(
        http_error(429, error="rate_limited", headers={"Retry-After": "invalid"}))
    stopped = run(request, store, fresh, op)
    assert len(op.calls) == 1 and stopped["receipt"]["latest_attempt"]["status"] == "blocked"
    unit = stopped["collection"]["units"]["calendar:SSE"]
    assert unit["status"] == "blocked" and unit["reason_code"] == "RELAY_HTTP_429"
    assert stopped["receipt"]["unit_counts"]["pending"] == 3
    later = CollectionOpener(start=END, end=END)
    same = run(request, store, fresh, later, previous=stopped["reference"])
    assert later.calls == [] and same["collection"]["units"]["calendar:SSE"] == unit


@pytest.mark.parametrize("with_previous", [False, True])
def test_new_generation_cannot_supply_unproved_calendar_sessions(with_previous):
    _, store, fresh = memory()
    original = plan(start=END)
    previous = run(original, store, fresh, CollectionOpener(start=END, end=END))["reference"] \
        if with_previous else None
    changed = plan(generation="g2", start=END)
    changed["sessions"] = [END]
    op = CollectionOpener(start=END, end=END)
    before = store.backend.head()
    with pytest.raises(RecordError, match="ASHARE_CALENDAR_DERIVATION_REQUIRED"):
        run(changed, store, fresh, op, previous=previous)
    assert op.calls == [] and store.backend.head() == before


def test_unknown_reservation_write_stops_before_http_and_recovery_is_blocked(monkeypatch):
    backend, store, fresh = memory()
    request = plan(start=END)
    commit, events = backend.commit, []

    def uncertain(files, *, expected):
        events.append(files)
        result = commit(files, expected=expected)
        if any(c["control"]["retry"] and c["control"]["retry"]["phase"] == "in-flight"
               for c in manifest_bodies(files, CHECKPOINT_SCHEMA)):
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        return result

    monkeypatch.setattr(backend, "commit", uncertain)
    op = CollectionOpener(start=END, end=END)
    with pytest.raises(CollectionSyncInterrupted) as caught:
        run(request, store, fresh, op)
    assert op.calls == [] and len(events) == 2
    assert caught.value.receipt["unit_counts"]["unknown-write"] == 1
    cp = latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, "calendar:SSE"))
    state = restore_market_checkpoint(cp, REGISTRY, fresh())["state"]
    assert state["control"]["retry"]["reserved_attempt"] == 1
    monkeypatch.setattr(backend, "commit", commit)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    blocked = run(request, store, fresh, previous=caught.value.reference, max_queries=1,
                  latest_checkpoint=latest_checkpoint(fresh()))
    assert blocked["collection"]["units"]["calendar:SSE"]["reason_code"] == "RELAY_CHECKPOINT_INFLIGHT"
    assert blocked["receipt"]["latest_attempt"]["source_requests"] == 0


def test_complete_checkpoint_recovers_unknown_query_write_without_credentials(monkeypatch):
    backend, store, fresh = memory()
    request = plan(start=END)
    commit, events = backend.commit, []

    def uncertain(files, *, expected):
        events.append(files)
        result = commit(files, expected=expected)
        if list(manifest_bodies(files, QUERY_SCHEMA)):
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        return result

    monkeypatch.setattr(backend, "commit", uncertain)
    op = CollectionOpener(start=END, end=END)
    with pytest.raises(CollectionSyncInterrupted) as caught:
        run(request, store, fresh, op)
    assert len(op.calls) == 1
    assert list(manifest_bodies(events[-1], QUERY_SCHEMA))
    cp = latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, "calendar:SSE"))
    frozen = restore_market_checkpoint(cp, REGISTRY, fresh())["state"]
    assert frozen["control"]["state"] == "complete"
    monkeypatch.setattr(backend, "commit", commit)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    reconciled = run(request, store, fresh, previous=caught.value.reference, max_queries=1,
                     latest_checkpoint=latest_checkpoint(fresh()))
    assert reconciled["collection"]["units"]["calendar:SSE"]["status"] == "complete"
    assert reconciled["receipt"]["latest_attempt"]["source_requests"] == 0
    assert len(op.calls) == 1


def test_saved_page_with_unknown_control_outcome_does_not_authorize_repeat_http(monkeypatch):
    backend, store, fresh = memory()
    request = plan(start=END)
    commit, events = backend.commit, []

    def uncertain(files, *, expected):
        events.append(files)
        result = commit(files, expected=expected)
        if list(manifest_bodies(files, CAPTURE)):
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        return result

    monkeypatch.setattr(backend, "commit", uncertain)
    op = CollectionOpener(start=END, end=END)
    with pytest.raises(CollectionSyncInterrupted) as caught:
        run(request, store, fresh, op)
    assert len(op.calls) == 1 and len(events) == 3
    assert list(manifest_bodies(events[-1], CAPTURE))
    cp = latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, "calendar:SSE"))
    state = restore_market_checkpoint(cp, REGISTRY, fresh())["state"]
    assert state["control"]["retry"]["phase"] == "in-flight" and state["capture"]["pages"] == []
    monkeypatch.setattr(backend, "commit", commit)
    later = CollectionOpener(start=END, end=END)
    blocked = run(request, store, fresh, later, previous=caught.value.reference,
                  latest_checkpoint=latest_checkpoint(fresh()))
    assert later.calls == []
    assert blocked["collection"]["units"]["calendar:SSE"]["reason_code"] == "RELAY_CHECKPOINT_INFLIGHT"


def test_partial_retry_history_keeps_original_four_attempt_limit_across_work_units():
    _, store, fresh = memory()
    request, first_clock = plan(start=END), Clock()
    op = CollectionOpener(start=END, end=END)

    def failure(api, params):
        raise http_error(503, error="upstream_pool_exhausted")

    op.before = failure
    paused = run(request, store, fresh, op, clock=first_clock,
                 pause_requested=lambda: "WORK_UNIT_COMPLETE" if len(op.calls) >= 2 else None)
    unit = paused["collection"]["units"]["calendar:SSE"]
    state = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fresh())["state"]
    assert unit["status"] == "paused" and len(op.calls) == 2 and first_clock.waits == [30, 30]
    assert [a["attempt"] for a in state["control"]["retry"]["attempts"]] == [1, 2]
    later, second_clock = CollectionOpener(start=END, end=END), Clock(75)
    later.before = failure
    exhausted = run(request, store, fresh, later, previous=paused["reference"],
                    clock=second_clock, max_queries=1)
    failed = exhausted["collection"]["units"]["calendar:SSE"]
    assert failed["status"] == "failed" and len(later.calls) == 2 and second_clock.waits == [30]
    final = restore_market_checkpoint(failed["checkpoint"], REGISTRY, fresh())["state"]
    assert final["control"]["state"] == "terminal"
    assert [a["attempt"] for a in final["control"]["retry"]["attempts"]] == [1, 2, 3, 4]
    assert final["control"]["retry"]["decision"]["reason"] == "attempt-limit"
    third = CollectionOpener(start=END, end=END)
    peers = run(request, store, fresh, third, previous=exhausted["reference"], clock=Clock(120))
    assert source_calls(third) == [("trade_cal", "SZSE", 0), ("stock_basic", "SZSE", 0)]
    assert peers["collection"]["units"]["calendar:SSE"] == failed


def test_remaining_retry_budget_resumes_after_work_unit_deadline():
    _, store, fresh = memory()
    request, clock = plan(start=END), Clock()
    op = CollectionOpener(start=END, end=END)

    def deadline_failure(api, params):
        clock.seconds = 550
        raise http_error(503, error="upstream_pool_exhausted", headers={"Retry-After": "50"})

    op.before = deadline_failure
    paused = run(request, store, fresh, op, clock=clock)
    unit = paused["collection"]["units"]["calendar:SSE"]
    state = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fresh())["state"]
    assert state["control"]["state"] == "paused" and state["control"]["pause_reason"] == "RUN_DEADLINE"
    assert unit["status"] == "paused" and len(op.calls) == 1 and clock.waits == []
    resumed_clock = Clock(seconds=575)
    later = CollectionOpener(start=END, end=END)
    resumed = run(request, store, fresh, later, previous=paused["reference"],
                  clock=resumed_clock, max_queries=1)
    assert source_calls(later) == [("trade_cal", "SSE", 0)]
    assert resumed_clock.waits == [25]
    final = restore_market_checkpoint(resumed["collection"]["units"]["calendar:SSE"]["checkpoint"],
                                      REGISTRY, fresh())["state"]
    attempts = final["capture"]["pages"][0]["attempts"]
    assert [a["attempt"] for a in attempts] == [1, 2]
    assert attempts[0]["checkpoint_wait_seconds"] == 50
    assert attempts[0]["completed_wait_seconds"] == 25


def test_stale_checkpoint_resolver_cannot_rollback_known_page_progress():
    _, store, fresh = memory()
    request = plan(start=END)
    op = CollectionOpener(count=501, start=END, end=END)
    refs = []

    def record(api, params):
        if api == "stock_basic" and params["exchange"] == "SSE":
            refs.append(latest_market_reference(fresh(), REGISTRY,
                checkpoint_id=checkpoint_id(request, "master:SSE")))

    op.before = record
    paused = run(request, store, fresh, op,
                 pause_requested=lambda: "WORK_UNIT_COMPLETE" if len(op.calls) >= 3 else None)
    assert len(refs) == 1
    before = restore_market_checkpoint(refs[0], REGISTRY, fresh())["state"]
    assert before["control"]["retry"]["phase"] == "in-flight"
    assert len(before["capture"]["pages"]) == 0
    later = CollectionOpener(count=501, start=END, end=END)
    blocked = run(request, store, fresh, later, previous=paused["reference"],
                  latest_checkpoint=lambda owner: refs[0])
    assert later.calls == []
    assert blocked["collection"]["units"]["master:SSE"]["reason_code"] == "ASHARE_CHECKPOINT_ROLLBACK"


def test_real_git_new_process_resumes_exact_queries_then_reads_without_credentials(tmp_path):
    root = tmp_path / "synthetic.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    fresh = lambda: VersionedStore(GitStore(root))
    request = plan(start="20261015")
    op = CollectionOpener(start="20261015", end=END)
    partial = run(request, store, fresh, op, max_queries=7)
    assert len(op.calls) == 7 and partial["receipt"]["unit_counts"]["complete"] == 7
    retained = {slot: u["query"] for slot, u in partial["collection"]["units"].items()
                if u["status"] == "complete"}
    data = tmp_path / "resume.json"
    data.write_bytes(encoded({"plan": request, "reference": partial["reference"]}))
    script = """
import json, sys
from pathlib import Path
from test_market_collection_sync import CollectionOpener, END, REGISTRY, run, source_calls
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.market_collection_reading import read_collection_version
root, data = sys.argv[1:]
saved = json.loads(Path(data).read_text())
store = VersionedStore(GitStore(root))
fresh = lambda: VersionedStore(GitStore(root))
op = CollectionOpener(start='20261015', end=END)
result = run(saved['plan'], store, fresh, op, previous=saved['reference'])
view = read_collection_version(result['reference'], REGISTRY, fresh(), read_as_of='2026-10-17T00:00:00+00:00')
print(json.dumps({'result': result, 'calls': source_calls(op), 'view': view}))
"""
    env = {"PATH": os.defpath, "LANG": "C.UTF-8",
           "PYTHONPATH": os.pathsep.join([str(ROOT_DIR / "src"), str(ROOT_DIR / "tests")])}
    process = subprocess.run([sys.executable, "-c", script, str(root), str(data)],
        check=True, capture_output=True, text=True, env=env, timeout=60)
    finished = json.loads(process.stdout)
    assert finished["calls"] == [["daily", "20261015", 0], ["adj_factor", "20261015", 0],
                                 ["suspend_d", "20261015", 0]]
    assert finished["result"]["status"] == "complete" and finished["view"]["status"] == "restored"
    assert finished["view"]["source_requests"] == 0 and finished["view"]["view"]["scope_count"] == 4
    for slot, reference in retained.items():
        assert encoded(finished["result"]["collection"]["units"][slot]["query"]) == encoded(reference)
    assert restore_market_collection(finished["result"]["reference"], REGISTRY, fresh())["collection"] \
        == finished["result"]["collection"]
