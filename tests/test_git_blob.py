"""Exact local blob bytes through the existing bounded, acquisition-free Git reader."""
import base64
import hashlib

import pytest

from decision_kernel.domain.records import MAX_BYTES, encoded
from decision_kernel.infrastructure import git_tree as local
from decision_kernel.infrastructure.git_tree import LocalTreeUnavailable
from test_git_tree import (
    git, no_network as no_network, object_id, reader, repository as repository, tree,
)


def response(oid, raw):
    return {"sha": oid, "size": len(raw), "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii")}


@pytest.mark.parametrize("raw", [b"", b"\x00\xff\xfeSynthetic\r\nbytes\x00\n"])
def test_exact_bytes_and_empty_blob_have_independent_receipts(repository, raw):
    oid = object_id(repository, "blob", raw)
    source = reader(repository)
    actual = source.read_blob(oid, len(raw))
    assert actual == response(oid, raw)
    assert oid == hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert source.blob_stats() == {"schema": "local-git-blob-v1", "answered": 1, "declined": 0}
    assert source.stats() == {"schema": "local-git-tree-v1", "answered": 0, "declined": 0}
    actual["content"] = "consumer mutation"
    source.blob_stats()["answered"] = 999
    assert source.read_blob(oid, len(raw)) == response(oid, raw)
    assert source.blob_stats()["answered"] == 2


def test_real_packed_blob_preserves_bytes_without_a_loose_object(repository):
    raw = b"Synthetic packed bytes.\x00\xff\r\n"
    oid = object_id(repository, "blob", raw)
    root = tree(repository, [("100644", "blob", oid, "packed.bin")])
    commit = git(repository, "commit-tree", root, data=b"Synthetic packed blob\n").decode().strip()
    git(repository, "update-ref", "refs/heads/synthetic-packed", commit)
    loose = repository / ".git" / "objects" / oid[:2] / oid[2:]
    assert loose.is_file()
    git(repository, "repack", "-a", "-d")
    assert not loose.exists()
    assert list((repository / ".git" / "objects" / "pack").glob("*.pack"))
    assert reader(repository).read_blob(oid, len(raw)) == response(oid, raw)


def test_raw_hard_limit_and_actual_json_response_keep_the_exact_boundary(repository):
    raw = b"\xff" * (2 * MAX_BYTES)
    oid = object_id(repository, "blob", raw)
    expected = response(oid, raw)
    response_size = len(encoded(expected))
    assert len(raw) == 4 * 1024 * 1024
    assert reader(repository, max_bytes=response_size).read_blob(oid, len(raw)) == expected
    source = reader(repository, max_bytes=response_size - 1)
    with pytest.raises(LocalTreeUnavailable):
        source.read_blob(oid, len(raw))
    assert source.blob_stats() == {"schema": "local-git-blob-v1", "answered": 0, "declined": 1}


@pytest.mark.parametrize("oid,size", [
    ("HEAD", 0), ("a" * 40 + "^{blob}", 0), ("A" * 40, 0),
    ("a" * 40, True), ("a" * 40, -1), ("a" * 40, 2 * MAX_BYTES + 1),
])
def test_invalid_identity_or_expected_size_never_starts_git(repository, monkeypatch, oid, size):
    source = reader(repository)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid blob query reached Git"))
    with pytest.raises(LocalTreeUnavailable):
        source.read_blob(oid, size)
    assert source.blob_stats() == {"schema": "local-git-blob-v1", "answered": 0, "declined": 1}


@pytest.mark.parametrize("kind", ["tree", "commit"])
def test_treeish_objects_are_never_peeled_into_blob_answers(repository, kind):
    oid = tree(repository)
    if kind == "commit":
        oid = git(repository, "commit-tree", oid, data=b"Synthetic blob type test\n").decode().strip()
    with pytest.raises(LocalTreeUnavailable, match="NOT_BLOB"):
        reader(repository).read_blob(oid, 0)


@pytest.mark.parametrize("failure", ["missing", "corrupt", "wrong-oid"])
def test_missing_or_corrupt_local_object_never_becomes_an_answer(repository, failure):
    raw = b"Synthetic exact bytes.\n"
    oid = object_id(repository, "blob", raw)
    location = repository / ".git" / "objects" / oid[:2] / oid[2:]
    location.unlink()
    if failure == "corrupt":
        location.write_bytes(b"not a Git object")
    elif failure == "wrong-oid":
        replacement = object_id(repository, "blob", b"x" * len(raw))
        other = repository / ".git" / "objects" / replacement[:2] / replacement[2:]
        location.write_bytes(other.read_bytes())
    source = reader(repository)
    with pytest.raises(LocalTreeUnavailable):
        source.read_blob(oid, len(raw))
    assert source.blob_stats() == {"schema": "local-git-blob-v1", "answered": 0, "declined": 1}


@pytest.mark.parametrize("difference", [-1, 1])
def test_validated_tree_size_must_match_the_actual_blob(repository, difference):
    raw = b"Synthetic size boundary.\n"
    oid = object_id(repository, "blob", raw)
    with pytest.raises(LocalTreeUnavailable):
        reader(repository).read_blob(oid, len(raw) + difference)


def test_replace_ref_cannot_substitute_different_bytes(repository, monkeypatch):
    raw = b"Original synthetic bytes.\n"
    oid = object_id(repository, "blob", raw)
    replacement = object_id(repository, "blob", b"Replacement synthetic bytes.\n")
    git(repository, "replace", oid, replacement)
    assert git(repository, "cat-file", "blob", oid) != raw
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    assert reader(repository).read_blob(oid, len(raw)) == response(oid, raw)
    assert git(repository, "rev-parse", "refs/replace/" + oid).decode().strip() == replacement


def test_partial_config_blocks_blob_lookup_without_relying_on_the_lazy_fetch_flag(
        repository, tmp_path, monkeypatch):
    oid = object_id(repository, "blob", b"Synthetic missing promisor blob.\n")
    (repository / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    included = tmp_path / "promisor-config"
    git(repository, "config", "--file", str(included), "remote.origin.promisor", "true")
    git(repository, "config", "include.path", str(included))
    git(repository, "config", "remote.origin.url", "https://synthetic.invalid/never-fetch.git")
    git(repository, "config", "protocol.https.allow", "always")
    calls, popen = [], local.subprocess.Popen

    def old_git(command, **kwargs):
        kwargs["env"].pop("GIT_NO_LAZY_FETCH")
        calls.append(command)
        return popen(command, **kwargs)

    monkeypatch.setattr(local.subprocess, "Popen", old_git)
    with pytest.raises(LocalTreeUnavailable, match="PARTIAL_REPOSITORY"):
        reader(repository).read_blob(oid, len(b"Synthetic missing promisor blob.\n"))
    assert len(calls) == 1 and "config" in calls[0]
    assert not (repository / ".git" / "objects" / oid[:2] / oid[2:]).exists()
