"""One selected daily clock; no transport, source budget or stored cycle state."""
from __future__ import annotations

import re

from decision_kernel.domain.records import require

CLOCK_PATH = "config/daily-market-clock.json"
CLOCK_SCHEMA = "daily-market-clock-v1"
GITHUB_SCHEDULE = "github-schedule"
EXTERNAL_CRON = "external-cron"
EXTERNAL_CLOCK = "external-clock"
CLOCK_PROBE = "clock-probe"
CURRENT_MAIN = "current-main"
RUN_TITLE = "decision-os-daily-market"
EXTERNAL_RUN_TITLE = RUN_TITLE + " / external-clock"
PROBE_RUN_TITLE = RUN_TITLE + " / clock-probe"
MANUAL_OPERATIONS = frozenset({"status", "update", "recover-late", "repair-calendar",
    "reconcile-write", "release-master"})
DISPATCH_OPERATIONS = MANUAL_OPERATIONS | {EXTERNAL_CLOCK, CLOCK_PROBE}


def clock_policy(value):
    require(isinstance(value, dict) and set(value) == {"schema", "primary"}
        and value["schema"] == CLOCK_SCHEMA
        and isinstance(value["primary"], str)
        and value["primary"] in {GITHUB_SCHEDULE, EXTERNAL_CRON}, "DAILY_CLOCK_POLICY")
    return {"schema": CLOCK_SCHEMA, "primary": value["primary"]}


def admit_clock(invocation, clock=None):
    # A pure legacy caller's omitted configuration never enables the new entry.
    # Real hosts load the fixed checkout file explicitly before calling here.
    approved = clock_policy({"schema": CLOCK_SCHEMA, "primary": GITHUB_SCHEDULE}
        if clock is None else clock)
    require(isinstance(invocation, str) and invocation in DISPATCH_OPERATIONS | {"schedule"},
        "DAILY_CLOCK_INVOCATION")
    require(invocation != "schedule" or approved["primary"] == GITHUB_SCHEDULE,
        "DAILY_CLOCK_NOT_PRIMARY")
    require(invocation != EXTERNAL_CLOCK or approved["primary"] == EXTERNAL_CRON,
        "DAILY_CLOCK_NOT_PRIMARY")
    return approved["primary"]


def dispatch_selection(inputs, native_sha, clock=None, *, input_error="DAILY_CLOCK_INPUT",
                       sha_error="DAILY_CLOCK_CODE_SHA"):
    require(isinstance(inputs, dict) and set(inputs) == {"operation", "code_sha"}
        and isinstance(inputs.get("operation"), str)
        and inputs["operation"] in DISPATCH_OPERATIONS, input_error)
    invocation = inputs["operation"]
    require(isinstance(native_sha, str) and re.fullmatch(r"[a-f0-9]{40}", native_sha), sha_error)
    if invocation in {EXTERNAL_CLOCK, CLOCK_PROBE}:
        require(inputs["code_sha"] == CURRENT_MAIN, sha_error)
    else:
        require(inputs["code_sha"] == native_sha, sha_error)
    admit_clock(invocation, clock)
    operation = "status" if invocation in {"status", CLOCK_PROBE} else "update"
    return {"operation": operation, "invocation": invocation, "code_sha": native_sha}


def run_title(invocation):
    return EXTERNAL_RUN_TITLE if invocation == EXTERNAL_CLOCK else (
        PROBE_RUN_TITLE if invocation == CLOCK_PROBE else RUN_TITLE)
