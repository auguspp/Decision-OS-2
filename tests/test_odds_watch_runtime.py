"""Existing host/CAS/permissions and original cold installation, no live feeds."""
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
import json
import subprocess
import pytest

from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import research_runtime, watch_commands
from test_research_runtime import host, invoke
from test_odds_runtime import prepare
from test_conditional_odds import authorized, price_payload
from test_odds_watch import watch_payload, request
from test_probability_odds_runtime import prepare_host


def native_watch_fetch(host, calls):
    from test_market_snapshot import local_fetch
    from decision_kernel.infrastructure.git_store import GitStore
    fetch = local_fetch(host, calls)
    def native_fetch(binding, api, destination):
        fetch(binding, api, destination)
        # The existing GitAPI maps its declared branch to a physical synthetic
        # `records` ref. Match the real fetch's destination ref in this copy.
        if binding.branch != "records":
            head = host.git.head()
            for args in (("update-ref", "-d", "refs/heads/records", head),
                         ("update-ref", "refs/heads/" + binding.branch, head, "0" * 40)):
                subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "--git-dir=" + str(destination), *args],
                               env=GitStore._env(), capture_output=True, check=True, timeout=30)
    return native_fetch


def setup_host(host, monkeypatch, *, probability=False):
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch", native_watch_fetch(host, []))
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
    if probability:
        store, service, spec, assessment, market = prepare_host(host, monkeypatch)
        ref = service.freeze(authorized("assessment", assessment), submission_id="assessment")["reference"]
        watch = watch_payload(ref, "probability")
    else:
        spec = prepare(host)
        store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
        ref = ConditionalOdds(store).freeze(authorized("spec", spec), submission_id="spec")["reference"]
        watch, market = watch_payload(ref), price_payload()
    return store, watch, market


@pytest.mark.parametrize("probability", [False, True])
def test_watch_host_two_checks_save_and_fresh_read_with_original_budget(host, tmp_path, monkeypatch, probability):
    store, watch, market = setup_host(host, monkeypatch, probability=probability)
    original = host.request; calls = {"n": 0}
    def bounded(method, path, payload=None):
        calls["n"] += 1
        assert calls["n"] <= 512, "Original API budget exhausted"
        return original(method, path, payload)
    monkeypatch.setattr(host, "request", bounded)
    w = invoke(host, tmp_path / "watch", "freeze-odds-watch", authorized("watch", watch), "watch")
    assert w["write"] == "published" and w["result"]["readback"]["status"] == "verified"
    calls["n"] = 0
    query = request(store, w["result"]["reference"], price=market)
    preview = invoke(host, tmp_path / "preview", "check-odds-watch", query)
    assert preview["write"] == "not-requested" and preview["result"]["result"]["status"] == "first-trigger"
    calls["n"] = 0
    a = invoke(host, tmp_path / "save1", "save-odds-watch-check", authorized("check", query), "check1")
    assert a["write"] == "published" and a["result"]["readback"]["status"] == "verified"
    calls["n"] = 0
    bq = request(store, w["result"]["reference"], price=market, at="2026-01-03T04:01:00Z", previous=a["result"]["reference"])
    b = invoke(host, tmp_path / "save2", "save-odds-watch-check", authorized("check", bq), "check2")
    assert b["result"]["status"] == "continued-trigger" and b["result"]["readback"]["status"] == "verified"
    calls["n"] = 0; before = host.writes
    viewed = invoke(host, tmp_path / "read", "read-odds-watch-check", {"reference": b["result"]["reference"]})
    assert viewed["result"]["result"]["status"] == "continued-trigger" and host.writes == before
    assert viewed["result"]["recalculation"] == "matches"


@pytest.mark.parametrize("operation", ["freeze-odds-watch", "save-odds-watch-check"])
def test_lost_watch_write_reconciles_same_original_intent_read_only(host, tmp_path, monkeypatch, operation):
    store, watch, price = setup_host(host, monkeypatch)
    w = OddsWatch(store).freeze(authorized("watch", watch), submission_id="watch")["reference"]
    payload = authorized("watch", {**watch, "watch_id": "lost"}) if operation == "freeze-odds-watch" else authorized("check", request(store, w, price=price))
    expected = host.git.head()
    def lose(_): raise UnknownWrite("SIMULATED_WATCH_RECEIPT_LOST")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", operation, payload, "original", expected)
    host.after_write = None
    before, head = host.writes, host.git.head()
    reconcile = "reconcile-odds-watch" if operation == "freeze-odds-watch" else "reconcile-odds-watch-check"
    out = invoke(host, tmp_path / "restore", reconcile, payload, "original", expected)
    assert out["write"] == "reconciled-without-write" and out["result"]["readback"]["status"] == "verified"
    assert host.writes == before and host.git.head() == head
    with pytest.raises(MissingObject, match="NO_WRITE"):
        invoke(host, tmp_path / "absent", reconcile, payload, "missing")
    assert host.writes == before


def test_watch_publication_rights_checked_before_public_artifact(host, tmp_path, monkeypatch):
    _, watch, _ = setup_host(host, monkeypatch); before = host.writes
    payload = authorized("watch", watch); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="RIGHTS_REQUIRED"):
        invoke(host, tmp_path / "unapproved", "freeze-odds-watch", payload, "bad")
    assert host.writes == before and not (tmp_path / "unapproved").exists()


def test_watch_jobs_keep_existing_permissions():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/research-runtime.yml").read_text()
    for op in watch_commands.WRITES:
        assert op in research_runtime.WRITE_OPERATIONS
        assert "inputs.operation == '" + op + "'" in workflow and "inputs.operation != '" + op + "'" in workflow
    for op in watch_commands.READS + watch_commands.RECONCILES:
        assert op in research_runtime.READ_OPERATIONS and op not in research_runtime.WRITE_OPERATIONS
    assert "schedule:" not in workflow


def exercise_installed_watch(command, outside, executable, env):
    """Same installed wheel. Research-only tracking needs no price or Registry."""
    path = outside / "watch-input.json"
    def call(operation, payload, intent=None, *, reference=False):
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = [operation, "--ref" if reference else "--input", str(path)]
        if intent: args += ["--submission-id", intent]
        return command(*args)
    research = command("current", "--case-id", "odds-case")["reference"]
    now = datetime.now(timezone.utc)
    w = watch_payload(research, "research")
    w["activation"]["as_of"] = w["window"]["starts_at"] = now.isoformat()
    w["window"]["ends_at"] = (now + timedelta(days=2)).isoformat()
    frozen = call("freeze-odds-watch", authorized("watch", w), "installed-watch")
    assert frozen["readback"]["status"] == "verified"
    # Native current's reference commit is a declared historical snapshot, not a made-up current SHA.
    req = {"watch_ref": frozen["reference"], "records_snapshot": frozen["reference"]["commit"],
           "checked_at": datetime.now(timezone.utc).isoformat(), "previous_check": None, "input": None, "review_reasons": []}
    result = call("check-odds-watch", req)
    assert result["result"]["status"] == "not-met" and result["result"]["observation"]["calculation"] is None
    saved = call("save-odds-watch-check", authorized("check", req), "installed-watch-check")
    assert saved["readback"]["status"] == "verified"
    read = call("read-odds-watch-check", saved["reference"], reference=True)
    assert read["result"] == result["result"] and read["recalculation"] == "matches"
    assert command("current", "--case-id", "odds-case")["reference"] == research

    from test_watch_activity_runtime import exercise_installed_activity
    exercise_installed_activity(call, frozen["reference"], saved["reference"])
    from test_watch_conditions_integration import exercise_installed_conditions
    exercise_installed_conditions(call, research)
