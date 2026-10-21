"""Source-free daily ReadingRoom step on the existing private GitHub host.

Only GITHUB_TOKEN is consumed. The market owner resolves its latest attempt;
the original reading_scope opens research and Brief. No records or providers
are written, and a failed update never becomes an older green daily receipt.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import os
from pathlib import Path
import re
import sys

from decision_kernel.application.daily_market import daily_market_reference, policy
from decision_kernel.application.daily_discovery import reading_discovery
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.runtime_performance import annotate_performance, diagnosed, measure_phase, timed
from decision_kernel.interfaces.daily_market_cli import POLICY_FILE, native_ready, settings
from decision_kernel.interfaces.ftshare_check_cli import _regular, _safe_error
from decision_kernel.interfaces.market_collection_cli import REGISTRY, _records_runtime_binding
from decision_kernel.interfaces.reading_commands import check_ai_brief_output, perform
from decision_kernel.interfaces.reading_page import render_page

OUTPUTS = ("reading-room.html", "reading-room.json", "reading-summary.md")
UPDATE_FIELDS = ("operation", "invocation", "admission", "status", "reason_code", "reading_reason_code", "frontier_reason_code", "cycle_date", "cycle_status",
    "source_requests", "calendar_source_requests", "price_source_requests", "expected_completed_session",
    "calendar_last_failure", "calendar_source_budget", "calendar_original_source_budget",
    "calendar_additional_source_budget", "calendar_reserved_requests", "calendar_repair",
    "write_reconciliation", "write_reconciliation_native_proof", "write_last_failure",
    "master_source_requests", "master_refresh", "master_reserved_queries", "master_query_budget",
    "master_page_budget", "master_source_budget", "master_execution",
    "calendar_checked_through", "started_at", "finished_at", "code_sha", "run_id", "run_attempt", "event")
STATES = {"read": "仅检查已存状态", "complete": "本次限定行情更新完成", "already-complete": "本轮已完成，未重复请求",
    "no-new-session": "没有新增交易日", "paused": "更新已暂停", "not-due": "尚未到约定更新时间",
    "blocked": "本轮更新受阻", "source-held": "来源已暂停，等待复核", "persistence-unconfirmed": "保存结果待对账",
    "reading-unavailable": "行情读回尚未完成", "started": "本轮尚未完成", "in-progress": "本轮正在进行",
    "partial": "本轮部分完成", "failed": "本轮更新失败", "exhausted": "本轮额度已耗尽",
    "calendar-pending": "交易日历更新尚未完成", "master-pending": "证券主表更新尚未完成",
    "market-pending": "价格更新尚未完成"}


def _error(exc, token=None):
    code = _safe_error(exc)
    return ("DAILY_READING_FAILED" if code == "FTSHARE_CHECK_FAILED" or token and token in code else code)


@timed("reading.model")
def build_reading(store, market_store, registry, approved, update_receipt, *, as_of, discovery_receipt=None):
    """Compose original readers; unresolved latest history is never a seed fallback."""
    context = {"schema": "daily-reading-context-v1", "read_as_of": as_of,
        "source_receipt": {key: deepcopy(update_receipt[key]) for key in UPDATE_FIELDS if key in update_receipt},
        "source_receipt_sha256": digest(encoded(update_receipt)), "market_resolution": "unavailable",
        "market_cycle": None, "market_cycle_reference": None, "market_error": None,
        "frontier_reason_code": None, "frontier_blocking_reference": None,
        "frontier_successor_references": [],
        "source_http_requests": 0, "records_write": "not-requested", "research_executed": False,
        "brief_created": False, "notification_sent": False,
        "discovery_update": deepcopy(discovery_receipt), "discovery": None}
    plan = {"as_of": as_of, "catalogue": {"limit": 4}, "brief": {"select": "latest-saved"},
            "ai_brief": {"select": "latest-ai-brief"}}
    reference = None
    try:
        resolved = daily_market_reference(market_store, registry, approved)
        plan["market"] = {"reference": deepcopy(resolved["reference"]), "offset": 0, "limit": 20, "entity_id": None}
        context.update(market_resolution="resolved", market_cycle=deepcopy(resolved["cycle"]),
                       market_cycle_reference=deepcopy(resolved["cycle_reference"]))
        for key in ("frontier_reason_code", "frontier_blocking_reference", "frontier_successor_references"):
            context[key] = deepcopy(resolved.get(key, context[key]))
        reference = resolved["reference"]
    except (RecordError, KeyError, TypeError, ValueError) as exc:
        context["market_error"] = _error(exc)
    try:
        discovery = reading_discovery(store, market_store, registry, reference, as_of=as_of)
        context["discovery"] = {key: deepcopy(value) for key, value in discovery.items() if key != "panel"}
        if discovery["panel"] is not None:
            plan["discovery"] = discovery["panel"]
    except (RecordError, KeyError, TypeError, ValueError) as exc:
        context["discovery"] = {"status": "unavailable", "persistence": "not-written",
            "reason_code": _error(exc), "source_requests": 0, "records_write": "not-requested"}
    # This is the same private read-only export path, including its expiring
    # scope and final access checks, not a second catalogue or data service.
    result = perform(store, {"plan": plan}, public=False, market_registry=registry)
    result["daily_update"] = context
    if context["market_resolution"] != "resolved" or context["frontier_reason_code"] is not None \
            or context["discovery"]["status"] == "unavailable" \
            or discovery_receipt is not None and discovery_receipt["status"] == "unavailable" \
            or update_receipt.get("status") not in {
            "read", "complete", "already-complete", "no-new-session", "paused", "not-due"}:
        result["status"] = "partial"
    require(len(encoded(result)) <= 6 * 1024 * 1024, "ROOM_VIEW_BOUND")
    return result


def _summary_lines(model):
    context = model["daily_update"]
    receipt = context["source_receipt"]
    status = receipt.get("status")
    lines = ["阅读时点：" + context["read_as_of"],
             "本次行情更新：" + STATES.get(status, "状态尚未确认") + "。"]
    admission = receipt.get("admission")
    if isinstance(admission, dict):
        day = admission.get("business_date")
        if isinstance(day, str) and re.fullmatch(r"[0-9]{8}", day):
            lines.append("本次计划更新日期：" + day[:4] + "-" + day[4:6] + "-" + day[6:] + "。")
    for key, title in (("reason_code", "更新原因"), ("reading_reason_code", "原行情读回限制")):
        if receipt.get(key):
            lines.append(title + "：" + str(receipt[key]))
    failure = receipt.get("calendar_last_failure")
    if isinstance(failure, dict):
        from decision_kernel.application.daily_calendar_repair import TRANSPORT_CLASSES
        category = failure.get("classification")
        if isinstance(category, str) and category in TRANSPORT_CLASSES | {"unclassified"}:
            lines.append("日历传输分类：" + category + "；本次没有自动重试。")
    if receipt.get("calendar_additional_source_budget") == 1:
        lines.append("日历恢复：原2次预算之外显式追加1次；原失败与已用请求仍保留。")
    reconciliation = receipt.get("write_reconciliation")
    if isinstance(reconciliation, dict) and reconciliation.get("schema") == "daily-write-reconciliation-v1" \
            and type(reconciliation.get("additional_source_requests")) is int \
            and reconciliation["additional_source_requests"] == 0:
        lines.append("写回执接续：已核原提交，仅续原预算内未用请求；额外来源预算为0，原失败保留。")
    write_failure = receipt.get("write_last_failure")
    phase_names = {"response": "返回结构", "commit": "提交关系", "binding": "保管绑定",
        "tree": "完整写入集合", "readback": "原始字节读回"}
    if isinstance(write_failure, dict) and write_failure.get("schema") == "github-write-diagnostic-v1":
        phase = write_failure.get("phase")
        if isinstance(phase, str) and phase in phase_names:
            lines.append("保存后核验未通过：" + phase_names[phase] + "；原写入结果仍须对账。")
    cycle = context["market_cycle"]
    if cycle is not None:
        lines.append("已存日更任务：" + str(cycle["date"]) + "，" + STATES.get(cycle["status"], cycle["status"]) + "。")
        if cycle.get("reason_code") and cycle["reason_code"] != receipt.get("reason_code"):
            lines.append("任务保留的限制：" + cycle["reason_code"])
        if cycle.get("schema") == "daily-market-cycle-v4":
            if cycle["status"] == "complete":
                lines.append("证券主表：本轮两所完整主表已被行情版本采用，资料取得时间按原页保留。")
            elif cycle["status"] == "master-pending":
                lines.append("证券主表尚未全部更新完成，本轮新价格尚未开始取得。")
    elif context["market_resolution"] == "resolved":
        lines.append("尚无日更记录。")
    else:
        lines.append("暂时无法读取日更状态。")
    if context["market_resolution"] != "resolved":
        lines.append("最新市场版本尚未确定：" + str(context["market_error"]) + "。")
    if context.get("frontier_reason_code"):
        lines.append("最新行情仍有未解决限制：" + context["frontier_reason_code"] + "。")
    part = model.get("market")
    view = ((part.get("view") or {}).get("view") or {}) if part and part.get("status") == "readable" else {}
    if view:
        dates = view.get("as_of_by_exchange")
        date = " / ".join(venue + " " + str(day or "未取得") for venue, day in dates.items()) if dates else view["raw"].get("as_of", "未取得")
        lines.append("市场数据实际截止：" + str(date) + "。")
        for name, title in (("raw", "原价"), ("qfq", "前复权")):
            values = view[name]
            observed, total = values.get("observed_count"), values.get("scope_count")
            lines.append(title + "完整窗口覆盖：" + str(observed if observed is not None else "未知") + " / "
                         + str(total if total is not None else "完整范围未知") + "。")
        fresh = view.get("freshness", {}).get("status")
        lines.append("市场当前时效：" + {"unknown": "未知", "stale": "尚未覆盖最近交易日",
            "current-through-completed-day": "已覆盖当前读取所需交易日"}.get(fresh, "按原版本日期阅读") + "。")
    elif part:
        lines.append("行情暂时无法读取，数据日期和覆盖情况未知。")
    discovery = context.get("discovery") or {}
    state_update = context.get("discovery_update") or {}
    if state_update.get("status") == "unavailable":
        lines.append("本次观察状态保存未完成：" + str(state_update.get("reason_code")) + "。")
    if discovery.get("status") == "saved":
        lines.append("观察状态：读取已存原始版本，计算时点 " + str(discovery["saved_as_of"]) + "。")
        if not discovery.get("current_market_confirmed", True):
            lines.append("这份观察状态尚未与当前行情版本核对。")
    elif discovery.get("status") == "preview":
        lines.append("观察状态：按当前已存行情计算的预览，尚未保存本次状态。")
    elif discovery.get("status") == "unavailable":
        lines.append("观察状态暂不可读：" + str(discovery.get("reason_code")) + "。")
    state_part = model.get("discovery") or {}
    if state_part.get("status") == "readable":
        surfaces = state_part["view"]["surfaces"]
        for kind, title in (("sector", "行业"), ("concept", "概念"), ("stock", "独立个股")):
            surface = surfaces[kind]
            if surface["status"] == "readable":
                lines.append(title + "观察实际截止：" + str(surface.get("as_of") or "未知")
                    + "，可判断 " + str(surface.get("qualified_count", 0)) + " / "
                    + str(surface.get("scope_count") if surface.get("scope_count") is not None else "范围未知") + "。")
            else:
                lines.append(title + "观察：" + ("尚无已存输入" if surface["status"] == "not-supplied" else "当前输入不可用") + "。")
    elif discovery.get("status") in {"preview", "saved"} and state_part:
        lines.append("本次观察状态未能通过原始证据读取，不能据此确认持续、减弱或退出。")
    brief = model["brief"]
    if brief["status"] == "readable":
        lines.append("已存简报内容截止：" + brief["view"]["plan"]["cutoff"] + "。")
        age = brief.get("age") or {}
        if age.get("status") == "older-than-24h":
            lines.append("这份简报的内容截止距本次阅读已超过24小时。")
        elif age.get("status") == "read-clock-before-cutoff":
            lines.append("阅读时钟早于简报截止，不能确认其更新时效。")
        lines.append("最新已存版本不等于今天的新简报。")
    else:
        lines.append("简报：" + {"no-saved-brief": "正常发行目录尚无已存版本", "not-supplied": "未提供",
            "missing": "缺失", "invalid": "校验未通过", "unavailable": "暂不可用", "partial": "最新发现尚不完整",
            "not-included": "超过当前读取容量", "permission-required": "反馈导出条件未通过",
            "sources-permission-required": "来源导出条件未通过"}.get(brief["status"], "当前未能读回") + "。")
    ai_brief = model.get("ai_brief")
    if ai_brief is not None:
        if ai_brief["status"] == "readable":
            doc = ai_brief["view"]["document"]
            lines.append("已存 AI 晚报：" + doc["execution_date"] + "；原内容截止 " + doc["cutoff"] + "。")
        else:
            lines.append("AI 晚报：" + {"no-saved-handoff": "最近七天尚无已存对象，任务执行状态未知",
                "partial": "发现尚不完整", "missing": "确切对象缺失", "invalid": "原对象校验未通过",
                "not-included": "显示容量不足，请用确切引用读取"}.get(ai_brief["status"], "当前未能读回") + "。")
    catalogue = model["catalogue"]
    lines.append("研究目录：" + str(catalogue["total_cases"]) + " 个已存案例，本页 " + str(catalogue["returned"]) + " 个；原文及待复核问题在阅读页。")
    return lines


def _markdown_text(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escape(str(value), quote=False))


def summary_markdown(model):
    lines = _summary_lines(model)
    result = "## Decision OS 日常阅读\n\n" + "\n\n".join(_markdown_text(line) for line in lines)
    result += "\n\n在本次运行附件中打开 **reading-room.html** 阅读全文。\n"
    require(len(result.encode()) <= 32 * 1024, "DAILY_READING_SUMMARY_BOUND")
    return result


def daily_page(model):
    # This host paragraph is derived from the same model as the JSON/summary.
    # All values are escaped; the original room renderer and CSP stay intact.
    lines = _summary_lines(model)
    primary = [line for line in lines if line.startswith(("本次行情更新", "更新原因", "原行情读回限制",
        "最新市场版本尚未确定", "最新行情仍有未解决限制"))]
    panel = '<section class="panel" aria-label="本次更新情况"><h2>本次更新情况</h2>'
    panel += "".join("<p>" + escape(line) + "</p>" for line in primary)
    panel += '<details><summary>数据日期与内容覆盖</summary>'
    panel += "".join("<p>" + escape(line) + "</p>" for line in lines if line not in primary)
    panel += "</details></section>"
    page = render_page(model)
    require(page.count("<main>") == 1, "DAILY_READING_PAGE_LAYOUT")
    page = page.replace("<main>", "<main>" + panel, 1)
    require(len(page.encode()) <= 8 * 1024 * 1024, "ROOM_HTML_BOUND")
    return page


def _open_readers(api):
    binding = _records_runtime_binding("status")
    require(binding.write_enabled is False, "DAILY_READING_READ_ONLY")
    return VersionedStore(GitHubStore(binding, api)), VersionedStore(GitMarketReader(binding, api))


def read_update_receipt(out, selected):
    """The source writer's canonical native-identity receipt, reused by both consumers."""
    raw = _regular(out / "result.json", 1024 * 1024)
    receipt = _json(raw)
    require(isinstance(receipt, dict) and receipt.get("schema") == "daily-market-runtime-v1"
            and encoded(receipt) == raw and all(type(receipt.get(key)) is type(value)
                and receipt[key] == value for key, value in selected.items()), "DAILY_READING_RUNTIME_MISMATCH")
    require(isinstance(receipt.get("status"), str) and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", receipt["status"]),
            "DAILY_READING_RUNTIME_STATUS")
    require(all(receipt.get(key) is None or isinstance(receipt[key], str)
                and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", receipt[key])
                for key in ("reason_code", "reading_reason_code")), "DAILY_READING_RUNTIME_REASON")
    return receipt


def read_discovery_receipt(out, selected, receipt):
    path = out / "discovery-result.json"
    if not path.exists() and not path.is_symlink():
        return None
    raw = _regular(path, 1024 * 1024)
    result = _json(raw)
    require(isinstance(result, dict) and encoded(result) == raw
        and result.get("schema") == "daily-discovery-runtime-v1"
        and result.get("source_receipt_sha256") == digest(encoded(receipt))
        and all(type(result.get(key)) is type(value) and result[key] == value for key, value in selected.items())
        and result.get("status") in {"stored-and-verified", "already-recorded", "not-updated", "unavailable"}
        and type(result.get("source_requests")) is int and result["source_requests"] == 0,
        "DAILY_DISCOVERY_RUNTIME_MISMATCH")
    return {key: deepcopy(result.get(key)) for key in (
        "status", "reason_code", "as_of", "records_writes", "reference", "persistence", "source_requests")}


@diagnosed
def run_runtime(event, env, out, *, api=None, clock=None):
    with measure_phase("reading.execute"):
        approved = policy(_json(_regular(POLICY_FILE, 8192)))
        selected = settings(event, env, approved)
        token = env.get("GITHUB_TOKEN")
        require(isinstance(token, str) and bool(token), "DAILY_READING_TOKEN_REQUIRED")
        root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
        require(out.is_dir() and not out.is_symlink() and out.resolve().is_relative_to(root)
                and out.resolve() != root, "DAILY_READING_OUTPUT")
        require(all(not (out / name).exists() and not (out / name).is_symlink() for name in OUTPUTS),
                "DAILY_READING_OUTPUT_EXISTS")
        receipt = read_update_receipt(out, selected)
        discovery_receipt = read_discovery_receipt(out, selected, receipt)
        api = GitHubAPI(token) if api is None else api
        native_ready(api, selected)
        store, market_store = _open_readers(api)
        as_of = (clock or (lambda: datetime.now(timezone.utc).isoformat()))()
        model = build_reading(store, market_store, RelayRegistry(REGISTRY), approved, receipt, as_of=as_of,
                              discovery_receipt=discovery_receipt)
    # Freeze one model before all three exports. Render and the final native
    # eligibility check are deliberately outside this model's timing boundary.
    observed_model = annotate_performance(model, max_bytes=6 * 1024 * 1024)
    try:
        page = daily_page(observed_model)
    except RecordError as exc:
        if str(exc) != "ROOM_HTML_BOUND" or observed_model is model:
            raise
        # Optional diagnostics must not invalidate an otherwise valid escaped
        # HTML projection. One capacity-only fallback renders the original model;
        # it does not repeat reads or attempt to measure its own render duration.
        page = daily_page(model)
    else:
        model = observed_model
    exports = (page.encode(), encoded(model), summary_markdown(model).encode())
    # Shared main/CI/private host eligibility is checked again immediately
    # before material leaves the reader. The workflow still owns release.
    native_ready(api, selected)
    check_ai_brief_output(store, model)
    for name, data in zip(OUTPUTS, exports, strict=True):
        with (out / name).open("xb") as stream:
            stream.write(data)
    print("Daily reading ready; source HTTP requests: 0; records writes: 0.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return run_runtime(event, os.environ, args.out)
    except Exception as exc:
        print(_error(exc, os.environ.get("GITHUB_TOKEN")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
