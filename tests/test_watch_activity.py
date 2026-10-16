"""Lifecycle selections, atomic check adoption and auditable derived summaries."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import pytest

from decision_kernel.application.watch_activity import WatchActivity, pointer_path, verify_fresh
from decision_kernel.application.conditional_odds import ConditionalOdds, record_artifact
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, UnknownWrite, encoded
from decision_kernel.domain.watch_activity import ACTIVITY_RECORD
from test_conditional_odds import authorized, price_payload
from test_odds_watch import case, request, watch_payload


def change(w, prev=None, action="register", at="2026-01-03T04:00:00Z", seed=None):
    return {"watch_ref": w, "expected_event": prev, "action": action, "at": at,
            "reason": "Explicit synthetic lifecycle change, not a user investment decision", "seed_check": seed}


def query(store, w, prev, at="2026-01-03T04:02:00Z", price=None):
    return {"watch_ref": w, "expected_event": prev, "checked_at": at, "records_snapshot": store.backend.head(),
            "input": price, "review_reasons": []}


def apply(service, value, intent):
    return service.change(authorized("change", value), submission_id=intent)["reference"]


def activate(case):
    store, _, _, _, w = case
    svc = WatchActivity(store)
    first = apply(svc, change(w), "register")
    active = apply(svc, change(w, first, "resume", "2026-01-03T04:01:00Z"), "resume")
    return svc, first, active


def test_registered_paused_then_resume_check_pause_continue_retire(case):
    store, research, _, old, w = case
    svc, registration, ref = activate(case)
    q = query(store, w, ref, price=price_payload())
    preview = svc.preview(q)["event"]
    saved = svc.save_check(authorized("check", q), submission_id="first")
    assert svc.read(saved["reference"])["event"] == preview
    assert saved["check_status"] == "first-trigger"
    pause = apply(svc, change(w, saved["reference"], "pause", "2026-01-03T04:03:00Z"), "pause")
    paused = svc.read(pause)
    assert paused["event"]["state"] == "paused" and paused["summary"]["checks"] == 1
    before = store.backend.head()
    with pytest.raises(RecordError, match="NOT_ACTIVE"):
        svc.save_check(authorized("check", query(store, w, pause, "2026-01-03T04:04:00Z")), submission_id="blocked")
    assert store.backend.head() == before
    resumed = apply(svc, change(w, pause, "resume", "2026-01-03T04:05:00Z"), "resume2")
    later = svc.save_check(authorized("check", query(store, w, resumed, "2026-01-03T04:06:00Z", price_payload())), submission_id="continued")
    result = svc.read(later["reference"])["event"]["result"]
    assert result["status"] == "continued-trigger"
    assert result["trigger_continuity"] == "intervening-checks-unknown"
    assert result["request"]["previous_check"] == saved["reference"]
    closed = apply(svc, change(w, later["reference"], "retire", "2026-01-03T04:07:00Z"), "retire")
    assert svc.read(closed)["event"]["state"] == "retired"
    with pytest.raises(RecordError, match="RETIRED"):
        apply(svc, change(w, closed, "resume", "2026-01-03T04:08:00Z"), "invalid")
    assert store.current("case") == research
    assert not svc.read(registration)["current_activity"]["same_event"]


def test_stale_writer_and_snapshot_cannot_overwrite_pause(case):
    store, _, _, _, w = case; svc, _, active = activate(case)
    stale = query(store, w, active, price=price_payload())
    paused = apply(svc, change(w, active, "pause", "2026-01-03T04:02:00Z"), "pause")
    head = store.backend.head()
    with pytest.raises(Conflict, match="SNAPSHOT_NOT_CURRENT"):
        svc.save_check(authorized("check", stale), submission_id="stale")
    stale["records_snapshot"] = head
    with pytest.raises(Conflict, match="STALE_EVENT"):
        svc.save_check(authorized("check", stale), submission_id="stale2")
    assert svc.current(w)["reference"] == paused and store.backend.head() == head


def test_old_normal_execution_rejected_but_historical_read_kept(case):
    store, _, _, old, w = case
    original = old.save(authorized("check", request(store, w, price=price_payload())), submission_id="unmanaged")
    svc = WatchActivity(store)
    registered = apply(svc, change(w, at="2026-01-03T04:01:00Z", seed=original["reference"]), "register")
    for operation in (lambda: old.inspect(request(store, w, at="2026-01-03T04:02:00Z", price=price_payload())),
                      lambda: old.save(authorized("check", request(store, w)), submission_id="bypass")):
        with pytest.raises(RecordError, match="USE_ACTIVITY_ENTRY"): operation()
    assert old.read_check(original["reference"])["recalculation"] == "matches"
    active = apply(svc, change(w, registered, "resume", "2026-01-03T04:02:00Z"), "resume")
    r = svc.save_check(authorized("check", query(store, w, active, "2026-01-03T04:03:00Z", price_payload())), submission_id="managed")
    view = svc.read(r["reference"])
    assert view["event"]["result"]["status"] == "continued-trigger"
    assert view["summary"]["seed_control_state_retained"] and view["summary"]["checks"] == 1


def test_same_submission_recovers_original_after_later_state_and_conflicts(case):
    _, _, _, _, w = case; svc, _, active = activate(case)
    payload = authorized("change", change(w, active, "pause", "2026-01-03T04:02:00Z"))
    first = svc.change(payload, submission_id="pause")
    apply(svc, change(w, first["reference"], "retire", "2026-01-03T04:03:00Z"), "retire")
    head = svc.store.backend.head()
    assert svc.change(payload, submission_id="pause") == first
    bad = deepcopy(payload); bad["change"]["reason"] = "Different intent"
    with pytest.raises(Conflict, match="CONTENT_CONFLICT"): svc.change(bad, submission_id="pause")
    assert svc.store.backend.head() == head


@pytest.mark.parametrize("damage", ["state", "summary", "condition", "previous", "snapshot", "key"])
def test_self_consistent_hash_does_not_hide_corrupt_event(case, damage):
    store, _, _, _, w = case; svc, _, active = activate(case)
    saved = svc.save_check(authorized("check", query(store, w, active, price=price_payload())), submission_id="check")
    event = svc.read(saved["reference"])["event"]
    if damage == "state": event["state"] = "paused"
    elif damage == "summary": event["summary"]["checks"] += 1
    elif damage == "condition": event["result"]["condition_met"] = False
    elif damage == "previous": event["command"]["input"]["expected_event"] = None
    elif damage == "snapshot": event["snapshot"] = saved["reference"]["commit"]
    else: event["watch_key"] = "a" * 64
    ref = ConditionalOdds(store)._put("tampered", ACTIVITY_RECORD, event, "Synthetic corrupted fixture")
    with pytest.raises(RecordError): svc.read(asdict(ref))


@pytest.mark.parametrize("action", ["pause", "resume", "retire"])
def test_requires_registration(case, action):
    _, _, _, _, w = case
    with pytest.raises(RecordError, match="NOT_REGISTERED"):
        apply(WatchActivity(case[0]), change(w, action=action), "missing")


def test_restart_reader_and_immutable_inputs(case):
    store, _, _, _, w = case; svc, first, active = activate(case)
    q = query(store, w, active, price=price_payload()); saved = svc.save_check(authorized("check", q), submission_id="check")
    q["input"]["price"] = "999"
    view = WatchActivity(VersionedStore(store.backend)).read(saved["reference"])
    assert view["event"]["result"]["status"] == "first-trigger"
    assert verify_fresh(saved, "save-odds-watch-activity-check", lambda: VersionedStore(store.backend))["readback"]["status"] == "verified"


def test_catalogue_is_snapshot_paged_and_keeps_closed_history(case):
    store, _, spec, old, w = case; svc, _, active = activate(case)
    watch2 = old.freeze(authorized("watch", watch_payload(spec, watch_id="other-watch")), submission_id="w2")["reference"]
    apply(svc, change(watch2), "register2")
    tip = store.backend.head()
    page = {"snapshot": tip, "after": None, "limit": 1, "at": "2026-01-03T05:00:00Z"}
    a = svc.catalogue(page); assert len(a["items"]) == 1 and a["next_after"]
    b = svc.catalogue({**page, "after": a["next_after"]}); assert len(b["items"]) == 1 and not b["next_after"]
    assert a["items"][0]["watch_key"] != b["items"][0]["watch_key"]
    apply(svc, change(w, active, "retire", "2026-01-03T04:02:00Z"), "close")
    assert svc.catalogue(page) == a
    current = svc.catalogue({**page, "snapshot": None, "limit": 8})
    assert {r["state"] for r in current["items"]} == {"paused", "retired"}


@pytest.mark.parametrize("mode", ["disabled", "expired", "end-after-trigger"])
def test_resume_cannot_expand_original_enable_or_end_conditions(case, mode):
    store, _, spec, old, _ = case
    w = watch_payload(spec)
    if mode == "disabled": w["activation"]["enabled"] = False
    elif mode == "expired": w["window"]["ends_at"] = "2026-01-03T04:00:00Z"
    else: w["window"]["end_after_trigger"] = True
    ref = old.freeze(authorized("watch", w), submission_id="other")["reference"]
    seed = old.save(authorized("check", request(store, ref, price=price_payload())), submission_id="seed")["reference"] if mode == "end-after-trigger" else None
    svc = WatchActivity(store)
    reg = apply(svc, change(ref, at="2026-01-03T04:01:00Z", seed=seed), "reg")
    with pytest.raises(RecordError, match="DISABLED|WINDOW_ENDED"):
        apply(svc, change(ref, reg, "resume", "2026-01-03T04:02:00Z"), "resume")


def test_gap_unknown_not_due_and_reentry_counts_derived(case):
    store, _, _, _, w = case; svc, _, ref = activate(case)
    states = [("100", "first-trigger"), (None, "cannot-determine"), ("95", "continued-trigger"), ("101", "not-met"), ("99", "first-trigger")]
    for i, (price, expected) in enumerate(states):
        q = query(store, w, ref, f"2026-01-03T04:{i+2:02}:00Z", price_payload(price=price) if price else None)
        saved = svc.save_check(authorized("check", q), submission_id=f"check{i}"); ref = saved["reference"]
        assert saved["check_status"] == expected
    summary = svc.read(ref)["summary"]
    assert (summary["checks"], summary["first_triggers"], summary["continued_triggers"], summary["cannot_determine"], summary["not_met"]) == (5, 2, 1, 1, 1)


def test_atomic_update_has_pointer_event_and_original_journal(case, monkeypatch):
    store, _, _, _, w = case; svc = WatchActivity(store)
    original = store.backend.commit; writes = []
    def observe(files, *, expected):
        writes.append(deepcopy(files)); return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", observe)
    ref = apply(svc, change(w), "register")
    assert len(writes) == 1 and len(writes[0]) == 4
    assert ref["path"] in writes[0] and any(p.endswith("/current.json") for p in writes[0])
    assert "submissions/register.json" in writes[0]


def test_unknown_write_original_replay_not_duplicate(case, monkeypatch):
    store, _, _, _, w = case; svc = WatchActivity(store)
    original = store.backend.commit
    def lost(files, *, expected):
        original(files, expected=expected); raise UnknownWrite("SIMULATED_LOST_RECEIPT")
    monkeypatch.setattr(store.backend, "commit", lost)
    with pytest.raises(UnknownWrite): apply(svc, change(w), "register")
    head = store.backend.head(); monkeypatch.setattr(store.backend, "commit", original)
    receipt = apply(svc, change(w), "register")
    assert store.backend.head() == head and svc.current(w)["reference"] == receipt


def test_conflict_preserves_original_and_never_uses_last_writer_wins(case, monkeypatch):
    store, _, _, _, w = case; svc = WatchActivity(store); original = store.backend.commit
    def conflict(files, *, expected):
        original({"submissions/other.json": encoded({"synthetic": True})}, expected=expected)
        return original(files, expected=expected)
    monkeypatch.setattr(store.backend, "commit", conflict)
    with pytest.raises(Conflict): apply(svc, change(w), "register")
    assert svc.current(w)["reference"] is None


def test_event_limit_refuses_without_truncating_or_resetting(case, monkeypatch):
    store, _, _, _, w = case; svc, _, active = activate(case)
    monkeypatch.setattr("decision_kernel.application.watch_activity.MAX_EVENTS", 2)
    head = store.backend.head()
    with pytest.raises(RecordError, match="HISTORY_LIMIT"):
        apply(svc, change(w, active, "pause", "2026-01-03T04:02:00Z"), "too-long")
    assert store.backend.head() == head and svc.read(active)["summary"]["events"] == 2


@pytest.mark.parametrize("bad", ["false-rights", "future", "bad-time", "not-paused", "seed-late", "extra-key", "nonmatching-watch"])
def test_invalid_changes_never_publish(case, bad):
    store, _, spec, old, w = case; svc, _, active = activate(case)
    c = change(w, active, "pause", "2026-01-03T04:02:00Z")
    if bad == "future": c["at"] = "2199-01-01T00:00:00Z"
    elif bad == "bad-time": c["at"] = "2026-01-03T04:00:00Z"
    elif bad == "not-paused": c["action"] = "resume"
    elif bad == "seed-late": c["seed_check"] = active
    elif bad == "extra-key": c["enabled"] = True
    elif bad == "nonmatching-watch":
        c["watch_ref"] = old.freeze(authorized("watch", watch_payload(spec, watch_id="different")), submission_id="different")["reference"]
    p = authorized("change", c)
    if bad == "false-rights": p["rights_confirmed"] = False
    head = store.backend.head()
    with pytest.raises(RecordError): svc.change(p, submission_id="invalid")
    assert store.backend.head() == head


def test_retired_and_paused_do_not_require_new_price(case):
    store, _, _, _, w = case; svc, _, active = activate(case)
    paused = apply(svc, change(w, active, "pause", "2026-01-03T04:02:00Z"), "pause")
    for state in [paused, apply(svc, change(w, paused, "retire", "2026-01-03T04:03:00Z"), "retire")]:
        event = svc.read(state)["event"]
        assert event["result"] is None and event["summary"]["checks"] == 0


def test_unknown_reconciliation_with_no_journal_does_not_try_to_resume(case):
    store, _, _, _, w = case; svc, _, active = activate(case); head = store.backend.head()
    from decision_kernel.domain.records import MissingObject
    with pytest.raises(MissingObject, match="NO_WRITE"):
        svc.change(authorized("change", change(w, active, "pause", "2026-01-03T04:02:00Z")), submission_id="absent", reconcile=True)
    assert store.backend.head() == head


def test_current_pointer_to_different_watch_is_rejected(case):
    store, _, spec, old, w = case; svc, _, active = activate(case)
    other = old.freeze(authorized("watch", watch_payload(spec, watch_id="other")), submission_id="other")["reference"]
    event = apply(svc, change(other), "reg-other")
    key = svc.current(w)["watch_key"]
    store.backend.commit({pointer_path(key): encoded({"schema": "watch-activity-pointer-v1", "watch_key": key, "event_path": event["path"]})}, expected=store.backend.head())
    with pytest.raises(RecordError):
        svc.catalogue({"snapshot": None, "after": None, "limit": 8, "at": "2026-01-03T05:00:00Z"})


def test_end_known_without_price_and_original_projection_is_preserved(case):
    store, _, spec, old, w = case
    frozen = ConditionalOdds(store).read_spec(spec)["spec"]
    old.clock = lambda: datetime(2031, 1, 1, tzinfo=timezone.utc)
    legacy = old.check(request(store, w, at=frozen["target_date"] + "T00:00:00+08:00"))
    assert legacy["status"] == "cannot-determine"  # Preserve existing v1 historical bytes.
    svc, _, active = activate(case)
    svc.clock = old.clock
    q = query(store, w, active, frozen["target_date"] + "T00:00:00+08:00")
    out = svc.save_check(authorized("check", q), submission_id="ended")
    assert out["check_status"] == "ended" and out["state"] == "retired"


def test_pointer_rollback_to_valid_old_event_is_not_accepted(case):
    store, _, _, _, w = case; svc, registration, active = activate(case)
    key = svc.current(w)["watch_key"]
    pointer = encoded({"schema": "watch-activity-pointer-v1", "watch_key": key, "event_path": registration["path"]})
    store.backend.commit({pointer_path(key): pointer}, expected=store.backend.head())
    with pytest.raises(RecordError, match="NOT_ATOMIC"): svc.current(w)


def test_managed_resume_refuses_elapsed_fixed_horizon(case):
    store, _, spec, _, w = case
    frozen = ConditionalOdds(store).read_spec(spec)["spec"]
    service = WatchActivity(store, clock=lambda: datetime(2031, 1, 1, tzinfo=timezone.utc))
    reg = apply(service, change(w), "reg")
    with pytest.raises(RecordError, match="HORIZON_ENDED"):
        apply(service, change(w, reg, "resume", frozen["target_date"] + "T00:00:00+08:00"), "late")


def test_original_write_replay_survives_later_registration(case):
    store, _, _, old, w = case
    payload = authorized("check", request(store, w, price=price_payload()))
    first = old.save(payload, submission_id="old-saved")
    apply(WatchActivity(store), change(w, at="2026-01-03T04:01:00Z"), "register")
    head = store.backend.head()
    assert old.save(payload, submission_id="old-saved") == first
    assert store.backend.head() == head


def test_more_than_32_managed_checks_keep_original_trigger_state(case):
    store, research, _, old, _ = case
    w = old.freeze(authorized("watch", watch_payload(asdict(research), "research")), submission_id="research-only")["reference"]
    service = WatchActivity(store)
    reg = apply(service, change(w), "reg")
    ref = apply(service, change(w, reg, "resume", "2026-01-03T04:01:00Z"), "resume")
    for i in range(33):
        q = query(store, w, ref, f"2026-01-03T05:{i:02}:00Z")
        ref = service.save_check(authorized("check", q), submission_id=f"step-{i}")["reference"]
    output = WatchActivity(VersionedStore(store.backend)).read(ref)
    assert output["summary"]["checks"] == 33 and output["summary"]["not_met"] == 33
    assert output["summary"]["events"] == 35 and len(output["history"]) == 35
    assert output["event"]["result"]["last_decisive_match"] is False


def test_last_event_slot_is_reserved_for_retirement(case, monkeypatch):
    store, _, _, _, w = case; svc, _, active = activate(case)
    monkeypatch.setattr("decision_kernel.application.watch_activity.MAX_EVENTS", 3)
    with pytest.raises(RecordError, match="LAST_SLOT_RESERVED_FOR_RETIRE"):
        apply(svc, change(w, active, "pause", "2026-01-03T04:02:00Z"), "reserve-pause")
    done = apply(svc, change(w, active, "retire", "2026-01-03T04:03:00Z"), "reserve-retire")
    assert svc.read(done)["event"]["state"] == "retired"
    assert svc.read(done)["summary"]["events"] == 3
