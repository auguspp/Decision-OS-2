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
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces.daily_market_cli import POLICY_FILE, native_ready, settings
from decision_kernel.interfaces.ftshare_check_cli import _regular, _safe_error
from decision_kernel.interfaces.market_collection_cli import REGISTRY, _records_runtime_binding
from decision_kernel.interfaces.reading_commands import perform
from decision_kernel.interfaces.reading_page import render_page

OUTPUTS = ("reading-room.html", "reading-room.json", "reading-summary.md")
UPDATE_FIELDS = ("operation", "status", "reason_code", "reading_reason_code", "cycle_date", "cycle_status",
    "source_requests", "calendar_source_requests", "price_source_requests", "expected_completed_session",
    "calendar_checked_through", "started_at", "finished_at", "code_sha", "run_id", "run_attempt", "event")
STATES = {"read": "仅检查已存状态", "complete": "本次限定行情更新完成", "already-complete": "本轮已完成，未重复请求",
    "no-new-session": "没有新增交易日", "paused": "更新已暂停", "not-due": "尚未到约定更新时间",
    "blocked": "本轮更新受阻", "source-held": "来源已暂停，等待复核", "persistence-unconfirmed": "保存结果待对账",
    "reading-unavailable": "行情读回尚未完成", "started": "本轮尚未完成", "in-progress": "本轮正在进行",
    "partial": "本轮部分完成", "failed": "本轮更新失败", "exhausted": "本轮额度已耗尽"}


def _error(exc, token=None):
    code = _safe_error(exc)
    return ("DAILY_READING_FAILED" if code == "FTSHARE_CHECK_FAILED" or token and token in code else code)


def build_reading(store, market_store, registry, approved, update_receipt, *, as_of):
    """Compose original readers; unresolved latest history is never a seed fallback."""
    context = {"schema": "daily-reading-context-v1", "read_as_of": as_of,
        "source_receipt": {key: deepcopy(update_receipt[key]) for key in UPDATE_FIELDS if key in update_receipt},
        "source_receipt_sha256": digest(encoded(update_receipt)), "market_resolution": "unavailable",
        "market_cycle": None, "market_cycle_reference": None, "market_error": None,
        "source_http_requests": 0, "records_write": "not-requested", "research_executed": False,
        "brief_created": False, "notification_sent": False}
    plan = {"as_of": as_of, "catalogue": {"limit": 4}, "brief": {"select": "latest-saved"}}
    try:
        resolved = daily_market_reference(market_store, registry, approved)
        plan["market"] = {"reference": deepcopy(resolved["reference"]), "offset": 0, "limit": 20, "entity_id": None}
        context.update(market_resolution="resolved", market_cycle=deepcopy(resolved["cycle"]),
                       market_cycle_reference=deepcopy(resolved["cycle_reference"]))
    except (RecordError, KeyError, TypeError, ValueError) as exc:
        context["market_error"] = _error(exc)
    # This is the same private read-only export path, including its expiring
    # scope and final access checks, not a second catalogue or data service.
    result = perform(store, {"plan": plan}, public=False, market_registry=registry)
    result["daily_update"] = context
    if context["market_resolution"] != "resolved" or update_receipt.get("status") not in {
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
    for key, title in (("reason_code", "更新原因"), ("reading_reason_code", "原行情读回限制")):
        if receipt.get(key):
            lines.append(title + "：" + str(receipt[key]))
    cycle = context["market_cycle"]
    if cycle is not None:
        lines.append("已存日更任务：" + str(cycle["date"]) + "，" + STATES.get(cycle["status"], cycle["status"]) + "。")
        if cycle.get("reason_code") and cycle["reason_code"] != receipt.get("reason_code"):
            lines.append("任务保留的限制：" + cycle["reason_code"])
    elif context["market_resolution"] == "resolved":
        lines.append("尚无可读的已存日更任务；若市场根可读，仍按其原日期与覆盖解释。")
    else:
        lines.append("日更任务状态未能核定，不能推断没有历史任务。")
    if context["market_resolution"] != "resolved":
        lines.append("最新市场版本尚未确定：" + str(context["market_error"]) + "；未用旧版本替代。")
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
                         + str(total if total is not None else "完整范围未知") + "；未将日常跟踪的有限证券数当全市场分母。")
        fresh = view.get("freshness", {}).get("status")
        lines.append("市场当前时效：" + {"unknown": "未知", "stale": "尚未覆盖最近交易日",
            "current-through-completed-day": "已覆盖当前读取所需交易日"}.get(fresh, "按原版本日期阅读") + "。")
    elif part:
        lines.append("市场面板未成功读回，实际日期与覆盖均不能由本次运行成功推断。")
    brief = model["brief"]
    if brief["status"] == "readable":
        lines.append("已存简报内容截止：" + brief["view"]["plan"]["cutoff"] + "。")
        age = brief.get("age") or {}
        if age.get("status") == "older-than-24h":
            lines.append("这份简报的内容截止距本次阅读已超过24小时；本轮没有生成新的简报。")
        elif age.get("status") == "read-clock-before-cutoff":
            lines.append("阅读时钟早于简报截止，不能确认其更新时效。")
        lines.append("最新已存版本不等于今天的新简报；没有因此启动研究或产生新的概率判断。")
    else:
        lines.append("简报：" + {"no-saved-brief": "正常发行目录尚无已存版本", "not-supplied": "未提供",
            "missing": "缺失", "invalid": "校验未通过", "unavailable": "暂不可用", "partial": "最新发现尚不完整",
            "not-included": "超过当前读取容量", "permission-required": "反馈导出条件未通过",
            "sources-permission-required": "来源导出条件未通过"}.get(brief["status"], "当前未能读回") + "。")
    catalogue = model["catalogue"]
    lines.append("研究目录：" + str(catalogue["total_cases"]) + " 个已存案例，本页 " + str(catalogue["returned"]) + " 个；原文及待复核问题在阅读页。")
    lines.append("本阅读步骤未请求来源网站、写入records、生成研究或发送通知；行情更新的实际请求和失败仍保留在原运行回执。")
    return lines


def _markdown_text(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escape(str(value), quote=False))


def summary_markdown(model):
    lines = _summary_lines(model)
    result = "## Decision OS 日常阅读\n\n" + "\n\n".join(_markdown_text(line) for line in lines)
    result += "\n\n在本次私有运行附件中打开 **reading-room.html** 阅读；**reading-room.json** 保留对应精确引用与状态。\n"
    require(len(result.encode()) <= 32 * 1024, "DAILY_READING_SUMMARY_BOUND")
    return result


def daily_page(model):
    # This host paragraph is derived from the same model as the JSON/summary.
    # All values are escaped; the original room renderer and CSP stay intact.
    lines = _summary_lines(model)
    primary = [line for line in lines if line.startswith(("本次行情更新", "更新原因", "原行情读回限制", "最新市场版本尚未确定"))]
    panel = '<section class="panel" aria-label="本次更新情况"><h2>本次更新情况</h2>'
    panel += "".join("<p>" + escape(line) + "</p>" for line in primary)
    panel += '<details><summary>数据日期、覆盖与本次运行范围</summary>'
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


def run_runtime(event, env, out, *, api=None, clock=None):
    approved = policy(_json(_regular(POLICY_FILE, 8192)))
    selected = settings(event, env, approved)
    token = env.get("GITHUB_TOKEN")
    require(isinstance(token, str) and bool(token), "DAILY_READING_TOKEN_REQUIRED")
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(out.is_dir() and not out.is_symlink() and out.resolve().is_relative_to(root)
            and out.resolve() != root, "DAILY_READING_OUTPUT")
    require(all(not (out / name).exists() and not (out / name).is_symlink() for name in OUTPUTS),
            "DAILY_READING_OUTPUT_EXISTS")
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
    api = GitHubAPI(token) if api is None else api
    native_ready(api, selected)
    store, market_store = _open_readers(api)
    as_of = (clock or (lambda: datetime.now(timezone.utc).isoformat()))()
    model = build_reading(store, market_store, RelayRegistry(REGISTRY), approved, receipt, as_of=as_of)
    exports = (daily_page(model).encode(), encoded(model), summary_markdown(model).encode())
    # Shared main/CI/private host eligibility is checked again immediately
    # before material leaves the reader. The workflow still owns release.
    native_ready(api, selected)
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
