"""Persist qualified daily discovery on the existing authorized daily writer.

Only the job token and the source owner's canonical receipt/control are used.
No provider credentials, network source request, new store or notification.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

from decision_kernel.application.daily_discovery import (
    SAVE_STATUSES, STREAM, save_daily_discovery,
)
from decision_kernel.application.daily_market import policy
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.infrastructure.runtime_performance import annotate_performance, diagnosed
from decision_kernel.interfaces.daily_market_cli import POLICY_FILE, _admit, native_ready, settings
from decision_kernel.interfaces.daily_reading_cli import read_update_receipt
from decision_kernel.interfaces.ftshare_check_cli import _regular, _safe_error
from decision_kernel.interfaces.market_collection_cli import REGISTRY, _records_runtime_binding

OUTPUT = "discovery-result.json"


def _reason(exc, token=None):
    code = _safe_error(exc)
    return "DAILY_DISCOVERY_FAILED" if code == "FTSHARE_CHECK_FAILED" or token and token in code else code


def _open_writer(api):
    binding = _records_runtime_binding("sync")
    require(binding.write_enabled is True, "DAILY_DISCOVERY_WRITE_BINDING_REQUIRED")
    store = VersionedStore(GitHubStore(binding, api))
    source = VersionedStore(GitMarketReader(replace(binding, write_enabled=False), api))
    return store, source


@diagnosed
def run_runtime(event, env, out, *, api=None, clock=None):
    approved = policy(_json(_regular(POLICY_FILE, 8192)))
    selected = settings(event, env, approved)
    require(selected["operation"] == "update", "DAILY_DISCOVERY_WRITE_OPERATION_REQUIRED")
    token = env.get("GITHUB_TOKEN")
    require(isinstance(token, str) and bool(token), "DAILY_DISCOVERY_TOKEN_REQUIRED")
    root, out = Path(env["RUNNER_TEMP"]).resolve(strict=True), Path(out)
    require(out.is_dir() and not out.is_symlink() and out.resolve().is_relative_to(root)
            and out.resolve() != root, "DAILY_DISCOVERY_OUTPUT")
    require(not (out / OUTPUT).exists() and not (out / OUTPUT).is_symlink(),
            "DAILY_DISCOVERY_OUTPUT_EXISTS")
    receipt = read_update_receipt(out, selected)
    expected = None
    if receipt["status"] in SAVE_STATUSES:
        raw = _regular(out / "control.json", 16384)
        control = _json(raw)
        require(isinstance(control, dict) and encoded(control) == raw
            and control.get("schema") == "daily-market-reading-control-v1"
            and control.get("market_reference") is not None, "DAILY_DISCOVERY_SOURCE_CONTROL_REQUIRED")
        expected = control["market_reference"]
        SourceRef.from_dict(expected)
    api = GitHubAPI(token) if api is None else api
    native_ready(api, selected)
    at = (clock or (lambda: datetime.now(timezone.utc).isoformat()))()
    try:
        if receipt["status"] in SAVE_STATUSES:
            _admit(api)
        store, source = _open_writer(api)
        result = save_daily_discovery(store, source, RelayRegistry(REGISTRY), approved, receipt,
            as_of=at, submission_id="daily-discovery-" + str(selected["run_id"]) + "-1",
            expected_reference=expected, guard=lambda: native_ready(api, selected))
    except (RecordError, KeyError, TypeError, ValueError) as exc:
        # A write could have completed before readback failed. Do not claim
        # zero writes or publish an older state as a successful replacement.
        result = {"schema": "daily-discovery-runtime-v1", "stream": STREAM, "as_of": at,
            "status": "unavailable", "update_status": receipt["status"], "source_requests": 0,
            "records_writes": None, "reference": None, "persistence": "unconfirmed",
            "reason_code": _reason(exc, token)}
    native_ready(api, selected)
    result.update(selected)
    result["source_receipt_sha256"] = digest(encoded(receipt))
    result = annotate_performance(result)
    raw = encoded(result)
    require(len(raw) <= 1024 * 1024, "DAILY_DISCOVERY_RECEIPT_BOUND")
    with (out / OUTPUT).open("xb") as stream:
        stream.write(raw)
    print("Daily discovery: " + result["status"] + "; source HTTP requests: 0.")
    return 2 if result["status"] == "unavailable" else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ["GITHUB_EVENT_PATH"]), 1024 * 1024))
        return run_runtime(event, os.environ, args.out)
    except Exception as exc:
        print(_reason(exc, os.environ.get("GITHUB_TOKEN")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

