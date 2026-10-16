"""Explicit visibility-bound GitHub storage; native conditional commits, never a sync daemon."""
from __future__ import annotations

import base64
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

API_ROOT = "https://api.github.com"
MAX_RESPONSE = 8 * 1024 * 1024
MAX_TREE_ENTRIES = 8192
MAX_REQUESTS = 512
COMMIT_MUTATION = """mutation StoreCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid } }
}"""


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

    @classmethod
    def from_dict(cls, value: dict) -> GitHubBinding:
        require(isinstance(value, dict), "INVALID_GITHUB_BINDING")
        try:
            return cls(**value)
        except TypeError as exc:
            raise RecordError("INVALID_GITHUB_BINDING") from exc

    def marker(self) -> bytes:
        # The anchor is a separate trusted commit, avoiding a self-referential hash.
        return encoded(dict(schema="github-store-binding-v1", project="decision-os-2", mode=self.visibility,
                            repository=self.repository, repository_id=self.repository_id,
                            branch=self.branch, store_id=self.store_id))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubAPI:
    """Bounded, non-retrying transport. Never forwards credentials to another host."""
    def __init__(self, token: str):
        require(isinstance(token, str) and 0 < len(token) <= 4096
                and all(33 <= ord(c) <= 126 for c in token), "STORE_TOKEN_REQUIRED")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._requests = 0

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
        if self._requests > MAX_REQUESTS:
            raise StoreUnavailable("REMOTE_REQUEST_LIMIT")
        data = encoded(payload) if payload is not None else None
        require(data is None or len(data) <= MAX_RESPONSE, "REMOTE_REQUEST_TOO_LARGE")
        request = Request(API_ROOT + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token, "Accept": "application/vnd.github+json",
            "Content-Type": "application/json", "X-GitHub-Api-Version": "2026-03-10",
        })
        try:
            with self._opener.open(request, timeout=30) as response:
                if response.status != 200 or response.geturl() != API_ROOT + path:
                    raise IntegrityError("UNEXPECTED_REMOTE_RESPONSE")
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE:
                    raise IntegrityError("REMOTE_RESPONSE_TOO_LARGE")
            return _json(raw)
        except HTTPError as exc:
            # Never include GitHub's error body, request URL or authorization in a public error.
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
    def __init__(self, binding: GitHubBinding, api: GitHubAPI):
        require(isinstance(binding, GitHubBinding), "INVALID_GITHUB_BINDING")
        self.binding, self.api = binding, api
        self.repository = binding.repository
        self._prefix = "/repos/" + self.repository
        self._trees: dict[str, dict] = {}
        self._blobs: dict[str, bytes] = {}
        self._blob_bytes = 0
        self._branch_node: str | None = None
        self.head()  # Read/validate an existing binding; never initialize a remote target.

    def _get(self, path: str):
        return self.api.request("GET", self._prefix + "/" + path)

    def _repository(self) -> None:
        meta = self.api.request("GET", self._prefix)
        require(isinstance(meta, dict) and meta.get("id") == self.binding.repository_id
                and type(meta.get("id")) is int and meta.get("full_name") == self.repository
                and meta.get("private") is (self.binding.visibility == "private")
                and meta.get("archived") is False
                and isinstance(meta.get("default_branch"), str)
                and meta["default_branch"] != self.binding.branch,
                "PRIVATE_BINDING_MISMATCH" if self.binding.visibility == "private" else "PUBLIC_BINDING_MISMATCH")

    def _ancestor(self, base: str, head: str) -> None:
        _sha(base); _sha(head)
        if base == head:
            return
        value = self._get(f"compare/{base}...{head}?per_page=1")
        require(isinstance(value, dict) and value.get("status") == "ahead"
                and value.get("behind_by") == 0
                and isinstance(value.get("merge_base_commit"), dict)
                and value.get("merge_base_commit", {}).get("sha") == base,
                "COMMIT_OUTSIDE_BINDING")

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
            rf"|submissions/{atom}\.json)", path)), "PATH_OUTSIDE_STORE")

    def _tree(self, commit: str) -> dict[str, dict]:
        _sha(commit)
        if commit in self._trees:
            return self._trees[commit]
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
                require(path in ("source-views", "research", "research/cases", "research/artifacts", "submissions")
                        or path.startswith(("source-views/", "research/cases/", "research/artifacts/", "submissions/")),
                        "PATH_OUTSIDE_STORE")
                continue
            self._path(path)
            require(row.get("type") == "blob" and row.get("mode") == "100644"
                    and type(row.get("size")) is int and 0 <= row["size"] <= 2 * MAX_BYTES,
                    "REGULAR_REMOTE_BLOB_REQUIRED")
            entries[path] = row
        # Exact immutable object data only; privacy, branch head and reachability are never cached.
        if len(self._trees) < 32:
            self._trees[commit] = entries
        return entries

    def _read(self, commit: str, path: str, *, tree: dict | None = None) -> bytes:
        self._path(path)
        entries = self._tree(commit) if tree is None else tree
        if path not in entries:
            raise MissingObject("OBJECT_MISSING")
        row = entries[path]
        if row["sha"] in self._blobs:
            return self._blobs[row["sha"]]
        value = self._get("git/blobs/" + row["sha"])
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
            require(isinstance(prefix, str) and prefix in ("source-views/", "research/cases/", "research/artifacts/", "submissions/"),
                    "PATH_OUTSIDE_STORE")
        return sorted(p for p in self._tree(commit) if p.startswith(prefix))

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
        for path, raw in files.items():
            if path in tree and not re.fullmatch(r"research/cases/[a-z0-9][a-z0-9_-]{0,95}/current\.json", path):
                require(self._read(expected, path, tree=tree) == raw, "IMMUTABLE_RECORD_CONFLICT")
        payload = {"query": COMMIT_MUTATION, "variables": {"input": {
            "branch": {"id": self._branch_node},
            "expectedHeadOid": expected, "message": {"headline": "Store versioned records"},
            "fileChanges": {"additions": [{"path": path, "contents": base64.b64encode(raw).decode("ascii")}
                                            for path, raw in sorted(files.items())]},
        }}}
        self._repository()  # Re-check immediately before sending write bytes.
        value = self.api.request("POST", "/graphql", payload)
        try:
            if not isinstance(value, dict) or value.get("errors"):
                # Includes CAS rejection: no automatic resend and no parsing private error prose.
                raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION")
            commit = _sha(value["data"]["createCommitOnBranch"]["commit"]["oid"])
            actual = self._commit(commit)
            require(actual.get("parents") == [{"sha": expected}] or
                    isinstance(actual.get("parents"), list) and len(actual["parents"]) == 1
                    and actual["parents"][0].get("sha") == expected, "REMOTE_PARENT_MISMATCH")
            self.check_commit(commit)
            actual_tree = self._tree(commit)
            require(set(actual_tree) == set(tree) | set(files), "REMOTE_WRITE_SET_CHANGED")
            for path, row in tree.items():
                if path not in files:
                    require(actual_tree[path]["sha"] == row["sha"], "REMOTE_UNRELATED_CHANGE")
            for path, raw in files.items():
                require(self._read(commit, path, tree=actual_tree) == raw, "REMOTE_WRITE_READBACK_MISMATCH")
            return commit
        except (RecordError, KeyError, TypeError, AttributeError) as exc:
            raise UnknownWrite("REMOTE_WRITE_REQUIRES_RECONCILIATION") from None
