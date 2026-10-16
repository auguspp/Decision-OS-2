"""Existing host and one cold-wheel consumer; no production probabilities or network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import subprocess

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, UnknownWrite
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import odds_commands, research_runtime
from test_research_runtime import host, invoke
from test_conditional_odds import authorized, spec_payload
from test_probability_odds import (
    CODE, EVALUATED, REGISTRY_PATH, assessment_payload, build_context, market_payload,
)


def prepare_host(host, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    return build_context(store, lambda: VersionedStore(GitHubStore(host.binding, host)))


def test_probability_host_freeze_compute_save_read(host, tmp_path, monkeypatch):
    _, _, _, assessment, market = prepare_host(host, monkeypatch)
    original = host.request
    calls = {"count": 0}
    def bounded(method, path, payload=None):
        calls["count"] += 1
        assert calls["count"] <= 512, "Original shared API request budget exceeded"
        return original(method, path, payload)
    monkeypatch.setattr(host, "request", bounded)
    frozen = invoke(host, tmp_path / "freeze", "freeze-odds-assessment", authorized("assessment", assessment), "assessment")
    assert frozen["write"] == "published" and frozen["result"]["readback"]["status"] == "verified"
    calc = {"assessment_ref": frozen["result"]["reference"], "market": market, "evaluated_at": EVALUATED}
    before = host.writes
    calls["count"] = 0
    result = invoke(host, tmp_path / "compute", "calculate-probability-odds", calc)
    assert result["write"] == "not-requested" and host.writes == before
    calls["count"] = 0
    saved = invoke(host, tmp_path / "save", "save-probability-odds", authorized("calculation", calc), "saved")
    assert saved["write"] == "published" and saved["result"]["readback"]["status"] == "verified"
    calls["count"] = 0
    read = invoke(host, tmp_path / "read", "read-probability-odds", {"reference": saved["result"]["reference"]})
    assert read["result"]["result"] == result["result"] and read["result"]["recalculation"] == "matches"
    assert host.writes == before + 1


@pytest.mark.parametrize("operation", ["freeze-odds-assessment", "save-probability-odds"])
def test_lost_probability_write_reconciles_original_intent_only(host, tmp_path, monkeypatch, operation):
    _, service, _, assessment, market = prepare_host(host, monkeypatch)
    ref = service.freeze(authorized("assessment", assessment), submission_id="assessment")["reference"]
    payload = authorized("assessment", {**assessment, "assessment_id": "lost"}) if operation == "freeze-odds-assessment" else authorized(
        "calculation", {"assessment_ref": ref, "market": market, "evaluated_at": EVALUATED})
    expected = host.git.head()
    def lose(_):
        raise UnknownWrite("SIMULATED_PROBABILITY_RECEIPT_LOST")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", operation, payload, "lost-intent", expected)
    host.after_write = None
    before, head = host.writes, host.git.head()
    reconcile = "reconcile-odds-assessment" if operation == "freeze-odds-assessment" else "reconcile-probability-odds"
    restored = invoke(host, tmp_path / "reconcile", reconcile, payload, "lost-intent", expected)
    assert restored["write"] == "reconciled-without-write" and restored["result"]["readback"]["status"] == "verified"
    assert host.writes == before and host.git.head() == head
    with pytest.raises(MissingObject, match="NO_WRITE"):
        invoke(host, tmp_path / "absent", reconcile, payload, "absent-intent")
    assert host.writes == before


def test_probability_host_rights_fail_before_effect(host, tmp_path, monkeypatch):
    _, _, _, assessment, _ = prepare_host(host, monkeypatch)
    before = host.writes
    payload = authorized("assessment", assessment); payload["rights_confirmed"] = False
    with pytest.raises(RecordError, match="PUBLICATION_RIGHTS_REQUIRED"):
        invoke(host, tmp_path / "rights", "freeze-odds-assessment", payload, "bad")
    assert host.writes == before and not (tmp_path / "rights").exists()


def test_probability_routes_keep_read_write_separation():
    from pathlib import Path
    workflow = (Path(__file__).parents[1] / ".github/workflows/research-runtime.yml").read_text()
    for operation in odds_commands.PROBABILITY_WRITES:
        assert "inputs.operation != '" + operation + "'" in workflow
        assert "inputs.operation == '" + operation + "'" in workflow
        assert operation in research_runtime.WRITE_OPERATIONS
    for operation in odds_commands.PROBABILITY_READS + odds_commands.PROBABILITY_RECONCILES:
        assert operation in research_runtime.READ_OPERATIONS and operation not in research_runtime.WRITE_OPERATIONS


def exercise_installed_probability(command, outside, executable, env, store_path):
    """Extend the repository's original single wheel, preserving all prior consumers."""
    path = outside / "probability-input.json"
    def call(operation, payload, intent=None, *, reference=False, registry=False):
        path.write_text(json.dumps(payload), encoding="utf-8")
        args = [operation, "--ref" if reference else "--input", str(path)]
        if intent: args += ["--submission-id", intent]
        if registry: args += ["--registry", str(REGISTRY_PATH)]
        return command(*args)
    src = call("put-source", {"source_id": "prob-basis", "kind": "content", "title": "Synthetic basis",
        "origin": "synthetic:probability-installation", "text": "Synthetic probability evidence, not company research"}, "prob-source")
    research = call("submit-revision", {"case_id": "prob-case", "revision_id": "r1", "kind": "full", "body": "Synthetic two-world analysis",
        "mandate": "Synthetic installation", "method": "synthetic", "sources": [src], "unresolved": ["Unverified economic parameters"]}, "prob-research")
    spec = spec_payload(research); spec["security"] = {"code": CODE, "share_class": "ordinary-a"}
    down = deepcopy(spec["worlds"][0]); down.update(id="down", name="Down", value="5000", distributions=[]); spec["worlds"].append(down)
    frozen = call("freeze-valuation", authorized("spec", spec), "prob-spec")
    a = assessment_payload(frozen["reference"])
    a["as_of"] = a["policy"]["as_of"] = datetime.now(timezone.utc).isoformat()
    # Explicit synthetic historical-price policy, not a production freshness default.
    a["policy"].update(max_quote_age_days=36525, max_master_age_days=36525, max_capital_age_days=36525)
    assessment = call("freeze-odds-assessment", authorized("assessment", a), "prob-assessment")
    assert assessment["readback"]["status"] == "verified"
    store = VersionedStore(GitStore(store_path))
    market = market_payload(store, lambda: VersionedStore(GitStore(store_path)))
    calc = {"assessment_ref": assessment["reference"], "market": market, "evaluated_at": datetime.now(timezone.utc).isoformat()}
    result = call("calculate-probability-odds", calc, registry=True)
    assert result["aggregate"]["expected_holding_period_return"] == "-0.05"
    saved = call("save-probability-odds", authorized("calculation", calc), "prob-result", registry=True)
    assert saved["readback"]["status"] == "verified"
    read = call("read-probability-odds", saved["reference"], reference=True, registry=True)
    assert read["result"] == result and read["recalculation"] == "matches"
    assert command("current", "--case-id", "prob-case")["reference"] == research
    invalid = authorized("assessment", deepcopy(a)); invalid["assessment"]["probabilities"][0]["probability"] = "0.61"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    rejected = subprocess.run([str(executable), "store", "--path", str(store_path), "freeze-odds-assessment", "--input", str(path),
        "--submission-id", "prob-invalid"], cwd=outside, env=env, capture_output=True, text=True)
    assert rejected.returncode == 2 and "COMPLETE_PROBABILITY_DISTRIBUTION_REQUIRED" in rejected.stderr
