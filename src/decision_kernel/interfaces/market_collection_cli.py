"""Plan, resume, inspect and read an explicitly bound durable SH/SZ collection."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from decision_kernel.application.market_collection import _reason, plan_collection
from decision_kernel.application.market_collection_runtime import REPAIR_TRANSPORT_CODE_SHA, run_collection
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubRateLimits, GitHubStore
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces.store_cli import _load

REGISTRY = Path("docs/sources/relay-registry.json")
MARKET_TOKEN_ENV = "DECISION_OS_MARKET_STORE_TOKEN"
BINDING_ENV = "DECISION_OS_MARKET_BINDING_JSON"  # Reject overrides of canonical records.
RECORDS_BINDING_FILE = Path("config/store-binding.json")


def selection_plan(value, registry):
    require(isinstance(value, dict) and {"collection_id", "generation", "start", "end", "calendar_end"}
            <= set(value) <= {"collection_id", "generation", "start", "end", "calendar_end", "refresh_dates"},
            "ASHARE_SELECTION")
    return plan_collection(**value, registry=registry)


def _parse(raw, limit=6000):
    from decision_kernel.interfaces.market_cli import _object, _constant
    require(isinstance(raw, str) and 0 < len(raw.encode()) <= limit, "ASHARE_RUNTIME_JSON_BOUND")
    value = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    require(isinstance(value, dict), "ASHARE_RUNTIME_JSON_OBJECT")
    return value


def _records_runtime_binding(operation):
    """Use the existing public-origin records contract as the sole run target.

    Write capability is derived only for a separately authorized sync/repair, not
    persisted in the trusted binding or provided by an event/source payload.
    """
    binding = GitHubBinding.from_dict(_load(RECORDS_BINDING_FILE))
    require(binding.repository == "auguspp/Decision-OS-2" and binding.repository_id == 1420041744
            and binding.branch == "records" and binding.visibility == "public"
            and binding.private_upgrade_allowed is True
            and binding.write_enabled is False, "ASHARE_RECORDS_BINDING_MISMATCH")
    return replace(binding, write_enabled=operation in {"sync", "repair"})

 
def _verified_private_upgrade(binding, token):
    """Check actual privacy through the authenticated, immutable-origin store."""
    reader = GitHubStore(replace(binding, write_enabled=False), GitHubAPI(token))
    return reader.live_visibility() == "private"


def _open_remote(binding, token, *, require_private=False, execution_code_sha=None):
    require(not require_private or isinstance(execution_code_sha, str)
            and re.fullmatch(r"[a-f0-9]{40}", execution_code_sha), "ASHARE_REPAIR_EXECUTION_CODE_REQUIRED")
    rate_limits = GitHubRateLimits()
    def open_unit(write):
        require(not write or binding.write_enabled, "REMOTE_READ_ONLY")
        api = GitHubAPI(token)
        # Finite per-unit counters are separate from GitHub's repository quota.
        api.rate_limits = rate_limits
        readonly = replace(binding, write_enabled=False)
        store = VersionedStore(GitHubStore(binding if write else readonly, api))
        if require_private:
            # Every finite repair unit checks actual privacy, including grant
            # continuation after a previous unit opened another API budget.
            require(store.backend.live_visibility() == "private", "ASHARE_REPAIR_PRIVATE_REQUIRED")
            store.backend._ancestor(REPAIR_TRANSPORT_CODE_SHA, execution_code_sha)
        def fresh():
            return VersionedStore(GitMarketReader(readonly, api))
        return store, fresh, api
    return open_unit


def _save_receipt(path, value):
    temporary = path.with_suffix(".pending")
    temporary.write_bytes(encoded(value))
    os.replace(temporary, path)


def run_runtime(event, env, out, *, execute=run_collection):
    """Main-only host for existing records; native privacy governs source rights."""
    require(env.get("GITHUB_REPOSITORY") == "auguspp/Decision-OS-2"
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "ASHARE_MANUAL_MAIN_ONLY")
    inputs = event.get("inputs") if isinstance(event, dict) else None
    require(isinstance(inputs, dict) and set(inputs) <= {
        "operation", "code_sha", "selection", "work_units", "access_and_custody_confirmed",
        "public_rights_confirmed", "repair_request"}, "ASHARE_RUNTIME_INPUT")
    sha = inputs.get("code_sha")
    require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
            and sha == env.get("GITHUB_SHA"), "ASHARE_CODE_SHA_MISMATCH")
    operation = inputs.get("operation")
    require(operation in {"sync", "repair", "status"}, "ASHARE_RUNTIME_OPERATION")
    writing = operation in {"sync", "repair"}
    authorized = (inputs.get("access_and_custody_confirmed") is True
                  or inputs.get("access_and_custody_confirmed") == "true")
    require(not writing or authorized, "ASHARE_SOURCE_AND_CUSTODY_REQUIRED")
    public_rights = (inputs.get("public_rights_confirmed") is True
                     or inputs.get("public_rights_confirmed") == "true")
    units = str(inputs.get("work_units", "1"))
    require(re.fullmatch(r"[1-9][0-9]?", units) and 1 <= int(units) <= 32, "ASHARE_WORK_UNIT_BOUND")
    require(writing or int(units) == 1, "ASHARE_READ_ONE_OPERATION")
    repair_raw = inputs.get("repair_request")
    require(operation == "repair" or repair_raw in (None, ""), "ASHARE_REPAIR_EXPLICIT_OPERATION_REQUIRED")
    repair_request = None if repair_raw in (None, "") else _parse(repair_raw, limit=16000)
    registry = RelayRegistry(REGISTRY)
    plan = selection_plan(_parse(inputs.get("selection")), registry)
    root = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    require(not out.exists() and not out.is_symlink() and out.parent.resolve().is_relative_to(root)
            and out.resolve() != root, "ASHARE_RUNTIME_OUTPUT")
    out.mkdir(parents=False, exist_ok=False)
    receipt_path = out / "result.json"
    context = {"code_sha": sha, "run_id": env.get("GITHUB_RUN_ID"), "run_attempt": env.get("GITHUB_RUN_ATTEMPT")}
    _save_receipt(receipt_path, {**context, "status": "configuration-not-validated", "source_requests": 0,
        "supplier_rows_in_receipt": False, "private_locations_in_receipt": False})
    try:
        require(not env.get(BINDING_ENV), "ASHARE_RUNTIME_BINDING_OVERRIDE_FORBIDDEN")
        binding = _records_runtime_binding(operation)
        require(bool(env.get(MARKET_TOKEN_ENV)), "ASHARE_STORE_TOKEN_REQUIRED")
        # A public sync still requires redistribution permission. Repair's
        # mandatory private check uses each existing unit API below, so native
        # evidence admission cannot hide reads in an extra, uncounted session.
        if operation == "sync" and not public_rights:
            require(_verified_private_upgrade(binding, env[MARKET_TOKEN_ENV]),
                    "ASHARE_PUBLIC_RIGHTS_REQUIRED")
        attempt = "run-" + str(env.get("GITHUB_RUN_ID", "")) + "-" + str(env.get("GITHUB_RUN_ATTEMPT", ""))
        extra = {} if operation != "repair" else {
            "repair_request": repair_request, "execution_code_sha": sha,
        }
        result = execute(operation=operation, plan=plan, registry=registry,
            open_unit=_open_remote(binding, env[MARKET_TOKEN_ENV], require_private=True, execution_code_sha=sha)
                if operation == "repair" else _open_remote(binding, env[MARKET_TOKEN_ENV]), attempt_id=attempt,
            max_work_units=int(units), access_confirmed=authorized, custody_authorized=authorized,
            public_rights_confirmed=public_rights,
            progress=lambda value: _save_receipt(receipt_path, {**context, **value}), **extra)
        _save_receipt(receipt_path, {**context, **result["receipt"]})
        # The exact retained reference stays in the store/controlled reader;
        # source rows and references are excluded from public Actions artifacts.
        return 0 if result["receipt"]["status"] in {"complete", "already-complete", "read", "repair-complete"} else 2
    except Exception as exc:
        saved = _load(receipt_path)
        saved.update(status="blocked", reason_code=_reason(exc))
        _save_receipt(receipt_path, saved)
        return 2
    finally:
        # This is the same allowlisted engineering receipt as the artifact.
        # Native job logs remain diagnosable when an artifact cannot be fetched.
        print(encoded(_load(receipt_path)).decode(), end="")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "sync", "status", "read", "runtime"))
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--binding", type=Path)
    where.add_argument("--path", type=Path, help="already initialized synthetic GitStore only")
    parser.add_argument("--work-units", type=int, default=1)
    parser.add_argument("--attempt-id")
    parser.add_argument("--access-confirmed", action="store_true")
    parser.add_argument("--custody-authorized", action="store_true")
    parser.add_argument("--public-rights-confirmed", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == "runtime":
            require(args.out is not None, "ASHARE_RUNTIME_OUTPUT")
            return run_runtime(_load(Path(os.environ["GITHUB_EVENT_PATH"])), dict(os.environ), args.out)
        if args.out is not None:
            require(not args.out.exists() and not args.out.is_symlink() and args.out.parent.is_dir(),
                    "ASHARE_OUTPUT_ALREADY_EXISTS_OR_PARENT_MISSING")
        require(args.selection is not None, "ASHARE_SELECTION_REQUIRED")
        registry = RelayRegistry(args.registry)
        plan = selection_plan(_load(args.selection), registry)
        if args.operation == "plan":
            result = plan
        else:
            require(args.binding is not None or args.path is not None, "ASHARE_STORE_REQUIRED")
            if args.binding is not None:
                binding = GitHubBinding.from_dict(_load(args.binding))
                open_unit = _open_remote(binding, os.environ.get(MARKET_TOKEN_ENV, ""))
            else:
                def open_unit(write):
                    return VersionedStore(GitStore(args.path)), lambda: VersionedStore(GitStore(args.path)), None
            result = run_collection(operation=args.operation, plan=plan, registry=registry,
                open_unit=open_unit, attempt_id=args.attempt_id or "manual-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
                max_work_units=args.work_units, access_confirmed=args.access_confirmed,
                custody_authorized=args.custody_authorized, public_rights_confirmed=args.public_rights_confirmed)
        if args.out is not None:
            require(not args.out.exists(), "ASHARE_OUTPUT_ALREADY_EXISTS")
            args.out.write_bytes(encoded(result))
        else:
            print(encoded(result).decode(), end="")
        return 0 if args.operation == "plan" or result["receipt"]["status"] in {"complete", "already-complete", "read"} else 2
    except (RecordError, ValueError, TypeError, KeyError, OSError, RecursionError) as exc:
        print(json.dumps({"error": _reason(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
