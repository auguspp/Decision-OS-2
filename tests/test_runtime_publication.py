"""Owner-authorized public operation keeps native identity and original budgets."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.infrastructure import runtime_publication as policy
from decision_kernel.interfaces import daily_visibility_cli as visibility
from test_daily_visibility import NativeAPI as VisibilityNative


@pytest.fixture
def grant(tmp_path, monkeypatch):
    doc = {
        "schema": "public-runtime-authorization-v1", "enabled": True,
        "repository": policy.REPOSITORY, "repository_id": policy.REPOSITORY_ID,
        "branch": "records", "anchor": policy.ANCHOR, "store_id": policy.STORE_ID,
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
        "scopes": sorted(policy.SCOPES),
    }
    path = tmp_path / "public-runtime.json"
    path.write_text(json.dumps(doc))
    monkeypatch.setattr(policy, "AUTHORIZATION_FILE", path)
    return doc, path


def binding():
    return GitHubBinding(policy.REPOSITORY, policy.REPOSITORY_ID, "records", policy.ANCHOR,
                         policy.STORE_ID, visibility="public", private_upgrade_allowed=True)


def context(operation="update", event="workflow_dispatch", workflow="daily-market.yml"):
    selected = {"operation": operation, "code_sha": "a" * 40, "run_id": 8001, "run_attempt": 1,
                "event": event}
    env = {"GITHUB_REPOSITORY": policy.REPOSITORY, "GITHUB_REPOSITORY_ID": str(policy.REPOSITORY_ID),
           "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": policy.REPOSITORY + "/.github/workflows/" + workflow + "@refs/heads/main",
           "GITHUB_EVENT_NAME": event, "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40,
           "GITHUB_RUN_ID": "8001", "GITHUB_WORKFLOW_SHA": "a" * 40}
    payload = {"schedule": "35 10 * * 1-5"} if event == "schedule" else {
        "inputs": {"operation": operation, "code_sha": "a" * 40}}
    return selected, env, payload


class Native:
    def __init__(self, private=False):
        self.execution = VisibilityNative(private=private)
        self.meta = {"id": policy.REPOSITORY_ID, "full_name": policy.REPOSITORY,
                     "private": private, "archived": False, "default_branch": "main"}
        self.run = {"id": 8001, "head_sha": "a" * 40, "head_branch": "main", "event": "workflow_dispatch",
                    "run_attempt": 1, "path": visibility.WORKFLOW, "name": visibility.WORKFLOW_NAME,
                    "status": "in_progress", "repository": {"id": policy.REPOSITORY_ID},
                    "head_repository": {"id": policy.REPOSITORY_ID}}
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        assert method == "GET"
        if path == visibility.PREFIX:
            return deepcopy(self.meta)
        if path == visibility.PREFIX + "/actions/runs/8001":
            return deepcopy(self.run)
        if path == visibility.PREFIX + "/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "a" * 40}}
        if "/compare/" in path:
            return {"status": "diverged", "merge_base_commit": {"sha": "b" * 40}}
        if any(marker in path for marker in ("/contents/", "/git/commits/", "/actions/workflows/ci.yml/runs?")):
            return self.execution.request(method, path)
        pytest.fail("No source, artifact, records or write API permitted: " + path)


def test_grant_is_tied_to_the_original_immutable_store_and_does_not_enable_writes(grant):
    doc, _ = grant
    bound = binding()
    assert policy.publication_basis(bound, "sources") == doc["authorization_ref"]
    assert not bound.write_enabled
    assert policy.publication_basis(SimpleNamespace(**bound.__dict__), "sources") is None
    assert policy.publication_basis(None, "sources") is None


@pytest.mark.parametrize("change", [{"repository": "someone/fork"}, {"repository_id": 2},
    {"branch": "records/other"}, {"anchor": "b" * 40}, {"store_id": "other-store"},
    {"visibility": "private", "private_upgrade_allowed": False}])
def test_other_store_does_not_inherit_publication(grant, change):
    assert policy.publication_basis(replace(binding(), **change), "research") is None


@pytest.mark.parametrize("mode", ["disabled", "absent", "scope-not-granted"])
def test_revocation_uses_the_same_binding_without_changing_history(grant, mode):
    doc, path = grant
    if mode == "absent":
        path.unlink()
    else:
        doc["enabled"] = mode != "disabled"
        if mode == "scope-not-granted": doc["scopes"] = ["market"]
        path.write_text(json.dumps(doc))
    before = binding().marker()
    assert policy.publication_basis(binding(), "research") is None
    assert binding().marker() == before


@pytest.mark.parametrize("change", [{"enabled": "true"}, {"repository_id": True},
    {"repository": "someone/fork"}, {"branch": "main"}, {"anchor": "b" * 40},
    {"scopes": ["sources", "sources"]}, {"scopes": ["trade"]}, {"scopes": [False]},
    {"authorization_ref": "source-provided-permission"}, {"unexpected": True}])
def test_malformed_or_source_selected_grant_is_rejected(grant, change):
    doc, path = grant
    path.write_text(json.dumps({**doc, **change}))
    with pytest.raises(RecordError, match="POLICY_INVALID"):
        policy.authorization()


@pytest.mark.parametrize("raw", ['null', '{"enabled":true,"enabled":false}', '{invalid', 'x' * 8193])
def test_policy_is_bounded_and_has_no_duplicate_keys(grant, raw):
    _, path = grant
    path.write_text(raw)
    with pytest.raises(RecordError, match="POLICY_INVALID"):
        policy.authorization()


def test_policy_symlink_is_not_a_new_authority(grant, tmp_path):
    _, path = grant
    other = tmp_path / "actual.json"
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(RecordError, match="POLICY_INVALID"):
        policy.authorization()


@pytest.mark.parametrize("operation,event,workflow", [
    ("status", "workflow_dispatch", "daily-market.yml"),
    ("update", "schedule", "daily-market.yml"),
    ("recover-late", "workflow_dispatch", "daily-market.yml"),
    ("ftshare-sync", "workflow_dispatch", "ashare-data.yml"),
    ("ftshare-status", "workflow_dispatch", "ashare-data.yml"),
])
def test_only_normal_main_hosts_inherit_native_publication(grant, operation, event, workflow):
    selected, env, _ = context(operation, event, workflow)
    assert policy.native_publication_basis(selected, env=env) == grant[0]["authorization_ref"]


@pytest.mark.parametrize("key,value", [("GITHUB_REPOSITORY", "someone/fork"),
    ("GITHUB_REPOSITORY_ID", "2"), ("GITHUB_REF", "refs/pull/4/merge"),
    ("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_RUN_ATTEMPT", "2"),
    ("GITHUB_SHA", "b" * 40), ("GITHUB_RUN_ID", "8002"),
    ("GITHUB_WORKFLOW_REF", policy.REPOSITORY + "/.github/workflows/ci.yml@refs/heads/main")])
def test_forks_untrusted_code_and_old_attempts_get_no_source_authority(grant, key, value):
    selected, env, _ = context()
    assert policy.native_publication_basis(selected, env={**env, key: value}) is None


@pytest.mark.parametrize("operation", ["repair", "ftshare-check", "ftshare-check-continue",
    "ftshare-check-kernel", "ftshare-calendar", "ftshare-adopt-kernel"])
def test_publication_does_not_reopen_historical_studies_repairs_or_adoption(grant, operation):
    selected, env, _ = context(operation, workflow="ashare-data.yml")
    assert policy.native_publication_basis(selected, env=env) is None
    selected, env, _ = context()
    assert policy.native_publication_basis({**selected, "study": "already-exhausted"}, env=env) is None


@pytest.mark.parametrize("scope", ["research", "reading", "brief", "market", "sources"])
def test_composed_daily_update_requires_its_complete_publication_scope(grant, scope):
    doc, path = grant
    doc["scopes"].remove(scope)
    path.write_text(json.dumps(doc))
    selected, env, _ = context()
    assert policy.native_publication_basis(selected, env=env) is None


@pytest.mark.parametrize("operation", ["status", "update", "recover-late"])
def test_public_daily_enters_normal_runtime_instead_of_metadata_only(grant, operation):
    _, env, event = context(operation)
    api = Native()
    report, code = visibility.preflight(event, env, api, use_authorization=True)
    assert code == 0 and report["runtime_authorized"] is True
    assert report["repository_visibility"] == "public" and report["status"] == "public-runtime-pending"
    assert report["reason_code"] is None and report["publication_authority"] == grant[0]["authorization_ref"]
    assert report["source_requests"] == 0 and report["records_read"] is False
    assert report["runtime_version"]["workflow_ci_run"] == 6001
    assert all(method == "GET" and "/records" not in path for method, path in api.calls)
    assert "作业获准接续" in visibility.summary(report)


def test_legacy_metadata_command_retains_its_original_meaning(grant):
    _, env, event = context()
    report, code = visibility.preflight(event, env, Native())
    assert code == 2 and report["status"] == "blocked" and "runtime_authorized" not in report


def test_normal_publication_revocation_returns_explicit_block_without_source(grant):
    doc, path = grant
    doc["enabled"] = False; path.write_text(json.dumps(doc))
    _, env, event = context()
    with pytest.raises(RecordError, match="DAILY_EXECUTION_PUBLICATION_REVOKED"):
        visibility.preflight(event, env, Native(), use_authorization=True)


def test_release_checks_real_visibility_and_does_not_pretend_public_is_private(grant):
    class Backend:
        binding = binding()
        checks = 0
        def live_visibility(self):
            self.checks += 1
            return "public"
    b = Backend()
    assert policy.checked_output_visibility(b, "reading", "DENIED") == "public" and b.checks == 1
    doc, path = grant
    doc["enabled"] = False; path.write_text(json.dumps(doc))
    with pytest.raises(RecordError, match="DENIED"):
        policy.checked_output_visibility(b, "reading", "DENIED")
    assert b.checks == 2


@pytest.mark.parametrize("operation,key", [("reading-room", "plan"), ("industry-read", "reference"),
    ("deliver", "progress"), ("put-artifact", "artifact"), ("save-hosted-handoff", "document"),
    ("save-decision-preparation", "preparation"), ("save-odds", "calculation"),
    ("freeze-odds-watch-execution", "plan"), ("agenda", "plan")])
def test_effective_permission_fields_do_not_change_sources_research_or_human_decision(grant, operation, key):
    original = {key: {"body": "原文\n不能改写", "human_acceptance": "not-confirmed", "budget": 12}}
    before = deepcopy(original)
    effective = policy.publication_payload(operation, original, binding())
    assert original == before and effective[key] == before[key]
    assert "actor" not in effective and "execution_authority" not in effective
    if operation in {"reading-room", "industry-read", "agenda"}:
        assert any(k.startswith("public_") and v is True for k, v in effective.items())
    else:
        assert effective["rights_confirmed"] is True
    if operation == "save-hosted-handoff":
        assert set(effective) == {"document", "rights_confirmed"}


def test_raw_source_or_execution_slot_is_not_rewritten_into_a_publication_envelope(grant):
    for operation in ("put-source", "checkpoint", "read-source", "finish-odds-watch-execution"):
        original = {"slot_ref": {"rights_confirmed": False}, "body": "unchanged"}
        assert policy.publication_payload(operation, original, binding()) is original


def test_owner_publication_does_not_grant_source_access_or_write_permission(grant):
    from decision_kernel.application.market_custody import authorize_market_custody
    from decision_kernel.application.versioned_store import VersionedStore
    backend = SimpleNamespace(binding=binding(), repository=policy.REPOSITORY, live_visibility=lambda: "public")
    store = VersionedStore(backend)
    with pytest.raises(RecordError, match="RIGHTS_REQUIRED"):
        authorize_market_custody(store)
    assert authorize_market_custody(store, custody_authorized=True) == "public"
    assert backend.binding.write_enabled is False


def test_existing_ftshare_native_check_admits_normal_public_not_historical_study(grant, monkeypatch):
    from decision_kernel.interfaces.ftshare_check_cli import check_private
    selected, env, _ = context()
    for name, value in env.items(): monkeypatch.setenv(name, value)
    api = Native()
    check_private(api, selected)
    assert len(api.calls) == 2
    with pytest.raises(RecordError, match="PRIVATE_REPOSITORY_REQUIRED"):
        check_private(Native(), {"code_sha": "a" * 40, "study": "terminal"})
