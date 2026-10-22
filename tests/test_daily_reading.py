"""Daily read-only host composition, release checks and truthful human summary."""
from copy import deepcopy
from pathlib import Path
import re
import shlex

import pytest
import yaml

from decision_kernel.application.brief_discovery import cutoff_age
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.interfaces import daily_market_cli, daily_reading_cli as cli
from test_daily_market import native_context
from test_ftshare_collection_runtime import NativeAPI
from test_reading_room import Model

AT = "2026-10-19T12:10:00Z"


def model():
    m = Model()
    result = m.build({"as_of": AT})
    result["brief"] = {"status": "readable", "reference": None, "view": deepcopy(m.brief),
        "age": cutoff_age(m.brief["plan"]["cutoff"], AT)}
    result["market"] = {"status": "readable", "view": {"view": {
        "as_of_by_exchange": {"SSE": "2026-10-16", "SZSE": "2026-10-16"},
        "raw": {"scope_count": 5226, "observed_count": 6},
        "qfq": {"scope_count": 5226, "observed_count": 6}, "freshness": {"status": "unknown"}}}}
    return result


def receipt(selected=None, **changes):
    return {"schema": "daily-market-runtime-v1", "status": "read", "reason_code": None,
            "source_requests": 0, "calendar_source_requests": 0, "price_source_requests": 0,
            **(selected or {}), **changes}


def fake_readers(monkeypatch, *, status="read", resolution_failure=False):
    calls, readers = [], (object(), object())
    def resolve(market_store, registry, approved):
        assert market_store is readers[1]
        calls.append(("resolve", registry, deepcopy(approved)))
        if resolution_failure:
            raise RecordError("DAILY_MARKET_HISTORY_FORK")
        return {"reference": approved["seed_reference"], "cycle_reference": None,
                "cycle": None if status == "read" else {"date": "20261019", "status": "blocked",
                    "reason_code": "FTSHARE_HTTP_403"}}
    def read(store, payload, *, public, market_registry):
        assert store is readers[0] and public is False
        calls.append(("read", deepcopy(payload), market_registry))
        result = model()
        if "market" not in payload["plan"]:
            result.pop("market")
        return result
    monkeypatch.setattr(cli, "daily_market_reference", resolve)
    monkeypatch.setattr(cli, "reading_discovery", lambda *args, **kwargs: {
        "status": "saved", "persistence": "saved-exact-version", "saved_as_of": AT,
        "current_market_confirmed": True, "panel": None})
    monkeypatch.setattr(cli, "perform", read)
    monkeypatch.setattr(cli, "_open_readers", lambda api: readers)
    return readers, calls


def approved():
    return cli.policy(cli._json(cli.POLICY_FILE.read_bytes()))


def test_same_reading_room_receives_exact_market_and_latest_selector_without_write_permissions(monkeypatch):
    readers, calls = fake_readers(monkeypatch)
    original = receipt()
    result = cli.build_reading(*readers, "trusted-registry", approved(), original, as_of=AT)
    _, payload, registry = calls[1]
    assert payload["plan"]["brief"] == {"select": "latest-saved"}
    assert payload["plan"]["ai_brief"] == {"select": "latest-ai-brief"}
    assert payload["plan"]["market"]["reference"] == approved()["seed_reference"]
    assert payload["plan"]["market"]["entity_id"] is None and registry == "trusted-registry"
    assert result["daily_update"]["source_http_requests"] == 0
    assert result["daily_update"]["records_write"] == "not-requested" and original == receipt()


def test_failed_update_and_older_market_remain_visible_in_human_summary(monkeypatch):
    readers, _ = fake_readers(monkeypatch, status="blocked")
    result = cli.build_reading(*readers, "registry", approved(),
        receipt(status="source-held", reason_code="FTSHARE_HTTP_403", source_requests=1), as_of=AT)
    summary = cli.summary_markdown(result)
    assert result["status"] == "partial" and result["daily_update"]["source_receipt"]["source_requests"] == 1
    assert "来源已暂停" in summary and "2026\\-10\\-16" in summary
    assert summary.count("6 / 5226") == 2 and "超过24小时" in summary and "不等于今天的新简报" in summary
    assert "市场当前时效：未知" in summary and "**reading-room.html** 阅读全文" in summary
    assert all(text not in summary for text in ("分母", "records", "来源网站", "发送通知", "新的概率判断"))
    html = cli.daily_page(result)
    assert "本次更新情况" in html and "来源已暂停" in html and "FTSHARE_HTTP_403" in html
    assert '<details><summary>数据日期与内容覆盖</summary>' in html


def test_unresolved_market_history_does_not_fall_back_to_seed_or_claim_no_history(monkeypatch):
    readers, calls = fake_readers(monkeypatch, resolution_failure=True)
    result = cli.build_reading(*readers, "registry", approved(), receipt(), as_of=AT)
    assert "market" not in calls[1][1]["plan"] and "market" not in result
    assert result["status"] == "partial" and result["daily_update"]["market_error"] == "DAILY_MARKET_HISTORY_FORK"
    summary = cli.summary_markdown(result)
    assert "暂时无法读取日更状态" in summary and "最新市场版本尚未确定" in summary
    assert "0 / 5226" not in summary and "尚无日更记录" not in summary


def test_no_saved_brief_or_research_is_not_filled_with_generated_content(monkeypatch):
    readers, _ = fake_readers(monkeypatch)
    result = cli.build_reading(*readers, "registry", approved(), receipt(), as_of=AT)
    result["brief"] = {"status": "no-saved-brief", "reference": None}
    result["catalogue"].update(total_cases=0, returned=0)
    summary = cli.summary_markdown(result)
    assert "正常发行目录尚无已存版本" in summary and "0 个已存案例" in summary
    assert "已存简报内容截止" not in summary and result["daily_update"]["brief_created"] is False


def test_host_paragraph_and_job_summary_escape_nonexecuting_input(monkeypatch):
    readers, _ = fake_readers(monkeypatch)
    result = cli.build_reading(*readers, "registry", approved(), receipt(), as_of=AT)
    injection = '<script>alert("private")</script>[go](javascript:bad)'
    result["daily_update"]["source_receipt"]["reason_code"] = injection
    assert injection not in cli.daily_page(result)
    summary = cli.summary_markdown(result)
    assert "<script>" not in summary and "[go](javascript:bad)" not in summary


def native_run(tmp_path, monkeypatch, **changes):
    event, env = native_context()
    env.update(RUNNER_TEMP=str(tmp_path), GITHUB_TOKEN="synthetic-job-token")
    selected = daily_market_cli.settings(event, env, approved())
    out = tmp_path / "daily-market"
    out.mkdir()
    (out / "result.json").write_bytes(encoded(receipt(selected, **changes)))
    api = NativeAPI()
    api.run.update(path=daily_market_cli.WORKFLOW, name=daily_market_cli.TITLE, created_at=AT)
    api.ci.update(total_count=1)
    api.ci["workflow_runs"][0].update(name="decision-os-ci", run_attempt=1,
        repository={"id": 1420041744}, head_repository={"id": 1420041744})
    readers, calls = fake_readers(monkeypatch)
    return event, env, out, api, readers, calls


def test_native_step_uses_only_github_token_and_leaves_market_receipt_unchanged(tmp_path, monkeypatch):
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch)
    original = (out / "result.json").read_bytes()
    assert "FTSHARE_API_KEY" not in env and "TUSHARE_PROXY_API_KEY" not in env and "DECISION_OS_STORE_TOKEN" not in env
    assert cli.run_runtime(event, env, out, api=api, clock=lambda: AT) == 0
    assert all((out / name).is_file() for name in cli.OUTPUTS)
    assert (out / "result.json").read_bytes() == original
    assert api.calls.count(daily_market_cli.PREFIX) == 2  # Before reading and before export.
    saved = cli._json((out / "reading-room.json").read_bytes())
    assert saved["daily_update"]["records_write"] == "not-requested"
    assert (out / "reading-summary.md").read_text() == cli.summary_markdown(saved)


def test_output_is_withheld_when_native_privacy_changes_after_reading(tmp_path, monkeypatch):
    event, env, out, api, _, _ = native_run(tmp_path, monkeypatch)
    original = cli.perform
    def revoked(*args, **kwargs):
        result = original(*args, **kwargs)
        api.meta["private"] = False
        return result
    monkeypatch.setattr(cli, "perform", revoked)
    with pytest.raises(RecordError, match="PRIVATE_REPOSITORY"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: AT)
    assert not any((out / name).exists() for name in cli.OUTPUTS)


@pytest.mark.parametrize("change", [{"run_id": 9999}, {"run_attempt": True}, {"code_sha": "b" * 40},
                                    {"event": "schedule"}, {"operation": "status"}])
def test_copied_or_mismatched_source_receipt_is_rejected_before_reads(tmp_path, monkeypatch, change):
    event, env, out, api, _, calls = native_run(tmp_path, monkeypatch, **change)
    with pytest.raises(RecordError, match="RUNTIME_MISMATCH"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: AT)
    assert not calls and not api.calls and not any((out / name).exists() for name in cli.OUTPUTS)


def test_existing_or_symlink_output_is_not_overwritten(tmp_path, monkeypatch):
    event, env, out, api, _, calls = native_run(tmp_path, monkeypatch)
    existing = tmp_path / "retained.txt"
    existing.write_text("keep")
    (out / "reading-room.html").symlink_to(existing)
    with pytest.raises(RecordError, match="OUTPUT_EXISTS"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: AT)
    assert existing.read_text() == "keep" and not calls and not api.calls


def test_reading_step_does_not_use_a_different_store_or_source_token(tmp_path, monkeypatch):
    event, env, out, api, _, calls = native_run(tmp_path, monkeypatch)
    env.pop("GITHUB_TOKEN")
    env["DECISION_OS_STORE_TOKEN"] = "another-token"
    env["FTSHARE_API_KEY"] = "source-key"
    with pytest.raises(RecordError, match="READING_TOKEN_REQUIRED"):
        cli.run_runtime(event, env, out, api=api, clock=lambda: AT)
    assert not calls and not api.calls


def test_native_reader_binding_stays_read_only(monkeypatch):
    opened = []
    def backend(binding, api):
        opened.append(binding)
        assert binding.write_enabled is False
        return object()
    monkeypatch.setattr(cli, "GitHubStore", backend)
    monkeypatch.setattr(cli, "GitMarketReader", backend)
    cli._open_readers(object())
    assert len(opened) == 2 and opened[0] == opened[1]


def test_native_workflow_wires_reading_with_only_job_token_before_gated_release():
    workflow = yaml.load((Path(__file__).resolve().parents[1] / ".github/workflows/daily-market.yml").read_text(),
                         Loader=yaml.BaseLoader)
    daily_gate = workflow["jobs"]["daily"]["if"]
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert re.search(r"github\.event_name\s*==\s*(['\"])schedule\1", daily_gate)
    # Retain the historical schedule branch in the original job admission gate
    # without scheduling new native ticks; callers do not supply authoritative
    # repository visibility, which the native host still checks.
    assert "github.event.repository.private" not in daily_gate
    for name in ("status", "daily"):
        job = workflow["jobs"][name]
        steps = job["steps"]
        commands = [shlex.split(step.get("run", "")) for step in steps]
        readers = [index for index, command in enumerate(commands)
                   if command[:3] == ["python", "-m", cli.__name__]]
        releases = [index for index, command in enumerate(commands)
                    if command[:4] == ["python", "-m", daily_market_cli.__name__, "release"]]
        assert len(readers) == len(releases) == 1, name
        reading_index, release_index = readers[0], releases[0]
        reading, release = steps[reading_index], steps[release_index]
        # Include inherited workflow/job env: source keys must stay local to
        # the independent source step, not leak into this read-only command.
        effective_env = {**workflow.get("env", {}), **job.get("env", {}), **reading.get("env", {})}
        assert effective_env == {"GITHUB_TOKEN": "${{ github.token }}"}, name
        assert "secrets." not in reading["run"], name
        assert reading_index < release_index, name
        summaries = [index for index, step in enumerate(steps) if "GITHUB_STEP_SUMMARY" in step.get("run", "")]
        artifacts = [index for index, step in enumerate(steps)
                     if step.get("uses", "").startswith("actions/upload-artifact@")]
        assert summaries and artifacts, name
        required = re.compile(r"steps\." + re.escape(release["id"]) + r"\.outcome\s*==\s*(['\"])success\1")
        for index in summaries + artifacts:
            assert release_index < index, name
            gate = steps[index].get("if", "").strip()
            if gate.startswith("${{") and gate.endswith("}}"):
                gate = gate[3:-2].strip()
            # Require an AND guard; merely mentioning release success beside
            # an OR fallback would still allow private output after rejection.
            assert "||" not in gate and any(required.fullmatch(term.strip()) for term in gate.split("&&")), name
