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
    assert result["reuse"] and result["source"]["count"] == len(runtime.E["GROUPS"])


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
    monkeypatch.setattr(runtime, "git", lambda *args: pytest.fail("unknown base has no first parent to resolve"))
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] is None and plan["durations"] == {}
    assert "timing_fallback" not in plan


@pytest.fixture
def timing_route(runtime, evidence, bundle, monkeypatch, tmp_path):
    base, parent = "d" * 40, "e" * 40
    ident = dict(repo=runtime.REPO, code_sha="a" * 40, tree="b" * 40,
                 event="pull_request", run_id="300", attempt="1")
    hints = {"tests/test_example.py::test_retained": 2.5}
    responses, downloads, calls, git_calls, downloads_read = {}, {}, [], [], []

    def publish(sha, run_id, hint, *, change=None, timing_bytes=None, omit_timing=False):
        run = native_run(evidence, id=run_id, head_sha=sha, event="push", head_branch="main")
        summary = dict(identity={**ident, "code_sha": sha, "event": "push", "run_id": str(run_id)},
                       scope="full", merge_eligible=True)
        if change:
            change(summary)
        name = f"dos2-ci-{run_id}-1"
        files = {"summary.json": evidence.encoded(summary)}
        if not omit_timing:
            files["durations.json"] = evidence.encoded(hint) if timing_bytes is None else timing_bytes
        raw, meta = bundle["archive"](files, name)
        meta.update(id=run_id + 1000, workflow_run=dict(id=run_id, head_sha=sha))
        responses[runtime.run_query(sha, "push")] = dict(total_count=1, workflow_runs=[run])
        responses[f"actions/runs/{run_id}/artifacts?per_page=100"] = dict(total_count=1, artifacts=[meta])
        downloads[f"repos/{runtime.REPO}/actions/artifacts/{meta['id']}/zip"] = raw
        return meta

    parent_meta = publish(parent, 190, hints)
    responses[runtime.run_query(base, "push")] = dict(total_count=2, workflow_runs=[
        native_run(evidence, id=199, head_sha=base, event="push", head_branch="main"),
        native_run(evidence, id=200, head_sha=base, event="push", head_branch="main", conclusion="failure")])

    def api(path):
        calls.append(path)
        assert path in responses, "only the exact base and its one first parent may be queried"
        value = responses[path]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def command(*args, **kw):
        assert args[:2] == ("gh", "api") and len(args) == 3 and kw == {"token": True}
        downloads_read.append(args[2])
        assert args[2] in downloads, "an older same-head run must not be downloaded"
        return downloads[args[2]]

    def git(*args):
        git_calls.append(args)
        assert args == ("rev-parse", "--verify", base + "^1"), "only one first parent may be resolved"
        return parent

    monkeypatch.setattr(runtime, "identity", lambda: deepcopy(ident))
    monkeypatch.setattr(runtime, "changes", lambda *args: [row("CHANGELOG.md")])
    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setattr(runtime, "git", git)
    monkeypatch.setenv("CI_BASE_SHA", base)
    event = tmp_path / "event.json"
    event.write_text(json.dumps(payload(runtime.REPO, draft=False)))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs"))
    return dict(base=base, parent=parent, ident=ident, hints=hints, responses=responses, calls=calls,
                git_calls=git_calls, downloads_read=downloads_read, parent_meta=parent_meta, publish=publish)


def test_failed_exact_base_can_borrow_first_parent_timings_without_content_qualification(runtime, timing_route, tmp_path):
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    case = timing_route
    assert plan["scope"] == "full" and plan["baseline"] is None
    assert plan["reason"] == "BASE_UNAVAILABLE_NO_TRUSTED_LATEST_RUN"
    assert plan["durations"] == case["hints"] and "source" not in plan
    assert plan["timing_fallback"] == dict(base_sha=case["base"], head_sha=case["parent"], run_id=190,
        attempt=1, artifact_id=1190, digest=case["parent_meta"]["digest"], scope="full", advisory_only=True)
    assert case["git_calls"] == [("rev-parse", "--verify", case["base"] + "^1")]
    assert case["downloads_read"] == [f"repos/{runtime.REPO}/actions/artifacts/1190/zip"]


@pytest.mark.parametrize("change", [{"conclusion": "failure"}, {"status": "in_progress"}, {"run_attempt": 2}])
def test_first_parent_latest_failure_never_falls_back_to_its_older_green_or_grandparent(
        runtime, evidence, timing_route, tmp_path, change):
    case = timing_route
    query = runtime.run_query(case["parent"], "push")
    case["responses"][query] = dict(total_count=2, workflow_runs=[
        native_run(evidence, id=189, head_sha=case["parent"], event="push", head_branch="main"),
        native_run(evidence, id=190, head_sha=case["parent"], event="push", head_branch="main", **change)])
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] is None and plan["durations"] == {}
    assert plan["reason"] == "BASE_UNAVAILABLE_NO_TRUSTED_LATEST_RUN" and "timing_fallback" not in plan
    assert case["calls"] == [runtime.run_query(case["base"], "push"), query]
    assert len(case["git_calls"]) == 1 and case["downloads_read"] == []


@pytest.mark.parametrize("hint", [{"tests/test_example.py::test_retained": 2.5}, {}, {"tests/test_example.py::test_retained": -1}])
def test_qualified_exact_base_keeps_its_existing_role_without_parent_reads(runtime, timing_route, tmp_path, hint):
    case = timing_route
    case["publish"](case["base"], 200, hint)
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "content" and plan["baseline"] == dict(id=200, head_sha=case["base"])
    assert plan["durations"] == (hint if runtime.E["valid_hints"](hint) else {})
    assert "timing_fallback" not in plan and case["git_calls"] == []
    assert case["calls"] == [runtime.run_query(case["base"], "push"), "actions/runs/200/artifacts?per_page=100"]
    assert case["downloads_read"] == [f"repos/{runtime.REPO}/actions/artifacts/1200/zip"]


@pytest.mark.parametrize("fault", ["missing", "invalid-json"])
def test_qualified_base_summary_keeps_its_role_when_its_timing_file_cannot_be_read(
        runtime, timing_route, monkeypatch, tmp_path, fault):
    case = timing_route
    case["publish"](case["base"], 200, case["hints"],
                    omit_timing=fault == "missing", timing_bytes=b"{" if fault == "invalid-json" else None)
    monkeypatch.setattr(runtime, "changes", lambda *args: [row()])
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] == dict(id=200, head_sha=case["base"])
    assert plan["durations"] == {} and "timing_fallback" not in plan and case["git_calls"] == []
    assert plan["reason"] == "BASE_UNAVAILABLE_" + ("KeyError" if fault == "missing" else "JSONDecodeError")


@pytest.mark.parametrize("fault", ["digest", "summary", "hints", "empty-hints", "read"])
def test_bad_or_unavailable_parent_evidence_keeps_full_without_timing(runtime, timing_route, tmp_path, fault):
    case = timing_route
    if fault == "digest":
        case["parent_meta"]["digest"] = "sha256:" + "0" * 64
    elif fault == "summary":
        case["publish"](case["parent"], 190, case["hints"],
                        change=lambda summary: summary["identity"].update(code_sha=case["base"]))
    elif fault in ("hints", "empty-hints"):
        case["publish"](case["parent"], 190, {"tests/test_example.py::test_retained": -1} if fault == "hints" else {})
    else:
        case["responses"][runtime.run_query(case["parent"], "push")] = OSError("external body must not become qualification")
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] is None and plan["durations"] == {}
    assert plan["reason"] == "BASE_UNAVAILABLE_NO_TRUSTED_LATEST_RUN" and "timing_fallback" not in plan
    assert len(case["git_calls"]) == 1


@pytest.mark.parametrize("parent", [None, "not-a-sha", "0" * 40, "d" * 40])
def test_missing_or_invalid_first_parent_does_not_start_a_history_search(runtime, timing_route, monkeypatch, tmp_path, parent):
    def git(*args):
        assert args == ("rev-parse", "--verify", timing_route["base"] + "^1")
        if parent is None:
            raise subprocess.CalledProcessError(128, ["git", *args])
        return parent
    monkeypatch.setattr(runtime, "git", git)
    runtime.route(tmp_path)
    plan = runtime.read(tmp_path, "route.json")
    assert plan["scope"] == "full" and plan["baseline"] is None and plan["durations"] == {}
    assert "timing_fallback" not in plan
    assert timing_route["calls"] == [runtime.run_query(timing_route["base"], "push")]


def test_independent_merge_reuse_replaces_timings_and_clears_parent_attribution(
        runtime, timing_route, bundle, monkeypatch, tmp_path):
    timing_route["ident"]["event"] = "push"
    runtime.route(tmp_path)
    assert "timing_fallback" in runtime.read(tmp_path, "route.json")
    proof = dict(reuse=True, source=dict(run_id=250, head_sha="f" * 40),
                 durations={"tests/test_example.py::test_new": 4.5})
    environment = bundle["plan"]["environment"]
    runtime.write(tmp_path, "environment.json", environment)
    runtime.write(tmp_path, "reuse.json", proof)
    monkeypatch.setattr(runtime, "environment", lambda: deepcopy(environment))
    monkeypatch.setattr(runtime, "smoke", lambda out, plan, targets: plan.update(smoke_exit_code=0))
    runtime.prepare(tmp_path)
    plan = runtime.read(tmp_path, "plan.json")
    assert plan["scope"] == "merge_reuse" and plan["source"] == proof["source"]
    assert plan["durations"] == proof["durations"] and "timing_fallback" not in plan
    assert runtime.read(tmp_path, "durations-hint.json") == proof["durations"]


def test_pytest_child_has_no_read_token(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "synthetic-not-a-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-a-secret")
    def fake(argv, **kw):
        assert "GH_TOKEN" not in kw["env"] and "GITHUB_TOKEN" not in kw["env"]
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(runtime.subprocess, "run", fake)
    assert runtime.pytest_run(tmp_path, "synthetic", [], collect=True)[0] == 0


@pytest.fixture
def gate_case(runtime, evidence, bundle, monkeypatch):
    """Exercise the real gate/ZIP verifier, replacing only native I/O."""
    plan = bundle["plan"]
    plan_archive = bundle["archive"]({"plan.json": evidence.encoded(plan)}, "dos2-plan-100-1")
    inventory, downloads = [], {}
    for index, (raw, metadata) in enumerate([plan_archive, *bundle["sources"].values()], 1):
        metadata = {**deepcopy(metadata), "id": index}
        inventory.append(metadata)
        downloads[f"repos/{runtime.REPO}/actions/artifacts/{index}/zip"] = raw
    payload = dict(total_count=len(inventory), artifacts=inventory)
    queries, fetched = [], []

    def api(path):
        queries.append(path)
        assert path == "actions/runs/100/artifacts?per_page=100"
        return deepcopy(payload)

    def command(*args, **kw):
        assert args[:2] == ("gh", "api") and len(args) == 3 and kw == {"token": True}
        fetched.append(args[2])
        result = downloads[args[2]]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runtime, "identity", lambda: deepcopy(plan["identity"]))
    monkeypatch.setattr(runtime, "api", api)
    monkeypatch.setattr(runtime, "command", command)
    monkeypatch.setenv("CI_NEEDS", json.dumps(dict(
        prepare=dict(result="success", outputs=dict(scope="full")),
        contracts=dict(result="success"), remaining=dict(result="success"))))
    return dict(plan=plan, inventory=inventory, payload=payload, downloads=downloads,
                queries=queries, fetched=fetched)


def test_full_gate_uses_one_inventory_and_preserves_every_original(runtime, evidence, bundle, gate_case, tmp_path):
    runtime.gate(tmp_path)
    assert gate_case["queries"] == ["actions/runs/100/artifacts?per_page=100"]
    assert gate_case["fetched"] == list(gate_case["downloads"])
    assert runtime.read(tmp_path, "durations.json") == dict.fromkeys(gate_case["plan"]["collection"], 0.01)
    assert runtime.read(tmp_path, "summary.json") == dict(identity=gate_case["plan"]["identity"],
        scope="full", merge_eligible=True, executed_full_count=len(gate_case["plan"]["collection"]), source=None)
    metadata = runtime.read(tmp_path, "sources.json")
    for group, original in bundle["sources"].items():
        assert (tmp_path / f"part-{group}.zip").read_bytes() == original[0]
        assert metadata[group] in gate_case["inventory"]
    assert evidence.verify_full(gate_case["plan"], {
        group: ((tmp_path / f"part-{group}.zip").read_bytes(), metadata[group])
        for group in evidence.GROUPS}, fresh=True) == runtime.read(tmp_path, "durations.json")


@pytest.mark.parametrize("fault, error", [
    ("truncated", "INCOMPLETE_ARTIFACTS"), ("empty", "NO_UNIQUE_ARTIFACT"),
    ("duplicate", "NO_UNIQUE_ARTIFACT"), ("attempt", "NO_UNIQUE_ARTIFACT"),
    ("head", "ARTIFACT_IDENTITY"), ("run", "ARTIFACT_IDENTITY"), ("expired", "ARTIFACT_ID"),
    ("deleted", None), ("replaced-bytes", "ARCHIVE_DIGEST"),
])
def test_gate_inventory_failure_never_reselects_or_emits_success(runtime, gate_case, tmp_path, fault, error):
    case = gate_case
    last = case["inventory"][-1]
    download = f"repos/{runtime.REPO}/actions/artifacts/{last['id']}/zip"
    if fault == "truncated":
        case["payload"]["total_count"] += 1
    elif fault == "empty":
        case["payload"].update(total_count=0, artifacts=[])
    elif fault == "duplicate":
        case["inventory"].append(deepcopy(last))
        case["payload"]["total_count"] += 1
    elif fault == "attempt":
        last["name"] = last["name"].removesuffix("-1") + "-2"
    elif fault in ("head", "run"):
        last["workflow_run"]["head_sha" if fault == "head" else "id"] = "f" * 40 if fault == "head" else 101
    elif fault == "expired":
        last["expired"] = True
    elif fault == "deleted":
        case["downloads"][download] = subprocess.CalledProcessError(1, ["gh", "api", "synthetic-deleted-id"])
    else:
        original = case["downloads"][download]
        case["downloads"][download] = original[:-1] + bytes([original[-1] ^ 1])
    with pytest.raises(subprocess.CalledProcessError if fault == "deleted" else ValueError, match=error):
        runtime.gate(tmp_path)
    assert case["queries"] == ["actions/runs/100/artifacts?per_page=100"]
    assert len(case["fetched"]) == len(set(case["fetched"]))
    assert not (tmp_path / "summary.json").exists()


def test_gate_failed_prerequisite_stops_before_inventory(runtime, gate_case, monkeypatch, tmp_path):
    monkeypatch.setenv("CI_NEEDS", json.dumps(dict(
        prepare=dict(result="success", outputs=dict(scope="full")),
        contracts=dict(result="success"), remaining=dict(result="failure"))))
    with pytest.raises(ValueError, match="REQUIRED_JOBS_NOT_PASSED"):
        runtime.gate(tmp_path)
    assert gate_case["queries"] == gate_case["fetched"] == []
