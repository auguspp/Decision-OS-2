"""Explicit repair host gates, with original bounded API and synthetic native wire.

Native metadata here is a fixture, not a real supplier or hosted acceptance.
Core tests independently cover retained query/CP evidence and grant consumption.
"""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
import yaml

from decision_kernel.application import market_collection_runtime as runtime
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, StoreUnavailable
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import market_collection_cli as cli
from test_github_store import GitAPI
from test_market_collection_runtime import (
    ROOT_DIR, SHA, STORE_TOKEN, CollectionOpener, NOW, REGISTRY, UnitHost, runtime_input,
)
from test_news_read_budget import transport


REPOSITORY = "auguspp/Decision-OS-2"
PREFIX = "/repos/" + REPOSITORY
EXECUTION_SHA = "e" * 40


@pytest.fixture
def native_evidence(tmp_path):
    model = GitAPI(tmp_path / "repair-native-model.git")
    model.binding = replace(model.binding, repository=REPOSITORY, repository_id=1420041744,
                            branch="records", visibility="public", private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta.update(id=1420041744, full_name=REPOSITORY, private=True)
    native = {"id": 123, "run_attempt": 1, "name": "decision-os-ashare-data",
        "path": ".github/workflows/ashare-data.yml", "event": "workflow_dispatch",
        "head_branch": "main", "head_sha": runtime.REPAIR_TRANSPORT_CODE_SHA,
        "status": "completed", "conclusion": "failure",
        "run_started_at": "2026-10-18T02:30:00Z", "updated_at": "2026-10-18T03:00:00Z",
        "repository": {"id": 1420041744, "full_name": REPOSITORY},
        "head_repository": {"id": 1420041744, "full_name": REPOSITORY}}
    compare = {"status": "ahead", "behind_by": 0,
               "merge_base_commit": {"sha": runtime.REPAIR_TRANSPORT_CODE_SHA}}
    request = model.request

    def original_wire(method, path, payload=None):
        if path == PREFIX + "/actions/runs/123":
            model.requests.append((method, path, deepcopy(payload)))
            return deepcopy(native)
        if path == PREFIX + "/compare/" + runtime.REPAIR_TRANSPORT_CODE_SHA + "..." + EXECUTION_SHA + "?per_page=1":
            model.requests.append((method, path, deepcopy(payload)))
            return deepcopy(compare)
        return request(method, path, payload)

    model.request = original_wire
    api = transport(model)
    store = VersionedStore(GitHubStore(model.binding, api))
    # Only the native identity projection is consumed by this adapter. The core
    # requires and independently restores the full SourceRefs before calling it.
    evidence = {"transport_evidence": {api_name: {
        "code_sha": runtime.REPAIR_TRANSPORT_CODE_SHA, "run_id": "123", "run_attempt": "1"}
        for api_name in ("daily", "adj_factor")}}
    return model, api, store, native, compare, evidence


def native_verify(fixture, **changes):
    model, api, store, native, compare, evidence = fixture
    return runtime.verify_native_repair_evidence(evidence, store=store, api=api,
                                                execution_code_sha=changes.get("code", EXECUTION_SHA))


@pytest.mark.parametrize("conclusion", ["success", "failure"])
def test_native_query_evidence_allows_completed_partial_run_without_filling_it_green(native_evidence, conclusion):
    model, api, store, native, compare, evidence = native_evidence
    native["conclusion"] = conclusion
    before = api._requests
    context = native_verify(native_evidence)
    assert context == {"execution_code_sha": EXECUTION_SHA, "transport_runs": {
        api_name: {"run_id": "123", "run_attempt": "1", "started_at": native["run_started_at"],
                   "completed_at": native["updated_at"]} for api_name in ("daily", "adj_factor")}}
    assert native["conclusion"] == conclusion
    assert api._requests - before == 3  # privacy + one shared native run + code ancestry
    assert len([x for x in model.requests if "/actions/runs/" in x[1]]) == 1
    assert api._requests == len(model.requests) and model.writes == 0


@pytest.mark.parametrize("field,value", [
    ("id", True), ("id", 124), ("run_attempt", 2), ("run_attempt", True),
    ("event", "push"), ("head_branch", "feature"), ("name", "other-workflow"),
    ("path", ".github/workflows/source-check.yml"), ("head_sha", "d" * 40),
    ("status", "in_progress"), ("conclusion", None), ("conclusion", "cancelled"),
])
def test_wrong_or_unsettled_native_run_never_authenticates_a_grant(native_evidence, field, value):
    native_evidence[3][field] = value
    with pytest.raises(RecordError, match="ASHARE_REPAIR_NATIVE_RUN_MISMATCH"):
        native_verify(native_evidence)
    assert native_evidence[0].writes == 0


@pytest.mark.parametrize("repository", ["repository", "head_repository"])
def test_native_evidence_cannot_borrow_a_same_named_run_from_another_repository(native_evidence, repository):
    native_evidence[3][repository]["id"] = 999
    with pytest.raises(RecordError, match="ASHARE_REPAIR_NATIVE_REPOSITORY_MISMATCH"):
        native_verify(native_evidence)


def test_later_attempt_is_checked_again_instead_of_reusing_old_native_success(native_evidence):
    native_verify(native_evidence)
    native_evidence[3]["run_attempt"] = 2
    with pytest.raises(RecordError, match="ASHARE_REPAIR_NATIVE_RUN_MISMATCH"):
        native_verify(native_evidence)
    assert len([x for x in native_evidence[0].requests if "/actions/runs/" in x[1]]) == 2


def test_execution_code_requires_actual_native_ancestry_from_approved_transport(native_evidence):
    native_evidence[4].update(status="diverged", behind_by=1)
    with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
        native_verify(native_evidence)
    assert native_evidence[0].writes == 0


@pytest.mark.parametrize("field,value", [
    ("run_started_at", None), ("run_started_at", "2026-10-18T02:30:00"),
    ("updated_at", "2026-10-18T02:00:00Z"), ("updated_at", "not-a-T-date"),
])
def test_unknown_or_reversed_native_time_window_cannot_be_claimed_by_source_data(native_evidence, field, value):
    native_evidence[3][field] = value
    with pytest.raises(RecordError, match="ASHARE_REPAIR_NATIVE_RUN_TIME"):
        native_verify(native_evidence)


def test_source_claim_cannot_change_the_approved_transport_code(native_evidence):
    native_evidence[5]["transport_evidence"]["daily"].update(code_sha="d" * 40, verified=True)
    with pytest.raises(RecordError, match="ASHARE_REPAIR_TRANSPORT_IDENTITY"):
        native_verify(native_evidence)
    assert not any("/actions/runs/" in x[1] for x in native_evidence[0].requests)


def test_native_evidence_reads_consume_the_same_hard_api_budget(native_evidence):
    native_evidence[1]._requests = 510
    with pytest.raises(StoreUnavailable, match="REMOTE_REQUEST_LIMIT"):
        native_verify(native_evidence)
    assert native_evidence[1]._requests == 513 and native_evidence[0].writes == 0


def test_verified_private_evidence_rejects_a_live_public_reversal(native_evidence):
    native_evidence[0].meta["private"] = False
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        native_verify(native_evidence)


@pytest.mark.parametrize("change", ["privacy", "code-ancestry"])
def test_every_repair_unit_checks_native_private_code_with_its_existing_api(native_evidence, monkeypatch, change):
    model, _, _, native, compare, _ = native_evidence
    apis = []

    def new_api(token):
        assert token == STORE_TOKEN
        apis.append(transport(model))
        return apis[-1]

    monkeypatch.setattr(cli, "GitHubAPI", new_api)
    before = len(model.requests)
    open_unit = cli._open_remote(model.binding, STORE_TOKEN, require_private=True,
                                 execution_code_sha=EXECUTION_SHA)
    store, fresh, api = open_unit(True)
    assert api is apis[0] and store.backend.api is api and api._requests == len(model.requests) - before
    if change == "privacy":
        model.meta["private"] = False
    else:
        compare.update(status="diverged", behind_by=1)
    with pytest.raises(RecordError, match="ASHARE_REPAIR_PRIVATE_REQUIRED|COMMIT_OUTSIDE_BINDING"):
        open_unit(True)
    assert len(apis) == 2 and apis[0].rate_limits is apis[1].rate_limits
    assert sum(a._requests for a in apis) == len(model.requests) - before and model.writes == 0


@pytest.mark.parametrize("operation", ["sync", "status"])
def test_normal_operations_reject_nonempty_repair_input_before_execution(tmp_path, monkeypatch, operation):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation=operation, work_units="1", repair_request='{"verified":true}')
    called = []
    with pytest.raises(RecordError, match="ASHARE_REPAIR_EXPLICIT_OPERATION_REQUIRED"):
        cli.run_runtime(event, env, tmp_path / "rejected", execute=lambda **kw: called.append(kw))
    assert called == []


@pytest.mark.parametrize("request_json", ["", '{"schema":"ashare-terminal-repair-request-v1"}'])
def test_repair_host_forwards_only_explicit_request_and_actual_execution_sha(tmp_path, monkeypatch, request_json):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="repair", work_units="4", repair_request=request_json,
                           public_rights_confirmed=False)
    observed = []

    def open_remote(binding, token, *, require_private=False, execution_code_sha=None):
        assert binding.write_enabled and token == STORE_TOKEN and require_private is True
        assert execution_code_sha == SHA
        return "trusted-unit-factory"

    def execute(**kwargs):
        observed.append(kwargs)
        return {"receipt": {"status": "repair-complete", "source_requests": 0,
                           "supplier_rows_in_receipt": False}}

    monkeypatch.setattr(cli, "_open_remote", open_remote)
    assert cli.run_runtime(event, env, tmp_path / "repair", execute=execute) == 0
    assert observed[0]["operation"] == "repair" and observed[0]["execution_code_sha"] == SHA
    assert observed[0]["repair_request"] == (json.loads(request_json) if request_json else None)
    assert observed[0]["max_work_units"] == 4 and observed[0]["custody_authorized"] is True
    receipt = json.loads((tmp_path / "repair/result.json").read_text())
    assert "repair_request" not in receipt and "terminal_references" not in receipt


def test_public_rights_confirmation_does_not_enable_private_only_repair(tmp_path, monkeypatch, native_evidence):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="repair", work_units="1", public_rights_confirmed=True)
    model = native_evidence[0]
    model.meta["private"] = False
    monkeypatch.setattr(cli, "_records_runtime_binding", lambda operation: model.binding)
    monkeypatch.setattr(cli, "GitHubAPI", lambda token: transport(model))
    source_calls = []

    def execute(**kwargs):
        kwargs["open_unit"](True)
        source_calls.append("forbidden-source")
        raise AssertionError("Public repair reached source execution")

    out = tmp_path / "rejected-private"
    assert cli.run_runtime(event, env, out, execute=execute) == 2
    assert source_calls == [] and model.writes == 0
    receipt = json.loads((out / "result.json").read_text())
    assert receipt["reason_code"] == "ASHARE_REPAIR_PRIVATE_REQUIRED" and receipt["source_requests"] == 0


@pytest.mark.parametrize("field,value", [
    ("code_sha", "f" * 40), ("access_and_custody_confirmed", False),
    ("work_units", "33"), ("repair_request", '{"x":"' + "a" * 16000 + '"}'),
    ("repair_request", '{"schema":"one","schema":"another"}'),
])
def test_repair_still_rejects_changed_code_scope_or_unbounded_input_before_opening_a_unit(tmp_path, monkeypatch, field, value):
    monkeypatch.chdir(ROOT_DIR)
    event, env = runtime_input(tmp_path)
    event["inputs"].update(operation="repair", work_units="1")
    event["inputs"][field] = value
    called = []
    with pytest.raises((RecordError, ValueError)):
        cli.run_runtime(event, env, tmp_path / "invalid-repair", execute=lambda **kw: called.append(kw))
    assert called == []


def test_repair_job_has_only_needed_native_read_permission_and_same_source_serial_group():
    workflow = yaml.load((ROOT_DIR / ".github/workflows/ashare-data.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["concurrency"] == {"group": "decision-os-relay-source", "cancel-in-progress": "false"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["repair_request"]["default"] == ""
    repair = workflow["jobs"]["repair"]
    assert repair["permissions"] == {"contents": "write", "actions": "read"}
    assert "inputs.operation == 'repair'" in repair["if"]
    assert "github.event.repository.private == true" in repair["if"]
    assert "inputs.public_rights_confirmed" not in repair["if"]
    assert "actions" not in workflow["jobs"]["sync"]["permissions"]
    assert "secrets." not in str(workflow["jobs"]["status"])
    source = next(s for s in repair["steps"] if "python -m" in s.get("run", ""))
    assert source["env"] == {cli.MARKET_TOKEN_ENV: "${{ github.token }}",
                             "TUSHARE_PROXY_API_KEY": "${{ secrets.TUSHARE_PROXY_API_KEY }}"}
    artifact = next(s for s in repair["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert artifact["with"]["path"] == "${{ runner.temp }}/dos2-ashare-data/result.json"


def repair_runtime_fixture(monkeypatch):
    from test_market_terminal_repair import repair_fixture
    host = UnitHost()
    store, fresh, _ = host.open(True)
    fixture = repair_fixture(backend_store=(store.backend, store, fresh))

    def native_context(document, *, store, api, execution_code_sha):
        # Native wire authentication is independently exercised above. Here the
        # shared synthetic corpus tests the real runtime/core/custody boundary.
        assert store.backend.api is api and execution_code_sha == fixture["execution_code_sha"]
        return fixture["verify_evidence"](document)

    monkeypatch.setattr(runtime, "verify_native_repair_evidence", native_context)
    return fixture, host


def execute_repair(fixture, host, opener, *, activate=False, **options):
    clock = fixture["clock"]
    args = {"operation": "repair", "plan": fixture["plan"], "registry": REGISTRY,
        "open_unit": host.open, "attempt_id": "run-200-1", "access_confirmed": True,
        "custody_authorized": True, "now": NOW, "execution_code_sha": fixture["execution_code_sha"],
        "sync_options": {"client": RelayHTTP(opener=opener, credential="synthetic-key-only"), "clock": clock.now,
            "monotonic": clock.monotonic, "sleep": clock.sleep}}
    if activate:
        args["repair_request"] = fixture["grant_request"]
    return runtime.run_collection(**{**args, **options})


def test_real_runtime_grant_only_unit_then_four_repair_units_and_no_finished_replay(monkeypatch):
    fixture, host = repair_runtime_fixture(monkeypatch)
    opened_before = len(host.opened)
    opener = CollectionOpener()
    result = execute_repair(fixture, host, opener, activate=True, max_work_units=32)
    receipt = result["receipt"]
    assert receipt["status"] == "repair-complete" and receipt["source_requests"] == len(opener.calls) == 4
    assert [u["source_requests"] for u in receipt["work_units"]] == [0, 1, 1, 1, 1]
    assert len(host.opened) - opened_before == 5 and len(fixture["callbacks"]) == 1
    assert receipt["latest"]["unit_counts"]["failed"] == 4
    assert receipt["latest"]["effective_unit_counts"]["failed"] == 0
    assert receipt["latest"]["all_partitions_retained"] is False
    assert receipt["latest"]["latest_attempt"]["repair_execution_code_sha"] == fixture["execution_code_sha"]
    assert receipt["repair"]["reserved_requests"] == 4 and receipt["repair"]["max_new_requests"] == 32
    before = host.shared["head"]
    repeated = execute_repair(fixture, host, opener, max_work_units=32)
    assert repeated["receipt"]["status"] == "repair-complete"
    assert repeated["receipt"]["source_requests"] == 0 and len(repeated["receipt"]["work_units"]) == 1
    assert host.shared["head"] == before and len(opener.calls) == 4


def test_runtime_normal_sync_preserves_grant_and_does_not_consume_repair(monkeypatch):
    from test_market_collection_sync import source_calls
    fixture, host = repair_runtime_fixture(monkeypatch)
    granted = execute_repair(fixture, host, CollectionOpener(), activate=True)
    assert granted["receipt"]["source_requests"] == 0 and granted["receipt"]["status"] == "partial"
    opener = CollectionOpener()
    ordinary = execute_repair(fixture, host, opener, operation="sync")
    assert source_calls(opener) == [("suspend_d", "20261014", 0)]
    assert ordinary["receipt"]["latest"]["repair"]["reserved_requests"] == 0
    assert ordinary["receipt"]["latest"]["repair"]["counts"]["pending"] == 4


def test_attempt_only_publication_cannot_manufacture_runtime_progress(monkeypatch):
    from decision_kernel.application import market_collection_repair as repair
    from decision_kernel.application.market_collection import collection_summary
    from decision_kernel.application.market_collection_custody import retain_market_collection, restore_market_collection
    fixture, host = repair_runtime_fixture(monkeypatch)
    execute_repair(fixture, host, CollectionOpener(), activate=True)

    def no_source_progress(**kwargs):
        collection = restore_market_collection(kwargs["previous_ref"], REGISTRY, kwargs["fresh_reader"]())["collection"]
        fixture["clock"].sleep(1)
        collection["attempt"].update(id=kwargs["attempt_id"], updated_at=fixture["clock"].now())
        saved = retain_market_collection(collection, REGISTRY, kwargs["store"],
            predecessor=kwargs["previous_ref"], fresh_reader=kwargs["fresh_reader"], custody_authorized=True)
        return {"status": "partial", "collection": collection, "reference": saved["reference"],
                "receipt": collection_summary(collection, saved["reference"])}

    monkeypatch.setattr(repair, "repair_collection", no_source_progress)
    opener = CollectionOpener()
    before = len(host.opened)
    result = execute_repair(fixture, host, opener, max_work_units=32)
    assert result["receipt"]["status"] == "blocked" and result["receipt"]["reason_code"] == "ASHARE_NO_PROGRESS"
    assert len(host.opened) - before == 1 and opener.calls == []
