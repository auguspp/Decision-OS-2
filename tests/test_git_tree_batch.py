"""Operation-scoped object process reuse keeps the existing bounded answers."""
import subprocess
import sys

import pytest

from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure import git_tree as local
from decision_kernel.infrastructure.git_tree import LocalTreeUnavailable
from test_git_blob import response
from test_git_tree import (
    complete as complete, git, no_network as no_network, object_id, reader,
    repository as repository,
)


def test_one_channel_preserves_full_tree_and_blob_answers(repository, complete, monkeypatch):
    baseline = reader(repository).read(complete["root"])
    calls, native = [], local.subprocess.Popen

    def start(command, **kwargs):
        calls.append((command, kwargs))
        return native(command, **kwargs)

    monkeypatch.setenv("DECISION_OS_READ_TOKEN", "synthetic-not-in-child")
    monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
    monkeypatch.setattr(local.subprocess, "Popen", start)
    source = reader(repository, reuse_processes=True)
    try:
        assert source.read(complete["root"]) == baseline
        for row in baseline["tree"]:
            if row["type"] == "blob":
                raw = git(repository, "cat-file", "blob", row["sha"])
                # Fixture queries are not part of the accelerator's count.
                calls.pop()
                assert source.read_blob(row["sha"], len(raw)) == response(row["sha"], raw)
        assert source.read(complete["root"]) == baseline
        stats = source.process_stats()
        assert stats["mode"] == "batch"
        assert stats["processes"] == len(calls) == 5  # config, probe, batch, two ls-tree
        assert stats["batch_processes"] == 1
        assert stats["batch_requests"] == 7
        batch = [call for call in calls if "--batch-command" in call[0]]
        assert len(batch) == 1 and batch[0][1]["stdin"] == subprocess.PIPE
        assert sum("config" in command for command, _ in calls) == 1
        for command, kwargs in calls:
            assert kwargs["shell"] is False
            assert "DECISION_OS_READ_TOKEN" not in kwargs["env"]
            assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
            assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
            if "ls-tree" in command or "--batch-command" in command:
                assert "--no-lazy-fetch" in command
    finally:
        source.close()
    source.close()
    before = len(calls)
    with pytest.raises(LocalTreeUnavailable, match="CLOSED"):
        source.read(complete["root"])
    assert len(calls) == before


def test_unsupported_native_no_fetch_switch_keeps_old_local_path(repository, monkeypatch):
    raws = [b"first synthetic blob\n", b"second synthetic blob\0"]
    objects = [(object_id(repository, "blob", raw), raw) for raw in raws]
    calls, native = [], local.subprocess.Popen

    def old_git(command, **kwargs):
        calls.append(command)
        # Real exit status, without a platform-specific executable wrapper.
        if "--version" in command:
            return native([sys.executable, "-c", "raise SystemExit(129)"], **kwargs)
        assert "--no-lazy-fetch" not in command and "--batch-command" not in command
        kwargs["env"].pop("GIT_NO_LAZY_FETCH", None)
        return native(command, **kwargs)

    monkeypatch.setattr(local.subprocess, "Popen", old_git)
    source = reader(repository, reuse_processes=True)
    try:
        for oid, raw in objects:
            assert source.read_blob(oid, len(raw)) == response(oid, raw)
        assert sum("--version" in command for command in calls) == 1
        assert sum("config" in command for command in calls) == 2
        assert source.process_stats() == {"schema": "local-git-process-v1",
            "processes": 7, "batch_processes": 0, "batch_requests": 0, "mode": "legacy"}
        git(repository, "config", "remote.origin.promisor", "true")
        with pytest.raises(LocalTreeUnavailable, match="PARTIAL_REPOSITORY"):
            source.read_blob(objects[0][0], len(raws[0]))
        assert calls[-1][calls[-1].index("config") + 1] == "--includes"
    finally:
        source.close()


@pytest.mark.parametrize("limit", ["entries", "tree-bytes", "blob-bytes", "blob-size"])
def test_modern_path_preserves_tree_and_body_bounds(repository, complete, limit):
    baseline = reader(repository).read(complete["root"])
    size = len(encoded(baseline))
    changes = {"reuse_processes": True}
    if limit == "entries":
        changes["max_entries"] = len(baseline["tree"]) - 1
    elif limit == "tree-bytes":
        changes["max_bytes"] = size - 1
    elif limit == "blob-bytes":
        changes["max_bytes"] = len(encoded(response(complete["blob"], complete["raw"]))) - 1
    source = reader(repository, **changes)
    try:
        with pytest.raises(LocalTreeUnavailable):
            if limit.startswith("blob"):
                source.read_blob(complete["blob"], len(complete["raw"]) + (limit == "blob-size"))
            else:
                source.read(complete["root"])
    finally:
        source.close()


def test_partial_repository_is_refused_before_probe_or_batch(repository, monkeypatch, tmp_path):
    raw = b"synthetic\n"
    oid = object_id(repository, "blob", raw)
    included = tmp_path / "included-config"
    git(repository, "config", "--file", str(included), "remote.origin.promisor", "true")
    git(repository, "config", "include.path", str(included))
    calls, native = [], local.subprocess.Popen

    def start(command, **kwargs):
        calls.append(command)
        return native(command, **kwargs)

    monkeypatch.setattr(local.subprocess, "Popen", start)
    source = reader(repository, reuse_processes=True)
    try:
        with pytest.raises(LocalTreeUnavailable, match="PARTIAL_REPOSITORY"):
            source.read_blob(oid, len(raw))
        assert len(calls) == 1 and "config" in calls[0]
        assert source.process_stats()["batch_processes"] == 0
    finally:
        source.close()


def test_modern_channel_does_not_follow_replacement_refs(repository):
    raw = b"original synthetic bytes\n"
    oid = object_id(repository, "blob", raw)
    replacement = object_id(repository, "blob", b"replacement synthetic bytes\n")
    git(repository, "replace", oid, replacement)
    source = reader(repository, reuse_processes=True)
    try:
        assert source.read_blob(oid, len(raw)) == response(oid, raw)
    finally:
        source.close()
