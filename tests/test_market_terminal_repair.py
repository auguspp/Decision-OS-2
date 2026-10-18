"""Synthetic original terminals and one explicit retained repair episode.

The fixed production plan is used with synthetic rows and native evidence.
No supplier, repository or native GitHub request is issued by these tests.
"""
from copy import deepcopy
from datetime import timedelta

import pytest

from decision_kernel.application.market_collection import (
    CollectionSyncInterrupted, collection_progress_digest, effective_unit, plan_collection,
)
from decision_kernel.application.market_collection_custody import (
    CHECKPOINT_SCHEMA, REPAIR_COLLECTION_SCHEMA, retain_market_collection, restore_market_checkpoint, restore_market_collection,
)
from decision_kernel.application.market_collection_reading import latest_market_reference, read_collection_version
from decision_kernel.application.market_collection_repair import (
    REPAIR_SLOTS, checkpoint_reserved_requests, repair_checkpoint_id, repair_collection, repair_finished, repair_summary,
)
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from test_market_collection_sync import Clock, CollectionOpener, NOW, REGISTRY, manifest, memory, run, source_calls

EXECUTION_SHA = "e" * 40
TRANSPORT_SHA = "a" * 40


def repair_fixture(*, backend_store=None, only_three_sessions=False):
    """A real checkpoint/custody base; callers own and may fault its fresh copy."""
    backend, store, fresh = memory() if backend_store is None else backend_store
    clock = Clock()
    plan = plan_collection(collection_id="cn-eod", generation="g20261016", start="20260701",
        end="20261016", calendar_end="20261016", registry=REGISTRY)
    original = CollectionOpener()
    if only_three_sessions:
        for venue in ("SSE", "SZSE"):
            for row in original.rows["trade_cal", venue]:
                row["is_open"] = int(row["cal_date"] in {"20261014", "20261015", "20261016"})

    def timeout_original(api, parameters):
        if parameters.get("trade_date", "") + ":" + api in REPAIR_SLOTS:
            raise TimeoutError("SYNTHETIC_ORIGINAL_TIMEOUT")

    original.before = timeout_original
    # Long calendars first consume six other endpoint queries. Construct the
    # same four original exhausted owners before collecting repair evidence.
    base = run(plan, store, fresh, original, max_queries=10 if only_three_sessions else 16,
        clock=clock, attempt_id="run-100-1-u1")
    assert all(base["collection"]["units"][slot]["status"] == "failed" for slot in REPAIR_SLOTS)
    assert all(sum(day + ":" + api == slot for api, day, _ in source_calls(original)) == 4
               for slot in REPAIR_SLOTS)
    evidence = {}
    native_start = clock.now()
    for index, api in enumerate(("daily", "adj_factor"), 1):
        base = run(plan, store, fresh, CollectionOpener(), previous=base["reference"],
            max_queries=1, clock=clock, attempt_id=f"run-123-1-u{index}")
        base["collection"] = restore_market_collection(base["reference"], REGISTRY, fresh())["collection"]
        slot = "20261014:" + api
        unit = base["collection"]["units"][slot]
        assert unit["status"] == "complete"
        evidence[api] = {"code_sha": TRANSPORT_SHA, "run_id": "123", "run_attempt": "1",
            "collection_reference": base["reference"], "slot": slot,
            "checkpoint_reference": unit["checkpoint"], "query_reference": unit["query"]}
    if only_three_sessions:
        base = run(plan, store, fresh, CollectionOpener(), previous=base["reference"],
            max_queries=1, clock=clock, attempt_id="run-123-1-u3")
        base["collection"] = restore_market_collection(base["reference"], REGISTRY, fresh())["collection"]
    request = {"schema": "ashare-terminal-repair-request-v1", "base_reference": base["reference"],
        "terminal_references": {slot: base["collection"]["units"][slot]["checkpoint"] for slot in REPAIR_SLOTS},
        "transport_evidence": evidence}
    callbacks = []
    context = {"execution_code_sha": EXECUTION_SHA, "transport_runs": {
        api: {"run_id": "123", "run_attempt": "1", "started_at": native_start, "completed_at": clock.now()}
        for api in evidence}}

    def verify_evidence(document):
        assert encoded(document) == encoded(request)
        callbacks.append(deepcopy(document))
        return deepcopy(context)

    return {"backend": backend, "store": store, "fresh": fresh, "clock": clock, "plan": plan,
        "base": base, "grant_request": request, "verify_evidence": verify_evidence,
        "execution_code_sha": EXECUTION_SHA, "callbacks": callbacks, "native_context": context,
        "original_opener": original}


def repair_run(fixture, *, previous=None, activate=False, opener=None, **options):
    clock = fixture["clock"]
    arguments = {"plan": fixture["plan"], "registry": REGISTRY, "store": fixture["store"],
        "fresh_reader": fixture["fresh"], "attempt_id": "run-200-1-u1",
        "previous_ref": previous or fixture["base"]["reference"], "custody_authorized": True,
        "access_confirmed": True, "now": NOW, "clock": clock.now, "monotonic": clock.monotonic,
        "sleep": clock.sleep, "execution_code_sha": fixture["execution_code_sha"]}
    if activate:
        arguments.update(grant_request=fixture["grant_request"], verify_evidence=fixture["verify_evidence"])
    if opener is not None:
        arguments["client"] = RelayHTTP(opener=opener, credential="synthetic-key-only")
    return repair_collection(**{**arguments, **options})


def test_activation_has_zero_source_and_complete_repair_preserves_original_budget_and_normal_reading():
    fixture = repair_fixture()
    original = deepcopy(fixture["base"]["collection"]["units"])
    opener = CollectionOpener()
    granted = repair_run(fixture, activate=True, opener=opener)
    assert opener.calls == [] and len(fixture["callbacks"]) == 1
    assert granted["receipt"]["repair_operation"] == "repair-granted"
    assert granted["receipt"]["latest_attempt"]["source_requests"] == 0
    assert collection_progress_digest(granted["collection"]) != collection_progress_digest(fixture["base"]["collection"])
    assert manifest(fixture["store"], granted["reference"])["schema"] == REPAIR_COLLECTION_SCHEMA
    done = repair_run(fixture, previous=granted["reference"], opener=opener, max_queries=4)
    assert done["status"] == "repair-complete" and repair_finished(done["collection"])
    assert len(opener.calls) == 4 and done["collection"]["units"] == original
    assert done["receipt"]["unit_counts"]["failed"] == 4
    assert done["receipt"]["effective_unit_counts"]["failed"] == 0
    assert done["receipt"]["repair"]["reserved_requests"] == 4
    assert done["receipt"]["repair"]["max_new_requests"] == 32
    assert done["receipt"]["repair"]["original_requests"] == 16
    assert done["receipt"]["repair"]["max_combined_requests"] == 48
    assert not done["receipt"]["all_partitions_retained"]
    restored = restore_market_collection(done["reference"], REGISTRY, fixture["fresh"]())
    assert restored["collection"]["units"] == original
    for slot in REPAIR_SLOTS:
        assert effective_unit(restored["collection"], slot)["status"] == "complete"
        state = restore_market_checkpoint(original[slot]["checkpoint"], REGISTRY, fixture["fresh"]())["state"]
        assert state["control"]["state"] == "terminal" and checkpoint_reserved_requests(state) == 4
    view = read_collection_version(done["reference"], REGISTRY, fixture["fresh"](), limit=1)
    assert view["sources"]["20261016:daily"]["status"] == "restored"
    assert view["sources"]["20261016:daily"]["repair"]["origin"] == original["20261016:daily"]["checkpoint"]
    before = fixture["backend"].head()
    again = repair_run(fixture, previous=done["reference"], opener=opener)
    assert again["status"] == "repair-complete" and fixture["backend"].head() == before and len(opener.calls) == 4


def test_normal_sync_never_starts_repair_and_second_grant_is_rejected():
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    opener = CollectionOpener()
    ordinary = run(fixture["plan"], fixture["store"], fixture["fresh"], opener,
        previous=granted["reference"], clock=fixture["clock"], max_queries=1)
    assert source_calls(opener) == [("suspend_d", "20261014", 0)]
    assert ordinary["collection"]["repair"] == granted["collection"]["repair"]
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="ASHARE_REPAIR_SECOND_GRANT"):
        repair_run(fixture, previous=ordinary["reference"], activate=True, opener=opener)
    assert fixture["backend"].head() == before and len(opener.calls) == 1


def test_retry_exhaustion_cannot_repair_repair_or_reset_the_old_terminals():
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    opener = CollectionOpener()
    opener.before = lambda api, params: (_ for _ in ()).throw(TimeoutError("SYNTHETIC_REPAIR_TIMEOUT"))
    failed = repair_run(fixture, previous=granted["reference"], opener=opener, max_queries=4)
    assert failed["status"] == "repair-exhausted" and len(opener.calls) == 16
    assert repair_summary(failed["collection"])["reserved_requests"] == 16
    assert failed["collection"]["repair"]["grant"] == granted["collection"]["repair"]["grant"]
    before = fixture["backend"].head()
    same = repair_run(fixture, previous=failed["reference"], opener=opener)
    assert same["status"] == "repair-exhausted" and len(opener.calls) == 16 and fixture["backend"].head() == before


def test_custody_rejects_grant_removal_and_false_budget_reset_without_writing():
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    finished = repair_run(fixture, previous=granted["reference"], opener=CollectionOpener())
    for change in (lambda c: c.pop("repair"),
                   lambda c: c["repair"]["units"][REPAIR_SLOTS[0]].update(reserved_requests=0)):
        broken = deepcopy(finished["collection"])
        change(broken)
        before = fixture["backend"].head()
        with pytest.raises(RecordError):
            retain_market_collection(broken, REGISTRY, fixture["store"], predecessor=finished["reference"],
                fresh_reader=fixture["fresh"], custody_authorized=True)
        assert fixture["backend"].head() == before


def test_callback_is_required_and_native_window_is_checked_before_grant_write():
    fixture = repair_fixture()
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="ASHARE_REPAIR_NATIVE_EVIDENCE_REQUIRED"):
        repair_run(fixture, activate=True, verify_evidence=None)
    assert fixture["backend"].head() == before
    wrong = deepcopy(fixture["native_context"])
    for window in wrong["transport_runs"].values():
        window.update(started_at=(NOW - timedelta(days=2)).isoformat(), completed_at=(NOW - timedelta(days=1)).isoformat())
    with pytest.raises(CollectionSyncInterrupted) as captured:
        repair_run(fixture, activate=True, verify_evidence=lambda document: wrong)
    assert captured.value.receipt["persistence_error"] == "ASHARE_REPAIR_EVIDENCE_OUTSIDE_RUN"
    assert fixture["backend"].head() == before


def test_first_source_storage_pause_is_a_valid_zero_attempt_genesis_and_resumes_original_chain():
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    opener = CollectionOpener()
    paused = repair_run(fixture, previous=granted["reference"], opener=opener,
        pause_requested=lambda: "STORAGE_BUDGET")
    slot = REPAIR_SLOTS[0]
    unit = paused["collection"]["repair"]["units"][slot]
    assert opener.calls == [] and unit["status"] == "paused" and unit["reserved_requests"] == 0
    state = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fixture["fresh"]())["state"]
    assert state["control"]["state"] == "paused" and state["control"]["retry"] is None
    assert state["capture"]["pages"] == []
    restored = restore_market_collection(paused["reference"], REGISTRY, fixture["fresh"]())
    assert restored["collection"]["repair"]["units"][slot] == unit
    repeated = repair_run(fixture, previous=paused["reference"], opener=opener,
        pause_requested=lambda: "STORAGE_BUDGET", attempt_id="run-200-2-u1")
    assert repeated["collection"]["repair"]["units"][slot]["checkpoint"] == unit["checkpoint"]
    assert collection_progress_digest(repeated["collection"]) == collection_progress_digest(paused["collection"])
    assert opener.calls == []
    resumed = repair_run(fixture, previous=repeated["reference"], opener=opener)
    assert len(opener.calls) == 1 and resumed["collection"]["repair"]["units"][slot]["status"] == "complete"
    assert resumed["collection"]["repair"]["grant"] == granted["collection"]["repair"]["grant"]


@pytest.mark.parametrize("lost_phase", ["in-flight", "complete"])
def test_unknown_checkpoint_write_reconciles_the_same_chain_without_source_replay(lost_phase):
    import json
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    backend, original_commit = fixture["backend"], fixture["backend"].commit
    lost = []

    def commit(files, *, expected):
        head = original_commit(files, expected=expected)
        for path, raw in files.items():
            if not path.endswith("/content.txt"):
                continue
            body = json.loads(raw)
            if body.get("schema") != CHECKPOINT_SCHEMA or body.get("checkpoint_id") != repair_checkpoint_id(REPAIR_SLOTS[0]):
                continue
            control = body["control"]
            phase = "complete" if control["state"] == "complete" else (control["retry"] or {}).get("phase")
            if phase == lost_phase and not lost:
                lost.append(head)
                raise UnknownWrite("SYNTHETIC_LOST_CHECKPOINT_RESPONSE")
        return head

    backend.commit = commit
    opener = CollectionOpener()
    with pytest.raises(CollectionSyncInterrupted) as interrupted:
        repair_run(fixture, previous=granted["reference"], opener=opener)
    assert len(opener.calls) == (0 if lost_phase == "in-flight" else 1)
    assert interrupted.value.receipt["repair"]["reserved_requests"] == 1
    backend.commit = original_commit
    latest = latest_market_reference(fixture["fresh"](), REGISTRY, collection_id="cn-eod")
    resumed = repair_run(fixture, previous=latest, opener=opener, attempt_id="run-999-2-u7")
    assert len(opener.calls) == (0 if lost_phase == "in-flight" else 1)
    assert resumed["receipt"]["repair"]["reserved_requests"] == 1
    if lost_phase == "in-flight":
        assert resumed["receipt"]["latest_attempt"]["reason_code"] == "RELAY_CHECKPOINT_INFLIGHT"
        assert resumed["receipt"]["latest_attempt"]["status"] == "blocked"
    else:
        assert resumed["collection"]["repair"]["units"][REPAIR_SLOTS[0]]["status"] == "complete"


def test_two_pages_charge_each_attempt_once_and_maximum_additional_requests_is_32():
    fixture = repair_fixture()
    current = repair_run(fixture, activate=True)
    opener = CollectionOpener(count=4998)  # 5001 synthetic whole-market rows, two pages.
    attempts = {}

    def succeed_fourth(api, parameters):
        key = (parameters["trade_date"], api, parameters.get("offset", "0"))
        attempts[key] = attempts.get(key, 0) + 1
        if attempts[key] < 4:
            raise TimeoutError("SYNTHETIC_FINITE_PAGE_RETRY")

    opener.before = succeed_fourth
    for index, slot in enumerate(REPAIR_SLOTS, 1):
        current = repair_run(fixture, previous=current["reference"], opener=opener, attempt_id=f"run-200-1-u{index}")
        unit = current["collection"]["repair"]["units"][slot]
        assert unit["status"] == "complete" and unit["reserved_requests"] == 8
        state = restore_market_checkpoint(unit["checkpoint"], REGISTRY, fixture["fresh"]())["state"]
        assert len(state["capture"]["pages"]) == 2 and checkpoint_reserved_requests(state) == 8
    assert len(opener.calls) == 32 and set(attempts.values()) == {4}
    assert current["status"] == "repair-complete" and current["receipt"]["repair"]["reserved_requests"] == 32
    assert current["receipt"]["repair"]["original_requests"] == 16


def test_generation_cannot_clear_pending_grant_or_reissue_finished_grant():
    fixture = repair_fixture(only_three_sessions=True)
    granted = repair_run(fixture, activate=True)
    next_plan = plan_collection(collection_id="cn-eod", generation="g-next", start="20260701",
        end="20261016", calendar_end="20261016", registry=REGISTRY)
    opener = CollectionOpener()
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="ASHARE_RECONCILE_REPAIR_EPISODE"):
        run(next_plan, fixture["store"], fixture["fresh"], opener, previous=granted["reference"], clock=fixture["clock"])
    assert fixture["backend"].head() == before and opener.calls == []
    done = repair_run(fixture, previous=granted["reference"], opener=opener, max_queries=4)
    assert done["receipt"]["all_partitions_retained"]
    opener = CollectionOpener()
    inherited = run(next_plan, fixture["store"], fixture["fresh"], opener,
        previous=done["reference"], clock=fixture["clock"])
    assert inherited["receipt"]["all_partitions_retained"]
    assert set(source_calls(opener)) == {("stock_basic", "SSE", 0), ("stock_basic", "SZSE", 0)}
    assert inherited["collection"]["repair"] == done["collection"]["repair"]
    assert all(effective_unit(inherited["collection"], slot)["query"] == effective_unit(done["collection"], slot)["query"]
        for slot in REPAIR_SLOTS)
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="ASHARE_REPAIR_FIXED_SCOPE"):
        repair_run(fixture, previous=inherited["reference"], plan=next_plan, opener=opener)
    assert fixture["backend"].head() == before and len(opener.calls) == 2


def test_custody_validates_replacement_dependencies_before_publishing_any_root():
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    opener = CollectionOpener(count=4998)
    paused = repair_run(fixture, previous=granted["reference"], opener=opener,
        pause_requested=lambda: "STORAGE_BUDGET" if opener.calls else None)
    slot = REPAIR_SLOTS[0]
    assert paused["collection"]["repair"]["units"][slot]["reserved_requests"] == 1
    for kind in ("count", "checkpoint", "query"):
        broken = deepcopy(paused["collection"])
        unit = broken["repair"]["units"][slot]
        if kind == "count":
            unit["reserved_requests"] = 2
        elif kind == "checkpoint":
            unit["checkpoint"] = fixture["base"]["collection"]["units"][slot]["checkpoint"]
        else:
            unit.update(status="complete", query=fixture["grant_request"]["transport_evidence"]["daily"]["query_reference"])
        before = fixture["backend"].head()
        with pytest.raises(RecordError):
            retain_market_collection(broken, REGISTRY, fixture["store"], fresh_reader=fixture["fresh"],
                predecessor=paused["reference"], custody_authorized=True)
        assert fixture["backend"].head() == before


def test_qualified_hidden_checkpoint_cannot_be_mislabeled_as_a_new_terminal(monkeypatch):
    from decision_kernel.application import market_collection_custody as custody
    fixture = repair_fixture()
    granted = repair_run(fixture, activate=True)
    original = custody.retain_market_collection

    def lose_final(collection, *args, **kwargs):
        if collection["repair"]["units"][REPAIR_SLOTS[0]]["status"] == "complete":
            raise UnknownWrite("SYNTHETIC_FINAL_ROOT_NOT_PUBLISHED")
        return original(collection, *args, **kwargs)

    monkeypatch.setattr(custody, "retain_market_collection", lose_final)
    opener = CollectionOpener()
    with pytest.raises(CollectionSyncInterrupted):
        repair_run(fixture, previous=granted["reference"], opener=opener)
    monkeypatch.setattr(custody, "retain_market_collection", original)
    reference = latest_market_reference(fixture["fresh"](), REGISTRY, collection_id="cn-eod")
    candidate = restore_market_collection(reference, REGISTRY, fixture["fresh"]())["collection"]
    checkpoint = latest_market_reference(fixture["fresh"](), REGISTRY,
        checkpoint_id=repair_checkpoint_id(REPAIR_SLOTS[0]))
    candidate["repair"]["units"][REPAIR_SLOTS[0]].update(checkpoint=checkpoint,
        reserved_requests=1, status="failed", reason_code="SYNTHETIC_FALSE_TERMINAL")
    before = fixture["backend"].head()
    with pytest.raises(RecordError, match="ASHARE_REPAIR_FALSE_TERMINAL"):
        retain_market_collection(candidate, REGISTRY, fixture["store"], fresh_reader=fixture["fresh"],
            predecessor=reference, custody_authorized=True)
    assert fixture["backend"].head() == before
    recovered = repair_run(fixture, previous=reference, opener=opener)
    assert len(opener.calls) == 1 and recovered["collection"]["repair"]["units"][REPAIR_SLOTS[0]]["status"] == "complete"
