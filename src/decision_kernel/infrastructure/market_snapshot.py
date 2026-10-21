"""Controlled exact market reads through the existing native GitSnapshot.

One outer reading scope pins an authenticated records head, fetches that bounded
branch, follows original SourceRefs locally, and rechecks the live binding before
returning. It never publishes a bundle, writes the remote, or changes visibility.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from decision_kernel.domain.records import RecordError, StoreUnavailable, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_snapshot import GitSnapshot, MAX_BUNDLE_BYTES
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore, github_tree_inventory
from decision_kernel.infrastructure.native_git_memo import native_git_memo_scope
from decision_kernel.infrastructure.runtime_performance import measure_phase, timed


@timed("snapshot.fetch")
def _fetch_market_branch(binding, api, destination):
    """Fixed GitHub origin only; credentials exist only in this child's environment."""
    require(isinstance(binding, GitHubBinding) and not binding.write_enabled
            and isinstance(api, GitHubAPI), "MARKET_SNAPSHOT_CONFIGURATION")
    require(not destination.exists(), "MARKET_SNAPSHOT_NEW_DESTINATION_REQUIRED")
    credential = base64.b64encode(("x-access-token:" + api._token).encode("ascii")).decode("ascii")
    child_env = {**GitStore._env(), "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: Basic " + credential}
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "gc.auto=0",
        "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
        "-c", "credential.helper=", "-c", "http.followRedirects=false",
        "clone", "--bare", "--single-branch", "--branch", binding.branch, "--template=",
        "https://github.com/" + binding.repository + ".git", str(destination)]
    try:
        completed = subprocess.run(command, env=child_env, capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise StoreUnavailable("MARKET_SNAPSHOT_FETCH_UNAVAILABLE") from None
    if completed.returncode:
        # Git stderr can contain repository paths or server text; it is not a receipt.
        raise StoreUnavailable("MARKET_SNAPSHOT_FETCH_FAILED")
    total = 0
    for path in destination.rglob("*"):
        require(not path.is_symlink(), "MARKET_SNAPSHOT_SYMLINK")
        if path.is_file():
            total += path.stat().st_size
            require(total <= MAX_BUNDLE_BYTES, "MARKET_SNAPSHOT_SIZE_LIMIT")


class GitMarketReader:
    """A source-only, independently instantiated, lazily pinned store reader.

    Every outer scope rechecks and pins the actual remote head. Nested query and
    checkpoint readers share that exact snapshot; the caller cannot publish a
    scope's result before its final live visibility/reachability check succeeds.
    The same GitHubAPI object accounts for all remote REST checks in a work unit.
    """
    def __init__(self, binding, api, *, fetch=None):
        require(isinstance(binding, GitHubBinding) and not binding.write_enabled
                and isinstance(api, GitHubAPI), "MARKET_SNAPSHOT_CONFIGURATION")
        self.binding, self.api, self.repository = binding, api, binding.repository
        self._fetch = _fetch_market_branch if fetch is None else fetch
        self._reader = None
        self._depth = 0

    @contextmanager
    def market_read_scope(self):
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        with measure_phase("snapshot.admit"):
            remote = GitHubStore(self.binding, self.api)
            snapshot = remote.head()
        with TemporaryDirectory(prefix="dos2-market-read-") as temporary:
            root = Path(temporary) / "records.git"
            self._fetch(self.binding, self.api, root)
            with measure_phase("snapshot.init"):
                native = GitSnapshot(root, self.binding, snapshot)
            with native_git_memo_scope(native, closed_error="MARKET_SNAPSHOT_SCOPE_CLOSED",
                    read_only_error="MARKET_SNAPSHOT_READ_ONLY") as reader:
                self._reader = reader
                self._depth = 1
                try:
                    yield self
                    with measure_phase("snapshot.exit"):
                        remote.check_commit(snapshot)
                finally:
                    self._depth = 0
                    self._reader = None

    @contextmanager
    def source_read_scope(self, snapshot):
        with self.market_read_scope():
            self._reader.check_commit(snapshot)
            yield self

    @timed("snapshot.read")
    def _call(self, method, *args, **kwargs):
        if self._depth:
            return getattr(self._reader, method)(*args, **kwargs)
        with self.market_read_scope():
            return getattr(self._reader, method)(*args, **kwargs)

    def head(self):
        return self._call("head")

    def check_commit(self, commit):
        return self._call("check_commit", commit)

    def read(self, commit, path):
        require(path.startswith(("source-views/", "submissions/")) or path == "store.json",
                "MARKET_SNAPSHOT_SOURCE_ONLY")
        return self._call("read", commit, path)

    def paths(self, commit, prefix=""):
        require(prefix == "source-views/", "MARKET_SNAPSHOT_SOURCE_ONLY")
        return self._call("paths", commit, prefix)

    def reference(self, commit, path):
        require(path.startswith("source-views/"), "MARKET_SNAPSHOT_SOURCE_ONLY")
        return self._call("reference", commit, path)

    def resolve(self, reference):
        require(reference.path.startswith("source-views/"), "MARKET_SNAPSHOT_SOURCE_ONLY")
        return self._call("resolve", reference)

    def last_change(self, head, path):
        require(path.startswith("source-views/"), "MARKET_SNAPSHOT_SOURCE_ONLY")
        return self._call("last_change", head, path)

    def market_inventory(self):
        """Return bounded counts for the same pinned tree, never private paths."""
        with self.market_read_scope():
            rows = [row for row in self._reader._run("ls-tree", "-r", "-t", "-l", "-z",
                self._reader.head()).stdout.split(b"\0") if row]
            files, directories = {}, []
            for row in rows:
                meta, path = row.split(b"\t")
                fields = meta.split()
                if fields[1] == b"blob":
                    files[path.decode("ascii")] = int(fields[3])
                else:
                    directories.append(path.decode("ascii"))
            counts = github_tree_inventory(self.binding, files, directories=directories)
            total = 0
            def failed(error):
                raise error
            try:
                for current, directories, files in os.walk(self._reader.root, followlinks=False, onerror=failed):
                    require(not any((Path(current) / name).is_symlink() for name in directories + files),
                            "MARKET_SNAPSHOT_SYMLINK")
                    for name in files:
                        total += (Path(current) / name).stat().st_size
            except OSError:
                raise StoreUnavailable("MARKET_SNAPSHOT_SIZE_UNAVAILABLE") from None
            require(counts["tree_entries"] == len(rows), "MARKET_SNAPSHOT_TREE_INVENTORY_CHANGED")
            return {**counts, "native_bytes": total, "tree_response_applicability": "required"}

    def commit(self, files, *, expected):
        raise RecordError("MARKET_SNAPSHOT_READ_ONLY")
