"""Local read-only console: the same reads, on the reader's machine, with no runner.

Reads do not need a GitHub runner. They need the exact store, a token that cannot
write, and a receipt that says which store and which command produced the output.
This entry reuses ``decision-os store`` argument parsing and execution, so there is
one implementation of every read; what it adds is a hard gate in front of it.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.git_ancestry import SCHEMA as ANCESTRY_SCHEMA, LocalGitAncestry
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.interfaces import (
    auction_commands, auction_next_session_commands, close_structure_commands, delivery_commands,
    odds_commands, outcome_review_commands, research_commands, store_cli,
)

READ_TOKEN = "DECISION_OS_READ_TOKEN"
STORE_TOKEN = "DECISION_OS_STORE_TOKEN"
MIRROR = "DECISION_OS_GIT_MIRROR"
SCHEMA = "local-read-receipt-v1"

# Commands a workbench may run on a laptop. Writes and reconciliations are absent on
# purpose: a reconciliation can replay an existing intent, so it is not a read.
READ_ONLY = (
    "research-catalogue", "prepare-research", "audit-numbers", "audit-source-use",
    "verify-reference-fixing", "resume-research", "current", "search",
    "read-source", "read-revision", "research-room",
    "research-agenda", "read-research-agenda", "research-outcome-review",
    "read-close-structure", "read-opening-auction", "read-auction-next-session",
    "verify-delivery", "open-delivery", "read-artifact",
) + odds_commands.READ_COMMANDS

# Private decision preparation is excluded even though its two operations only read.
# Its content is private custody and must keep the checked private adapter; a laptop
# copy would move that content outside the boundary that verifies it.
PRIVATE_ONLY = ("prepare-decision", "read-decision-preparation")


def _binding(path: Path) -> GitHubBinding:
    # Keep the shared parser's duplicate-key and size checks when carrying this
    # already checked object into execution instead of reopening the file.
    return GitHubBinding.from_dict(store_cli._load(path))


def check(operation: str, binding_path: Path | None, environ) -> GitHubBinding:
    """Refuse before any API call; every refusal names its own reason."""
    require(operation in READ_ONLY + PRIVATE_ONLY, "LOCAL_READ_REFUSES_WRITE_COMMAND")
    require(operation not in PRIVATE_ONLY, "LOCAL_READ_PRIVATE_CUSTODY_REQUIRES_GITHUB")
    require(binding_path is not None, "LOCAL_READ_REQUIRES_A_BINDING")
    require(bool(environ.get(READ_TOKEN)), "LOCAL_READ_TOKEN_REQUIRED")
    require(not environ.get(STORE_TOKEN), "LOCAL_READ_REFUSES_STORE_TOKEN")
    binding = _binding(Path(binding_path))
    require(not binding.write_enabled, "LOCAL_READ_REQUIRES_READ_ONLY_BINDING")
    require(binding.visibility == "public", "LOCAL_READ_PUBLIC_STORE_ONLY")
    # The shared binding may permit a hosted private upgrade. This console's
    # public-only boundary does not: keep the exact identity/marker and tighten
    # only this execution's native repository check.
    return replace(binding, private_upgrade_allowed=False)


def _split_mirror(argv: list) -> tuple[list, str | None]:
    """Remove the mirror flag before the reused store parser claims the rest.

    The store parser owns the command surface, so a flag it does not know would
    be rejected. Pulling one flag out here keeps that surface unchanged.
    """
    rest, mirror, index = [], None, 0
    while index < len(argv):
        item = argv[index]
        if item == "--git-mirror":
            require(index + 1 < len(argv), "LOCAL_READ_GIT_MIRROR_REQUIRES_A_PATH")
            mirror, index = argv[index + 1], index + 2
            continue
        if item.startswith("--git-mirror="):
            mirror, index = item.split("=", 1)[1], index + 1
            continue
        rest.append(item)
        index += 1
    return rest, mirror


def mirror(flag: str | None, environ) -> LocalGitAncestry | None:
    """An optional local object graph, used only to answer ancestry questions.

    Absent is the normal state and changes nothing. Present is an accelerator: the
    graph never decides reachability on its own, and a question it cannot answer
    falls back to the remote comparison inside the store.
    """
    path = flag or environ.get(MIRROR) or None
    if not path:
        return None
    ancestry = LocalGitAncestry(path)
    require(ancestry.available(), "LOCAL_READ_GIT_MIRROR_NOT_A_REPOSITORY")
    return ancestry


def receipt(operation: str, binding: GitHubBinding, *, started: str, finished: str,
            duration_ms: int, code: int, ancestry=None) -> dict:
    return {"schema": SCHEMA, "operation": operation, "runner": "local",
            "store": {"repository": binding.repository, "branch": binding.branch,
                      "anchor": binding.anchor, "store_id": binding.store_id,
                      "visibility": binding.visibility},
            "identity": {"token": "read-only-required", "write": "not-requested",
                         "authority": "local-read-only-console"},
            "ancestry": {**(ancestry.stats() if ancestry is not None else
                           {"schema": ANCESTRY_SCHEMA, "answered": 0, "declined": 0}),
                         "mirror": "local-git-graph" if ancestry is not None else "absent"},
            "started_at": started, "finished_at": finished,
            "duration_ms": duration_ms, "exit_code": code}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="decision-os local-read",
        description="Read the exact records snapshot locally; no runner, no write path")
    commands = parser.add_subparsers(dest="local_command", required=True)
    store_cli.configure(commands)
    return parser


def main(argv: list | None = None) -> int:
    parser = _parser()
    try:
        rest, flag = _split_mirror(list(sys.argv[1:] if argv is None else argv))
    except RecordError:
        print('{"error":"LOCAL_READ_GIT_MIRROR_REQUIRES_A_PATH"}', file=sys.stderr)
        return 2
    # The single "store" level is reused verbatim so every read keeps its own
    # arguments; the caller passes them without naming that level.
    args = parser.parse_args(["store", *rest])
    started_at = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    code = 2
    ancestry = None
    try:
        binding = check(args.store_command, args.binding, os.environ)
        ancestry = mirror(flag, os.environ)
    except RecordError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        print('{"error":"LOCAL_READ_BINDING_UNREADABLE"}', file=sys.stderr)
        return 2
    # GitHubAPI reads this name and nothing else, so the read token is the only
    # credential in the process for the duration of the command.
    os.environ[STORE_TOKEN] = os.environ[READ_TOKEN]
    try:
        code = store_cli.execute(args, ancestry=ancestry, read_binding=binding)
    finally:
        os.environ.pop(STORE_TOKEN, None)
        finished_at = datetime.now(timezone.utc).isoformat()
        record = receipt(args.store_command, binding, started=started_at, finished=finished_at,
                         duration_ms=int((time.monotonic() - clock) * 1000), code=code,
                         ancestry=ancestry)
        print(json.dumps({"local_read_receipt": record}, sort_keys=True), file=sys.stderr)
        target = os.environ.get("DECISION_OS_READ_RECEIPT")
        if target:
            Path(target).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
