"""One finite private Relay calendar extension for the FTShare daily transition.

The fixed two metadata requests use existing query custody in records. This
host does not request prices, alter the legacy collection, retry, or advance a
daily generation. Retire it with this dated continuity operation.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
import time

from decision_kernel.application.market_capacity import market_capacity
from decision_kernel.application.market_custody import (
    authorize_market_custody, fresh_market_reader, retain_market_query,
)
from decision_kernel.application.market_relay_inputs import qualified_trade_calendar
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_sources import capture_registered, validate_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.github_store import (
    MAX_REQUESTS, GitHubAPI, GitHubRateLimits, GitHubStore,
)
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.relay_http import RelayHTTP, SECRET_ENV
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy
from decision_kernel.interfaces.ftshare_check_cli import (
    REPO, WORKFLOW, _regular, _safe_error, check_private, native_ready,
)
from decision_kernel.interfaces.market_collection_cli import (
    BINDING_ENV, REGISTRY, _records_runtime_binding, _save_receipt,
)

CALENDAR_TITLE = "FTShare calendar continuity 20261018"
TITLE = CALENDAR_TITLE
STUDY = "ftshare-calendar-continuity-20261018"
START, END = "20260701", "20261018"
VENUES = ("SSE", "SZSE")
SOURCE_BUDGET = 2
SOURCE_ADMISSION_API_REQUESTS = 240
TOKEN_ENV = "DECISION_OS_STORE_TOKEN"


def settings(event, env):
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1", "FTSHARE_CALENDAR_MANUAL_MAIN_ATTEMPT_ONE")
    data = event.get("inputs") if isinstance(event, dict) else None
    require(isinstance(data, dict) and set(data) <= {"operation", "code_sha", "selection",
        "work_units", "repair_request", "access_and_custody_confirmed", "public_rights_confirmed"},
        "FTSHARE_CALENDAR_EVENT")
    require(data.get("operation") == "ftshare-calendar"
        and (data.get("access_and_custody_confirmed") is True
             or data.get("access_and_custody_confirmed") == "true")
        and (data.get("public_rights_confirmed") is False
             or data.get("public_rights_confirmed") == "false")
        and str(data.get("work_units", "1")) == "1"
        and data.get("repair_request") in (None, ""), "FTSHARE_CALENDAR_EXPLICIT_PRIVATE_SCOPE")
    selection = data.get("selection")
    require(isinstance(selection, str) and 0 < len(selection.encode()) <= 256
        and _json(selection.encode()) == {"study": STUDY}, "FTSHARE_CALENDAR_FIXED_STUDY")
    sha, run_id = data.get("code_sha"), env.get("GITHUB_RUN_ID")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "FTSHARE_CALENDAR_CODE_SHA")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id),
        "FTSHARE_CALENDAR_RUN_ID")
    require(not env.get(BINDING_ENV), "ASHARE_RUNTIME_BINDING_OVERRIDE_FORBIDDEN")
    require(not env.get("FTSHARE_API_KEY"), "FTSHARE_CALENDAR_UNEXPECTED_PRICE_CREDENTIAL")
    return {"study": STUDY, "code_sha": sha, "run_id": run_id, "attempt": 1,
        "repository": REPO, "source_budget": SOURCE_BUDGET}


def calendar_plan(registry):
    return validate_plan({"schema": "research-source-plan-v1", "case_id": STUDY,
        "requests": [{"stage": "baseline", "purpose": "Retain " + venue
            + " calendar metadata through 20261018 for FTShare daily freshness",
            "api": "trade_cal", "parameters": {"exchange": venue, "start_date": START,
                "end_date": END, "fields": "exchange,cal_date,is_open", "limit": "5000"}}
            for venue in VENUES]}, registry)


def _open_store(api):
    binding = _records_runtime_binding("sync")
    readonly = replace(binding, write_enabled=False)
    store = VersionedStore(GitHubStore(binding, api))
    return store, lambda: VersionedStore(GitMarketReader(readonly, api))


def _admit(api):
    require(type(api._requests) is int and 0 <= api._requests < SOURCE_ADMISSION_API_REQUESTS,
        "FTSHARE_CALENDAR_STORE_ADMISSION_BUDGET")
    require(isinstance(getattr(api, "rate_limits", None), GitHubRateLimits)
        and api.rate_limits.admits(MAX_REQUESTS - api._requests), "FTSHARE_CALENDAR_STORE_RATE_LIMIT")


def _reason(exc, env):
    code = _safe_error(exc)
    # Stable local codes only; credentials are never valid error content.
    if code == "FTSHARE_CHECK_FAILED" or any(isinstance(env.get(key), str)
            and env[key] and env[key] in code for key in (TOKEN_ENV, SECRET_ENV)):
        return "FTSHARE_CALENDAR_FAILED"
    return code


def run_runtime(event, env, out, *, api=None, open_store=None, client_factory=None,
                capacity=market_capacity, clock=None, monotonic=time.monotonic):
    selected = settings(event, env)
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
        and out.resolve() != root, "FTSHARE_CALENDAR_OUTPUT")
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    # The native title inventory consumes the operation across runs. A fixed
    # runner-local marker also rejects a second invocation with another --out.
    try:
        with (root / (STUDY + ".used")).open("xb") as marker:
            marker.write(encoded(selected))
    except FileExistsError:
        raise RecordError("FTSHARE_CALENDAR_ALREADY_CONSUMED") from None
    out.mkdir(parents=False, exist_ok=False)
    report = {**selected, "schema": "ftshare-calendar-runtime-v1", "status": "started",
        "source_requests": 0, "reserved_requests": 0, "retained_queries": 0,
        "reason_code": None, "started_at": clock(), "private_release": "not-verified",
        "retry_allowed": False, "legacy_collection_changed": False, "price_source_requests": 0,
        "supplier_rows_in_receipt": False, "private_locations_in_receipt": False,
        "queries": [{"exchange": venue, "status": "not-requested", "source_requests": 0,
                     "reason_code": None} for venue in VENUES]}
    control = {**selected, "schema": "ftshare-calendar-control-v1", "status": "started",
        "calendar_references": {}, "representation": "requested-column-projection-not-original-http"}

    def save():
        report["updated_at"] = clock()
        control["status"] = report["status"]
        _save_receipt(out / "control.json", control)
        _save_receipt(out / "result.json", report)

    save()
    client, phase, active = None, "admission", None
    try:
        require(isinstance(env.get(TOKEN_ENV), str) and bool(env[TOKEN_ENV]), "STORE_TOKEN_REQUIRED")
        api = GitHubAPI(env[TOKEN_ENV]) if api is None else api
        report["proof"] = native_ready(api, selected, title=CALENDAR_TITLE)
        registry = RelayRegistry(REGISTRY)
        plan = calendar_plan(registry)  # Both exact queries before any source credential or HTTP.
        store, fresh = (open_store or _open_store)(api)
        authorize_market_custody(store, custody_authorized=True, public_rights_confirmed=False)
        reader = fresh_market_reader(store, fresh)
        report["capacity"] = capacity(reader)
        require(report["capacity"]["admit_work_unit"] is True, "FTSHARE_CALENDAR_STORAGE_CAPACITY")
        _admit(api)
        client = (client_factory() if client_factory is not None else RelayHTTP(
            credential=env.get(SECRET_ENV, ""), request_timeout_seconds=90))
        client.validate_credential()
        require(type(client.requests) is int and client.requests == 0, "FTSHARE_CALENDAR_FRESH_CLIENT")
        session = RetryingRelay(client, RetryPolicy(max_attempts=1, max_wait_seconds=0),
            clock=clock, monotonic=monotonic, deadline=monotonic() + 600)
        for row, use in zip(report["queries"], plan["requests"], strict=True):
            active, phase = row, "admission"
            check_private(api, selected)
            _admit(api)
            before = client.requests
            require(before == report["source_requests"] and before < SOURCE_BUDGET,
                "FTSHARE_CALENDAR_SOURCE_BUDGET")
            report["reserved_requests"] += 1
            row["status"], phase = "request-started", "source"
            save()  # Local interruption remains consumed; there is no resume operation.
            try:
                captured = capture_registered(use, registry, session, max_pages=1)
            finally:
                after = client.requests
                require(type(after) is int and before <= after <= before + 1,
                    "FTSHARE_CALENDAR_SOURCE_COUNTER")
                report["source_requests"], row["source_requests"] = after, after - before
                save()
            require(captured["complete"] and captured["error"] is None,
                captured["error"] or "FTSHARE_CALENDAR_INCOMPLETE")
            require(row["source_requests"] == 1 and len(captured["pages"]) == 1,
                "FTSHARE_CALENDAR_SINGLE_PAGE_REQUIRED")
            phase = "qualification"
            result = {"case_id": STUDY, "use": use, "status": "captured", "capture": captured}
            days = qualified_trade_calendar(result, registry=registry, exchange=row["exchange"],
                start=START, end=END)
            check_private(api, selected)
            phase = "custody"
            saved = retain_market_query(result, registry, store, fresh_reader=fresh,
                custody_authorized=True, public_rights_confirmed=False)
            restored = saved["restored"]
            require(saved["page_count"] == 1 and encoded(saved["reference"]) == encoded(restored["reference"])
                and days == qualified_trade_calendar(restored["result"], registry=registry,
                    exchange=row["exchange"], start=START, end=END), "FTSHARE_CALENDAR_RESTORE_CHANGED")
            control["calendar_references"][row["exchange"]] = _json(encoded(restored["reference"]))
            report["retained_queries"] += 1
            row.update(status="retained", row_count=len(captured["pages"][0]["table"]["items"]),
                open_session_count=len(days), independently_restored=True)
            save()
        report["status"] = "retained"
    except Exception as exc:
        code = _reason(exc, env)
        report["reason_code"] = code
        report["status"] = ("persistence-unconfirmed" if phase == "custody" else "partial"
            if report["retained_queries"] else "failed" if report["source_requests"] else "blocked")
        if active is not None:
            active.update(status="persistence-unconfirmed" if phase == "custody" else "failed",
                reason_code=code)
    # The workflow also checks private visibility immediately before publishing
    # its private artifact; no supplier rows or exact references go to stdout.
    if api is not None:
        report["store_api_requests"] = getattr(api, "_requests", None)
        limits = getattr(api, "rate_limits", None)
        try:
            require(type(api._requests) is int and api._requests + 2 <= MAX_REQUESTS
                and isinstance(limits, GitHubRateLimits) and limits.admits(2),
                "FTSHARE_CALENDAR_RELEASE_BUDGET")
            check_private(api, selected)
            report["private_release"] = "verified-private-only"
        except Exception as exc:
            code = _reason(exc, env)
            report.update(status="release-blocked", private_release="not-verified", release_reason_code=code)
            report["reason_code"] = report["reason_code"] or code
        report["store_api_requests"] = getattr(api, "_requests", None)
        if isinstance(limits, GitHubRateLimits):
            report["store_rate_limits"] = limits.receipt()
    save()
    return 0 if report["status"] == "retained" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("runtime",))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return run_runtime(event, os.environ, args.out)
    except Exception as exc:
        print(_reason(exc, os.environ), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
