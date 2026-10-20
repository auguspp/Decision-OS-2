"""Reconcile original immutable writes and only the original eleven pending slots."""
from copy import copy, deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application import daily_calendar_repair as calendar_repair
from decision_kernel.application import daily_market as daily
from decision_kernel.application import daily_write_reconciliation as reconciliation
from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Conflict, RecordError, SourceInput, SourceRef, UnknownWrite, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from test_daily_market import Host
from test_ftshare_collection import _retain, settle_single
from test_market_custody import MemoryBackend
from test_market_relay_inputs import REGISTRY

AUTHORITY = "https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6293255907"


def wire(value):
    return json.loads(encoded(value))


def settled_original(storage=None):
    """The normal owners save all five effects; the FT r002 response is lost."""
    host = Host(storage=storage, count=6)
    host.approved.update(symbols=sorted(host.fixture["context"]["eligible_symbols"]),
        max_price_requests=12)
    assert len(host.approved["symbols"]) == 6
    host.calendar_error = TimeoutError("synthetic calendar transport timeout")
    failed = host.run()
    assert failed["receipt"]["status"] == "blocked", failed["receipt"]
    calendar_failure = daily.latest_cycle(host.fixture["fresh"]())
    calendar_grant = {"schema": calendar_repair.PROOF_SCHEMA,
        "original_reference": calendar_failure["reference"], "venue": "SSE",
        "additional_requests": 1, "authorization_ref": AUTHORITY}
    host.calendar_error = None
    store, submissions, lost = host.fixture["store"], [], []
    original_put = store.put_source

    def saved_then_lost(submission_id, source, **options):
        before = store.backend.head()
        reference = original_put(submission_id, source, **options)
        submissions.append({"submission_id": submission_id,
            "intent_sha256": digest(encoded({"operation": "source", "input": asdict(source)})),
            "reference": wire(asdict(reference)), "expected_head": before})
        if source.source_id.startswith("ftshare-collection-"):
            state = json.loads(source.text)["collection"]
            if state["plan"]["generation"] == "daily-20261019" and state["revision"] == 2:
                lost.append(reference)
                raise UnknownWrite(reconciliation.ORIGINAL_REASON)
        return reference

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "put_source", saved_then_lost)
        stopped = host.run(calendar_repair=calendar_grant)
    assert stopped["receipt"]["status"] == "persistence-unconfirmed", stopped["receipt"]
    assert len(lost) == 1 and stopped["receipt"]["price_source_requests"] == 1
    item = daily.latest_cycle(host.fixture["fresh"]())
    assert item["cycle"]["revision"] == 8 and item["cycle"]["status"] == "blocked"
    grant = reconciliation.proof({"schema": reconciliation.PROOF_SCHEMA,
        "original_reference": item["reference"],
        "settled_market_reference": wire(asdict(lost[0])), "authorization_ref": AUTHORITY,
        "additional_source_requests": 0,
        "native_run": {"run_id": 12345, "run_attempt": 1, "code_sha": "c" * 40,
            "artifact_id": 67890, "sha256": "a" * 64, "size_bytes": 245783},
        "submissions": submissions[-5:]})
    return host, item, grant, calendar_failure


@pytest.fixture(scope="module")
def original_history():
    return settled_original()


@pytest.fixture
def history(original_history):
    original_host, item, grant, failure = original_history
    host = copy(original_host)
    backend = MemoryBackend(deepcopy(original_host.fixture["backend"].shared))
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    host.fixture = {**original_host.fixture, "backend": backend,
        "store": VersionedStore(backend), "fresh": fresh}
    host.approved = deepcopy(original_host.approved)
    host.calendar_clients, host.price_clients, host.openings = [], [], []
    return host, deepcopy(item), deepcopy(grant), deepcopy(failure)


def select(host, item, grant):
    return reconciliation.select_reconciliation(item, grant, at=host.now,
        registry=REGISTRY, store=host.fixture["fresh"](), approved=host.approved)


def reserve(host, item, grant):
    state = reconciliation.reserve(item, grant, at=host.now)
    return daily.retain_cycle(state, host.fixture["store"], registry=REGISTRY,
        fresh_reader=host.fixture["fresh"], previous=item,
        protected_symbols=host.approved["symbols"])


def test_original_five_keys_reconcile_without_any_write_or_source_request(history, monkeypatch):
    host, item, grant, failure = history
    backend = host.fixture["backend"]
    before = backend.head()
    old_bytes = host.fixture["store"].read_source(SourceRef.from_dict(failure["reference"]))["data"]
    monkeypatch.setattr(backend, "commit", lambda *args, **kwargs: pytest.fail("proof attempted a write"))
    restored = reconciliation.verify_original(item["cycle"], grant, registry=REGISTRY,
        store=host.fixture["store"])
    state = restored["collection"]
    assert state["revision"] == 2 and state["reserved_requests"] == 1
    assert state["plan"]["source_budget"] == 12 and state["source_hold"] is None
    assert sum(row["status"] == "complete" for row in state["units"].values()) == 1
    assert sum(row["status"] == "pending" and row["request_count"] == 0
        for row in state["units"].values()) == 11
    assert select(host, item, grant) is True
    assert backend.head() == before and not host.calendar_clients and not host.price_clients
    assert host.fixture["store"].read_source(SourceRef.from_dict(failure["reference"]))["data"] == old_bytes


def test_reservation_is_additive_keeps_original_budget_and_does_not_relax_v2(history):
    host, item, grant, _ = history
    original = encoded(item)
    state = reconciliation.reserve(item, grant, at=host.now)
    assert encoded(item) == original
    assert state["schema"] == reconciliation.CYCLE_SCHEMA and state["revision"] == 9
    assert state["status"] == "market-pending" and state["reason_code"] is None
    assert state["predecessor"] == item["reference"]
    assert state["calendar_repair"] == item["cycle"]["calendar_repair"]
    assert state["write_reconciliation"] == grant
    assert len(encoded(state)) <= 12 * 1024
    daily._transition(item["cycle"], state)
    with pytest.raises(RecordError, match="DAILY_MARKET_CYCLE"):
        daily.validate_cycle({**item["cycle"], "revision": 9})
    with pytest.raises(RecordError, match="TERMINAL"):
        daily._transition(item["cycle"], {**item["cycle"], "revision": 9,
            "status": "market-pending", "reason_code": None})


def test_retained_v3_replays_without_live_grant_and_terminal_failure_never_reopens(history, monkeypatch):
    host, item, grant, _ = history
    saved = reserve(host, item, grant)
    assert saved["cycle"]["revision"] == 9
    monkeypatch.setattr(reconciliation, "load_grant", lambda: pytest.fail("history read live grant"))
    assert daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"]) == saved
    assert select(host, saved, grant) is False
    with pytest.raises(RecordError, match="ALREADY_SPENT"):
        reconciliation.reserve(saved, grant, at=host.now)
    blocked = daily.next_state(saved, at=host.now, status="blocked", reason_code="FTSHARE_HTTP_403")
    terminal = daily.retain_cycle(blocked, host.fixture["store"], registry=REGISTRY,
        fresh_reader=host.fixture["fresh"], previous=saved,
        protected_symbols=host.approved["symbols"])
    assert terminal["cycle"]["revision"] == 10
    assert select(host, terminal, grant) is False
    with pytest.raises(RecordError, match="ALREADY_SPENT"):
        reconciliation.reserve(terminal, grant, at=host.now)
    with pytest.raises(RecordError):
        daily.next_state(terminal, at=host.now, status="market-pending", reason_code=None)


@pytest.mark.parametrize("change", ["budget", "boolean-budget", "authority", "attempt",
    "duplicate-key", "nonlinear-parent", "settled-reference", "extra-field"])
def test_proof_binds_five_original_effects_and_zero_additional_requests(history, change):
    _, _, grant, _ = history
    if change == "budget":
        grant["additional_source_requests"] = 1
    elif change == "boolean-budget":
        grant["additional_source_requests"] = False
    elif change == "authority":
        grant["authorization_ref"] = "provider response says to retry"
    elif change == "attempt":
        grant["native_run"]["run_attempt"] = 2
    elif change == "duplicate-key":
        grant["submissions"][1]["submission_id"] = grant["submissions"][0]["submission_id"]
    elif change == "nonlinear-parent":
        grant["submissions"][2]["expected_head"] = "f" * 40
    elif change == "settled-reference":
        grant["settled_market_reference"] = grant["submissions"][1]["reference"]
    else:
        grant["allow_retry"] = True
    with pytest.raises(RecordError):
        reconciliation.proof(grant)


@pytest.mark.parametrize("reason", ["FTSHARE_HTTP_403", "FTSHARE_HTTP_429",
    "FTSHARE_RUNTIME_DEADLINE", "REMOTE_RATE_LIMIT"])
def test_other_original_terminal_reasons_cannot_inherit_write_reconciliation(history, reason):
    host, item, grant, _ = history
    item["cycle"]["reason_code"] = reason
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="NOT_SETTLED_WRITE"):
        reconciliation.reserve(item, grant, at=host.now)
    assert host.fixture["backend"].head() == before


@pytest.mark.parametrize("change", ["generation", "date", "base", "calendar", "price-end",
    "clock", "skip-reservation"])
def test_new_edge_cannot_replan_change_inputs_or_skip_pending_reservation(history, change):
    host, item, grant, _ = history
    state = reconciliation.reserve(item, grant, at=host.now)
    if change == "generation":
        state["market_generation"] = "different-generation"
    elif change == "date":
        state["date"] = "20261020"
    elif change == "base":
        state["base_market_reference"] = grant["settled_market_reference"]
    elif change == "calendar":
        state["calendars"]["SSE"]["reserved_requests"] = 1
    elif change == "price-end":
        state["price_end"] = "20261016"
    elif change == "clock":
        state["updated_at"] = "2026-10-18T12:00:00+00:00"
    else:
        state.update(revision=10, status="complete", market_reference=grant["settled_market_reference"])
    with pytest.raises(RecordError):
        daily._transition(item["cycle"], state)


@pytest.mark.parametrize("change", ["wrong-key", "wrong-intent", "journal-rewrite",
    "key-already-existed", "original-source-bytes", "current-source-bytes"])
def test_journal_and_first_source_tampering_reject_before_reservation(history, change):
    host, item, grant, _ = history
    backend = host.fixture["backend"]
    row = grant["submissions"][2]
    path = "submissions/" + row["submission_id"] + ".json"
    if change == "wrong-key":
        row["submission_id"] = "a-different-submission"
    elif change == "wrong-intent":
        row["intent_sha256"] = "f" * 64
    elif change == "journal-rewrite":
        backend.commit({path: b"{}"}, expected=backend.head())
    elif change == "key-already-existed":
        backend.shared["trees"][row["expected_head"]][path] = backend.read(backend.head(), path)
    else:
        source = host.fixture["store"].read_source(SourceRef.from_dict(row["reference"]))
        body_path = source["manifest"]["representation"]["path"]
        if change == "original-source-bytes":
            backend.shared["trees"][row["reference"]["manifest"]["commit"]][body_path] = b"{}"
        else:
            backend.commit({body_path: b"{}"}, expected=backend.head())
    before = backend.head()
    with pytest.raises(RecordError):
        select(host, item, grant)
    assert backend.head() == before and not host.price_clients and not host.calendar_clients


@pytest.mark.parametrize("state", ["in-flight", "failed", "held", "complete"])
def test_newer_ft_state_cannot_select_old_frontier_or_clear_attempts(history, state):
    host, item, grant, _ = history
    f = host.fixture
    restored = ft.restore_ftshare_collection(grant["settled_market_reference"], REGISTRY, f["fresh"]())
    collection = restored["collection"]
    slot = next(slot for slot, row in collection["units"].items() if row["status"] == "pending")
    if state == "in-flight":
        started = ft.begin_ftshare_slot(collection, slot, at=host.now)
        saved = _retain(started, f["store"], f["fresh"], grant["settled_market_reference"])
    else:
        _, saved = settle_single(f, collection, {"reference": grant["settled_market_reference"]}, slot,
            reason_code="FTSHARE_HTTP_403" if state in {"failed", "held"} else None,
            source_hold="FTSHARE_HTTP_403" if state == "held" else None)
    before = f["backend"].head()
    with pytest.raises(RecordError, match="FT_FRONTIER_CHANGED"):
        select(host, item, grant)
    with pytest.raises(RecordError, match="FT_FRONTIER_CHANGED"):
        reserve(host, item, grant)
    assert f["backend"].head() == before
    assert ft.latest_ftshare_reference(f["fresh"](), REGISTRY,
        collection_id=collection["plan"]["collection_id"],
        generation=collection["plan"]["generation"]) == saved["reference"]


def test_competing_daily_revision_is_not_selected_by_ordinal(history):
    host, item, grant, _ = history
    fork = {**item["cycle"], "updated_at": "2026-10-19T12:20:00+00:00"}
    host.fixture["store"].put_source("synthetic-competing-daily-r008", daily._source(fork))
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="HISTORY_FORK"):
        reserve(host, item, grant)
    assert host.fixture["backend"].head() == before


def test_window_is_rechecked_after_native_proof_or_quota_wait_and_not_regranted(history):
    host, item, grant, _ = history
    host.now = "2026-10-26T12:09:59+00:00"
    assert select(host, item, grant) is True
    host.now = "2026-10-26T12:10:00+00:00"
    with pytest.raises(RecordError, match="WINDOW_EXPIRED"):
        reconciliation.reserve(item, grant, at=host.now)
    host.now = "2026-10-19T12:10:00+00:00"
    saved = reserve(host, item, grant)
    host.now = "2026-10-27T12:10:00+00:00"
    assert select(host, saved, grant) is False
    assert daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"]) == saved


def test_terminal_success_cannot_name_the_still_incomplete_original_ft_reference(history):
    host, item, grant, _ = history
    saved = reserve(host, item, grant)
    false_success = daily.next_state(saved, at=host.now, status="complete",
        market_reference=grant["settled_market_reference"])
    before = host.fixture["backend"].head()
    with pytest.raises(RecordError, match="FT_INCOMPLETE"):
        daily.retain_cycle(false_success, host.fixture["store"], registry=REGISTRY,
            fresh_reader=host.fixture["fresh"], previous=saved,
            protected_symbols=host.approved["symbols"])
    assert host.fixture["backend"].head() == before


def test_reconciliation_reservation_uses_entire_verified_snapshot_for_cas(history, monkeypatch):
    host, item, grant, _ = history
    store = host.fixture["store"]
    put = store.put_source
    original_head = store.backend.head()
    calls = []

    def competing(submission_id, source, **options):
        calls.append(options["expected_head"])
        put("synthetic-competing-write", SourceInput("synthetic-competing-write", "content",
            "Unrelated concurrent write", "https://example.org/synthetic", "Independent content"))
        return put(submission_id, source, **options)

    monkeypatch.setattr(store, "put_source", competing)
    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        reserve(host, item, grant)
    assert calls == [original_head]
    assert daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"]) == item


def test_runtime_finishes_original_eleven_then_next_day_uses_ordinary_budget(history):
    host, item, grant, failure = history
    before = {encoded(ref): host.fixture["store"].read_source(SourceRef.from_dict(ref))["data"]
        for ref in (failure["reference"], item["reference"], grant["submissions"][2]["reference"])}
    verified = []
    result = host.run(write_reconciliation=grant,
        reconciliation_verifier=lambda proof: verified.append(proof) or {"status": "synthetic-native-verified"})
    receipt = result["receipt"]
    assert receipt["status"] == "complete", receipt
    assert verified == [grant]
    assert (receipt["calendar_source_requests"], receipt["price_source_requests"]) == (0, 11)
    assert receipt["calendar_reserved_requests"] == receipt["calendar_source_budget"] == 3
    saved = daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])
    assert saved["cycle"]["revision"] == 10 and saved["cycle"]["write_reconciliation"] == grant
    collection = ft.restore_ftshare_collection(saved["cycle"]["market_reference"], REGISTRY,
        host.fixture["fresh"]())["collection"]
    assert collection["plan"]["generation"] == item["cycle"]["market_generation"]
    assert collection["reserved_requests"] == collection["plan"]["source_budget"] == 12
    assert all(unit["status"] == "complete" and unit["request_count"] == 1
        for unit in collection["units"].values())
    for encoded_ref, text in before.items():
        assert host.fixture["store"].read_source(SourceRef.from_dict(json.loads(encoded_ref)))["data"] == text
    repeat = host.run(write_reconciliation=grant,
        reconciliation_verifier=lambda proof: pytest.fail("completed reconciliation repeated native proof"))
    assert repeat["receipt"]["status"] == "already-complete"
    assert repeat["receipt"]["source_requests"] == 0
    host.now = "2026-10-20T12:10:00+00:00"
    next_day = host.run()["receipt"]
    assert next_day["status"] == "complete", next_day
    assert (next_day["calendar_source_requests"], next_day["price_source_requests"]) == (2, 12)
    ordinary = daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])["cycle"]
    assert ordinary["schema"] == daily.SCHEMA
    assert "write_reconciliation" not in ordinary and "calendar_repair" not in ordinary


def test_saved_r009_resumes_only_pending_work_without_regranting_native_evidence(history):
    host, item, grant, _ = history
    stopped = host.run()["receipt"]
    assert stopped["status"] == "source-held" and stopped["source_requests"] == 0
    reserve(host, item, grant)
    result = host.run(
        reconciliation_verifier=lambda proof: pytest.fail("persisted edge needs no new native artifact"))
    assert result["receipt"]["status"] == "complete", result["receipt"]
    assert (result["receipt"]["calendar_source_requests"], result["receipt"]["price_source_requests"]) == (0, 11)
    repeated = host.run()["receipt"]
    assert repeated["status"] == "already-complete" and repeated["source_requests"] == 0


def test_real_price_hold_becomes_r010_terminal_and_never_reopens(history):
    host, _, grant, _ = history
    host.price_error = RecordError("FTSHARE_HTTP_403")
    result = host.run(write_reconciliation=grant)
    assert result["receipt"]["status"] == "failed", result["receipt"]
    assert result["receipt"]["price_source_requests"] == 1
    saved = daily.latest_cycle(host.fixture["fresh"](), registry=REGISTRY,
        protected_symbols=host.approved["symbols"])
    assert saved["cycle"]["revision"] == 10 and saved["cycle"]["status"] == "blocked"
    before = host.fixture["backend"].head()
    host.price_error = None
    repeat = host.run(write_reconciliation=grant)
    assert repeat["receipt"]["status"] == "source-held" and repeat["receipt"]["source_requests"] == 0
    host.now = "2026-10-20T12:10:00+00:00"
    ordinary = host.run()
    assert ordinary["receipt"]["source_requests"] == 0
    assert host.fixture["backend"].head() == before


def test_original_keys_and_v3_edge_replay_in_independent_real_git(tmp_path, monkeypatch):
    backend = GitStore.init_synthetic(tmp_path / "records.git")
    storage = backend, VersionedStore(backend), lambda: VersionedStore(GitStore(backend.root))
    host, item, grant, failure = settled_original(storage)
    original = storage[1].read_source(SourceRef.from_dict(item["reference"]))["data"]
    assert select(host, item, grant) is True
    saved = reserve(host, item, grant)
    monkeypatch.setattr(reconciliation, "load_grant", lambda: pytest.fail("history read revoked grant"))
    reopened = VersionedStore(GitStore(backend.root))
    assert daily.latest_cycle(reopened, registry=REGISTRY,
        protected_symbols=host.approved["symbols"]) == saved
    assert reopened.read_source(SourceRef.from_dict(item["reference"]))["data"] == original
    assert json.loads(reopened.read_source(SourceRef.from_dict(failure["reference"]))["data"])["revision"] == 2
    assert saved["cycle"]["revision"] == 9 and grant["additional_source_requests"] == 0
