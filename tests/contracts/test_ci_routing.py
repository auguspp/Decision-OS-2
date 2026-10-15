"""Owner: CI scope and reuse qualification; synthetic native receipts, no HTTP."""
from copy import deepcopy
import json
import subprocess

import pytest


def row(path="src/decision_kernel/example.py", **kw):
    return dict(path=path, old_mode="100644", new_mode="100644", status="M", **kw)


def payload(repo, *, draft=True, action="synchronize"):
    return dict(action=action, pull_request=dict(draft=draft, head=dict(sha="a" * 40, repo=dict(full_name=repo)),
                base=dict(sha="d" * 40, ref="main", repo=dict(full_name=repo))))


def test_only_explicit_non_normative_prose(runtime):
    assert runtime.prose(row("CHANGELOG.md"))
    for path in ("AGENTS.md", "WORKING-PROTOCOLS.md", "docs/engineering/ci-contract.md", "README.md", "other.md"):
        assert not runtime.prose(row(path))


@pytest.mark.parametrize("change", [{"status": "D"}, {"status": "R100"}, {"new_mode": "120000"}, {"path": "../CHANGELOG.md"}, {"path": "CHANGELOG.md\n"}])
def test_nonordinary_content_requires_full(runtime, change):
    assert not runtime.prose({**row("CHANGELOG.md"), **change})


def test_owned_draft_feedback_is_not_ready_validation(runtime):
    p = payload(runtime.REPO)
    assert runtime.draft_allowed([row()], p, "a" * 40, "d" * 40)
    p["action"] = "ready_for_review"
    assert not runtime.draft_allowed([row()], p, "a" * 40, "d" * 40)
    p = payload(runtime.REPO, draft=False)
    assert not runtime.draft_allowed([row()], p, "a" * 40, "d" * 40)


@pytest.mark.parametrize("path", [".github/scripts/ci-runtime.py", "requirements/dev.txt", "pyproject.toml", "tests/contracts/test_ci_evidence.py", "tests/test_ci_workflow.py", "tests/conftest.py"])
def test_policy_changes_never_get_draft_exemption(runtime, path):
    assert not runtime.draft_allowed([row(path)], payload(runtime.REPO), "a" * 40, "d" * 40)


def test_foreign_or_unknown_draft_is_not_qualified(runtime):
    assert not runtime.draft_allowed([row()], payload("someone/else"), "a" * 40, "d" * 40)
    assert not runtime.draft_allowed([], payload(runtime.REPO), "a" * 40, "d" * 40)


def native_run(evidence, **kw):
    result = dict(id=100, head_sha="a" * 40, event="pull_request", name=evidence.NAME, path=evidence.WORKFLOW,
                  head_repository=dict(full_name=evidence.REPO), run_attempt=1, status="completed", conclusion="success")
    result.update(kw)
    return result


@pytest.mark.parametrize("change", [{"conclusion": "failure"}, {"status": "in_progress"}, {"run_attempt": 2}, {"head_sha": "b" * 40}, {"head_repository": {"full_name": "other/repo"}}])
def test_latest_run_cannot_fall_back_to_older_green(evidence, change):
    rows = [native_run(evidence, id=99), native_run(evidence, **change)]
    with pytest.raises(ValueError):
        evidence.latest_run(dict(total_count=2, workflow_runs=rows), "a" * 40, "pull_request")


def test_truncated_native_list_is_not_a_proof(evidence):
    with pytest.raises(ValueError):
        evidence.latest_run(dict(total_count=2, workflow_runs=[native_run(evidence)]), "a" * 40, "pull_request")


@pytest.fixture
def reuse_case(runtime, evidence, bundle, monkeypatch):
    source_plan = bundle["plan"]
    files = {"plan.json": evidence.encoded(source_plan), "sources.json": evidence.encoded({g: meta for g, (_, meta) in bundle["sources"].items()}),
             **{f"part-{g}.zip": raw for g, (raw, _) in bundle["sources"].items()}}
    raw, meta = bundle["archive"](files, "dos2-ci-100-1")
    main = dict(identity={**source_plan["identity"], "code_sha": "c" * 40, "event": "push", "run_id": "101"}, base_sha="d" * 40, scope="full")
    pr = dict(number=15, merged=True, draft=False, merge_commit_sha="c" * 40,
              head=dict(sha="a" * 40, repo=dict(full_name=evidence.REPO)),
              base=dict(sha="d" * 40, ref="main", repo=dict(full_name=evidence.REPO)))
    responses = {f"commits/{'c'*40}/pulls?per_page=100": [pr], "pulls/15": pr,
                 runtime.run_query("a" * 40, "pull_request"): dict(total_count=1, workflow_runs=[native_run(evidence)])}
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setattr(runtime, "api", lambda path: deepcopy(responses[path]))
    monkeypatch.setattr(runtime, "artifact", lambda *args: (raw, meta))
    monkeypatch.setattr(runtime, "git", lambda *args: " ".join(["c"*40, "d"*40, "a"*40]) if args[0] == "rev-list" else "b"*40)
    monkeypatch.setattr(runtime, "command", lambda *args, **kw: b"")
    return main, source_plan["environment"], responses


def test_exact_ready_pr_full_is_reusable(runtime, reuse_case):
    plan, env, _ = reuse_case
    result = runtime.select_reuse(plan, env)
    assert result["reuse"] and result["source"]["count"] == 5


def test_policy_change_falls_back_to_real_full(runtime, reuse_case, monkeypatch):
    plan, env, _ = reuse_case
    monkeypatch.setattr(runtime, "command", lambda *args, **kw: b".github/scripts/ci-runtime.py\n")
    result = runtime.select_reuse(plan, env)
    assert not result["reuse"] and result["reason"] == "FULL_REQUIRED_CI_POLICY_CHANGED"


def test_merge_tree_difference_is_not_reusable(runtime, reuse_case, monkeypatch):
    plan, env, _ = reuse_case
    old = runtime.git
    monkeypatch.setattr(runtime, "git", lambda *args: "f"*40 if args[0] == "rev-parse" else old(*args))
    assert runtime.select_reuse(plan, env)["reason"] == "FULL_REQUIRED_MERGE_TREE_DIFFERS"


def test_reuse_requires_strict_environment(runtime, reuse_case):
    plan, env, _ = reuse_case
    changed = {**env, "image_version": "20261002.1.0"}
    assert not runtime.select_reuse(plan, changed)["reuse"]


def test_native_api_read_failure_does_not_enable_reuse(runtime, reuse_case, monkeypatch):
    plan, env, _ = reuse_case
    def fail(*args):
        raise OSError("external body not for publication")
    monkeypatch.setattr(runtime, "api", fail)
    result = runtime.select_reuse(plan, env)
    assert result["reason"] == "FULL_REQUIRED_OSError" and not result["reuse"]


def test_no_base_bootstraps_full(runtime, monkeypatch, tmp_path):
    ident = dict(repo=runtime.REPO, code_sha="a"*40, tree="b"*40, event="pull_request", run_id="100", attempt="1")
    monkeypatch.setattr(runtime, "identity", lambda: ident)
    monkeypatch.setenv("CI_BASE_SHA", "0"*40)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload(runtime.REPO, draft=False)))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    monkeypatch.setattr(runtime, "api", lambda *args: pytest.fail("unknown base must not query a fake successful run"))
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] is None and plan["durations"] == {}


def test_pytest_child_has_no_read_token(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-a-secret")
    def fake(argv, **kw):
        assert "GH_TOKEN" not in kw["env"] and "GITHUB_TOKEN" not in kw["env"]
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(runtime.subprocess, "run", fake)
    assert runtime.pytest_run(tmp_path, "synthetic", [], collect=True)[0] == 0
