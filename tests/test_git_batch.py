"""Bounded binary cat-file exchanges and process ownership, with real pipes."""
import hashlib
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from decision_kernel.infrastructure import git_batch as batch
from decision_kernel.infrastructure.git_tree import _environment
from test_git_tree import git, no_network as no_network, object_id, repository as repository, tree

RAW = b"Synthetic\x00\xff\r\nblob bytes.\n"
OID = hashlib.sha1(b"blob " + str(len(RAW)).encode() + b"\0" + RAW).hexdigest()
HEADER = f"{OID} blob {len(RAW)}\n".encode()


def deadline(seconds=10):
    return time.monotonic() + seconds


def command(repository):
    return ["git", "--no-pager", "--no-replace-objects", "-c", "protocol.allow=never",
            "-C", str(repository), "cat-file", "--batch-command"]


@pytest.fixture
def sessions(monkeypatch):
    state = SimpleNamespace(children=[], commands=[], attempts=0, wrap=None, opened=[])
    popen = batch.subprocess.Popen

    def observed(argv, *args, **kwargs):
        selected = "--batch-command" in argv or argv[:2] == [sys.executable, "-u"]
        if selected:
            state.attempts += 1
            state.commands.append((list(argv), kwargs))
        process = popen(argv, *args, **kwargs)
        if selected:
            state.children.append(process)
            if state.wrap is not None:
                state.wrap(process)
        return process

    def open_session(argv):
        session = batch.GitObjectBatch(argv, env=_environment())
        state.opened.append(session)
        return session

    state.open = open_session
    monkeypatch.setattr(batch.subprocess, "Popen", observed)
    yield state
    for session in state.opened:
        session.close()
    # A failing assertion must not leave a synthetic child behind. Tests assert
    # normal cleanup before this emergency fixture cleanup can run.
    for process in state.children:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def fake(sessions, body):
    return sessions.open([sys.executable, "-u", "-c", "import sys, time\n" + body])


def assert_stopped(session, sessions):
    assert all(process.poll() is not None for process in sessions.children)
    assert all(process.stdin.closed and process.stdout.closed and process.stderr.closed
               for process in sessions.children)
    assert all(thread is None or not thread.is_alive()
               for thread in (session._worker, session._stderr_reader))


def assert_poisoned(session, sessions):
    counts, attempts = session.stats(), sessions.attempts
    assert_stopped(session, sessions)
    with pytest.raises(batch.GitBatchUnavailable):
        session.info(OID, deadline=deadline())
    with pytest.raises(batch.GitBatchUnavailable):
        session.blob(OID, len(RAW), deadline=deadline())
    assert session.stats() == counts and sessions.attempts == attempts
    session.close()
    session.close()
    assert_stopped(session, sessions)


def test_real_packed_objects_share_one_lazy_process_and_missing_keeps_frames_synchronized(
        repository, sessions):
    # Exercise native argv/path handling without shell quoting or POSIX-only I/O.
    moved = repository.with_name("synthetic mirror 空格")
    repository.rename(moved)
    repository = moved
    values = [b"", RAW, b"CRLF\r\nNUL\0end", b"a" * 40 + b" blob 3\nxyz\n",
              *[f"Unique retained synthetic blob {number}\n".encode() for number in range(16)]]
    objects = [(object_id(repository, "blob", raw), raw) for raw in values]
    root = tree(repository, [("100644", "blob", oid, f"object-{index}.bin")
                              for index, (oid, _) in enumerate(objects)])
    commit = git(repository, "commit-tree", root, data=b"Synthetic packed batch\n").decode().strip()
    git(repository, "update-ref", "refs/heads/synthetic-batch", commit)
    git(repository, "repack", "-a", "-d")
    assert all(not (repository / ".git" / "objects" / oid[:2] / oid[2:]).exists() for oid, _ in objects)
    session = sessions.open(command(repository))
    assert session.stats() == {"processes": 0, "requests": 0} and sessions.children == []
    for oid, raw in objects:
        assert session.info(oid, deadline=deadline()) == ("blob", len(raw))
        assert session.blob(oid, len(raw), deadline=deadline()) == raw
    assert session.info(root, deadline=deadline()) == ("tree", len(git(repository, "cat-file", "tree", root)))
    assert session.info(commit, deadline=deadline()) == ("commit", len(git(repository, "cat-file", "commit", commit)))
    missing = "f" * 40
    assert session.info(missing, deadline=deadline()) is None
    assert session.blob(missing, 0, deadline=deadline()) is None
    assert session.blob(objects[1][0], len(values[1]), deadline=deadline()) == values[1]
    assert session.stats() == {"processes": 1, "requests": 45}
    assert sessions.attempts == len(sessions.children) == 1
    session.stats()["requests"] = 999
    assert session.stats()["requests"] == 45
    session.close()
    assert_stopped(session, sessions)
    assert_poisoned(session, sessions)


def test_real_blob_at_four_mebibytes_is_returned_exactly(repository, sessions):
    raw = b"\xff" * (4 * 1024 * 1024)
    oid = object_id(repository, "blob", raw)
    session = sessions.open(command(repository))
    assert session.blob(oid, len(raw), deadline=deadline()) == raw
    assert session.stats() == {"processes": 1, "requests": 1}
    session.close()
    assert_stopped(session, sessions)


@pytest.mark.parametrize("operation,oid,size", [
    ("info", "HEAD", None), ("info", "A" * 40, None), ("info", "a" * 40 + "^{blob}", None),
    ("blob", OID, True), ("blob", OID, -1), ("blob", OID, 4 * 1024 * 1024 + 1),
])
def test_invalid_object_or_oversized_body_never_starts_a_process(sessions, operation, oid, size):
    session = fake(sessions, "raise RuntimeError('invalid input must never start this child')\n")
    with pytest.raises(batch.GitBatchUnavailable):
        if operation == "info":
            session.info(oid, deadline=deadline())
        else:
            session.blob(oid, size, deadline=deadline())
    assert sessions.attempts == 0 and session.stats() == {"processes": 0, "requests": 0}


@pytest.mark.parametrize("size,accepted", [(2**64 - 1, True), (2**64, False)])
def test_metadata_header_has_an_explicit_unsigned_64_bit_size_boundary(sessions, size, accepted):
    header = f"{OID} blob {size}\n".encode()
    session = fake(sessions, "sys.stdin.buffer.readline()\n"
        + f"sys.stdout.buffer.write({header!r}); sys.stdout.buffer.flush()\n"
        + "sys.stdin.buffer.read()\n")
    if accepted:
        assert session.info(OID, deadline=deadline()) == ("blob", size)
        session.close()
        assert_stopped(session, sessions)
    else:
        with pytest.raises(batch.GitBatchUnavailable):
            session.info(OID, deadline=deadline())
        assert_poisoned(session, sessions)


@pytest.mark.parametrize("name,wire,ends", [
    ("wrong-oid", b"a" * 40 + f" blob {len(RAW)}\n".encode() + RAW + b"\n", False),
    ("wrong-type", f"{OID} tree {len(RAW)}\n".encode() + RAW + b"\n", False),
    ("invalid-size", f"{OID} blob -1\n".encode(), False),
    ("different-size", f"{OID} blob {len(RAW) + 1}\n".encode(), False),
    ("overlong-header", b"x" * 129, False),
    ("header-eof", HEADER[:17], True),
    ("body-eof", HEADER + RAW[:-1], True),
    ("missing-lf", HEADER + RAW, True),
    ("wrong-separator", HEADER + RAW + b"\0", False),
    ("wrong-digest", HEADER + b"x" * len(RAW) + b"\n", False),
    ("foreign-missing", b"a" * 40 + b" missing\n", False),
])
def test_broken_exchange_poison_prevents_later_reads_or_process_restarts(sessions, name, wire, ends):
    session = fake(sessions, "sys.stdin.buffer.readline()\n"
        + f"sys.stdout.buffer.write({wire!r}); sys.stdout.buffer.flush()\n"
        + ("sys.exit(0)\n" if ends else "sys.stdin.buffer.read()\n"))
    with pytest.raises(batch.GitBatchUnavailable) as refused:
        session.blob(OID, len(RAW), deadline=deadline())
    assert refused.value.reason.startswith("GIT_BATCH_")
    assert session.stats() == {"processes": 1, "requests": 1}
    assert_poisoned(session, sessions)


@pytest.mark.parametrize("prefix", [b"", HEADER + RAW[:3]], ids=["header", "body"])
def test_one_deadline_bounds_a_stalled_header_or_partial_body_and_reaps_child(sessions, prefix):
    session = fake(sessions, "sys.stdin.buffer.readline()\n"
        + f"sys.stdout.buffer.write({prefix!r}); sys.stdout.buffer.flush()\n"
        + "time.sleep(5)\n")
    with pytest.raises(batch.GitBatchUnavailable, match="^GIT_BATCH_TIMEOUT$"):
        session.blob(OID, len(RAW), deadline=deadline(1))
    assert_poisoned(session, sessions)


def test_stderr_overflow_cannot_block_the_response_or_escape_in_an_error(sessions):
    detail = b"SYNTHETIC_CHILD_DETAIL"
    session = fake(sessions, "sys.stdin.buffer.readline()\n"
        + f"sys.stderr.buffer.write({detail!r} * 1024); sys.stderr.buffer.flush()\n"
        + "time.sleep(5)\n")
    with pytest.raises(batch.GitBatchUnavailable) as refused:
        session.blob(OID, len(RAW), deadline=deadline())
    assert refused.value.reason == "GIT_BATCH_OUTPUT_LIMIT"
    assert detail.decode() not in str(refused.value)
    assert_poisoned(session, sessions)


class ShortPipe:
    """Use actual OS pipes, forcing legal short reads/writes deterministically."""
    def __init__(self, original, calls):
        self.original, self.calls = original, calls

    def read(self, size):
        self.calls.append(("read", size))
        return self.original.read(min(size, 3))

    def write(self, value):
        self.calls.append(("write", len(value)))
        return self.original.write(value[:3])

    def __getattr__(self, name):
        return getattr(self.original, name)


def test_legal_short_pipe_reads_and_writes_preserve_binary_frames(repository, sessions):
    oid = object_id(repository, "blob", RAW)
    calls = []

    def wrap(process):
        process.stdin = ShortPipe(process.stdin, calls)
        process.stdout = ShortPipe(process.stdout, calls)

    sessions.wrap = wrap
    session = sessions.open(command(repository))
    assert session.blob(oid, len(RAW), deadline=deadline()) == RAW
    assert session.info(oid, deadline=deadline()) == ("blob", len(RAW))
    assert sum(kind == "write" for kind, _ in calls) > 2
    assert any(kind == "read" and size > 3 for kind, size in calls)
    assert session.stats() == {"processes": 1, "requests": 2}
    session.close()
    assert_stopped(session, sessions)


def test_closing_an_unstarted_session_is_idempotent_and_does_not_spawn(sessions):
    session = fake(sessions, "raise RuntimeError('closed session must not start')\n")
    session.close()
    session.close()
    with pytest.raises(batch.GitBatchUnavailable, match="^GIT_BATCH_CLOSED$"):
        session.info(OID, deadline=deadline())
    assert sessions.attempts == 0 and session.stats() == {"processes": 0, "requests": 0}


def test_keyboard_interrupt_preserves_the_original_exception_and_reaps_the_live_child(sessions, monkeypatch):
    interrupted = KeyboardInterrupt("synthetic caller interruption")

    class InterruptWait(threading.Event):
        def wait(self, timeout=None):
            if threading.current_thread() is threading.main_thread():
                raise interrupted
            return super().wait(timeout)

    monkeypatch.setattr(batch, "Event", InterruptWait)
    session = fake(sessions, "sys.stdin.buffer.readline()\ntime.sleep(5)\n")
    with pytest.raises(KeyboardInterrupt) as caught:
        session.info(OID, deadline=deadline())
    assert caught.value is interrupted and len(sessions.children) == 1
    assert_poisoned(session, sessions)


def test_process_created_during_close_is_disposed_even_when_shared_close_is_busy(sessions, monkeypatch):
    session = fake(sessions, "sys.stdin.buffer.readline()\ntime.sleep(5)\n")
    popen, held = batch.subprocess.Popen, []

    def closing_handover(argv, *args, **kwargs):
        process = popen(argv, *args, **kwargs)
        # close() has observed no registered child. Hold the close lock across
        # handover to model another closer still finishing that same snapshot.
        # The creating call must dispose its own local child without that lock.
        session.close()
        assert session._close_lock.acquire(blocking=False)
        held.append(True)
        return process

    monkeypatch.setattr(batch.subprocess, "Popen", closing_handover)
    try:
        with pytest.raises(batch.GitBatchUnavailable, match="^GIT_BATCH_CLOSED$"):
            session.info(OID, deadline=deadline())
        assert session.stats() == {"processes": 1, "requests": 0}
        assert_stopped(session, sessions)
    finally:
        if held:
            session._close_lock.release()
    assert_poisoned(session, sessions)


def test_failed_process_start_is_not_retried(sessions, tmp_path):
    session = sessions.open([str(tmp_path / "missing executable"), "cat-file", "--batch-command"])
    with pytest.raises(batch.GitBatchUnavailable, match="^GIT_BATCH_START_FAILED$"):
        session.info(OID, deadline=deadline())
    assert sessions.attempts == 1 and session.stats() == {"processes": 0, "requests": 0}
    assert_poisoned(session, sessions)
