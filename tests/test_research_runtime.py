"""Research host boundary over the existing real-Git API model; not live research."""
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
import yaml

from decision_kernel.domain.records import Conflict, MissingObject, RecordError, UnknownWrite, encoded
from decision_kernel.interfaces import research_runtime as runtime
from decision_kernel.methods.research import research_method
from test_github_store import GitAPI

CODE = "a" * 40


@pytest.fixture
def host(tmp_path):
    api = GitAPI(tmp_path / "synthetic.git")
    api.binding = replace(api.binding, visibility="public", write_enabled=False)
    anchor = api.git.commit({"store.json": api.binding.marker()}, expected=api.git.head())
    api.binding = replace(api.binding, anchor=anchor)
    api.meta["private"] = False
    return api


def source(**changes):
    return {"source_id": "sample", "kind": "content", "title": "合成来源",
            "origin": "synthetic:research-runtime", "text": "合成资料，不是真实投资研究。", **changes}


def request(**changes):
    return {"case_id": "example", "kind": "full", "mandate": "合成有限委托",
            "questions": [{"question_id": "economics", "text": "合成问题"}], **changes}


def work(ref, **changes):
    return {"request": request(), "revision_id": "r1", "method_sha256": research_method("full")["sha256"],
            "progress": {"body": "合成部分成果", "stop": "continue", "stop_reason": "仍需查证",
                         "sources": [ref], "findings": [{"question_id": "economics", "status": "open",
                         "finding": "尚未完成", "remaining_kind": "acquired-unanalysed", "next_action": "继续核算"}]},
            **changes}


def inputs(operation, payload, submission="", expected=""):
    return {"operation": operation, "payload": json.dumps(payload, ensure_ascii=False), "code_sha": CODE,
            "submission_id": submission, "expected_head": expected}


def invoke(host, tmp_path, operation, payload, submission="", expected=None):
    original = inputs(operation, payload, submission,
                      host.git.head() if expected is None and operation in runtime.WRITE_OPERATIONS else expected or "")
    return runtime.run_command(host.binding, api=host, inputs=original, code_sha=CODE,
                               output=tmp_path, run_id="123", attempt="1")


def test_full_source_checkpoint_and_exact_read_roundtrip(host, tmp_path):
    prepared = invoke(host, tmp_path / "prepare", "prepare", {"request": request()})
    assert prepared["result"]["previous_context"] is None
    assert prepared["result"]["method"]["kind"] == "full" and host.writes == 0
    saved = invoke(host, tmp_path / "source", "put-source", source(), "source-one")
    ref = saved["result"]["reference"]
    assert saved["write"] == "published" and not host.binding.write_enabled
    first = invoke(host, tmp_path / "first", "checkpoint", work(ref), "research-one")
    revision = first["result"]["reference"]
    assert first["write"] == "published" and first["research_assessment"] == "not-performed"
    restored = invoke(host, tmp_path / "restore", "resume", {"reference": revision})
    assert restored["result"]["research"]["reference"] == revision
    assert restored["result"]["source_page"]["items"][0]["view"]["data"] == source()["text"]
    continued = invoke(host, tmp_path / "next", "prepare", {"case_id": "example"})
    assert continued["result"]["request"]["predecessor"] == revision
    second_work = work(ref, request=continued["result"]["request"], revision_id="r2")
    second = invoke(host, tmp_path / "second", "checkpoint", second_work, "research-two")
    replay = invoke(host, tmp_path / "reconcile", "reconcile-checkpoint", work(ref), "research-one")
    assert replay["result"]["reference"] == revision
    assert replay["write"] == "reconciled-without-write" and host.writes == 3
    assert second["result"]["reference"] != revision


def test_cursor_remains_on_original_revision_after_new_checkpoint(host, tmp_path):
    ref = invoke(host, tmp_path / "s1", "put-source", source(), "s1")["result"]["reference"]
    ref2 = invoke(host, tmp_path / "s2", "put-source", source(source_id="second"), "s2")["result"]["reference"]
    paper = work(ref)
    paper["progress"]["sources"].append(ref2)
    first = invoke(host, tmp_path / "r1", "checkpoint", paper, "r1")["result"]["reference"]
    page = invoke(host, tmp_path / "p1", "resume", {"case_id": "example", "source_limit": 1})["result"]
    paper["request"]["predecessor"], paper["revision_id"] = first, "r2"
    invoke(host, tmp_path / "r2", "checkpoint", paper, "r2")
    last = invoke(host, tmp_path / "p2", "resume", {"cursor": page["next_cursor"]})["result"]
    assert last["research"]["reference"] == first and last["source_page"]["offset"] == 1


@pytest.mark.parametrize("operation", ["put-source", "checkpoint"])
def test_lost_receipt_has_original_inputs_and_readonly_recovery(host, tmp_path, operation):
    ref = invoke(host, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]
    payload = source(source_id="lost") if operation == "put-source" else work(ref)
    expected = host.git.head()
    def lose(_):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    host.after_write = lose
    with pytest.raises(UnknownWrite, match="READONLY_RECONCILIATION"):
        invoke(host, tmp_path / "lost", operation, payload, "original-intent", expected)
    raw_inputs = json.loads((tmp_path / "lost/inputs.json").read_text())
    assert encoded(json.loads(raw_inputs["payload"])) == encoded(payload)
    assert raw_inputs["expected_head"] == expected
    assert json.loads((tmp_path / "lost/report.json").read_text())["status"] == "unknown-write"
    host.after_write = None
    published = host.git.head()
    # The branch may advance independently before the missing response is recovered.
    invoke(host, tmp_path / "later", "put-source", source(source_id="later"), "later")
    count = host.writes
    reconcile = "reconcile-source" if operation == "put-source" else "reconcile-checkpoint"
    recovered = invoke(host, tmp_path / "recovered", reconcile, payload, "original-intent", expected)
    reference = recovered["result"]["reference"]
    assert (reference.get("manifest") or reference)["commit"] == published
    assert recovered["write"] == "reconciled-without-write" and host.writes == count


def test_source_reconcile_missing_is_not_a_write(host, tmp_path):
    with pytest.raises(MissingObject, match="SUBMISSION_NOT_FOUND_NO_WRITE"):
        invoke(host, tmp_path / "missing", "reconcile-source", source(), "missing")
    assert host.writes == 0
    report = json.loads((tmp_path / "missing/report.json").read_text())
    assert report["status"] == "failed" and report["write"] == "not-requested"


def test_unpublished_research_reconcile_cannot_write(host, tmp_path):
    ref = invoke(host, tmp_path / "src", "put-source", source(), "src")["result"]["reference"]
    with pytest.raises(MissingObject, match="SUBMISSION_NOT_FOUND_NO_WRITE"):
        invoke(host, tmp_path / "none", "reconcile-checkpoint", work(ref), "none")
    assert host.writes == 1


def test_original_intent_conflict_and_stale_head_never_overwrite(host, tmp_path):
    initial = host.git.head()
    invoke(host, tmp_path / "s", "put-source", source(), "same")
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        invoke(host, tmp_path / "bad", "reconcile-source", source(text="different"), "same")
    with pytest.raises(Conflict, match="PLANNED_HEAD_CHANGED"):
        invoke(host, tmp_path / "stale", "put-source", source(source_id="new"), "new", initial)
    # A legitimate replay with the *old planned head* remains possible, with no mutation.
    repeated = invoke(host, tmp_path / "repeated", "put-source", source(), "same", initial)
    assert repeated["write"] == "replayed" and host.writes == 1


def test_native_cas_race_reports_unknown_without_automatic_resend(host, tmp_path):
    def race():
        host.git.commit({"submissions/other.json": b"{}\n"}, expected=host.git.head())
    host.before_write = race
    with pytest.raises(UnknownWrite):
        invoke(host, tmp_path / "race", "put-source", source(), "race")
    assert host.writes == 1
    assert host.git.read(host.git.head(), "submissions/other.json") == b"{}\n"


def test_readback_failure_does_not_claim_saved_and_recovers(host, tmp_path):
    original = runtime._CommandStore.reference
    def fail(self, commit, path):
        if self.published is not None:
            raise RecordError("READBACK_INTERRUPTED")
        return original(self, commit, path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime._CommandStore, "reference", fail)
        with pytest.raises(UnknownWrite):
            invoke(host, tmp_path / "fail", "put-source", source(), "intent")
    recovered = invoke(host, tmp_path / "good", "reconcile-source", source(), "intent")
    assert recovered["status"] == "completed" and host.writes == 1


def test_method_pin_rejects_changed_method_before_new_write(host, tmp_path):
    ref = invoke(host, tmp_path / "src", "put-source", source(), "src")["result"]["reference"]
    with pytest.raises(RecordError, match="RESEARCH_METHOD_CHANGED"):
        invoke(host, tmp_path / "method", "checkpoint", work(ref, method_sha256="0" * 64), "bad")
    assert host.writes == 1


def test_source_text_is_data_not_shell_or_automatic_url_fetch(host, tmp_path):
    text = '::error::not-a-workflow-command\n$(touch /tmp/should-not-run); ${{ secrets.TEST }}'
    ref = invoke(host, tmp_path / "src", "put-source", source(text=text, origin="https://example.invalid/private"), "literal")["result"]["reference"]
    view = invoke(host, tmp_path / "read", "read-source", ref)
    assert view["result"]["data"] == text
    assert all(path.startswith(("/repos/", "/graphql")) for _, path, _ in host.requests)


@pytest.mark.parametrize("change", [
    {"operation": "delete"}, {"operation": "decision"}, {"token": "invented"},
    {"branch": "main"}, {"code_sha": "main"}, {"expected_head": "latest", "operation": "put-source"},
    {"payload": "[]"}, {"payload": '{"a":1,"a":2}'}, {"payload": '{"a":NaN}'},
    {"payload": '{"a":1e999}'}, {"payload": '{"a":"\\ud800"}'}, {"payload": True},
    {"payload": '{"a":"' + 'x' * runtime.MAX_PAYLOAD_BYTES + '"}'},
])
def test_dispatch_input_rejects_ambiguous_or_oversized_fields(change):
    with pytest.raises(RecordError):
        runtime.parse_inputs({**inputs("prepare", {"request": request()}), **change})


def test_encoded_multibyte_payload_budget_and_schema_no_authority(host, tmp_path):
    with pytest.raises(RecordError, match="TOO_LARGE"):
        runtime.parse_inputs(inputs("prepare", {"x": "铜" * 22000}))
    for op, payload in [("prepare", {"request": request(), "actor": "human"}),
                        ("resume", {"case_id": "example", "request": request()})]:
        with pytest.raises(RecordError, match="SELECTOR"):
            invoke(host, tmp_path / op, op, payload)
    assert host.writes == 0


def test_code_and_public_binding_preflight_no_network(host, tmp_path):
    original = inputs("prepare", {"request": request()})
    with pytest.raises(RecordError, match="RUNTIME_CODE_CHANGED"):
        runtime.run_command(host.binding, api=host, inputs=original, code_sha="b" * 40, output=tmp_path / "bad")
    for binding in (replace(host.binding, visibility="private"), replace(host.binding, write_enabled=True)):
        with pytest.raises(RecordError, match="PUBLIC_READ_ONLY_CONFIG_REQUIRED"):
            runtime.run_command(binding, api=host, inputs=original, code_sha=CODE, output=tmp_path / "bad")
    assert host.requests == []


def native_environment(patch, host, tmp_path, command, mode="read"):
    patch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/store-binding.json").write_bytes(encoded(asdict(host.binding)))
    event = tmp_path / "event.json"
    event.write_bytes(encoded({"inputs": command}))
    for key, value in {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
                       "GITHUB_REPOSITORY": host.binding.repository, "GITHUB_REPOSITORY_ID": "1234",
                       "GITHUB_SHA": CODE, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                       "GITHUB_WORKFLOW_REF": host.binding.repository + "/" + runtime.WORKFLOW + "@refs/heads/main",
                       "GITHUB_EVENT_PATH": str(event), "DOS2_RESEARCH_MODE": mode}.items():
        patch.setenv(key, value)
    patch.setattr(runtime.GitHubAPI, "from_environment", lambda: host)


def test_native_main_bridge_retains_result_without_printing_body(host, tmp_path, monkeypatch, capsys):
    native_environment(monkeypatch, host, tmp_path, inputs("put-source", source(), "cli", host.git.head()), "write")
    assert runtime.main(["--out", str(tmp_path / "out")]) == 0
    console = capsys.readouterr()
    assert source()["text"] not in console.out and console.err == ""
    report = json.loads((tmp_path / "out/report.json").read_text())
    assert report["run_id"] == "123" and report["write"] == "published"


@pytest.mark.parametrize("key,value", [
    ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/records"),
    ("GITHUB_WORKFLOW_REF", "other/main"), ("GITHUB_SHA", "b" * 40),
    ("GITHUB_REPOSITORY_ID", "9999"), ("DOS2_RESEARCH_MODE", "read"),
])
def test_native_context_cannot_request_write_token_in_wrong_job(host, tmp_path, monkeypatch, capsys, key, value):
    native_environment(monkeypatch, host, tmp_path, inputs("put-source", source(), "denied", host.git.head()), "write")
    monkeypatch.setenv(key, value)
    def deny_token():
        raise AssertionError("must reject before retrieving token")
    monkeypatch.setattr(runtime.GitHubAPI, "from_environment", deny_token)
    assert runtime.main(["--out", str(tmp_path / "denied")]) == 2
    assert host.writes == 0 and "error" in capsys.readouterr().err


def test_workflow_has_separate_read_write_jobs_and_data_never_in_shell():
    path = Path(__file__).resolve().parents[1] / runtime.WORKFLOW
    flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(flow["on"]) == {"workflow_dispatch"} and flow["permissions"] == {}
    assert set(flow["jobs"]) == {"read", "write"}
    assert flow["jobs"]["read"]["permissions"] == {"contents": "read", "actions": "read"}
    assert flow["jobs"]["write"]["permissions"] == {"contents": "write"}
    assert set(flow["on"]["workflow_dispatch"]["inputs"]["operation"]["options"]) == runtime.WRITE_OPERATIONS | runtime.READ_OPERATIONS
    for name, job in flow["jobs"].items():
        assert "refs/heads/main" in job["if"] and job["timeout-minutes"] == "10"
        for step in job["steps"]:
            assert "continue-on-error" not in step
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {"ref": "${{ github.sha }}", "persist-credentials": "false"}
            if "run" in step:
                assert "${{" not in step["run"] and "inputs." not in step["run"]
                assert step["env"]["DOS2_RESEARCH_MODE"] == name
                assert step["env"]["DECISION_OS_STORE_TOKEN"] == "${{ github.token }}"
