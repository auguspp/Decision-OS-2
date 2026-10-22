"""Native public/private daily routing without source or records access."""
from copy import deepcopy
import json
from pathlib import Path
import shlex

import pytest
import yaml

from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces import daily_visibility_cli as cli

SHA = "a" * 40


def context(operation="status", *, scheduled=False):
    env = {"GITHUB_REPOSITORY": cli.REPOSITORY, "GITHUB_REPOSITORY_ID": str(cli.REPOSITORY_ID),
        "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": cli.REPOSITORY + "/" + cli.WORKFLOW + "@refs/heads/main",
        "GITHUB_EVENT_NAME": "schedule" if scheduled else "workflow_dispatch",
        "GITHUB_RUN_ID": "8001", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": SHA}
    event = {"schedule": "35 10 * * 1-5"} if scheduled else {"inputs": {"operation": operation, "code_sha": SHA}}
    return event, env


class NativeAPI:
    def __init__(self, *, private=False, scheduled=False):
        self.calls = []
        self.meta = {"id": cli.REPOSITORY_ID, "full_name": cli.REPOSITORY,
            "private": private, "archived": False, "default_branch": "main"}
        self.run = {"id": 8001, "head_sha": SHA, "head_branch": "main", "run_attempt": 1,
            "event": "schedule" if scheduled else "workflow_dispatch", "path": cli.WORKFLOW,
            "name": cli.WORKFLOW_NAME, "display_title": cli.WORKFLOW_NAME, "status": "in_progress",
            "repository": {"id": cli.REPOSITORY_ID}, "head_repository": {"id": cli.REPOSITORY_ID},
            "created_at": "2026-10-19T12:10:00+00:00"}
        self.main = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": SHA}}
        self.ci = {"total_count": 1, "workflow_runs": [{
            "id": 6001, "head_sha": SHA, "head_branch": "main", "event": "push", "run_attempt": 1,
            "path": ".github/workflows/ci.yml", "name": "decision-os-ci",
            "status": "completed", "conclusion": "success", "repository": {"id": cli.REPOSITORY_ID},
            "head_repository": {"id": cli.REPOSITORY_ID}}]}
        self.final_meta = None

    def request(self, method, path):
        assert method == "GET", "This probe must never write."
        self.calls.append((method, path))
        if path == cli.PREFIX:
            return deepcopy(self.final_meta if len(self.calls) >= 3 and self.final_meta is not None else self.meta)
        if path == cli.PREFIX + "/git/ref/heads/main":
            return deepcopy(self.main)
        if path.startswith(cli.PREFIX + "/actions/workflows/ci.yml/runs?"):
            return deepcopy(self.ci)
        assert path == cli.PREFIX + "/actions/runs/8001", "No data, artifact, source or records endpoints."
        return deepcopy(self.run)


@pytest.mark.parametrize("operation", ["status", "update", "recover-late"])
@pytest.mark.parametrize("private", [False, True])
def test_explicit_routes_and_truthful_metadata_only_result(operation, private):
    event, env = context(operation)
    api = NativeAPI(private=private)
    report, code = cli.preflight(event, env, api)
    assert report["repository_visibility"] == ("private" if private else "public")
    assert report["operation"] == ("status" if operation == "status" else "update")
    assert report["invocation"] == operation and report["code_sha"] == SHA
    assert report["scope"] == "native-metadata-only"
    assert report["source_requests"] == 0 and report["records_read"] is False and report["records_written"] is False
    assert report["data_freshness"] == "not-checked" and report["historical_exposure_remediated"] is False
    assert api.calls == [("GET", cli.PREFIX), ("GET", cli.PREFIX + "/actions/runs/8001"), ("GET", cli.PREFIX)]
    if private:
        assert code == 0 and report["status"] == "private-runtime-pending"
    elif operation == "status":
        assert code == 0 and report["status"] == "metadata-only"
    else:
        assert code == 2 and report["status"] == "blocked"
        assert report["reason_code"] == cli.PUBLIC_REASON
    text = cli.summary(report)
    assert "新鲜度：未检查" in text and "未隐藏或清理" in text


@pytest.mark.parametrize("private", [False, True])
def test_schedule_without_repository_payload_uses_native_metadata(private):
    event, env = context(scheduled=True)
    assert "repository" not in event
    api = NativeAPI(private=private, scheduled=True)
    report, code = cli.preflight(event, env, api)
    assert code == (0 if private else 2)
    assert report["operation"] == "update" and report["event"] == "schedule"
    assert len(api.calls) == (7 if private else 3)
    if private:
        assert report["native_proof"]["main_ci_run"] == 6001


@pytest.mark.parametrize("private", [False, True])
def test_spoofed_or_stale_event_privacy_is_not_an_authorization(private):
    event, env = context("update")
    event["repository"] = {"id": -1, "private": not private, "full_name": "attacker/repo"}
    report, code = cli.preflight(event, env, NativeAPI(private=private))
    assert report["repository_visibility"] == ("private" if private else "public")
    assert code == (0 if private else 2)


@pytest.mark.parametrize("change", [
    {"GITHUB_REPOSITORY": "someone/fork"}, {"GITHUB_REPOSITORY_ID": "1"},
    {"GITHUB_REF": "refs/pull/1/merge"}, {"GITHUB_RUN_ATTEMPT": "2"},
    {"GITHUB_EVENT_NAME": "pull_request_target"}, {"GITHUB_EVENT_NAME": "pull_request"},
    {"GITHUB_WORKFLOW_REF": cli.REPOSITORY + "/" + cli.WORKFLOW + "@refs/heads/other"},
    {"GITHUB_SHA": "HEAD"}, {"GITHUB_RUN_ID": "01"}, {"GITHUB_RUN_ID": "8001\ninjected=true"},
])
def test_wrong_context_fails_before_any_api_request(change):
    event, env = context()
    api = NativeAPI()
    with pytest.raises(RecordError):
        cli.preflight(event, {**env, **change}, api)
    assert api.calls == []


@pytest.mark.parametrize("event", [None, [], {},
    {"inputs": {"operation": "delete", "code_sha": SHA}},
    {"inputs": {"operation": "status", "code_sha": "b" * 40}},
    {"inputs": {"operation": "update", "code_sha": SHA, "public_rights_confirmed": True}},
])
def test_bad_dispatch_or_added_rights_switch_cannot_open_public_runtime(event):
    _, env = context()
    api = NativeAPI()
    with pytest.raises(RecordError):
        cli.preflight(event, env, api)
    assert api.calls == []


@pytest.mark.parametrize("change", [
    {"id": float(cli.REPOSITORY_ID)}, {"id": 1}, {"full_name": "someone/repo"},
    {"private": "false"}, {"private": 0}, {"private": 1}, {"private": None},
    {"archived": True}, {"archived": 0}, {"default_branch": "records"},
    {"visibility": "private"},
])
def test_malformed_or_wrong_native_repository_fails_closed(change):
    event, env = context()
    api = NativeAPI()
    api.meta.update(change)
    with pytest.raises(RecordError, match="DAILY_VISIBILITY_REPOSITORY"):
        cli.preflight(event, env, api)
    assert len(api.calls) == 1


@pytest.mark.parametrize("change", [
    {"id": 8001.0}, {"id": 8002}, {"head_sha": "b" * 40}, {"head_branch": "records"},
    {"event": "pull_request"}, {"run_attempt": True}, {"run_attempt": 2},
    {"path": ".github/workflows/ci.yml"}, {"name": "other"}, {"status": "completed"},
    {"repository": {"id": 1}}, {"head_repository": {"id": float(cli.REPOSITORY_ID)}},
])
def test_native_run_identity_cannot_be_borrowed_from_another_job(change):
    event, env = context()
    api = NativeAPI()
    api.run.update(change)
    with pytest.raises(RecordError, match="DAILY_VISIBILITY_NATIVE_RUN"):
        cli.preflight(event, env, api)
    assert len(api.calls) == 2


@pytest.mark.parametrize("private", [False, True])
def test_live_visibility_change_during_probe_does_not_release_a_route(private):
    event, env = context()
    api = NativeAPI(private=private)
    api.final_meta = {**api.meta, "private": not private}
    released = []
    with pytest.raises(RecordError, match="DAILY_VISIBILITY_CHANGED"):
        released.append(cli.preflight(event, env, api))
    assert released == []


def hosted(tmp_path, operation="status", scheduled=False):
    event, env = context(operation, scheduled=scheduled)
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    env.update(GITHUB_EVENT_PATH=str(path), DECISION_OS_STORE_TOKEN="synthetic-job-token",
        GITHUB_OUTPUT=str(tmp_path / "output"), GITHUB_STEP_SUMMARY=str(tmp_path / "summary"))
    return env


@pytest.mark.parametrize("operation,expected", [("status", 0), ("update", 2), ("recover-late", 2)])
def test_real_cli_publishes_only_bounded_safe_metadata(tmp_path, capsys, operation, expected):
    env = hosted(tmp_path, operation)
    api = NativeAPI()
    api.meta["private_sentinel"] = "UNAPPROVED-RAW-CONTENT"
    api.run["provider_body"] = "UNAPPROVED-RAW-CONTENT"
    received = []
    def factory(token):
        received.append(token)
        return api
    assert cli.main([], env=env, api_factory=factory) == expected
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert received == ["synthetic-job-token"] and output.err == ""
    assert Path(env["GITHUB_OUTPUT"]).read_text() == "visibility=public\n"
    assert report["records_read"] is False and report["records_written"] is False
    text = output.out + Path(env["GITHUB_STEP_SUMMARY"]).read_text()
    assert "UNAPPROVED-RAW-CONTENT" not in text and env[cli.TOKEN_ENV] not in text
    assert not any(p.suffix in {".html", ".zip", ".bundle"} for p in tmp_path.iterdir())


def test_missing_dedicated_token_does_not_fall_back_to_ci_token(tmp_path, capsys):
    env = hosted(tmp_path)
    del env[cli.TOKEN_ENV]
    env["GITHUB_TOKEN"] = "must-not-be-used"
    def factory(_):
        pytest.fail("No client may be created without the explicit metadata token.")
    assert cli.main([], env=env, api_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "DAILY_VISIBILITY_TOKEN"
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@pytest.mark.parametrize("failure", [RuntimeError("private payload, https://secret.invalid/raw, synthetic-job-token"),
    RecordError("PRIVATE_BODY_123"), RecordError("DAILY_VISIBILITY_CHANGED")])
def test_errors_never_echo_data_or_grant_output(tmp_path, capsys, failure):
    env = hosted(tmp_path)
    class Failing(NativeAPI):
        def request(self, method, path):
            raise failure
    assert cli.main([], env=env, api_factory=lambda _: Failing()) == 2
    result = capsys.readouterr()
    value = json.loads(result.out)
    assert value["status"] == "failed" and value["reason_code"] in cli.ERROR_CODES
    assert "private payload" not in result.out and "secret.invalid" not in result.out
    assert "PRIVATE_BODY_123" not in result.out and "synthetic-job-token" not in result.out
    assert result.err == "" and not Path(env["GITHUB_OUTPUT"]).exists()


@pytest.mark.parametrize("raw", [b'null', b'{"inputs": {}, "inputs": {}}', b'{invalid', b' ' * (cli.MAX_EVENT_BYTES + 1)])
def test_invalid_native_event_is_bounded_and_not_logged(tmp_path, capsys, raw):
    env = hosted(tmp_path)
    Path(env["GITHUB_EVENT_PATH"]).write_bytes(raw)
    def factory(_):
        pytest.fail("Malformed native input must be rejected before any client.")
    assert cli.main([], env=env, api_factory=factory) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not Path(env["GITHUB_OUTPUT"]).exists()


def test_visibility_reversal_in_cli_does_not_leave_a_success_output(tmp_path, capsys):
    env = hosted(tmp_path)
    api = NativeAPI(private=True)
    api.final_meta = {**api.meta, "private": False}
    assert cli.main([], env=env, api_factory=lambda _: api) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == "DAILY_VISIBILITY_CHANGED"
    assert not Path(env["GITHUB_OUTPUT"]).exists()


def test_workflow_native_preflight_dispatch_only_keeps_original_gates():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / cli.WORKFLOW).read_text(), Loader=yaml.BaseLoader)
    assert workflow["permissions"] == {} and workflow["concurrency"]["group"] == "decision-os-relay-source"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    job = workflow["jobs"]["visibility"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert "repository.private" not in job["if"] and "schedule" in job["if"]
    assert job["outputs"] == {"visibility": "${{ steps.visibility.outputs.visibility }}",
                              "runtime_authorized": "${{ steps.visibility.outputs.runtime_authorized }}",
                              "admitted": "${{ steps.visibility.outputs.admitted }}"}
    commands = [step for step in job["steps"] if "run" in step]
    assert len(commands) == 1 and commands[0]["id"] == "visibility"
    assert shlex.split(commands[0]["run"]) == ["python", "-m", cli.__name__, "--use-runtime-authorization"]
    env = {**workflow.get("env", {}), **job.get("env", {}), **commands[0].get("env", {})}
    assert env == {"DECISION_OS_STORE_TOKEN": "${{ github.token }}"}
    assert "secrets." not in json.dumps(job) and "upload-artifact" not in json.dumps(job)
    for name in ("status", "daily"):
        child = workflow["jobs"][name]
        assert child["needs"] == "visibility"
        assert child["if"].startswith("needs.visibility.outputs.runtime_authorized == 'true' && ")
        assert "needs.visibility.outputs.admitted == 'true'" in child["if"]
        assert "always()" not in child["if"] and "github.event.repository.private" not in child["if"]
        # Do not remove the original last native check guarding full outputs.
        assert any(step.get("id") == "release" for step in child["steps"])
        for step in child["steps"]:
            if step.get("uses", "").startswith("actions/upload-artifact@") or "GITHUB_STEP_SUMMARY" in step.get("run", ""):
                assert "steps.release.outcome == 'success'" in step["if"]
    assert workflow["jobs"]["status"]["permissions"]["contents"] == "read"
    assert workflow["jobs"]["daily"]["permissions"]["contents"] == "write"


@pytest.mark.parametrize("env", [[], "invalid", 1])
def test_invalid_environment_returns_safe_failure_without_traceback(env, capsys):
    assert cli.main([], env=env) == 2
    value = json.loads(capsys.readouterr().out)
    assert value["reason_code"] == "DAILY_VISIBILITY_CONTEXT"
