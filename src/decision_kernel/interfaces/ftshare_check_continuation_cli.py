"""One fixed continuation within the original six-request FTShare study budget."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import os
from pathlib import Path
import re
import sys
import time
import zipfile

from decision_kernel.application.ftshare_daily import capture_batch
from decision_kernel.application.relay_registry import _json
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI
from decision_kernel.infrastructure.research_receipts import download_archive
from decision_kernel.interfaces.ftshare_check_cli import (
    BASE_REFERENCE, PREFIX, REPO, STUDY, TITLE, WORKFLOW, _regular, _safe_error,
    check_plan, check_private, load_baseline, native_ready, next_request_delay,
)

CONTINUATION_TITLE = "FTShare daily check continuation 20261018"
PREDECESSOR_RUN = 39141799035
PREDECESSOR_CODE = "fd60f1f41e3d311132a1694704d12cda0d605899"
PREDECESSOR_ARTIFACT = 12141618961
PREDECESSOR_SHA256 = "c51d2ea9ddc5167b9de0056716338252fcfd8bad1e9172121c425e5aee59a02f"
PREDECESSOR_BYTES = 34673
TOTAL_BUDGET = 6
PREVIOUS_REQUESTS = 1
MAX_REQUESTS = TOTAL_BUDGET - PREVIOUS_REQUESTS
SCHEMA = "ftshare-daily-check-continuation-v1"


def settings(event, env):
    require(env.get("GITHUB_REPOSITORY") == REPO and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_WORKFLOW_REF") == REPO + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1", "FTSHARE_CONTINUATION_MANUAL_MAIN")
    data = event.get("inputs") if isinstance(event, dict) else None
    require(isinstance(data, dict) and set(data) <= {"operation", "code_sha", "selection", "work_units",
        "repair_request", "access_and_custody_confirmed", "public_rights_confirmed"},
        "FTSHARE_CONTINUATION_EVENT")
    require(data.get("operation") == "ftshare-check-continue"
        and (data.get("access_and_custody_confirmed") is True or data.get("access_and_custody_confirmed") == "true")
        and (data.get("public_rights_confirmed") is False or data.get("public_rights_confirmed") == "false")
        and str(data.get("work_units", "1")) == "1" and not data.get("repair_request"),
        "FTSHARE_CONTINUATION_EXPLICIT_PRIVATE")
    raw = data.get("selection")
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= 256, "FTSHARE_CONTINUATION_FIXED_SELECTION")
    selection = _json(raw.encode())
    require(selection == {"study": STUDY, "predecessor_run": PREDECESSOR_RUN}
        and type(selection["predecessor_run"]) is int, "FTSHARE_CONTINUATION_FIXED_SELECTION")
    sha, run_id = data.get("code_sha"), env.get("GITHUB_RUN_ID")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and sha == env.get("GITHUB_SHA"), "FTSHARE_CONTINUATION_CODE_SHA")
    require(isinstance(run_id, str) and re.fullmatch(r"[1-9][0-9]{0,14}", run_id)
        and int(run_id) != PREDECESSOR_RUN, "FTSHARE_CONTINUATION_RUN_ID")
    return {"study": STUDY, "repository": REPO, "code_sha": sha, "run_id": run_id, "attempt": 1,
        "source_budget": TOTAL_BUDGET, "continuation_budget": MAX_REQUESTS}


def predecessor_identity():
    return {"run_id": PREDECESSOR_RUN, "run_attempt": 1, "code_sha": PREDECESSOR_CODE,
        "artifact_id": PREDECESSOR_ARTIFACT, "artifact_sha256": PREDECESSOR_SHA256,
        "artifact_bytes": PREDECESSOR_BYTES, "source_requests": PREVIOUS_REQUESTS,
        "outcome": "first-raw-pair-http-403-other-five-not-requested"}


def _archive_documents(raw, *, sha256, size):
    """Private parser: the native admission caller supplies the fixed original digest."""
    require(isinstance(raw, bytes) and 0 < len(raw) == size <= 1024 * 1024
        and digest(raw) == sha256, "FTSHARE_PREDECESSOR_ARCHIVE_DIGEST")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            items = archive.infolist()
            require(len(items) == 3 and {item.filename for item in items} == {
                "baseline.json", "request.json", "result.json"}
                and all(not item.is_dir() and not item.flag_bits & 1
                    and item.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    and (item.external_attr >> 16) & 0o170000 != 0o120000 for item in items)
                and sum(item.file_size for item in items) <= 4 * 1024 * 1024,
                "FTSHARE_PREDECESSOR_ARCHIVE_MEMBERS")
            require(archive.testzip() is None, "FTSHARE_PREDECESSOR_ARCHIVE_CRC")
            return {item.filename: _json(archive.read(item)) for item in items}
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError):
        raise RecordError("FTSHARE_PREDECESSOR_ARCHIVE_INVALID") from None


def _predecessor_documents(docs):
    require(isinstance(docs, dict) and set(docs) == {"baseline.json", "request.json", "result.json"},
        "FTSHARE_PREDECESSOR_DOCUMENTS")
    baseline, request, result = (docs[k] for k in ("baseline.json", "request.json", "result.json"))
    require(isinstance(baseline, dict) and baseline.get("reference") == BASE_REFERENCE,
        "FTSHARE_PREDECESSOR_BASELINE")
    expected = {"study": STUDY, "repository": REPO, "code_sha": PREDECESSOR_CODE,
        "run_id": str(PREDECESSOR_RUN), "attempt": 1, "source_budget": TOTAL_BUDGET,
        "plan": check_plan(baseline["symbols"]), "baseline_sha256": digest(encoded(baseline))}
    require(request == expected, "FTSHARE_PREDECESSOR_REQUEST_CHANGED")
    require(isinstance(result, dict) and result.get("schema") == "ftshare-daily-check-v1"
        and all(result.get(k) == expected[k] for k in ("study", "repository", "code_sha", "run_id", "attempt", "source_budget"))
        and result.get("status") == "failed" and type(result.get("source_requests")) is int
        and result["source_requests"] == PREVIOUS_REQUESTS and result.get("records_written") is False
        and result.get("active_source_changed") is False and result.get("raw_release") == "private-artifact-only"
        and result.get("required_candle_checks") == 5 and result.get("qualified_candle_checks") == 0,
        "FTSHARE_PREDECESSOR_SPENT_REQUESTS")
    rows = result.get("results")
    require(isinstance(rows, list) and len(rows) == TOTAL_BUDGET
        and [row.get("id") for row in rows] == [item["id"] for item in expected["plan"]]
        and rows[0].get("status") == "failed" and rows[0].get("error") == "FTSHARE_HTTP_403"
        and rows[0].get("transport", {}).get("http_status") == 403
        and all(row == {"id": spec["id"], "status": "not-requested"}
            for row, spec in zip(rows[1:], expected["plan"][1:])), "FTSHARE_PREDECESSOR_OUTCOME")
    return {"identity": predecessor_identity(), "documents": deepcopy(docs)}


def load_predecessor(api, selected, *, archive_loader=None):
    """Read the original failed run and ZIP, never caller-supplied native assertions."""
    run = api.request("GET", PREFIX + "/actions/runs/" + str(PREDECESSOR_RUN))
    require(type(run.get("id")) is int and run["id"] == PREDECESSOR_RUN
        and type(run.get("run_attempt")) is int and run["run_attempt"] == 1
        and run.get("head_sha") == PREDECESSOR_CODE and run.get("head_branch") == "main"
        and run.get("event") == "workflow_dispatch" and run.get("path") == WORKFLOW
        and run.get("display_title") == TITLE and run.get("status") == "completed"
        and run.get("conclusion") == "failure" and run.get("repository", {}).get("id") == 1420041744,
        "FTSHARE_PREDECESSOR_NATIVE_RUN")
    meta = api.request("GET", PREFIX + "/actions/artifacts/" + str(PREDECESSOR_ARTIFACT))
    producer = meta.get("workflow_run", {})
    require(type(meta.get("id")) is int and meta["id"] == PREDECESSOR_ARTIFACT
        and meta.get("name") == "dos2-ftshare-check-" + str(PREDECESSOR_RUN) + "-1"
        and meta.get("expired") is False and type(meta.get("size_in_bytes")) is int
        and meta["size_in_bytes"] == PREDECESSOR_BYTES
        and meta.get("digest") == "sha256:" + PREDECESSOR_SHA256
        and producer.get("id") == PREDECESSOR_RUN and producer.get("head_sha") == PREDECESSOR_CODE
        and producer.get("repository_id") == 1420041744
        and producer.get("head_repository_id") == 1420041744,
        "FTSHARE_PREDECESSOR_NATIVE_ARTIFACT")
    raw = (archive_loader or download_archive)(REPO, PREDECESSOR_ARTIFACT)
    docs = _archive_documents(raw, sha256=PREDECESSOR_SHA256, size=PREDECESSOR_BYTES)
    prior = _predecessor_documents(docs)
    check_private(api, selected)
    return prior


def _prepared(selected, prior, baseline):
    return {**selected, "predecessor": prior["identity"], "plan": check_plan(baseline["symbols"])[:5],
        "baseline_sha256": digest(encoded(baseline)),
        "predecessor_result_sha256": digest(encoded(prior["documents"]["result.json"])),
        "predecessor_request_sha256": digest(encoded(prior["documents"]["request.json"]))}


def execute(stage, event, env, out, *, api=None, client_factory=None, baseline_loader=load_baseline,
            predecessor_loader=load_predecessor, monotonic=time.monotonic, sleep=time.sleep, clock=None):
    selected = settings(event, env)
    require(stage in {"prepare", "capture", "release"}, "FTSHARE_CONTINUATION_STAGE")
    root, out = Path(env["RUNNER_TEMP"]).resolve(), Path(out)
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
        "FTSHARE_CONTINUATION_OUTPUT_PATH")
    api = GitHubAPI.from_environment() if api is None else api
    proof = native_ready(api, selected, title=CONTINUATION_TITLE)
    prior = predecessor_loader(api, selected)
    require(prior["identity"] == predecessor_identity(), "FTSHARE_CONTINUATION_PREDECESSOR_CHANGED")
    prior = _predecessor_documents(prior["documents"])
    if stage == "prepare":
        baseline = baseline_loader(api)
        require(encoded(baseline) == encoded(prior["documents"]["baseline.json"]),
            "FTSHARE_CONTINUATION_BASELINE_CHANGED")
        check_private(api, selected)
        out.mkdir(parents=False, exist_ok=False)
        (out / "baseline.json").write_bytes(encoded(baseline))
        (out / "predecessor.json").write_bytes(encoded(prior))
        (out / "request.json").write_bytes(encoded(_prepared(selected, prior, baseline)))
        (out / "result.json").write_bytes(encoded({"status": "prepared-no-source-requests", "proof": proof,
            "source_requests": 0, "prior_source_requests": PREVIOUS_REQUESTS,
            "cumulative_source_requests": PREVIOUS_REQUESTS, "source_budget": TOTAL_BUDGET,
            "continuation_budget": MAX_REQUESTS}))
        return 0
    baseline = _json(_regular(out / "baseline.json", 2 * 1024 * 1024))
    require(encoded(baseline) == encoded(prior["documents"]["baseline.json"])
        and _json(_regular(out / "predecessor.json", 4 * 1024 * 1024)) == prior,
        "FTSHARE_CONTINUATION_PREPARED_PREDECESSOR_CHANGED")
    request = _json(_regular(out / "request.json", 65536))
    require(request == _prepared(selected, prior, baseline), "FTSHARE_CONTINUATION_PREPARED_REQUEST_CHANGED")
    previous = _json(_regular(out / "result.json", 262144))
    if stage == "release":
        require(previous.get("schema") == SCHEMA and previous.get("status") in {"validated", "partial", "failed"}
            and previous.get("raw_release") == "not-released" and previous.get("predecessor") == prior["identity"]
            and type(previous.get("source_requests")) is int and 0 <= previous["source_requests"] <= MAX_REQUESTS
            and previous.get("cumulative_source_requests") == PREVIOUS_REQUESTS + previous["source_requests"],
            "FTSHARE_CONTINUATION_RELEASE_STATE")
        check_private(api, selected)
        previous["raw_release"] = "private-artifact-only"
        (out / "result.json").write_bytes(encoded(previous))
        print(encoded({k: v for k, v in previous.items() if k != "results"}).decode(), end="")
        return 0
    require(previous.get("status") == "prepared-no-source-requests" and previous.get("source_requests") == 0
        and previous.get("prior_source_requests") == PREVIOUS_REQUESTS
        and previous.get("cumulative_source_requests") == PREVIOUS_REQUESTS,
        "FTSHARE_CONTINUATION_ALREADY_CONSUMED")
    # Every --out in this native run shares one admission marker. A copied or
    # newly prepared directory cannot renew the remaining source allowance.
    marker = root / (".ftshare-continuation-" + selected["run_id"] + "-1.used")
    try:
        with marker.open("xb") as stream:
            stream.write(encoded({"study": STUDY, "run_id": selected["run_id"], "attempt": 1,
                "predecessor_run": PREDECESSOR_RUN, "continuation_budget": MAX_REQUESTS}))
    except FileExistsError:
        raise RecordError("FTSHARE_CONTINUATION_ALREADY_STARTED") from None
    except OSError:
        raise RecordError("FTSHARE_CONTINUATION_MARKER_UNAVAILABLE") from None
    from decision_kernel.infrastructure.ftshare_http import FTShareHTTP
    client = (client_factory or FTShareHTTP)()
    client.validate_credential()
    require(type(client.requests) is int and client.requests == 0, "FTSHARE_FRESH_CLIENT_REQUIRED")
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    report = {**selected, "schema": SCHEMA, "predecessor": prior["identity"], "proof": proof,
        "status": "started", "source_requests": 0, "reserved_requests": 0,
        "prior_source_requests": PREVIOUS_REQUESTS, "cumulative_source_requests": PREVIOUS_REQUESTS,
        "remaining_source_requests": MAX_REQUESTS, "records_written": False, "active_source_changed": False,
        "raw_release": "not-released", "started_at": clock(), "results": [
            {"id": item["id"], "status": "not-requested"} for item in request["plan"]],
        "factor_query_status": "cancelled-within-original-total-budget", "factor_qualification_granted": False,
        "factor_semantics": "not-established", "account_daily_limit_verified": False,
        "long_term_uptime_certified": False, "header_alignment_proves_403_cause": False}
    def save():
        (out / "result.json").write_bytes(encoded(report))
    save()
    normalized, failed = {}, False
    for spec, row in zip(request["plan"], report["results"]):
        try:
            check_private(api, selected)
            if report["source_requests"]:
                sleep(next_request_delay(client))
                check_private(api, selected)
        except Exception as exc:
            report["stop_reason"] = _safe_error(exc)
            failed = True
            break
        before, started = client.requests, monotonic()
        report["reserved_requests"] += 1
        require(report["reserved_requests"] <= MAX_REQUESTS, "FTSHARE_CONTINUATION_SOURCE_BUDGET")
        row.update(status="request-started", requested_at=clock())
        save()
        try:
            client.candlesticks_batch(spec["symbols"], since=spec["since"], until=spec["until"],
                adjust_kind=spec["adjust_kind"])
            raw = client.last_response_bytes
            require(isinstance(raw, bytes) and 0 < len(raw) <= 2 * 1024 * 1024, "FTSHARE_ORIGINAL_BODY_REQUIRED")
            check_private(api, selected)
            name = spec["id"] + ".raw.json"
            (out / name).write_bytes(raw)
            row.update(raw_file=name, raw_bytes=len(raw), raw_sha256=digest(raw))
            checked = capture_batch(raw, symbols=spec["symbols"], since=spec["since"], until=spec["until"],
                adjust_kind=spec["adjust_kind"], acquired_at=clock())["normalized"]
            require(checked["counts"]["quarantined_rows"] == 0, "FTSHARE_CHECK_QUARANTINED_ROWS")
            require(checked["counts"]["observed_rows"] > 0, "FTSHARE_CHECK_EMPTY_RESPONSE")
            if spec["id"] in {"raw-pair", "raw-pair-repeat"}:
                require(all(len(checked["series"][code]) == 1 for code in spec["symbols"]), "FTSHARE_CHECK_PAIR_MISSING")
            if spec["id"] == "raw-pair-repeat":
                row["repeat_equal"] = checked["series"] == normalized["raw-pair"]["series"]
                require(row["repeat_equal"], "FTSHARE_REPEAT_CHANGED")
            normalized[spec["id"]] = checked
            row.update(status="qualified-response", counts=checked["counts"],
                empty_symbol_count=len(checked["missing"]["empty_symbols"]))
        except Exception as exc:
            row.update(status="failed", error=_safe_error(exc))
            failed = True
        finally:
            require(type(client.requests) is int and before <= client.requests <= before + 1
                and client.requests <= MAX_REQUESTS, "FTSHARE_CONTINUATION_ONE_CALL_PER_SLOT")
            row.update(elapsed_seconds=round(max(0, monotonic() - started), 6),
                source_requests=client.requests - before,
                transport=client.last_error_meta or client.last_response_meta)
            report.update(source_requests=client.requests,
                cumulative_source_requests=PREVIOUS_REQUESTS + client.requests,
                remaining_source_requests=MAX_REQUESTS - client.requests)
            save()
        if failed:
            break
    successful = sum(row["status"] == "qualified-response" for row in report["results"])
    report.update(status="validated" if successful == 5 and not failed and client.requests == 5 else
        "partial" if successful else "failed", required_candle_checks=5, qualified_candle_checks=successful,
        completed_at=clock())
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
