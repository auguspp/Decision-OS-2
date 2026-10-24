"""仅用既有合成 fixture 验证 release；不连接来源、网络或真实保管分支。"""
from contextlib import contextmanager
from copy import copy, deepcopy
from datetime import datetime, timezone

import pytest

from decision_kernel.application import daily_master_refresh as refresh
from decision_kernel.application.market_collection_custody import retain_market_checkpoint
from decision_kernel.application.market_custody import restore_market_query
from decision_kernel.application.relay_capture_checkpoint import UNKNOWN_ATTEMPT, release_in_flight
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, StoreUnavailable, UnknownWrite
from test_daily_master_refresh import Host, REGISTRY, STAMP
from test_market_custody import MemoryBackend


def unconfirmed(host, venue="SSE"):
    """让合成 HTTP 已返回而 checkpoint 写前 guard 失败，留下原预留。"""
    before = len(host.opener.calls)

    def stop():
        if len(host.opener.calls) > before:
            raise StoreUnavailable("SYNTHETIC_ADMISSION_STOP")

    result = host.run(venue=venue, guard=stop)
    assert result["status"] == "persistence-unconfirmed", result
    assert result["source_requests"] == 1
    host.cycle["masters"][venue]["checkpoint_reference"] = result["checkpoint_reference"]
    return result


def release(host, **options):
    return refresh.release_master_venue(host.cycle, "SSE", REGISTRY, host.store, host.fresh,
        **{"clock": host.clock.now, **options})


def release_all(host, **options):
    return refresh.release_unconfirmed_masters(host.cycle, REGISTRY, host.store, host.fresh,
        **{"clock": host.clock.now, **options})


def forbidden(*args, **kwargs):
    pytest.fail("重复裁定不应写入、调用 clock 或发出 HTTP")


def test_release_charges_original_attempt_then_one_unit_resumes_normal_query_reader():
    host = Host()
    original = unconfirmed(host)
    value = deepcopy(host.cycle)
    host.clock.seconds = 17
    result = release(host)
    assert result["status"] == "released" and result["outcome"] == "ready"
    assert not result["terminal"] and result["reason_code"] is None
    assert result["charged_attempt"] == result["released_attempt"] == 1
    assert result["attempts_charged"] == result["recorded_attempts"] == 1
    assert result["cumulative_source_requests"] == 1 and result["source_requests"] == 0
    assert result["prior_checkpoint_reference"] == original["checkpoint_reference"]
    assert result["reconciled_at"] == host.clock.now() != STAMP
    assert host.cycle == value and len(host.opener.calls) == 1
    unknown = host.checkpoint()["state"]["control"]["retry"]["attempts"][0]
    assert unknown["outcome"] == UNKNOWN_ATTEMPT
    assert unknown["reconciled_at"] == host.clock.now()
    assert "requested_at" not in unknown and "received_at" not in unknown
    done = host.run()
    assert done["status"] == "complete" and done["source_requests"] == 1, done
    assert done["cumulative_source_requests"] == 2 and len(host.opener.calls) == 2
    checkpoint = host.checkpoint()
    assert checkpoint["state"]["capture"]["pages"][0]["attempts"][0] == unknown
    query = restore_market_query(done["reference"], REGISTRY, host.fresh())
    assert query["page_references"] == checkpoint["page_references"]
    host.complete(done)
    assert refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh()) == query


def test_four_releases_exhaust_original_budget_and_terminal_repeat_never_writes(monkeypatch):
    host = Host()
    for attempt in range(1, 5):
        original = unconfirmed(host)
        assert host.checkpoint()["state"]["control"]["retry"]["reserved_attempt"] == attempt
        host.clock.seconds += 1
        result = release_all(host)[0]
        assert result["prior_checkpoint_reference"] == original["checkpoint_reference"]
        assert result["charged_attempt"] == result["recorded_attempts"] == attempt
        assert result["cumulative_source_requests"] == attempt
        assert result["attempts_charged"] == 1 and result["source_requests"] == 0
        assert result["terminal"] is (attempt == 4)
        assert result["outcome"] == ("failed" if attempt == 4 else "ready")
    assert result["reason_code"] == "RELAY_REQUEST_UNCONFIRMED"
    state = host.checkpoint()["state"]
    assert state["control"]["state"] == "terminal"
    assert state["control"]["retry"]["phase"] == "failed"
    assert [a["attempt"] for a in state["control"]["retry"]["attempts"]] == [1, 2, 3, 4]
    assert all(a["outcome"] == UNKNOWN_ATTEMPT for a in state["control"]["retry"]["attempts"])
    assert host.clock.waits == [] and len(host.opener.calls) == 4
    before = host.backend.head()
    monkeypatch.setattr(refresh, "retain_market_checkpoint", forbidden)
    again = release_all(host, clock=forbidden)[0]
    assert again == {**result, "status": "already-released", "attempts_charged": 0}
    stopped = host.run(client_factory=forbidden)
    assert stopped["status"] == "failed" and stopped["reason_code"] == "RELAY_REQUEST_UNCONFIRMED"
    assert stopped["source_requests"] == 0 and stopped["cumulative_source_requests"] == 4
    assert host.backend.head() == before and len(host.opener.calls) == 4


def test_ready_repeat_returns_retained_clock_reference_and_zero_new_charge(monkeypatch):
    host = Host()
    unconfirmed(host)
    first = release(host)
    before = host.backend.head()
    monkeypatch.setattr(refresh, "retain_market_checkpoint", forbidden)
    monkeypatch.setattr(host.backend, "commit", forbidden)
    again = release(host, clock=forbidden)
    assert again == {**first, "status": "already-released", "attempts_charged": 0}
    assert host.backend.head() == before and len(host.opener.calls) == 1


@pytest.mark.parametrize("attempt", [1, 4])
@pytest.mark.parametrize("fault", ["unknown-commit", "lost-receipt", "lost-readback"])
def test_successful_write_lost_receipt_is_read_from_latest_without_resend(monkeypatch, attempt, fault):
    host = Host()
    for _ in range(attempt - 1):
        unconfirmed(host)
        release(host)
    unconfirmed(host)
    before_count = host.backend.shared["count"]
    commit = host.backend.commit
    retain = refresh.retain_market_checkpoint

    if fault == "unknown-commit":
        def lost_commit(*args, **kwargs):
            commit(*args, **kwargs)
            raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
        monkeypatch.setattr(host.backend, "commit", lost_commit)
    elif fault == "lost-receipt":
        def lost_receipt(*args, **kwargs):
            retain(*args, **kwargs)
            raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
        monkeypatch.setattr(refresh, "retain_market_checkpoint", lost_receipt)

    def preflight(reader):
        if fault == "lost-readback" and host.backend.shared["count"] > before_count:
            raise StoreUnavailable("SYNTHETIC_READBACK_LOST")

    with pytest.raises((UnknownWrite, StoreUnavailable)):
        release(host, preflight=preflight)
    assert host.backend.shared["count"] == before_count + 1
    state = host.checkpoint()["state"]
    assert len(state["control"]["retry"]["attempts"]) == attempt
    assert state["control"]["retry"]["phase"] == ("failed" if attempt == 4 else "ready")
    before = host.backend.head()
    monkeypatch.setattr(refresh, "retain_market_checkpoint", forbidden)
    monkeypatch.setattr(host.backend, "commit", forbidden)
    result = release_all(host, clock=forbidden)[0]
    assert result["status"] == "already-released" and result["attempts_charged"] == 0
    assert result["terminal"] is (attempt == 4)
    assert result["charged_attempt"] == result["cumulative_source_requests"] == attempt
    assert host.backend.head() == before and len(host.opener.calls) == attempt


def test_unknown_write_without_effect_keeps_reservation_until_fresh_reconciliation(monkeypatch):
    host = Host()
    unconfirmed(host)
    before = host.backend.head()
    commit = host.backend.commit
    writes = []

    def unknown(*args, **kwargs):
        writes.append(kwargs["expected"])
        raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")

    monkeypatch.setattr(host.backend, "commit", unknown)
    with pytest.raises(UnknownWrite):
        release(host)
    assert writes == [before] and host.backend.head() == before
    assert host.checkpoint()["state"]["control"]["retry"]["phase"] == "in-flight"
    monkeypatch.setattr(host.backend, "commit", commit)
    result = release(host)
    assert result["attempts_charged"] == result["recorded_attempts"] == 1
    assert len(host.opener.calls) == 1


def test_default_clock_is_actual_utc_not_cycle_created_at():
    host = Host()
    unconfirmed(host)
    before = datetime.now(timezone.utc)
    result = release(host, clock=None)
    after = datetime.now(timezone.utc)
    stamp = datetime.fromisoformat(result["reconciled_at"])
    assert before <= stamp <= after and stamp.utcoffset().total_seconds() == 0
    assert result["reconciled_at"] != host.cycle["created_at"]


def test_old_reconciliation_clock_is_rejected_before_write():
    host = Host()
    unconfirmed(host)
    before = host.backend.head()
    with pytest.raises(RecordError, match="DAILY_MASTER_CLOCK"):
        release(host, clock=lambda: "2026-10-19T12:00:00+00:00")
    assert host.backend.head() == before and len(host.opener.calls) == 1


@pytest.mark.parametrize("phase", [1, 2, 3])
def test_guard_stops_each_prewrite_boundary_without_charging(phase):
    host = Host()
    unconfirmed(host)
    before, calls = host.backend.head(), []

    def guard():
        calls.append(True)
        if len(calls) == phase:
            raise StoreUnavailable("SYNTHETIC_GUARD_STOP")

    with pytest.raises(StoreUnavailable, match="SYNTHETIC_GUARD_STOP"):
        release(host, guard=guard)
    assert len(calls) == phase and host.backend.head() == before
    assert host.checkpoint()["state"]["control"]["retry"]["attempts"] == []
    assert len(host.opener.calls) == 1


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("code", ["DAILY_MARKET_STALE_CONTROL", "DAILY_MARKET_STALE_FRONTIER"])
def test_preflight_stops_stale_daily_current_or_frontier_before_custody(phase, code):
    host = Host()
    unconfirmed(host)
    before, calls = host.backend.head(), []

    def preflight(reader):
        calls.append(reader.backend.head())
        if len(calls) == phase:
            raise RecordError(code)

    with pytest.raises(RecordError, match=code):
        release(host, preflight=preflight)
    assert len(calls) == phase and host.backend.head() == before
    assert host.checkpoint()["state"]["control"]["retry"]["phase"] == "in-flight"
    assert len(host.opener.calls) == 1


class ScopedReader(MemoryBackend):
    """仅对合成 reader 固定 scope 快照，供 preflight 与 exact reads 共用。"""
    @contextmanager
    def market_read_scope(self):
        if getattr(self, "scoped", False):
            yield self
            return
        reader = copy(self)
        reader.scoped = True
        reader.snapshot = super().head()
        yield reader

    def head(self):
        return self.snapshot if getattr(self, "scoped", False) else super().head()


def test_fresh_scoped_reads_share_reader_with_preflight_and_preserve_custody_cas():
    host = Host()
    unconfirmed(host)
    readers, checks, writes = [], [], []
    commit = host.backend.commit

    def fresh():
        backend = ScopedReader(host.backend.shared)
        readers.append(backend)
        return VersionedStore(backend)

    def preflight(reader):
        assert reader.backend.scoped
        assert reader.backend is not host.backend
        checks.append(reader.backend.head())
        saved = refresh._release_latest(host.cycle, "SSE", REGISTRY, reader)
        assert saved["checkpoint_id"] == refresh.checkpoint_id(host.cycle, "SSE")

    def checked_commit(files, *, expected):
        assert expected == host.backend.head() == checks[-1]
        writes.append(expected)
        return commit(files, expected=expected)

    host.fresh = fresh
    host.backend.commit = checked_commit
    result = release(host, preflight=preflight)
    assert result["outcome"] == "ready"
    assert len(readers) == 3 and len(checks) == 3 and len(writes) == 1
    assert checks[0] == checks[1] == writes[0] and checks[-1] == host.backend.head()
    assert len(host.opener.calls) == 1


@pytest.mark.parametrize("boundary", ["before-put", "before-commit"])
@pytest.mark.parametrize("count", [2, 501])
def test_release_checked_head_conflict_never_refreshes_or_retries(monkeypatch, boundary, count):
    host = Host(count=count)
    if count > 500:
        assert host.run()["status"] == "partial"
    unconfirmed(host)
    saved = host.checkpoint()
    before, calls = host.backend.shared["count"], len(host.opener.calls)
    host.fresh = lambda: VersionedStore(ScopedReader(host.backend.shared))
    checks, puts, writes, moved = [], [], [], []
    put, commit = host.store.put_source, host.backend.commit
    other = MemoryBackend(host.backend.shared)

    def move():
        moved.append(other.commit({"synthetic-writer.txt": b"other"}, expected=other.head()))

    def preflight(reader):
        checks.append(reader.backend.head())
        assert refresh._release_latest(host.cycle, "SSE", REGISTRY, reader)["reference"] == saved["reference"]

    def checked_put(submission_id, source, *, expected_head=None):
        puts.append(expected_head)
        assert expected_head == checks[-1]
        assert source.source_id == saved["source_id"]
        if boundary == "before-put":
            move()
        return put(submission_id, source, expected_head=expected_head)

    def checked_commit(files, *, expected):
        writes.append(expected)
        assert expected == checks[-1]
        assert all(path.startswith(("source-views/" + saved["source_id"] + "/", "submissions/"))
            for path in files)
        if boundary == "before-commit":
            move()
        return commit(files, expected=expected)

    monkeypatch.setattr(host.store, "put_source", checked_put)
    monkeypatch.setattr(host.backend, "commit", checked_commit)
    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        release(host, preflight=preflight)
    assert len(checks) == 2 and puts == [checks[-1]]
    assert writes == ([] if boundary == "before-put" else [checks[-1]])
    assert host.backend.shared["count"] == before + 1 and host.backend.head() == moved[0]
    assert host.checkpoint() == saved and len(host.opener.calls) == calls
    monkeypatch.setattr(host.store, "put_source", put)
    monkeypatch.setattr(host.backend, "commit", commit)
    # 下一次显式调用先对账，仍是原 in-flight 时才裁定；不是原调用自动重试。
    host.clock.seconds += 1
    result = release(host)
    assert result["attempts_charged"] == 1 and result["recorded_attempts"] == 1
    assert host.checkpoint()["page_references"] == saved["page_references"]
    assert host.backend.shared["count"] == before + 2 and len(host.opener.calls) == calls


def test_competing_release_after_final_check_is_reconciled_on_next_call(monkeypatch):
    host = Host()
    unconfirmed(host)
    saved, before = host.checkpoint(), host.backend.shared["count"]
    host.fresh = lambda: VersionedStore(ScopedReader(host.backend.shared))
    retain, retained = refresh.retain_market_checkpoint, []
    host.clock.seconds = 1

    def competing(*args, **kwargs):
        retained.append(retain_market_checkpoint(
            release_in_flight(saved["state"], reconciled_at=STAMP), REGISTRY, host.store,
            checkpoint_id=saved["checkpoint_id"], predecessor=saved["reference"],
            fresh_reader=host.fresh, custody_authorized=True))
        return retain(*args, **kwargs)

    monkeypatch.setattr(refresh, "retain_market_checkpoint", competing)
    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        release(host)
    assert len(retained) == 1 and host.backend.shared["count"] == before + 1
    monkeypatch.setattr(refresh, "retain_market_checkpoint", forbidden)
    result = release(host, clock=forbidden)
    assert result["status"] == "already-released" and result["attempts_charged"] == 0
    assert result["checkpoint_reference"] == retained[0]["reference"]
    assert result["reconciled_at"] == STAMP and result["recorded_attempts"] == 1
    assert host.backend.shared["count"] == before + 1 and len(host.opener.calls) == 1


def test_pinned_final_preflight_keeps_checked_head_when_live_writer_moves(monkeypatch):
    host = Host()
    unconfirmed(host)
    saved, before = host.checkpoint(), host.backend.head()
    host.fresh = lambda: VersionedStore(ScopedReader(host.backend.shared))
    checks, puts = [], []
    put = host.store.put_source
    other = MemoryBackend(host.backend.shared)

    def preflight(reader):
        checks.append(reader.backend.head())
        if len(checks) == 2:
            other.commit({"synthetic-frontier.txt": b"moved"}, expected=other.head())
            assert reader.backend.head() == before != other.head()
        assert refresh._release_latest(host.cycle, "SSE", REGISTRY, reader)["reference"] == saved["reference"]

    def checked_put(submission_id, source, *, expected_head=None):
        puts.append(expected_head)
        return put(submission_id, source, expected_head=expected_head)

    monkeypatch.setattr(host.store, "put_source", checked_put)
    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        release(host, preflight=preflight)
    assert checks == [before, before] and puts == [before]
    assert host.checkpoint() == saved and len(host.opener.calls) == 1


@pytest.mark.parametrize("released", [False, True])
def test_fresh_snapshot_tip_can_be_newer_than_write_store_reconciliation_head(monkeypatch, released):
    host = Host()
    stale = host.backend.head()
    unconfirmed(host)
    first = release(host) if released else None
    current = host.backend.head()
    checks, writes = [], []
    commit = host.backend.commit
    host.fresh = lambda: VersionedStore(ScopedReader(host.backend.shared))
    # 写 store 的旧 head 不能成为最终 fresh 检查的替代或 fallback。
    monkeypatch.setattr(host.backend, "head", lambda: stale)

    def preflight(reader):
        checks.append(reader.backend.head())
        assert checks[-1] == current

    def checked_commit(files, *, expected):
        writes.append(expected)
        return commit(files, expected=expected)

    monkeypatch.setattr(host.backend, "commit", checked_commit)
    if released:
        result = release(host, preflight=preflight, clock=forbidden)
        assert result == {**first, "status": "already-released", "attempts_charged": 0}
        assert writes == [] and len(checks) == 1
    else:
        with pytest.raises(Conflict, match="HEAD_CHANGED"):
            release(host, preflight=preflight)
        assert writes == [] and len(checks) == 2
    assert host.backend.shared["head"] == current and len(host.opener.calls) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_unpinned_fallback_rejects_head_change_between_preflight_and_latest(batch):
    host = Host()
    unconfirmed(host)
    saved = host.checkpoint()
    before, checks = host.backend.shared["count"], []
    other = MemoryBackend(host.backend.shared)

    def preflight(reader):
        checks.append(reader.backend.head())
        other.commit({"synthetic-writer.txt": b"other"}, expected=other.head())

    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        (release_all if batch else release)(host, preflight=preflight)
    assert len(checks) == 1 and host.backend.shared["count"] == before + 1
    assert host.checkpoint() == saved and len(host.opener.calls) == 1


def test_snapshot_including_new_original_tip_succeeds_with_that_head(monkeypatch):
    host = Host()
    unconfirmed(host)
    release(host)
    old = host.cycle["masters"]["SSE"]["checkpoint_reference"]
    unconfirmed(host)
    saved, before = host.checkpoint(), host.backend.head()
    assert saved["reference"] != old
    host.cycle["masters"]["SSE"]["checkpoint_reference"] = old
    host.fresh = lambda: VersionedStore(ScopedReader(host.backend.shared))
    commit, writes = host.backend.commit, []

    def checked_commit(files, *, expected):
        writes.append(expected)
        return commit(files, expected=expected)

    monkeypatch.setattr(host.backend, "commit", checked_commit)
    result = release(host)
    assert writes == [before] and result["prior_checkpoint_reference"] == saved["reference"]
    assert result["charged_attempt"] == result["recorded_attempts"] == 2
    assert len(host.opener.calls) == 2


def test_checkpoint_without_expected_head_preserves_original_put_and_replay(monkeypatch):
    host = Host()
    unconfirmed(host)
    saved = host.checkpoint()
    checked = release_in_flight(saved["state"], reconciled_at=host.clock.now())
    put, calls = host.store.put_source, []

    def ordinary_put(submission_id, source):
        calls.append(source.source_id)
        return put(submission_id, source)

    monkeypatch.setattr(host.store, "put_source", ordinary_put)
    before = host.backend.shared["count"]
    result = retain_market_checkpoint(checked, REGISTRY, host.store,
        checkpoint_id=saved["checkpoint_id"], predecessor=saved["reference"],
        fresh_reader=host.fresh, custody_authorized=True)
    assert calls == [saved["source_id"]] and host.backend.shared["count"] == before + 1
    monkeypatch.setattr(host.store, "put_source", put)
    replay = retain_market_checkpoint(checked, REGISTRY, host.store,
        checkpoint_id=saved["checkpoint_id"], predecessor=saved["reference"],
        fresh_reader=host.fresh, custody_authorized=True, expected_head="0" * 40)
    assert replay == result and host.backend.shared["count"] == before + 1


def test_checked_checkpoint_cannot_write_a_new_page(monkeypatch):
    host = Host(count=501)
    assert host.run()["status"] == "partial"
    reservations = []
    host.opener.before = lambda *args: reservations.append(host.checkpoint())
    assert host.run()["status"] == "complete"
    saved, complete = reservations[0], host.checkpoint()
    before = host.backend.head()
    monkeypatch.setattr(host.store, "put_source", forbidden)
    with pytest.raises(RecordError, match="MARKET_CHECKPOINT_CONTROL_ONLY_REQUIRED"):
        retain_market_checkpoint(complete["state"], REGISTRY, host.store,
            checkpoint_id=saved["checkpoint_id"], predecessor=saved["reference"],
            fresh_reader=host.fresh, custody_authorized=True, expected_head=before)
    assert host.backend.head() == before


def test_latest_checkpoint_owner_is_checked_even_without_daily_reference(monkeypatch):
    host = Host()
    unconfirmed(host)
    host.cycle["masters"]["SZSE"].update(status="in-flight", reserved_queries=1)
    other = unconfirmed(host, "SZSE")
    host.cycle["masters"]["SSE"]["checkpoint_reference"] = None
    before = host.backend.head()
    monkeypatch.setattr(refresh, "latest_market_reference", lambda *a, **k: other["checkpoint_reference"])
    with pytest.raises(RecordError, match="DAILY_MASTER_CHECKPOINT_OWNER"):
        release(host)
    assert host.backend.head() == before and len(host.opener.calls) == 2


@pytest.mark.parametrize("batch", [False, True])
def test_daily_known_reference_must_be_ancestor_of_latest(batch):
    host = Host()
    unconfirmed(host)
    host.cycle["masters"]["SZSE"].update(status="in-flight", reserved_queries=1)
    other = unconfirmed(host, "SZSE")
    host.cycle["masters"]["SSE"]["checkpoint_reference"] = other["checkpoint_reference"]
    before = host.backend.head()
    with pytest.raises(RecordError, match="ASHARE_CHECKPOINT_ROLLBACK"):
        (release_all if batch else release)(host)
    assert host.backend.head() == before and len(host.opener.calls) == 2


def test_checkpoint_tip_is_rechecked_before_custody_without_forking_original_chain():
    host = Host()
    unconfirmed(host)
    saved = host.checkpoint()
    before = host.backend.shared["count"]
    checks = []

    def preflight(reader):
        checks.append(True)
        if len(checks) == 2:
            retain_market_checkpoint(release_in_flight(saved["state"], reconciled_at=host.clock.now()),
                REGISTRY, host.store, checkpoint_id=saved["checkpoint_id"],
                predecessor=saved["reference"], fresh_reader=host.fresh, custody_authorized=True)

    with pytest.raises(RecordError, match="DAILY_MASTER_STALE_CHECKPOINT"):
        release(host, preflight=preflight)
    assert host.backend.shared["count"] == before + 1
    again = release(host, clock=forbidden)
    assert again["status"] == "already-released" and again["attempts_charged"] == 0
    assert again["recorded_attempts"] == 1 and len(host.opener.calls) == 1


def test_batch_selects_only_unconfirmed_or_already_released_original_owners():
    host = Host()
    assert release_all(host) == []
    unconfirmed(host)
    host.cycle["masters"]["SZSE"].update(status="in-flight", reserved_queries=1)
    assert host.run(venue="SZSE")["status"] == "complete"
    first = release_all(host)
    assert [row["venue"] for row in first] == ["SSE"]
    before = host.backend.head()
    again = release_all(host, clock=forbidden)
    assert again == [{**first[0], "status": "already-released", "attempts_charged": 0}]
    assert host.backend.head() == before and len(host.opener.calls) == 2


def test_daily_state_validates_unknown_reconciled_time_against_cycle_birth():
    host = Host()
    unconfirmed(host)
    released = release_in_flight(host.checkpoint()["state"], reconciled_at=host.clock.now())
    later = deepcopy(host.cycle)
    host.clock.seconds = 1
    later["created_at"] = host.clock.now()
    with pytest.raises(RecordError, match="DAILY_MASTER_OLD_CAPTURE"):
        refresh._check_state(released, later, refresh._use(later, "SSE", REGISTRY), REGISTRY)


@pytest.mark.parametrize("batch", [False, True])
def test_stale_write_store_is_never_used_for_latest_reads(batch):
    host = Host()
    unconfirmed(host)
    original = host.backend.head
    host.fresh = lambda: VersionedStore(MemoryBackend(host.backend.shared))
    host.backend.head = forbidden
    reads = []

    def preflight(reader):
        saved = refresh._release_latest(host.cycle, "SSE", REGISTRY, reader)
        assert saved["state"]["control"]["retry"]["phase"] == "in-flight"
        reads.append(saved["reference"])
        raise RecordError("SYNTHETIC_READ_CONFIRMED")

    try:
        with pytest.raises(RecordError, match="SYNTHETIC_READ_CONFIRMED"):
            (release_all if batch else release)(host, preflight=preflight)
    finally:
        host.backend.head = original
    assert len(reads) == len(host.opener.calls) == 1


def test_release_preserves_accepted_page_prefix_and_charges_second_page_only():
    host = Host(count=501)
    assert host.run()["status"] == "partial"
    prefix = host.checkpoint()
    unconfirmed(host)
    result = release(host)
    assert result["page_number"] == 2 and result["charged_attempt"] == 1
    assert result["recorded_attempts"] == 1 and result["cumulative_source_requests"] == 2
    released = host.checkpoint()
    assert released["page_references"] == prefix["page_references"]
    assert released["state"]["capture"]["pages"] == prefix["state"]["capture"]["pages"]
    done = host.run()
    assert done["status"] == "complete" and done["cumulative_source_requests"] == 3
    assert [int(params.get("offset", 0)) for _, params in host.opener.calls] == [0, 500, 500]
    host.complete(done)
    refresh.verify_completed_master(host.cycle, "SSE", REGISTRY, host.fresh())


def test_batch_releases_both_venues_once_without_new_owner_or_query_budget():
    host = Host()
    unconfirmed(host)
    host.cycle["masters"]["SZSE"].update(status="in-flight", reserved_queries=1)
    unconfirmed(host, "SZSE")
    cycle = deepcopy(host.cycle)
    before = host.backend.shared["count"]
    results = release_all(host)
    assert [row["venue"] for row in results] == ["SSE", "SZSE"]
    assert all(row["attempts_charged"] == row["charged_attempt"] == 1 for row in results)
    assert host.backend.shared["count"] == before + 2 and len(host.opener.calls) == 2
    assert host.cycle == cycle


def test_release_keeps_original_known_failure_attempt_and_wait_budget():
    host = Host()
    host.opener.fail, host.opener.once = "stock_basic", True
    assert host.run()["status"] == "partial"
    known = host.checkpoint()["state"]["control"]["retry"]
    unconfirmed(host)
    result = release(host)
    assert result["charged_attempt"] == result["recorded_attempts"] == 2
    retry = host.checkpoint()["state"]["control"]["retry"]
    assert retry["attempts"][:-1] == known["attempts"]
    assert retry["wait_budget_seconds"] == known["wait_budget_seconds"] == 30
    done = host.run()
    assert done["status"] == "complete" and done["cumulative_source_requests"] == 3
    assert host.clock.waits == [30] and len(host.opener.calls) == 3
