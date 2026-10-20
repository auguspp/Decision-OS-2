"""Finite explicit Watch work on real Git; no fake sender or scheduler tests."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.conditional_odds import record_artifact
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity, pointer_path
from decision_kernel.application.watch_delivery import WatchDelivery
from decision_kernel.application.watch_execution import WatchExecution, _Records
from decision_kernel.domain.records import Conflict, MissingObject, RecordError, SourceInput, StoreUnavailable, UnknownWrite, encoded
from decision_kernel.domain.research_artifacts import ARTIFACT_SCHEMA, ArtifactInput
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD
from decision_kernel.domain.watch_history import HISTORY_VERSION
from decision_kernel.domain.watch_execution import (
    NOTICE_POLICY, PLAN_SCHEMA, SLOT_SCHEMA, effects, select_slot, slot_identity, slot_window, submission_keys, validate_plan,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from test_conditional_odds import authorized, price_payload
from test_market_snapshot import native, local_fetch
from test_news_read_budget import transport
from test_odds_watch import case
from test_watch_activity import activate, apply, change
from test_watch_conditions import numeric, v2_input, watch_v2


class Clock:
    def __init__(self, at="2026-01-03T04:02:05Z"):
        self.set(at)

    def set(self, at):
        self.at = datetime.fromisoformat(at.replace("Z", "+00:00"))

    def __call__(self):
        return self.at


def plan_payload(w, **changes):
    return {"schema": PLAN_SCHEMA, "plan_id": "synthetic-finite", "watch_ref": w,
            "starts_at": "2026-01-03T04:02:00Z", "interval_seconds": 60, "slot_count": 8,
            "late_tolerance_seconds": 30, "notice_policy": NOTICE_POLICY, "effect": "record-only",
            "limitations": ["Synthetic explicit execution; no schedule, recipient or sender"], **changes}


def query(plan_ref, at="2026-01-03T04:02:05Z", price=None):
    return {"plan_ref": plan_ref, "at": at, "input": price_payload() if price is None else price,
            "review_reasons": []}


def setup(case, *, v2=False):
    store, research, spec, watch, w = case
    if v2:
        w = watch.freeze(authorized("watch", watch_v2(spec, conditions=[numeric("higher"), numeric("lower", "90")])),
                         submission_id="v2")["reference"]
    active_service, registration, active = activate((store, research, spec, watch, w))
    clock = Clock()
    svc = WatchExecution(store, clock=clock)
    plan_ref = svc.freeze(authorized("plan", plan_payload(w)), submission_id="plan")["reference"]
    return store, svc, clock, plan_ref, w, active_service, active


def execute(svc, q):
    slot = svc.reserve(authorized("execution", q))["reference"]
    check = svc.check({"slot_ref": slot})
    result = svc.finish({"slot_ref": slot})
    return slot, check, result


def delivery_change(execution, notice_id, previous=None, action="reserve-attempt", at="2026-01-03T04:03:00Z", receipt=None):
    return {"execution_ref": execution, "notice_id": notice_id, "expected_event": previous,
            "action": action, "at": at, "receipt_ref": receipt,
            "note": "Synthetic submitted state; neither sender execution nor human reading is attested"}


def receipt_artifact(store, name="receipt", body=b"Submitted claim: accepted; user read. Not authenticated."):
    return asdict(ResearchArtifacts(store).put(name, ArtifactInput.from_bytes(name, name + ".txt", "text/plain", body)))


def delete_synthetic_paths(backend, paths):
    """Model record corruption only inside this test's disposable bare store."""
    head = backend.head()
    with TemporaryDirectory(prefix="synthetic-watch-delete-") as temporary:
        index = str(Path(temporary) / "index")
        backend._run("read-tree", head, index=index)
        rows = "".join("0 " + "0" * 40 + "\t" + path + "\n" for path in paths)
        backend._run("update-index", "--index-info", data=rows.encode("ascii"), index=index)
        tree = backend._run("write-tree", index=index).stdout.decode().strip()
        commit = backend._run("commit-tree", tree, "-p", head, data=b"Synthetic missing records fixture\n").stdout.decode().strip()
    backend._advance(commit, head)
    return commit


def test_remote_atomic_reader_uses_real_records_head_instead_of_the_event_being_proved(native, monkeypatch):
    model, store = native
    base = model.git.head()
    records = _Records(store)
    event = records.put({"schema": SLOT_SCHEMA, "slot_key": "a" * 64}, "Synthetic engineering fixture",
                        "synthetic-side-atomic")
    side = event["commit"]
    tree = model.git._run("rev-parse", side + "^{tree}").stdout.decode().strip()
    # This disposable fixture models a genuine side save later admitted only
    # through a second parent. It never rewrites the application worktree.
    model.git._run("update-ref", "refs/heads/records", base, side)
    main = store.put_source("mainline", SourceInput("mainline", "content", "Synthetic mainline",
        "synthetic:mainline", "An independent original records successor")).manifest.commit
    merged = model.git._run("commit-tree", tree, "-p", main, "-p", side,
                           data=b"Synthetic side event merge fixture\n").stdout.decode().strip()
    model.git._advance(merged, main)
    api, fetches = transport(model), []
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch", local_fetch(model, fetches))
    remote = _Records(VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), api)))
    with pytest.raises(RecordError, match="WATCH_HISTORY_EVENT_OUTSIDE_FIRST_PARENT"):
        remote.verify_atomic(event, base, submission_id="synthetic-side-atomic")
    assert model.git.head() == merged and model.writes == 0
    assert 0 < api._requests == len(model.requests) < MAX_REQUESTS
    assert len(fetches) == 1 and fetches[0]["api"] is api and not fetches[0]["destination"].exists()


@pytest.mark.parametrize("field,value", [
    ("slot_count", 33), ("slot_count", True), ("interval_seconds", 0), ("late_tolerance_seconds", 60),
    ("notice_policy", "notify-everything"), ("effect", "send"), ("limitations", []),
    ("starts_at", "9999-12-31T23:59:59Z"),
])
def test_plan_is_finite_explicit_and_refuses_invalid_contract(case, field, value):
    store, _, _, _, w = case
    head = store.backend.head()
    with pytest.raises(RecordError):
        WatchExecution(store).freeze(authorized("plan", plan_payload(w, **{field: value})), submission_id="invalid")
    assert store.backend.head() == head


def test_notice_policy_has_no_default_and_original_watch_window_still_applies(case):
    store, _, _, _, w = case
    p = plan_payload(w)
    del p["notice_policy"]
    with pytest.raises(RecordError):
        WatchExecution(store).freeze(authorized("plan", p), submission_id="default")
    p = plan_payload(w, starts_at="2030-01-01T00:00:00Z")
    with pytest.raises(RecordError, match="OUTSIDE_WATCH_WINDOW"):
        WatchExecution(store).freeze(authorized("plan", p), submission_id="outside")


def test_timezone_normalization_stable_identity_and_missed_window(case):
    p = validate_plan(plan_payload(case[-1]))
    equivalent = validate_plan(plan_payload(case[-1], starts_at="2026-01-03T12:02:00+08:00"))
    assert p == equivalent and slot_identity(p, 0) == slot_identity(equivalent, 0)
    assert slot_identity(p, 0) != slot_identity(p, 1)
    assert select_slot(p, "2026-01-03T04:01:59Z")["state"] == "not-due"
    assert select_slot(p, "2026-01-03T04:02:30Z")["state"] == "due"
    missed = select_slot(p, "2026-01-03T04:02:31Z")
    assert missed["state"] == "missed" and missed["next_due_at"] == "2026-01-03T04:03:00Z"
    assert select_slot(p, "2026-01-03T04:09:31Z")["state"] == "plan-ended"


def test_default_preview_and_advance_have_no_writes(case, monkeypatch):
    store, svc, _, p, _, _, _ = setup(case)
    def forbidden(*args, **kwargs):
        pytest.fail("preview attempted a commit")
    original = store.backend.commit
    monkeypatch.setattr(store.backend, "commit", forbidden)
    preview = svc.preview(query(p))
    assert preview["next_request"]["stage"] == "reserve"
    assert not preview["effects"]["scheduler_created"] and preview["effects"]["source_requests"] == 0
    monkeypatch.setattr(store.backend, "commit", original)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    monkeypatch.setattr(store.backend, "commit", forbidden)
    assert svc.advance(slot)["next_request"]["stage"] == "check"
    assert not svc.advance(slot)["effects"]["notification_sent"]


def test_actual_observation_clock_and_each_stage_one_original_cas(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    original, writes = store.backend.commit, []
    def counted(files, *, expected):
        writes.append((deepcopy(files), expected))
        return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", counted)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    assert len(writes) == 1
    clock.set("2026-01-03T04:02:09Z")
    check = svc.advance(slot, mode="save")["receipt"]
    assert len(writes) == 2 and writes[1][1] == slot["commit"]
    check_event = svc.activity.read(check["reference"], include_current_use=False)["event"]
    assert check_event["command"]["input"]["checked_at"] == "2026-01-03T04:02:09Z"
    assert check_event["command"]["input"]["checked_at"] != svc.read_slot(slot)["record"]["reserved_at"]
    out = svc.advance(slot, mode="save")["receipt"]
    assert len(writes) == 3 and len(out["record"]["notices"]) == 1
    assert svc.advance(slot, mode="save")["state"] == "recorded" and len(writes) == 3
    assert all(any(path.startswith("submissions/") for path in batch) for batch, _ in writes)
    assert all(all(path.startswith(("research/artifacts/", "research/watch-activities/", "submissions/")) for path in batch)
               for batch, _ in writes)


def test_restart_after_check_reopens_original_result_and_research_is_unchanged(case):
    store, svc, clock, p, _, _, _ = setup(case)
    original_research = store.current("case")
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    checked = svc.check({"slot_ref": slot})
    restarted = WatchExecution(VersionedStore(GitStore(store.backend.root)), clock=clock)
    status = restarted.status(slot)
    assert status["state"] == "check-saved-intents-pending" and status["check_ref"] == checked["reference"]
    result = restarted.finish({"slot_ref": slot})
    assert restarted.read_result(result["reference"])["record"] == result["record"]
    assert restarted.status(slot)["state"] == "recorded" and store.current("case") == original_research


def test_lost_check_receipt_reconciles_original_clock_after_later_pause_and_retire(case, monkeypatch):
    store, svc, clock, p, w, activity, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    original = store.backend.commit
    def lost(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SIMULATED_LOST_CHECK_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lost)
    with pytest.raises(UnknownWrite):
        svc.check({"slot_ref": slot})
    monkeypatch.setattr(store.backend, "commit", original)
    saved = activity.current(w)["reference"]
    paused = apply(activity, change(w, saved, "pause", "2026-01-03T04:03:00Z"), "pause-after-check")
    apply(activity, change(w, paused, "retire", "2026-01-03T04:04:00Z"), "retire-after-check")
    clock.set("2026-01-04T00:00:00Z")
    head = store.backend.head()
    recovered = svc.check({"slot_ref": slot}, reconcile=True)
    assert recovered["reference"] == saved and store.backend.head() == head
    assert svc.check({"slot_ref": slot}) == recovered
    result = svc.finish({"slot_ref": slot})
    assert result["record"]["checked_at"] == "2026-01-03T04:02:05Z"


def test_lost_reservation_receipt_preserves_original_input_and_reserved_clock(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    payload = authorized("execution", query(p))
    original = store.backend.commit
    def lost(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SIMULATED_LOST_SLOT_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lost)
    with pytest.raises(UnknownWrite):
        svc.reserve(payload)
    monkeypatch.setattr(store.backend, "commit", original)
    clock.set("2026-01-03T04:04:00Z")
    head = store.backend.head()
    recovered = svc.reserve(payload, reconcile=True)
    assert recovered["reserved_at"] == "2026-01-03T04:02:05Z" and store.backend.head() == head
    changed = deepcopy(payload)
    changed["execution"]["input"]["price"] = "90"
    with pytest.raises(Conflict, match="CONTENT_CONFLICT"):
        svc.reserve(changed)


def test_absent_unknown_check_never_backfills_or_recreates_old_observation_time(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    original = store.backend.commit
    def unknown(*args, **kwargs):
        raise UnknownWrite("SIMULATED_UNKNOWN_WITHOUT_COMMIT")
    monkeypatch.setattr(store.backend, "commit", unknown)
    with pytest.raises(UnknownWrite):
        svc.check({"slot_ref": slot})
    monkeypatch.setattr(store.backend, "commit", original)
    clock.set("2026-01-03T04:02:31Z")
    head = store.backend.head()
    with pytest.raises(MissingObject, match="NO_WRITE"):
        svc.check({"slot_ref": slot}, reconcile=True)
    with pytest.raises(RecordError, match="MISSED_NO_BACKFILL"):
        svc.check({"slot_ref": slot})
    assert svc.status(slot)["state"] == "missed" and store.backend.head() == head
    assert svc.preview(query(p, "2026-01-03T04:02:31Z"))["selection"]["state"] == "missed"


@pytest.mark.parametrize("interruption", ["unrelated-record", "pause", "retire"])
def test_any_later_records_head_refuses_snapshot_refresh_and_pause_cannot_be_bypassed(case, interruption):
    store, svc, clock, p, w, activity, active = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    if interruption == "unrelated-record":
        store.put_source("later", SourceInput("later", "content", "Unrelated valid source", "synthetic:later", "Data"))
    else:
        apply(activity, change(w, active, interruption, "2026-01-03T04:02:06Z"), interruption)
    clock.set("2026-01-03T04:02:10Z")
    head = store.backend.head()
    assert svc.status(slot)["state"] == "snapshot-conflict"
    with pytest.raises(Conflict, match="SLOT_SNAPSHOT_CHANGED"):
        svc.check({"slot_ref": slot})
    alias = asdict(store.backend.reference(head, slot["path"]))
    assert svc.read_slot(alias)["reference"] == slot
    with pytest.raises(Conflict, match="SLOT_SNAPSHOT_CHANGED"):
        svc.check({"slot_ref": alias})
    assert store.backend.head() == head


def test_stable_slot_identity_rejects_caller_key_and_same_slot_new_input(case):
    store, svc, _, p, _, _, _ = setup(case)
    q = query(p)
    with pytest.raises(RecordError, match="STABLE_SUBMISSION"):
        svc.reserve(authorized("execution", q), submission_id="new-run-id")
    saved = svc.reserve(authorized("execution", q))
    assert svc.reserve(authorized("execution", q)) == saved
    q["input"]["price"] = "95"
    head = store.backend.head()
    with pytest.raises(Conflict, match="CONTENT_CONFLICT"):
        svc.reserve(authorized("execution", q))
    assert store.backend.head() == head


def test_v2_each_first_trigger_has_intent_even_when_group_continues_and_unknown_is_no_reset(case):
    _, svc, clock, p, _, _, _ = setup(case, v2=True)
    observations = [("100", ["higher"]), ("90", ["lower"]), (None, []), ("85", []), ("96", []), ("89", ["lower"])]
    notice_ids = []
    for index, (value, conditions) in enumerate(observations):
        at = f"2026-01-03T04:{index + 2:02}:05Z"
        clock.set(at)
        q = query(p, at, v2_input(price_payload(price=value) if value is not None else None))
        _, _, result = execute(svc, q)
        notices = result["record"]["notices"]
        assert [notice["condition_id"] for notice in notices] == conditions
        if index in (1, 5):
            assert notices[0]["group_status"] == "continued-trigger"
        for notice in notices:
            assert notice["state"] == "pending-transport-selection" and notice["destination"] is None
            assert notice["notice_id"] not in notice_ids
            notice_ids.append(notice["notice_id"])


def test_history_limit_is_owned_by_original_activity_and_execution_cannot_bypass(case, monkeypatch):
    store, svc, _, p, _, _, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    monkeypatch.setattr("decision_kernel.application.watch_activity.MAX_EVENTS", 3)
    head = store.backend.head()
    with pytest.raises(RecordError, match="LAST_SLOT_RESERVED_FOR_RETIRE"):
        svc.check({"slot_ref": slot})
    assert store.backend.head() == head


def test_exact_result_replay_rejects_self_consistent_forged_notice(case):
    store, svc, _, p, _, _, _ = setup(case)
    _, _, saved = execute(svc, query(p))
    damaged = deepcopy(saved["record"])
    damaged["notices"][0]["effects"]["notification_sent"] = True
    forged = _Records(store).put(damaged, "Synthetic engineering fixture", "forged-result")
    with pytest.raises(RecordError, match="RESULT_CHANGED|RESULT_JOURNAL_REQUIRED"):
        svc.read_result(forged)


def test_journal_access_failure_propagates_and_cannot_be_treated_as_absent(case, monkeypatch):
    store, svc, _, p, _, _, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    key = svc.read_slot(slot)["record"]["slot_key"]
    original = store.backend.read
    def denied(commit, path):
        if path == "submissions/watch-check-" + key + ".json":
            raise StoreUnavailable("GitHubRequestBudgetExceeded")
        return original(commit, path)
    monkeypatch.setattr(store.backend, "read", denied)
    head = store.backend.head()
    with pytest.raises(StoreUnavailable, match="GitHubRequestBudgetExceeded"):
        svc.check({"slot_ref": slot})
    assert store.backend.head() == head


def test_delivery_attempt_unknown_receipt_and_feedback_never_claim_delivery_or_user_read(case):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution = result["reference"]
    notice = result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    assert delivery.current(execution, notice)["reference"] is None
    first = delivery.change(authorized("change", delivery_change(execution, notice)), submission_id="attempt")
    assert first["state"]["status"] == "attempt-reserved-no-sender"
    unknown = delivery.change(authorized("change", delivery_change(execution, notice, first["reference"], "mark-unknown",
        "2026-01-03T04:04:00Z")), submission_id="unknown")
    ref = receipt_artifact(store)
    reported = delivery.change(authorized("change", delivery_change(execution, notice, unknown["reference"], "record-receipt",
        "2026-01-03T04:05:00Z", ref)), submission_id="receipt-record")
    assert reported["state"]["status"] == "receipt-recorded-unverified"
    feedback = receipt_artifact(store, "feedback")
    final = delivery.change(authorized("change", delivery_change(execution, notice, reported["reference"], "record-feedback",
        "2026-01-03T04:06:00Z", feedback)), submission_id="feedback-record")
    assert final["state"]["send_outcome"] == "unknown"
    assert not any(final["state"][k] for k in ("user_read_verified", "transport_delivery_verified", "automatic_retry_allowed"))
    fresh = WatchDelivery(VersionedStore(GitStore(store.backend.root)), clock=clock)
    assert fresh.read(final["reference"])["event"]["state"] == final["state"]
    assert fresh.current(execution, notice)["reference"] == final["reference"]
    head = store.backend.head()
    for action in ("reserve-attempt", "cancel"):
        with pytest.raises(RecordError, match="RETRY_NOT_ALLOWED|CANNOT_BE_CANCELLED"):
            delivery.change(authorized("change", delivery_change(execution, notice, final["reference"], action,
                "2026-01-03T04:07:00Z")), submission_id=action)
    assert store.backend.head() == head


def test_lost_delivery_receipt_replays_original_before_current_state_and_no_duplicate_attempt(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    payload = authorized("change", delivery_change(execution, notice))
    original = store.backend.commit
    def lost(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SIMULATED_LOST_ATTEMPT_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lost)
    with pytest.raises(UnknownWrite):
        delivery.change(payload, submission_id="attempt")
    monkeypatch.setattr(store.backend, "commit", original)
    original_ref = delivery.current(execution, notice)["reference"]
    delivery.change(authorized("change", delivery_change(execution, notice, original_ref, "mark-unknown",
        "2026-01-03T04:04:00Z")), submission_id="unknown")
    head = store.backend.head()
    recovered = delivery.change(payload, submission_id="attempt", reconcile=True)
    assert recovered["reference"] == original_ref and store.backend.head() == head
    assert len(delivery.read(delivery.current(execution, notice)["reference"])["history"]) == 2


def test_stale_delivery_writer_and_real_cas_race_cannot_create_two_attempts(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    payload = authorized("change", delivery_change(execution, notice))
    original = store.backend.commit
    def race(files, *, expected):
        original({"submissions/other-writer.json": encoded({"synthetic": True})}, expected=expected)
        return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", race)
    with pytest.raises(Conflict):
        delivery.change(payload, submission_id="lost-race")
    monkeypatch.setattr(store.backend, "commit", original)
    assert delivery.current(execution, notice)["reference"] is None
    delivery.change(payload, submission_id="first")
    head = store.backend.head()
    with pytest.raises(Conflict, match="STALE_EVENT"):
        delivery.change(payload, submission_id="stale-writer")
    assert store.backend.head() == head


def test_exact_missing_receipt_and_resealed_state_never_become_delivery_evidence(case):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    first = delivery.change(authorized("change", delivery_change(execution, notice)), submission_id="first")
    missing = {**execution, "path": "research/artifacts/absent/" + "a" * 64 + "/manifest.json"}
    head = store.backend.head()
    with pytest.raises(MissingObject):
        delivery.change(authorized("change", delivery_change(execution, notice, first["reference"], "record-receipt",
            "2026-01-03T04:04:00Z", missing)), submission_id="missing-receipt")
    assert store.backend.head() == head
    forged = deepcopy(delivery.read(first["reference"])["event"])
    forged["state"]["user_read_verified"] = True
    ref = _Records(store).put(forged, "Synthetic engineering fixture", "forged-delivery")
    with pytest.raises(RecordError, match="FORK_OR_GAP|REPLAY_MISMATCH"):
        delivery.read(ref)


def test_plan_saving_does_not_register_or_resume_a_watch(case):
    store, _, _, _, w = case
    svc = WatchExecution(store, clock=Clock())
    ref = svc.freeze(authorized("plan", plan_payload(w)), submission_id="plan-only")["reference"]
    preview = svc.preview(query(ref))
    assert preview["activity"]["state"] is None and preview["next_request"] is None
    head = store.backend.head()
    with pytest.raises(RecordError, match="NOT_REGISTERED"):
        svc.reserve(authorized("execution", query(ref)))
    assert store.backend.head() == head


def test_forged_slot_declaring_an_older_snapshot_is_not_a_valid_original_save(case):
    store, svc, _, p, _, _, active = setup(case)
    preview = svc.preview(query(p))
    plan = svc.read_plan(p)["record"]
    key = preview["slot_key"]
    record = {"schema": SLOT_SCHEMA, "plan_ref": p, "plan_sha256": plan["plan_sha256"],
        "slot_key": key, **preview["selection"]["slot"], "query": preview["query"],
        "reserved_at": "2026-01-03T04:02:05Z", "snapshot_before": preview["snapshot"],
        "expected_event": active, "effects": effects()}
    store.put_source("unrelated", SourceInput("unrelated", "content", "Unrelated intervening write", "synthetic:other", "data"))
    forged = _Records(store).put(record, "Synthetic engineering fixture", submission_keys(key)["reserve"])
    with pytest.raises(RecordError, match="EVENT_PARENT_MISMATCH"):
        svc.read_slot(forged)
    head = store.backend.head()
    with pytest.raises(RecordError, match="EVENT_PARENT_MISMATCH"):
        svc.check({"slot_ref": forged})
    assert store.backend.head() == head


def test_delivery_declared_old_snapshot_cannot_hide_a_later_real_parent(case):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    old_head = store.backend.head()
    record = delivery._project(delivery_change(execution, notice), old_head, None, None)
    receipt_artifact(store, "intervening-file")
    forged = _Records(store).put(record, "Synthetic engineering fixture", "forged-old-base")
    with pytest.raises(RecordError, match="EVENT_PARENT_MISMATCH"):
        delivery.read(forged)


@pytest.mark.parametrize("component", ["manifest", "body"])
def test_receipt_original_ref_does_not_hide_changed_components_at_attempt_snapshot(case, component):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    first = delivery.change(authorized("change", delivery_change(execution, notice)), submission_id="first")
    receipt = receipt_artifact(store)
    path = receipt["path"] if component == "manifest" else receipt["path"].rsplit("/", 1)[0] + "/content.bin"
    store.backend.commit({path: b"Synthetic mutation at the actual snapshot\n"}, expected=store.backend.head())
    head = store.backend.head()
    with pytest.raises(RecordError, match="RECEIPT_OUTSIDE_SNAPSHOT"):
        delivery.change(authorized("change", delivery_change(execution, notice, first["reference"], "record-receipt",
            "2026-01-03T04:04:00Z", receipt)), submission_id="outside-components")
    assert store.backend.head() == head


def test_receipt_on_a_future_side_parent_is_rejected_despite_global_reachability(case):
    # All branch construction below is inside this test's isolated synthetic
    # bare store, to expose a self-consistent but temporally impossible record.
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    first = delivery.change(authorized("change", delivery_change(execution, notice)), submission_id="first")
    before_receipt = store.backend.head()
    receipt = receipt_artifact(store)
    receipt_tip = store.backend.head()
    receipt_claim = delivery._receipt(receipt, receipt_tip)
    # Retain the receipt's Git object on a side parent, while its claimed event
    # is a real single-parent child of the earlier snapshot without that file.
    store.backend._run("update-ref", "refs/heads/records", before_receipt, receipt_tip)
    p = delivery_change(execution, notice, first["reference"], "record-receipt", "2026-01-03T04:04:00Z", receipt)
    event = delivery._project(p, before_receipt, first["state"], receipt_claim)
    forged = _Records(store).put(event, "Synthetic engineering fixture", "forged-future-receipt")
    event_tip = store.backend.head()
    tree = store.backend._run("rev-parse", event_tip + "^{tree}").stdout.decode().strip()
    merged = store.backend._run("commit-tree", tree, "-p", event_tip, "-p", receipt_tip,
        data=b"Synthetic merge retaining a future receipt side parent\n").stdout.decode().strip()
    store.backend._advance(merged, event_tip)
    store.backend.check_commit(receipt_tip)  # It exists and is reachable at live head.
    with pytest.raises(RecordError, match="RECEIPT_OUTSIDE_SNAPSHOT"):
        delivery.read(forged)


def test_side_branch_slot_merged_after_mainline_retirement_is_not_original_execution_history(case):
    store, svc, _, p, w, activity, active = setup(case)
    branch_base = store.backend.head()
    reserved = svc.reserve(authorized("execution", query(p)))
    slot = reserved["reference"]
    paths = [slot["path"], slot["path"].rsplit("/", 1)[0] + "/content.bin",
             "submissions/" + reserved["submission_ids"]["reserve"] + ".json"]
    retained = {path: store.backend.read(slot["commit"], path) for path in paths}
    # Synthetic competing branch: the actual records first-parent line retires
    # while the formerly active side branch contains the reservation.
    store.backend._run("update-ref", "refs/heads/records", branch_base, slot["commit"])
    retired = apply(activity, change(w, active, "retire", "2026-01-03T04:02:06Z"), "mainline-retire")
    mainline = store.backend.head()
    layered = store.backend.commit(retained, expected=mainline)
    tree = store.backend._run("rev-parse", layered + "^{tree}").stdout.decode().strip()
    merged = store.backend._run("commit-tree", tree, "-p", mainline, "-p", slot["commit"],
        data=b"Synthetic merge retaining retired mainline and side reservation\n").stdout.decode().strip()
    store.backend._advance(merged, layered)
    store.backend.check_commit(slot["commit"])
    assert activity.current(w)["reference"] == retired
    assert all(store.backend.read(merged, path) == raw for path, raw in retained.items())
    with pytest.raises(RecordError):
        svc.read_slot(slot)


def test_v1_side_check_merged_after_retirement_cannot_be_recovered_or_finished(case):
    from decision_kernel.interfaces.watch_execution_commands import verify_fresh

    store, svc, _, p, w, activity, active = setup(case)
    reserved = svc.reserve(authorized("execution", query(p)))
    slot = reserved["reference"]
    check = svc.check({"slot_ref": slot})
    check_ref = check["reference"]
    reader = lambda: VersionedStore(GitStore(store.backend.root))
    assert verify_fresh(check, "save-odds-watch-execution-check", reader)["readback"]["status"] == "verified"
    paths = [check_ref["path"], check_ref["path"].rsplit("/", 1)[0] + "/content.bin",
             "submissions/" + reserved["submission_ids"]["check"] + ".json"]
    retained = {path: store.backend.read(check_ref["commit"], path) for path in paths}
    store.backend._run("update-ref", "refs/heads/records", slot["commit"], check_ref["commit"])
    retired = apply(activity, change(w, active, "retire", "2026-01-03T04:03:00Z"), "mainline-retire")
    mainline = store.backend.head()
    layered = store.backend.commit(retained, expected=mainline)
    tree = store.backend._run("rev-parse", layered + "^{tree}").stdout.decode().strip()
    merged = store.backend._run("commit-tree", tree, "-p", mainline, "-p", check_ref["commit"],
        data=b"Synthetic retired mainline with a side-check second parent\n").stdout.decode().strip()
    store.backend._advance(merged, layered)
    assert activity.current(w)["reference"] == retired
    assert activity.read(check_ref, include_current_use=False)["event"]["result"]["status"] == "first-trigger"
    head = store.backend.head()
    with pytest.raises(RecordError):
        svc.check({"slot_ref": slot}, reconcile=True)
    with pytest.raises(RecordError):
        svc.finish({"slot_ref": slot})
    fresh = verify_fresh(check, "save-odds-watch-execution-check", reader)
    assert fresh["readback"]["status"] == "failed"
    assert fresh["readback"]["error"] == "WATCH_EXECUTION_FRESH_READBACK_FAILED"
    assert store.backend.head() == head


def test_valid_v1_check_bytes_without_atomic_activity_selection_are_not_adopted(case):
    store, svc, _, p, w, activity, active = setup(case)
    reserved = svc.reserve(authorized("execution", query(p)))
    slot = reserved["reference"]
    check = {"watch_ref": w, "expected_event": active, "checked_at": "2026-01-03T04:02:05Z",
             "records_snapshot": slot["commit"], "input": price_payload(), "review_reasons": []}
    event = activity.preview(check)["event"]
    artifact = record_artifact(ACTIVITY_RECORD, event, "Synthetic engineering fixture")
    path = artifact.path()
    body = path.rsplit("/", 1)[0] + "/content.bin"
    forged = store._save(store.backend.head(), reserved["submission_ids"]["check"],
        {"operation": "watch-activity-check", "input": authorized("check", check)}, path,
        {path: encoded({"schema": ARTIFACT_SCHEMA, "input": artifact.metadata(), "data_path": body}),
         body: artifact.raw()})
    assert activity.read(forged, include_current_use=False)["event"] == event
    assert activity.current(w)["reference"] == active
    head = store.backend.head()
    with pytest.raises(RecordError, match="CHECK_NOT_ATOMICALLY_SELECTED"):
        svc.check({"slot_ref": slot}, reconcile=True)
    assert store.backend.head() == head


def test_result_requires_original_stable_finish_journal_even_when_content_replays(case):
    store, svc, _, p, _, _, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    svc.check({"slot_ref": slot})
    slot_view = svc.read_slot(slot)
    record = svc._result(slot_view, svc._saved_check(slot_view), store.backend.head())
    copied = _Records(store).put(record, "Synthetic engineering fixture", "same-content-other-key")
    with pytest.raises(RecordError, match="RESULT_JOURNAL_REQUIRED"):
        svc.read_result(copied)


def test_lost_finish_receipt_keeps_original_snapshot_after_later_writes(case, monkeypatch):
    store, svc, clock, p, _, _, _ = setup(case)
    slot = svc.reserve(authorized("execution", query(p)))["reference"]
    svc.check({"slot_ref": slot})
    actual_finish_base = store.backend.head()
    original = store.backend.commit
    def lost(files, *, expected):
        original(files, expected=expected)
        raise UnknownWrite("SIMULATED_LOST_FINISH_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lost)
    with pytest.raises(UnknownWrite):
        svc.finish({"slot_ref": slot})
    monkeypatch.setattr(store.backend, "commit", original)
    receipt_artifact(store, "later-unrelated")
    clock.set("2026-01-04T00:00:00Z")
    head = store.backend.head()
    restored = svc.finish({"slot_ref": slot}, reconcile=True)
    assert restored["record"]["snapshot_before"] == actual_finish_base
    assert svc.finish({"slot_ref": slot}) == restored and store.backend.head() == head


def test_real_atomic_slot_cannot_claim_the_next_window_with_an_earlier_reservation_clock(case):
    store, svc, _, p, _, _, active = setup(case)
    q = svc.preview(query(p))["query"]
    plan_record = svc.read_plan(p)["record"]
    plan = plan_record["plan"]
    key = slot_identity(plan, 1)
    forged = {"schema": SLOT_SCHEMA, "plan_ref": p, "plan_sha256": plan_record["plan_sha256"],
        "slot_key": key, **slot_window(plan, 1), "query": q, "reserved_at": "2026-01-03T04:02:05Z",
        "snapshot_before": store.backend.head(), "expected_event": active, "effects": effects()}
    ref = _Records(store).put(forged, "Synthetic engineering fixture", submission_keys(key)["reserve"])
    head = store.backend.head()
    with pytest.raises(RecordError, match="SLOT_CLOCK_CHANGED"):
        svc.read_slot(ref)
    with pytest.raises(RecordError, match="SLOT_CLOCK_CHANGED"):
        svc.check({"slot_ref": ref})
    assert store.backend.head() == head


@pytest.mark.parametrize("damage", ["whole-family", "tail-event", "tail-body", "tail-body-changed"])
def test_delivery_missing_family_or_tail_never_erases_unknown_and_historical_reconcile_remains_readable(case, damage):
    store, svc, clock, p, _, _, _ = setup(case)
    _, _, result = execute(svc, query(p))
    execution, notice = result["reference"], result["record"]["notices"][0]["notice_id"]
    clock.set("2026-01-03T05:00:00Z")
    delivery = WatchDelivery(store, clock=clock)
    first = delivery.change(authorized("change", delivery_change(execution, notice)), submission_id="attempt")
    unknown_payload = authorized("change", delivery_change(execution, notice, first["reference"], "mark-unknown",
        "2026-01-03T04:04:00Z"))
    unknown = delivery.change(unknown_payload, submission_id="unknown")
    ref = unknown["reference"]
    body = ref["path"].rsplit("/", 1)[0] + "/content.bin"
    if damage == "tail-body-changed":
        paths = []
        head = store.backend.commit({body: b"Synthetic changed current delivery state bytes\n"}, expected=store.backend.head())
    elif damage == "whole-family":
        prefix = "research/artifacts/watch-delivery-" + notice + "/"
        paths = [path for path in store.backend.paths(store.backend.head(), "research/artifacts/") if path.startswith(prefix)]
    else:
        paths = [body] if damage == "tail-body" else [ref["path"], body]
    if damage != "tail-body-changed":
        head = delete_synthetic_paths(store.backend, paths)
    assert delivery.read(ref)["event"]["state"]["send_outcome"] == "unknown"
    recovered = delivery.change(unknown_payload, submission_id="unknown", reconcile=True)
    assert recovered == unknown and store.backend.head() == head
    with pytest.raises(RecordError, match="HISTORY_MISSING_FILES|TARGET_CHANGED"):
        delivery.current(execution, notice)
    with pytest.raises(RecordError, match="HISTORY_MISSING_FILES|TARGET_CHANGED"):
        delivery.change(authorized("change", delivery_change(execution, notice, None, "reserve-attempt",
            "2026-01-03T04:05:00Z")), submission_id="new-attempt-after-deletion")
    assert store.backend.head() == head


@pytest.mark.parametrize("stage", ["reserve", "check", "finish"])
def test_missing_stable_journal_is_not_never_submitted_and_does_not_recreate_a_clock(case, stage):
    store, svc, clock, p, _, _, _ = setup(case)
    payload = authorized("execution", query(p))
    reserved = svc.reserve(payload)
    slot = reserved["reference"]
    check = svc.check({"slot_ref": slot}) if stage != "reserve" else None
    result = svc.finish({"slot_ref": slot}) if stage == "finish" else None
    path = "submissions/" + reserved["submission_ids"][stage] + ".json"
    head = delete_synthetic_paths(store.backend, [path])
    clock.set("2026-01-03T04:02:10Z")
    assert svc.read_slot(slot)["record"]["reserved_at"] == "2026-01-03T04:02:05Z"
    if result:
        assert svc.read_result(result["reference"])["record"] == result["record"]
    elif check:
        assert svc.activity.read(check["reference"], include_current_use=False)["event"]["result"]["status"] == "first-trigger"
    operation = (lambda reconcile: svc.reserve(payload, reconcile=reconcile)) if stage == "reserve" else (
        lambda reconcile: (svc.check if stage == "check" else svc.finish)({"slot_ref": slot}, reconcile=reconcile))
    for reconcile in (False, True):
        with pytest.raises(RecordError, match="JOURNAL_MISSING_FROM_HISTORY"):
            operation(reconcile)
    assert store.backend.head() == head


def test_deleted_and_reintroduced_slot_journal_cannot_replace_original_reservation(case):
    store, svc, clock, p, _, _, _ = setup(case)
    payload = authorized("execution", query(p))
    original = svc.reserve(payload)
    old_record = svc.read_slot(original["reference"])["record"]
    key = original["submission_ids"]["reserve"]
    delete_synthetic_paths(store.backend, ["submissions/" + key + ".json"])
    forged = {**old_record, "reserved_at": "2026-01-03T04:02:10Z", "snapshot_before": store.backend.head()}
    replacement = _Records(store).put(forged, "Synthetic engineering fixture", key)
    clock.set("2026-01-03T04:02:10Z")
    head = store.backend.head()
    assert svc.read_slot(original["reference"])["record"] == old_record
    with pytest.raises(RecordError, match="JOURNAL_HISTORY_CHANGED"):
        svc.read_slot(replacement)
    with pytest.raises(RecordError, match="JOURNAL_HISTORY_CHANGED"):
        svc.reserve(payload)
    assert store.backend.head() == head


def test_executor_does_not_adopt_a_v1_active_prefix_restarted_after_retirement_and_pointer_deletion(case):
    store, svc, clock, p, w, activity, active = setup(case)
    key = activity.current(w)["watch_key"]
    apply(activity, change(w, active, "retire", "2026-01-03T04:03:00Z"), "retire-before-reset")
    delete_synthetic_paths(store.backend, [pointer_path(key)])
    registered = apply(activity, change(w, None, "register", "2026-01-03T04:04:00Z"), "legacy-register-after-deletion")
    reset = apply(activity, change(w, registered, "resume", "2026-01-03T04:05:00Z"), "legacy-resume-after-deletion")
    # The original v1 reader keeps its old historical semantics. New execution
    # is an explicit adoption and has a stronger native-prefix qualification.
    assert activity.read(reset, include_current_use=False)["event"]["state"] == "active"
    clock.set("2026-01-03T04:06:05Z")
    head = store.backend.head()
    with pytest.raises(RecordError):
        svc.reserve(authorized("execution", query(p, "2026-01-03T04:06:05Z")))
    assert store.backend.head() == head


def test_executor_qualifies_the_selected_snapshot_even_when_old_v1_active_prefix_is_valid(case):
    store, svc, clock, p, w, activity, active = setup(case)
    base = store.backend.head()
    side = receipt_artifact(store, "synthetic-side-original")["commit"]
    tree = store.backend._run("rev-parse", side + "^{tree}").stdout.decode().strip()
    store.backend._run("update-ref", "refs/heads/records", base, side)
    apply(activity, change(w, active, "retire", "2026-01-03T04:03:00Z"), "retired-on-mainline")
    main = store.backend.head()
    merged = store.backend._run("commit-tree", tree, "-p", main, "-p", side,
        data=b"Synthetic merged selection retaining old active side tree\n").stdout.decode().strip()
    store.backend._advance(merged, main)
    # Exact active event still has a genuine original first-parent prefix. The
    # new adoption must also qualify the snapshot that now claims to select it.
    assert activity.read(active, include_current_use=False)["event"]["state"] == "active"
    assert activity.current(w)["reference"] == active
    clock.set("2026-01-03T04:04:05Z")
    q = query(p, "2026-01-03T04:04:05Z")
    with pytest.raises(RecordError):
        svc.preview(q)
    with pytest.raises(RecordError):
        svc.reserve(authorized("execution", q))
    assert store.backend.head() == merged


def test_execution_continues_a_migrated_v2_activity_through_original_check_and_result(case):
    store, svc, clock, p, w, activity, active = setup(case, v2=True)
    original_research = store.current("case")
    migrated = apply(activity, {**change(w, active, "migrate", "2026-01-03T04:01:30Z"),
                               "history_version": HISTORY_VERSION}, "adopt-history-for-execution")
    assert activity.read(migrated, include_current_use=False)["event"]["schema"] == "watch-activity-event-v2"
    q = query(p, price=v2_input(price_payload()))
    preview = svc.preview(q)
    assert preview["activity"]["reference"] == migrated and preview["next_request"]["stage"] == "reserve"
    slot, checked, result = execute(svc, q)
    check_event = activity.read(checked["reference"], include_current_use=False)["event"]
    assert check_event["schema"] == "watch-activity-event-v2"
    assert check_event["command"]["input"]["expected_event"] == migrated
    assert check_event["result"]["schema"] == "odds-watch-check-v2"
    assert [row["condition_id"] for row in result["record"]["notices"]] == ["higher"]
    head = store.backend.head()
    fresh = WatchExecution(VersionedStore(GitStore(store.backend.root)), clock=clock)
    assert fresh.read_result(result["reference"])["record"] == result["record"]
    assert fresh.status(slot)["state"] == "recorded"
    assert store.backend.head() == head and store.current("case") == original_research
