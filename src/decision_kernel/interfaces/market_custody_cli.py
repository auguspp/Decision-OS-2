"""Explicit market source custody and exact readback through the existing store.

Does not collect market data or initialize a remote target. Local --path is only
for an already initialized synthetic GitStore. Remote --binding is run-owner input.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
import re
import subprocess
from pathlib import Path
import sys

from decision_kernel.application.market_custody import retain_market_reading, market_reading_projection
from decision_kernel.application.reading_market import read_market_reference
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.market_cli import MAX_INPUT_BYTES, _object, _constant
from decision_kernel.interfaces.research_payload import decode_payload
from decision_kernel.observations.market_daily import MarketInputError


def _load(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise RecordError("INPUT_TOO_LARGE")
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)


def run_runtime(event, env, out, *, run=subprocess.run):
    """Manual main-only composition root. Uses the existing trusted store binding."""
    if (env.get("GITHUB_REPOSITORY") != "auguspp/Decision-OS-2"
            or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise RecordError("MARKET_CUSTODY_MANUAL_MAIN_ONLY")
    data = event.get("inputs") if isinstance(event, dict) else None
    allowed = {"operation", "code_sha", "payload", "public_rights_confirmed", "offset", "limit", "entity_id"}
    if not isinstance(data, dict) or not set(data) <= allowed:
        raise RecordError("MARKET_CUSTODY_RUNTIME_INPUT")
    sha = data.get("code_sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{40}", sha) or sha != env.get("GITHUB_SHA"):
        raise RecordError("MARKET_CUSTODY_CODE_SHA_MISMATCH")
    operation = data.get("operation")
    rights = data.get("public_rights_confirmed") is True or data.get("public_rights_confirmed") == "true"
    discovery = operation in ("discovery-build", "discovery-save", "discovery-read")
    writing = operation in ("save", "discovery-save")
    if operation not in ("save", "read", "discovery-build", "discovery-save", "discovery-read") or writing and not rights:
        raise RecordError("MARKET_CUSTODY_RUNTIME_AUTHORIZATION")
    raw = data.get("payload")
    if not isinstance(raw, str) or not 0 < len(raw.encode()) <= 60000:
        raise RecordError("MARKET_CUSTODY_RUNTIME_PAYLOAD")
    # Reuse the existing bounded, digest-checked transport. Full THS catalogues
    # must survive the dispatch field without trimming their declared universe.
    payload = decode_payload(raw)
    if not isinstance(payload, dict):
        raise RecordError("MARKET_CUSTODY_RUNTIME_PAYLOAD")
    offset, limit = str(data.get("offset", "0")), str(data.get("limit", "50"))
    if not re.fullmatch(r"[0-9]{1,8}", offset) or not re.fullmatch(r"[0-9]{1,3}", limit) or not 1 <= int(limit) <= 100:
        raise RecordError("PAGE_INVALID")
    entity_id = data.get("entity_id", "")
    if not isinstance(entity_id, str) or len(entity_id) > 128:
        raise RecordError("ENTITY_REQUIRED")
    if (writing or discovery) and (int(offset) != 0 or int(limit) != 50 or entity_id):
        raise RecordError("MARKET_CUSTODY_SAVE_NOT_PAGE_SELECTION")
    root = Path(env["RUNNER_TEMP"]).resolve()
    if out.is_symlink() or not out.resolve().is_relative_to(root) or out.resolve() == root:
        raise RecordError("MARKET_CUSTODY_RUNTIME_OUTPUT")
    if discovery:
        from decision_kernel.application.market_discovery import plan_input
        from decision_kernel.domain.research_artifacts import artifact_reference
        if operation == "discovery-read":
            artifact_reference(payload)
        else:
            payload = plan_input(payload)
    elif operation == "save":
        payload = market_reading_projection(payload, RelayRegistry(Path("docs/sources/relay-registry.json")))
    else:
        SourceRef.from_dict(payload)
    binding = GitHubBinding.from_dict(_load(Path("config/store-binding.json")))
    # A boolean in source data never chooses the target; only this explicit operation does.
    selected = replace(binding, write_enabled=writing)
    out.mkdir(parents=False, exist_ok=False)
    (out / "input.json").write_bytes(encoded(payload))
    (out / "binding.json").write_text(json.dumps(selected.__dict__), encoding="utf-8")
    summary = {"code_sha": sha, "operation": operation, "payload_sha256": digest(raw.encode()),
               "input_sha256": digest(encoded(payload)),
               "status": "not-executed", "source_requests": 0}
    (out / "result.json").write_text(json.dumps(summary), encoding="utf-8")
    command = [sys.executable, "-m", "decision_kernel.interfaces.market_custody_cli", operation,
        "--binding", str(out / "binding.json"), "--registry", "docs/sources/relay-registry.json",
        "--input", str(out / "input.json"), "--offset", offset, "--limit", limit]
    if entity_id:
        command += ["--entity-id", entity_id]
    if operation == "save":
        command += ["--public-rights-confirmed"]
    if discovery:
        command = [sys.executable, "-m", "decision_kernel.interfaces.market_discovery_cli",
            operation.removeprefix("discovery-"), "--binding", str(out / "binding.json"),
            "--registry", "docs/sources/relay-registry.json", "--input", str(out / "input.json")]
        if writing:
            command += ["--submission-id", "market-discovery-" + digest(encoded(payload))[:48],
                        "--public-rights-confirmed"]
    child_env = {k: v for k, v in env.items() if k not in ("TUSHARE_PROXY_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")}
    summary["status"] = "in-progress-or-unknown"
    (out / "result.json").write_text(json.dumps(summary), encoding="utf-8")
    completed = run(command, env=child_env, capture_output=True, text=True, check=False, timeout=600)
    (out / "output.json").write_text(completed.stdout, encoding="utf-8")
    (out / "error.json").write_text(completed.stderr, encoding="utf-8")
    summary.update(status="returned" if completed.returncode == 0 else "failed-or-partial-see-output",
                   exit_code=completed.returncode)
    (out / "result.json").write_text(json.dumps(summary), encoding="utf-8")
    return completed.returncode


def runtime_main(argv):
    parser = argparse.ArgumentParser(description="Explicit market custody Actions host")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return run_runtime(_load(Path(os.environ["GITHUB_EVENT_PATH"])), dict(os.environ), args.out)
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        print('{"error":"MARKET_CUSTODY_RUNTIME_REJECTED_OR_INTERRUPTED"}', file=sys.stderr)
        return 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "runtime":
        return runtime_main(argv[1:])
    if argv == ["release"]:
        try:
            binding = GitHubBinding.from_dict(_load(Path("config/store-binding.json")))
            backend = GitHubStore(replace(binding, write_enabled=False), GitHubAPI.from_environment())
            if backend.live_visibility() != "private":
                raise RecordError("MARKET_CUSTODY_PRIVATE_RELEASE_REQUIRED")
            return 0
        except (RecordError, OSError, ValueError, TypeError, KeyError):
            print('{"error":"MARKET_CUSTODY_PRIVATE_RELEASE_REQUIRED"}', file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("save", "read"))
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--binding", type=Path)
    parser.add_argument("--registry", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path, help="Reading payload for save, exact root SourceRef for read")
    parser.add_argument("--public-rights-confirmed", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--entity-id")
    args = parser.parse_args(argv)
    try:
        if args.operation == "save" and (args.offset != 0 or args.limit != 50 or args.entity_id is not None):
            raise RecordError("MARKET_CUSTODY_SAVE_NOT_PAGE_SELECTION")
        if args.operation == "save" and not args.public_rights_confirmed:
            raise RecordError("MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED")
        payload, registry = _load(args.input), RelayRegistry(args.registry)
        if args.operation == "read":
            SourceRef.from_dict(payload)
        if args.binding is not None:
            binding = GitHubBinding.from_dict(_load(args.binding))
            if args.operation == "save" and not binding.write_enabled:
                raise RecordError("REMOTE_READ_ONLY")
            api = GitHubAPI.from_environment()
            def build(read_only=True):
                return VersionedStore(GitHubStore(replace(binding, write_enabled=False) if read_only else binding,
                                                 api))
        else:
            def build(read_only=True):
                return VersionedStore(GitStore(args.path))
        if args.operation == "save":
            result = retain_market_reading(payload, registry, build(False), fresh_reader=build,
                                            public_rights_confirmed=True)
        else:
            result = read_market_reference(payload, registry, build(), offset=args.offset,
                                           limit=args.limit, entity_id=args.entity_id)
    except (RecordError, MarketInputError) as exc:
        error = str(exc)
    except (ValueError, UnicodeError, OSError, TypeError, KeyError, RecursionError):
        error = "MARKET_CUSTODY_INPUT_OR_STORED_DATA_INVALID"
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2 if result["status"] == "partial-restoration" else 0
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
