"""Dedicated local bare Git storage; no network, hooks, checkout or remote writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import uuid

from decision_kernel.domain.records import (
    BlobRef, Conflict, IntegrityError, MAX_BYTES, MissingObject, RecordError, SHA,
    StoreUnavailable, UnknownWrite, digest, encoded, require,
)

REF = "refs/heads/records"


class GitStore:
    """A local synthetic binding, not a production credential/role boundary."""

    def __init__(self, root: Path):
        try:
            self.root = Path(root).resolve(strict=True)
        except OSError as exc:
            raise StoreUnavailable("STORE_UNAVAILABLE") from exc
        require(self._run("rev-parse", "--is-bare-repository").stdout.strip() == b"true", "DEDICATED_BARE_STORE_REQUIRED")
        head = self.head()
        require(head is not None, "STORE_NOT_INITIALIZED")
        try:
            binding = json.loads(self.read(head, "store.json"))
        except (MissingObject, ValueError) as exc:
            raise RecordError("STORE_BINDING_REQUIRED") from exc
        require(isinstance(binding, dict) and set(binding) == {"schema", "mode", "repository"} and binding["schema"] == "store-binding-v1"
                and binding["mode"] == "synthetic"
                and isinstance(binding["repository"], str)
                and binding["repository"].startswith("synthetic-git:"), "UNSUPPORTED_STORE_BINDING")
        self.repository = binding["repository"]

    @staticmethod
    def _env() -> dict[str, str]:
        # Do not forward model/source/engineering credentials or Git override env.
        return {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_AUTHOR_NAME": "Decision OS local store", "GIT_AUTHOR_EMAIL": "store@localhost",
                "GIT_COMMITTER_NAME": "Decision OS local store", "GIT_COMMITTER_EMAIL": "store@localhost"}

    def _run(self, *args: str, data: bytes | None = None, index: str | None = None,
             allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        env = self._env()
        if index is not None:
            env["GIT_INDEX_FILE"] = index
        try:
            result = subprocess.run(["git", "-c", "core.hooksPath=" + os.devnull,
                                     "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
                                     "--git-dir=" + str(self.root), *args],
                                    input=data, capture_output=True, env=env, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StoreUnavailable("GIT_UNAVAILABLE") from exc
        if result.returncode not in allowed:
            raise StoreUnavailable("GIT_OPERATION_FAILED")
        return result

    @classmethod
    def init_synthetic(cls, root: Path) -> GitStore:
        """Create only a new, explicitly synthetic store; never reuse an existing path."""
        root = Path(root).absolute()
        try:
            root.mkdir(mode=0o700, parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise Conflict("STORE_PATH_EXISTS") from exc
        except OSError as exc:
            raise StoreUnavailable("STORE_PATH_UNAVAILABLE") from exc
        try:
            subprocess.run(["git", "init", "--bare", "--object-format=sha1", "--initial-branch=records",
                            "--template=", str(root)], env=cls._env(), capture_output=True, check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            # Preserve partial creation for inspection; never delete an uncertain target.
            raise StoreUnavailable("STORE_INITIALIZATION_FAILED") from exc
        instance = object.__new__(cls)
        instance.root = root.resolve()
        instance.commit({"store.json": encoded({"schema": "store-binding-v1", "mode": "synthetic",
                         "repository": "synthetic-git:" + uuid.uuid4().hex})}, expected=None)
        return cls(root)

    def head(self) -> str | None:
        result = self._run("rev-parse", "--verify", "--quiet", REF, allowed=(0, 1))
        if result.returncode == 1:
            return None
        value = result.stdout.decode("ascii").strip()
        require(bool(SHA.fullmatch(value)), "INVALID_STORE_HEAD")
        return value

    def check_commit(self, commit: str) -> None:
        require(isinstance(commit, str) and bool(SHA.fullmatch(commit)), "INVALID_COMMIT")
        current = self.head()
        require(current is not None, "STORE_NOT_INITIALIZED")
        result = self._run("merge-base", "--is-ancestor", commit, current, allowed=(0, 1, 128))
        if result.returncode != 0:
            raise MissingObject("COMMIT_NOT_REACHABLE")

    @staticmethod
    def _path(path: str) -> None:
        BlobRef("local-path-validation", "0" * 40, path, "0" * 64)

    def paths(self, commit: str, prefix: str = "") -> list[str]:
        require(isinstance(commit, str) and bool(SHA.fullmatch(commit)), "INVALID_COMMIT")
        if prefix:
            self._path(prefix.rstrip("/"))
        output = self._run("ls-tree", "-r", "--name-only", "-z", commit, "--", prefix or ".").stdout
        try:
            return [p.decode("ascii") for p in output.split(b"\0") if p]
        except UnicodeError as exc:
            raise IntegrityError("UNEXPECTED_STORE_PATH") from exc

    def read(self, commit: str, path: str) -> bytes:
        require(isinstance(commit, str) and bool(SHA.fullmatch(commit)), "INVALID_COMMIT")
        self._path(path)
        row = self._run("ls-tree", "-z", commit, "--", path).stdout
        if not row:
            raise MissingObject("OBJECT_MISSING")
        try:
            meta, name = row.rstrip(b"\0").split(b"\t")
            mode, kind, sha = meta.split()
        except ValueError as exc:
            raise IntegrityError("OBJECT_METADATA_INVALID") from exc
        if name.decode() != path or mode != b"100644" or kind != b"blob":
            raise IntegrityError("REGULAR_BLOB_REQUIRED")
        size = int(self._run("cat-file", "-s", sha.decode()).stdout)
        require(0 <= size <= MAX_BYTES * 2, "OBJECT_TOO_LARGE")
        raw = self._run("cat-file", "blob", sha.decode()).stdout
        if len(raw) != size:
            raise IntegrityError("OBJECT_SIZE_CHANGED")
        return raw

    def last_change(self, head: str, path: str) -> str:
        self._path(path)
        require(bool(SHA.fullmatch(head)), "INVALID_COMMIT")
        commit = self._run("log", "-1", "--format=%H", head, "--", path).stdout.decode().strip()
        require(bool(SHA.fullmatch(commit)), "RECEIPT_COMMIT_MISSING")
        return commit

    def reference(self, commit: str, path: str) -> BlobRef:
        return BlobRef(self.repository, commit, path, digest(self.read(commit, path)))

    def resolve(self, ref: BlobRef) -> bytes:
        require(ref.repository == self.repository, "FOREIGN_REPOSITORY")
        self.check_commit(ref.commit)
        raw = self.read(ref.commit, ref.path)
        if digest(raw) != ref.sha256:
            raise IntegrityError("BLOB_DIGEST_MISMATCH")
        return raw

    def _advance(self, commit: str, expected: str | None) -> None:
        try:
            result = self._run("update-ref", REF, commit, expected or "0" * 40, allowed=(0, 1, 128))
        except StoreUnavailable as exc:
            # A timed-out ref update may have committed. Retry the same submission, not a new key.
            raise UnknownWrite("REF_UPDATE_UNCONFIRMED") from exc
        if result.returncode:
            if self.head() != expected:
                raise Conflict("HEAD_CHANGED")
            raise StoreUnavailable("REF_UPDATE_FAILED")

    def commit(self, files: dict[str, bytes], *, expected: str | None) -> str:
        require(bool(files) and len(files) <= 128, "INVALID_WRITE_SET")
        if expected is not None:
            require(bool(SHA.fullmatch(expected)), "INVALID_COMMIT")
        if self.head() != expected:
            raise Conflict("HEAD_CHANGED")
        # The temporary index never touches a checkout or the repository's shared index.
        with TemporaryDirectory(prefix="dos2-index-") as directory:
            index = str(Path(directory) / "index")
            self._run("read-tree", expected if expected else "--empty", index=index)
            rows = []
            for path, raw in sorted(files.items()):
                self._path(path)
                require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES * 2, "INVALID_BLOB")
                sha = self._run("hash-object", "-w", "--stdin", data=raw).stdout.decode().strip()
                rows.append(f"100644 {sha}\t{path}\n")
            self._run("update-index", "--index-info", data="".join(rows).encode("ascii"), index=index)
            tree = self._run("write-tree", index=index).stdout.decode().strip()
        args = ["commit-tree", tree]
        if expected:
            args.extend(("-p", expected))
        commit = self._run(*args, data=b"Store versioned records\n").stdout.decode().strip()
        self._advance(commit, expected)
        # Losing/uncertain transactions may leave unreachable Git objects. No unsafe cleanup.
        return commit
