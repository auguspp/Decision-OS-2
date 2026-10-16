"""Odds through the real-Git GitHub API model and same cold installed CLI."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import subprocess

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, MissingObject, RecordError, RevisionInput, SourceInput, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import research_runtime as runtime
from test_conditional_odds import spec_payload, price_payload, authorized
from test_research_runtime import host, invoke


def prepare(host):
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    s = store.put_source("s", SourceInput("s", "content", "Synthetic", "synthetic:test", "Not a forecast"))
    ref = store.submit_revision("r", RevisionInput("case", "r1", "full", "Synthetic", "Synthetic", "synthetic", (s,)))
    return spec_payload(asdict(ref))


def test_runtime_freeze_calculate_save_and_read_are_separate(host, tmp_path):
    spec = prepare(host)
    frozen = invoke(host, tmp_path / "freeze", "freeze-valuation", authorized("spec", spec), "freeze")
    assert frozen["write"] == "published" and frozen["result"]["readback"]["status"] == "verified"
    payload = {"spec_ref": frozen["result"]["reference"], "price": price_payload()}
    writes = host.writes
    result = invoke(host, tmp_path / "compute", "calculate-odds", payload)
    assert host.writes == writes and result["write"] == "not-requested"
    saved = invoke(host, tmp_path / "save", "save-odds", authorized("calculation", payload), "save")
    assert saved["result"]["readback"]["status"] == "verified"
    viewed = invoke(host, tmp_path / "read", "read-odds", {"reference": saved["result"]["reference"]})
    assert viewed["result"]["result"] == result["result"]
    assert viewed["result"]["recalculation"] == "matches" and host.writes == writes+1


@pytest.mark.parametrize("operation", ["freeze-valuation", "save-odds"])
def test_unknown_write_same_original_intent_recovers_readonly(host, tmp_path, operation):
    spec = prepare(host)
    frozen = invoke(host, tmp_path / "freeze", "freeze-valuation", authorized("spec", spec), "freeze")
    payload = authorized("spec", {**spec, "spec_id": "lost"}) if operation == "freeze-valuation" else authorized(
        "calculation", {"spec_ref": frozen["result"]["reference"], "price": price_payload()})
    expected = host.git.head()
    def lose(_):
        raise UnknownWrite("SIMULATED_RESPONSE_LOST")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", operation, payload, "original", expected)
    host.after_write = None
    before = host.writes; head = host.git.head()
    reconcile = "reconcile-valuation" if operation == "freeze-valuation" else "reconcile-odds"
    restored = invoke(host, tmp_path / "reconcile", reconcile, payload, "original", expected)
    assert restored["write"] == "reconciled-without-write" and host.writes == before and host.git.head() == head
    assert restored["result"]["readback"]["status"] == "verified"


def test_missing_intent_cannot_write_during_reconciliation(host, tmp_path):
    spec = prepare(host); before = host.writes
    with pytest.raises(MissingObject, match="NO_WRITE"):
        invoke(host, tmp_path / "missing", "reconcile-valuation", authorized("spec", spec), "absent")
    assert host.writes == before


def test_public_rights_and_stale_head_refused_before_effect(host, tmp_path):
    spec = prepare(host); before = host.writes
    payload = authorized("spec", spec); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="PUBLICATION_RIGHTS_REQUIRED"):
        invoke(host, tmp_path / "rights", "freeze-valuation", payload, "invalid")
    assert not (tmp_path / "rights").exists()
    with pytest.raises(Conflict, match="PLANNED_HEAD_CHANGED"):
        invoke(host, tmp_path / "stale", "freeze-valuation", authorized("spec", spec), "stale", "b"*40)
    assert host.writes == before


def test_new_routes_have_explicit_job_permissions():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/research-runtime.yml").read_text()
    for op in ("freeze-valuation", "save-odds"):
        assert "inputs.operation != '"+op+"'" in workflow
        assert "inputs.operation == '"+op+"'" in workflow
    for op in ("calculate-odds", "read-odds", "read-valuation", "reconcile-odds", "reconcile-valuation"):
        assert op in runtime.READ_OPERATIONS and op not in runtime.WRITE_OPERATIONS


def exercise_installed_odds(command, outside, python, env, executable, store_path):
    """Extends the existing single cold-wheel test; separate actual CLI processes."""
    path = outside / "odds-input.json"
    path.write_text(json.dumps({"source_id": "odds-source", "kind": "content", "title": "Synthetic",
        "origin": "synthetic:odds", "text": "Not a real investment"}), encoding="utf-8")
    source = command("put-source", "--input", str(path), "--submission-id", "odds-source")
    path.write_text(json.dumps({"case_id": "odds-case", "revision_id": "r1", "kind": "full", "body": "Synthetic",
        "mandate": "Synthetic", "method": "synthetic", "sources": [source]}), encoding="utf-8")
    research = command("submit-revision", "--input", str(path), "--submission-id", "odds-research")
    path.write_text(json.dumps(authorized("spec", spec_payload(research))), encoding="utf-8")
    frozen = command("freeze-valuation", "--input", str(path), "--submission-id", "odds-freeze")
    assert frozen["readback"]["status"] == "verified"
    refs = []
    for i, price in enumerate(("100", "80")):
        payload = {"spec_ref": frozen["reference"], "price": price_payload(price=price, price_id="price-"+str(i))}
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = command("calculate-odds", "--input", str(path))
        assert result["weighted_aggregate"] is None
        path.write_text(json.dumps(authorized("calculation", payload)), encoding="utf-8")
        saved = command("save-odds", "--input", str(path), "--submission-id", "odds-save-"+str(i))
        refs.append(saved["reference"])
        path.write_text(json.dumps(saved["reference"]), encoding="utf-8")
        assert command("read-odds", "--ref", str(path))["result"] == result
    assert refs[0] != refs[1] and command("current", "--case-id", "odds-case")["reference"] == research
    bad = {"spec_ref": frozen["reference"], "price": price_payload(share_count="101")}
    path.write_text(json.dumps(bad), encoding="utf-8")
    failed = subprocess.run([str(executable), "store", "--path", str(store_path), "calculate-odds", "--input", str(path)],
                            cwd=outside, env=env, capture_output=True, text=True)
    assert failed.returncode == 2 and "CAPITAL_CHANGE_REQUIRES_NEW_SPEC" in failed.stderr

    # Continue this same installed Odds consumer, not another wheel or store.
    from test_probability_odds_runtime import exercise_installed_probability
    exercise_installed_probability(command, outside, executable, env, store_path)

    # Same original cold wheel: finite explicit Watch, no new installation.
    from test_odds_watch_runtime import exercise_installed_watch
    exercise_installed_watch(command, outside, executable, env)
