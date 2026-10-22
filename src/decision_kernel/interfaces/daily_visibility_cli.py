"""Metadata-only admission for the existing daily host, before source secrets.

This probe does not open a Store or authenticate source clients. Public status
is not a market read, and a public update must not become a skipped green run.
The existing private jobs still perform their own native and publication checks.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import sys

from decision_kernel.application.daily_clock import (
    CLOCK_PATH, CLOCK_PROBE, EXTERNAL_CLOCK, admit_clock, clock_policy,
    dispatch_selection, run_title,
)
from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.github_store import GitHubAPI
from decision_kernel.infrastructure.runtime_publication import native_publication_basis

REPOSITORY = "auguspp/Decision-OS-2"
REPOSITORY_ID = 1420041744
WORKFLOW = ".github/workflows/daily-market.yml"
WORKFLOW_NAME = "decision-os-daily-market"
PREFIX = "/repos/" + REPOSITORY
TOKEN_ENV = "DECISION_OS_STORE_TOKEN"
MAX_EVENT_BYTES = 65536
CLOCK_POLICY_FILE = Path(CLOCK_PATH)
SCHEMA = "daily-runtime-visibility-v1"
PUBLIC_REASON = "PUBLIC_DAILY_RUNTIME_NOT_AUTHORIZED"
ERROR_CODES = frozenset({
    "DAILY_VISIBILITY_CONTEXT", "DAILY_VISIBILITY_EVENT", "DAILY_VISIBILITY_INPUT",
    "DAILY_VISIBILITY_CODE_SHA", "DAILY_VISIBILITY_RUN_ID", "DAILY_VISIBILITY_TOKEN",
    "DAILY_VISIBILITY_REPOSITORY", "DAILY_VISIBILITY_NATIVE_RUN",
    "DAILY_VISIBILITY_CHANGED", "DAILY_VISIBILITY_OUTPUT", "DAILY_VISIBILITY_FAILED",
    "DAILY_CLOCK_POLICY", "DAILY_CLOCK_NOT_PRIMARY", "DAILY_CLOCK_INVOCATION",
    "DAILY_CLOCK_METADATA_MODE", "DAILY_MARKET_NATIVE_RUN", "DAILY_MARKET_MAIN_CI",
    "FTSHARE_MAIN_MOVED",
})


def selection(event, env, *, clock=None):
    """Validate trusted workflow coordinates; event privacy is not authoritative."""
    require(isinstance(env, Mapping)
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"},
        "DAILY_VISIBILITY_CONTEXT")
    require(isinstance(event, dict), "DAILY_VISIBILITY_EVENT")
    sha = env.get("GITHUB_SHA")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha), "DAILY_VISIBILITY_CODE_SHA")
    run_id = env.get("GITHUB_RUN_ID")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id),
        "DAILY_VISIBILITY_RUN_ID")
    if env["GITHUB_EVENT_NAME"] == "schedule":
        # Schedule events may have no repository object. The private daily host
        # owns the approved cron, due slot, calendar, holds and source budgets.
        require(isinstance(event.get("schedule"), str) and 0 < len(event["schedule"]) <= 128
            and not event.get("inputs"), "DAILY_VISIBILITY_EVENT")
        operation, invocation = "update", "schedule"
        admit_clock(invocation, clock)
    else:
        selected = dispatch_selection(event.get("inputs"), sha, clock,
            input_error="DAILY_VISIBILITY_INPUT", sha_error="DAILY_VISIBILITY_CODE_SHA")
        operation, invocation = selected["operation"], selected["invocation"]
    return {"operation": operation, "invocation": invocation, "event": env["GITHUB_EVENT_NAME"],
        "code_sha": sha, "run_id": int(run_id), "run_attempt": 1}


def _repository(api):
    meta = api.request("GET", PREFIX)
    require(isinstance(meta, dict) and type(meta.get("id")) is int
        and meta["id"] == REPOSITORY_ID and meta.get("full_name") == REPOSITORY
        and type(meta.get("private")) is bool and meta.get("archived") is False
        and meta.get("default_branch") == "main", "DAILY_VISIBILITY_REPOSITORY")
    visibility = "private" if meta["private"] else "public"
    require("visibility" not in meta or meta["visibility"] == visibility, "DAILY_VISIBILITY_REPOSITORY")
    return visibility


def preflight(event, env, api, *, use_authorization=False, clock=None):
    """Three metadata GETs, seven for an admitted clock; no records or source I/O.

    Automatic producers must still be the current main with applicable native
    CI before their job receives source secrets. The runtime checks again.
    """
    selected = selection(event, env, clock=clock)
    visibility = _repository(api)
    run = api.request("GET", PREFIX + "/actions/runs/" + str(selected["run_id"]))
    require(isinstance(run, dict) and type(run.get("id")) is int and run["id"] == selected["run_id"]
        and run.get("head_sha") == selected["code_sha"] and run.get("head_branch") == "main"
        and run.get("event") == selected["event"] and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1 and run.get("path") == WORKFLOW
        and run.get("name") == run_title(selected["invocation"]) and run.get("status") == "in_progress"
        and (selected["invocation"] not in {EXTERNAL_CLOCK, CLOCK_PROBE}
             or run.get("display_title") == run_title(selected["invocation"]))
        and all(isinstance(run.get(key), dict) and type(run[key].get("id")) is int
            and run[key]["id"] == REPOSITORY_ID for key in ("repository", "head_repository")),
        "DAILY_VISIBILITY_NATIVE_RUN")
    public = visibility == "public"
    require(type(use_authorization) is bool, "DAILY_VISIBILITY_INPUT")
    probe = selected["invocation"] == CLOCK_PROBE
    basis = native_publication_basis(selected, env=env) if public and use_authorization and not probe else None
    allowed = not public or basis is not None
    proof = None
    if allowed and selected["invocation"] in {"schedule", EXTERNAL_CLOCK}:
        from decision_kernel.interfaces.daily_market_cli import native_ready
        proof = native_ready(api, selected, metadata_only=True)
    require(_repository(api) == visibility, "DAILY_VISIBILITY_CHANGED")
    if selected["invocation"] == "repair-calendar" and allowed:
        from decision_kernel.application.daily_calendar_repair import load_grant
        load_grant()
    if selected["invocation"] == "reconcile-write" and allowed:
        from decision_kernel.application.daily_write_reconciliation import load_grant
        load_grant()
    blocked = public and not allowed and selected["operation"] != "status" and not probe
    report = {"schema": SCHEMA, "scope": "native-metadata-only", **selected,
        "repository_visibility": visibility,
        "status": "blocked" if blocked else "metadata-only" if public else "private-runtime-pending",
        "reason_code": PUBLIC_REASON if public else None,
        "source_requests": 0, "records_read": False, "records_written": False,
        "data_freshness": "not-checked", "historical_exposure_remediated": False}
    if use_authorization:
        report.update(runtime_authorized=allowed and not probe, admitted=allowed and not probe,
                      publication_authority=basis)
        if public and allowed and not probe:
            report.update(status="public-runtime-pending", reason_code=None)
    if proof is not None:
        report["native_proof"] = proof
    if probe:
        report.update(status="clock-probe-metadata-only", reason_code=None,
            clock_primary=admit_clock(CLOCK_PROBE, clock), external_origin_verified=False)
    return report, 2 if blocked else 0


def summary(report):
    """Render only the fixed, validated diagnostic fields, never provider output."""
    if report["status"] == "failed":
        return "## 日常运行准入检查失败\n\n未取得可用准入回执；未读取资料或调用来源。\n\n原因：`" + report["reason_code"] + "`\n"
    if report.get("invocation") == CLOCK_PROBE:
        note = "本次仅为时钟连接检查；后续只核当前主线与原生 CI，不进入行情、资料、研究或 Brief 读取与写入。外部调度来源尚未证明。"
    elif report["repository_visibility"] == "private":
        note = "已核仓库为 Private；后续作业仍须通过原有权限、代码、来源及输出检查。此回执不证明数据已更新。"
    elif report.get("runtime_authorized") is True:
        note = "已核仓库为 Public，并采用已登记的用户公开授权；后续正常行情、研究与 Brief 作业获准接续。此预检不代表数据已更新。"
    elif report["status"] == "blocked":
        note = "仓库为 Public；本次更新未执行，未注入来源密钥。仓库公开不授予第三方数据的公开再分发权。"
    else:
        note = "仓库为 Public；本次 status 仅完成元数据诊断，没有读取行情、研究或 Brief，也没有生成阅读页。"
    return ("## 日常运行状态（仅元数据）\n\n" + note + "\n\n"
        + "操作：`" + report.get("invocation", report["operation"]) + "`；状态：`" + report["status"] + "`。\n\n"
        + "来源请求：0；records 读取/写入：未执行；资料新鲜度：未检查。\n\n"
        + "已有明确公开权利的手动入口仍按原合同使用。此检查未隐藏或清理历史 records、日志和附件。\n")


def _event(path, *, max_bytes=MAX_EVENT_BYTES):
    require(isinstance(path, str) and bool(path), "DAILY_VISIBILITY_EVENT")
    target = Path(path)
    require(target.is_file() and not target.is_symlink(), "DAILY_VISIBILITY_EVENT")
    with target.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    require(len(raw) <= max_bytes, "DAILY_VISIBILITY_EVENT")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "DAILY_VISIBILITY_EVENT")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def load_clock_policy():
    """Load the reviewed checkout file; missing or malformed config never defaults."""
    return clock_policy(_event(str(CLOCK_POLICY_FILE), max_bytes=8192))


def _append(env, key, text):
    path = env.get(key)
    if path is not None:
        require(isinstance(path, str) and bool(path), "DAILY_VISIBILITY_OUTPUT")
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(text)


def main(argv=None, *, env=None, api_factory=GitHubAPI):
    env = os.environ if env is None else env
    try:
        require(isinstance(env, Mapping), "DAILY_VISIBILITY_CONTEXT")
        args = sys.argv[1:] if argv is None else argv
        require(args in ([], ["--use-runtime-authorization"]), "DAILY_VISIBILITY_INPUT")
        use_authorization = bool(args)
        event = _event(env.get("GITHUB_EVENT_PATH"))
        clock = load_clock_policy()
        selection(event, env, clock=clock)  # Reject malformed contexts before creating a client.
        token = env.get(TOKEN_ENV)
        require(isinstance(token, str) and bool(token), "DAILY_VISIBILITY_TOKEN")
        report, exit_code = preflight(event, env, api_factory(token),
            use_authorization=use_authorization, clock=clock)
        _append(env, "GITHUB_STEP_SUMMARY", summary(report))
        # Emit the two-value enum only after both live visibility reads agree.
        # Routing is necessary, not sufficient: the existing jobs recheck again.
        output = "visibility=" + report["repository_visibility"] + "\n"
        if use_authorization:
            output += "runtime_authorized=" + ("true" if report["runtime_authorized"] else "false") + "\n"
            output += "admitted=" + ("true" if report["admitted"] else "false") + "\n"
        _append(env, "GITHUB_OUTPUT", output)
    except Exception as exc:
        code = str(exc) if isinstance(exc, RecordError) and str(exc) in ERROR_CODES else "DAILY_VISIBILITY_FAILED"
        token = env.get(TOKEN_ENV) if isinstance(env, Mapping) else None
        if isinstance(token, str) and token and token in code:
            code = "DAILY_VISIBILITY_FAILED"
        report = {"schema": SCHEMA, "scope": "native-metadata-only", "status": "failed",
            "reason_code": code, "source_requests": 0, "records_read": False, "records_written": False}
        # Even metadata failures must not print request URLs, event payloads,
        # arbitrary exception bodies, tokens, or a traceback into public logs.
        try:
            _append(env, "GITHUB_STEP_SUMMARY", summary(report))
        except Exception:
            pass
        exit_code = 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
