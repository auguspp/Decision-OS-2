"""Explicit local or visibility-bound GitHub storage under decision-os store."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.interfaces import research_commands
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MAX_BYTES, RecordError, RevisionInput, SourceInput, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore


def configure(commands) -> None:
    parser = commands.add_parser("store", help="versioned storage with an explicit local or GitHub binding")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--path", type=Path, help="dedicated synthetic local bare Git store")
    location.add_argument("--binding", type=Path, help="trusted run-owner GitHub binding with explicit visibility; not source data")
    actions = parser.add_subparsers(dest="store_command", required=True)
    research_commands.configure(actions)
    actions.add_parser("init-synthetic", help="create a NEW synthetic-only store; never a production target")
    for command in ("put-source", "submit-revision"):
        action = actions.add_parser(command)
        action.add_argument("--submission-id", required=True)
        action.add_argument("--input", required=True, type=Path, help="UTF-8 JSON input; synthetic locally, authorized for the bound visibility remotely")
    for command in ("read-source", "read-revision"):
        actions.add_parser(command).add_argument("--ref", required=True, type=Path, help="saved JSON reference")
    actions.add_parser("current").add_argument("--case-id", required=True)
    search = actions.add_parser("search")
    search.add_argument("--query", default="")
    search.add_argument("--entity-id")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--cursor", type=Path)
    resume = actions.add_parser("resume-research", help="read a saved Quick/Full context; never execute research")
    selector = resume.add_mutually_exclusive_group(required=True)
    selector.add_argument("--case-id", help="select current revision once")
    selector.add_argument("--ref", type=Path, help="exact saved research BlobRef JSON")
    selector.add_argument("--cursor", type=Path, help="next_cursor JSON from a previous source page")
    resume.add_argument("--source-limit", type=int, default=4, help="sources per page, 1 to 8")


def _load(path: Path) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RecordError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise RecordError("INPUT_TOO_LARGE")
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(RecordError("INVALID_JSON_NUMBER")))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("INPUT_UNREADABLE_OR_INVALID") from exc
    if not isinstance(value, dict):
        raise RecordError("JSON_OBJECT_REQUIRED")
    return value


def execute(args: argparse.Namespace) -> int:
    try:
        if args.store_command == "init-synthetic":
            if args.binding is not None:
                raise RecordError("REMOTE_INITIALIZATION_NOT_SUPPORTED")
            backend = GitStore.init_synthetic(args.path)
            result = {"repository": backend.repository, "mode": "synthetic", "head": backend.head(),
                      "remote_binding": "not-configured"}
        else:
            if args.binding is None:
                backend = GitStore(args.path)
            else:
                binding = GitHubBinding.from_dict(_load(args.binding))
                if args.store_command in ("put-source", "submit-revision", "checkpoint-research") and not binding.write_enabled:
                    raise RecordError("REMOTE_READ_ONLY")
                backend = GitHubStore(binding, GitHubAPI.from_environment())
            store = VersionedStore(backend)
            if args.store_command in research_commands.COMMANDS:
                result = research_commands.execute(args, store, _load)
            elif args.store_command == "put-source":
                result = asdict(store.put_source(args.submission_id, SourceInput.from_dict(_load(args.input))))
            elif args.store_command == "submit-revision":
                result = asdict(store.submit_revision(args.submission_id, RevisionInput.from_dict(_load(args.input))))
            elif args.store_command == "read-source":
                result = store.read_source(SourceRef.from_dict(_load(args.ref)))
            elif args.store_command == "read-revision":
                result = store.read_revision(BlobRef(**_load(args.ref)))
            elif args.store_command == "resume-research":
                result = ResearchReader(store).resume(
                    args.case_id, reference=BlobRef(**_load(args.ref)) if args.ref else None,
                    cursor=_load(args.cursor) if args.cursor else None, source_limit=args.source_limit)
            elif args.store_command == "current":
                current = store.current(args.case_id)
                result = {"reference": asdict(current) if current else None}
            else:
                result = store.search(args.query, entity_id=args.entity_id, limit=args.limit,
                                      cursor=_load(args.cursor) if args.cursor else None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2 if args.store_command == "audit-numbers" and result["status"] not in ("checked", "conditional") else 0
    except RecordError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (TypeError, KeyError):
        print('{"error":"INVALID_INPUT_SHAPE"}', file=sys.stderr)
        return 2
