"""Exact local Git tree metadata, bounded refusal and no automatic acquisition."""
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure import git_tree as local
from decision_kernel.infrastructure.git_tree import LocalGitTree, LocalTreeUnavailable


def git(path, *args, data=None):
    env = {key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Synthetic", GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Synthetic", GIT_COMMITTER_EMAIL="test@example.invalid")
    done = subprocess.run(["git", "-C", str(path), *args], input=data,
        capture_output=True, env=env, timeout=10, check=True)
    return done.stdout


def object_id(path, kind, raw):
    return git(path, "hash-object", "-w", "--stdin", "-t", kind, data=raw).decode().strip()


def tree(path, rows=()):
    raw = b"".join(f"{mode} {kind} {sha}\t".encode() + name.encode("utf-8") + b"\0"
        for mode, kind, sha, name in rows)
    return git(path, "mktree", "--missing", "-z", data=raw).decode().strip()


def reader(path, **changes):
    return LocalGitTree(path, **{"max_entries": 100, "max_bytes": 16384, **changes})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Local tree tests cannot acquire remote objects")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def repository(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "--template=", "--object-format=sha1")
    return path


@pytest.fixture
def complete(repository):
    raw, unicode_raw = b"binary\0bytes\n", "合成资料\n".encode("utf-8")
    blob = object_id(repository, "blob", raw)
    unicode_blob = object_id(repository, "blob", unicode_raw)
    link = object_id(repository, "blob", b"nested/file.bin")
    empty = tree(repository)
    deep = tree(repository, [("100644", "blob", unicode_blob, "资料.txt")])
    nested = tree(repository, [("040000", "tree", deep, "deep"),
        ("100644", "blob", blob, "file.bin")])
    root = tree(repository, [("040000", "tree", empty, "empty"),
        ("120000", "blob", link, "link"), ("160000", "commit", "e" * 40, "module"),
        ("040000", "tree", nested, "nested"), ("100755", "blob", blob, "run.sh"),
        ("100644", "blob", blob, "store.json")])
    expected = {
        "empty": {"mode": "040000", "type": "tree", "sha": empty},
        "link": {"mode": "120000", "type": "blob", "sha": link, "size": len(b"nested/file.bin")},
        "module": {"mode": "160000", "type": "commit", "sha": "e" * 40},
        "nested": {"mode": "040000", "type": "tree", "sha": nested},
        "nested/deep": {"mode": "040000", "type": "tree", "sha": deep},
        "nested/deep/资料.txt": {"mode": "100644", "type": "blob", "sha": unicode_blob, "size": len(unicode_raw)},
        "nested/file.bin": {"mode": "100644", "type": "blob", "sha": blob, "size": len(raw)},
        "run.sh": {"mode": "100755", "type": "blob", "sha": blob, "size": len(raw)},
        "store.json": {"mode": "100644", "type": "blob", "sha": blob, "size": len(raw)},
    }
    return {"root": root, "blob": blob, "empty": empty, "nested": nested,
        "raw": raw, "expected": expected}


def test_complete_tree_preserves_empty_nested_nonascii_and_nonregular_entries(repository, complete):
    source = reader(repository)
    actual = source.read(complete["root"])
    assert actual == {"sha": complete["root"], "truncated": False,
        "tree": [{"path": path, **row} for path, row in complete["expected"].items()]}
    raw = complete["raw"]
    assert complete["blob"] == hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    assert source.stats() == {"schema": "local-git-tree-v1", "answered": 1, "declined": 0}
    # No result cache: a consumer cannot mutate a later answer or the counters.
    actual["tree"].clear()
    assert len(source.read(complete["root"])["tree"]) == 9
    source.stats()["answered"] = 999
    assert source.stats()["answered"] == 2


def test_actual_empty_tree_is_distinct_from_missing_output(repository, complete):
    assert reader(repository).read(complete["empty"]) == {
        "sha": complete["empty"], "truncated": False, "tree": []}


@pytest.mark.parametrize("bad", ["HEAD", "a" * 39, "a" * 41, "A" * 40, "a" * 40 + "^{tree}", None, True])
def test_only_an_exact_lowercase_sha_reaches_git(repository, monkeypatch, bad):
    source = reader(repository)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *a, **k: pytest.fail("invalid SHA reached Git"))
    with pytest.raises(LocalTreeUnavailable, match="INVALID_TREE"):
        source.read(bad)
    assert source.stats()["answered"] == 0 and source.stats()["declined"] == 1


@pytest.mark.parametrize("kind", ["blob", "commit"])
def test_treeish_peeling_cannot_substitute_for_an_actual_tree(repository, complete, kind):
    sha = (complete["blob"] if kind == "blob" else
        git(repository, "commit-tree", complete["root"], data=b"synthetic\n").decode().strip())
    with pytest.raises(LocalTreeUnavailable, match="NOT_TREE"):
        reader(repository).read(sha)


@pytest.mark.parametrize("missing", ["root", "nested", "blob"])
def test_missing_root_subtree_or_blob_never_returns_a_partial_tree(repository, complete, missing):
    sha = complete[missing]
    (repository / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
    source = reader(repository)
    with pytest.raises(LocalTreeUnavailable):
        source.read(complete["root"])
    assert source.stats() == {"schema": "local-git-tree-v1", "answered": 0, "declined": 1}


def test_missing_repository_declines(tmp_path):
    with pytest.raises(LocalTreeUnavailable):
        reader(tmp_path / "absent").read("a" * 40)


def test_entry_and_response_byte_limits_keep_exact_boundary(repository, complete):
    result = reader(repository).read(complete["root"])
    size, count = len(encoded(result)), len(result["tree"])
    assert reader(repository, max_entries=count, max_bytes=size).read(complete["root"]) == result
    for limits in ({"max_entries": count - 1}, {"max_bytes": size - 1}, {"max_bytes": 8}):
        source = reader(repository, **limits)
        with pytest.raises(LocalTreeUnavailable, match="LIMIT"):
            source.read(complete["root"])
        assert source.stats()["answered"] == 0 and source.stats()["declined"] == 1
    with pytest.raises(LocalTreeUnavailable, match="OUTPUT_LIMIT"):
        reader(repository, max_bytes=8).read(complete["empty"])


@pytest.mark.parametrize("options", [{"max_entries": 0}, {"max_entries": True}, {"max_bytes": -1},
    {"max_bytes": True}, {"timeout": 0}, {"timeout": True}, {"timeout": float("nan")},
    {"timeout": float("inf")}, {"timeout": "1"}])
def test_invalid_limits_are_configuration_errors(repository, options):
    with pytest.raises(RecordError):
        reader(repository, **options)


@pytest.mark.parametrize("raw", [
    b"100644 blob " + b"a" * 40 + b"       1\tfile",  # Missing final NUL.
    b"100644 blob " + b"a" * 40 + b"       1\tfile\0\0",
    b"100644 blob " + b"a" * 40 + b"       1\tbad-\xff\0",
    b"100644 blob " + b"a" * 39 + b"       1\tfile\0",
    b"100644 blob " + b"a" * 40 + b"       -\tfile\0",
    b"100644 blob " + b"a" * 40 + b"     BAD\tfile\0",
    b"100644 blob " + b"a" * 40 + b"      -1\tfile\0",
    b"100644 blob " + b"a" * 40 + b" 18446744073709551616\tfile\0",
    b"040000 tree " + b"a" * 40 + b"       1\tdir\0",
    b"100644 blob " + b"a" * 40 + b"       1\t\0",
])
def test_success_exit_with_malformed_or_unavailable_metadata_still_declines(repository, monkeypatch, raw):
    source = reader(repository)
    def native(*args, **kwargs):
        return (1, b"") if args[0] == "config" else (0, b"tree\n" if args[0] == "cat-file" else raw)
    monkeypatch.setattr(source, "_git", native)
    with pytest.raises(LocalTreeUnavailable):
        source.read("a" * 40)
    assert source.stats()["answered"] == 0


def simple_tree(path):
    blob = object_id(path, "blob", b"original\n")
    sha = tree(path, [("100644", "blob", blob, "store.json")])
    return sha, blob


def test_dirty_index_worktree_and_current_subdirectory_do_not_change_the_object(repository):
    sha, blob = simple_tree(repository)
    commit = git(repository, "commit-tree", sha, data=b"synthetic\n").decode().strip()
    git(repository, "update-ref", "refs/heads/main", commit)
    git(repository, "symbolic-ref", "HEAD", "refs/heads/main")
    git(repository, "reset", "--hard", "HEAD")
    (repository / "store.json").write_text("staged change\n")
    git(repository, "add", "store.json")
    (repository / "store.json").write_text("unstaged change\n")
    child = repository / "untracked"
    child.mkdir()
    result = reader(child).read(sha)
    assert result["tree"] == [{"path": "store.json", "mode": "100644", "type": "blob",
        "sha": blob, "size": len(b"original\n")}]
    assert (repository / "store.json").read_text() == "unstaged change\n"


@pytest.mark.parametrize("layout", ["bare", "linked-worktree"])
def test_native_repository_layouts_are_supported(repository, tmp_path, layout):
    if layout == "bare":
        path = tmp_path / "bare.git"
        path.mkdir()
        git(path, "init", "--bare", "-q", "--template=", "--object-format=sha1")
        sha, blob = simple_tree(path)
    else:
        sha, blob = simple_tree(repository)
        commit = git(repository, "commit-tree", sha, data=b"synthetic\n").decode().strip()
        git(repository, "update-ref", "refs/heads/main", commit)
        path = tmp_path / "linked"
        git(repository, "worktree", "add", "--detach", str(path), commit)
    assert reader(path).read(sha)["tree"][0]["sha"] == blob


def test_replacements_and_inherited_git_or_credential_overrides_cannot_rewrite_answer(repository, tmp_path, monkeypatch):
    sha, blob = simple_tree(repository)
    replacement_blob = object_id(repository, "blob", b"replacement bytes\n")
    replacement = tree(repository, [("100644", "blob", replacement_blob, "replacement.json")])
    git(repository, "replace", sha, replacement)
    assert b"replacement.json" in git(repository, "ls-tree", "-r", sha)
    bad_config = tmp_path / "invalid-config"
    bad_config.write_text("not valid Git configuration\n")
    for key, value in {"GIT_DIR": str(tmp_path / "wrong.git"), "GIT_WORK_TREE": str(tmp_path),
            "GIT_OBJECT_DIRECTORY": str(tmp_path), "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(tmp_path),
            "GIT_CONFIG_GLOBAL": str(bad_config), "GIT_CONFIG_SYSTEM": str(bad_config),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.abbrev", "GIT_CONFIG_VALUE_0": "4",
            "GIT_CONFIG_PARAMETERS": "invalid override", "GIT_NO_REPLACE_OBJECTS": "0",
            "GIT_NO_LAZY_FETCH": "0", "GIT_ALLOW_PROTOCOL": "https:file:ext",
            "GIT_TRACE": str(tmp_path / "trace"), "SSH_ASKPASS": "untrusted-helper",
            "SSH_AUTH_SOCK": "untrusted-agent", "DECISION_OS_STORE_TOKEN": "synthetic-secret",
            "ARBITRARY_PROVIDER_CREDENTIAL": "synthetic-source-secret"}.items():
        monkeypatch.setenv(key, value)
    calls, popen = [], local.subprocess.Popen
    def observed(command, **kwargs):
        calls.append((command, kwargs))
        return popen(command, **kwargs)
    monkeypatch.setattr(local.subprocess, "Popen", observed)
    actual = reader(repository).read(sha)
    assert actual["tree"][0]["sha"] == blob and actual["tree"][0]["path"] == "store.json"
    assert not (tmp_path / "trace").exists()
    assert len(calls) == 3
    for command, kwargs in calls:
        env = kwargs["env"]
        assert kwargs["shell"] is False and kwargs["stdin"] == subprocess.DEVNULL
        assert "--no-replace-objects" in command and env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_ALLOW_PROTOCOL"] == ""
        assert env["GIT_CONFIG_GLOBAL"] == env["GIT_CONFIG_SYSTEM"] == os.devnull
        assert not any(key in env for key in ("GIT_DIR", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "SSH_AUTH_SOCK", "SSH_ASKPASS",
            "DECISION_OS_STORE_TOKEN", "ARBITRARY_PROVIDER_CREDENTIAL"))


@pytest.mark.parametrize("location,key,value", [
    ("local", "remote.origin.promisor", "true"),
    ("local", "remote.origin.partialCloneFilter", "blob:none"),
    ("local", "extensions.partialClone", "origin"),
    ("include", "remote.origin.promisor", "true"),
    ("worktree", "remote.origin.promisor", "true"),
])
def test_partial_repositories_decline_before_object_lookup_even_without_lazy_fetch_flag(
        repository, tmp_path, monkeypatch, location, key, value):
    sha, blob = simple_tree(repository)
    (repository / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    git(repository, "config", "remote.origin.url", "https://synthetic.invalid/never-fetch.git")
    git(repository, "config", "protocol.https.allow", "always")
    if location == "include":
        included = tmp_path / "partial-config"
        git(repository, "config", "--file", str(included), key, value)
        git(repository, "config", "include.path", str(included))
    elif location == "worktree":
        git(repository, "config", "extensions.worktreeConfig", "true")
        git(repository, "config", "--worktree", key, value)
    else:
        git(repository, "config", key, value)
    calls, popen = [], local.subprocess.Popen
    def old_git(command, **kwargs):
        # The conservative config gate does not depend on newer Git recognizing
        # GIT_NO_LAZY_FETCH. No object query or helper may run in this case.
        kwargs["env"].pop("GIT_NO_LAZY_FETCH")
        calls.append(command)
        return popen(command, **kwargs)
    monkeypatch.setattr(local.subprocess, "Popen", old_git)
    with pytest.raises(LocalTreeUnavailable, match="PARTIAL_REPOSITORY"):
        reader(repository).read(sha)
    assert len(calls) == 1 and "config" in calls[0]
    assert not (repository / ".git" / "objects" / blob[:2] / blob[2:]).exists()


@pytest.mark.parametrize("failure", ["stdout", "stderr", "entries", "timeout", "exit"])
def test_child_output_and_time_are_bounded_before_any_answer(repository, monkeypatch, failure):
    sha, _ = simple_tree(repository)
    row = b"100644 blob " + b"a" * 40 + b"       1\tfile\0"
    if failure == "stdout":
        body = "sys.stdout.buffer.write(b'x' * 65536); sys.stdout.flush(); time.sleep(30)"
    elif failure == "stderr":
        body = "sys.stderr.buffer.write(b'x' * 65536); sys.stderr.flush(); time.sleep(30)"
    elif failure == "entries":
        body = "sys.stdout.buffer.write(b'\\0' * 2048); sys.stdout.flush(); time.sleep(30)"
    elif failure == "timeout":
        body = "sys.stdout.buffer.write(" + repr(row) + "); sys.stdout.flush(); time.sleep(30)"
    else:
        body = "sys.stdout.buffer.write(" + repr(row) + "); sys.stdout.flush(); sys.exit(7)"
    children, popen = [], local.subprocess.Popen
    def controlled(command, **kwargs):
        if "ls-tree" in command:
            command = [sys.executable, "-c", "import sys, time; " + body]
        process = popen(command, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(local.subprocess, "Popen", controlled)
    source = reader(repository, max_entries=100, max_bytes=4096, timeout=1)
    start = time.monotonic()
    with pytest.raises(LocalTreeUnavailable) as refused:
        source.read(sha)
    assert time.monotonic() - start < 5
    expected = {"stdout": "OUTPUT_LIMIT", "stderr": "OUTPUT_LIMIT", "entries": "ENTRY_LIMIT",
        "timeout": "TIMEOUT", "exit": "GIT_FAILED"}[failure]
    assert refused.value.reason == "LOCAL_TREE_" + expected
    assert children and all(child.poll() is not None for child in children)
    assert source.stats() == {"schema": "local-git-tree-v1", "answered": 0, "declined": 1}
