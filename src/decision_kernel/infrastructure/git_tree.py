"""Read exact, bounded Git objects from an existing local object database.

This is an optional object accelerator. The Store still owns live admission,
path/mode validation and blob custody. Nothing here fetches, writes or caches Git
objects; any incomplete local answer must fall back to the native remote owner.
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
from threading import Lock, Thread
import time

from decision_kernel.domain.records import MAX_BYTES, encoded, require
from decision_kernel.infrastructure.git_batch import GitBatchUnavailable, GitObjectBatch

SCHEMA = "local-git-tree-v1"
BLOB_SCHEMA = "local-git-blob-v1"
PROCESS_SCHEMA = "local-git-process-v1"
MAX_BLOB_BYTES = 2 * MAX_BYTES
DEFAULT_TIMEOUT_SECONDS = 30
_SHA = re.compile(r"[a-f0-9]{40}\Z")
_META = re.compile(rb"([0-7]{6}) (blob|tree|commit) ([a-f0-9]{40}) +([0-9]+|-)\Z")
_PARTIAL = r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$"
_PROCESS_ENV = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "TMPDIR", "SYSTEMDRIVE"})


class LocalTreeUnavailable(Exception):
    """No complete local answer is available; this never means an empty tree."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _environment():
    # A whitelist also excludes source/Store credentials, SSH agents, injected
    # config, object directories and Git executable/replace overrides. Preserve
    # Windows process essentials without inheriting user or system Git config.
    env = {key: value for key, value in os.environ.items() if key.upper() in _PROCESS_ENV}
    env.setdefault("PATH", os.defpath)
    env.update(LANG="C", LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
        GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="", GIT_PROTOCOL_FROM_USER="0")
    return env


class LocalGitTree:
    """Bounded exact-object questions, compatible with ordinary/bare Git."""
    def __init__(self, path, *, max_entries: int, max_bytes: int,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS, reuse_processes: bool = False):
        require(type(max_entries) is int and max_entries > 0
            and type(max_bytes) is int and max_bytes > 0, "LOCAL_TREE_INVALID_LIMITS")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
            "LOCAL_TREE_INVALID_TIMEOUT")
        require(type(reuse_processes) is bool, "LOCAL_TREE_INVALID_REUSE")
        self.path = str(Path(path).expanduser().resolve())
        self._max_entries, self._max_bytes, self._timeout = max_entries, max_bytes, timeout
        self._answered = self._declined = 0
        self._blob_answered = self._blob_declined = 0
        self._batch_mode = None if reuse_processes else False
        self._batch = None
        self._closed = False
        self._processes = 0

    def _command(self, *args):
        return ["git", "--no-pager", "--no-replace-objects",
            *(["--no-lazy-fetch"] if self._batch_mode is True else []),
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "gc.auto=0", "-c", "protocol.allow=never", "-c", "credential.helper=",
            "-C", self.path, *args]

    def _git(self, *args, deadline, limit, rows=None, allowed=(0,)):
        """Drain both pipes with finite buffers; kill on overflow or deadline.

        Threads support Windows pipes too. communicate()/capture_output would
        allocate the entire child output before a caller could enforce a bound.
        All native queries in one object read share one deadline.
        """
        if self._closed:
            raise LocalTreeUnavailable("LOCAL_TREE_CLOSED")
        if time.monotonic() >= deadline:
            raise LocalTreeUnavailable("LOCAL_TREE_TIMEOUT")
        command = self._command(*args)
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                env=_environment(), shell=False)
            self._processes += 1
        except (OSError, ValueError):
            raise LocalTreeUnavailable("LOCAL_TREE_GIT_UNAVAILABLE") from None
        output, errors, refusal = bytearray(), bytearray(), []
        lock = Lock()

        def stop(reason):
            with lock:
                if not refusal:
                    refusal.append(reason)
            try:
                process.kill()
            except OSError:
                pass

        def drain(stream, target, bound, row_bound=None):
            count = 0
            try:
                while True:
                    part = stream.read(min(65536, bound - len(target) + 1))
                    if not part:
                        return
                    if len(target) + len(part) > bound:
                        stop("LOCAL_TREE_OUTPUT_LIMIT")
                        return
                    if row_bound is not None:
                        count += part.count(b"\0")
                        if count > row_bound:
                            stop("LOCAL_TREE_ENTRY_LIMIT")
                            return
                    target.extend(part)
            except (OSError, ValueError):
                stop("LOCAL_TREE_GIT_UNAVAILABLE")
            finally:
                stream.close()

        readers = [Thread(target=drain, args=(process.stdout, output, limit, rows), daemon=True),
            Thread(target=drain, args=(process.stderr, errors, min(limit, 8192)), daemon=True)]
        try:
            for reader in readers:
                reader.start()
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                stop("LOCAL_TREE_TIMEOUT")
            for reader in readers:
                reader.join(timeout=max(0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers) or time.monotonic() >= deadline:
                stop("LOCAL_TREE_TIMEOUT")
            if refusal:
                raise LocalTreeUnavailable(refusal[0])
            if process.returncode not in allowed:
                raise LocalTreeUnavailable("LOCAL_TREE_GIT_FAILED")
            return process.returncode, bytes(output)
        except (OSError, RuntimeError):
            stop("LOCAL_TREE_GIT_UNAVAILABLE")
            raise LocalTreeUnavailable("LOCAL_TREE_GIT_UNAVAILABLE") from None
        finally:
            if process.poll() is None:
                stop("LOCAL_TREE_TIMEOUT")
            # Kill/reap has its own small cleanup bound, never another read
            # timeout. A killed Git cannot leave an unlimited capture running.
            try:
                process.wait(timeout=0.2)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=0.1)

    def _check_no_partial(self, *, deadline):
        # Older Git may ignore GIT_NO_LAZY_FETCH. Refuse every declared partial
        # or promisor repository before object lookup, including included and
        # linked-worktree config. The empty protocol whitelist is also enforced
        # independently of repo-specific protocol.*.allow settings.
        status, partial = self._git("config", "--includes", "--null", "--get-regexp", _PARTIAL,
            deadline=deadline, limit=8192, allowed=(0, 1))
        if status != 1 or partial:
            raise LocalTreeUnavailable("LOCAL_TREE_PARTIAL_REPOSITORY")

    def _prepare_objects(self, *, deadline):
        if self._closed:
            raise LocalTreeUnavailable("LOCAL_TREE_CLOSED")
        if self._batch_mode is True:
            return
        self._check_no_partial(deadline=deadline)
        if self._batch_mode is None:
            # Probe the native no-fetch switch, not a version string. Older Git
            # keeps the existing one-shot local path and its per-object config
            # check. Modern Git enforces this switch on every object process,
            # so this one invocation can reuse its already qualified channel.
            status, _ = self._git("--no-lazy-fetch", "--version", deadline=deadline,
                limit=8192, allowed=(0, 129))
            self._batch_mode = status == 0
            if self._batch_mode:
                self._batch = GitObjectBatch(self._command("cat-file", "--batch-command"),
                    env=_environment())

    def _info(self, oid, *, deadline):
        try:
            return self._batch.info(oid, deadline=deadline)
        except GitBatchUnavailable as exc:
            raise LocalTreeUnavailable(exc.reason) from None

    def object_type(self, oid, *, timeout):
        """Exact type for the same operation's ancestry owner; no ref peeling."""
        if not isinstance(oid, str) or _SHA.fullmatch(oid) is None:
            raise LocalTreeUnavailable("LOCAL_TREE_INVALID_OID")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
            "LOCAL_TREE_INVALID_TIMEOUT")
        deadline = time.monotonic() + timeout
        self._prepare_objects(deadline=deadline)
        if self._batch_mode:
            info = self._info(oid, deadline=deadline)
            return info[0] if info is not None else None
        status, _ = self._git("cat-file", "-e", oid, deadline=deadline, limit=8192,
            allowed=(0, 1))
        if status == 1:
            return None
        _, raw = self._git("cat-file", "-t", oid, deadline=deadline, limit=32)
        if raw not in (b"blob\n", b"tree\n", b"commit\n", b"tag\n"):
            raise LocalTreeUnavailable("LOCAL_TREE_INVALID_TYPE")
        return raw.decode("ascii").strip()

    def run_git(self, *args, timeout, limit=8192, allowed=(0, 1)):
        """Bounded native graph commands for the same operation's ancestry."""
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
            "LOCAL_TREE_INVALID_TIMEOUT")
        deadline = time.monotonic() + timeout
        # --git-dir is an admission question without object lookup. All actual
        # graph/ref queries qualify the no-fetch path before consulting objects.
        if args != ("rev-parse", "--git-dir"):
            self._prepare_objects(deadline=deadline)
        return self._git(*args, deadline=deadline, limit=limit, allowed=allowed)

    def close(self):
        """End this operation's channel; a closed owner never respawns it."""
        self._closed = True
        if self._batch is not None:
            self._batch.close()

    def process_stats(self):
        """Count process starts and protocol requests, without command data."""
        batch = self._batch.stats() if self._batch is not None else {"processes": 0, "requests": 0}
        return {"schema": PROCESS_SCHEMA, "processes": self._processes + batch["processes"],
            "batch_processes": batch["processes"], "batch_requests": batch["requests"],
            "mode": "unprobed" if self._batch_mode is None else
                ("batch" if self._batch_mode else "legacy")}

    def _read(self, tree_sha):
        if not isinstance(tree_sha, str) or _SHA.fullmatch(tree_sha) is None:
            raise LocalTreeUnavailable("LOCAL_TREE_INVALID_TREE")
        deadline = time.monotonic() + self._timeout
        self._prepare_objects(deadline=deadline)
        if self._batch_mode:
            info = self._info(tree_sha, deadline=deadline)
            kind = info[0] if info is not None else None
        else:
            _, raw_kind = self._git("cat-file", "-t", tree_sha, deadline=deadline, limit=32)
            kind = "tree" if raw_kind == b"tree\n" else None
        if kind != "tree":
            raise LocalTreeUnavailable("LOCAL_TREE_NOT_TREE")
        _, raw = self._git("ls-tree", "-r", "-t", "-l", "-z", "--full-tree", tree_sha,
            deadline=deadline, limit=self._max_bytes, rows=self._max_entries)
        if raw and not raw.endswith(b"\0"):
            raise LocalTreeUnavailable("LOCAL_TREE_INVALID_OUTPUT")
        if raw.count(b"\0") > self._max_entries:
            raise LocalTreeUnavailable("LOCAL_TREE_ENTRY_LIMIT")
        result = {"sha": tree_sha, "truncated": False, "tree": []}
        size = len(encoded(result))
        for raw_row in raw.split(b"\0")[:-1]:
            if time.monotonic() >= deadline:
                raise LocalTreeUnavailable("LOCAL_TREE_TIMEOUT")
            metadata, separator, path = raw_row.partition(b"\t")
            match = _META.fullmatch(metadata)
            if not separator or not path or match is None:
                raise LocalTreeUnavailable("LOCAL_TREE_INVALID_OUTPUT")
            mode, kind, sha, length = match.groups()
            try:
                row = {"path": path.decode("utf-8"), "mode": mode.decode("ascii"),
                    "type": kind.decode("ascii"), "sha": sha.decode("ascii")}
            except UnicodeError:
                raise LocalTreeUnavailable("LOCAL_TREE_INVALID_OUTPUT") from None
            if kind == b"blob":
                if not length.isdigit() or len(length) > 20 or int(length) >= 2 ** 64:
                    raise LocalTreeUnavailable("LOCAL_TREE_BLOB_SIZE")
                row["size"] = int(length)
            elif length != b"-":
                raise LocalTreeUnavailable("LOCAL_TREE_INVALID_OUTPUT")
            # Bound the actual GitHub-shaped JSON too, not just Git's more
            # compact native output. encoded(row) contributes one trailing LF.
            size += len(encoded(row)) - 1 + bool(result["tree"])
            if size > self._max_bytes:
                raise LocalTreeUnavailable("LOCAL_TREE_OUTPUT_LIMIT")
            result["tree"].append(row)
        if size > self._max_bytes:
            raise LocalTreeUnavailable("LOCAL_TREE_OUTPUT_LIMIT")
        if time.monotonic() >= deadline:
            raise LocalTreeUnavailable("LOCAL_TREE_TIMEOUT")
        return result

    def _read_blob(self, oid, expected_size):
        if not isinstance(oid, str) or _SHA.fullmatch(oid) is None:
            raise LocalTreeUnavailable("LOCAL_BLOB_INVALID_OID")
        if type(expected_size) is not int or expected_size < 0:
            raise LocalTreeUnavailable("LOCAL_BLOB_INVALID_SIZE")
        if expected_size > MAX_BLOB_BYTES:
            raise LocalTreeUnavailable("LOCAL_BLOB_SIZE_LIMIT")
        result = {"sha": oid, "size": expected_size, "encoding": "base64", "content": ""}
        # Base64 is ASCII with no JSON escaping. Refuse before reading bytes if
        # the requested response cannot fit the existing API response bound.
        if len(encoded(result)) + 4 * ((expected_size + 2) // 3) > self._max_bytes:
            raise LocalTreeUnavailable("LOCAL_BLOB_RESPONSE_LIMIT")
        deadline = time.monotonic() + self._timeout
        self._prepare_objects(deadline=deadline)
        if self._batch_mode:
            try:
                raw = self._batch.blob(oid, expected_size, deadline=deadline)
            except GitBatchUnavailable as exc:
                raise LocalTreeUnavailable(exc.reason) from None
            if raw is None:
                raise LocalTreeUnavailable("LOCAL_BLOB_MISSING")
        else:
            _, kind = self._git("cat-file", "-t", oid, deadline=deadline, limit=32)
            if kind != b"blob\n":
                raise LocalTreeUnavailable("LOCAL_BLOB_NOT_BLOB")
            # No textconv, filters, replacement refs or worktree lookup. A zero-
            # byte bound is intentional: an empty blob cannot supply body bytes.
            _, raw = self._git("cat-file", "blob", oid, deadline=deadline, limit=expected_size)
        if len(raw) != expected_size:
            raise LocalTreeUnavailable("LOCAL_BLOB_SIZE_MISMATCH")
        actual_oid = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
        if actual_oid != oid:
            raise LocalTreeUnavailable("LOCAL_BLOB_OID_MISMATCH")
        result["content"] = base64.b64encode(raw).decode("ascii")
        if len(encoded(result)) > self._max_bytes:
            raise LocalTreeUnavailable("LOCAL_BLOB_RESPONSE_LIMIT")
        if time.monotonic() >= deadline:
            raise LocalTreeUnavailable("LOCAL_TREE_TIMEOUT")
        return result

    def read_blob(self, oid, expected_size):
        """Read a caller-admitted tree row's exact blob; never establish access."""
        try:
            result = self._read_blob(oid, expected_size)
        except LocalTreeUnavailable:
            self._blob_declined += 1
            raise
        self._blob_answered += 1
        return result

    def blob_stats(self):
        """Blob diagnostics are independent from tree inventory counters."""
        return {"schema": BLOB_SCHEMA, "answered": self._blob_answered, "declined": self._blob_declined}

    def read(self, tree_sha):
        """Return a complete GitHub-shaped tree or one explicit local refusal."""
        try:
            result = self._read(tree_sha)
        except LocalTreeUnavailable:
            self._declined += 1
            raise
        self._answered += 1
        return result

    def stats(self):
        """Additive diagnostics only; never paths, object IDs or Git output."""
        return {"schema": SCHEMA, "answered": self._answered, "declined": self._declined}
