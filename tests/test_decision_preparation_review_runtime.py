"""Native read-only preparation reviews preserve custody, failures and exact inputs."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from decision_kernel.application.decision_preparation import DecisionPreparation
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, StoreUnavailable
from decision_kernel.infrastructure import github_store as remote, runtime_publication as policy
from decision_kernel.interfaces import cli, decision_preparation_commands as commands, research_runtime
from test_decision_preparation import LATER, authorized, plan, save_revision
from test_news_read_budget import transport
from test_research_runtime import CODE, host as host, inputs

OPERATION = "review-decision-preparation"


@pytest.fixture
def retained_review(host, monkeypatch):
    # The existing real-Git API model supplies native repository/ref/object
    # responses. Only these synthetic fixtures write; every review uses GETs.
    host.binding = replace(host.binding, branch="records", private_upgrade_allowed=True)
    anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
    host.binding = replace(host.binding, anchor=anchor)
    host.meta["private"] = True
    store = VersionedStore(remote.GitHubStore(replace(host.binding, write_enabled=True), host))
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-01T08:00:00Z")
    old_source = store.put_source("original-source", SourceInput(
        "original", "content", "Synthetic original evidence", "synthetic:original", "Original delivery facts"))
    before = save_revision(store, old_source)
    value = plan(before)
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-11T08:00:00Z")
    saved = DecisionPreparation(store).save(authorized(value), submission_id="retained-preparation")
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-04-10T08:00:00Z")
    later_source = store.put_source("later-source", SourceInput(
        "later", "content", "Synthetic later evidence", "synthetic:later", "Later retained delivery facts"))
    after = save_revision(store, later_source, "r2", before)
    payload = {"reference": saved["reference"], "horizon_id": "near", "at": LATER,
               "review": {"before": {"reference": asdict(before), "start": 1, "end": 1},
                          "after": {"reference": asdict(after), "start": 1, "end": 1},
                          "question_id": "q1", "evidence_limit": 2}}
    host.requests.clear()
    return SimpleNamespace(host=host, store=store, preparation=value, payload=payload,
                           old_source=old_source, later_source=later_source,
                           head=host.git.head(), writes=host.writes)


def invoke(retained, api, output):
    return research_runtime.run_command(retained.host.binding, api=api, code_sha=CODE, output=output,
        inputs=inputs(OPERATION, retained.payload), run_id="123", attempt="1")


def invoke_cli(retained, api, tmp_path, monkeypatch):
    binding, payload = tmp_path / "binding.json", tmp_path / "review.json"
    binding.write_text(json.dumps(asdict(retained.host.binding)), encoding="utf-8")
    payload.write_text(json.dumps(retained.payload), encoding="utf-8")
    monkeypatch.setattr(remote.GitHubAPI, "from_environment", lambda: api)
    return cli.main(["store", "--binding", str(binding), OPERATION, "--input", str(payload)])


def unchanged(retained):
    assert retained.host.git.head() == retained.head and retained.host.writes == retained.writes
    assert all(method == "GET" for method, _, _ in retained.host.requests)


def refused_report(output, code):
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed" and report["error"] == code
    assert "result" not in report and report["write"] == "not-requested"
    assert not (output / "inputs.json").exists()
    assert not (output / "reading-room.html").exists()


@pytest.mark.parametrize("entry", ["native-runtime", "native-cli"])
def test_review_reads_exact_retained_preparation_and_direct_successor_without_writes(
        retained_review, tmp_path, monkeypatch, capsys, entry):
    retained = retained_review
    api = transport(retained.host)
    if entry == "native-runtime":
        output = tmp_path / "review-output"
        report = invoke(retained, api, output)
        result = report["result"]
        assert report["status"] == "completed" and report["write"] == "not-requested"
        assert report["repository_visibility"] == "private"
        assert json.loads((output / "inputs.json").read_text()) == inputs(OPERATION, retained.payload)
    else:
        assert invoke_cli(retained, api, tmp_path, monkeypatch) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        result = json.loads(captured.out)
    assert result["schema"] == "decision-preparation-review-v1"
    assert result["reference"] == retained.payload["reference"]
    assert result["horizon"]["horizon_id"] == result["horizon_id"] == "near"
    assert result["preparation"]["document"]["input"] == retained.preparation
    assert result["preparation"]["document"]["human_confirmation"] == "not-established"
    assert result["review"]["revision_relation"] == "exact-direct-successor"
    assert result["review"]["after"]["reference"] == retained.payload["review"]["after"]["reference"]
    assert result["persistence"] == "not-written" and result["execution"] == "not-started"
    assert 0 < api._requests == len(retained.host.requests) < remote.MAX_REQUESTS
    unchanged(retained)


def test_review_is_read_only_and_does_not_accept_write_intent_fields(retained_review):
    assert OPERATION in commands.READS and OPERATION in research_runtime.READ_OPERATIONS
    assert OPERATION in research_runtime.PRIVATE_PREPARATION_OPERATIONS
    assert OPERATION not in research_runtime.WRITE_OPERATIONS | research_runtime.RECONCILE_OPERATIONS
    assert policy.publication_scope(OPERATION) == "decision-preparation"
    for submission, expected in (("new-intent", ""), ("", retained_review.head)):
        with pytest.raises(RecordError, match="^UNEXPECTED_WRITE_FIELDS$"):
            research_runtime.parse_inputs(inputs(OPERATION, retained_review.payload, submission, expected))
    assert retained_review.host.requests == []


@pytest.fixture
def public_review(retained_review, tmp_path, monkeypatch):
    retained = retained_review
    binding = retained.host.binding
    retained.host.meta["private"] = False
    # Exercise the actual fixed-identity policy with this synthetic repository;
    # no production binding or authorization file is opened or rewritten.
    for name, value in (("REPOSITORY", binding.repository), ("REPOSITORY_ID", binding.repository_id),
                        ("ANCHOR", binding.anchor), ("STORE_ID", binding.store_id)):
        monkeypatch.setattr(policy, name, value)
    grant = {"schema": "public-runtime-authorization-v1", "enabled": True,
             "repository": binding.repository, "repository_id": binding.repository_id,
             "branch": binding.branch, "anchor": binding.anchor, "store_id": binding.store_id,
             "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
             "scopes": ["decision-preparation", "research", "sources"]}
    path = tmp_path / "synthetic-public-authorization.json"
    path.write_text(json.dumps(grant), encoding="utf-8")
    monkeypatch.setattr(policy, "AUTHORIZATION_FILE", path)
    return retained, grant, path


@pytest.mark.parametrize("missing_scope", [None, "decision-preparation", "research", "sources"])
@pytest.mark.parametrize("entry", ["native-runtime", "native-cli"])
def test_public_review_requires_each_component_scope_and_only_the_minimal_three(
        public_review, tmp_path, monkeypatch, capsys, missing_scope, entry):
    retained, grant, path = public_review
    if missing_scope is not None:
        grant["scopes"].remove(missing_scope)
        path.write_text(json.dumps(grant), encoding="utf-8")
    api = transport(retained.host)
    output = tmp_path / "public-review"
    if missing_scope is None:
        assert set(grant["scopes"]) == {"decision-preparation", "research", "sources"}
        if entry == "native-runtime":
            report = invoke(retained, api, output)
            assert report["repository_visibility"] == "public"
            assert report["publication_authorization"] == grant["authorization_ref"]
            result = report["result"]
        else:
            assert invoke_cli(retained, api, tmp_path, monkeypatch) == 0
            captured = capsys.readouterr()
            assert captured.err == ""
            result = json.loads(captured.out)
        assert result["reference"] == retained.payload["reference"]
        assert result["review"]["after"]["evidence"]["items"][0]["view"]["data"] == "Later retained delivery facts"
    else:
        code = ("DECISION_PREPARATION_PRIVATE_CUSTODY_REQUIRED" if missing_scope == "decision-preparation"
                else "DECISION_PREPARATION_REVIEW_PUBLICATION_REQUIRED")
        if entry == "native-runtime":
            with pytest.raises(RecordError, match="^" + code + "$"):
                invoke(retained, api, output)
            assert not output.exists()
        else:
            assert invoke_cli(retained, api, tmp_path, monkeypatch) == 2
            captured = capsys.readouterr()
            assert captured.out == "" and json.loads(captured.err) == {"error": code}
    unchanged(retained)


def test_ordinary_preparation_read_keeps_its_existing_publication_scope(public_review, tmp_path):
    retained, grant, path = public_review
    grant["scopes"] = ["decision-preparation"]
    path.write_text(json.dumps(grant), encoding="utf-8")
    operation = "read-decision-preparation"
    payload = {"reference": retained.payload["reference"], "at": retained.payload["at"]}
    report = research_runtime.run_command(retained.host.binding, api=transport(retained.host),
        code_sha=CODE, output=tmp_path / "ordinary-read", inputs=inputs(operation, payload),
        run_id="123", attempt="1")
    assert report["status"] == "completed" and report["repository_visibility"] == "public"
    assert report["result"]["document"]["input"] == retained.preparation
    unchanged(retained)


@pytest.mark.parametrize("scope", ["research", "sources"])
@pytest.mark.parametrize("entry", ["native-runtime", "native-cli"])
def test_component_scope_revoked_after_read_refuses_public_body_and_exact_inputs(
        public_review, tmp_path, monkeypatch, capsys, scope, entry):
    retained, grant, path = public_review
    perform, state = commands.perform, {"completed": False}

    def revoke_after_read(*args, **kwargs):
        result = perform(*args, **kwargs)
        grant["scopes"].remove(scope)
        path.write_text(json.dumps(grant), encoding="utf-8")
        state["completed"] = True
        return result

    monkeypatch.setattr(commands, "perform", revoke_after_read)
    code = "DECISION_PREPARATION_REVIEW_PUBLICATION_REQUIRED"
    api = transport(retained.host)
    if entry == "native-runtime":
        output = tmp_path / "component-revoked"
        with pytest.raises(RecordError, match="^" + code + "$"):
            invoke(retained, api, output)
        refused_report(output, code)
    else:
        assert invoke_cli(retained, api, tmp_path, monkeypatch) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and json.loads(captured.err) == {"error": code}
    assert state["completed"] and "decision-preparation" in grant["scopes"]
    unchanged(retained)


def test_last_public_authority_gate_revocation_suppresses_result_and_exact_input_export(
        public_review, tmp_path, monkeypatch):
    retained, grant, path = public_review
    check, state = research_runtime.checked_output_visibility, {"revoked": False}

    def revoke_at_last_gate(backend, scope, error):
        if error == "PUBLIC_RUNTIME_AUTHORIZATION_REVOKED":
            grant["enabled"] = False
            path.write_text(json.dumps(grant), encoding="utf-8")
            state["revoked"] = True
        return check(backend, scope, error)

    monkeypatch.setattr(research_runtime, "checked_output_visibility", revoke_at_last_gate)
    output = tmp_path / "last-gate-denied"
    with pytest.raises(RecordError, match="^PUBLIC_RUNTIME_AUTHORIZATION_REVOKED$"):
        invoke(retained, transport(retained.host), output)
    assert state["revoked"]
    refused_report(output, "PUBLIC_RUNTIME_AUTHORIZATION_REVOKED")
    unchanged(retained)


@pytest.mark.parametrize("scope", ["research", "sources"])
def test_component_revocation_during_last_generic_gate_is_checked_before_export(
        public_review, tmp_path, monkeypatch, scope):
    retained, grant, path = public_review
    check, state = research_runtime.checked_output_visibility, {"revoked": False}

    def revoke_after_generic_gate(backend, selected_scope, error):
        visibility = check(backend, selected_scope, error)
        if error == "PUBLIC_RUNTIME_AUTHORIZATION_REVOKED":
            # The owner's generic preparation gate has passed. Dependency
            # qualification must still be checked before releasing any result.
            assert selected_scope == "decision-preparation"
            grant["scopes"].remove(scope)
            path.write_text(json.dumps(grant), encoding="utf-8")
            state["revoked"] = True
        return visibility

    monkeypatch.setattr(research_runtime, "checked_output_visibility", revoke_after_generic_gate)
    output = tmp_path / "last-component-denied"
    code = "DECISION_PREPARATION_REVIEW_PUBLICATION_REQUIRED"
    with pytest.raises(RecordError, match="^" + code + "$"):
        invoke(retained, transport(retained.host), output)
    assert state["revoked"] and grant["enabled"]
    refused_report(output, code)
    unchanged(retained)


@pytest.mark.parametrize("entry", ["native-runtime", "native-cli"])
def test_private_to_public_reversal_after_completed_review_releases_no_result(
        retained_review, tmp_path, monkeypatch, capsys, entry):
    retained = retained_review
    perform, state = commands.perform, {"completed": False}

    def reverse_after_read(*args, **kwargs):
        result = perform(*args, **kwargs)
        retained.host.meta["private"] = False
        state["completed"] = True
        return result

    monkeypatch.setattr(commands, "perform", reverse_after_read)
    api = transport(retained.host)
    if entry == "native-runtime":
        output = tmp_path / "visibility-reversed"
        with pytest.raises(RecordError, match="^PRIVATE_UPGRADE_REVERSED$"):
            invoke(retained, api, output)
        refused_report(output, "PRIVATE_UPGRADE_REVERSED")
    else:
        assert invoke_cli(retained, api, tmp_path, monkeypatch) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and json.loads(captured.err) == {"error": "PRIVATE_UPGRADE_REVERSED"}
    assert state["completed"]
    unchanged(retained)


@pytest.mark.parametrize("fault,code", [
    ("permission", "REMOTE_NOT_AUTHORIZED"), ("request-budget", "REMOTE_REQUEST_LIMIT"),
])
@pytest.mark.parametrize("entry", ["native-runtime", "native-cli"])
def test_later_source_access_failure_keeps_original_error_and_stops_io_and_export(
        retained_review, tmp_path, monkeypatch, capsys, fault, code, entry):
    retained = retained_review
    api = transport(retained.host)
    source_read, wire_open = VersionedStore.read_source, api._opener.open
    state = {"sources": [], "pending": False, "failed": False, "after_failure": []}

    def read_source(self, reference):
        state["sources"].append(reference)
        if reference == retained.later_source:
            if fault == "permission":
                state["pending"] = True
            else:
                api._requests = remote.MAX_REQUESTS
        try:
            return source_read(self, reference)
        except StoreUnavailable:
            state["failed"] = True
            raise

    def open_wire(request, timeout):
        if state["failed"]:
            state["after_failure"].append(request.full_url)
        if state["pending"]:
            state["pending"] = False
            raise HTTPError(request.full_url, 403, "Synthetic permission revoked", {}, None)
        return wire_open(request, timeout)

    monkeypatch.setattr(VersionedStore, "read_source", read_source)
    monkeypatch.setattr(api._opener, "open", open_wire)
    if entry == "native-runtime":
        output = tmp_path / "later-source-denied"
        with pytest.raises(StoreUnavailable, match="^" + code + "$"):
            invoke(retained, api, output)
        refused_report(output, code)
    else:
        assert invoke_cli(retained, api, tmp_path, monkeypatch) == 2
        captured = capsys.readouterr()
        assert captured.out == "" and json.loads(captured.err) == {"error": code}
    assert state["failed"] and state["sources"] == [retained.old_source, retained.later_source]
    assert state["after_failure"] == []
    if fault == "request-budget":
        assert api._requests == remote.MAX_REQUESTS + 1
    unchanged(retained)
