"""Native GitHub daily host for the existing six-stock FTShare collection."""
from __future__ import annotations
from decision_kernel.infrastructure.runtime_performance import annotate_performance, diagnosed, measure_phase, timed

import argparse
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from decision_kernel.application.daily_clock import (
    CLOCK_PROBE, EXTERNAL_CLOCK, admit_clock, dispatch_selection, run_title,
)
from decision_kernel.application.daily_market import (
    MASTER_SCHEMAS, SUCCESS, VENUES, _stamp, calendar_plan, check_cycle_frontier,
    completed_session, daily_market_reference, initial_cycle, latest_cycle,
    next_state, policy, price_configuration, retain_cycle,
)
from decision_kernel.application.ftshare_collection_inputs import read_ftshare_collection_version
from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_collection_runtime import health_receipt
from decision_kernel.application.market_custody import fresh_market_reader, market_read_scope, retain_market_query
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
from decision_kernel.interfaces.daily_visibility_cli import load_clock_policy

WORKFLOW = ".github/workflows/daily-market.yml"
TITLE = "decision-os-daily-market"
POLICY_FILE = Path("config/daily-market.json")
MASTER_POLICY_FILE = Path("config/daily-master-refresh.json")
TOKEN_ENV = "DECISION_OS_STORE_TOKEN"
ADMISSION_REQUESTS = 240
MAX_QUOTA_REFRESH_WAITS = 5
MAX_QUOTA_WAIT_SECONDS = 5 * 3600


class DailyQuotaWindows:
    """Wait only between safe units; new quota must come from native headers."""
    def __init__(self, limits, refresh, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.limits, self.refresh = limits, refresh
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.waits, self.waited = 0, 0.0
        self.status, self.reason = "not-needed", None
        self.progress = None

    def receipt(self):
        return {"schema": "daily-market-quota-wait-v1", "status": self.status,
            "reason_code": self.reason, "refresh_waits": self.waits,
            "max_refresh_waits": MAX_QUOTA_REFRESH_WAITS, "waited_seconds": round(self.waited, 6),
            "max_wait_seconds": MAX_QUOTA_WAIT_SECONDS, "native_quota": self.limits.receipt()}

    def _emit(self, status, reason=None):
        self.status, self.reason = status, reason
        if self.progress is not None:
            self.progress()

    def _paused(self, reason):
        self._emit("paused", reason)
        return False

    def wait(self, api, reserve=MAX_REQUESTS):
        require(isinstance(api, GitHubAPI) and api.rate_limits is self.limits,
            "DAILY_MARKET_QUOTA_IDENTITY")
        if self.limits.admits(reserve):
            return True
        observed = self.limits.receipt()
        resources = observed["resources"]
        if observed["throttled"] or not observed["observed"]:
            return self._paused("DAILY_MARKET_QUOTA_NOT_WAITABLE")
        # A GET cannot establish a new GraphQL quota window. Never replenish an
        # old GraphQL observation from wall time or from a core response.
        if "graphql" in resources and resources["graphql"]["remaining"] < reserve:
            return self._paused("DAILY_MARKET_GRAPHQL_QUOTA_REQUIRES_REVIEW")
        core = resources["core"]
        now = self.clock()
        if not isinstance(now, (int, float)) or not math.isfinite(now) \
                or not now < core["reset"] <= now + 3600 or core["limit"] < reserve:
            return self._paused("DAILY_MARKET_QUOTA_RESET_UNUSABLE")
        delay = core["reset"] - now
        if self.waits >= MAX_QUOTA_REFRESH_WAITS or self.waited + delay > MAX_QUOTA_WAIT_SECONDS \
                or api._requests + 4 > MAX_REQUESTS:
            return self._paused("DAILY_MARKET_QUOTA_WAIT_BOUND")
        self.waits += 1
        self._emit("waiting-for-native-reset")
        while self.clock() < core["reset"]:
            delay = min(60.0, core["reset"] - self.clock(), MAX_QUOTA_WAIT_SECONDS - self.waited)
            if delay <= 0:
                return self._paused("DAILY_MARKET_QUOTA_WAIT_BOUND")
            before = self.monotonic()
            with measure_phase("quota.wait"):
                self.sleep(delay)
            elapsed = self.monotonic() - before
            if not math.isfinite(elapsed) or elapsed <= 0:
                return self._paused("DAILY_MARKET_QUOTA_CLOCK")
            self.waited += elapsed
            self._emit("waiting-for-native-reset")
            if self.waited > MAX_QUOTA_WAIT_SECONDS:
                return self._paused("DAILY_MARKET_QUOTA_WAIT_BOUND")
        # Permission/429 failures propagate. They never become another wait or
        # permission to reset a source client, reservation, or native allowance.
        try:
            self.refresh(api)
        except Exception:
            self._emit("failed", "DAILY_MARKET_QUOTA_NATIVE_REFRESH_FAILED")
            raise
        newer = self.limits.receipt()
        latest = newer["resources"].get("core")
        if newer["throttled"] or latest is None or latest["reset"] <= core["reset"] \
                or latest["reset"] <= self.clock() or not self.limits.admits(reserve):
            return self._paused("DAILY_MARKET_QUOTA_REFRESH_UNCONFIRMED")
        self._emit("observed-new-native-window")
        return True


def settings(event, env, approved, *, clock=None, allow_probe=False):
    approved = policy(approved)
    require(type(allow_probe) is bool, "DAILY_CLOCK_PROBE_ONLY")
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
        operation, invocation, sha = "update", "schedule", env.get("GITHUB_SHA")
        admit_clock(invocation, clock)
    else:
        selected = dispatch_selection(event.get("inputs"), env.get("GITHUB_SHA"), clock,
            input_error="DAILY_MARKET_INPUT", sha_error="DAILY_MARKET_CODE_SHA")
        operation, invocation, sha = selected["operation"], selected["invocation"], selected["code_sha"]
    require(invocation != CLOCK_PROBE or allow_probe, "DAILY_CLOCK_PROBE_ONLY")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "DAILY_MARKET_CODE_SHA")
    run_id = env.get("GITHUB_RUN_ID")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id), "DAILY_MARKET_RUN_ID")
    return {"operation": operation, "invocation": invocation,
        "code_sha": sha, "run_id": int(run_id), "run_attempt": 1,
        "event": event_name}


def native_ready(api, selected, *, metadata_only=False):
    require(type(metadata_only) is bool, "DAILY_CLOCK_METADATA_MODE")
    if metadata_only or selected.get("invocation") == CLOCK_PROBE:
        # Metadata inspection needs no business publication authority or Store.
        # Preflight and the dedicated clock probe may only inspect metadata here;
        # business runtimes still use the publication check below.
        from decision_kernel.interfaces.daily_visibility_cli import _repository
        _repository(api)
        main = api.request("GET", PREFIX + "/git/ref/heads/main")
        require(isinstance(main, dict) and main.get("ref") == "refs/heads/main"
            and isinstance(main.get("object"), dict) and main["object"].get("type") == "commit"
            and main["object"].get("sha") == selected["code_sha"], "FTSHARE_MAIN_MOVED")
    else:
        check_private(api, selected)
    run = api.request("GET", PREFIX + "/actions/runs/" + str(selected["run_id"]))
    require(run.get("id") == selected["run_id"] and type(run.get("id")) is int
        and run.get("head_sha") == selected["code_sha"] and run.get("head_branch") == "main"
        and run.get("event") == selected["event"] and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1 and run.get("path") == WORKFLOW
        and run.get("name") == run_title(selected.get("invocation")) and run.get("status") == "in_progress"
        and (selected.get("invocation") not in {EXTERNAL_CLOCK, CLOCK_PROBE}
             or run.get("display_title") == run_title(selected["invocation"]))
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


def remote_units(token, selected, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
    limits = GitHubRateLimits()
    quota = DailyQuotaWindows(limits, lambda api: native_ready(api, selected),
        clock=clock, monotonic=monotonic, sleep=sleep)
    def open_unit(write):
        require(type(write) is bool and (not write or selected["operation"] == "update"),
            "DAILY_MARKET_READ_ONLY")
        api = GitHubAPI(token)
        api.rate_limits = limits
        native_ready(api, selected)
        if write:
            require(quota.wait(api), "DAILY_MARKET_STORE_RATE_LIMIT")
        binding = _records_runtime_binding("sync" if write else "status")
        readonly = replace(binding, write_enabled=False)
        store = VersionedStore(GitHubStore(binding, api))
        return store, lambda: VersionedStore(GitMarketReader(readonly, api)), api
    open_unit.quota_wait = quota.wait
    open_unit.quota_receipt = quota.receipt
    open_unit.set_quota_progress = lambda callback: setattr(quota, "progress", callback)
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


def _master_interruption(venue, value):
    """Say whether a venue's interrupted master unit can retry on its own.

    A retained checkpoint means the last page request may have reached the source
    without a recorded response; this run will not send it again and the durable
    reservation stays for explicit reconciliation. No checkpoint means the unit
    simply starts over on the next run. Both cases used to look identical in the
    receipt, so an operator could not tell whether to act.
    """
    retained = value.get("checkpoint_reference") is not None
    return {"schema": "daily-master-interruption-v1", "venue": venue,
        "reason_code": value.get("reason_code"), "checkpoint_retained": retained,
        "automatic_retry": not retained,
        "next_action": "explicit-reconciliation" if retained else "retry-next-run"}


def _reconciliation_summary(cycle):
    original = cycle.get("write_reconciliation")
    if original is None:
        return None
    return {"schema": original["schema"], "authorization_ref": original["authorization_ref"],
        "additional_source_requests": 0, "original_run_id": original["native_run"]["run_id"],
        "original_run_attempt": original["native_run"]["run_attempt"],
        "original_price_source_budget": 12, "original_price_reserved_requests": 1,
        "original_price_pending_requests": 11}


@timed("daily.execute")
def run_daily(*, approved, registry, open_unit, operation="update", clock=None,
              guard=None, calendar_client_factory=None, price_client_factory=None,
              progress=None, capacity=market_capacity, monotonic=time.monotonic,
              sleep=time.sleep, execute_prices=run_collection, admission_at=None, allow_late=False,
              calendar_repair=None, write_reconciliation=None, reconciliation_verifier=None,
              master_refresh=None, master_client_factory=None, execute_master_unit=None):
    """Advance at most one retained daily cycle, then use the normal exact reader."""
    approved = policy(approved)
    require(operation in {"status", "update"}, "DAILY_MARKET_OPERATION")
    require(type(allow_late) is bool, "DAILY_MARKET_ADMISSION_MODE")
    require(calendar_repair is None or operation == "update", "DAILY_CALENDAR_REPAIR_OPERATION")
    require(write_reconciliation is None or operation == "update" and calendar_repair is None,
        "DAILY_WRITE_RECONCILIATION_OPERATION")
    if master_refresh is not None:
        from decision_kernel.application.daily_master_refresh import policy as master_policy
        master_refresh = master_policy(master_refresh)
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    guard = guard or (lambda api: None)
    report = {"schema": "daily-market-runtime-v1", "operation": operation, "status": "started",
        "reason_code": None, "source_requests": 0, "calendar_source_requests": 0,
        "price_source_requests": 0, "master_source_requests": 0, "calendar_source_budget": 2,
        "price_source_budget": approved["max_price_requests"], "stock_count": len(approved["symbols"]),
        "schedule_enabled": approved["enabled"], "scheduled_time": "18:35 Asia/Shanghai Monday-Friday",
        "started_at": clock(), "admission": None,
        "private_locations_in_receipt": False, "supplier_rows_in_receipt": False,
        "research_executed": False, "notification_sent": False, "long_term_uptime_certified": False}
    item = None
    control = {"schema": "daily-market-reading-control-v1", "market_reference": None,
        "cycle_reference": None, "generation": None, "calendar_references": {},
        "master_references": {},
        "frontier_reason_code": None, "frontier_blocking_reference": None,
        "frontier_successor_references": []}
    def update():
        report["source_requests"] = (report["calendar_source_requests"] + report["price_source_requests"]
            + report["master_source_requests"])
        quota_receipt = getattr(open_unit, "quota_receipt", None)
        if callable(quota_receipt):
            report["store_quota_wait"] = quota_receipt()
        if item is not None:
            report.update(cycle_date=item["cycle"]["date"], cycle_status=item["cycle"]["status"],
                calendar_reserved_requests=sum(row["reserved_requests"] for row in item["cycle"]["calendars"].values()))
        if item is not None and item["cycle"].get("calendar_repair") is not None:
            report.update(calendar_original_source_budget=2, calendar_additional_source_budget=1,
                calendar_source_budget=3, calendar_repair=deepcopy(item["cycle"]["calendar_repair"]))
        elif item is not None:
            # A new ordinary cycle must not inherit its predecessor's display
            # authority. The actual request owner already uses the normal cap.
            report["calendar_source_budget"] = approved["max_calendar_requests"]
            for key in ("calendar_original_source_budget", "calendar_additional_source_budget", "calendar_repair"):
                report.pop(key, None)
        if item is not None and item["cycle"].get("write_reconciliation") is not None:
            report["write_reconciliation"] = _reconciliation_summary(item["cycle"])
        elif item is not None:
            report.pop("write_reconciliation", None)
        if item is not None and item["cycle"]["schema"] in MASTER_SCHEMAS:
            report["master_refresh"] = deepcopy(item["cycle"]["master_refresh"])
            report["master_reserved_queries"] = sum(row["reserved_queries"]
                for row in item["cycle"]["masters"].values())
            report["master_query_budget"] = 2
            report["master_page_budget"] = 40
            report["master_source_budget"] = 160
        if progress is not None:
            progress(deepcopy(report), deepcopy(control))
    def save(state, store, fresh):
        nonlocal item
        item = retain_cycle(state, store, registry=registry, fresh_reader=fresh, previous=item,
            protected_symbols=approved["symbols"])
        update()
    def unit(write):
        store, fresh, api = open_unit(write)
        with measure_phase("daily.guard"):
            guard(api)
        if write:
            with measure_phase("daily.capacity"):
                require(capacity(fresh())["admit_work_unit"] is True, "DAILY_MARKET_STORAGE_CAPACITY")
            _admit(api)
        return store, fresh, api
    def refresh_masters():
        from decision_kernel.application.daily_master_refresh import run_master_unit
        execute = execute_master_unit or run_master_unit
        for venue in VENUES:
            if item["cycle"]["masters"][venue]["status"] == "complete":
                continue
            if item["cycle"]["masters"][venue]["status"] == "pending":
                store, fresh, api = unit(True)
                masters = deepcopy(item["cycle"]["masters"])
                masters[venue].update(status="in-flight", reserved_queries=1)
                save(next_state(item, at=clock(), masters=masters), store, fresh)
            # One accepted source attempt per finite unit, with one final
            # zero-request promotion allowed. Checkpoints keep the original
            # per-page retry allowance across clients and native quota windows.
            for _ in range(81):
                store, fresh, api = unit(True)
                def preflight(reader):
                    require(latest_cycle(reader, registry=registry, protected_symbols=approved["symbols"]) == item,
                        "DAILY_MARKET_STALE_CONTROL")
                    check_cycle_frontier(item["cycle"], registry, reader, protected_symbols=approved["symbols"])
                def admitted():
                    guard(api)
                    _admit(api)
                value = execute(cycle=item["cycle"], venue=venue, registry=registry, store=store,
                    fresh_reader=fresh, client_factory=master_client_factory or calendar_client_factory,
                    clock=clock, monotonic=monotonic, sleep=sleep, guard=admitted,
                    preflight=preflight)
                count = value["source_requests"]
                require(type(count) is int and 0 <= count <= 1, "DAILY_MASTER_WORK_UNIT_BUDGET")
                report["master_source_requests"] += count
                report["master_execution"] = {**deepcopy(value), "venue": venue}
                update()
                if value["status"] in {"complete", "failed", "source-held"}:
                    masters = deepcopy(item["cycle"]["masters"])
                    complete = value["status"] == "complete"
                    masters[venue].update(status="complete" if complete else "failed",
                        reference=value.get("reference") if complete else None,
                        checkpoint_reference=value.get("checkpoint_reference"),
                        reason_code=None if complete else value["reason_code"] or "DAILY_MASTER_REVIEW_REQUIRED")
                    store, fresh, api = unit(True)
                    save(next_state(item, at=clock(), masters=masters,
                        status="master-pending" if complete else "blocked",
                        reason_code=None if complete else masters[venue]["reason_code"]), store, fresh)
                    if not complete:
                        report.update(status=value["status"], reason_code=masters[venue]["reason_code"])
                        return
                    break
                require(value["status"] in {"partial", "persistence-unconfirmed"}, "DAILY_MASTER_UNIT_STATUS")
                if value["status"] == "persistence-unconfirmed" or count == 0:
                    report.update(status=value["status"], reason_code=value.get("reason_code"))
                    report["master_interruption"] = _master_interruption(venue, value)
                    return
            else:
                report.update(status="partial", reason_code="DAILY_MASTER_WORK_UNIT_BOUND")
                return
        store, fresh, api = unit(True)
        save(next_state(item, at=clock(), status="market-pending"), store, fresh)
    quota_progress = getattr(open_unit, "set_quota_progress", None)
    if callable(quota_progress):
        quota_progress(update)
    update()
    try:
        store, fresh, api = unit(operation == "update" and approved["enabled"])
        if operation == "status" or not approved["enabled"]:
            report["status"] = "read" if approved["enabled"] else "paused"
        else:
            repair_new = False
            reconciliation_new = False
            if write_reconciliation is not None:
                from decision_kernel.application.daily_market import policy_digest
                from decision_kernel.application.daily_write_reconciliation import select_reconciliation
                current = latest_cycle(fresh_market_reader(store, fresh), registry=registry,
                    protected_symbols=approved["symbols"])
                now = clock()
                reconciliation_new = select_reconciliation(current, write_reconciliation, at=now,
                    registry=registry, store=fresh_market_reader(store, fresh), approved=approved)
                require(current["cycle"]["policy_sha256"] == policy_digest(approved),
                    "DAILY_MARKET_POLICY_CHANGED")
                prepared = {"existing": current, "candidate": None, "admission": {
                    "mode": "explicit-write-reconciliation", "business_date": current["cycle"]["date"],
                    "admitted_at": now, "authorization_ref": write_reconciliation["authorization_ref"]}}
                if reconciliation_new:
                    require(callable(reconciliation_verifier) or api is None,
                        "DAILY_WRITE_RECONCILIATION_NATIVE_PROOF_REQUIRED")
                    if reconciliation_verifier is not None:
                        report["write_reconciliation_native_proof"] = reconciliation_verifier(write_reconciliation)
            elif calendar_repair is None:
                prepared = initial_cycle(approved, fresh_market_reader(store, fresh), registry,
                    at=clock(), admission_at=admission_at, allow_late=allow_late,
                    master_refresh=master_refresh)
            else:
                from decision_kernel.application.daily_market import policy_digest
                from decision_kernel.application.daily_calendar_repair import select_repair
                current = latest_cycle(fresh_market_reader(store, fresh), registry=registry,
                    protected_symbols=approved["symbols"])
                now = clock()
                repair_new = select_repair(current, calendar_repair, at=now)
                require(current["cycle"]["policy_sha256"] == policy_digest(approved),
                    "DAILY_MARKET_POLICY_CHANGED")
                if current["cycle"]["schema"] in MASTER_SCHEMAS:
                    require(master_refresh == current["cycle"]["master_refresh"], "DAILY_MASTER_POLICY_CHANGED")
                prepared = {"existing": current, "candidate": None, "admission": {
                    "mode": "explicit-calendar-repair", "business_date": current["cycle"]["date"],
                    "admitted_at": now, "authorization_ref": calendar_repair["authorization_ref"]}}
            item = prepared["existing"]
            report["admission"] = prepared["admission"]
            update()
            if prepared["candidate"] is not None:
                save(prepared["candidate"], store, fresh)
            if reconciliation_new:
                from decision_kernel.application.daily_write_reconciliation import reserve
                store, fresh, api = unit(True)
                save(reserve(item, write_reconciliation, at=clock()), store, fresh)
            cycle = item["cycle"]
            if cycle["status"] in SUCCESS:
                report["status"] = "already-complete"
            elif cycle["status"] == "blocked" and not repair_new:
                report.update(status="source-held", reason_code=cycle["reason_code"])
            elif any(row["status"] == "in-flight" for row in cycle["calendars"].values()):
                report.update(status="persistence-unconfirmed", reason_code="DAILY_MARKET_CALENDAR_RECONCILIATION_REQUIRED")
            else:
                plan = calendar_plan(cycle, registry)
                calendar_client = None
                for venue, use in zip(VENUES, plan["requests"], strict=True):
                    if item["cycle"]["calendars"][venue]["status"] == "complete":
                        continue
                    with measure_phase("daily.calendar"):
                        store, fresh, api = unit(True)
                        if repair_new and item["cycle"]["calendars"][venue]["status"] == "failed":
                            from decision_kernel.application.daily_calendar_repair import reserve
                            state = reserve(item, calendar_repair, at=clock())
                            repair_new = False
                        else:
                            calendars = deepcopy(item["cycle"]["calendars"])
                            calendars[venue].update(status="in-flight", reserved_requests=1)
                            state = next_state(item, at=clock(), calendars=calendars)
                        save(state, store, fresh)
                        calendars = deepcopy(item["cycle"]["calendars"])
                        guard(api)
                        _admit(api)
                        request_started = None
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
                            request_started = monotonic()
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
                            with measure_phase("relay.wait"):
                                sleep(delay)
                            calendars[venue].update(status="complete", reference=retained["reference"])
                            save(next_state(item, at=clock(), calendars=calendars), store, fresh)
                        except Exception as exc:
                            # A lost Store response keeps the durable in-flight reservation.
                            # No new date/run id can reserve this query again.
                            if isinstance(exc, UnknownWrite):
                                raise
                            if request_started is not None:
                                from decision_kernel.application.daily_calendar_repair import diagnostic
                                report["calendar_last_failure"] = diagnostic(venue, calendar_client,
                                    elapsed_seconds=monotonic() - request_started)
                                update()
                            guard(api)
                            calendars[venue].update(status="failed", reference=None, reason_code=_reason(exc))
                            save(next_state(item, at=clock(), calendars=calendars,
                                status="blocked", reason_code=_reason(exc)), store, fresh)
                            raise
                if item["cycle"]["status"] == "calendar-pending":
                    try:
                        session = completed_session(item["cycle"], registry, fresh_market_reader(store, fresh))
                        save(next_state(item, at=clock(), price_end=session["price_end"],
                            status=("master-pending" if item["cycle"]["schema"] in MASTER_SCHEMAS else "market-pending")
                                if session["new_session"] else "no-new-session",
                            market_reference=None if session["new_session"] else item["cycle"]["base_market_reference"]),
                            store, fresh)
                    except Exception as exc:
                        if isinstance(exc, UnknownWrite):
                            raise
                        save(next_state(item, at=clock(), status="blocked", reason_code=_reason(exc)), store, fresh)
                        raise
                if item["cycle"]["status"] == "master-pending":
                    with measure_phase("daily.masters"):
                        refresh_masters()
                if item["cycle"]["status"] == "market-pending":
                    config = price_configuration(item["cycle"], approved, registry, fresh_market_reader(store, fresh))
                    references = {venue: item["cycle"]["calendars"][venue]["reference"] for venue in VENUES}
                    def price_progress(value):
                        report["price_source_requests"] = value["source_requests"]
                        report["price_execution"] = value
                        update()
                    with measure_phase("daily.ft"):
                        executed = execute_prices(operation="ftshare-sync", config=config, registry=registry,
                            open_unit=open_unit, max_work_units=approved["max_price_requests"],
                            access_confirmed=True, custody_authorized=True, client_factory=price_client_factory,
                            guard=guard, progress=price_progress, clock=clock, monotonic=monotonic,
                            sleep=sleep, capacity=capacity, calendar_references=references,
                            **({"master_references": {v: item["cycle"]["masters"][v]["reference"] for v in VENUES}}
                                if item["cycle"]["schema"] in MASTER_SCHEMAS else {}),
                            quota_wait=getattr(open_unit, "quota_wait", None))
                    price = executed["receipt"]
                    report["price_source_requests"], report["price_execution"] = price["source_requests"], price
                    if price.get("write_last_failure") is not None:
                        report["write_last_failure"] = deepcopy(price["write_last_failure"])
                    if price["status"] in {"complete", "already-complete"}:
                        store, fresh, api = unit(True)
                        save(next_state(item, at=clock(), status="complete", market_reference=executed["reference"]), store, fresh)
                    elif price["status"] in {"failed", "exhausted", "source-held", "persistence-unconfirmed"}:
                        store, fresh, api = unit(True)
                        save(next_state(item, at=clock(), status="blocked",
                            reason_code=price["reason_code"] or "DAILY_MARKET_PRICE_REVIEW_REQUIRED"), store, fresh)
                    report.update(status=price["status"], reason_code=price["reason_code"])
                elif item["cycle"]["status"] != "master-pending" and report["status"] not in {
                        "failed", "source-held", "persistence-unconfirmed"}:
                    report["status"] = item["cycle"]["status"]
    except Exception as exc:
        reason = _reason(exc)
        from decision_kernel.infrastructure.github_store import write_failure_diagnostic
        diagnosis = write_failure_diagnostic(exc)
        if diagnosis is not None:
            report["write_last_failure"] = diagnosis
        report.update(status="not-due" if reason in {
            "DAILY_MARKET_BEFORE_CLOSE", "DAILY_MARKET_NO_RECENT_SCHEDULE"} else
            "persistence-unconfirmed" if isinstance(exc, UnknownWrite) else "blocked", reason_code=reason)
    # Latest attempts remain visible; a failed update does not silently become
    # an older green snapshot. The normal reader keeps the actual complete
    # current-L denominator and every retained request owner.
    try:
        _, fresh, api = unit(False)
        reader = fresh()
        pending_control, pending_report = deepcopy(control), deepcopy(report)
        reading_failure = None
        with measure_phase("daily.read_scope"), market_read_scope(reader) as grouped:
            with measure_phase("daily.resolve"):
                resolved = daily_market_reference(grouped, registry, approved)
            pending_control.update(market_reference=resolved["reference"], cycle_reference=resolved["cycle_reference"])
            for key in ("frontier_reason_code", "frontier_blocking_reference", "frontier_successor_references"):
                pending_control[key] = deepcopy(resolved[key])
            if resolved["frontier_reason_code"] is not None:
                pending_report["frontier_reason_code"] = resolved["frontier_reason_code"]
                if pending_report["status"] in {"read", "complete", "already-complete", "no-new-session"}:
                    pending_report.update(status="blocked", reason_code=resolved["frontier_reason_code"])
            if resolved["cycle"] is not None:
                current = resolved["cycle"]
                reconciliation = _reconciliation_summary(current)
                if reconciliation is not None:
                    pending_report["write_reconciliation"] = reconciliation
                pending_report["calendar_reserved_requests"] = sum(row["reserved_requests"] for row in current["calendars"].values())
                if current.get("calendar_repair") is not None:
                    pending_report.update(calendar_original_source_budget=2, calendar_additional_source_budget=1,
                        calendar_source_budget=3, calendar_repair=deepcopy(current["calendar_repair"]))
                pending_control.update(generation=current["market_generation"], calendar_references={venue: row["reference"]
                    for venue, row in current["calendars"].items() if row["reference"] is not None})
                if current["schema"] in MASTER_SCHEMAS:
                    pending_control["master_references"] = {venue: row["reference"]
                        for venue, row in current["masters"].items() if row["reference"] is not None}
                pending_report.update(cycle_date=current["date"], cycle_status=current["status"],
                    calendar_checked_through=current["calendar_end"]
                        if all(row["status"] == "complete" for row in current["calendars"].values()) else None,
                    expected_completed_session=current["price_end"])
            try:
                with measure_phase("daily.ft_read"):
                    reading = read_ftshare_collection_version(resolved["reference"], registry, grouped, read_as_of=clock())
                with measure_phase("daily.health"):
                    pending_report["health"] = health_receipt(reading)
            except Exception as exc:
                # A qualified frontier may remain useful when its FT view is
                # unavailable, but it must first pass the same final live check.
                reading_failure = exc
        if reading_failure is None:
            with measure_phase("daily.guard"):
                guard(api)
        control.update(pending_control)
        report.update(pending_report)
        if reading_failure is not None:
            raise reading_failure
    except Exception as exc:
        report["reading_reason_code"] = _reason(exc)
        if report["status"] in {"read", "complete", "already-complete", "no-new-session"}:
            report.update(status="reading-unavailable", reason_code=_reason(exc))
    report["finished_at"] = clock()
    update()
    return {"receipt": report, "control": control}


@diagnosed
def run_runtime(event, env, out, *, api=None, open_unit=None, execute=run_daily):
    approved = policy(_json(_regular(POLICY_FILE, 8192)))
    selected = settings(event, env, approved, clock=load_clock_policy())
    require(isinstance(env.get(TOKEN_ENV), str) and bool(env[TOKEN_ENV]), "STORE_TOKEN_REQUIRED")
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
        and out.resolve() != root, "DAILY_MARKET_OUTPUT")
    api = GitHubAPI(env[TOKEN_ENV]) if api is None else api
    proof = native_ready(api, selected)
    repair_options = {}
    if selected["invocation"] == "repair-calendar":
        from decision_kernel.application.daily_calendar_repair import load_grant
        repair_options["calendar_repair"] = load_grant()
    elif selected["invocation"] == "reconcile-write":
        from decision_kernel.application.daily_write_reconciliation import load_grant
        from decision_kernel.interfaces.daily_write_reconciliation_cli import verify_native_reconciliation
        repair_options["write_reconciliation"] = load_grant()
        repair_options["reconciliation_verifier"] = lambda grant: verify_native_reconciliation(
            api, grant, token=env[TOKEN_ENV])
    if selected["operation"] == "update" and selected["invocation"] != "reconcile-write":
        from decision_kernel.application.daily_master_refresh import policy as master_policy
        repair_options["master_refresh"] = master_policy(_json(_regular(MASTER_POLICY_FILE, 8192)))
    now = datetime.now(timezone.utc)
    # Freeze admission to the native invocation clock, even if a queue or a
    # safe quota wait later crosses midnight. Only the selected automatic clock
    # and explicit recover-late may
    # select the latest elapsed weekday slot; source timestamps stay real.
    created = _stamp(proof["native_run_created_at"])
    require(0 <= (now - created).total_seconds() < 24 * 3600, "DAILY_MARKET_NATIVE_CLOCK")
    out.mkdir(parents=False, exist_ok=False)
    def progress(receipt, control):
        _save_receipt(out / "result.json", annotate_performance({**receipt, **selected, "native_proof": proof}))
        _save_receipt(out / "control.json", control)
    executed = execute(approved=approved, registry=RelayRegistry(REGISTRY),
        open_unit=open_unit or remote_units(env[TOKEN_ENV], selected), operation=selected["operation"],
        guard=lambda current: check_private(current, selected), progress=progress,
        calendar_client_factory=lambda: RelayHTTP(credential=env.get(SECRET_ENV, ""), request_timeout_seconds=90),
        admission_at=created.isoformat(),
        allow_late=selected["invocation"] in {"schedule", "recover-late", EXTERNAL_CLOCK}, **repair_options)
    check_private(api, selected)
    progress(executed["receipt"], executed["control"])
    print(json.dumps({**executed["receipt"], **selected}, ensure_ascii=False))
    return 0 if executed["receipt"]["status"] in {"read", "complete", "already-complete", "no-new-session", "paused", "not-due"} else 2


def run_clock_probe(event, env, out, *, api=None):
    """Inspect this engineering entry only; never open records or a source client."""
    clock = load_clock_policy()
    approved = policy(_json(_regular(POLICY_FILE, 8192)))
    selected = settings(event, env, approved, clock=clock, allow_probe=True)
    require(selected["invocation"] == CLOCK_PROBE, "DAILY_CLOCK_PROBE_ONLY")
    require(isinstance(env.get(TOKEN_ENV), str) and bool(env[TOKEN_ENV]), "STORE_TOKEN_REQUIRED")
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
        and out.resolve() != root, "DAILY_MARKET_OUTPUT")
    api = GitHubAPI(env[TOKEN_ENV]) if api is None else api
    proof = native_ready(api, selected)
    report = {"schema": "daily-clock-probe-v1", "scope": "native-metadata-only", **selected,
        "status": "metadata-ok", "clock_primary": clock["primary"], "native_proof": proof,
        "external_origin_verified": False, "business_execution": False,
        "source_requests": 0, "records_read": False, "records_written": False,
        "dispatches": 0, "notifications": 0}
    # Recheck the same native evidence before releasing even this small report.
    require(native_ready(api, selected) == proof, "DAILY_CLOCK_PROBE_CHANGED")
    markdown = ("# 日更外部时钟连接检查\n\n"
        "状态：metadata-ok；仅核工程元数据，不证明外部服务已经启用或自然触发。\n\n"
        f"当前主时钟：`{clock['primary']}`。\n\n"
        f"代码：`{selected['code_sha']}`；运行：{selected['run_id']} / attempt 1；"
        f"原生事件：`{selected['event']}`；入口：`{selected['invocation']}`。\n\n"
        f"适用 main CI：{proof['main_ci_run']}。\n\n"
        "来源请求、records 读取/写入、工作流派发、通知均为 0；未读取研究或 Brief。\n")
    out.mkdir(parents=False, exist_ok=False)
    with (out / "probe.json").open("xb") as stream:
        stream.write(encoded(report))
    with (out / "probe.md").open("x", encoding="utf-8") as stream:
        stream.write(markdown)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("runtime", "release", "clock-probe"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "release":
            check_private(GitHubAPI(os.environ[TOKEN_ENV]), {"code_sha": os.environ["GITHUB_SHA"]})
            return 0
        require(args.out is not None, "DAILY_MARKET_OUTPUT")
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        if args.command == "clock-probe":
            return run_clock_probe(event, os.environ, args.out)
        return run_runtime(event, os.environ, args.out)
    except Exception as exc:
        print(_reason(exc, os.environ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
