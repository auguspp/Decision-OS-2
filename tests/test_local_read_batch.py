"""One local command owns its shared Git reader and always ends that session."""
import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from decision_kernel.infrastructure.git_ancestry import AncestryUnavailable, LocalGitAncestry
from decision_kernel.infrastructure.git_tree import LocalGitTree, LocalTreeUnavailable, PROCESS_SCHEMA
from decision_kernel.infrastructure.github_store import GitHubAPI, MAX_RESPONSE, MAX_TREE_ENTRIES
from decision_kernel.interfaces import local_read, research_commands
from test_git_ancestry import commit, git, mirror
from test_hosted_watch_runtime import public_host
from test_local_read import console, records, run_console
from test_news_read_budget import transport
from test_research_runtime import host


@pytest.fixture
def graph_objects(mirror):
    body = b"Synthetic exact object.\x00\xff\n"
    (mirror / "synthetic.bin").write_bytes(body)
    git(mirror, "add", "synthetic.bin")
    first = commit(mirror, "first")
    second = commit(mirror, "second")
    tree = git(mirror, "rev-parse", first + "^{tree}")
    blob = git(mirror, "rev-parse", first + ":synthetic.bin")
    git(mirror, "checkout", "-q", "--orphan", "aside")
    stranger = commit(mirror, "unrelated")
    return SimpleNamespace(path=mirror, body=body, first=first, second=second,
                           stranger=stranger, tree=tree, blob=blob)


def test_shared_reader_preserves_exact_types_and_ancestry_three_way_answer(graph_objects):
    objects = graph_objects
    reader = LocalGitTree(objects.path, max_entries=MAX_TREE_ENTRIES,
                          max_bytes=MAX_RESPONSE, reuse_processes=True)
    graph = LocalGitAncestry(objects.path, object_reader=reader)
    try:
        assert graph.object_reader is reader and graph.available()
        for oid, kind in ((objects.first, "commit"), (objects.tree, "tree"),
                          (objects.blob, "blob"), ("f" * 40, None)):
            assert reader.object_type(oid, timeout=5) == kind
            assert graph.has_commit(oid) is (kind == "commit")
        assert reader.read(objects.tree)["sha"] == objects.tree
        raw = reader.read_blob(objects.blob, len(objects.body))
        assert base64.b64decode(raw["content"]) == objects.body
        assert graph.ref_commit("HEAD") == objects.stranger
        assert graph.ref_commit("refs/heads/absent") is None
        assert graph.is_ancestor(objects.first, objects.second) is True
        assert graph.is_ancestor(objects.stranger, objects.second) is False
        with pytest.raises(AncestryUnavailable, match="LOCAL_ANCESTRY_OBJECT_MISSING"):
            graph.is_ancestor(objects.first, "f" * 40)
        assert graph.stats() == {"schema": "local-git-ancestry-v1", "answered": 2, "declined": 1}
        # A missing exact object is an answer, not a poisoned protocol session.
        assert graph.has_commit(objects.second)
    finally:
        reader.close()


def test_shared_reader_failure_remains_unavailable_and_does_not_change_standalone(graph_objects):
    objects = graph_objects
    reader = LocalGitTree(objects.path, max_entries=MAX_TREE_ENTRIES,
                          max_bytes=MAX_RESPONSE, reuse_processes=True)
    graph = LocalGitAncestry(objects.path, object_reader=reader)
    reader.close()
    assert graph.available() is False
    with pytest.raises(AncestryUnavailable, match="LOCAL_ANCESTRY_GIT_UNAVAILABLE"):
        graph.has_commit(objects.first)
    with pytest.raises(AncestryUnavailable, match="LOCAL_ANCESTRY_GIT_UNAVAILABLE"):
        graph.is_ancestor(objects.first, objects.second)
    assert graph.stats()["answered"] == 0 and graph.stats()["declined"] == 1
    standalone = LocalGitAncestry(objects.path)
    assert standalone.object_reader is None and standalone.available()
    assert standalone.is_ancestor(objects.first, objects.second) is True


@pytest.fixture
def sessions(monkeypatch):
    """Observe actual accelerator children, excluding the synthetic API's Git."""
    state = SimpleNamespace(readers=[], launches=[], close_error=None)
    create, spawn = local_read.LocalGitTree, subprocess.Popen

    def reader(*args, **kwargs):
        actual = create(*args, **kwargs)
        item = SimpleNamespace(reader=actual, closes=0)
        close = actual.close

        def finish():
            item.closes += 1
            close()
            if state.close_error is not None:
                raise state.close_error

        monkeypatch.setattr(actual, "close", finish)
        state.readers.append(item)
        return actual

    def popen(command, *args, **kwargs):
        process = spawn(command, *args, **kwargs)
        if isinstance(command, (list, tuple)) and "--no-replace-objects" in command:
            state.launches.append((tuple(command), process))
        return process

    monkeypatch.setattr(local_read, "LocalGitTree", reader)
    monkeypatch.setattr(subprocess, "Popen", popen)
    yield state
    # Keep a failing assertion from leaking a child into another test.
    for item in state.readers:
        local_read._close(item.reader)


def assert_closed(sessions):
    assert sessions.readers and sessions.launches
    assert all(item.closes == 1 for item in sessions.readers)
    assert all(process.poll() is not None for _, process in sessions.launches)
    starts = len(sessions.launches)
    for item in sessions.readers:
        with pytest.raises(LocalTreeUnavailable, match="LOCAL_TREE_CLOSED"):
            item.reader.object_type("a" * 40, timeout=1)
    assert len(sessions.launches) == starts
    assert local_read.STORE_TOKEN not in os.environ


def read_receipt(captured):
    return json.loads(captured.err.splitlines()[-1])["local_read_receipt"]


@pytest.mark.parametrize("deny_after_read", [False, True])
def test_actual_console_closes_shared_session_on_success_or_native_denial(
        console, records, sessions, monkeypatch, capsys, deny_after_read):
    execute = research_commands.execute

    def completed(*args, **kwargs):
        result = execute(*args, **kwargs)
        if deny_after_read:
            console.api.meta["private"] = True
        return result

    monkeypatch.setattr(research_commands, "execute", completed)
    status = run_console(console, "research-catalogue", "--git-mirror", str(console.api.git.root))
    captured = capsys.readouterr()
    assert status == (2 if deny_after_read else 0)
    if deny_after_read:
        assert captured.out == ""
        assert json.loads(captured.err.splitlines()[0]) == {"error": "LOCAL_READ_PUBLIC_STORE_ONLY"}
    else:
        assert json.loads(captured.out)["snapshot"] == records.snapshot
    receipt = read_receipt(captured)
    assert receipt["blob_content"]["answered"] > 0 and receipt["ancestry"]["answered"] > 0
    process = receipt["git_process"]
    assert process["schema"] == PROCESS_SCHEMA and process["mirror"] == "local-git-graph"
    assert process["processes"] == len(sessions.launches)
    batches = sum("--batch-command" in command for command, _ in sessions.launches)
    assert process["batch_processes"] == batches
    assert process["mode"] == ("batch" if batches else "legacy")
    assert (process["batch_requests"] > 0) is bool(batches)
    assert len(sessions.readers) == 1
    assert_closed(sessions)
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_actual_console_closes_and_preserves_interrupt_after_local_body(
        console, records, sessions, monkeypatch, capsys, error):
    read = LocalGitTree.read_blob
    interrupted = error("synthetic interruption")
    state = {}

    def interrupt(self, *args, **kwargs):
        read(self, *args, **kwargs)
        state["native_requests"] = len(console.api.requests)
        raise interrupted

    monkeypatch.setattr(LocalGitTree, "read_blob", interrupt)
    with pytest.raises(error) as caught:
        run_console(console, "research-catalogue", "--git-mirror", str(console.api.git.root))
    assert caught.value is interrupted
    captured = capsys.readouterr()
    assert captured.out == ""
    receipt = read_receipt(captured)
    assert receipt["exit_code"] == 2 and receipt["blob_content"]["answered"] == 1
    assert len(console.api.requests) == state["native_requests"]
    assert_closed(sessions)
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


def test_receipt_write_failure_still_cleans_token_and_children(
        console, records, sessions, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DECISION_OS_READ_RECEIPT", str(tmp_path))
    with pytest.raises(OSError):
        run_console(console, "research-catalogue", "--git-mirror", str(console.api.git.root))
    captured = capsys.readouterr()
    assert json.loads(captured.out)["snapshot"] == records.snapshot
    assert read_receipt(captured)["exit_code"] == 0
    assert_closed(sessions)


def test_cleanup_error_cannot_hide_success_or_prevent_receipt(
        console, records, sessions, capsys):
    sessions.close_error = RuntimeError("synthetic cleanup failure")
    assert run_console(console, "research-catalogue", "--git-mirror", str(console.api.git.root)) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["snapshot"] == records.snapshot
    assert read_receipt(captured)["exit_code"] == 0
    assert_closed(sessions)


def test_invalid_mirror_is_closed_before_native_read_or_execution_receipt(
        console, sessions, tmp_path, capsys):
    plain = tmp_path / "not-a-repository"
    plain.mkdir()
    before = len(console.api.requests)
    assert run_console(console, "research-catalogue", "--git-mirror", str(plain)) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error": "LOCAL_READ_GIT_MIRROR_NOT_A_REPOSITORY"}
    assert len(console.api.requests) == before
    assert sessions.readers[0].reader.process_stats()["mode"] == "unprobed"
    assert_closed(sessions)


def test_next_console_gets_new_session_and_rechecks_changed_partial_configuration(
        console, records, sessions, monkeypatch, capsys):
    command = ("research-catalogue", "--git-mirror", str(console.api.git.root))
    assert run_console(console, *command) == 0
    first = capsys.readouterr()
    first_receipt = read_receipt(first)
    assert first_receipt["blob_content"]["answered"] > 0
    assert_closed(sessions)
    boundary = len(sessions.launches)

    # Only the temporary synthetic repository changes. A new command must not
    # inherit its predecessor's qualification or reuse that command's process.
    console.api.git._run("config", "remote.synthetic.promisor", "true")
    console.api.requests.clear()
    console.wire = transport(console.api)
    monkeypatch.setattr(GitHubAPI, "from_environment", lambda: console.wire)
    assert run_console(console, *command) == 0
    second = capsys.readouterr()
    second_receipt = read_receipt(second)
    assert second.out == first.out
    assert len(sessions.readers) == 2
    assert sessions.readers[0].reader is not sessions.readers[1].reader
    assert all(any("config" in command for command, _ in launches) for launches in (
        sessions.launches[:boundary], sessions.launches[boundary:]))
    assert second_receipt["git_process"]["processes"] == len(sessions.launches) - boundary
    assert second_receipt["git_process"]["batch_processes"] == 0
    for key in ("ancestry", "tree_inventory", "blob_content"):
        assert second_receipt[key]["declined"] > 0
    assert second_receipt["tree_inventory"]["answered"] == 0
    assert second_receipt["blob_content"]["answered"] == 0
    assert any("/compare/" in path for _, path, _ in console.api.requests)
    assert any("/git/trees/" in path for _, path, _ in console.api.requests)
    assert any("/git/blobs/" in path for _, path, _ in console.api.requests)
    assert all(method == "GET" for method, _, _ in console.api.requests)
    assert console.wire._requests == len(console.api.requests) <= 512
    assert_closed(sessions)
    assert console.api.git.head() == records.snapshot and console.api.writes == 0


def test_absent_process_diagnostic_has_no_claim_of_a_session(console, capsys):
    assert run_console(console, "research-catalogue") == 0
    receipt = read_receipt(capsys.readouterr())
    assert receipt["git_process"] == {"schema": PROCESS_SCHEMA, "processes": 0,
        "batch_processes": 0, "batch_requests": 0, "mode": "unprobed", "mirror": "absent"}
