"""Owner: Odds Book host/publication and its original cold-wheel consumer.

Native GitHubStore and bounded GitHubAPI use the existing real-Git wire model.
No production records, new acquisition, semantic Odds replay or second install.
"""
from dataclasses import asdict, replace
import argparse
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError

import pytest
import yaml

from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.odds_watch import WATCH_RECORD
from decision_kernel.domain.records import RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded
from decision_kernel.infrastructure import runtime_publication as policy
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import odds_commands, research_runtime as runtime, store_cli
from test_conditional_odds import authorized, price_payload, spec_payload
from test_hosted_watch_runtime import public_host
from test_news_read_budget import ModelWire, transport
from test_research_runtime import CODE, host, inputs

OPERATION = "read-odds-book"
SCOPES = ("reading", "research", "sources", "watch", "market")


def seed(model):
    writer = VersionedStore(GitHubStore(replace(model.binding, write_enabled=True), model))
    source = writer.put_source("book-source", SourceInput("book-source", "content", "Synthetic Book basis",
        "synthetic:odds-book-host", "SYNTHETIC_BOOK_SOURCE_NOT_REAL_RESEARCH"))
    research = writer.submit_revision("book-full", RevisionInput("book-case", "r1", "full",
        "SYNTHETIC_BOOK_RESEARCH_BODY", "Synthetic host contract", "synthetic", (source,),
        unresolved=("Unverified synthetic economic assumption",)))
    writer.submit_revision("book-no-odds", RevisionInput("book-no-odds", "r1", "full",
        "SYNTHETIC_FULL_WITHOUT_ODDS", "Synthetic host contract", "synthetic", (source,)))
    service = ConditionalOdds(writer)
    valuation = service.freeze(authorized("spec", spec_payload(asdict(research))), submission_id="book-spec")
    odds = service.save(authorized("calculation", {"spec_ref": valuation["reference"],
        "price": price_payload()}), submission_id="book-odds")
    model.requests.clear()
    return source, research, odds


def run(model, api, output, payload=None):
    return runtime.run_command(model.binding, api=api, inputs=inputs(OPERATION, payload or {}),
        code_sha=CODE, output=output, run_id="123", attempt="1")


def assert_failed_export(output):
    report = json.loads((output / "report.json").read_bytes())
    assert report["status"] == "failed" and report["write"] == "not-requested"
    assert "result" not in report and not (output / "inputs.json").exists()
    assert "SYNTHETIC_BOOK_RESEARCH_BODY" not in (output / "report.json").read_text()
    return report


@pytest.mark.parametrize("private", [False, True], ids=["authorized-public", "native-private"])
def test_native_book_uses_original_store_budget_and_release(public_host, tmp_path, private):
    model, grant, path = public_host
    source, research, odds = seed(model)
    if private:
        model.binding = replace(model.binding, private_upgrade_allowed=True)
        model.meta["private"] = True
        grant["enabled"] = False
        path.write_bytes(encoded(grant))
    api = transport(model)
    before, writes = model.git.head(), model.writes
    payload = {"limit": 8, "scan_limit": 32}
    output = tmp_path / "book"
    report = run(model, api, output, payload)
    result = report["result"]
    assert result["schema"] == "odds-book-v1" and result["coverage"] == "this-page-only"
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["repository_visibility"] == ("private" if private else "public")
    assert report["research_executed"] is False and report["human_acceptance"] == "not-confirmed"
    assert report["research_assessment"] == "not-performed"
    assert json.loads((output / "inputs.json").read_bytes()) == inputs(OPERATION, payload)
    assert json.loads((output / "report.json").read_bytes()) == report
    raw = encoded(result).decode()
    assert source.manifest.sha256 in raw and research.sha256 in raw
    assert odds["reference"]["sha256"] in raw
    assert any(item["kind"] == RESULT_RECORD and item["status"] == "readable" for item in result["items"])
    assert 0 < api._requests == len(model.requests) < MAX_REQUESTS == 512
    assert all(method == "GET" for method, _, _ in model.requests)
    assert model.git.head() == before and model.writes == writes
    if not private:
        assert report["publication_authorization"] == grant["authorization_ref"]
        assert report["effective_payload_sha256"] == report["payload_sha256"]


@pytest.mark.parametrize("scope", SCOPES)
def test_each_component_scope_required_before_book_or_query_export(public_host, tmp_path, monkeypatch, scope):
    from decision_kernel.application import odds_book
    model, grant, path = public_host
    grant["scopes"].remove(scope)
    path.write_bytes(encoded(grant))
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: pytest.fail("Book read before publication grant"))
    output = tmp_path / "denied"
    before, writes = model.git.head(), model.writes
    with pytest.raises(RecordError, match="ODDS_BOOK_PUBLICATION_NOT_CONFIRMED"):
        run(model, transport(model), output)
    assert not output.exists() and model.git.head() == before and model.writes == writes


@pytest.mark.parametrize("scope", SCOPES)
def test_last_host_gate_rechecks_all_scopes_after_composed_read(public_host, tmp_path, monkeypatch, scope):
    model, grant, path = public_host
    seed(model)
    original = runtime._perform
    output = tmp_path / "revoked"
    def revoke(*args, **kwargs):
        assert not (output / "inputs.json").exists()
        result = original(*args, **kwargs)
        changed = {**grant, "scopes": [name for name in grant["scopes"] if name != scope]}
        path.write_bytes(encoded(changed))
        return result
    monkeypatch.setattr(runtime, "_perform", revoke)
    before, writes = model.git.head(), model.writes
    with pytest.raises(RecordError, match="PUBLICATION_NOT_CONFIRMED|AUTHORIZATION_REVOKED"):
        run(model, transport(model), output)
    assert_failed_export(output)
    assert model.git.head() == before and model.writes == writes


def test_native_private_to_public_reversal_keeps_original_adapter_latch(public_host, tmp_path, monkeypatch):
    model, _, _ = public_host
    seed(model)
    model.binding = replace(model.binding, private_upgrade_allowed=True)
    model.meta["private"] = True
    original = runtime._perform
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs)
        model.meta["private"] = False
        return result
    monkeypatch.setattr(runtime, "_perform", reverse)
    output = tmp_path / "reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        run(model, transport(model), output)
    assert_failed_export(output)


@pytest.mark.parametrize("status", [401, 403, 404, 429])
def test_transport_denial_inside_book_remains_global_terminal(public_host, tmp_path, monkeypatch, status):
    from decision_kernel.application import odds_book
    model, _, _ = public_host
    seed(model)
    api = transport(model)
    original = odds_book.read_book
    failed = []
    class DeniedWire(ModelWire):
        def open(self, request, timeout):
            if "/git/blobs/" in request.full_url:
                failed.append(request.full_url)
                raise HTTPError(request.full_url, status, "Synthetic denied", {}, None)
            assert not failed, "No further dependency read after a terminal denial"
            return super().open(request, timeout)
    def denied(store, payload, registry=None):
        assert store.backend.api is api
        api._opener = DeniedWire(model)
        return original(store, payload, registry=registry)
    monkeypatch.setattr(odds_book, "read_book", denied)
    output = tmp_path / "transport-denied"
    before, writes = model.git.head(), model.writes
    with pytest.raises(StoreUnavailable, match="REMOTE_"):
        run(model, api, output)
    assert_failed_export(output)
    assert len(failed) == 1 and model.git.head() == before and model.writes == writes


def test_book_never_resets_original_api_request_budget(public_host, tmp_path, monkeypatch):
    from decision_kernel.application import odds_book
    model, _, _ = public_host
    seed(model)
    api = transport(model)
    original = odds_book.read_book
    def exhausted(store, payload, registry=None):
        assert store.backend.api is api
        api._requests = MAX_REQUESTS  # Earlier calls in this same operation consumed its allowance.
        return original(store, payload, registry=registry)
    monkeypatch.setattr(odds_book, "read_book", exhausted)
    output = tmp_path / "budget"
    before, writes = model.git.head(), model.writes
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        run(model, api, output)
    assert_failed_export(output)
    assert api._requests == MAX_REQUESTS + 1 and model.git.head() == before and model.writes == writes


def test_original_result_size_gate_runs_before_book_query_export(public_host, tmp_path, monkeypatch):
    from decision_kernel.application import odds_book
    model, _, _ = public_host
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: {
        "schema": "odds-book-v1", "body": "x" * runtime.MAX_RESULT_BYTES})
    output = tmp_path / "oversize"
    with pytest.raises(RecordError, match="COMMAND_RESULT_TOO_LARGE"):
        run(model, transport(model), output)
    assert_failed_export(output)


@pytest.mark.parametrize("payload", [{"rights_confirmed": True}, {"publication_scope": "research"},
    {"limit": 9}, {"scan_limit": 33}, {"limit": True}])
def test_book_plan_has_no_self_authority_and_fails_before_export(public_host, tmp_path, payload):
    model, _, _ = public_host
    output = tmp_path / "invalid"
    with pytest.raises(RecordError):
        run(model, transport(model), output, payload)
    assert not output.exists()


@pytest.mark.parametrize("change", ["revocation", "privacy-reversal"])
def test_normal_bound_cli_does_not_print_book_after_final_gate_failure(public_host, tmp_path, monkeypatch,
                                                                      capsys, change):
    from decision_kernel.application import odds_book
    model, grant, path = public_host
    seed(model)
    if change == "privacy-reversal":
        model.binding = replace(model.binding, private_upgrade_allowed=True)
        model.meta["private"] = True
    binding_file, query = tmp_path / "binding.json", tmp_path / "query.json"
    binding_file.write_bytes(encoded(asdict(model.binding)))
    query.write_text("{}")
    api = transport(model)
    monkeypatch.setattr(GitHubAPI, "from_environment", lambda: api)
    original = odds_book.read_book
    def change_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "revocation":
            grant["scopes"].remove("sources")
            path.write_bytes(encoded(grant))
        else:
            model.meta["private"] = False
        return result
    monkeypatch.setattr(odds_book, "read_book", change_after_read)
    before, writes = model.git.head(), model.writes
    parser = argparse.ArgumentParser()
    store_cli.configure(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["store", "--binding", str(binding_file), OPERATION, "--input", str(query)])
    assert store_cli.execute(args) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "error" in json.loads(captured.err)
    assert model.git.head() == before and model.writes == writes


def test_normal_workflow_routes_book_only_to_existing_read_job():
    path = Path(__file__).parents[1] / ".github/workflows/research-runtime.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert OPERATION in workflow["on"]["workflow_dispatch"]["inputs"]["operation"]["options"]
    assert OPERATION in odds_commands.READ_COMMANDS and OPERATION in runtime.READ_OPERATIONS
    assert OPERATION not in runtime.WRITE_OPERATIONS | runtime.RECONCILE_OPERATIONS
    assert OPERATION not in workflow["jobs"]["write"]["if"]
    assert OPERATION not in odds_commands.REFERENCE_COMMANDS
    assert workflow["jobs"]["read"]["permissions"] == {"contents": "read", "actions": "read"}
    assert policy.publication_scope(OPERATION) == "reading"
    for write in ({"submission_id": "not-a-write"}, {"expected_head": "a" * 40}):
        with pytest.raises(RecordError, match="UNEXPECTED_WRITE_FIELDS"):
            runtime.parse_inputs({**inputs(OPERATION, {}), **write})


def exercise_installed_odds_book(command, outside, executable, env, store_path):
    """Extend the original Odds caller, wheel and store through real CLI processes."""
    query = outside / "odds-book-input.json"
    def read(payload):
        query.write_bytes(encoded(payload))
        return command(OPERATION, "--input", str(query))
    def head():
        return subprocess.run(["git", "--git-dir=" + str(store_path), "rev-parse", "HEAD"],
            cwd=outside, env=env, check=True, capture_output=True, text=True).stdout.strip()
    before = head()
    page = read({"limit": 2, "scan_limit": 8})
    assert page["schema"] == "odds-book-v1" and page["coverage"] == "this-page-only"
    assert page["next_cursor"] is not None
    next_page = read({"cursor": page["next_cursor"], "limit": 2, "scan_limit": 8})
    assert next_page["snapshot"] == page["snapshot"]
    assert next_page["scan"]["start"] == page["scan"]["end"]
    research = command("current", "--case-id", "odds-case")["reference"]
    query_plan = {"research_ref": research, "limit": 8, "scan_limit": 32}
    selected = read(query_plan)
    assert selected["schema"] == "odds-book-v1"
    # Follow only this original finite page chain; no new research or store.
    rows = list(selected["items"])
    for _ in range(16):
        if selected["next_cursor"] is None:
            break
        selected = read({"cursor": selected["next_cursor"], "limit": 8, "scan_limit": 32})
        rows.extend(selected["items"])
    else:
        raise AssertionError("Installed Book did not finish its bounded original fixture")
    conditional = next(row for row in rows if row["kind"] == RESULT_RECORD and row["status"] == "readable")
    assert any(row["kind"] == WATCH_RECORD and row["status"] == "readable" for row in rows)
    # Exact originals still open through their established reader, outside checkout.
    query.write_bytes(encoded(conditional["reference"]))
    restored = command("read-odds", "--ref", str(query))
    assert restored["recalculation"] == "matches" and restored["result"]["research_ref"] == research
    assert head() == before
    query.write_bytes(encoded({"rights_confirmed": True}))
    rejected = subprocess.run([str(executable), "store", "--path", str(store_path), OPERATION,
        "--input", str(query)], cwd=outside, env=env, capture_output=True, text=True)
    assert rejected.returncode == 2 and rejected.stdout == ""
    assert head() == before
