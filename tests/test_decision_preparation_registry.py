"""Installed/default preparation needs a trusted catalogue only for retained Odds."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.decision_preparation import DecisionPreparation
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import cli, decision_preparation_commands as commands
from test_conditional_odds import authorized as odds_authorized, spec_payload
from test_decision_preparation import LATER, authorized, context, existing_conditional, horizon, plan, save_revision
from test_decision_preparation_runtime import probability_plan
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH


def invoke(store, directory, capsys, operation, payload, *extra, code=0):
    path = directory / "preparation-input.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert cli.main(["store", "--path", str(store.backend.root), operation,
                     "--input", str(path), *extra]) == code
    output = capsys.readouterr()
    assert not (output.err if code == 0 else output.out)
    return json.loads(output.out if code == 0 else output.err)


def copy_registry(directory):
    """Copy the actual byte-bound catalogue and its originals into a test cwd."""
    path = directory / "docs/sources/relay-registry.json"
    metadata = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    for relative in (REGISTRY_PATH.name, metadata["manual"]["path"],
                     metadata["live_catalogue"]["path"], metadata["live_catalogue"]["receipt_path"]):
        target = path.parent / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REGISTRY_PATH.parent / relative, target)
    RelayRegistry(path)
    return path


def review_input(reference, before, after, *, at=LATER):
    return {"reference": reference, "horizon_id": "near", "at": at,
            "review": {"before": {"reference": before, "start": 1, "end": 1},
                       "after": {"reference": after, "start": 1, "end": 1},
                       "question_id": "q1", "evidence_limit": 2}}


@pytest.mark.parametrize("valuation_only", [False, True])
def test_outside_cli_no_odds_prepare_save_replay_read_and_review(context, tmp_path, monkeypatch, capsys, valuation_only):
    store, source, ref = context
    value = plan(ref)
    if valuation_only:
        frozen = ConditionalOdds(store).freeze(odds_authorized("spec", spec_payload(asdict(ref))),
                                               submission_id="valuation-only")
        value["analysis_context"]["valuation_ref"] = frozen["reference"]
    monkeypatch.chdir(tmp_path)
    assert not Path("docs").exists()
    before = store.backend.head()
    prepared = invoke(store, tmp_path, capsys, "prepare-decision", value)
    assert store.backend.head() == before and prepared["persistence"] == "not-written"
    assert prepared["document"]["analysis_context"]["odds"] is None
    assert (prepared["document"]["analysis_context"]["valuation"] is not None) is valuation_only
    saved = invoke(store, tmp_path, capsys, "save-decision-preparation", authorized(value),
                   "--submission-id", "outside-preparation")
    assert saved["readback"] == {"status": "verified", "new_reader": True, "human_confirmation": "not-established"}
    after = store.backend.head()
    assert after != before
    assert invoke(store, tmp_path, capsys, "save-decision-preparation", authorized(value),
                  "--submission-id", "outside-preparation") == saved
    reopened = invoke(store, tmp_path, capsys, "read-decision-preparation",
                      {"reference": saved["reference"], "at": LATER})
    assert reopened["document"] == prepared["document"] and reopened["replay"] == "matches"
    assert reopened["document"]["analysis_context"]["source_requests"] == 0
    assert store.backend.head() == after and store.current("case") == ref
    successor = save_revision(store, source, "r2", ref)
    before_review = store.backend.head()
    reviewed = invoke(store, tmp_path, capsys, "review-decision-preparation",
                      review_input(saved["reference"], asdict(ref), asdict(successor)))
    assert reviewed["schema"] == "decision-preparation-review-v1"
    assert reviewed["preparation"]["document"] == prepared["document"]
    assert reviewed["horizon"]["horizon_id"] == "near"
    assert reviewed["review"]["revision_relation"] == "exact-direct-successor"
    assert reviewed["review"]["before"]["reference"] == asdict(ref)
    assert reviewed["review"]["after"]["reference"] == asdict(successor)
    assert reviewed["persistence"] == "not-written" and reviewed["execution"] == "not-started"
    assert not Path("docs").exists()
    assert store.backend.head() == before_review and store.current("case") == successor


@pytest.mark.parametrize("present", [False, True])
def test_explicit_registry_remains_strict_even_without_odds(context, tmp_path, monkeypatch, capsys, present):
    store, _, ref = context
    registry = tmp_path / "explicit-registry.json"
    if present:
        registry.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    before = store.backend.head()
    rejected = invoke(store, tmp_path, capsys, "save-decision-preparation", authorized(plan(ref)),
                      "--submission-id", "invalid-explicit", "--registry", str(registry), code=2)
    assert rejected == {"error": "RELAY_REGISTRY_INVALID"}
    assert store.backend.head() == before


@pytest.fixture
def retained_probability(tmp_path, monkeypatch):
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "probability.git"))
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    value, assessment, odds_ref = probability_plan(store, fresh)
    return store, fresh, value, assessment, odds_ref


@pytest.mark.parametrize("tampered", [False, True])
def test_retained_probability_fails_closed_without_matching_default_catalogue(
        retained_probability, tmp_path, monkeypatch, capsys, tampered):
    store, _, value, _, _ = retained_probability
    expected = "RELAY_REGISTRY_INVALID"
    if tampered:
        registry = copy_registry(tmp_path)
        metadata = json.loads(registry.read_text(encoding="utf-8"))
        manual = registry.parent / metadata["manual"]["path"]
        manual.write_bytes(manual.read_bytes() + b"\nchanged after capture\n")
        expected = "RELAY_REGISTRY_MANUAL_CHANGED"
    monkeypatch.chdir(tmp_path)
    before = store.backend.head()
    rejected = invoke(store, tmp_path, capsys, "save-decision-preparation", authorized(value),
                      "--submission-id", "untrusted-default", code=2)
    assert rejected == {"error": expected} and store.backend.head() == before


def test_nonnull_conditional_odds_does_not_bypass_default_catalogue_gate(context, tmp_path, monkeypatch, capsys):
    store, _, ref = context
    _, odds, _ = existing_conditional(store, ref)
    value = plan(ref, analysis_context={"valuation_ref": None,
                                       "odds": {"kind": "conditional", "reference": odds["reference"]}})
    monkeypatch.chdir(tmp_path)
    before = store.backend.head()
    assert invoke(store, tmp_path, capsys, "prepare-decision", value, code=2) == {"error": "RELAY_REGISTRY_INVALID"}
    assert store.backend.head() == before


def test_null_latest_cannot_skip_predecessor_odds_qualification(retained_probability, tmp_path, monkeypatch, capsys):
    store, _, original, assessment, odds_ref = retained_probability
    service = DecisionPreparation(store, REGISTRY)
    first = service.save(authorized(original), submission_id="history-first")
    research = BlobRef(**original["research_ref"])
    source = SourceRef.from_dict(store.read_revision(research)["record"]["input"]["sources"][0])
    structured = save_revision(store, source, "r2", research)
    value = deepcopy(original)
    value.update(predecessor=first["reference"], prepared_at="2026-01-11T08:00:00+00:00",
                 research_ref=asdict(structured), question_ids=["q1"], horizons=[horizon()],
                 analysis_context={"valuation_ref": None, "odds": None}, revision_note="Numerical context removed from this draft")
    second = service.save(authorized(value), submission_id="history-second")
    monkeypatch.chdir(tmp_path)
    before = store.backend.head()
    read = {"reference": second["reference"], "at": LATER}
    assert invoke(store, tmp_path, capsys, "prepare-decision", value, code=2) == {"error": "RELAY_REGISTRY_INVALID"}
    assert invoke(store, tmp_path, capsys, "read-decision-preparation", read, code=2) == {"error": "RELAY_REGISTRY_INVALID"}
    restored = invoke(store, tmp_path, capsys, "read-decision-preparation", read, "--registry", str(REGISTRY_PATH))
    assert restored["document"]["analysis_context"]["odds"] is None
    assert len(restored["history"]) == 2
    prior = restored["history"][0]["document"]["analysis_context"]
    assert prior["odds"]["reference"] == odds_ref and prior["probability_assessment"]["assessment"] == assessment
    assert store.backend.head() == before
    successor = save_revision(store, source, "r3", structured)
    review = review_input(second["reference"], asdict(structured), asdict(successor))
    before_review = store.backend.head()
    assert invoke(store, tmp_path, capsys, "review-decision-preparation", review,
                  code=2) == {"error": "RELAY_REGISTRY_INVALID"}
    reviewed = invoke(store, tmp_path, capsys, "review-decision-preparation", review,
                      "--registry", str(REGISTRY_PATH))
    assert reviewed["preparation"]["document"] == restored["document"]
    assert reviewed["preparation"]["history"] == restored["history"]
    assert reviewed["review"]["revision_relation"] == "exact-direct-successor"
    assert reviewed["review"]["after"]["reference"] == asdict(successor)
    assert store.backend.head() == before_review


def test_default_save_and_fresh_reader_share_actual_validated_catalogue(retained_probability, tmp_path, monkeypatch):
    store, fresh, value, assessment, _ = retained_probability
    registry = copy_registry(tmp_path)
    monkeypatch.chdir(tmp_path)
    before, opened = store.backend.head(), []

    def fresh_after_catalogue_changes():
        opened.append(store.backend.head())
        registry.write_text("{}", encoding="utf-8")
        return fresh()

    args = SimpleNamespace(store_command="save-decision-preparation", registry=None,
                           input=tmp_path / "unused-input.json", submission_id="same-catalogue")
    receipt = commands.execute(args, store, lambda _: authorized(value), fresh_after_catalogue_changes)
    assert opened == [store.backend.head()] and opened[0] != before
    assert receipt["readback"]["status"] == "verified"
    restored = DecisionPreparation(fresh(), REGISTRY).read(receipt["reference"], at=LATER)
    assert restored["document"]["analysis_context"]["probability_assessment"]["assessment"] == assessment


def exercise_installed_decision_preparation(command, outside, store, research_ref, successor_ref):
    """Reuse one installed wheel and its already stored adjacent workpapers."""
    assert not (outside / "docs").exists()
    at = datetime.now(timezone.utc).isoformat()
    value = plan(BlobRef(**research_ref), prepared_at=at, horizons=[horizon()])
    path = outside / "decision-preparation.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    before = GitStore(store).head()
    prepared = command("prepare-decision", "--input", str(path))
    assert prepared["persistence"] == "not-written" and GitStore(store).head() == before
    assert prepared["document"]["analysis_context"]["odds"] is None
    path.write_text(json.dumps(authorized(value)), encoding="utf-8")
    saved = command("save-decision-preparation", "--input", str(path), "--submission-id", "installed-preparation")
    assert saved["readback"]["status"] == "verified" and saved["readback"]["new_reader"] is True
    after = GitStore(store).head()
    assert after != before
    assert command("save-decision-preparation", "--input", str(path), "--submission-id", "installed-preparation") == saved
    at = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps({"reference": saved["reference"], "at": at}), encoding="utf-8")
    reopened = command("read-decision-preparation", "--input", str(path))
    assert reopened["document"] == prepared["document"] and reopened["replay"] == "matches"
    assert reopened["document"]["human_confirmation"] == "not-established"
    assert reopened["document"]["formal_decision"] == "not-created"
    assert reopened["document"]["analysis_context"]["source_requests"] == 0
    path.write_text(json.dumps(review_input(saved["reference"], research_ref, successor_ref, at=at)), encoding="utf-8")
    reviewed = command("review-decision-preparation", "--input", str(path))
    assert reviewed["schema"] == "decision-preparation-review-v1"
    assert reviewed["preparation"] == reopened
    assert reviewed["horizon"]["horizon_id"] == "near"
    assert reviewed["review"]["before"]["reference"] == research_ref
    assert reviewed["review"]["after"]["reference"] == successor_ref
    assert reviewed["review"]["revision_relation"] == "exact-direct-successor"
    assert reviewed["review"]["finding_changed"] is True
    assert reviewed["persistence"] == "not-written" and reviewed["execution"] == "not-started"
    assert not (outside / "docs").exists()
    assert command("current", "--case-id", "work")["reference"] == successor_ref
    assert GitStore(store).head() == after
