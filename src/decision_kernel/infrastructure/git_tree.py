"""Read one exact, complete Git tree from an existing local object database.

This is an optional metadata accelerator. The Store still owns live admission,
path/mode validation and blob custody. Nothing here fetches, writes or caches Git
objects; any incomplete local answer must fall back to the native remote owner.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
from threading import Lock, Thread
import time

from decision_kernel.domain.records import encoded, require

SCHEMA = "local-git-tree-v1"
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
    """A bounded read-only tree question, compatible with ordinary/bare Git."""
    def __init__(self, path, *, max_entries: int, max_bytes: int,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS):
        require(type(max_entries) is int and max_entries > 0
            and type(max_bytes) is int and max_bytes > 0, "LOCAL_TREE_INVALID_LIMITS")
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
            "LOCAL_TREE_INVALID_TIMEOUT")
        self.path = str(Path(path).expanduser().resolve())
        self._max_entries, self._max_bytes, self._timeout = max_entries, max_bytes, timeout
        self._answered = self._declined = 0

    def _git(self, *args, deadline, limit, rows=None, allowed=(0,)):
        """Drain both pipes with finite buffers; kill on overflow or deadline.

        Threads support Windows pipes too. communicate()/capture_output would
        allocate the entire child output before a caller could enforce a bound.
        The three native queries in read() share one deadline.
        """
        if time.monotonic() >= deadline:
            raise LocalTreeUnavailable("LOCAL_TREE_TIMEOUT")
        command = ["git", "--no-pager", "--no-replace-objects",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "gc.auto=0", "-c", "protocol.allow=never", "-c", "credential.helper=",
            "-C", self.path, *args]
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                env=_environment(), shell=False)
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

    def _read(self, tree_sha):
        if not isinstance(tree_sha, str) or _SHA.fullmatch(tree_sha) is None:
            raise LocalTreeUnavailable("LOCAL_TREE_INVALID_TREE")
        deadline = time.monotonic() + self._timeout
        # Older Git may ignore GIT_NO_LAZY_FETCH. Refuse every declared partial
        # or promisor repository before object lookup, including included and
        # linked-worktree config. The empty protocol whitelist is also enforced
        # independently of repo-specific protocol.*.allow settings.
        status, partial = self._git("config", "--includes", "--null", "--get-regexp", _PARTIAL,
            deadline=deadline, limit=8192, allowed=(0, 1))
        if status != 1 or partial:
            raise LocalTreeUnavailable("LOCAL_TREE_PARTIAL_REPOSITORY")
        _, kind = self._git("cat-file", "-t", tree_sha, deadline=deadline, limit=32)
        if kind != b"tree\n":
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
