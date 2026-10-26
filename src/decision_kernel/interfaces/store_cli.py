"""Explicit local or visibility-bound GitHub storage under decision-os store."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys

from decision_kernel.interfaces import research_commands, delivery_commands, odds_commands
from decision_kernel.interfaces import decision_preparation_commands
from decision_kernel.interfaces import outcome_review_commands
from decision_kernel.interfaces import close_structure_commands
from decision_kernel.interfaces import auction_commands
from decision_kernel.interfaces import auction_next_session_commands
from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.research_source_use import audit_demonstrated
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MAX_BYTES, RecordError, RevisionInput, SHA, SourceInput, SourceRef, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_tree import LocalTreeUnavailable
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore, MAX_REQUESTS


class _PublicReadAPI(GitHubAPI):
    """One local read session, sharing the original transport and its budgets.

    Nested readers reuse this same API, including readers that instantiate their
    own GitHubStore. A caught native denial cannot turn into a publishable partial
    result after the repository or permission later changes back.
    """
    def __init__(self, api, binding, *, tree_reader=None, blob_reader=None):
        self._delegate, self._repository_path = api, "/repos/" + binding.repository
        self._failure = None
        self._tree_reader = tree_reader
        self._blob_reader = blob_reader
        self._native_trees = set()

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def check_read_session(self):
        """Expose the existing refusal latch without a request or new admission."""
        if self._failure is not None:
            raise self._failure

    def read_tree_blob(self, repository, row):
        """Internal Store hook, only after its complete tree validation.

        The Store selected this row from a checked exact commit/tree/path. An
        arbitrary blob GET never gets local eligibility. Keep the original
        response shape so the same Store still verifies encoding, size and OID.
        """
        if self._failure is not None:
            raise self._failure
        if self._blob_reader is not None and "/repos/" + repository == self._repository_path:
            try:
                return self._blob_reader.read_blob(row["sha"], row["size"])
            except LocalTreeUnavailable:
                # A missing/corrupt local object is not a missing Store object.
                # The existing transport owns the sole fallback and its budget.
                pass
        return self.request("GET", "/repos/" + repository + "/git/blobs/" + row["sha"])

    def request(self, method, path, payload=None):
        if self._failure is not None:
            raise self._failure
        try:
            require(method == "GET", "LOCAL_READ_REFUSES_WRITE_COMMAND")
            tree_prefix, recursive = self._repository_path + "/git/trees/", "?recursive=1"
            if self._tree_reader is not None and payload is None and path.startswith(tree_prefix) and path.endswith(recursive):
                tree_sha = path[len(tree_prefix):-len(recursive)]
                if tree_sha in self._native_trees:
                    try:
                        # Only native commit responses select a local immutable
                        # tree. The shared Store still validates every returned
                        # row, directory, mode, size and path without filtering.
                        return self._tree_reader.read(tree_sha)
                    except LocalTreeUnavailable:
                        # A partial/missing local graph is not another truth.
                        # Use the original single request and its existing budget.
                        pass
            value = self._delegate.request(method, path, payload)
            if path == self._repository_path:
                require(isinstance(value, dict) and value.get("private") is False,
                        "LOCAL_READ_PUBLIC_STORE_ONLY")
            commit_prefix = self._repository_path + "/git/commits/"
            if self._tree_reader is not None and payload is None and path.startswith(commit_prefix):
                commit = path[len(commit_prefix):]
                tree = value.get("tree") if isinstance(value, dict) else None
                tree_sha = tree.get("sha") if isinstance(tree, dict) else None
                if (SHA.fullmatch(commit) and isinstance(value, dict) and value.get("sha") == commit
                        and isinstance(tree_sha, str) and SHA.fullmatch(tree_sha)
                        and len(self._native_trees) < MAX_REQUESTS):
                    self._native_trees.add(tree_sha)
            return value
        except RecordError as error:
            self._failure = error
            raise


def configure(commands) -> None:
    parser = commands.add_parser("store", help="versioned storage with an explicit local or GitHub binding")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--path", type=Path, help="dedicated synthetic local bare Git store")
    location.add_argument("--binding", type=Path, help="trusted run-owner GitHub binding with explicit visibility; not source data")
    actions = parser.add_subparsers(dest="store_command", required=True)
    research_commands.configure(actions)
    delivery_commands.configure(actions)
    odds_commands.configure(actions)
    decision_preparation_commands.configure(actions)
    outcome_review_commands.configure(actions)
    close_structure_commands.configure(actions)
    auction_commands.configure(actions)
    auction_next_session_commands.configure(actions)
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


def execute(args: argparse.Namespace, *, ancestry=None, tree_reader=None, blob_reader=None,
            read_binding: GitHubBinding | None = None) -> int:
    """Execute once; a local reader may supply its already checked public binding.

    This internal argument is not a command-line authority flag. It preserves
    the local console's exact preflight identity and restricts both native reads
    and final exports; ordinary hosted/shared CLI binding semantics are unchanged.
    """
    try:
        if read_binding is not None:
            require(args.binding is not None and isinstance(read_binding, GitHubBinding)
                    and not read_binding.write_enabled and read_binding.visibility == "public"
                    and not read_binding.private_upgrade_allowed, "LOCAL_READ_BINDING_INVALID")
        before_export = None
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
                binding = read_binding if read_binding is not None else GitHubBinding.from_dict(_load(args.binding))
                if args.store_command in ("put-source", "submit-revision", "checkpoint-research", *delivery_commands.WRITE_COMMANDS, *odds_commands.WRITE_COMMANDS, *decision_preparation_commands.WRITES) and not binding.write_enabled:
                    raise RecordError("REMOTE_READ_ONLY")
                api = GitHubAPI.from_environment()
                if read_binding is not None:
                    api = _PublicReadAPI(api, binding, tree_reader=tree_reader, blob_reader=blob_reader)
                backend = GitHubStore(binding, api, ancestry=ancestry)
                if read_binding is not None:
                    def before_export():
                        # Constructor admission already checked native metadata
                        # before records. Recheck the same adapter after reading
                        # and rendering, before each JSON/HTML export.
                        require(backend.live_visibility() == "public", "LOCAL_READ_PUBLIC_STORE_ONLY")
            store = VersionedStore(backend)
            if args.store_command in delivery_commands.COMMANDS + odds_commands.COMMANDS + decision_preparation_commands.COMMANDS:
                def fresh_reader():
                    if args.binding is not None and args.store_command in decision_preparation_commands.COMMANDS:
                        return decision_preparation_commands.private_reader(binding, backend.api)
                    return VersionedStore(GitStore(args.path) if args.binding is None else
                        GitHubStore(replace(binding, write_enabled=False), backend.api, ancestry=ancestry))
                handler = (decision_preparation_commands if args.store_command in decision_preparation_commands.COMMANDS else
                           odds_commands if args.store_command in odds_commands.COMMANDS else delivery_commands)
                if handler is decision_preparation_commands and args.binding is not None:
                    decision_preparation_commands.checked_visibility(backend, args.store_command)
                result = handler.execute(args, store, _load, fresh_reader)
                if handler is decision_preparation_commands and args.binding is not None:
                    decision_preparation_commands.checked_visibility(backend, args.store_command)
            elif args.store_command in research_commands.COMMANDS:
                if read_binding is not None and args.store_command == "research-catalogue":
                    # Catalogue already chooses one exact snapshot. Reuse the
                    # existing expiring reader for this local JSON operation;
                    # entry/exit and final export still check native authority.
                    # Room owns its own scope before HTML generation and must
                    # not be wrapped by an outer scope that would exit too late.
                    with reading_scope(backend, fail_closed=True) as reader:
                        result = research_commands.execute(args, VersionedStore(reader), _load,
                                                           before_export=before_export)
                else:
                    result = research_commands.execute(args, store, _load, before_export=before_export)
            elif args.store_command == outcome_review_commands.COMMAND:
                result = outcome_review_commands.execute(args, store, _load)
            elif args.store_command == close_structure_commands.COMMAND:
                result = close_structure_commands.execute(args, store, _load)
            elif args.store_command == auction_commands.COMMAND:
                result = auction_commands.execute(args, store, _load)
            elif args.store_command == auction_next_session_commands.COMMAND:
                result = auction_next_session_commands.execute(args, store, _load)
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
        output = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if before_export is not None:
            before_export()
        print(output)
        if args.store_command in odds_commands.WRITE_COMMANDS:
            return 0 if result["readback"]["status"] == "verified" else 2
        if args.store_command in decision_preparation_commands.WRITES:
            return 0 if result["readback"]["status"] == "verified" else 2
        if args.store_command == "put-artifact":
            return 0 if result["readback"].get("bytes_verified") is True else 2
        if args.store_command in ("deliver-research", "verify-delivery"):
            status = result.get("delivery_verification", result).get("status")
            return 0 if status == "verified-declared-inventory" else 2
        if args.store_command == "audit-source-use":
            return 0 if audit_demonstrated(result) else 2
        return 2 if args.store_command == "audit-numbers" and result["status"] not in ("checked", "conditional") else 0
    except RecordError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (TypeError, KeyError):
        print('{"error":"INVALID_INPUT_SHAPE"}', file=sys.stderr)
        return 2
