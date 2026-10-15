"""Explicit local synthetic storage commands, composed under decision-os store."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MAX_BYTES, RecordError, RevisionInput, SourceInput, SourceRef
from decision_kernel.infrastructure.git_store import GitStore


def configure(commands) -> None:
    parser = commands.add_parser("store", help="versioned synthetic local storage; not a private runtime binding")
    parser.add_argument("--path", required=True, type=Path, help="dedicated local bare Git store")
    actions = parser.add_subparsers(dest="store_command", required=True)
    actions.add_parser("init-synthetic", help="create a NEW synthetic-only store; never a production target")
    for command in ("put-source", "submit-revision"):
        action = actions.add_parser(command)
        action.add_argument("--submission-id", required=True)
        action.add_argument("--input", required=True, type=Path, help="UTF-8 JSON input, containing only synthetic material")
    for command in ("read-source", "read-revision"):
        actions.add_parser(command).add_argument("--ref", required=True, type=Path, help="saved JSON reference")
    actions.add_parser("current").add_argument("--case-id", required=True)
    search = actions.add_parser("search")
    search.add_argument("--query", default="")
    search.add_argument("--entity-id")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--cursor", type=Path)


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
            backend = GitStore.init_synthetic(args.path)
            result = {"repository": backend.repository, "mode": "synthetic", "head": backend.head(),
                      "remote_binding": "not-configured"}
        else:
            backend = GitStore(args.path)
            store = VersionedStore(backend)
            if args.store_command == "put-source":
                result = asdict(store.put_source(args.submission_id, SourceInput.from_dict(_load(args.input))))
            elif args.store_command == "submit-revision":
                result = asdict(store.submit_revision(args.submission_id, RevisionInput.from_dict(_load(args.input))))
            elif args.store_command == "read-source":
                result = store.read_source(SourceRef.from_dict(_load(args.ref)))
            elif args.store_command == "read-revision":
                result = store.read_revision(BlobRef(**_load(args.ref)))
            elif args.store_command == "current":
                current = store.current(args.case_id)
                result = {"reference": asdict(current) if current else None}
            else:
                result = store.search(args.query, entity_id=args.entity_id, limit=args.limit,
                                      cursor=_load(args.cursor) if args.cursor else None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except RecordError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (TypeError, KeyError):
        print('{"error":"INVALID_INPUT_SHAPE"}', file=sys.stderr)
        return 2
