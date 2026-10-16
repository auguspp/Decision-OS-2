"""Read-only recovery of an exact GitHub store snapshot, using native Git bundles."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from decision_kernel.domain.records import Conflict, RecordError, SHA, StoreUnavailable, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubStore, MAX_TREE_ENTRIES

MAX_BUNDLE_BYTES = 64 * 1024 * 1024


def git(*args: str, https: bool = False) -> bytes:
    """Only callers here select commands/paths; never inherit credentials or Git config."""
    env = {**GitStore._env(), "GIT_NO_LAZY_FETCH": "1"}
    command = ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "gc.auto=0",
               "-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
    if https:
        command += ["-c", "protocol.https.allow=always", "-c", "credential.helper="]
    try:
        result = subprocess.run(command + list(args), capture_output=True, env=env,
                                timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise StoreUnavailable("SNAPSHOT_GIT_UNAVAILABLE") from None
    if result.returncode:
        raise StoreUnavailable("SNAPSHOT_GIT_FAILED")
    return result.stdout


class GitSnapshot(GitStore):
    """Same read contract, no mutation or live permission claim. Owner can still alter files."""
    def __init__(self, root: Path, binding: GitHubBinding, snapshot: str):
        require(isinstance(binding, GitHubBinding) and not binding.write_enabled,
                "SNAPSHOT_READ_ONLY_BINDING_REQUIRED")
        require(isinstance(snapshot, str) and bool(SHA.fullmatch(snapshot)), "INVALID_SNAPSHOT")
        try:
            self.root = Path(root).resolve(strict=True)
        except OSError:
            raise StoreUnavailable("SNAPSHOT_UNAVAILABLE") from None
        self.binding, self.repository, self._snapshot = binding, binding.repository, snapshot
        require(self._run("rev-parse", "--is-bare-repository").stdout.strip() == b"true",
                "DEDICATED_BARE_STORE_REQUIRED")
        require(self._run("rev-parse", "--is-shallow-repository").stdout.strip() == b"false",
                "COMPLETE_SNAPSHOT_REQUIRED")
        require(not (self.root / "objects/info/alternates").exists()
                and not (self.root / "info/grafts").exists(), "EXTERNAL_OBJECTS_FORBIDDEN")
        require(self._run("config", "--get-regexp", r"(promisor|partialclone)", allowed=(0, 1)).returncode == 1,
                "EXTERNAL_OBJECTS_FORBIDDEN")
        self.check_commit(snapshot)
        require(GitStore.read(self, binding.anchor, "store.json") == binding.marker()
                and GitStore.read(self, snapshot, "store.json") == binding.marker(), "STORE_MARKER_MISMATCH")
        # Validate the actual restored tree, not a user-supplied list of expected files.
        rows = self._run("ls-tree", "-r", "-z", snapshot).stdout.split(b"\0")
        require(len(rows) - 1 <= MAX_TREE_ENTRIES, "SNAPSHOT_TREE_LIMIT")
        for row in filter(None, rows):
            try:
                meta, path = row.split(b"\t")
                mode, kind, oid = meta.split()
                self._path(path.decode("ascii"))
            except (ValueError, UnicodeError):
                raise RecordError("INVALID_SNAPSHOT_TREE") from None
            require(mode == b"100644" and kind == b"blob" and bool(SHA.fullmatch(oid.decode())),
                    "REGULAR_BLOB_REQUIRED")

    @staticmethod
    def _env() -> dict[str, str]:
        return {**GitStore._env(), "GIT_NO_LAZY_FETCH": "1", "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "protocol.allow", "GIT_CONFIG_VALUE_0": "never"}

    @staticmethod
    def _path(path: str) -> None:
        GitHubStore._path(path)

    def head(self) -> str:
        return self._snapshot

    def check_commit(self, commit: str) -> None:
        require(isinstance(commit, str) and bool(SHA.fullmatch(commit)), "INVALID_COMMIT")
        for base, head in ((self.binding.anchor, commit), (commit, self._snapshot)):
            require(self._run("merge-base", "--is-ancestor", base, head, allowed=(0, 1, 128)).returncode == 0,
                    "COMMIT_OUTSIDE_BINDING")

    def read(self, commit: str, path: str) -> bytes:
        self.check_commit(commit)
        return super().read(commit, path)

    def paths(self, commit: str, prefix: str = "") -> list[str]:
        self.check_commit(commit)
        # Base helper treats a directory prefix as a complete blob path; list then filter instead.
        require(prefix in ("", "source-views/", "research/cases/", "submissions/"), "PATH_OUTSIDE_STORE")
        paths = self._run("ls-tree", "-r", "--name-only", "-z", commit).stdout
        try:
            return sorted(p.decode("ascii") for p in paths.split(b"\0") if p and p.decode("ascii").startswith(prefix))
        except UnicodeError:
            raise RecordError("INVALID_SNAPSHOT_TREE") from None

    def last_change(self, head: str, path: str) -> str:
        self.check_commit(head)
        commit = super().last_change(head, path)
        self.check_commit(commit)
        return commit

    def commit(self, files: dict[str, bytes], *, expected: str | None) -> str:
        raise RecordError("SNAPSHOT_READ_ONLY")


def bundle_from_bare(root: Path, binding: GitHubBinding, snapshot: str, output: Path) -> dict:
    """Output is new; snapshot is retained by a local-only ref, never pushed."""
    # Audit the complete, SHA-pinned native Git mirror rather than exhausting a
    # bounded remote REST session one historic source/revision at a time.
    from decision_kernel.application.store_audit import audit_store
    from decision_kernel.application.versioned_store import VersionedStore
    mirror = GitSnapshot(root, replace(binding, write_enabled=False), snapshot)
    mirror_audit = audit_store(VersionedStore(mirror), snapshot)
    if output.exists():
        raise Conflict("BUNDLE_ALREADY_EXISTS")
    ref = "refs/heads/" + binding.branch
    current = git("--git-dir=" + str(root), "rev-parse", ref).decode().strip()
    require(current == snapshot, "SNAPSHOT_HEAD_MISMATCH")
    git("--git-dir=" + str(root), "fsck", "--full", "--strict", "--no-reflogs")
    git("--git-dir=" + str(root), "bundle", "create", str(output), ref)
    require(0 < output.stat().st_size <= MAX_BUNDLE_BYTES, "BUNDLE_SIZE_LIMIT")
    # Empty destination verification proves that this is not a thin/incremental
    # backup. Compare two independently instantiated local readers and all refs.
    restored_audit = verify_bundle(output, binding, snapshot)
    require(restored_audit == mirror_audit, "RESTORED_REFERENCES_MISMATCH")
    return mirror_audit


def verify_bundle(bundle: Path, binding: GitHubBinding, snapshot: str) -> dict:
    """Restore without GitHub or credentials, then follow the original version references."""
    from decision_kernel.application.store_audit import audit_store
    from decision_kernel.application.versioned_store import VersionedStore
    require(bundle.is_file() and 0 < bundle.stat().st_size <= MAX_BUNDLE_BYTES, "BUNDLE_SIZE_LIMIT")
    with TemporaryDirectory(prefix="dos2-restore-") as directory:
        empty, restored = Path(directory) / "empty.git", Path(directory) / "restored.git"
        git("init", "--bare", "--template=", str(empty))
        git("--git-dir=" + str(empty), "bundle", "verify", str(bundle.resolve()))
        ref = "refs/heads/" + binding.branch
        advertised = git("bundle", "list-heads", str(bundle.resolve())).decode().splitlines()
        require(advertised == [snapshot + " " + ref], "BUNDLE_REF_MISMATCH")
        git("clone", "--bare", "--single-branch", "--branch", binding.branch, "--template=",
            str(bundle.resolve()), str(restored))
        git("--git-dir=" + str(restored), "fsck", "--full", "--strict", "--no-reflogs")
        reader = GitSnapshot(restored, replace(binding, write_enabled=False), snapshot)
        return audit_store(VersionedStore(reader), snapshot)


def export_public_snapshot(binding: GitHubBinding, snapshot: str, output: Path) -> dict:
    """Full native Git backup of the bound public branch; no token is passed to Git."""
    require(binding.visibility == "public", "PUBLIC_BACKUP_ONLY")
    require(isinstance(snapshot, str) and bool(SHA.fullmatch(snapshot)), "INVALID_SNAPSHOT")
    with TemporaryDirectory(prefix="dos2-export-") as directory:
        bare = Path(directory) / "records.git"
        git("clone", "--bare", "--single-branch", "--branch", binding.branch, "--template=",
            "https://github.com/" + binding.repository + ".git", str(bare), https=True)
        # A subsequent legitimate write may have advanced the remote. Retain the pinned older snapshot.
        GitSnapshot(bare, replace(binding, write_enabled=False), snapshot)
        ref = "refs/heads/" + binding.branch
        fetched = git("--git-dir=" + str(bare), "rev-parse", ref).decode().strip()
        git("--git-dir=" + str(bare), "merge-base", "--is-ancestor", snapshot, fetched)
        git("--git-dir=" + str(bare), "update-ref", ref, snapshot, fetched)
        return bundle_from_bare(bare, binding, snapshot, output)
