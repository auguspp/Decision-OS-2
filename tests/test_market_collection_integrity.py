"""Execution-owner integrity regressions through real source and store contracts."""
import pytest

from decision_kernel.application.market_collection import CollectionSyncInterrupted, checkpoint_id
from decision_kernel.application.market_collection_custody import restore_market_checkpoint, restore_market_collection
from decision_kernel.application.market_collection_reading import (
    latest_market_reference, read_collection_version,
)
from decision_kernel.application.market_custody import QUERY_SCHEMA
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.infrastructure.relay_http import SECRET_ENV
from test_market_collection_sync import (
    Clock, CollectionOpener, END, REGISTRY, http_error, manifest_bodies,
    memory, plan, run, source_calls,
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
