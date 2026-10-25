"""Synthetic native daily approvals, without sources, credentials or records I/O."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure import daily_execution as execution

WORKFLOW_SHA = "a" * 40
EXECUTION_SHA = "b" * 40
MOVED_SHA = "c" * 40
TREE_SHA = "d" * 40
APPROVAL = "https://github.com/auguspp/Decision-OS-2/issues/221#issuecomment-12345"


def contents(path, raw):
    """Realistic native contents response with the Git blob identity of raw bytes."""
    return {"type": "file", "path": path, "encoding": "base64", "size": len(raw),
        "sha": hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest(),
        "content": base64.b64encode(raw).decode("ascii")}


class Native:
    """Reusable native fake for real admission/consumer/quota integration tests.

    ``freeze()`` runs the actual protocol and returns a bound selection.
    ``env`` then contains its original preflight envelope. ``advance()`` changes
    the native main head and copies or changes its two policy files. It never
    updates the frozen envelope or the original workflow's native head.
    """
    def __init__(self, pinned=False, public=True):
        self.calls = []
        self.workflow_sha = WORKFLOW_SHA
        self.code_sha = EXECUTION_SHA if pinned else WORKFLOW_SHA
        self.tree_sha = TREE_SHA
        self.main = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": WORKFLOW_SHA}}
        self.meta = {"id": execution.REPOSITORY_ID, "full_name": execution.REPOSITORY,
            "private": not public, "archived": False, "default_branch": "main"}
        self.selected = {"operation": "update", "invocation": "update", "event": "workflow_dispatch",
            "run_id": 8001, "run_attempt": 1, "code_sha": WORKFLOW_SHA}
        self.env = {"GITHUB_REPOSITORY": execution.REPOSITORY,
            "GITHUB_REPOSITORY_ID": str(execution.REPOSITORY_ID), "GITHUB_REF": "refs/heads/main",
            "GITHUB_WORKFLOW_REF": execution.REPOSITORY + "/" + execution.WORKFLOW + "@refs/heads/main",
            "GITHUB_WORKFLOW_SHA": WORKFLOW_SHA, "GITHUB_SHA": WORKFLOW_SHA,
            "GITHUB_RUN_ID": "8001", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "workflow_dispatch"}
        self.run = {"id": 8001, "head_sha": WORKFLOW_SHA, "head_branch": "main",
            "event": "workflow_dispatch", "run_attempt": 1, "path": execution.WORKFLOW,
            "name": "decision-os-daily-market", "display_title": "decision-os-daily-market",
            "status": "in_progress", "repository": {"id": execution.REPOSITORY_ID},
            "head_repository": {"id": execution.REPOSITORY_ID}, "created_at": "2026-10-20T10:35:00Z"}
        self.policies = {WORKFLOW_SHA: {"schema": execution.POLICY_SCHEMA, "enabled": True,
            "mode": "pinned" if pinned else "current-main", "pinned": {
                "code_sha": self.code_sha, "tree_sha": TREE_SHA, "main_ci_run": 102,
                "approval_ref": APPROVAL} if pinned else None}}
        public_grant = json.loads(Path("config/public-runtime.json").read_bytes())
        self.publications = {WORKFLOW_SHA: public_grant}
        self.interfaces = {self.code_sha: deepcopy(execution.INTERFACE)}
        self.trees = {self.code_sha: TREE_SHA}
        self.cis = {WORKFLOW_SHA: self.ci(WORKFLOW_SHA, 101)}
        if pinned:
            self.cis[EXECUTION_SHA] = self.ci(EXECUTION_SHA, 102)
        self.bad_ancestry = set()
        self.content_override = None

    @staticmethod
    def ci(code, ident):
        return {"total_count": 1, "workflow_runs": [{"id": ident, "head_sha": code,
            "head_branch": "main", "event": "push", "path": ".github/workflows/ci.yml",
            "status": "completed", "conclusion": "success", "run_attempt": 1,
            "name": "decision-os-ci", "repository": {"id": execution.REPOSITORY_ID},
            "head_repository": {"id": execution.REPOSITORY_ID}}]}

    def advance(self, head=MOVED_SHA, *, policy=None, publication=None):
        old = self.main["object"]["sha"]
        self.policies[head] = deepcopy(self.policies[old] if policy is None else policy)
        self.publications[head] = deepcopy(self.publications[old] if publication is None else publication)
        self.main["object"]["sha"] = head

    def freeze(self, *, cache=None):
        selected = execution.select_execution(self, self.selected, env=self.env, cache=cache)
        self.env[execution.ENVIRONMENT_KEY] = json.dumps({key: selected[key] for key in (
            "workflow_sha", "code_sha", "runtime_version")}, separators=(",", ":"))
        return selected

    def request(self, method, path, payload=None):
        assert method == "GET", (method, path)
        self.calls.append((method, path))
        if path == execution.PREFIX:
            return deepcopy(self.meta)
        if path == execution.PREFIX + "/git/ref/heads/main":
            return deepcopy(self.main)
        if path == execution.PREFIX + "/actions/runs/8001":
            return deepcopy(self.run)
        if path.startswith(execution.PREFIX + "/actions/workflows/ci.yml/runs?"):
            code = parse_qs(urlsplit(path).query)["head_sha"][0]
            return deepcopy(self.cis[code])
        if path.startswith(execution.PREFIX + "/git/commits/"):
            code = path.rsplit("/", 1)[1]
            return {"sha": code, "tree": {"sha": self.trees[code]}}
        if path.startswith(execution.PREFIX + "/contents/"):
            target = path.split("/contents/", 1)[1].split("?", 1)[0]
            code = parse_qs(urlsplit(path).query)["ref"][0]
            values = {execution.POLICY_PATH: self.policies, execution.PUBLIC_PATH: self.publications,
                execution.INTERFACE_PATH: self.interfaces}
            value = values[target].get(code)
            response = contents(target, encoded(value)) if value is not None else {"type": "missing"}
            return self.content_override(target, code, response) if self.content_override else response
        if path.startswith(execution.PREFIX + "/compare/"):
            base, head = path.split("/compare/", 1)[1].split("?", 1)[0].split("...")
            return {"status": "diverged" if (base, head) in self.bad_ancestry else "ahead",
                "merge_base_commit": {"sha": base}}
        raise AssertionError("unexpected native route: " + path)


def test_current_main_has_no_self_referential_future_commit():
    native = Native()
    bound = native.freeze()
    assert bound["workflow_sha"] == bound["code_sha"] == WORKFLOW_SHA
    assert bound["runtime_version"]["mode"] == "current-main"
    assert bound["runtime_version"]["workflow_ci_run"] == bound["runtime_version"]["execution_ci_run"] == 101
    assert bound["runtime_version"]["approval_ref"] is None
    assert json.loads(Path("config/daily-runtime-version.json").read_bytes()) == native.policies[WORKFLOW_SHA]


def test_pinned_runtime_and_native_workflow_have_distinct_proofs():
    native = Native(pinned=True)
    bound = native.freeze()
    assert bound["workflow_sha"] == WORKFLOW_SHA and bound["code_sha"] == EXECUTION_SHA
    assert bound["runtime_version"]["code_tree"] == TREE_SHA
    assert bound["runtime_version"]["workflow_ci_run"] == 101
    assert bound["runtime_version"]["execution_ci_run"] == 102
    assert execution.native_head_sha(bound) == WORKFLOW_SHA
    assert native.env["GITHUB_SHA"] == native.env["GITHUB_WORKFLOW_SHA"] == WORKFLOW_SHA


def test_main_advancement_rechecks_exact_new_policy_and_never_reselects():
    native = Native(pinned=True)
    bound = native.freeze()
    native.advance()
    start = len(native.calls)
    result = execution.require_live_execution(native, bound, cache={})
    calls = native.calls[start:]
    assert len(calls) == execution.AUTH_GUARD_REQUESTS == 6
    assert result["main_sha"] == MOVED_SHA
    assert all("ref=" + MOVED_SHA in path for _, path in calls if "/contents/" in path)
    assert execution.select_execution(native, native.selected, env=native.env) == bound
    assert bound["runtime_version"]["policy_commit"] == WORKFLOW_SHA


def test_current_main_inflight_binding_does_not_follow_the_new_head():
    native = Native()
    bound = native.freeze()
    native.advance()
    assert execution.select_execution(native, native.selected, env=native.env) == bound
    assert bound["code_sha"] != native.main["object"]["sha"]


def test_same_run_static_cache_reuses_only_static_proofs_across_api_units():
    first = Native(pinned=True)
    cache = {}
    bound = first.freeze(cache=cache)
    second = Native(pinned=True)
    second.env = deepcopy(first.env)
    second.advance()
    assert execution.select_execution(second, second.selected, env=second.env, cache=cache) == bound
    assert any("/actions/runs/8001" in path for _, path in second.calls)
    assert not any("/actions/workflows/ci.yml/runs?" in path or "/git/commits/" in path for _, path in second.calls)
    disabled = deepcopy(second.policies[MOVED_SHA]); disabled["enabled"] = False
    second.advance("e" * 40, policy=disabled)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_DISABLED$"):
        execution.require_live_execution(second, bound, cache=cache)


@pytest.mark.parametrize("field", ["enabled", "sources", "handoff", "watch"])
def test_public_authorization_is_read_live_not_from_old_checkout(field):
    native = Native(pinned=True)
    bound = native.freeze()
    grant = deepcopy(native.publications[WORKFLOW_SHA])
    if field == "enabled":
        grant["enabled"] = False
    else:
        grant["scopes"].remove(field)
    native.advance(publication=grant)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_PUBLICATION_REVOKED$"):
        execution.require_live_execution(native, bound)


def test_private_still_needs_execution_approval_but_not_public_grant():
    native = Native(pinned=True, public=False)
    native.publications[WORKFLOW_SHA]["enabled"] = False
    bound = native.freeze()
    assert execution.require_live_execution(native, bound)["publication_basis"] is None
    disabled = deepcopy(native.policies[WORKFLOW_SHA]); disabled["enabled"] = False
    native.advance(policy=disabled)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_DISABLED$"):
        execution.require_live_execution(native, bound)


@pytest.mark.parametrize("change", ["code", "tree", "ci", "approval", "mode"])
def test_changed_approval_cannot_silently_rebind_an_inflight_run(change):
    native = Native(pinned=True)
    bound = native.freeze()
    changed = deepcopy(native.policies[WORKFLOW_SHA])
    if change == "mode":
        changed.update(mode="current-main", pinned=None)
    else:
        field, value = {"code": ("code_sha", "e" * 40), "tree": ("tree_sha", "e" * 40),
            "ci": ("main_ci_run", 103), "approval": ("approval_ref", APPROVAL + "0")}[change]
        changed["pinned"][field] = value
    native.advance(policy=changed)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_POLICY_CHANGED$"):
        execution.require_live_execution(native, bound)


@pytest.mark.parametrize("code", [WORKFLOW_SHA, EXECUTION_SHA])
def test_either_code_losing_main_ancestry_stops_execution(code):
    native = Native(pinned=True)
    bound = native.freeze()
    native.advance()
    native.bad_ancestry.add((code, MOVED_SHA))
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_OFF_MAIN$"):
        execution.require_live_execution(native, bound)


@pytest.mark.parametrize("target", [WORKFLOW_SHA, EXECUTION_SHA])
def test_each_code_needs_its_own_latest_successful_main_ci(target):
    native = Native(pinned=True)
    native.cis[target]["workflow_runs"][0]["conclusion"] = "failure"
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_MAIN_CI$"):
        native.freeze()


@pytest.mark.parametrize("fault", ["tree", "ci", "missing-protocol", "wrong-protocol"])
def test_a_pinned_old_or_unapproved_runtime_is_not_admitted(fault):
    native = Native(pinned=True)
    if fault == "tree":
        native.trees[EXECUTION_SHA] = "e" * 40
    elif fault == "ci":
        native.policies[WORKFLOW_SHA]["pinned"]["main_ci_run"] = 103
    elif fault == "missing-protocol":
        del native.interfaces[EXECUTION_SHA]
    else:
        native.interfaces[EXECUTION_SHA]["protocol"] = "unknown-v99"
    with pytest.raises(RecordError):
        native.freeze()


@pytest.mark.parametrize("fault", ["sha", "size", "base64", "type", "path", "duplicate", "oversize"])
def test_native_policy_bytes_and_identity_are_verified(fault):
    native = Native()
    def corrupt(path, code, response):
        if path != execution.POLICY_PATH:
            return response
        if fault == "sha": response["sha"] = "f" * 40
        elif fault == "size": response["size"] += 1
        elif fault == "base64": response["content"] = "?"
        elif fault == "type": response["type"] = "symlink"
        elif fault == "path": response["path"] = "untrusted.json"
        elif fault == "duplicate": response = contents(path, b'{"schema":1,"schema":2}')
        elif fault == "oversize": response = contents(path, b" " * 8193)
        return response
    native.content_override = corrupt
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_CONTENT$"):
        native.freeze()


@pytest.mark.parametrize("change", [None, {}, {"enabled": 1}, {"mode": "latest"}, {"mode": []}, {"extra": True},
    {"mode": "pinned", "pinned": None}])
def test_policy_is_strict_and_has_no_implicit_fallback(change):
    value = Native().policies[WORKFLOW_SHA]
    if change is None or change == {}:
        value = change
    else:
        value.update(change)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_POLICY$"):
        execution.policy(value)


def test_environment_parsing_is_not_authority_and_cannot_replace_native_head():
    native = Native(pinned=True)
    bound = native.freeze()
    parsed = execution.execution_identity(native.env)
    assert parsed["code_sha"] == bound["code_sha"]
    native.env["GITHUB_SHA"] = EXECUTION_SHA
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_NATIVE_CONTEXT$"):
        execution.select_execution(native, native.selected, env=native.env)
    assert execution.execution_identity({}) is None
    with pytest.raises(RecordError):
        execution.execution_identity({execution.ENVIRONMENT_KEY: '{"workflow_sha":1,"workflow_sha":2}'})


def test_frozen_policy_provenance_is_checked_on_each_new_entry():
    native = Native(pinned=True)
    bound = native.freeze()
    native.advance()
    envelope = execution.execution_identity(native.env)
    envelope["runtime_version"]["policy_commit"] = "e" * 40
    native.policies["e" * 40] = {"schema": execution.POLICY_SCHEMA, "enabled": False,
        "mode": "current-main", "pinned": None}
    native.env[execution.ENVIRONMENT_KEY] = json.dumps(envelope)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_POLICY_PROVENANCE$"):
        execution.select_execution(native, native.selected, env=native.env, cache={})
    assert bound["runtime_version"]["policy_commit"] == WORKFLOW_SHA


def test_a_matching_policy_from_an_unmerged_commit_cannot_forge_provenance():
    native = Native(pinned=True)
    native.freeze()
    native.advance()
    envelope = execution.execution_identity(native.env)
    unmerged = "e" * 40
    envelope["runtime_version"]["policy_commit"] = unmerged
    native.policies[unmerged] = deepcopy(native.policies[WORKFLOW_SHA])
    native.bad_ancestry.add((unmerged, MOVED_SHA))
    native.env[execution.ENVIRONMENT_KEY] = json.dumps(envelope)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_OFF_MAIN$"):
        execution.select_execution(native, native.selected, env=native.env, cache={})


@pytest.mark.parametrize("change", [{"operation": "status"}, {"event": "schedule"},
    {"invocation": "untrusted"}, {"invocation": []}, {"operation": []}, {"event": []}])
def test_operation_event_and_invocation_are_one_checked_identity(change):
    native = Native()
    native.selected.update(change)
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_IDENTITY$"):
        native.freeze()


def test_static_cache_does_not_authorize_another_native_run():
    native = Native(pinned=True)
    cache = {}
    native.freeze(cache=cache)
    native.env["GITHUB_RUN_ID"] = "8002"
    selected = {**native.selected, "run_id": 8002}
    original = native.request
    def request(method, path, payload=None):
        if path.endswith("/actions/runs/8002"):
            return {**native.run, "id": 8002}
        return original(method, path, payload)
    native.request = request
    native.cis[EXECUTION_SHA]["workflow_runs"][0]["conclusion"] = "failure"
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_MAIN_CI$"):
        execution.select_execution(native, selected, env=native.env, cache=cache)


def test_actual_checkout_and_tracked_bytes_must_match_frozen_tree(tmp_path):
    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
            capture_output=True, text=True).stdout.strip()
    git("init")
    git("config", "user.name", "Synthetic Test")
    git("config", "user.email", "synthetic@example.invalid")
    path = tmp_path / execution.INTERFACE_PATH
    path.parent.mkdir()
    path.write_bytes(encoded(execution.INTERFACE))
    git("add", ".")
    git("commit", "-m", "synthetic approved code")
    head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    identity = Native().freeze()
    identity.update(workflow_sha=head, code_sha=head)
    identity["runtime_version"]["code_tree"] = tree
    execution.verify_checkout(identity, root=tmp_path)
    extra = tmp_path / "src" / "decision_kernel" / "untracked_module.py"
    extra.parent.mkdir(parents=True)
    extra.write_text("raise RuntimeError('synthetic unapproved module')\n")
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_CHECKOUT$"):
        execution.verify_checkout(identity, root=tmp_path)
    extra.unlink()
    (tmp_path / "config" / "untracked-policy.json").write_text("{}\n")
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_CHECKOUT$"):
        execution.verify_checkout(identity, root=tmp_path)
    (tmp_path / "config" / "untracked-policy.json").unlink()
    path.write_text("{}\n")
    with pytest.raises(RecordError, match="^DAILY_EXECUTION_CHECKOUT$"):
        execution.verify_checkout(identity, root=tmp_path)
