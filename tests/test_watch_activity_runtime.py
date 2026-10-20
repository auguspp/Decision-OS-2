"""Existing native-CAS host, original budget and one existing installed wheel."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import pytest

from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.git_snapshot import GitSnapshot, bundle_from_bare, verify_bundle
from decision_kernel.application.store_audit import audit_store
from decision_kernel.interfaces import research_runtime, watch_activity_commands
from test_research_runtime import host, invoke
from test_odds_watch_runtime import setup_host
from test_store_snapshot import public_api
from test_github_visibility import public_api as base_public_api
from test_watch_activity import change, query
from test_conditional_odds import authorized, price_payload
from test_odds_watch import watch_payload


def prepare(host, monkeypatch):
    store, watch, price = setup_host(host, monkeypatch)
    ref = OddsWatch(store).freeze(authorized("watch", watch), submission_id="watch")["reference"]
    return store, ref, price


def test_native_host_activity_check_and_fresh_read_under_original_budget(host, tmp_path, monkeypatch):
    store, w, price = prepare(host, monkeypatch)
    original = host.request; calls = {"n": 0}
    def bounded(method, path, payload=None):
        calls["n"] += 1
        assert calls["n"] <= 512, "Original native request budget exceeded"
        return original(method, path, payload)
    monkeypatch.setattr(host, "request", bounded)
    reg = invoke(host, tmp_path / "reg", "change-odds-watch-activity", authorized("change", change(w)), "reg")
    assert reg["result"]["readback"]["status"] == "verified"
    calls["n"] = 0
    resume = invoke(host, tmp_path / "resume", "change-odds-watch-activity",
                    authorized("change", change(w, reg["result"]["reference"], "resume", "2026-01-03T04:01:00Z")), "resume")
    assert resume["result"]["readback"]["status"] == "verified"
    calls["n"] = 0
    q = query(store, w, resume["result"]["reference"], price=price)
    saved = invoke(host, tmp_path / "check", "save-odds-watch-activity-check", authorized("check", q), "check")
    assert saved["result"]["readback"]["status"] == "verified"
    calls["n"] = 0
    before = host.writes
    read = invoke(host, tmp_path / "read", "read-odds-watch-activity", {"reference": saved["result"]["reference"]})
    assert read["result"]["event"]["result"]["status"] == "first-trigger" and host.writes == before
    calls["n"] = 0
    page = invoke(host, tmp_path / "list", "list-odds-watch-activities",
                  {"snapshot": None, "after": None, "limit": 1, "at": "2026-01-03T04:03:00Z"})
    assert page["result"]["items"][0]["summary"]["checks"] == 1


def test_lost_activity_write_reconciles_original_intent_read_only(host, tmp_path, monkeypatch):
    store, w, _ = prepare(host, monkeypatch)
    payload = authorized("change", change(w)); expected = host.git.head()
    def lose(_): raise UnknownWrite("SIMULATED_RECEIPT_LOST")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", "change-odds-watch-activity", payload, "original", expected)
    host.after_write = None
    before, head = host.writes, host.git.head()
    out = invoke(host, tmp_path / "restore", "reconcile-odds-watch-activity", payload, "original", expected)
    assert out["write"] == "reconciled-without-write" and out["result"]["readback"]["status"] == "verified"
    assert host.writes == before and host.git.head() == head


def test_rights_preflight_and_unchanged_host_permissions(host, tmp_path, monkeypatch):
    store, w, _ = prepare(host, monkeypatch)
    payload = authorized("change", change(w)); payload["rights_confirmed"] = False
    before = host.writes
    with pytest.raises(RecordError, match="RIGHTS_REQUIRED"):
        invoke(host, tmp_path / "unapproved", "change-odds-watch-activity", payload, "bad")
    assert not (tmp_path / "unapproved").exists() and host.writes == before
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/research-runtime.yml").read_text()
    for name in watch_activity_commands.WRITES:
        assert name in research_runtime.WRITE_OPERATIONS
        assert f"inputs.operation == '{name}'" in workflow and f"inputs.operation != '{name}'" in workflow
    for name in watch_activity_commands.READS + watch_activity_commands.RECONCILES:
        assert name in research_runtime.READ_OPERATIONS and name not in research_runtime.WRITE_OPERATIONS
    assert "schedule:" not in workflow


def test_activity_bundle_keeps_index_and_exact_events(public_api, tmp_path, monkeypatch):
    # Use the same real Git-backed API and pinned snapshot audit as existing custody tests.
    store, w, _ = prepare(public_api, monkeypatch)
    service = WatchActivity(store)
    ref = service.change(authorized("change", change(w)), submission_id="reg")["reference"]
    snapshot = public_api.git.head(); binding = replace(public_api.binding, write_enabled=False)
    live = audit_store(store, snapshot)
    bundle = tmp_path / "records.bundle"
    assert bundle_from_bare(public_api.git.root, binding, snapshot, bundle) == live
    assert verify_bundle(bundle, binding, snapshot) == live
    restored = WatchActivity(VersionedStore(GitSnapshot(public_api.git.root, binding, snapshot)))
    assert restored.read(ref)["event"] == service.read(ref)["event"]
    assert restored.catalogue({"snapshot": snapshot, "after": None, "limit": 1, "at": "2026-01-03T04:01:00Z"})["items"][0]["state"] == "paused"


def exercise_installed_activity(call, watch_ref, seed):
    """Extend original Watch exercise, never build a second environment/wheel."""
    now = lambda: datetime.now(timezone.utc).isoformat()
    reg = call("change-odds-watch-activity", authorized("change", change(watch_ref, at=now(), seed=seed)), "installed-activity")
    assert reg["readback"]["status"] == "verified" and reg["state"] == "paused"
    active = call("change-odds-watch-activity", authorized("change", change(watch_ref, reg["reference"], "resume", now())), "installed-resume")
    q = {"watch_ref": watch_ref, "expected_event": active["reference"], "records_snapshot": active["reference"]["commit"],
         "checked_at": now(), "input": None, "review_reasons": []}
    saved = call("save-odds-watch-activity-check", authorized("check", q), "installed-managed-check")
    assert saved["readback"]["status"] == "verified" and saved["check_status"] == "not-met"
    read = call("read-odds-watch-activity", saved["reference"], reference=True)
    assert read["recalculation"] == "matches" and read["summary"]["checks"] == 1
    pause = call("change-odds-watch-activity", authorized("change", change(watch_ref, saved["reference"], "pause", now())), "installed-pause")
    page = call("list-odds-watch-activities", {"snapshot": None, "after": None, "limit": 1, "at": now()})
    assert page["items"][0]["state"] == "paused" and page["items"][0]["event_ref"] == pause["reference"]
    from test_watch_history_runtime import exercise_installed_history
    migrated = exercise_installed_history(call, watch_ref, pause["reference"])
    retired = call("change-odds-watch-activity", authorized("change", change(watch_ref, migrated, "retire", now())), "installed-retire")
    assert retired["readback"]["status"] == "verified"
