"""Trusted Git archive export, with synthetic committed source and hostile tar members."""
from pathlib import Path
import hashlib
import io
import os
import subprocess
import tarfile

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.reader_archive import PATHS, archive_reader, inspect_archive


def commit(root, message="synthetic"):
    for cmd in (("add", "."), ("commit", "-qm", message)):
        subprocess.run(["git", "-C", str(root), *cmd], env=GitStore._env(), check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()


@pytest.fixture
def code(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    subprocess.run(["git", "init", "--template=", str(root)], check=True, capture_output=True)
    for name in PATHS:
        path = root / (name + "/__init__.py" if name == "src/decision_kernel" else
                       name + "/synthetic.json" if name == "docs/sources" else name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Synthetic committed recovery input.\n")
    return root, commit(root)


def test_exports_only_pinned_committed_bytes_not_dirty_files_or_credentials(code, tmp_path):
    root, sha = code
    original = (root / "src/decision_kernel/__init__.py").read_bytes()
    (root / "src/decision_kernel/__init__.py").write_text("DIRTY NOT EXPORTED")
    (root / "src/decision_kernel/untracked.py").write_text("SECRET NOT EXPORTED")
    (root / ".env").write_text("CREDENTIAL NOT EXPORTED")
    output = tmp_path / "reader.tar"
    result = archive_reader(root, sha, output)
    assert result["code_sha"] == sha
    assert result["file_count"] == len(PATHS)
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert "SECRET NOT EXPORTED".encode() not in output.read_bytes()
    assert "DIRTY NOT EXPORTED".encode() not in output.read_bytes()
    with tarfile.open(output) as t:
        assert t.extractfile("src/decision_kernel/__init__.py").read() == original
    same = tmp_path / "same.tar"
    assert archive_reader(root, sha, same)["sha256"] == result["sha256"]
    assert same.read_bytes() == output.read_bytes()
    assert result["runtime"]["interpreter_and_os_included"] is False
    with pytest.raises(RecordError, match="OUTPUT_UNAVAILABLE"):
        archive_reader(root, sha, output)
    assert output.read_bytes() == same.read_bytes()


def test_rejects_wrong_code_head_without_output(code, tmp_path):
    with pytest.raises(RecordError, match="CODE_HEAD_MISMATCH"):
        archive_reader(code[0], "a" * 40, tmp_path / "bad.tar")
    assert not (tmp_path / "bad.tar").exists()


@pytest.mark.parametrize("attr", ["src/decision_kernel/__init__.py export-ignore\n",
                                  "src/decision_kernel/__init__.py export-subst\n"])
def test_archive_attributes_cannot_silently_omit_or_rewrite_committed_files(code, tmp_path, attr):
    root, _ = code
    (root / ".gitattributes").write_text(attr)
    (root / "src/decision_kernel/__init__.py").write_text("$Format:%H$\n")
    sha = commit(root, "synthetic archive attributes")
    with pytest.raises(RecordError, match="(BLOB_MISMATCH|FILES_MISMATCH)"):
        archive_reader(root, sha, tmp_path / "bad.tar")
    assert not (tmp_path / "bad.tar").exists()


def test_committed_symlink_is_not_an_exportable_reader(code, tmp_path):
    root, _ = code
    (root / "src/decision_kernel/link.py").symlink_to("/etc/passwd")
    sha = commit(root, "synthetic link")
    with pytest.raises(RecordError, match="COMMITTED_PATH_INVALID"):
        archive_reader(root, sha, tmp_path / "bad.tar")


def test_missing_required_configuration_refused(code, tmp_path):
    root, _ = code
    (root / "config/store-binding.json").unlink()
    sha = commit(root, "synthetic missing binding")
    with pytest.raises(RecordError, match="REQUIRED_PATH_MISSING"):
        archive_reader(root, sha, tmp_path / "bad.tar")


@pytest.mark.parametrize("damage", ["duplicate", "traversal", "absolute", "symlink", "hardlink", "unknown", "bytes", "commit"])
def test_member_validation_rejects_ambiguous_or_executable_filesystem_entries(damage):
    name, data, sha = "src/decision_kernel/example.py", b"synthetic\n", "a" * 40
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT,
                      pax_headers={"comment": "b" * 40 if damage == "commit" else sha}) as t:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        if damage == "traversal": member.name = "../escape.py"
        if damage == "absolute": member.name = "/escape.py"
        if damage == "unknown": member.name = "src/decision_kernel/other.py"
        if damage in ("symlink", "hardlink"):
            member.type = tarfile.SYMTYPE if damage == "symlink" else tarfile.LNKTYPE
            member.linkname = "/tmp/escape"
            member.size = 0
        t.addfile(member, io.BytesIO(b"corrupted\n" if damage == "bytes" else data))
        if damage == "duplicate": t.addfile(member, io.BytesIO(data))
    with pytest.raises(RecordError): inspect_archive(stream.getvalue(), code_sha=sha, expected={name: blob})


def test_archive_size_bound_before_tar_parse(monkeypatch):
    from decision_kernel.infrastructure import reader_archive
    monkeypatch.setattr(reader_archive, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(RecordError, match="ARCHIVE_BOUND"):
        inspect_archive(b"X" * 11, code_sha="a" * 40, expected={"README.md": "b" * 40})


def test_archive_binding_must_match_authenticated_snapshot_binding(code, tmp_path):
    from dataclasses import asdict, replace
    import json
    from decision_kernel.infrastructure.github_store import GitHubBinding
    root, _ = code
    binding = GitHubBinding("example/records", 1234, "records", "a" * 40, "test", visibility="public")
    (root / "config/store-binding.json").write_text(json.dumps(asdict(binding)))
    sha = commit(root, "synthetic valid binding")
    with pytest.raises(RecordError, match="BINDING_MISMATCH"):
        archive_reader(root, sha, tmp_path / "wrong.tar", binding=replace(binding, anchor="b" * 40))
    assert not (tmp_path / "wrong.tar").exists()
    assert archive_reader(root, sha, tmp_path / "right.tar", binding=binding)["code_sha"] == sha
