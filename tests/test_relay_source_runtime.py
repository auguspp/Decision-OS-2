"""Protect opt-in secret scope and the installed source CLI host; never call Relay."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("relay_source_runtime_under_test", ROOT / "src/decision_kernel/interfaces/relay_source_runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
SHA = "a" * 40


def context(tmp_path, **inputs):
    return ({"inputs": {"operation": "plan", "code_sha": SHA, "codes": "000001.SZ,600000.SH",
                       "periods": "20251231 20260630", **inputs}},
            {"GITHUB_REPOSITORY": runtime.REPO, "GITHUB_REF": "refs/heads/main",
             "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_SHA": SHA, "RUNNER_TEMP": str(tmp_path)})


def test_plan_does_not_need_secret_and_uses_existing_cli(tmp_path):
    event, env = context(tmp_path)
    calls = []
    out = tmp_path / "source"
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs == {"check": False}
        return SimpleNamespace(returncode=0)
    assert runtime.execute("prepare", event, env, out, run=run) == 0
    assert len(calls) == 1 and calls[0][1:4] == ["-m", "decision_kernel.interfaces.relay_financial_cli", "plan"]
    assert "--live" not in calls[0] and "save" not in calls[0]
    result = json.loads((out / "result.json").read_text())
    assert result == {"status": "planned-not-captured", "code_sha": SHA, "records_written": False}


@pytest.mark.parametrize("change", [{"codes": "$(cat /secret)"}, {"codes": "--output"},
    {"periods": "20260230"}, {"periods": "20260629"}, {"apis": "income;curl"},
    {"page_size": "-1"}, {"max_pages": "0"}, {"code_sha": "b" * 40},
    {"operation": "save"}, {"operation": "capture"}, {"token": "do-not-accept"}])
def test_invalid_selection_rejected_before_subprocess(tmp_path, change):
    event, env = context(tmp_path, **change)
    with pytest.raises(ValueError):
        runtime.execute("prepare", event, env, tmp_path / "source", run=lambda *a, **k: pytest.fail("ran"))
    assert not (tmp_path / "source").exists()


@pytest.mark.parametrize("key,value", [("GITHUB_REPOSITORY", "other/repo"),
    ("GITHUB_REF", "refs/heads/unreviewed"), ("GITHUB_EVENT_NAME", "pull_request")])
def test_host_only_manual_original_main(tmp_path, key, value):
    event, env = context(tmp_path); env[key] = value
    with pytest.raises(ValueError, match="MANUAL_MAIN"):
        runtime.settings(event, env)


@pytest.mark.parametrize("returncode", [0, 2])
def test_capture_explicit_rights_same_plan_and_failure_status(tmp_path, returncode):
    event, env = context(tmp_path, operation="capture", public_rights_confirmed="true")
    out = tmp_path / "source"
    assert runtime.execute("prepare", event, env, out, run=lambda *a, **k: SimpleNamespace(returncode=0)) == 0
    calls = []
    def run(args, **kwargs):
        calls.append(args); return SimpleNamespace(returncode=returncode)
    assert runtime.execute("capture", event, env, out, run=run) == returncode
    assert calls[0][3] == "capture" and "--live" in calls[0]
    assert "--binding" not in calls[0] and "save" not in calls[0]
    assert json.loads((out / "result.json").read_text())["exit_code"] == returncode


def test_changed_plan_and_failed_prepare_do_not_capture(tmp_path):
    event, env = context(tmp_path, operation="capture", public_rights_confirmed=True)
    out = tmp_path / "source"
    assert runtime.execute("prepare", event, env, out, run=lambda *a, **k: SimpleNamespace(returncode=2)) == 2
    assert not (out / "request.json").exists()
    out = tmp_path / "next"
    runtime.execute("prepare", event, env, out, run=lambda *a, **k: SimpleNamespace(returncode=0))
    (out / "request.json").write_text("{}")
    with pytest.raises(ValueError, match="PLAN_CHANGED"):
        runtime.execute("capture", event, env, out, run=lambda *a, **k: pytest.fail("ran"))


def test_output_cannot_escape_owned_temp(tmp_path):
    event, env = context(tmp_path)
    with pytest.raises(ValueError, match="OUTPUT_INVALID"):
        runtime.execute("prepare", event, env, tmp_path.parent / "outside", run=lambda *a, **k: pytest.fail("ran"))


def test_workflow_has_scoped_secret_consumers_no_write_or_automatic_trigger():
    text = (ROOT / ".github/workflows/relay-source-runtime.yml").read_text()
    assert "on:\n  workflow_dispatch:" in text
    assert "pull_request:" not in text and "push:" not in text and "schedule:" not in text
    assert "contents: read" in text and "contents: write" not in text
    assert "persist-credentials: false" in text and "cancel-in-progress: false" in text
    # Keep the original source job's two consumers unchanged. The finite bulk
    # job is isolated and has its own native/one-shot guards and dedicated tests.
    text, bulk = text.split("\n  bulk-check:", 1)
    assert bulk.count("secrets.TUSHARE_PROXY_API_KEY") == 1
    assert bulk.count("DECISION_OS_STORE_TOKEN") == 2
    assert "inputs.family == 'market-bulk-check'" in bulk
    assert text.count("secrets.TUSHARE_PROXY_API_KEY") == 2
    assert "if: inputs.operation == 'capture'\n        env:\n          TUSHARE_PROXY_API_KEY:" in text
    check_step = text.split("      - name: Check retained PMI reference without exporting supplier rows")[1].split("      - name:")[0]
    assert "if: inputs.family == 'industry-pmi' && inputs.operation == 'check'" in check_step
    assert "DECISION_OS_STORE_TOKEN: ${{ github.token }}" in check_step
    assert text.count("DECISION_OS_STORE_TOKEN") == 1
    assert "inputs.codes }}" not in text and "inputs.periods }}" not in text
    assert "steps.prepare.outcome == 'success'" in text
    assert "retention-days: 7" in text


def test_vendor_manual_is_the_uploaded_original():
    from hashlib import sha256
    raw = (ROOT / "docs/sources/vendor/tushare-relay-v0.5.61.md").read_bytes()
    assert len(raw) == 44358
    assert sha256(raw).hexdigest() == "402bbfdfa77e6035b5cd41bc9a5d66b6ed975db2f2a25748ab37b797c12c827f"


@pytest.mark.parametrize("value", [1, [], {}])
def test_rights_not_coerced_from_non_boolean_data(tmp_path, value):
    event, env = context(tmp_path, operation="capture", public_rights_confirmed=value)
    with pytest.raises(ValueError, match="PUBLIC_RIGHTS"):
        runtime.settings(event, env)


def test_non_object_event_rejected(tmp_path):
    _, env = context(tmp_path)
    with pytest.raises(ValueError, match="EVENT_INVALID"):
        runtime.settings([], env)


def test_host_real_plan_via_existing_cli_without_source_secret(tmp_path, monkeypatch):
    import os
    import subprocess
    monkeypatch.delenv("TUSHARE_PROXY_API_KEY", raising=False)
    event, env = context(tmp_path)
    out = tmp_path / "actual-cli"
    def run(args, **kwargs):
        return subprocess.run(args, **kwargs, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                              cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert runtime.execute("prepare", event, env, out, run=run) == 0
    raw = (out / "plan.jsonl").read_text()
    # Existing CLI owns the serialization; it is JSONL through encoded().
    decoder, position, queries = json.JSONDecoder(), 0, []
    while position < len(raw):
        while position < len(raw) and raw[position].isspace():
            position += 1
        if position == len(raw):
            break
        query, position = decoder.raw_decode(raw, position)
        queries.append(query)
    assert len(queries) == 32
    assert json.loads((out / "result.json").read_text())["status"] == "planned-not-captured"
    assert not (out / "capture.jsonl").exists()
