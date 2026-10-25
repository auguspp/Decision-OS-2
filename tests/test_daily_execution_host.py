"""Approved preflight output reaches the real daily entry and final release."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure import daily_execution as execution
from decision_kernel.interfaces import daily_market_cli as market
from decision_kernel.interfaces import daily_visibility_cli as visibility
from test_daily_execution import Native


def environment(native, tmp_path):
    event = {"inputs": {"operation": "update", "code_sha": native.env["GITHUB_SHA"]}}
    event_path = tmp_path / "event.json"
    event_path.write_bytes(encoded(event))
    native.env.update(GITHUB_EVENT_PATH=str(event_path), RUNNER_TEMP=str(tmp_path),
        DECISION_OS_STORE_TOKEN="synthetic-approved-runtime")
    native.run["created_at"] = datetime.now(timezone.utc).isoformat()
    return event, native.env


def test_preflight_output_drives_exact_pinned_daily_and_release(tmp_path, monkeypatch, capsys):
    native = Native(pinned=True)
    event, env = environment(native, tmp_path)
    output = tmp_path / "outputs"
    env["GITHUB_OUTPUT"] = str(output)
    assert visibility.main(["--use-runtime-authorization"], env=env, api_factory=lambda _: native) == 0
    report = json.loads(capsys.readouterr().out)
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert outputs["admitted"] == "true"
    assert outputs["execution_sha"] == report["code_sha"] != env["GITHUB_SHA"]
    env[execution.ENVIRONMENT_KEY] = outputs["execution"]
    held = json.loads(outputs["execution"])
    checked, consumed = [], []
    monkeypatch.setattr(market, "verify_checkout", lambda selection: checked.append(deepcopy(selection)))
    def execute(**options):
        consumed.append(options["operation"])
        return {"receipt": {"schema": "daily-market-runtime-v1", "status": "read",
            "source_requests": 0}, "control": {}}
    out = tmp_path / "daily"
    native.advance()  # Ordinary main advancement cannot rebind this run.
    assert market.run_runtime(event, env, out, api=native, execute=execute) == 0
    result = json.loads((out / "result.json").read_bytes())
    assert {key: result[key] for key in held} == held
    assert consumed == ["update"] and len(checked) == 1
    assert {key: checked[0][key] for key in held} == held
    capsys.readouterr()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(market, "GitHubAPI", lambda _: native)
    assert market.main(["release"]) == 0
    assert len(checked) == 2 and capsys.readouterr().out == ""
    assert env["GITHUB_SHA"] == env["GITHUB_WORKFLOW_SHA"] == held["workflow_sha"]


@pytest.mark.parametrize("command", ["runtime", "release"])
def test_business_entry_cannot_reselect_when_preflight_envelope_is_missing(
        tmp_path, monkeypatch, capsys, command):
    native = Native()
    event, env = environment(native, tmp_path)
    monkeypatch.setattr(market, "verify_checkout", lambda _: pytest.fail("No checkout before frozen binding."))
    monkeypatch.setattr(market, "GitHubAPI", lambda _: native)
    if command == "runtime":
        with pytest.raises(RecordError, match="DAILY_EXECUTION_ENVIRONMENT"):
            market.run_runtime(event, env, tmp_path / "daily", api=native,
                execute=lambda **_: pytest.fail("No consumer without preflight binding."))
    else:
        monkeypatch.delenv(execution.ENVIRONMENT_KEY, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert market.main(["release"]) == 2
        assert "DAILY_EXECUTION_ENVIRONMENT" in capsys.readouterr().err
    assert native.calls == [] and not (tmp_path / "daily").exists()


def test_publication_revoked_after_business_call_cannot_release_stdout(
        tmp_path, monkeypatch, capsys):
    native = Native(pinned=True)
    event, env = environment(native, tmp_path)
    frozen = native.freeze()
    monkeypatch.setattr(market, "verify_checkout", lambda selected: None)
    def execute(**options):
        grant = deepcopy(native.publications[native.main["object"]["sha"]])
        grant["enabled"] = False
        native.advance(publication=grant)
        return {"receipt": {"status": "read", "source_requests": 0}, "control": {}}
    with pytest.raises(RecordError, match="DAILY_EXECUTION_PUBLICATION_REVOKED"):
        market.run_runtime(event, env, tmp_path / "daily", api=native, execute=execute)
    assert capsys.readouterr().out == ""
    assert native.env[execution.ENVIRONMENT_KEY] == json.dumps({key: frozen[key]
        for key in ("workflow_sha", "code_sha", "runtime_version")}, separators=(",", ":"))
