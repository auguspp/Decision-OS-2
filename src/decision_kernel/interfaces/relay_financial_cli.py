"""Batch financial CLI: plan -> capture -> inspect/save. Capture is immutable JSONL.

No automatic source fetch while retrying a save, no fixed number-of-companies quota.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

from decision_kernel.application.relay_financial_batch import capture_batch, save_capture
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, identifier, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.infrastructure.relay_financial import capture_query
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy
from decision_kernel.observations.relay_financial import SPECS, FinancialQuery, build_plan, inspect_capture

MAX_LINE = 16 * 1024 * 1024  # Per record memory guard; files are streamed.


def _decode(raw):
    try:
        def unique(pairs):
            out = {}
            for key, value in pairs:
                if key in out:
                    raise ValueError("duplicate key")
                out[key] = value
            return out
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(isinstance(value, dict), "FINANCIAL_JSON_OBJECT_REQUIRED")
        encoded(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("FINANCIAL_INPUT_JSON_INVALID") from None


def _lines(path):
    try:
        with path.open("rb") as stream:
            while True:
                raw = stream.readline(MAX_LINE + 1)
                if not raw:
                    break
                require(len(raw) <= MAX_LINE and raw.strip(), "FINANCIAL_INPUT_LINE_INVALID")
                yield _decode(raw)
    except OSError:
        raise RecordError("FINANCIAL_INPUT_UNREADABLE") from None


def _new_file(path):
    try:
        return path.open("x", encoding="utf-8")
    except OSError:
        raise RecordError("FINANCIAL_OUTPUT_EXISTS_OR_UNWRITABLE") from None


def main(argv=None, *, family="financial"):
    require(family in ("financial", "participants"), "RELAY_CLI_FAMILY_INVALID")
    participant = family == "participants"
    query_cls, specs, planner, inspector = FinancialQuery, SPECS, build_plan, inspect_capture
    capture_one, save_one, batch = capture_query, save_capture, capture_batch
    if participant:
        from decision_kernel.observations import relay_participants as own
        from functools import partial
        query_cls, specs, planner, inspector = own.ParticipantQuery, own.SPECS, own.build_plan, own.inspect_capture
        capture_one = partial(capture_query, query_type=query_cls, schema=own.CAPTURE_SCHEMA,
                              normalizer=own.normalize_page, failure_details=True, response_filter=own.metadata_envelope)
        save_one = partial(save_capture, inspector=inspector, source_builder=own.source_for_page)
        batch = partial(capture_batch, query_type=query_cls, inspector=inspector)
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.relay_" + ("participant" if participant else "financial") + "_cli")
    commands = parser.add_subparsers(dest="action", required=True)
    plan = commands.add_parser("plan", help="create explicit API/security/period queries; no network")
    plan.add_argument("--codes", nargs="+", required=True)
    plan.add_argument("--dates" if participant else "--periods", dest="periods", nargs="+", required=True)
    plan.add_argument("--apis", nargs="+", choices=tuple(specs), default=list(specs))
    if not participant:
        plan.add_argument("--dimensions", nargs="+", choices=("P", "D", "I"), default=["P"])
        plan.add_argument("--report-types", nargs="+")
    plan.add_argument("--output", type=Path, required=True)
    capture = commands.add_parser("capture", help="explicit finite plan; pages and failures retained")
    capture.add_argument("--plan", type=Path, required=True)
    capture.add_argument("--live", action="store_true", required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--page-size", type=int, default=100)
    capture.add_argument("--max-pages", type=int, default=100, help="per-query safety guard, adjustable; not a fee quota")
    capture.add_argument("--max-attempts", type=int, default=4)
    capture.add_argument("--max-wait-seconds", type=int, default=180)
    capture.add_argument("--run-seconds", type=int, default=900)
    inspect = commands.add_parser("inspect", help="local captured-source summary, not a new source request")
    inspect.add_argument("--capture", type=Path, required=True)
    inspect.add_argument("--include-rows", action="store_true", help="include original values, units and date qualifications")
    save = commands.add_parser("save", help="save exact captures, never re-fetch their source")
    save.add_argument("--capture", type=Path, required=True)
    save.add_argument("--batch-id", required=True)
    save.add_argument("--public-rights-confirmed", action="store_true")
    target = save.add_mutually_exclusive_group(required=True)
    target.add_argument("--local-synthetic-store", type=Path)
    target.add_argument("--binding", type=Path)
    save.add_argument("--receipts", type=Path, required=True, help="new output retaining every published reference")
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            queries = (planner(args.codes, args.periods, args.apis) if participant else
                       planner(args.codes, args.periods, args.apis, dimensions=args.dimensions,
                               report_types=args.report_types or (None,)))
            with _new_file(args.output) as out:
                for query in queries:
                    out.write(encoded(asdict(query)).decode())
            print(json.dumps({"queries": len(queries), "network_requests": 0}))
            return 0
        if args.action == "capture":
            queries = tuple(query_cls.from_dict(q) for q in _lines(args.plan))
            require(queries and len({q.key for q in queries}) == len(queries), "FINANCIAL_PLAN_INVALID")
            require(type(args.max_pages) is int and args.max_pages > 0, "FINANCIAL_PAGE_BOUND_INVALID")
            for query in queries:
                query.parameters(page_size=args.page_size)
            require(type(args.run_seconds) is int and 30 <= args.run_seconds <= 3600, "RELAY_RUN_BOUND_INVALID")
            transport = RetryingRelay(RelayHTTP(), RetryPolicy(args.max_attempts, args.max_wait_seconds),
                                      deadline=time.monotonic() + args.run_seconds)
            transport.validate_credential()
            failed = 0
            with _new_file(args.output) as out:
                loader = lambda q: capture_one(q, transport=transport, page_size=args.page_size, max_pages=args.max_pages)
                for item in batch(queries, loader):
                    out.write(encoded(item).decode()); out.flush()
                    failed += item["status"] != "captured"
            print(json.dumps({"queries": len(queries), "failed_or_not_attempted": failed, "http_attempts": transport.requests}))
            return 2 if failed else 0
        if args.action == "inspect":
            failed, processed = False, 0
            for item in _lines(args.capture):
                processed += 1
                if item.get("capture") is None:
                    print(json.dumps({"status": "no-capture", "query": item.get("query")})); failed = True
                    continue
                view = inspector(item["capture"])
                print(json.dumps(view if args.include_rows else {k: v for k, v in view.items() if k != "pages"}, ensure_ascii=False))
                failed |= not view["request_complete"]
            require(processed > 0, "FINANCIAL_EMPTY_CAPTURE_FILE")
            return 2 if failed else 0
        identifier(args.batch_id)
        require(len(args.batch_id) <= 48, "FINANCIAL_BATCH_ID_TOO_LONG")
        require(args.public_rights_confirmed, "FINANCIAL_PUBLIC_RIGHTS_REQUIRED")
        if args.binding is not None:
            with args.binding.open("rb") as stream:
                raw_binding = stream.read(65537)
            require(len(raw_binding) <= 65536, "FINANCIAL_BINDING_INVALID")
            binding = GitHubBinding.from_dict(_decode(raw_binding))
            require(binding.write_enabled, "REMOTE_READ_ONLY")
            factory = lambda: VersionedStore(GitHubStore(binding, GitHubAPI.from_environment()))
        else:
            factory = lambda: VersionedStore(GitStore(args.local_synthetic_store))
        failed, processed = False, 0
        with _new_file(args.receipts) as out:
            for item in _lines(args.capture):
                processed += 1
                if item.get("capture") is None:
                    result = {"complete": False, "status": "no-capture", "query": item.get("query")}
                else:
                    try:
                        result = save_one(item["capture"], factory, args.batch_id, public_rights_confirmed=True)
                    except RecordError as exc:
                        result = {"complete": False, "status": "rejected-before-save", "error": str(exc)}
                try:
                    out.write(encoded(result).decode()); out.flush()
                except OSError:
                    print(json.dumps({"status": "receipt-output-failed", "last_result": result}), file=sys.stderr)
                    return 2
                failed |= not result["complete"]
                if result["status"] in ("unknown-write", "saved-reference-unverified", "save-failed"):
                    # Retain all known effects and stop this writer until the original intent is reconciled.
                    break
        require(processed > 0, "FINANCIAL_EMPTY_CAPTURE_FILE")
        return 2 if failed else 0
    except RecordError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}), file=sys.stderr)
        return 2
    except (OSError, UnicodeError):
        print('{"status":"interrupted","error":"FINANCIAL_IO_FAILURE_RETAIN_RECEIPTS"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
