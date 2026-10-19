"""User-authorized Kernel v1 route check; original two v2 refusals stay spent.

Four whole-range requests share the existing six-call study ceiling. This host
does not start the all-market collection or claim that a sample covers it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import time

from decision_kernel.application.ftshare_single import capture_single
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.research_receipts import download_archive
from decision_kernel.interfaces import ftshare_check_continuation_cli as prior_check
from decision_kernel.interfaces.ftshare_check_cli import (
    BASE_REFERENCE, PREFIX, REPO, STUDY, WORKFLOW, _regular, _safe_error,
    check_private, date_bounds, load_baseline, native_ready, next_request_delay,
)
from decision_kernel.interfaces.market_collection_cli import REGISTRY, _records_runtime_binding

TITLE = "FTShare Kernel v1 daily check 20261019"
SCHEMA = "ftshare-kernel-daily-check-v1"
PREDECESSOR_RUN = 39145109553
PREDECESSOR_CODE = "64b4d5f6bfe0f9373a8e7f33f590979178de22e3"
PREDECESSOR_ARTIFACT = 12143465795
PREDECESSOR_SHA256 = "92bd239a077025ef24633b9050d8b859cc9233bfb650379a2be4c59eb85cd725"
PREDECESSOR_BYTES = 69318
PREVIOUS_REQUESTS, MAX_REQUESTS, TOTAL_BUDGET = 2, 4, 6
ROUTE = "kernel-v1"


def settings(event, env):
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1", "FTSHARE_KERNEL_MANUAL_MAIN")
    data = event.get("inputs") if isinstance(event, dict) else None
    require(isinstance(data, dict) and set(data) <= {"operation", "code_sha", "selection", "work_units",
        "repair_request", "access_and_custody_confirmed", "public_rights_confirmed"}, "FTSHARE_KERNEL_EVENT")
    require(data.get("operation") == "ftshare-check-kernel"
        and (data.get("access_and_custody_confirmed") is True or data.get("access_and_custody_confirmed") == "true")
        and (data.get("public_rights_confirmed") is False or data.get("public_rights_confirmed") == "false")
        and str(data.get("work_units", "1")) == "1" and not data.get("repair_request"),
        "FTSHARE_KERNEL_EXPLICIT_PRIVATE")
    raw = data.get("selection")
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= 256, "FTSHARE_KERNEL_SELECTION")
    selection = _json(raw.encode())
    require(selection == {"study": STUDY, "route": ROUTE, "predecessor_run": PREDECESSOR_RUN}
        and type(selection["predecessor_run"]) is int, "FTSHARE_KERNEL_SELECTION")
    sha, run_id = data.get("code_sha"), env.get("GITHUB_RUN_ID")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "FTSHARE_KERNEL_CODE_SHA")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id)
        and int(run_id) not in {PREDECESSOR_RUN, prior_check.PREDECESSOR_RUN}, "FTSHARE_KERNEL_RUN_ID")
    return {"study": STUDY, "route": ROUTE, "repository": REPO, "code_sha": sha,
        "run_id": run_id, "attempt": 1, "source_budget": TOTAL_BUDGET, "route_budget": MAX_REQUESTS}


def predecessor_identity():
    return {"run_id": PREDECESSOR_RUN, "run_attempt": 1, "code_sha": PREDECESSOR_CODE,
        "artifact_id": PREDECESSOR_ARTIFACT, "artifact_sha256": PREDECESSOR_SHA256,
        "artifact_bytes": PREDECESSOR_BYTES, "source_requests": 1,
        "cumulative_source_requests": PREVIOUS_REQUESTS, "outcome": "second-v2-first-request-http-403"}


def predecessor_documents(docs, original):
    require(isinstance(docs, dict) and set(docs) == {
        "baseline.json", "request.json", "result.json", "predecessor.json"}, "FTSHARE_KERNEL_PREDECESSOR_MEMBERS")
    original = prior_check._predecessor_documents(original["documents"])
    require(docs["predecessor.json"] == original
        and docs["baseline.json"] == original["documents"]["baseline.json"], "FTSHARE_KERNEL_ORIGINAL_CHANGED")
    selected = {"study": STUDY, "repository": REPO, "code_sha": PREDECESSOR_CODE,
        "run_id": str(PREDECESSOR_RUN), "attempt": 1, "source_budget": 6, "continuation_budget": 5}
    expected = prior_check._prepared(selected, original, docs["baseline.json"])
    require(docs["request.json"] == expected, "FTSHARE_KERNEL_PREDECESSOR_REQUEST")
    result = docs["result.json"]
    require(isinstance(result, dict) and result.get("schema") == prior_check.SCHEMA
        and all(result.get(k) == v for k, v in selected.items())
        and result.get("predecessor") == original["identity"] and result.get("status") == "failed"
        and type(result.get("source_requests")) is int and result["source_requests"] == 1
        and result.get("reserved_requests") == 1 and result.get("prior_source_requests") == 1
        and result.get("cumulative_source_requests") == PREVIOUS_REQUESTS
        and result.get("qualified_candle_checks") == 0 and result.get("required_candle_checks") == 5
        and result.get("records_written") is False and result.get("active_source_changed") is False
        and result.get("raw_release") == "private-artifact-only", "FTSHARE_KERNEL_PREVIOUS_SPENDING")
    rows = result.get("results")
    require(isinstance(rows, list) and len(rows) == 5 and rows[0].get("id") == "raw-pair"
        and rows[0].get("status") == "failed" and rows[0].get("source_requests") == 1
        and rows[0].get("error") == "FTSHARE_HTTP_403"
        and rows[0].get("transport", {}).get("http_status") == 403
        and all(row == {"id": spec["id"], "status": "not-requested"}
            for row, spec in zip(rows[1:], expected["plan"][1:])), "FTSHARE_KERNEL_PREVIOUS_OUTCOME")
    return {"identity": predecessor_identity(), "documents": docs}


def load_predecessor(api, selected, *, archive_loader=None):
    original = prior_check.load_predecessor(api, selected, archive_loader=archive_loader)
    run = api.request("GET", PREFIX + "/actions/runs/" + str(PREDECESSOR_RUN))
    require(run.get("id") == PREDECESSOR_RUN and run.get("run_attempt") == 1
        and run.get("head_sha") == PREDECESSOR_CODE and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch" and run.get("path") == WORKFLOW
        and run.get("display_title") == prior_check.CONTINUATION_TITLE
        and run.get("status") == "completed" and run.get("conclusion") == "failure"
        and run.get("repository", {}).get("id") == 1420041744, "FTSHARE_KERNEL_PREDECESSOR_RUN")
    meta = api.request("GET", PREFIX + "/actions/artifacts/" + str(PREDECESSOR_ARTIFACT))
    producer = meta.get("workflow_run", {})
    require(meta.get("id") == PREDECESSOR_ARTIFACT and meta.get("expired") is False
        and meta.get("name") == f"dos2-ftshare-check-continuation-{PREDECESSOR_RUN}-1"
        and meta.get("size_in_bytes") == PREDECESSOR_BYTES and meta.get("digest") == "sha256:" + PREDECESSOR_SHA256
        and producer.get("id") == PREDECESSOR_RUN and producer.get("head_sha") == PREDECESSOR_CODE
        and producer.get("repository_id") == 1420041744 and producer.get("head_repository_id") == 1420041744,
        "FTSHARE_KERNEL_PREDECESSOR_ARTIFACT")
    raw = (archive_loader or download_archive)(REPO, PREDECESSOR_ARTIFACT)
    docs = prior_check._archive_documents(raw, sha256=PREDECESSOR_SHA256, size=PREDECESSOR_BYTES,
        names=("baseline.json", "request.json", "result.json", "predecessor.json"))
    return predecessor_documents(docs, original)


def load_verification(api, baseline):
    """Read original retained decimal tokens and the existing exchange calendars."""
    from decision_kernel.application.ftshare_collection import _legacy_context
    from decision_kernel.application.market_custody import market_read_scope, restore_market_query
    registry = RelayRegistry(REGISTRY)
    store = VersionedStore(GitMarketReader(_records_runtime_binding("status"), api))
    with market_read_scope(store) as reader:
        context = _legacy_context(BASE_REFERENCE, registry, reader)
        restored = restore_market_query(baseline["daily_reference"], registry, reader)
        prices = {}
        for ref in restored["page_references"]:
            text = reader.read_source(SourceRef.from_dict(ref))["data"]
            page = json.loads(text, parse_float=str)["page"]["table"]
            for values in page["items"]:
                row = dict(zip(page["fields"], values, strict=True))
                code = row["ts_code"]
                if code in {"000001.SZ", "600000.SH"}:
                    require(code not in prices and row["trade_date"] == "20261016",
                        "FTSHARE_KERNEL_BASELINE_PRICE_IDENTITY")
                    value = Decimal(str(row["close"]))
                    require(value.is_finite() and value > 0, "FTSHARE_KERNEL_BASELINE_PRICE")
                    prices[code] = str(value)
        require(set(prices) == {"000001.SZ", "600000.SH"}, "FTSHARE_KERNEL_BASELINE_PRICES_REQUIRED")
        return {"close": prices, "sessions": context["sessions"],
            "daily_reference": baseline["daily_reference"], "page_references": restored["page_references"],
            "calendar_references": context["calendar_references"], "scope_count": len(context["scope_symbols"])}


def check_plan():
    since, until = date_bounds("20260701", "20261016")
    return [{"id": name, "symbol": symbol, "since": since, "until": until, "adjust_kind": kind}
        for name, symbol, kind in (("raw-sz-history", "000001.SZ", "none"),
            ("raw-sz-repeat", "000001.SZ", "none"), ("raw-sh-history", "600000.SH", "none"),
            ("forward-sz-history", "000001.SZ", "forward"))]


def _prepared(selected, prior, baseline, verification):
    return {**selected, "plan": check_plan(), "predecessor": prior["identity"],
        "predecessor_sha256": digest(encoded(prior)), "baseline_sha256": digest(encoded(baseline)),
        "verification_sha256": digest(encoded(verification))}


def execute(stage, event, env, out, *, api=None, client_factory=None, baseline_loader=load_baseline,
            predecessor_loader=load_predecessor, verification_loader=load_verification,
            sleep=time.sleep, monotonic=time.monotonic, clock=None):
    selected = settings(event, env)
    require(stage in {"prepare", "capture", "release"}, "FTSHARE_KERNEL_STAGE")
    root, out = Path(env["RUNNER_TEMP"]).resolve(), Path(out)
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
        "FTSHARE_KERNEL_OUTPUT_PATH")
    api = GitHubAPI.from_environment() if api is None else api
    proof = native_ready(api, selected, title=TITLE)
    prior = predecessor_loader(api, selected)
    prior = predecessor_documents(prior["documents"], prior["documents"]["predecessor.json"])
    if stage == "prepare":
        baseline = baseline_loader(api)
        require(baseline == prior["documents"]["baseline.json"], "FTSHARE_KERNEL_BASELINE_CHANGED")
        verification = verification_loader(api, baseline)
        check_private(api, selected)
        out.mkdir(parents=False, exist_ok=False)
        for name, value in {"baseline": baseline, "predecessor": prior, "verification": verification,
                "request": _prepared(selected, prior, baseline, verification),
                "result": {"status": "prepared-no-source-requests", "source_requests": 0}}.items():
            (out / (name + ".json")).write_bytes(encoded(value))
        return 0
    baseline = _json(_regular(out / "baseline.json", 2 * 1024 * 1024))
    verification = _json(_regular(out / "verification.json", 65536))
    require(baseline == prior["documents"]["baseline.json"]
        and _json(_regular(out / "predecessor.json", 4 * 1024 * 1024)) == prior
        and _json(_regular(out / "request.json", 65536)) == _prepared(selected, prior, baseline, verification),
        "FTSHARE_KERNEL_PREPARED_CHANGED")
    require(encoded(verification) == encoded(verification_loader(api, baseline)),
        "FTSHARE_KERNEL_VERIFICATION_CHANGED")
    previous = _json(_regular(out / "result.json", 262144))
    if stage == "release":
        require(previous.get("schema") == SCHEMA and previous.get("status") in {"validated", "partial", "failed"}
            and previous.get("raw_release") == "not-released" and previous.get("predecessor") == prior["identity"]
            and type(previous.get("source_requests")) is int and 0 <= previous["source_requests"] <= MAX_REQUESTS
            and previous.get("cumulative_source_requests") == PREVIOUS_REQUESTS + previous["source_requests"],
            "FTSHARE_KERNEL_RELEASE_STATE")
        check_private(api, selected)
        previous["raw_release"] = "private-artifact-only"
        (out / "result.json").write_bytes(encoded(previous))
        print(encoded({k: v for k, v in previous.items() if k != "results"}).decode())
        return 0
    require(previous == {"status": "prepared-no-source-requests", "source_requests": 0},
        "FTSHARE_KERNEL_ALREADY_CONSUMED")
    # Same native run, including a copied output directory, can enter only once.
    marker = root / (".ftshare-kernel-" + selected["run_id"] + "-1.used")
    try:
        with marker.open("xb") as stream:
            stream.write(encoded(selected))
    except FileExistsError:
        raise RecordError("FTSHARE_KERNEL_ALREADY_STARTED") from None
    from decision_kernel.infrastructure.ftshare_http import FTShareHTTP
    client = (client_factory or FTShareHTTP)()
    client.validate_credential()
    require(type(client.requests) is int and client.requests == 0, "FTSHARE_FRESH_CLIENT_REQUIRED")
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    report = {**selected, "schema": SCHEMA, "predecessor": prior["identity"], "proof": proof,
        "status": "started", "source_requests": 0, "reserved_requests": 0,
        "prior_source_requests": PREVIOUS_REQUESTS, "cumulative_source_requests": PREVIOUS_REQUESTS,
        "records_written": False, "active_source_changed": False, "raw_release": "not-released",
        "started_at": clock(), "results": [{"id": item["id"], "status": "not-requested"} for item in check_plan()],
        "full_market_coverage_certified": False, "long_term_uptime_certified": False,
        "factor_qualification_granted": False, "account_daily_limit_verified": False}
    def save():
        (out / "result.json").write_bytes(encoded(report))
    save()
    normalized, failed = {}, False
    for spec, row in zip(check_plan(), report["results"]):
        try:
            check_private(api, selected)
            if client.requests:
                sleep(next_request_delay(client))
                check_private(api, selected)
        except Exception as exc:
            report["stop_reason"], failed = _safe_error(exc), True
            break
        before, started = client.requests, monotonic()
        report["reserved_requests"] += 1
        require(report["reserved_requests"] <= MAX_REQUESTS, "FTSHARE_KERNEL_SOURCE_BUDGET")
        row.update(status="request-started", requested_at=clock())
        save()
        try:
            args = {k: v for k, v in spec.items() if k != "id"}
            client.candlesticks(**args)
            raw = client.last_response_bytes
            require(type(raw) is bytes and 0 < len(raw) <= 2 * 1024 * 1024, "FTSHARE_ORIGINAL_BODY_REQUIRED")
            check_private(api, selected)
            name = spec["id"] + ".raw.json"
            (out / name).write_bytes(raw)
            row.update(raw_file=name, raw_bytes=len(raw), raw_sha256=digest(raw))
            capture = capture_single(raw, **args, acquired_at=clock())
            checked = capture["normalized"]
            bars = checked["series"][spec["symbol"]]
            venue = "SSE" if spec["symbol"].endswith(".SH") else "SZSE"
            require(checked["counts"]["quarantined_rows"] == 0, "FTSHARE_CHECK_QUARANTINED_ROWS")
            require(bars and [bar["trade_date"] for bar in bars] == verification["sessions"][venue]
                and len(bars) >= 61, "FTSHARE_KERNEL_HISTORY_INCOMPLETE")
            if spec["adjust_kind"] == "none":
                row["close_matches_baseline"] = Decimal(bars[-1]["close"]) == Decimal(verification["close"][spec["symbol"]])
                require(row["close_matches_baseline"], "FTSHARE_KERNEL_CLOSE_MISMATCH")
            if spec["id"] == "raw-sz-repeat":
                row["repeat_equal"] = checked["series"] == normalized["raw-sz-history"]["series"]
                require(row["repeat_equal"], "FTSHARE_REPEAT_CHANGED")
            normalized[spec["id"]] = checked
            # Exact captures support source-free replay and the existing Store adapter.
            (out / (spec["id"] + ".capture.json")).write_bytes(encoded(capture))
            row.update(status="qualified-response", counts=checked["counts"],
                request=capture["request"], request_sha256=capture["request_sha256"])
        except Exception as exc:
            row.update(status="failed", error=_safe_error(exc))
            failed = True
        finally:
            require(type(client.requests) is int and before <= client.requests <= before + 1
                and client.requests <= MAX_REQUESTS, "FTSHARE_KERNEL_ONE_CALL_PER_SLOT")
            row.update(source_requests=client.requests - before,
                elapsed_seconds=round(max(0, monotonic() - started), 6),
                transport=client.last_error_meta or client.last_response_meta)
            report.update(source_requests=client.requests,
                cumulative_source_requests=PREVIOUS_REQUESTS + client.requests,
                remaining_source_requests=MAX_REQUESTS - client.requests)
            save()
        if failed:
            break
    successful = sum(row["status"] == "qualified-response" for row in report["results"])
    report.update(status="validated" if successful == MAX_REQUESTS and not failed and client.requests == MAX_REQUESTS
        else "partial" if successful else "failed", required_candle_checks=MAX_REQUESTS,
        qualified_candle_checks=successful, completed_at=clock())
    save()
    return 0 if report["status"] == "validated" else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "capture", "release"))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return execute(args.stage, event, os.environ, args.out)
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
