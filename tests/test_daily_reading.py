"""Daily read-only host composition, release checks and truthful human summary."""
from copy import deepcopy
from pathlib import Path
import re
import shlex

import pytest
import yaml

from decision_kernel.application.brief_discovery import cutoff_age
from decision_kernel.domain.records import RecordError, digest, encoded
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


def frozen_identity():
    """Synthetic receipt evidence; real admission is exercised separately."""
    return {"operation": "update", "invocation": "external-clock", "event": "workflow_dispatch",
        "run_id": 8001, "run_attempt": 1, "workflow_sha": "a" * 40, "code_sha": "b" * 40,
        "runtime_version": {"schema": "daily-runtime-selection-v1", "mode": "pinned",
            "code_tree": "c" * 40, "policy_commit": "a" * 40, "policy_sha256": "d" * 64,
            "protocol": "daily-runtime-v1", "workflow_ci_run": 1, "execution_ci_run": 2,
            "approval_ref": "https://github.com/auguspp/Decision-OS-2/issues/221#issuecomment-1"}}


def discovery_receipt(selected, source):
    return {"schema": "daily-discovery-runtime-v1", **deepcopy(selected),
        "source_receipt_sha256": digest(encoded(source)), "status": "not-updated",
        "reason_code": "DAILY_DISCOVERY_UPDATE_NOT_QUALIFIED", "as_of": AT,
        "records_writes": 0, "reference": None, "persistence": "not-written", "source_requests": 0}


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


def test_reading_preserves_frozen_workflow_execution_and_approval_evidence(tmp_path, monkeypatch):
    selected = frozen_identity()
    source = receipt(selected)
    state = discovery_receipt(selected, source)
    (tmp_path / "result.json").write_bytes(encoded(source))
    (tmp_path / "discovery-result.json").write_bytes(encoded(state))
    restored = cli.read_update_receipt(tmp_path, selected)
    discovered = cli.read_discovery_receipt(tmp_path, selected, restored)
    readers, _ = fake_readers(monkeypatch)
    result = cli.build_reading(*readers, "registry", approved(), restored, as_of=AT,
        discovery_receipt=discovered)
    context = result["daily_update"]
    for key, value in selected.items():
        assert context["source_receipt"][key] == context["discovery_update"][key] == value
    assert context["source_receipt_sha256"] == context["discovery_update"]["source_receipt_sha256"] \
        == digest(encoded(source))
    context["source_receipt"]["runtime_version"]["mode"] = "current-main"
    assert source["runtime_version"]["mode"] == "pinned"


@pytest.mark.parametrize("stage", ["source", "discovery"])
@pytest.mark.parametrize("field,fault", [
    ("workflow_sha", "missing"), ("workflow_sha", "changed"),
    ("code_sha", "changed"), ("runtime_version", "missing"),
    ("runtime_version", "changed"), ("runtime_version", "nested-bool"),
])
def test_receipts_cannot_omit_or_relabel_pinned_execution_evidence(tmp_path, stage, field, fault):
    selected = frozen_identity()
    source = receipt(selected)
    changed = deepcopy(source) if stage == "source" else discovery_receipt(selected, source)
    if fault == "missing":
        changed.pop(field)
    elif fault == "nested-bool":
        # bool and int compare equal inside a dict but identify different JSON.
        changed[field]["workflow_ci_run"] = True
    elif field == "runtime_version":
        changed[field]["policy_sha256"] = "e" * 64
    else:
        changed[field] = "f" * 40
    name = "result.json" if stage == "source" else "discovery-result.json"
    (tmp_path / name).write_bytes(encoded(changed))
    with pytest.raises(RecordError, match="RUNTIME_MISMATCH"):
        if stage == "source":
            cli.read_update_receipt(tmp_path, selected)
        else:
            cli.read_discovery_receipt(tmp_path, selected, source)


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
    from decision_kernel.interfaces import daily_discovery_cli

    # These original consumer fixtures exercise native run/visibility checks.
    # Approved checkout admission has its own real-module fixtures below.
    for entry in (cli, daily_discovery_cli):
        monkeypatch.setattr(entry, "select_execution", lambda api, selected, **kwargs: selected)
        monkeypatch.setattr(entry, "verify_checkout", lambda selected: None)
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


def approved_native_run(tmp_path, monkeypatch, *, pinned=True):
    """Real native approval with a synthetic checkout and existing reader model."""
    from decision_kernel.interfaces import daily_discovery_cli
    from test_daily_execution import Native

    preflight = Native(pinned=pinned)
    selected = preflight.freeze()
    event = {"inputs": {"operation": "update", "code_sha": selected["workflow_sha"]}}
    env = {**preflight.env, "RUNNER_TEMP": str(tmp_path), "GITHUB_TOKEN": "synthetic-job-token"}
    out = tmp_path / "daily-market"
    out.mkdir()
    (out / "result.json").write_bytes(encoded(receipt(selected, status="source-held")))
    checkouts, guards = [], []
    readers, calls = fake_readers(monkeypatch, status="blocked")
    for entry in (cli, daily_discovery_cli):
        monkeypatch.setattr(entry, "verify_checkout", lambda selected: checkouts.append(deepcopy(selected)))
    def writer(current, *, before_write):
        guards.append(before_write)
        return readers
    monkeypatch.setattr(daily_discovery_cli, "_open_writer", writer)
    return event, env, out, Native(pinned=pinned), selected, calls, checkouts, guards


def test_native_pinned_state_and_reading_keep_one_frozen_proof_after_main_advances(tmp_path, monkeypatch):
    from decision_kernel.interfaces import daily_discovery_cli
    from test_daily_execution import Native

    event, env, out, native, selected, _, checkouts, guards = approved_native_run(tmp_path, monkeypatch)
    original = (out / "result.json").read_bytes()
    assert daily_discovery_cli.run_runtime(event, env, out, api=native, clock=lambda: AT) == 0
    discovery = cli._json((out / daily_discovery_cli.OUTPUT).read_bytes())
    assert discovery["status"] == "not-updated" and discovery["records_writes"] == 0
    later = Native(pinned=True)
    later.advance()
    assert cli.run_runtime(event, env, out, api=later, clock=lambda: AT) == 0
    saved = cli._json((out / "reading-room.json").read_bytes())["daily_update"]
    for key, value in selected.items():
        assert discovery[key] == saved["source_receipt"][key] == saved["discovery_update"][key] == value
    assert checkouts == [selected, selected]
    assert len(guards) == 1 and selected["code_sha"] != selected["workflow_sha"]
    assert discovery["source_receipt_sha256"] == saved["source_receipt_sha256"] == digest(original)
    assert (out / "result.json").read_bytes() == original
    for api in (native, later):
        ci_calls = [path for _, path in api.calls if "/actions/workflows/ci.yml/runs?" in path]
        assert len(ci_calls) == 2  # Each new entry checks each exact CI once.
    disabled = deepcopy(native.policies[native.workflow_sha])
    disabled["enabled"] = False
    native.advance(policy=disabled)
    with pytest.raises(RecordError, match="DAILY_EXECUTION_DISABLED"):
        guards[0]()  # The discovery writer retains this same live authority.


@pytest.mark.parametrize("consumer", ["reading", "discovery"])
def test_native_consumer_cannot_reselect_when_its_frozen_envelope_is_missing(tmp_path, monkeypatch, consumer):
    from decision_kernel.interfaces import daily_discovery_cli

    event, env, out, native, _, calls, checkouts, guards = approved_native_run(
        tmp_path, monkeypatch, pinned=False)
    env.pop("DECISION_OS_EXECUTION")
    entry = cli if consumer == "reading" else daily_discovery_cli
    with pytest.raises(RecordError, match="DAILY_EXECUTION_ENVIRONMENT"):
        entry.run_runtime(event, env, out, api=native, clock=lambda: AT)
    assert native.calls == [] and not calls and not checkouts and not guards
    assert not (out / daily_discovery_cli.OUTPUT).exists()
    assert not any((out / name).exists() for name in cli.OUTPUTS)


@pytest.mark.parametrize("change,reason", [
    ("policy", "DAILY_EXECUTION_DISABLED"),
    ("publication", "DAILY_EXECUTION_PUBLICATION_REVOKED"),
    ("native-run", "DAILY_MARKET_NATIVE_RUN"),
])
def test_native_pinned_reading_rechecks_live_authority_after_render_before_export(
        tmp_path, monkeypatch, change, reason):
    event, env, out, native, selected, calls, checkouts, _ = approved_native_run(tmp_path, monkeypatch)
    original = cli.daily_page
    def changed(model):
        page = original(model)
        if change == "policy":
            disabled = deepcopy(native.policies[native.workflow_sha])
            disabled["enabled"] = False
            native.advance(policy=disabled)
        elif change == "publication":
            disabled = deepcopy(native.publications[native.workflow_sha])
            disabled["enabled"] = False
            native.advance(publication=disabled)
        else:
            native.run["head_sha"] = "f" * 40
        return page
    monkeypatch.setattr(cli, "daily_page", changed)
    with pytest.raises(RecordError, match=reason):
        cli.run_runtime(event, env, out, api=native, clock=lambda: AT)
    assert calls and checkouts == [selected]
    assert not any((out / name).exists() for name in cli.OUTPUTS)


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


@pytest.mark.parametrize("consumer", ["reading", "discovery"])
def test_new_admission_cannot_upgrade_a_legacy_receipt_missing_its_proof(tmp_path, monkeypatch, consumer):
    from decision_kernel.interfaces import daily_discovery_cli

    event, env, out, api, _, reads = native_run(tmp_path, monkeypatch)
    entry = cli if consumer == "reading" else daily_discovery_cli
    original = (out / "result.json").read_bytes()
    admitted = {**frozen_identity(), **daily_market_cli.settings(event, env, approved())}
    observed = []
    def admit(current, selected, *, env, require_inherited):
        assert current is api and "runtime_version" not in selected
        assert require_inherited is True
        observed.append("admit")
        return deepcopy(admitted)
    def checkout(selected):
        assert selected == admitted
        observed.append("checkout")
    monkeypatch.setattr(entry, "select_execution", admit)
    monkeypatch.setattr(entry, "verify_checkout", checkout)
    with pytest.raises(RecordError, match="RUNTIME_MISMATCH"):
        entry.run_runtime(event, env, out, api=api, clock=lambda: AT)
    assert observed == ["admit", "checkout"] and not reads and not api.calls
    assert (out / "result.json").read_bytes() == original
    assert not (out / daily_discovery_cli.OUTPUT).exists()
    assert not any((out / name).exists() for name in cli.OUTPUTS)


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
        assert effective_env == {"GITHUB_TOKEN": "${{ github.token }}",
            "DECISION_OS_EXECUTION": "${{ needs.visibility.outputs.execution }}"}, name
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
