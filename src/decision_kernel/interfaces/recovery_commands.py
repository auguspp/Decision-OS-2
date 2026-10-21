"""Explicit offline recovery into the existing reading room; never a failover writer."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import platform
import re
import time
from tempfile import TemporaryDirectory

from decision_kernel.domain.records import RecordError, SHA, digest, encoded, require
from decision_kernel.infrastructure.git_snapshot import MAX_BUNDLE_BYTES, restored_bundle
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.interfaces.research_payload import json_object


def configure(commands):
    parser = commands.add_parser("recover-room", help="offline bundle audit and original reading-room consumption")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True, help="digest from independently verified original backup receipt")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--plan", type=Path, required=True, help="existing reading-room plan, no executable code")
    parser.add_argument("--out", type=Path, required=True, help="new local output directory; never records or a live target")
    parser.add_argument("--market-registry", type=Path,
                        help="trusted local registry for market and Odds readers; use an absolute path outside the checkout")


def _read(path: Path, limit: int) -> bytes:
    require(path.is_file() and not path.is_symlink(), "RECOVERY_REGULAR_INPUT_REQUIRED")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(0 < len(raw) <= limit, "RECOVERY_INPUT_BOUND")
    return raw


def recover_room(args) -> dict:
    from decision_kernel.application.reading_room import plan_input
    from decision_kernel.application.store_audit import audit_store
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.interfaces.reading_commands import perform
    from decision_kernel.interfaces.reading_page import render_page

    binding = GitHubBinding.from_dict(json_object(_read(args.binding, 4096)))
    require(not binding.write_enabled, "SNAPSHOT_READ_ONLY_BINDING_REQUIRED")
    require(isinstance(args.bundle_sha256, str) and re.fullmatch(r"[a-f0-9]{64}", args.bundle_sha256),
            "RECOVERY_BUNDLE_DIGEST_REQUIRED")
    raw = _read(args.bundle, MAX_BUNDLE_BYTES)
    require(digest(raw) == args.bundle_sha256, "RECOVERY_BUNDLE_DIGEST_MISMATCH")
    require(isinstance(args.snapshot, str) and SHA.fullmatch(args.snapshot), "INVALID_SNAPSHOT")
    plan = plan_input(json_object(_read(args.plan, 60 * 1024)))
    registry = None
    if plan.get("market"):
        require(args.market_registry is not None, "RECOVERY_MARKET_REGISTRY_REQUIRED")
    if args.market_registry is not None:
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(args.market_registry)
    try:
        args.out.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        raise RecordError("NEW_OUTPUT_DIRECTORY_REQUIRED") from None
    started = time.monotonic()
    report = {"schema": "store-room-recovery-v1", "status": "started", "snapshot": args.snapshot,
              "repository": binding.repository, "bundle_sha256": args.bundle_sha256,
              "plan_sha256": digest(encoded(plan)), "python": platform.python_version(),
              "started_at": datetime.now(timezone.utc).isoformat(),
              "write": "not-requested", "source_acquisition": False, "notification": "not-sent",
              "failover": "not-performed", "human_acceptance": "not-established",
              "live_authorization": "not-certified-by-offline-snapshot",
              "production_backup_policy": "not-certified"}
    try:
        with TemporaryDirectory(prefix="dos2-recovery-input-") as directory:
            frozen = Path(directory) / "verified.bundle"
            frozen.write_bytes(raw)
            del raw
            with restored_bundle(frozen, binding, args.snapshot) as backend:
                store = VersionedStore(backend)
                audit = audit_store(store, args.snapshot)
                result = perform(store, {"plan": plan}, public=False, market_registry=registry)
                raw_result, page = encoded(result), render_page(result).encode("utf-8")
                report.update(audit=audit, reading_status=result["status"],
                              result_sha256=digest(raw_result), html_sha256=digest(page))
                # Export only after original whole-store audit and final reader checks.
                for name, data in (("room.json", raw_result), ("reading-room.html", page)):
                    with (args.out / name).open("xb") as stream:
                        stream.write(data)
        report["status"] = "completed" if result["status"] == "readable" else "completed-with-gaps"
    except (RecordError, OSError, ValueError, KeyError, TypeError) as exc:
        # Preserve a bounded diagnostic, not arbitrary source/file-system content.
        code = str(exc)
        report.update(status="failed", error=code if isinstance(exc, RecordError) and
                      re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", code) else "RECOVERY_READ_FAILED")
    report.update(finished_at=datetime.now(timezone.utc).isoformat(),
                  elapsed_seconds=round(time.monotonic() - started, 6))
    with (args.out / "recovery-report.json").open("xb") as stream:
        stream.write(encoded(report))
    return report
