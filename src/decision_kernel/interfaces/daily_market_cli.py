"""Native GitHub daily host for the existing six-stock FTShare collection."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from decision_kernel.application.daily_market import (
    SUCCESS, VENUES, _stamp, calendar_plan, completed_session, daily_market_reference,
    initial_cycle, latest_cycle, next_state, policy, price_configuration, retain_cycle,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection_runtime import health_receipt
from decision_kernel.application.market_custody import fresh_market_reader, retain_market_query
from decision_kernel.application.market_relay_inputs import qualified_trade_calendar
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_sources import capture_registered
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite, encoded, require
from decision_kernel.infrastructure.github_store import MAX_REQUESTS, GitHubAPI, GitHubRateLimits, GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.relay_http import RelayHTTP, SECRET_ENV
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy
from decision_kernel.interfaces.ftshare_check_cli import REPO, PREFIX, _regular, _safe_error, check_private
from decision_kernel.interfaces.ftshare_collection_cli import _source_instruction, run_collection
from decision_kernel.interfaces.market_collection_cli import BINDING_ENV, REGISTRY, _records_runtime_binding, _save_receipt

WORKFLOW = ".github/workflows/daily-market.yml"
TITLE = "decision-os-daily-market"
POLICY_FILE = Path("config/daily-market.json")
TOKEN_ENV = "DECISION_OS_STORE_TOKEN"
ADMISSION_REQUESTS = 240


def settings(event, env, approved):
    approved = policy(approved)
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") in {"schedule", "workflow_dispatch"}
        and env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1" and not env.get(BINDING_ENV),
        "DAILY_MARKET_NATIVE_MAIN_REQUIRED")
    require(isinstance(event, dict), "DAILY_MARKET_EVENT")
    event_name = env["GITHUB_EVENT_NAME"]
    if event_name == "schedule":
        require(event.get("schedule") == approved["schedule_utc"] and not event.get("inputs"),
            "DAILY_MARKET_SCHEDULE_CHANGED")
        operation, sha = "update", env.get("GITHUB_SHA")
    else:
        inputs = event.get("inputs")
        require(isinstance(inputs, dict) and set(inputs) == {"operation", "code_sha"}
            and inputs.get("operation") in {"status", "update"}, "DAILY_MARKET_INPUT")
        operation, sha = inputs["operation"], inputs["code_sha"]
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "DAILY_MARKET_CODE_SHA")
    run_id = env.get("GITHUB_RUN_ID")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id), "DAILY_MARKET_RUN_ID")
    return {"operation": operation, "code_sha": sha, "run_id": int(run_id), "run_attempt": 1,
        "event": event_name}


def native_ready(api, selected):
    check_private(api, selected)
    run = api.request("GET", PREFIX + "/actions/runs/" + str(selected["run_id"]))
    require(run.get("id") == selected["run_id"] and type(run.get("id")) is int
        and run.get("head_sha") == selected["code_sha"] and run.get("head_branch") == "main"
        and run.get("event") == selected["event"] and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1 and run.get("path") == WORKFLOW
        and run.get("name") == TITLE and run.get("status") == "in_progress"
        and run.get("repository", {}).get("id") == 1420041744
        and run.get("head_repository", {}).get("id") == 1420041744, "DAILY_MARKET_NATIVE_RUN")
    ci = api.request("GET", PREFIX + "/actions/workflows/ci.yml/runs?" + urlencode({
        "branch": "main", "event": "push", "head_sha": selected["code_sha"], "per_page": 5}))
    rows = ci.get("workflow_runs")
    require(isinstance(rows, list) and 0 < len(rows) <= 5 and type(ci.get("total_count")) is int
        and ci["total_count"] == len(rows)
        and all(isinstance(row, dict) and type(row.get("id")) is int for row in rows), "DAILY_MARKET_MAIN_CI")
    latest = max(rows, key=lambda row: row["id"])
    require(latest.get("head_sha") == selected["code_sha"] and latest.get("head_branch") == "main"
        and latest.get("event") == "push" and latest.get("path") == ".github/workflows/ci.yml"
        and latest.get("status") == "completed" and latest.get("conclusion") == "success"
        and type(latest.get("run_attempt")) is int and latest["run_attempt"] == 1
        and latest.get("name") == "decision-os-ci"
        and latest.get("repository", {}).get("id") == 1420041744
        and latest.get("head_repository", {}).get("id") == 1420041744, "DAILY_MARKET_MAIN_CI")
    return {"main_ci_run": latest["id"], "native_run_created_at": run.get("created_at")}


def remote_units(token, selected):
    limits = GitHubRateLimits()
    def open_unit(write):
        require(type(write) is bool and (not write or selected["operation"] == "update"),
            "DAILY_MARKET_READ_ONLY")
        api = GitHubAPI(token)
        api.rate_limits = limits
        native_ready(api, selected)
        binding = _records_runtime_binding("sync" if write else "status")
        readonly = replace(binding, write_enabled=False)
        store = VersionedStore(GitHubStore(binding, api))
        return store, lambda: VersionedStore(GitMarketReader(readonly, api)), api
    return open_unit


def _admit(api):
    if api is None:  # Explicit synthetic/local application caller, never the native host.
        return
    require(type(api._requests) is int and 0 <= api._requests < ADMISSION_REQUESTS
        and isinstance(api.rate_limits, GitHubRateLimits)
        and api.rate_limits.admits(MAX_REQUESTS - api._requests), "DAILY_MARKET_STORE_ADMISSION")


def _reason(exc, env=None):
    code = _safe_error(exc)
    if code == "FTSHARE_CHECK_FAILED" or any(isinstance(value, str) and value and value in code
            for key, value in (env or {}).items() if key in {TOKEN_ENV, SECRET_ENV, "FTSHARE_API_KEY"}):
        return "DAILY_MARKET_FAILED"
    return code


def run_daily(*, approved, registry, open_unit, operation="update", clock=None,
              guard=None, calendar_client_factory=None, price_client_factory=None,
              progress=None, capacity=market_capacity, monotonic=time.monotonic,
              sleep=time.sleep, execute_prices=run_collection, admission_at=None):
    """Advance at most one retained daily cycle, then use the normal exact reader."""
    approved = policy(approved)
    require(operation in {"status", "update"}, "DAILY_MARKET_OPERATION")
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    guard = guard or (lambda api: None)
    report = {"schema": "daily-market-runtime-v1", "operation": operation, "status": "started",
        "reason_code": None, "source_requests": 0, "calendar_source_requests": 0,
        "price_source_requests": 0, "calendar_source_budget": 2,
        "price_source_budget": approved["max_price_requests"], "stock_count": len(approved["symbols"]),
        "schedule_enabled": approved["enabled"], "scheduled_time": "18:35 Asia/Shanghai Monday-Friday",
        "started_at": clock(), "private_locations_in_receipt": False, "supplier_rows_in_receipt": False,
        "research_executed": False, "notification_sent": False, "long_term_uptime_certified": False}
    item = None
    control = {"schema": "daily-market-reading-control-v1", "market_reference": None,
        "cycle_reference": None, "generation": None, "calendar_references": {}}
    def update():
        report["source_requests"] = report["calendar_source_requests"] + report["price_source_requests"]
        if item is not None:
            report.update(cycle_date=item["cycle"]["date"], cycle_status=item["cycle"]["status"],
                calendar_reserved_requests=sum(row["reserved_requests"] for row in item["cycle"]["calendars"].values()))
        if progress is not None:
            progress(deepcopy(report), deepcopy(control))
    def save(state, store, fresh):
        nonlocal item
        item = retain_cycle(state, store, fresh_reader=fresh, previous=item)
        update()
    def unit(write):
        store, fresh, api = open_unit(write)
        guard(api)
        if write:
            require(capacity(fresh())["admit_work_unit"] is True, "DAILY_MARKET_STORAGE_CAPACITY")
            _admit(api)
        return store, fresh, api
    update()
    try:
        store, fresh, api = unit(operation == "update" and approved["enabled"])
        if operation == "status" or not approved["enabled"]:
            report["status"] = "read" if approved["enabled"] else "paused"
        else:
            prepared = initial_cycle(approved, fresh_market_reader(store, fresh), registry,
                at=admission_at or clock())
            item = prepared["existing"]
            if prepared["candidate"] is not None:
                save(prepared["candidate"], store, fresh)
            cycle = item["cycle"]
            if cycle["status"] in SUCCESS:
                report["status"] = "already-complete"
            elif cycle["status"] == "blocked":
                report.update(status="source-held", reason_code=cycle["reason_code"])
            elif any(row["status"] == "in-flight" for row in cycle["calendars"].values()):
                report.update(status="persistence-unconfirmed", reason_code="DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED")
            else:
                plan = calendar_plan(cycle, registry)
                calendar_client = None
                for venue, use in zip(VENUES, plan["requests"], strict=True):
                    if item["cycle"]["calendars"][venue]["status"] == "complete":
                        continue
                    store, fresh, api = unit(True)
                    calendars = deepcopy(item["cycle"]["calendars"])
                    calendars[venue].update(status="in-flight", reserved_requests=1)
                    save(next_state(item, at=clock(), calendars=calendars), store, fresh)
                    guard(api)
                    _admit(api)
                    try:
                        if calendar_client is None:
                            calendar_client = (calendar_client_factory or RelayHTTP)()
                            calendar_client.validate_credential()
                            require(type(calendar_client.requests) is int and calendar_client.requests == 0,
                                "DAILY_MARKET_FRESH_CALENDAR_CLIENT")
                        before = calendar_client.requests
                        require(before < 2, "DAILY_MARKET_CALENDAR_BUDGET")
                        session = RetryingRelay(calendar_client, RetryPolicy(max_attempts=1, max_wait_seconds=0),
                            clock=clock, monotonic=monotonic, deadline=monotonic() + 600)
                        try:
                            captured = capture_registered(use, registry, session, max_pages=1)
                        finally:
                            count = calendar_client.requests - before
                            require(type(count) is int and 0 <= count <= 1, "DAILY_MARKET_ONE_CALENDAR_REQUEST")
                            report["calendar_source_requests"] += count
                            update()
                        require(count == 1 and captured["complete"] and captured["error"] is None
                            and len(captured["pages"]) == 1, captured["error"] or "DAILY_MARKET_CALENDAR_INCOMPLETE")
                        result = {"case_id": plan["case_id"], "use": use, "status": "captured", "capture": captured}
                        qualified_trade_calendar(result, registry=registry, exchange=venue,
                            start=cycle["calendar_start"], end=cycle["calendar_end"])
                        guard(api)
                        retained = retain_market_query(result, registry, store, fresh_reader=fresh,
                            custody_authorized=True, public_rights_confirmed=False)
                        hold, delay = _source_instruction({**(calendar_client.last_response_meta or {}),
                            "rate_limits": calendar_client.last_header_diagnostics or {}})
                        require(hold is None, (hold or "DAILY_MARKET_CALENDAR_WAIT").replace(
                            "FTSHARE_", "DAILY_MARKET_CALENDAR_"))
                        sleep(delay)
                        calendars[venue].update(status="complete", reference=retained["reference"])
                        save(next_state(item, at=clock(), calendars=calendars), store, fresh)
                    except Exception as exc:
                        # A lost Store response keeps the durable in-flight reservation.
                        # No new date/run id can reserve this query again.
                        if isinstance(exc, UnknownWrite):
                            raise
                        guard(api)
                        calendars[venue].update(status="failed", reference=None, reason_code=_reason(exc))
                        save(next_state(item, at=clock(), calendars=calendars,
                            status="blocked", reason_code=_reason(exc)), store, fresh)
                        raise
                if item["cycle"]["status"] == "calendar-pending":
                    try:
                        session = completed_session(item["cycle"], registry, fresh_market_reader(store, fresh))
                        save(next_state(item, at=clock(), price_end=session["price_end"],
                            status="market-pending" if session["new_session"] else "no-new-session",
                            market_reference=None if session["new_session"] else item["cycle"]["base_market_reference"]),
                            store, fresh)
                    except Exception as exc:
                        if isinstance(exc, UnknownWrite):
                            raise
                        save(next_state(item, at=clock(), status="blocked", reason_code=_reason(exc)), store, fresh)
                        raise
                if item["cycle"]["status"] == "market-pending":
                    config = price_configuration(item["cycle"], approved, registry, fresh_market_reader(store, fresh))
                    references = {venue: item["cycle"]["calendars"][venue]["reference"] for venue in VENUES}
                    def price_progress(value):
                        report["price_source_requests"] = value["source_requests"]
                        report["price_execution"] = value
                        update()
                    executed = execute_prices(operation="ftshare-sync", config=config, registry=registry,
                        open_unit=open_unit, max_work_units=approved["max_price_requests"],
                        access_confirmed=True, custody_authorized=True, client_factory=price_client_factory,
                        guard=guard, progress=price_progress, clock=clock, monotonic=monotonic,
                        sleep=sleep, capacity=capacity, calendar_references=references)
                    price = executed["receipt"]
                    report["price_source_requests"], report["price_execution"] = price["source_requests"], price
                    if price["status"] in {"complete", "already-complete"}:
                        store, fresh, api = unit(True)
                        save(next_state(item, at=clock(), status="complete", market_reference=executed["reference"]), store, fresh)
                    elif price["status"] in {"failed", "exhausted", "source-held", "persistence-unconfirmed"}:
                        store, fresh, api = unit(True)
                        save(next_state(item, at=clock(), status="blocked",
                            reason_code=price["reason_code"] or "DAILY_MARKET_PRICE_REVIEW_REQUIRED"), store, fresh)
                    report.update(status=price["status"], reason_code=price["reason_code"])
                else:
                    report["status"] = item["cycle"]["status"]
    except Exception as exc:
        reason = _reason(exc)
        report.update(status="not-due" if reason == "DAILY_MARKET_BEFORE_CLOSE" else
            "persistence-unconfirmed" if isinstance(exc, UnknownWrite) else "blocked", reason_code=reason)
    # Latest attempts remain visible; a failed update does not silently become
    # an older green snapshot. The normal reader keeps all 5,226 denominator gaps.
    try:
        _, fresh, api = unit(False)
        reader = fresh()
        resolved = daily_market_reference(reader, registry, approved)
        control.update(market_reference=resolved["reference"], cycle_reference=resolved["cycle_reference"])
        if resolved["cycle"] is not None:
            current = resolved["cycle"]
            control.update(generation=current["market_generation"], calendar_references={venue: row["reference"]
                for venue, row in current["calendars"].items() if row["reference"] is not None})
            report.update(cycle_date=current["date"], cycle_status=current["status"],
                calendar_checked_through=current["calendar_end"]
                    if all(row["status"] == "complete" for row in current["calendars"].values()) else None,
                expected_completed_session=current["price_end"])
        reading = read_ftshare_collection_version(resolved["reference"], registry, reader, read_as_of=clock())
        report["health"] = health_receipt(reading)
        guard(api)
    except Exception as exc:
        report["reading_reason_code"] = _reason(exc)
        if report["status"] in {"read", "complete", "already-complete", "no-new-session"}:
            report.update(status="reading-unavailable", reason_code=_reason(exc))
    report["finished_at"] = clock()
    update()
    return {"receipt": report, "control": control}


def run_runtime(event, env, out, *, api=None, open_unit=None, execute=run_daily):
    approved = policy(_json(_regular(POLICY_FILE, 8192)))
    selected = settings(event, env, approved)
    require(isinstance(env.get(TOKEN_ENV), str) and bool(env[TOKEN_ENV]), "STORE_TOKEN_REQUIRED")
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
        and out.resolve() != root, "DAILY_MARKET_OUTPUT")
    api = GitHubAPI(env[TOKEN_ENV]) if api is None else api
    proof = native_ready(api, selected)
    now = datetime.now(timezone.utc)
    # Use the native run's date when admitting today's intent. A delayed run
    # cannot silently turn yesterday's invocation into a new day's budget.
    created = _stamp(proof["native_run_created_at"])
    require(0 <= (now - created).total_seconds() < 24 * 3600, "DAILY_MARKET_NATIVE_CLOCK")
    out.mkdir(parents=False, exist_ok=False)
    def progress(receipt, control):
        _save_receipt(out / "result.json", {**receipt, **selected, "native_proof": proof})
        _save_receipt(out / "control.json", control)
    executed = execute(approved=approved, registry=RelayRegistry(REGISTRY),
        open_unit=open_unit or remote_units(env[TOKEN_ENV], selected), operation=selected["operation"],
        guard=lambda current: check_private(current, selected), progress=progress,
        calendar_client_factory=lambda: RelayHTTP(credential=env.get(SECRET_ENV, ""), request_timeout_seconds=90),
        admission_at=created.isoformat())
    check_private(api, selected)
    progress(executed["receipt"], executed["control"])
    print(json.dumps({**executed["receipt"], **selected}, ensure_ascii=False))
    return 0 if executed["receipt"]["status"] in {"read", "complete", "already-complete", "no-new-session", "paused", "not-due"} else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("runtime", "release"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "release":
            check_private(GitHubAPI(os.environ[TOKEN_ENV]), {"code_sha": os.environ["GITHUB_SHA"]})
            return 0
        require(args.out is not None, "DAILY_MARKET_OUTPUT")
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return run_runtime(event, os.environ, args.out)
    except Exception as exc:
        print(_reason(exc, os.environ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
