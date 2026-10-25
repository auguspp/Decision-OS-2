"""Explicit visibility-bound GitHub storage; native conditional commits, never a sync daemon."""
from __future__ import annotations
from decision_kernel.infrastructure.runtime_performance import count_event, measure_phase, timed

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import (
    BlobRef, Conflict, IntegrityError, MAX_BYTES, MissingObject, RecordError, SHA,
    StoreUnavailable, UnknownWrite, digest, encoded, identifier, require,
)
from decision_kernel.infrastructure.git_ancestry import AncestryUnavailable

API_ROOT = "https://api.github.com"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_TREE_ENTRIES = 32768
MAX_REQUESTS = 512
MAX_ANCESTOR_PROOFS = 128
TREE_RESPONSE_ROW_MARGIN = 128
TREE_RESPONSE_ENVELOPE_MARGIN = 1024
TREE_RESPONSE_WORK_UNIT_RESERVE = 256 * 1024
COMMIT_MUTATION = """mutation StoreCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}"""
_WRITE_PHASES = frozenset({"response", "commit", "binding", "tree", "readback"})
_WRITE_ERROR_CLASSIFICATIONS = {
    "REMOTE_PARENT_MISMATCH": "parent-mismatch",
    "REMOTE_WRITE_SET_CHANGED": "write-set-changed",
    "REMOTE_UNRELATED_CHANGE": "unrelated-change",
    "REMOTE_WRITE_READBACK_MISMATCH": "readback-mismatch",
    "REMOTE_BLOB_INVALID": "blob-integrity",
    "REMOTE_BLOB_ENCODING": "blob-integrity",
    "REMOTE_BLOB_SIZE": "blob-integrity",
    "REMOTE_BLOB_OID": "blob-integrity",
    "BLOB_DIGEST_MISMATCH": "blob-integrity",
    "PRIVATE_BINDING_MISMATCH": "binding-mismatch",
    "PUBLIC_BINDING_MISMATCH": "binding-mismatch",
    "PRIVATE_UPGRADE_REVERSED": "binding-mismatch",
    "STORE_MARKER_MISMATCH": "binding-mismatch",
    "REMOTE_REF_MISMATCH": "binding-mismatch",
    "REMOTE_BRANCH_ID_REQUIRED": "binding-mismatch",
    "COMMIT_OUTSIDE_BINDING": "reachability-failed",
    "REMOTE_COMMIT_MISMATCH": "commit-invalid",
    "REMOTE_TREE_MISSING": "commit-invalid",
    "INCOMPLETE_REMOTE_TREE": "tree-invalid",
    "REMOTE_TREE_LIMIT": "tree-invalid",
    "INVALID_REMOTE_TREE": "tree-invalid",
    "DUPLICATE_REMOTE_PATH": "tree-invalid",
    "REGULAR_REMOTE_BLOB_REQUIRED": "tree-invalid",
    "PATH_OUTSIDE_STORE": "tree-invalid",
    "OBJECT_MISSING": "readback-missing",
    "REMOTE_NOT_AUTHORIZED": "not-authorized",
    "REMOTE_NOT_FOUND_OR_NOT_AUTHORIZED": "not-found-or-not-authorized",
    "REMOTE_RATE_LIMIT": "rate-limited",
    "REMOTE_REQUEST_LIMIT": "request-limit",
    "REMOTE_HTTP_FAILURE": "http-failure",
    "REMOTE_RESPONSE_UNAVAILABLE": "response-unavailable",
    "REMOTE_JSON_INVALID": "response-invalid",
    "REMOTE_RESPONSE_TOO_LARGE": "response-invalid",
    "UNEXPECTED_REMOTE_RESPONSE": "response-invalid",
}
_WRITE_CLASSIFICATIONS = frozenset(_WRITE_ERROR_CLASSIFICATIONS.values()) | {
    "graphql-errors", "response-invalid", "validation-failed",
}


def write_failure_diagnostic(error):
    """Project only adapter-owned post-response enums, never exception prose.

    This describes the failed verification, not whether the write published or
    a provider/network root cause. Old unclassified writes remain unclassified.
    """
    if (not isinstance(error, UnknownWrite) or len(error.args) != 1 or type(error.args[0]) is not str
            or error.args[0] != "REMOTE_WRITE_REQUIRES_RECONCILIATION"):
        return None
    phase = getattr(error, "write_phase", None)
    classification = getattr(error, "write_classification", None)
    if (type(phase) is not str or phase not in _WRITE_PHASES
            or type(classification) is not str or classification not in _WRITE_CLASSIFICATIONS):
        return None
    return {"schema": "github-write-diagnostic-v1", "phase": phase,
            "classification": classification}


def _write_failure_classification(error, phase):
    # Inspect only exact known RecordError codes. Do not call str(error), inspect
    # causes, or parse response messages to invent a transport diagnosis.
    if isinstance(error, RecordError) and len(error.args) == 1 and type(error.args[0]) is str:
        classification = _WRITE_ERROR_CLASSIFICATIONS.get(error.args[0])
        if classification is not None:
            return classification
    return "response-invalid" if phase == "response" else "validation-failed"


def _sha(value) -> str:
    require(isinstance(value, str) and bool(SHA.fullmatch(value)), "INVALID_REMOTE_OID")
    return value


def _json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrityError("REMOTE_JSON_INVALID") from exc


@dataclass(frozen=True)
class GitHubBinding:
    """Trusted run-owner configuration, NOT a source/research payload or credential."""
    repository: str
    repository_id: int
    branch: str
    anchor: str
    store_id: str
    write_enabled: bool = False
    visibility: str = "private"
    private_upgrade_allowed: bool = False  # One-way private upgrade of this exact public-origin store.

    def __post_init__(self):
        require(isinstance(self.repository, str) and bool(re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", self.repository)),
            "INVALID_GITHUB_REPOSITORY")
        require(type(self.repository_id) is int and self.repository_id > 0, "INVALID_REPOSITORY_ID")
        require(isinstance(self.branch, str) and bool(re.fullmatch(
            r"records(?:/[a-z0-9][a-z0-9_-]{0,63})?", self.branch)), "DEDICATED_RECORDS_BRANCH_REQUIRED")
        _sha(self.anchor)
        identifier(self.store_id)
        require(type(self.write_enabled) is bool, "INVALID_WRITE_AUTHORIZATION")
        require(isinstance(self.visibility, str) and self.visibility in ("private", "public"),
                "INVALID_DATA_VISIBILITY")
        require(type(self.private_upgrade_allowed) is bool and
                (not self.private_upgrade_allowed or self.visibility == "public"),
                "INVALID_PRIVATE_UPGRADE_POLICY")

    @classmethod
    def from_dict(cls, value: dict) -> GitHubBinding:
        require(isinstance(value, dict), "INVALID_GITHUB_BINDING")
        try:
            return cls(**value)
        except TypeError as exc:
            raise RecordError("INVALID_GITHUB_BINDING") from exc

    def accepts_privacy(self, actual_private) -> bool:
        """Actual GitHub privacy, not source data or a mutable caller claim."""
        return type(actual_private) is bool and (
            actual_private is (self.visibility == "private")
            or self.private_upgrade_allowed and actual_private is True)

    def marker(self) -> bytes:
        # The anchor records its ORIGINAL visibility, not mutable GitHub privacy.
        # Upgrading that same repository to private MUST NOT rewrite legacy markers.
        # The separate opt-in only affects native live-repository validation.
        return encoded(dict(schema="github-store-binding-v1", project="decision-os-2", mode=self.visibility,
                            repository=self.repository, repository_id=self.repository_id,
                            branch=self.branch, store_id=self.store_id))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def github_tree_inventory(binding, files, *, directories=()):
    """Estimate the complete recursive response using the actual trusted origin.

    SHA lengths do not change with a commit. Include every documented row
    field, a size allowance even for directories, and formatting headroom;
    the transport still enforces its original independent eight-MiB limit.
    """
    require(isinstance(binding, GitHubBinding) and isinstance(files, dict)
            and isinstance(directories, (list, tuple, set, frozenset))
            and all(isinstance(path, str) for path in directories), "INVALID_TREE_INVENTORY")
    folders = set(directories)
    for path in directories:
        parts = path.split("/")
        folders.update("/".join(parts[:number]) for number in range(1, len(parts)))
    for path, size in files.items():
        GitHubStore._path(path)
        require(type(size) is int and 0 <= size <= 2 * MAX_BYTES, "INVALID_TREE_INVENTORY")
        parts = path.split("/")
        folders.update("/".join(parts[:number]) for number in range(1, len(parts)))
    require(not folders.intersection(files), "TREE_PATH_CONFLICT")
    for path in folders:
        BlobRef(binding.repository, "0" * 40, path, "0" * 64)
        require(path in ("source-views", "research", "research/cases", "research/artifacts",
                        "research/watch-activities", "submissions")
                or path.startswith(("source-views/", "research/cases/", "research/artifacts/",
                                    "research/watch-activities/", "submissions/")), "PATH_OUTSIDE_STORE")
    prefix = API_ROOT + "/repos/" + binding.repository + "/git/"
    response = len(encoded({"sha": "0" * 40, "url": prefix + "trees/" + "0" * 40,
                            "tree": [], "truncated": False})) + TREE_RESPONSE_ENVELOPE_MARGIN
    for kind, entries in (("blob", files), ("tree", folders)):
        for path in entries:
            row = {"path": path, "mode": "100644" if kind == "blob" else "040000", "type": kind,
                   "sha": "0" * 40, "size": files[path] if kind == "blob" else 2 * MAX_BYTES,
                   "url": prefix + ("blobs/" if kind == "blob" else "trees/") + "0" * 40}
            response += len(encoded(row)) + TREE_RESPONSE_ROW_MARGIN
    versions = sum((path.startswith("source-views/") and path.endswith("/manifest.json"))
        or (path.startswith("research/cases/") and path.endswith("/record.json"))
        or (path.startswith("research/artifacts/") and path.endswith("/manifest.json")) for path in files)
    return {"tree_entries": len(files) + len(folders), "version_count": versions,
            "tree_response_bytes_estimate": response}


class GitHubRateLimits:
    """Native quota observations shared across finite units, without retries.

    Only the documented numeric quota fields are retained. A new API object's
    local request counter must never manufacture a new GitHub hourly budget.
    """
    def __init__(self):
        self._resources = {}
        self.throttled = False

    def observe(self, headers):
        if headers is None or not callable(getattr(headers, "get", None)):
            return None
        resource = headers.get("x-ratelimit-resource")
        if resource not in {"core", "graphql"}:
            return None
        values = {}
        for name in ("limit", "remaining", "reset"):
            raw = headers.get("x-ratelimit-" + name)
            if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,12}", raw):
                return None
            values[name] = int(raw)
        if not 0 <= values["remaining"] <= values["limit"] or values["limit"] == 0 or values["reset"] == 0:
            return None
        previous = self._resources.get(resource)
        if previous is None or values["reset"] > previous["reset"]:
            self._resources[resource] = values
        elif values["reset"] == previous["reset"]:
            # Out-of-order or cached responses cannot replenish one reset window.
            self._resources[resource] = {**values, "remaining": min(previous["remaining"], values["remaining"])}
        return {"resource": resource, **values}

    def debit(self, resource):
        if resource in self._resources:
            current = self._resources[resource]
            current["remaining"] = max(0, current["remaining"] - 1)

    def admits(self, reserve):
        require(type(reserve) is int and reserve >= 0, "INVALID_REMOTE_QUOTA_RESERVE")
        # The first native REST response is required before source admission.
        return not self.throttled and "core" in self._resources and all(
            values["remaining"] >= reserve for values in self._resources.values())

    def receipt(self):
        return {"observed": "core" in self._resources, "throttled": self.throttled,
                "resources": {key: dict(value) for key, value in sorted(self._resources.items())}}


class GitHubAPI:
    """Bounded, non-retrying transport. Never forwards credentials to another host."""
    def __init__(self, token: str):
        require(isinstance(token, str) and 0 < len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token), "STORE_TOKEN_REQUIRED")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._requests = 0
        self.rate_limits = GitHubRateLimits()

    @classmethod
    def from_environment(cls) -> GitHubAPI:
        # Do not fall back to engineering GH_TOKEN/GITHUB_TOKEN or stored CLI auth.
        return cls(os.environ.get("DECISION_OS_STORE_TOKEN", ""))

    def request(self, method: str, path: str, payload: dict | None = None):
        require(method in ("GET", "POST") and isinstance(path, str)
                and path.startswith("/") and not path.startswith("//")
                and "#" not in path and "\\" not in path
                and all(33 <= ord(c) <= 126 for c in path), "INVALID_API_REQUEST")
        require((method == "GET" and path.startswith("/repos/") and payload is None)
                or (method == "POST" and path == "/graphql" and isinstance(payload, dict)),
                "API_OPERATION_NOT_ALLOWED")
        self._requests += 1
        count_event("github_api_requests")
        if self._requests > MAX_REQUESTS:
            raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
        data = encoded(payload) if payload is not None else None
        require(data is None or len(data) <= MAX_RESPONSE, "REMOTE_REQUEST_TOO_LARGE")
        request = Request(API_ROOT + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10",
        })
        self.rate_limits.debit("graphql" if method == "POST" else "core")
        try:
            with measure_phase("github.post" if method == "POST" else "github.get"):
                count_event("github_post_requests" if method == "POST" else "github_get_requests")
                with self._opener.open(request, timeout=30) as response:
                    if response.status != 200 or response.geturl() != API_ROOT + path:
                        raise IntegrityError("UNEXPECTED_REMOTE_RESPONSE")
                    self.rate_limits.observe(getattr(response, "headers", None))
                    with measure_phase("github.body"):
                        raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise IntegrityError("REMOTE_RESPONSE_TOO_LARGE")
            with measure_phase("github.json"):
                return _json(raw)
        except HTTPError as exc:
            # Never include GitHub's error body, request URL or authorization in a public error.
            quota = self.rate_limits.observe(exc.headers)
            retry_after = exc.headers.get("retry-after") if exc.headers is not None else None
            limited = exc.code == 429 or exc.code == 403 and (
                quota is not None and quota["remaining"] == 0
                or isinstance(retry_after, str) and bool(re.fullmatch(r"[0-9]{1,12}", retry_after)))
            if limited:
                self.rate_limits.throttled = True
                # Preserve the existing ambiguous-write boundary for POST 429.
                if method == "POST" and exc.code == 429:
                    raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED") from None
                raise StoreUnavailable("REMOTE_RATE_LIMIT") from None
            if exc.code in (401, 403):
                raise StoreUnavailable("REMOTE_NOT_AUTHORIZED") from None
            if exc.code == 404:
                raise StoreUnavailable("REMOTE_NOT_FOUND_OR_NOT_AUTHORIZED") from None
            if method == "POST":
                raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED") from None
            raise StoreUnavailable("REMOTE_HTTP_FAILURE") from None
        except (URLError, OSError, TimeoutError, HTTPException, IntegrityError) as exc:
            if method == "POST":
                raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED") from None
            raise StoreUnavailable("REMOTE_RESPONSE_UNAVAILABLE") from None


class GitHubStore:
    """A narrow authenticated adapter. Library checks are not server role isolation."""
    def __init__(self, binding: GitHubBinding, api: GitHubAPI, *, ancestry=None, before_write=None):
        require(isinstance(binding, GitHubBinding), "INVALID_GITHUB_BINDING")
        require(ancestry is None or callable(getattr(ancestry, "is_ancestor", None)),
                "INVALID_LOCAL_ANCESTRY")
        require(before_write is None or callable(before_write), "INVALID_BEFORE_WRITE_GUARD")
        self.binding, self.api = binding, api
        # Trusted runtime composition only, never a record/binding payload.
        # A reader and callers without this hook retain their original behavior.
        self._before_write = before_write
        self.repository = binding.repository
        self._prefix = "/repos/" + self.repository
        self._trees: dict[str, dict] = {}
        self._tree_directories: dict[str, frozenset[str]] = {}
        self._last_tree_directories = (None, frozenset())
        self._blobs: dict[str, bytes] = {}
        self._blob_bytes = 0
        self._ancestor_proofs: set[tuple[str, str]] = set()
        self._ancestry = ancestry  # Off unless the caller supplies a local graph.
        self._ancestry_sources = {"remote_compare": 0, "local_graph": 0, "fallbacks": 0}
        self._branch_node: str | None = None
        self._observed_private_upgrade = False
        self._verified_visibility: str | None = None
        self.head()  # Read/validate an existing binding; never initialize a remote target.

    def ancestry_receipt(self) -> dict:
        """Where this session's ancestry answers came from. Counts only, no ids."""
        return {"schema": "github-store-ancestry-receipt-v1", "local_enabled": self._ancestry is not None,
                **self._ancestry_sources}

    def _get(self, path: str):
        return self.api.request("GET", self._prefix + "/" + path)

    def _repository(self) -> None:
        meta = self.api.request("GET", self._prefix)
        expected_private = self.binding.visibility == "private"
        actual_private = meta.get("private") if isinstance(meta, dict) else None
        opt_in_upgrade = self.binding.private_upgrade_allowed and actual_private is True
        require(isinstance(meta, dict) and type(meta.get("id")) is int
                and meta.get("id") == self.binding.repository_id
                and meta.get("full_name") == self.repository
                and self.binding.accepts_privacy(actual_private)
                and meta.get("archived") is False
                and isinstance(meta.get("default_branch"), str)
                and meta["default_branch"] != self.binding.branch,
                "PRIVATE_BINDING_MISMATCH" if expected_private else "PUBLIC_BINDING_MISMATCH")
        # In a single checked session, privacy reversal must not become a
        # public write after a private source/record was admitted.
        require(not (self._observed_private_upgrade and actual_private is False),
                "PRIVATE_UPGRADE_REVERSED")
        if opt_in_upgrade:
            self._observed_private_upgrade = True
        self._verified_visibility = "private" if actual_private else "public"

    def live_visibility(self) -> str:
        """Recheck native GitHub metadata, never trust source/event visibility."""
        self._repository()
        return self._verified_visibility

    def _ancestor(self, base: str, head: str) -> None:
        _sha(base); _sha(head)
        if base == head:
            return
        proof = (base, head)
        if proof in self._ancestor_proofs:
            return
        if self._ancestry is not None:
            try:
                reachable = self._ancestry.is_ancestor(base, head)
            except AncestryUnavailable:
                # A mirror that stopped early, lost an object or failed mid-command
                # must not decide anything: fall through to the authoritative call.
                self._ancestry_sources["fallbacks"] += 1
            else:
                # Count before refusing: on a denial the receipt is the only place
                # that records which source spoke, and the refusal leaves with it.
                self._ancestry_sources["local_graph"] += 1
                # Same verdict and same failure code as the remote comparison, so a
                # caller cannot tell the two apart by behaviour -- only the receipt
                # records which one answered.
                require(reachable, "COMMIT_OUTSIDE_BINDING")
                if len(self._ancestor_proofs) < MAX_ANCESTOR_PROOFS:
                    self._ancestor_proofs.add(proof)
                return
        value = self._get(f"compare/{base}...{head}?per_page=1")
        require(isinstance(value, dict) and value.get("status") == "ahead"
                and value.get("behind_by") == 0
                and isinstance(value.get("merge_base_commit"), dict)
                and value.get("merge_base_commit", {}).get("sha") == base,
                "COMMIT_OUTSIDE_BINDING")
        self._ancestry_sources["remote_compare"] += 1
        # A successful relation between exact Git commits is immutable. Callers
        # still fetch the live repository/ref before checking branch reachability;
        # a changed head requires a different proof. Errors are never cached.
        if len(self._ancestor_proofs) < MAX_ANCESTOR_PROOFS:
            self._ancestor_proofs.add(proof)

    def _commit(self, commit: str) -> dict:
        value = self._get("git/commits/" + _sha(commit))
        require(isinstance(value, dict) and value.get("sha") == commit, "REMOTE_COMMIT_MISMATCH")
        require(isinstance(value.get("tree"), dict), "REMOTE_TREE_MISSING")
        _sha(value["tree"].get("sha"))
        return value

    @staticmethod
    def _path(path: str) -> None:
        BlobRef("path-check", "0" * 40, path, "0" * 64)
        atom = r"[a-z0-9][a-z0-9_-]{0,95}"
        require(path == "store.json" or bool(re.fullmatch(
            rf"(?:source-views/{atom}/(?:identity\.json|[a-f0-9]{{64}}/(?:manifest\.json|content\.txt|data\.csv))"
            rf"|research/cases/{atom}/(?:current\.json|revisions/{atom}/(?:record\.json|research\.md))"
            rf"|research/artifacts/{atom}/[a-f0-9]{{64}}/(?:manifest\.json|content\.bin)"
            rf"|research/watch-activities/[a-f0-9]{{64}}/current\.json"
            rf"|submissions/{atom}\.json)", path)), "PATH_OUTSIDE_STORE")

    def _tree(self, commit: str) -> dict[str, dict]:
        _sha(commit)
        if commit in self._trees:
            entries = self._trees.pop(commit)
            directories = self._tree_directories.pop(commit)
            self._trees[commit] = entries
            self._tree_directories[commit] = directories
            self._last_tree_directories = (commit, directories)
            return entries
        tree_sha = self._commit(commit)["tree"]["sha"]
        value = self._get("git/trees/" + tree_sha + "?recursive=1")
        require(isinstance(value, dict) and value.get("sha") == tree_sha
                and value.get("truncated") is False and isinstance(value.get("tree"), list),
                "INCOMPLETE_REMOTE_TREE")
        require(len(value["tree"]) <= MAX_TREE_ENTRIES, "REMOTE_TREE_LIMIT")
        paths, entries = set(), {}
        for row in value["tree"]:
            require(isinstance(row, dict) and isinstance(row.get("path"), str), "INVALID_REMOTE_TREE")
            path = row["path"]
            BlobRef(self.repository, commit, path, "0" * 64)
            require(path not in paths, "DUPLICATE_REMOTE_PATH")
            paths.add(path)
            _sha(row.get("sha"))
            if row.get("type") == "tree" and row.get("mode") == "040000":
                require(path in ("source-views", "research", "research/cases", "research/artifacts", "research/watch-activities", "submissions")
                        or path.startswith(("source-views/", "research/cases/", "research/artifacts/", "research/watch-activities/", "submissions/")),
                        "PATH_OUTSIDE_STORE")
                continue
            self._path(path)
            require(row.get("type") == "blob" and row.get("mode") == "100644"
                    and type(row.get("size")) is int and 0 <= row["size"] <= 2 * MAX_BYTES,
                    "REGULAR_REMOTE_BLOB_REQUIRED")
            entries[path] = row
        # Exact immutable object data only; mutable repository/ref checks remain live.
        directories = frozenset(paths - set(entries))
        self._last_tree_directories = (commit, directories)
        if len(self._trees) == 32:
            oldest = next(iter(self._trees))
            del self._trees[oldest]
            del self._tree_directories[oldest]
        self._trees[commit] = entries
        self._tree_directories[commit] = directories
        return entries

    def _read(self, commit: str, path: str, *, tree: dict | None = None) -> bytes:
        self._path(path)
        entries = self._tree(commit) if tree is None else tree
        if path not in entries:
            raise MissingObject("OBJECT_MISSING")
        row = entries[path]
        if row["sha"] in self._blobs:
            return self._blobs[row["sha"]]
        # Trusted local-read composition may answer only after this Store has
        # admitted the exact tree and selected its validated regular blob row.
        # The same wire-shaped validation and immutable cache remain below.
        read_tree_blob = getattr(self.api, "read_tree_blob", None)
        value = (read_tree_blob(self.repository, row) if callable(read_tree_blob)
                 else self._get("git/blobs/" + row["sha"]))
        require(isinstance(value, dict) and value.get("encoding") == "base64"
                and value.get("sha") == row["sha"] and value.get("size") == row["size"]
                and isinstance(value.get("content"), str), "REMOTE_BLOB_INVALID")
        try:
            raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
        except ValueError as exc:
            raise IntegrityError("REMOTE_BLOB_ENCODING") from exc
        require(len(raw) == row["size"], "REMOTE_BLOB_SIZE")
        oid = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        require(oid == row["sha"], "REMOTE_BLOB_OID")
        if len(self._blobs) < 128 and self._blob_bytes + len(raw) <= MAX_RESPONSE:
            self._blobs[row["sha"]] = raw
            self._blob_bytes += len(raw)
        return raw

    def _marker(self, commit: str, *, tree: dict | None = None) -> None:
        require(self._read(commit, "store.json", tree=tree) == self.binding.marker(), "STORE_MARKER_MISMATCH")

    def head(self) -> str:
        self._repository()  # Mutable privacy/ownership never comes from a cached snapshot.
        value = self._get("git/ref/heads/" + quote(self.binding.branch, safe="/"))
        require(isinstance(value, dict) and value.get("ref") == "refs/heads/" + self.binding.branch
                and isinstance(value.get("object"), dict) and value["object"].get("type") == "commit",
                "REMOTE_REF_MISMATCH")
        require(isinstance(value.get("node_id"), str) and bool(re.fullmatch(r"[A-Za-z0-9_+/=-]{1,256}", value["node_id"])),
                "REMOTE_BRANCH_ID_REQUIRED")
        self._branch_node = value["node_id"]
        head = _sha(value["object"].get("sha"))
        self._ancestor(self.binding.anchor, head)
        self._marker(self.binding.anchor)
        if head != self.binding.anchor:
            self._marker(head)
        return head

    def check_commit(self, commit: str) -> None:
        _sha(commit)
        current = self.head()  # Check visibility before reading the requested historic metadata.
        self._ancestor(self.binding.anchor, commit)
        self._ancestor(commit, current)
        self._marker(commit)

    def read(self, commit: str, path: str) -> bytes:
        self.check_commit(commit)
        return self._read(commit, path)

    def paths(self, commit: str, prefix: str = "") -> list[str]:
        self.check_commit(commit)
        if prefix:
            require(isinstance(prefix, str) and prefix in ("source-views/", "research/cases/", "research/artifacts/", "research/watch-activities/", "submissions/"),
                    "PATH_OUTSIDE_STORE")
        return sorted(p for p in self._tree(commit) if p.startswith(prefix))

    @contextmanager
    def source_read_scope(self, snapshot: str):
        """One bounded source scan; validate mutable access before AND after it.

        Only the immutable source bytes/tree are reused. No global checked-head
        flag, new API session, write access, or permission cache is introduced.
        The caller must not publish results before this context exits normally.
        """
        self.check_commit(snapshot)
        reader = _SourceReadScope(self, snapshot)
        try:
            yield reader
            self.check_commit(snapshot)
        finally:
            reader.active = False

    def last_change(self, head: str, path: str) -> str:
        self.check_commit(head)
        self._path(path)
        value = self._get("commits?" + urlencode(dict(sha=head, path=path, per_page=1)))
        require(isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict), "RECEIPT_COMMIT_MISSING")
        original = _sha(value[0].get("sha"))
        self._ancestor(self.binding.anchor, original)
        self._ancestor(original, head)
        require(self._read(original, path) == self._read(head, path), "RECEIPT_CONTENT_CHANGED")
        return original

    def reference(self, commit: str, path: str) -> BlobRef:
        return BlobRef(self.repository, commit, path, digest(self.read(commit, path)))

    def resolve(self, ref: BlobRef) -> bytes:
        require(isinstance(ref, BlobRef) and ref.repository == self.repository, "FOREIGN_REPOSITORY")
        raw = self.read(ref.commit, ref.path)
        if digest(raw) != ref.sha256:
            raise IntegrityError("BLOB_DIGEST_MISMATCH")
        return raw

    @timed("store.cas")
    def commit(self, files: dict[str, bytes], *, expected: str | None) -> str:
        require(self.binding.write_enabled, "REMOTE_READ_ONLY")
        _sha(expected)  # Initialization/branch creation is a separate, explicitly authorized operation.
        require(isinstance(files, dict) and 0 < len(files) <= 128, "INVALID_WRITE_SET")
        require(all(isinstance(raw, bytes) and len(raw) <= 2 * MAX_BYTES for raw in files.values())
                and sum(map(len, files.values())) <= 2 * MAX_BYTES, "INVALID_BLOB")
        for path in files:
            self._path(path)
            require(path != "store.json", "BINDING_WRITE_FORBIDDEN")
        if self.head() != expected:
            raise Conflict("HEAD_CHANGED")
        tree = self._tree(expected)
        # Validate the resulting whole tree before sending any write bytes. A
        # successful commit must not create a head that exceeds the reader's
        # original response bound or the store's independent version bound.
        from decision_kernel.application.versioned_store import MAX_STORE_VERSIONS
        projected = {path: row["size"] for path, row in tree.items()}
        projected.update({path: len(raw) for path, raw in files.items()})
        require(self._last_tree_directories[0] == expected, "TREE_INVENTORY_CHANGED")
        inventory = github_tree_inventory(self.binding, projected, directories=self._last_tree_directories[1])
        require(inventory["tree_entries"] <= MAX_TREE_ENTRIES, "REMOTE_TREE_LIMIT_BEFORE_WRITE")
        require(inventory["version_count"] <= MAX_STORE_VERSIONS, "STORE_VERSION_LIMIT_BEFORE_WRITE")
        require(inventory["tree_response_bytes_estimate"] <= MAX_RESPONSE,
                "REMOTE_TREE_RESPONSE_LIMIT_BEFORE_WRITE")
        for path, raw in files.items():
            if path in tree and not re.fullmatch(r"(?:research/cases/[a-z0-9][a-z0-9_-]{0,95}|research/watch-activities/[a-f0-9]{64})/current\.json", path):
                require(self._read(expected, path, tree=tree) == raw, "IMMUTABLE_RECORD_CONFLICT")
        payload = {"query": COMMIT_MUTATION, "variables": {"input": {
            "branch": {"id": self._branch_node},
            "expectedHeadOid": expected, "message": {"headline": "Store versioned records"},
            "fileChanges": {"additions": [{"path": path, "contents": base64.b64encode(raw).decode("ascii")}
                                            for path, raw in sorted(files.items())]},
        }}}
        self._repository()  # Re-check immediately before sending write bytes.
        # The guard may revoke an admitted run after its reads/preparation. A
        # refusal here is definitely before this POST; keep its original error
        # outside the post-send reconciliation/UnknownWrite handling below.
        if self._before_write is not None:
            self._before_write()
        with measure_phase("store.cas_post"):
            value = self.api.request("POST", "/graphql", payload)
        phase, classification = "response", None
        try:
            with measure_phase("store.cas_verify"):
                if not isinstance(value, dict) or value.get("errors"):
                    # Includes CAS rejection: no automatic resend and no parsing private error prose.
                    classification = "graphql-errors" if isinstance(value, dict) else "response-invalid"
                    raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
                commit = _sha(value["data"]["createCommitOnBranch"]["commit"]["oid"])
                phase = "commit"
                actual = self._commit(commit)
                require(actual.get("parents") == [{"sha": expected}] or
                        isinstance(actual.get("parents"), list) and len(actual["parents"]) == 1
                        and actual["parents"][0].get("sha") == expected, "REMOTE_PARENT_MISMATCH")
                phase = "binding"
                self.check_commit(commit)
                phase = "tree"
                actual_tree = self._tree(commit)
                require(set(actual_tree) == set(tree) | set(files), "REMOTE_WRITE_SET_CHANGED")
                for path, row in tree.items():
                    if path not in files:
                        require(actual_tree[path]["sha"] == row["sha"], "REMOTE_UNRELATED_CHANGE")
                phase = "readback"
                for path, raw in files.items():
                    require(self._read(commit, path, tree=actual_tree) == raw, "REMOTE_WRITE_READBACK_MISMATCH")
                return commit
        except (RecordError, KeyError, TypeError, AttributeError) as exc:
            error = UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
            error.write_phase = phase
            error.write_classification = classification or _write_failure_classification(exc, phase)
            raise error from None


class _SourceReadScope:
    """Private source-only reader; expires on exit and never changes the writer."""
    def __init__(self, owner: GitHubStore, snapshot: str):
        self.owner, self.snapshot, self.repository = owner, snapshot, owner.repository
        self.active = True

    def check_commit(self, commit: str) -> None:
        require(self.active and commit == self.snapshot, "SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED")

    def paths(self, commit: str, prefix: str = "") -> list[str]:
        self.check_commit(commit)
        require(prefix == "source-views/", "SOURCE_SCAN_PATH_OUTSIDE_SCOPE")
        return sorted(p for p in self.owner._tree(commit) if p.startswith(prefix))

    def read(self, commit: str, path: str) -> bytes:
        self.check_commit(commit)
        require(path.startswith("source-views/"), "SOURCE_SCAN_PATH_OUTSIDE_SCOPE")
        return self.owner._read(commit, path)

    def reference(self, commit: str, path: str) -> BlobRef:
        return BlobRef(self.repository, commit, path, digest(self.read(commit, path)))

    def resolve(self, ref: BlobRef) -> bytes:
        require(isinstance(ref, BlobRef) and ref.repository == self.repository, "FOREIGN_REPOSITORY")
        raw = self.read(ref.commit, ref.path)
        require(digest(raw) == ref.sha256, "BLOB_DIGEST_MISMATCH")
        return raw

    def commit(self, files, *, expected):
        raise RecordError("SOURCE_SCAN_READ_ONLY")
