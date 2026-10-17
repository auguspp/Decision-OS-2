"""Finite, byte-checked reader source export from an exact trusted Git commit.

This is not a package installer. Archive creation never reads the working tree,
executes archived code or includes Git configuration, credentials or caches.
"""
from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath
import platform
import re
import tarfile

from decision_kernel.domain.records import RecordError, SHA, digest, require
from decision_kernel.infrastructure.git_snapshot import git
from decision_kernel.infrastructure.github_store import GitHubBinding, _json

PATHS = ("src/decision_kernel", "docs/sources", "config/store-binding.json",
         "pyproject.toml", "README.md", "docs/engineering/store-runtime.md",
         "docs/engineering/recovery-kit.md")
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_FILES = 1024


def _path(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts) and not name.startswith("/") and all(
        re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p) and p not in (".", "..") for p in parts)


def _allowed(name: str) -> bool:
    return (_path(name) and (name in PATHS or
            (name.startswith("src/decision_kernel/") and name.endswith(".py")) or
            (name.startswith("docs/sources/") and name.endswith((".md", ".json", ".txt")))))


def _blob(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def inspect_archive(raw: bytes, *, code_sha: str, expected: dict[str, str]) -> dict:
    """Check *all* members against a separately obtained Git tree, without extracting."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_ARCHIVE_BYTES, "READER_ARCHIVE_BOUND")
    require(isinstance(code_sha, str) and SHA.fullmatch(code_sha), "INVALID_CODE_SHA")
    require(isinstance(expected, dict) and 0 < len(expected) <= MAX_FILES and
            all(_allowed(p) and isinstance(s, str) and SHA.fullmatch(s) for p, s in expected.items()),
            "READER_ARCHIVE_INVENTORY")
    seen, files, total = set(), {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            require(archive.pax_headers.get("comment") == code_sha, "READER_ARCHIVE_COMMIT")
            for member in archive:
                name = member.name.rstrip("/") if member.isdir() else member.name
                require(_path(name) and name not in seen, "READER_ARCHIVE_PATH")
                seen.add(name)
                require(len(seen) <= MAX_FILES * 2, "READER_ARCHIVE_BOUND")
                if member.isdir():
                    require(any(p.startswith(name + "/") for p in expected), "READER_ARCHIVE_PATH")
                    continue
                require(member.isfile() and not member.sparse and name in expected,
                        "READER_ARCHIVE_REGULAR_FILE")
                total += member.size
                require(0 <= member.size <= MAX_ARCHIVE_BYTES and total <= MAX_ARCHIVE_BYTES,
                        "READER_ARCHIVE_BOUND")
                stream = archive.extractfile(member)
                require(stream is not None, "READER_ARCHIVE_REGULAR_FILE")
                data = stream.read(MAX_ARCHIVE_BYTES + 1)
                require(len(data) == member.size and _blob(data) == expected[name],
                        "READER_ARCHIVE_BLOB_MISMATCH")
                files[name] = {"git_blob": expected[name], "size": len(data), "sha256": digest(data)}
    except (tarfile.TarError, OSError, ValueError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("READER_ARCHIVE_INVALID") from None
    require(files.keys() == expected.keys(), "READER_ARCHIVE_FILES_MISMATCH")
    return {"files": files, "file_count": len(files), "expanded_bytes": total}


def archive_reader(checkout: Path, code_sha: str, output: Path, *, binding=None) -> dict:
    """Export allowlisted committed files; reject attributes that omit or rewrite them."""
    require(isinstance(code_sha, str) and SHA.fullmatch(code_sha), "INVALID_CODE_SHA")
    root = str(Path(checkout).resolve(strict=True))
    require(git("-C", root, "rev-parse", "HEAD").decode().strip() == code_sha,
            "READER_CODE_HEAD_MISMATCH")
    tree = git("-C", root, "rev-parse", code_sha + "^{tree}").decode().strip()
    inventory, total = {}, 0
    for row in filter(None, git("-C", root, "ls-tree", "-rlz", code_sha, "--", *PATHS).split(b"\0")):
        try:
            meta, path = row.split(b"\t")
            mode, kind, oid, size = meta.split()
            name = path.decode("ascii")
            require(mode == b"100644" and kind == b"blob" and _allowed(name),
                    "READER_COMMITTED_PATH_INVALID")
            total += int(size)
            inventory[name] = oid.decode("ascii")
        except (ValueError, UnicodeError):
            raise RecordError("READER_COMMITTED_PATH_INVALID") from None
    require(0 < len(inventory) <= MAX_FILES and total <= MAX_ARCHIVE_BYTES, "READER_ARCHIVE_BOUND")
    require(all(p in inventory or any(n.startswith(p + "/") for n in inventory) for p in PATHS),
            "READER_REQUIRED_PATH_MISSING")
    if binding is not None:
        archived = GitHubBinding.from_dict(_json(git("-C", root, "show", code_sha + ":config/store-binding.json")))
        require(archived == binding, "READER_BINDING_MISMATCH")
    raw = git("-C", root, "archive", "--format=tar", code_sha, "--", *PATHS)
    checked = inspect_archive(raw, code_sha=code_sha, expected=inventory)
    try:
        with output.open("xb") as stream:
            stream.write(raw)
    except OSError:
        raise RecordError("READER_ARCHIVE_OUTPUT_UNAVAILABLE") from None
    return {"schema": "store-reader-archive-v1", "name": output.name, "code_sha": code_sha,
            "tree": tree, "size": len(raw), "sha256": digest(raw), **checked,
            "runtime": {"python": platform.python_version(), "git": git("--version").decode().strip(),
                        "prerequisites": "Python >=3.12 and Git installed separately; no third-party packages for recovery",
                        "interpreter_and_os_included": False},
            "execution": "not-executed-by-export", "scope": "committed-reader-source-and-local-configuration"}
