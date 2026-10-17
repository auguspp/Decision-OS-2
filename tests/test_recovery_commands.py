"""Original bundle and original reading-room consumption, never a recovery writer."""
from argparse import Namespace
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import patch

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, digest, encoded
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore
from decision_kernel.infrastructure.git_snapshot import bundle_from_bare
from decision_kernel.interfaces.recovery_commands import recover_room
from decision_kernel.interfaces.store_runtime import main
from test_store_snapshot import public_api, base_public_api

STAMP = "2026-10-16T12:00:00Z"


def make_case(api, directory):
    from decision_kernel.application.brief_delivery import build_edition
    from decision_kernel.application.news_rolling_index import rolling_index
    from decision_kernel.application.research_artifacts import ResearchArtifacts
    from decision_kernel.domain.research_artifacts import ArtifactInput
    from decision_kernel.observations.news_windows import newsnow_window, combine
    store = VersionedStore(GitHubStore(api.binding, api))
    with patch("decision_kernel.application.versioned_store._now", lambda: STAMP):
        source = store.put_source("input", SourceInput("input", "content", "Synthetic recovery evidence",
            "synthetic:recovery", "Original synthetic evidence.\n"))
        revision = store.submit_revision("research", RevisionInput("recovery-case", "v1", "quick",
            "Original synthetic research.\n", "Synthetic original question", "synthetic", (source,)))
        raw = encoded({"status": "success", "id": "cls", "items": [
            {"id": "recovery", "title": "Synthetic retained news", "url": "https://www.cls.cn/detail/123"}]})
        batch = combine([newsnow_window(raw, "cls", observed_at="2026-10-16T10:00:00Z")])
        batch_ref = store.put_source("batch", SourceInput("batch", "content", "Synthetic batch", "synthetic:batch", encoded(batch).decode()))
        window = rolling_index(store.read_source, [batch_ref], as_of=STAMP)
        window_ref = store.put_source("window", SourceInput("window", "content", "Synthetic window", "synthetic:window", encoded(window).decode()))
        artifacts = ResearchArtifacts(store)
        doc, _ = build_edition(store.read_source, artifacts, {"window": asdict(window_ref), "cutoff": STAMP, "kind": "adhoc"})
        brief = artifacts.put("brief", ArtifactInput.from_bytes("brief", "edition.json", "application/json", encoded(doc)))
    binding, snapshot = replace(api.binding, write_enabled=False), api.git.head()
    bundle = directory / "records.bundle"
    bundle_from_bare(api.git.root, binding, snapshot, bundle)
    plan = {"as_of": STAMP, "brief": asdict(brief)}
    (directory / "binding.json").write_bytes(encoded(asdict(binding)))
    (directory / "plan.json").write_bytes(encoded(plan))
    args = Namespace(bundle=bundle, bundle_sha256=digest(bundle.read_bytes()), binding=directory / "binding.json",
                     snapshot=snapshot, plan=directory / "plan.json", out=directory / "restored", market_registry=None)
    return args, doc, revision


@pytest.fixture
def recovery(public_api, tmp_path):
    return make_case(public_api, tmp_path)


def test_independent_offline_recovery_restores_original_brief_and_research(recovery, public_api, monkeypatch):
    args, doc, revision = recovery
    def no_network(*_, **__): raise AssertionError("Offline recovery must not create a remote API")
    monkeypatch.setattr(GitHubAPI, "from_environment", no_network)
    shutil.rmtree(public_api.git.root)  # This test's owned synthetic live store, not production.
    first = recover_room(args)
    assert first["status"] == "completed" and first["audit"]["status"] == "verified"
    assert first["write"] == "not-requested" and first["source_acquisition"] is False
    result = json.loads((args.out / "room.json").read_bytes())
    assert result["brief"]["view"] == doc and result["readings"][0]["reference"] == asdict(revision)
    assert result["readings"][0]["body"] == "Original synthetic research.\n"
    assert result["catalogue"]["snapshot"] == args.snapshot
    assert first["result_sha256"] == digest((args.out / "room.json").read_bytes())
    assert first["html_sha256"] == digest((args.out / "reading-room.html").read_bytes())
    other = Namespace(**{**vars(args), "out": args.out.parent / "second"})
    assert recover_room(other)["status"] == "completed"
    assert (args.out / "room.json").read_bytes() == (other.out / "room.json").read_bytes()
    assert (args.out / "reading-room.html").read_bytes() == (other.out / "reading-room.html").read_bytes()
    assert first["failover"] == "not-performed" and first["production_backup_policy"] == "not-certified"


def test_bundle_digest_rejection_precedes_restore_and_output(recovery):
    args = recovery[0]
    args.bundle_sha256 = "0" * 64
    with pytest.raises(RecordError, match="DIGEST_MISMATCH"): recover_room(args)
    assert not args.out.exists()


def test_actual_snapshot_mismatch_is_failed_not_an_empty_room(recovery):
    args = recovery[0]
    args.snapshot = "a" * 40
    report = recover_room(args)
    assert report["status"] == "failed" and report["error"] == "BUNDLE_REF_MISMATCH"
    assert not (args.out / "room.json").exists()


def test_corrupt_bundle_with_matching_digest_cannot_pass_native_restore(recovery):
    args = recovery[0]
    args.bundle.write_bytes(args.bundle.read_bytes()[:90])
    args.bundle_sha256 = digest(args.bundle.read_bytes())
    assert recover_room(args)["status"] == "failed"
    assert not (args.out / "reading-room.html").exists()


def test_partial_component_keeps_original_research_and_nonzero_exit(recovery, capsys):
    args = recovery[0]
    plan = json.loads(args.plan.read_bytes())
    plan["brief"]["sha256"] = "0" * 64
    args.plan.write_bytes(encoded(plan))
    argv = ["recover-room", "--bundle", str(args.bundle), "--bundle-sha256", args.bundle_sha256,
            "--binding", str(args.binding), "--snapshot", args.snapshot, "--plan", str(args.plan), "--out", str(args.out)]
    assert main(argv) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "completed-with-gaps" and report["reading_status"] == "partial"
    result = json.loads((args.out / "room.json").read_bytes())
    assert result["readings"][0]["status"] == "readable" and "view" not in result["brief"]


@pytest.mark.parametrize("mutation", ["write-enabled", "invalid-plan", "duplicate-plan", "invalid-snapshot", "symlink"])
def test_invalid_input_cannot_create_output(recovery, mutation):
    args = recovery[0]
    if mutation == "write-enabled":
        value = json.loads(args.binding.read_bytes()); value["write_enabled"] = True
        args.binding.write_bytes(encoded(value))
    elif mutation == "invalid-plan": args.plan.write_bytes(encoded({"as_of": STAMP, "execute": "forbidden"}))
    elif mutation == "duplicate-plan": args.plan.write_bytes(b'{"as_of":"2026-10-16T12:00:00Z","as_of":"2026-10-16T12:00:00Z"}')
    elif mutation == "invalid-snapshot": args.snapshot = "main"
    else:
        link = args.out.parent / "link.bundle"; link.symlink_to(args.bundle); args.bundle = link
    with pytest.raises(RecordError): recover_room(args)
    assert not args.out.exists()


def test_existing_output_not_overwritten(recovery):
    args = recovery[0]
    args.out.mkdir(); sentinel = args.out / "existing"; sentinel.write_bytes(b"preserve")
    with pytest.raises(RecordError, match="NEW_OUTPUT_DIRECTORY_REQUIRED"): recover_room(args)
    assert list(args.out.iterdir()) == [sentinel] and sentinel.read_bytes() == b"preserve"


def test_consumes_the_verified_byte_copy_even_if_input_changes_after_preflight(recovery, monkeypatch):
    from decision_kernel.interfaces import recovery_commands
    args = recovery[0]
    original = recovery_commands.restored_bundle
    def replace_input(frozen, binding, snapshot):
        assert frozen != args.bundle
        args.bundle.write_bytes(b"concurrent local replacement")
        return original(frozen, binding, snapshot)
    monkeypatch.setattr(recovery_commands, "restored_bundle", replace_input)
    assert recover_room(args)["status"] == "completed"


def exercise_installed_recovery(outside, python, env, _store):
    """Extend the ONE existing installed consumer; independent synthetic recovery source."""
    from test_github_store import GitAPI
    directory = outside / "recovery-input"
    directory.mkdir()
    api = GitAPI(directory / "original.git")
    binding = replace(api.binding, branch="records", visibility="public")
    anchor = api.git.commit({"store.json": binding.marker()}, expected=api.git.head())
    api.binding = replace(binding, anchor=anchor); api.meta["private"] = False
    args, original, _ = make_case(api, directory)
    shutil.rmtree(api.git.root)
    clean = {k: v for k, v in env.items() if not any(x in k.upper() for x in ("TOKEN", "SECRET", "KEY", "GITHUB", "PYTHONPATH"))}
    command = [str(python), "-I", "-m", "decision_kernel.interfaces.store_runtime", "recover-room",
               "--bundle", str(args.bundle), "--bundle-sha256", args.bundle_sha256, "--binding", str(args.binding),
               "--snapshot", args.snapshot, "--plan", str(args.plan)]
    results = []
    for name in ("recovery-first", "recovery-second"):
        target = outside / name
        run = subprocess.run(command + ["--out", str(target)], cwd=outside, env=clean, capture_output=True, text=True, check=False)
        assert run.returncode == 0, run.stderr + run.stdout
        assert json.loads(run.stdout)["status"] == "completed"
        model = json.loads((target / "room.json").read_bytes())
        assert model["brief"]["view"] == original
        results.append(((target / "room.json").read_bytes(), (target / "reading-room.html").read_bytes()))
    assert results[0] == results[1]
