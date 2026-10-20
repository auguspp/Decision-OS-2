"""Full Watch Brief publication over native GitHubStore and real synthetic Git.

All retained inputs below are engineering fixtures. No business research, price,
probability or economic finding is manufactured to demonstrate this route.
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure import runtime_publication as policy
from decision_kernel.interfaces import research_runtime as runtime
from test_conditional_odds import authorized
from test_full_watch import full_watch, question_link
from test_hosted_native_runtime import payload
from test_hosted_watch_handoff import document
from test_odds_watch import request as check_request
from test_odds_watch_runtime import native_watch_fetch
from test_research_runtime import host, invoke, source, work
from test_watch_conditions import evidence_condition, evidence_entry, v2_input


@pytest.fixture
def public_host(host, tmp_path, monkeypatch):
    # Only the trusted deployment identity is rebound to this isolated Git
    # repository. The real authorization parser and scope checks stay intact.
    host.binding = replace(host.binding, branch="records")
    anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
    host.binding = replace(host.binding, anchor=anchor)
    for key, value in (("REPOSITORY", host.binding.repository),
                       ("REPOSITORY_ID", host.binding.repository_id),
                       ("ANCHOR", anchor), ("STORE_ID", host.binding.store_id)):
        monkeypatch.setattr(policy, key, value)
    grant = {"schema": "public-runtime-authorization-v1", "enabled": True,
        "repository": host.binding.repository, "repository_id": host.binding.repository_id,
        "branch": host.binding.branch, "anchor": anchor, "store_id": host.binding.store_id,
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
        "scopes": sorted(policy.SCOPES)}
    path = tmp_path / "public-runtime.json"
    path.write_text(json.dumps(grant))
    monkeypatch.setattr(policy, "AUTHORIZATION_FILE", path)
    return host, grant, path


@pytest.fixture
def full_brief(public_host, tmp_path, monkeypatch):
    api, _, _ = public_host
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00Z")
    monkeypatch.setattr("decision_kernel.infrastructure.watch_snapshot._fetch_market_branch",
                        native_watch_fetch(api, []))
    src = invoke(api, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]
    research = invoke(api, tmp_path / "full", "checkpoint", work(src), "full")["result"]["reference"]
    value = full_watch(research, kind="research", conditions=[evidence_condition()],
                       links=[question_link("economics")])
    value["window"]["ends_at"] = "2026-01-05T03:30:00Z"
    watch = invoke(api, tmp_path / "watch", "freeze-odds-watch", authorized("watch", value),
                   "full-watch")["result"]["reference"]
    reader = VersionedStore(GitHubStore(api.binding, api))
    query = check_request(reader, watch, price=v2_input(evidence=[
        evidence_entry(SourceRef.from_dict(src), "cannot-determine")]))
    check = invoke(api, tmp_path / "check", "save-odds-watch-check", authorized("check", query),
                   "full-check")["result"]["reference"]
    result = invoke(api, tmp_path / "check-read", "read-odds-watch-check",
                    {"reference": check})["result"]["result"]
    return document(research, check, result, question="economics"), result


def test_native_public_full_evidence_brief_saves_and_independently_restores(public_host, full_brief, tmp_path):
    api, _, _ = public_host
    doc, check_result = full_brief
    saved = invoke(api, tmp_path / "save", "save-hosted-handoff", payload(doc), "brief")
    ref = saved["result"]["reference"]
    assert saved["write"] == "published" and saved["repository_visibility"] == "public"
    count, head = api.writes, api.git.head()
    # Each invocation restores through a fresh native adapter, with no writer cache.
    read = invoke(api, tmp_path / "read", "read-hosted-handoff", {"reference": ref})
    opened = read["result"]
    adopted = opened["watch_evidence_adoption"][0]
    assert opened["document"] == doc and adopted["result"] == check_result
    assert adopted["result"]["status"] == "cannot-determine"
    assert adopted["recalculation"] == "matches" and adopted["selection"]["question_id"] == "economics"
    assert adopted["definition"]["tracking"]["odds_result"] is None
    assert opened["human_acceptance"] == "not-confirmed"
    assert opened["economic_quality"] == "not-certified-by-reader"
    latest = invoke(api, tmp_path / "latest", "latest-hosted-handoff",
                    {"kind": "brief", "as_of": doc["cutoff"]})["result"]
    assert latest["items"] == [opened]
    replay = invoke(api, tmp_path / "replay", "save-hosted-handoff", payload(doc), "brief")
    assert replay["write"] == "replayed" and replay["result"] == saved["result"]
    assert api.writes == count and api.git.head() == head


@pytest.mark.parametrize("operation", ["save-hosted-handoff", "reconcile-hosted-handoff"])
@pytest.mark.parametrize("scope", ["watch", "sources", "research", "brief"])
def test_each_composed_scope_is_required_before_author_input_export(public_host, full_brief, tmp_path,
                                                                   operation, scope):
    api, grant, path = public_host
    doc, _ = full_brief
    grant["scopes"].remove(scope)
    path.write_text(json.dumps(grant))
    output = tmp_path / "denied"
    count, head = api.writes, api.git.head()
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        invoke(api, output, operation, payload(doc), "denied")
    assert not output.exists() and api.writes == count and api.git.head() == head


@pytest.mark.parametrize("schema", ["hosted-ai-handoff-v1", "hosted-ai-handoff-v2"])
def test_exact_baseline_watch_is_checked_before_input_export(public_host, full_brief, tmp_path, schema):
    api, grant, path = public_host
    doc, _ = full_brief
    ref = invoke(api, tmp_path / "baseline", "save-hosted-handoff", payload(doc), "baseline")["result"]["reference"]
    next_doc = deepcopy(doc)
    next_doc.update(schema=schema, execution_date="2026-01-04", cutoff="2026-01-04T05:00:00Z",
                    watch_adoptions=[], baseline=ref)
    if schema == "hosted-ai-handoff-v1":
        next_doc.pop("watch_adoptions")
    grant["scopes"].remove("watch")
    path.write_text(json.dumps(grant))
    count, head = api.writes, api.git.head()
    output = tmp_path / "hidden-baseline"
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        invoke(api, output, "save-hosted-handoff", payload(next_doc), "next")
    assert not output.exists() and api.writes == count and api.git.head() == head


@pytest.mark.parametrize("operation", ["read-hosted-handoff", "latest-hosted-handoff"])
@pytest.mark.parametrize("baseline_only", [False, True])
def test_actual_read_and_latest_never_release_watch_with_only_handoff_scope(public_host, full_brief, tmp_path,
                                                                          operation, baseline_only):
    api, grant, path = public_host
    doc, _ = full_brief
    ref = invoke(api, tmp_path / "saved", "save-hosted-handoff", payload(doc), "first")["result"]["reference"]
    if baseline_only:
        doc = deepcopy(doc)
        doc.update(schema="hosted-ai-handoff-v1", execution_date="2026-01-04", cutoff="2026-01-04T05:00:00Z",
                   baseline=ref)
        doc.pop("watch_adoptions")
        ref = invoke(api, tmp_path / "next", "save-hosted-handoff", payload(doc), "next")["result"]["reference"]
    grant["scopes"] = ["handoff"]
    path.write_text(json.dumps(grant))
    args = {"reference": ref} if operation == "read-hosted-handoff" else {
        "kind": "brief", "as_of": doc["cutoff"], "limit": 1}
    count, head = api.writes, api.git.head()
    output = tmp_path / "denied-read"
    with pytest.raises(RecordError, match="HOSTED_WATCH_PUBLICATION_NOT_CONFIRMED"):
        invoke(api, output, operation, args)
    report = json.loads((output / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report
    assert doc["body"] not in (output / "report.json").read_text()
    assert api.writes == count and api.git.head() == head


def test_after_read_scope_revocation_keeps_original_write_reconciliation(public_host, full_brief, tmp_path,
                                                                       monkeypatch):
    api, grant, path = public_host
    doc, _ = full_brief
    original = runtime._perform
    def revoke_after_restore(*args, **kwargs):
        result = original(*args, **kwargs)
        changed = {**grant, "scopes": ["handoff"]}
        path.write_text(json.dumps(changed))
        return result
    monkeypatch.setattr(runtime, "_perform", revoke_after_restore)
    expected = api.git.head()
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(api, tmp_path / "revoked", "save-hosted-handoff", payload(doc), "original", expected)
    failed = json.loads((tmp_path / "revoked" / "report.json").read_bytes())
    assert failed["status"] == "unknown-write" and "result" not in failed
    original_inputs = json.loads((tmp_path / "revoked" / "inputs.json").read_bytes())
    assert original_inputs["submission_id"] == "original" and original_inputs["expected_head"] == expected
    monkeypatch.setattr(runtime, "_perform", original)
    path.write_text(json.dumps(grant))
    count, head = api.writes, api.git.head()
    reconciled = invoke(api, tmp_path / "reconciled", "reconcile-hosted-handoff", payload(doc), "original", expected)
    assert reconciled["write"] == "reconciled-without-write" and reconciled["result"]["status"] == "readable"
    assert api.writes == count and api.git.head() == head


def test_document_is_validated_before_author_input_export(public_host, full_brief, tmp_path):
    api, _, _ = public_host
    doc, _ = full_brief
    doc["body"] = "No retained change adopted in this body."
    output = tmp_path / "invalid"
    count = api.writes
    with pytest.raises(RecordError, match="HOSTED_WATCH_CHANGE_NOT_IN_BODY"):
        invoke(api, output, "save-hosted-handoff", payload(doc), "invalid")
    assert not output.exists() and api.writes == count


def test_native_private_full_brief_needs_no_public_grant(public_host, full_brief, tmp_path):
    api, grant, path = public_host
    doc, _ = full_brief
    api.binding = replace(api.binding, private_upgrade_allowed=True)
    api.meta["private"] = True
    grant["enabled"] = False
    path.write_text(json.dumps(grant))
    saved = invoke(api, tmp_path / "private-save", "save-hosted-handoff", payload(doc), "private")
    assert saved["repository_visibility"] == "private"
    reopened = invoke(api, tmp_path / "private-read", "read-hosted-handoff", {"reference": saved["result"]["reference"]})
    assert reopened["repository_visibility"] == "private" and reopened["result"]["document"] == doc
