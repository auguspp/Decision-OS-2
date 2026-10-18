"""Execution-owner integrity regressions through real source and store contracts."""
from copy import deepcopy

import pytest

from decision_kernel.application.market_collection import CollectionSyncInterrupted, checkpoint_id
from decision_kernel.application.market_collection_custody import (
    retain_market_collection, restore_market_checkpoint, restore_market_collection,
)
from decision_kernel.application.market_collection_reading import (
    latest_market_reference, read_collection_version,
)
from decision_kernel.application.market_custody import QUERY_SCHEMA
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP, SECRET_ENV
from test_market_collection_sync import (
    Clock, CollectionOpener, END, REGISTRY, http_error, manifest_bodies,
    memory, plan, retained_checkpoint, run, source_calls,
)
from test_market_custody import MemoryBackend


def latest_collection(store, request):
    return latest_market_reference(store, REGISTRY, collection_id=request["collection_id"],
                                   generation=request["generation"])


def paused_master(store, fresh):
    request = plan(start=END)
    opener = CollectionOpener(count=501, start=END, end=END)
    paused = run(request, store, fresh, opener,
                 pause_requested=lambda: "WORK_UNIT_COMPLETE" if len(opener.calls) >= 3 else None)
    assert paused["collection"]["units"]["master:SSE"]["status"] == "paused"
    return request, paused


@pytest.mark.parametrize("status", ["blocked", "unknown-write"])
def test_later_durable_stop_precedes_every_pending_endpoint(monkeypatch, status):
    _, store, fresh = memory()
    request = plan(start="20260701")
    previous = run(request, store, fresh, CollectionOpener(), max_queries=4)
    collection = deepcopy(previous["collection"])
    slot = "20260701:suspend_d"
    collection["units"][slot].update(status=status, reason_code="SYNTHETIC_RECONCILIATION_REQUIRED")
    saved = retain_market_collection(collection, REGISTRY, store, fresh_reader=fresh,
        predecessor=previous["reference"], custody_authorized=True)
    monkeypatch.setattr(RelayHTTP, "validate_credential", lambda self: pytest.fail("credential before global stop"))
    opener = CollectionOpener()
    stopped = run(request, store, fresh, opener, previous=saved["reference"], max_queries=1)
    assert stopped["receipt"]["latest_attempt"]["status"] == "blocked" and opener.calls == []
    assert stopped["receipt"]["latest_attempt"]["source_requests"] == 0
    assert encoded(stopped["collection"]["units"]) == encoded(collection["units"])


@pytest.mark.parametrize("kind", ["in-flight", "response", "auth", "rate", "secret"])
def test_hidden_later_control_stops_endpoints_before_credentials_or_source(monkeypatch, kind):
    _, store, fresh = memory()
    request = plan(start="20260701")
    previous = run(request, store, fresh, CollectionOpener(), max_queries=4)
    slot, original = "20260701:daily", CollectionOpener()
    if kind in {"auth", "rate"}:
        original.before = lambda api, params: (_ for _ in ()).throw(
            http_error(403 if kind == "auth" else 429, headers={"Retry-After": "invalid"}))
    elif kind == "secret":
        original.edits["20260701", "daily"] = lambda rows: [{**row, "close": "synthetic-key-only"} for row in rows]
    hidden = retained_checkpoint(previous["collection"]["plan"], slot, store, fresh, original, Clock(),
        stop_phase=kind if kind in {"in-flight", "response"} else None)
    if kind in {"auth", "rate", "secret"}:
        assert hidden["state"]["control"]["state"] == "terminal"
    else:
        assert hidden["state"]["control"]["state"] == "executing"
        assert hidden["state"]["control"]["retry"]["phase"] == kind
    assert previous["collection"]["units"][slot]["checkpoint"] is None
    monkeypatch.setattr(RelayHTTP, "validate_credential", lambda self: pytest.fail("credential before hidden stop"))
    later = CollectionOpener()
    stopped = run(request, store, fresh, later, previous=previous["reference"], max_queries=1, clock=Clock(1))
    assert later.calls == [] and stopped["receipt"]["latest_attempt"]["source_requests"] == 0
    assert stopped["receipt"]["latest_attempt"]["reason_code"] == (
        "RELAY_CHECKPOINT_INFLIGHT" if kind in {"in-flight", "response"} else "RELAY_CHECKPOINT_GLOBAL_STOP")
    assert stopped["receipt"]["execution_error_persisted"] is False
    durable = restore_market_collection(stopped["reference"], REGISTRY, fresh())["collection"]
    assert encoded(durable["units"]) == encoded(previous["collection"]["units"])
    assert encoded(latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, slot))) == encoded(hidden["reference"])


def test_hidden_complete_later_query_is_adopted_before_endpoints_without_credentials(monkeypatch):
    _, store, fresh = memory()
    request = plan(start="20260701")
    previous = run(request, store, fresh, CollectionOpener(), max_queries=4)
    slot, opener = "20260701:daily", CollectionOpener()
    old = retained_checkpoint(previous["collection"]["plan"], slot, store, fresh, opener, Clock())
    assert old["state"]["control"]["state"] == "complete"
    monkeypatch.delenv(SECRET_ENV, raising=False)
    callbacks = []

    def selected_only(owner):
        callbacks.append(owner)
        assert owner == checkpoint_id(request, slot)
        return old["reference"]

    adopted = run(request, store, fresh, previous=previous["reference"], max_queries=1,
        clock=Clock(1), latest_checkpoint=selected_only)
    assert callbacks == [checkpoint_id(request, slot)]
    assert adopted["collection"]["units"][slot]["status"] == "complete"
    assert adopted["collection"]["units"][END + ":daily"]["status"] == "pending"
    assert adopted["receipt"]["latest_attempt"]["source_requests"] == 0 and len(opener.calls) == 1
    reading = read_collection_version(adopted["reference"], REGISTRY, fresh())
    assert reading["sources"][slot]["status"] == "restored" and reading["source_requests"] == 0


def test_newly_derived_date_owners_are_checked_before_more_source(monkeypatch):
    _, store, fresh = memory()
    request = plan(start="20260701")
    monkeypatch.delenv(SECRET_ENV, raising=False)
    entered = run(request, store, fresh, max_queries=1)
    initial = restore_market_collection(entered["reference"], REGISTRY, fresh())["collection"]
    assert initial["plan"]["sessions"] == [] and len(initial["units"]) == 4
    opener = CollectionOpener()
    for venue in ("SSE", "SZSE"):
        retained_checkpoint(request, "calendar:" + venue, store, fresh, opener, Clock())
    proved = {**request, "sessions": opener.days}
    hidden = retained_checkpoint(proved, "20260701:daily", store, fresh, opener, Clock(), stop_phase="in-flight")
    monkeypatch.setattr(RelayHTTP, "validate_credential", lambda self: pytest.fail("source before new owner scan"))
    later = CollectionOpener()
    stopped = run(request, store, fresh, later, previous=entered["reference"], max_queries=3, clock=Clock(1))
    assert later.calls == [] and stopped["receipt"]["latest_attempt"]["source_requests"] == 0
    assert stopped["receipt"]["latest_attempt"]["reason_code"] == "RELAY_CHECKPOINT_INFLIGHT"
    assert stopped["collection"]["plan"]["sessions"] == opener.days
    assert all(stopped["collection"]["units"]["calendar:" + v]["status"] == "complete" for v in ("SSE", "SZSE"))
    assert encoded(latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, "20260701:daily"))) == encoded(hidden["reference"])


@pytest.mark.parametrize("kind", ["in-flight", "auth"])
def test_hidden_repair_stop_blocks_ordinary_work_without_advancing_the_repair(monkeypatch, kind):
    from decision_kernel.application.market_collection_repair import REPAIR_SLOTS, repair_checkpoint_id
    from test_market_terminal_repair import repair_fixture, repair_run
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    slot, original = REPAIR_SLOTS[-1], CollectionOpener()
    if kind == "auth":
        original.before = lambda api, params: (_ for _ in ()).throw(http_error(403))
    retained_checkpoint(granted["collection"]["plan"], slot, fixture["store"], fixture["fresh"],
        original, fixture["clock"], owner=repair_checkpoint_id(slot),
        stop_phase="in-flight" if kind == "in-flight" else None)
    monkeypatch.setattr(RelayHTTP, "validate_credential", lambda self: pytest.fail("ordinary source after repair stop"))
    later = CollectionOpener()
    stopped = run(fixture["plan"], fixture["store"], fixture["fresh"], later,
        previous=granted["reference"], clock=fixture["clock"], max_queries=1)
    assert later.calls == [] and stopped["receipt"]["latest_attempt"]["source_requests"] == 0
    assert stopped["receipt"]["latest_attempt"]["reason_code"] == (
        "RELAY_CHECKPOINT_INFLIGHT" if kind == "in-flight" else "RELAY_CHECKPOINT_GLOBAL_STOP")
    assert stopped["receipt"]["execution_error_persisted"] is False
    assert stopped["collection"]["repair"] == granted["collection"]["repair"]
    assert stopped["collection"]["units"] == granted["collection"]["units"]


def test_missing_credential_blocks_only_this_operation_then_resumes_saved_page(monkeypatch):
    backend, store, fresh = memory()
    request, previous = paused_master(store, fresh)
    saved_unit = previous["collection"]["units"]["master:SSE"]
    old = restore_market_checkpoint(saved_unit["checkpoint"], REGISTRY, fresh())
    assert len(old["state"]["capture"]["pages"]) == 1
    writes = backend.shared["count"]
    monkeypatch.delenv(SECRET_ENV, raising=False)
    blocked = run(request, store, fresh, previous=previous["reference"], max_queries=1)
    assert blocked["receipt"]["latest_attempt"]["status"] == "blocked"
    assert blocked["receipt"]["latest_attempt"]["reason_code"] == "RELAY_CREDENTIAL_REQUIRED"
    assert blocked["receipt"]["latest_attempt"]["source_requests"] == 0
    assert blocked["receipt"]["execution_error_persisted"] is False
    # Only the entry plan was saved. The missing credential is not a new source
    # outcome, and no catch-block commit may turn the durable page into blocked.
    assert backend.shared["count"] == writes + 1
    durable = restore_market_collection(blocked["reference"], REGISTRY, fresh())["collection"]
    assert encoded(durable["units"]["master:SSE"]) == encoded(saved_unit)
    assert encoded(latest_collection(fresh(), request)) == encoded(blocked["reference"])
    supplied = CollectionOpener(count=501, start=END, end=END)
    resumed = run(request, store, fresh, supplied, previous=blocked["reference"], max_queries=1)
    assert source_calls(supplied) == [("stock_basic", "SSE", 500)]
    unit = resumed["collection"]["units"]["master:SSE"]
    assert unit["status"] == "complete"
    restored = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fresh())
    assert restored["state"]["capture"]["pages"][0] == old["state"]["capture"]["pages"][0]
    assert restored["page_references"][0] == old["page_references"][0]


def test_rejected_old_callback_does_not_poison_durable_query_or_repeat_successful_page():
    backend, store, fresh = memory()
    request, previous = paused_master(store, fresh)
    saved_unit = previous["collection"]["units"]["master:SSE"]
    old = restore_market_checkpoint(saved_unit["checkpoint"], REGISTRY, fresh())
    accepted = restore_market_checkpoint(old["predecessor"], REGISTRY, fresh())
    stale = accepted["predecessor"]
    assert restore_market_checkpoint(stale, REGISTRY, fresh())["state"]["capture"]["pages"] == []
    writes = backend.shared["count"]
    blocked_opener = CollectionOpener(count=501, start=END, end=END)
    blocked = run(request, store, fresh, blocked_opener, previous=previous["reference"],
                  max_queries=1, latest_checkpoint=lambda owner: stale)
    assert blocked["receipt"]["latest_attempt"]["status"] == "blocked"
    assert blocked["receipt"]["latest_attempt"]["reason_code"] == "ASHARE_CHECKPOINT_ROLLBACK"
    assert blocked["receipt"]["execution_error_persisted"] is False
    assert blocked_opener.calls == [] and backend.shared["count"] == writes + 1
    durable = restore_market_collection(blocked["reference"], REGISTRY, fresh())["collection"]
    assert encoded(durable["units"]["master:SSE"]) == encoded(saved_unit)
    corrected = CollectionOpener(count=501, start=END, end=END)
    resumed = run(request, store, fresh, corrected, previous=blocked["reference"], max_queries=1)
    assert source_calls(corrected) == [("stock_basic", "SSE", 500)]
    unit = resumed["collection"]["units"]["master:SSE"]
    assert unit["status"] == "complete"
    restored = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fresh())
    assert restored["state"]["capture"]["pages"][0] == old["state"]["capture"]["pages"][0]
    assert restored["page_references"][0] == old["page_references"][0]


def test_stale_collection_predecessor_is_rejected_before_new_writes_or_http():
    backend, store, fresh = memory()
    request, old = paused_master(store, fresh)
    completed_op = CollectionOpener(count=501, start=END, end=END)
    current = run(request, store, fresh, completed_op, previous=old["reference"], max_queries=1)
    assert source_calls(completed_op) == [("stock_basic", "SSE", 500)]
    assert current["collection"]["units"]["master:SSE"]["status"] == "complete"
    before = backend.head()
    repeated = CollectionOpener(count=501, start=END, end=END)
    with pytest.raises(RecordError):
        run(request, store, fresh, repeated, previous=old["reference"], max_queries=1,
            clock=Clock(120))
    assert repeated.calls == [] and backend.head() == before
    assert encoded(latest_collection(fresh(), request)) == encoded(current["reference"])


def test_existing_generation_cannot_be_recreated_with_no_previous_reference():
    backend, store, fresh = memory()
    request, old = paused_master(store, fresh)
    before = backend.head()
    repeated = CollectionOpener(count=501, start=END, end=END)
    with pytest.raises(RecordError):
        run(request, store, fresh, repeated, max_queries=1, clock=Clock(120))
    assert repeated.calls == [] and backend.head() == before
    assert encoded(latest_collection(fresh(), request)) == encoded(old["reference"])


@pytest.mark.parametrize("misleading_callback", [False, True])
def test_default_discovery_restores_complete_control_and_rejects_a_hiding_callback(monkeypatch, misleading_callback):
    backend, store, fresh = memory()
    request = plan(start=END)
    commit = backend.commit

    def unknown(files, *, expected):
        result = commit(files, expected=expected)
        if list(manifest_bodies(files, QUERY_SCHEMA)):
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED")
        return result

    monkeypatch.setattr(backend, "commit", unknown)
    opener = CollectionOpener(start=END, end=END)
    with pytest.raises(CollectionSyncInterrupted) as stopped:
        run(request, store, fresh, opener)
    cp = latest_market_reference(fresh(), REGISTRY,
        checkpoint_id=checkpoint_id(request, "calendar:SSE"))
    assert restore_market_checkpoint(cp, REGISTRY, fresh())["state"]["control"]["state"] == "complete"
    monkeypatch.setattr(backend, "commit", commit)
    monkeypatch.delenv(SECRET_ENV, raising=False)
    options = {"latest_checkpoint": lambda owner: None} if misleading_callback else {}
    reconciled = run(request, store, fresh, previous=stopped.value.reference, max_queries=1, **options)
    unit = reconciled["collection"]["units"]["calendar:SSE"]
    if misleading_callback:
        assert unit["status"] == "blocked" and unit["reason_code"] == "ASHARE_CHECKPOINT_ROLLBACK"
    else:
        assert unit["status"] == "complete"
    assert reconciled["receipt"]["latest_attempt"]["source_requests"] == 0
    assert len(opener.calls) == 1


def test_old_generation_parent_cannot_fork_past_a_newer_globally_blocked_attempt():
    backend, store, fresh = memory()
    old = run(plan(start=END), store, fresh, CollectionOpener(start=END, end=END))
    op = CollectionOpener(start=END, end=END)

    def denied(api, params):
        if api == "stock_basic" and params["exchange"] == "SSE":
            raise http_error(429, error="rate_limited", headers={"Retry-After": "invalid"})

    op.before = denied
    blocked = run(plan(start=END, generation="g2"), store, fresh, op, previous=old["reference"])
    assert blocked["collection"]["units"]["master:SSE"]["status"] == "blocked"
    before = backend.head()
    later = CollectionOpener(start=END, end=END)
    with pytest.raises(RecordError):
        run(plan(start=END, generation="g3"), store, fresh, later,
            previous=old["reference"], max_queries=1, clock=Clock(120))
    assert later.calls == [] and backend.head() == before


@pytest.mark.parametrize("old_end,new_end,new_through", [
    (END, "20261015", END),
    ("20261015", "20261015", "20261015"),
])
def test_successor_cannot_roll_back_data_or_calendar_boundary(old_end, new_end, new_through):
    backend, store, fresh = memory()
    old_plan = plan(start="20261015", end=old_end)
    old = run(old_plan, store, fresh, CollectionOpener(start="20261015", end=END))
    assert old["status"] == "complete"
    newer = plan(start="20261015", end=new_end, through=new_through, generation="g2")
    op = CollectionOpener(start="20261015", end=END)
    before = backend.head()
    with pytest.raises(RecordError):
        run(newer, store, fresh, op, previous=old["reference"])
    assert op.calls == [] and backend.head() == before


def test_used_generation_cannot_reopen_after_a_completed_successor():
    backend, store, fresh = memory()
    old = run(plan(start=END), store, fresh, CollectionOpener(start=END, end=END))
    current = run(plan(start=END, generation="g2"), store, fresh,
                  CollectionOpener(start=END, end=END), previous=old["reference"])
    assert current["status"] == "complete"
    before = backend.head()
    reopened = CollectionOpener(start=END, end=END)
    with pytest.raises(RecordError):
        run(plan(start=END), store, fresh, reopened, previous=current["reference"])
    assert reopened.calls == [] and backend.head() == before


@pytest.mark.parametrize("unfinished", ["pending", "paused", "blocked"])
def test_new_generation_requires_finishing_or_reconciling_original_owner(unfinished):
    backend, store, fresh = memory()
    request = plan(start=END)
    opener = CollectionOpener(count=501 if unfinished == "paused" else 2, start=END, end=END)
    options = {"max_queries": 1} if unfinished == "pending" else {}
    if unfinished == "paused":
        options["pause_requested"] = lambda: "WORK_UNIT_COMPLETE" if len(opener.calls) >= 3 else None
    if unfinished == "blocked":
        def denied(api, params):
            if api == "stock_basic" and params["exchange"] == "SSE":
                raise http_error(429, error="rate_limited", headers={"Retry-After": "invalid"})
        opener.before = denied
    previous = run(request, store, fresh, opener, **options)
    slot = "calendar:SZSE" if unfinished == "pending" else "master:SSE"
    assert previous["collection"]["units"][slot]["status"] == unfinished
    before = backend.head()
    next_op = CollectionOpener(start=END, end=END)
    with pytest.raises(RecordError, match="ASHARE_RECONCILE_ORIGINAL_GENERATION"):
        run(plan(start=END, generation="g2"), store, fresh, next_op,
            previous=previous["reference"], max_queries=1, clock=Clock(120))
    assert next_op.calls == [] and backend.head() == before


def test_terminal_master_keeps_original_budget_when_completed_peers_advance_generation():
    _, store, fresh = memory()
    request = plan(start=END)
    opener, clock = CollectionOpener(start=END, end=END), Clock()

    def exhausted(api, params):
        if api == "stock_basic" and params["exchange"] == "SSE":
            raise http_error(503, error="upstream_pool_exhausted")

    opener.before = exhausted
    previous = run(request, store, fresh, opener, clock=clock)
    failed = previous["collection"]["units"]["master:SSE"]
    assert failed["status"] == "failed"
    assert all(u["status"] in {"complete", "failed"} for u in previous["collection"]["units"].values())
    state = restore_market_checkpoint(failed["checkpoint"], REGISTRY, fresh())["state"]
    assert [a["attempt"] for a in state["control"]["retry"]["attempts"]] == [1, 2, 3, 4]
    later = CollectionOpener(start=END, end=END)
    successor = run(plan(start=END, generation="g2"), store, fresh, later,
                    previous=previous["reference"], clock=Clock(120))
    assert source_calls(later) == [("stock_basic", "SZSE", 0)]
    assert encoded(successor["collection"]["units"]["master:SSE"]) == encoded(failed)


def test_global_store_error_during_resume_preserves_source_state_and_stops_all_writes(monkeypatch):
    backend, store, fresh = memory()
    request, previous = paused_master(store, fresh)
    unit = previous["collection"]["units"]["master:SSE"]
    forbidden = unit["checkpoint"]["manifest"]["path"]
    events, commit = [], backend.commit

    class UnavailableReader(MemoryBackend):
        def read(self, at, path):
            if path == forbidden:
                events.append("store-unavailable")
                raise StoreUnavailable("STORE_SNAPSHOT_UNAVAILABLE")
            return super().read(at, path)

    def writer(files, *, expected):
        events.append("commit")
        return commit(files, expected=expected)

    monkeypatch.setattr(backend, "commit", writer)
    unavailable = lambda: VersionedStore(UnavailableReader(backend.shared))
    opener = CollectionOpener(count=501, start=END, end=END)
    with pytest.raises(StoreUnavailable, match="STORE_SNAPSHOT_UNAVAILABLE"):
        run(request, store, unavailable, opener, previous=previous["reference"])
    assert opener.calls == []
    assert events[-1] == "store-unavailable"
    assert "commit" not in events[events.index("store-unavailable") + 1:]
    retained = restore_market_collection(latest_collection(fresh(), request), REGISTRY, fresh())["collection"]
    assert encoded(retained["units"]["master:SSE"]) == encoded(unit)


def test_normal_read_propagates_global_store_error_without_a_partial_data_result():
    backend, store, fresh = memory()
    request = plan(start=END)
    complete = run(request, store, fresh, CollectionOpener(start=END, end=END))
    query = complete["collection"]["units"][END + ":daily"]["query"]["manifest"]["path"]
    touched = []

    class UnavailableReader(MemoryBackend):
        def read(self, at, path):
            touched.append(path)
            if path == query:
                raise StoreUnavailable("GITHUB_API_REQUEST_BOUND")
            return super().read(at, path)

    with pytest.raises(StoreUnavailable, match="GITHUB_API_REQUEST_BOUND"):
        read_collection_version(complete["reference"], REGISTRY,
                                VersionedStore(UnavailableReader(backend.shared)))
    assert touched[-1] == query
