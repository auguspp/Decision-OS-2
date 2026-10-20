"""Whole-prefix Watch replay on the original bounded native records snapshot.

The existing GitHubStore owns all permission checks, its private-upgrade latch
and its one API budget. A failed fetch/check never falls back to another reader.
"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from tempfile import TemporaryDirectory

from decision_kernel.domain.records import RecordError, SHA, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_snapshot import GitSnapshot
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_TREE_ENTRIES
from decision_kernel.infrastructure.github_reading import _ReadingScope
from decision_kernel.infrastructure.market_snapshot import _fetch_market_branch


_MEMO_BYTES = 8 * 1024 * 1024


def _sha(value):
    return isinstance(value, str) and bool(SHA.fullmatch(value))


def _count(value, prefix):
    return value.startswith(prefix) and value[len(prefix):].isdigit() and len(value[len(prefix):]) <= 20


def _immutable_query(args):
    """Only the existing complete SHA-pinned query forms can enter the memo."""
    if not args or not all(isinstance(arg, str) for arg in args):
        return False
    if len(args) == 4 and args[:2] == ("merge-base", "--is-ancestor"):
        return all(_sha(arg) for arg in args[2:])
    if len(args) == 3 and args[0] == "cat-file" and args[1] in ("-s", "blob"):
        return _sha(args[2])
    if len(args) == 5 and args[0] == "rev-list":
        if args[1:4] == ("--parents", "-n", "1"):
            return _sha(args[4])
        if args[1:3] == ("--first-parent", "--count"):
            return _sha(args[3]) and args[4].startswith("^") and _sha(args[4][1:])
        if args[1:3] == ("--first-parent", "--max-count=1"):
            return _count(args[3], "--skip=") and _sha(args[4])
    if args[0] == "log":
        if len(args) == 6 and args[1:3] == ("-1", "--format=%H"):
            return _sha(args[3]) and args[4] == "--"
        if len(args) in (10, 11) and args[1:5] == (
                "--first-parent", "--full-history", "--diff-merges=first-parent", "--no-patch"):
            return (_count(args[5], "--max-count=") and args[6] == "--format=%H"
                    and (len(args) == 10 or args[7] == "--diff-filter=AMT")
                    and _sha(args[-3]) and args[-2] == "--")
    if args[0] == "ls-tree":
        if len(args) == 5 and args[1] == "-z":
            return _sha(args[2]) and args[3] == "--"
        if len(args) in (5, 7) and args[1:4] == ("-r", "--name-only", "-z"):
            return _sha(args[4]) and (len(args) == 5 or args[5] == "--")
        if len(args) == 4 and args[1:3] == ("-r", "-z"):
            return _sha(args[3])
    return False


class _NativeMemo:
    """One admitted reader owns bounded immutable results, never live authority.

    Native read methods are inherited unchanged. Their ref reads still execute
    on every call; only successful queries containing exact object IDs reuse
    bytes. In particular, a pinned GitSnapshot never substitutes for the real
    records ref. The original native object is not patched or given cache state.
    """
    def __init__(self, native):
        self._native, self.root, self.repository = native, native.root, native.repository
        if type(native) is GitSnapshot:
            self.binding, self._snapshot = native.binding, native._snapshot
        self._memo, self._memo_bytes, self._active = {}, 0, True

    def _check_active(self):
        require(self._active, "WATCH_SNAPSHOT_SCOPE_CLOSED")

    def head(self):
        self._check_active()
        return super().head()

    def _run(self, *args, data=None, index=None, allowed=(0,)):
        self._check_active()
        reusable = data is None and index is None and _immutable_query(args)
        key = (args, tuple(allowed)) if reusable else None
        if key in self._memo:
            command, stdout, stderr = self._memo[key]
            return CompletedProcess(command, 0, stdout, stderr)
        result = self._native._run(*args, data=data, index=index, allowed=allowed)
        if (reusable and result.returncode == 0 and isinstance(result.stdout, bytes)
                and isinstance(result.stderr, bytes) and len(self._memo) < MAX_TREE_ENTRIES
                and (result.stdout or args[0] not in ("ls-tree", "log"))):
            command = tuple(result.args)
            size = len(result.stdout) + len(result.stderr) + sum(len(arg.encode("utf-8")) for arg in command)
            size += sum(len(arg.encode("utf-8")) for arg in args) + len(allowed) * 8 + 128
            if self._memo_bytes + size <= _MEMO_BYTES:
                self._memo[key] = (command, result.stdout, result.stderr)
                self._memo_bytes += size
                return CompletedProcess(command, 0, result.stdout, result.stderr)
        return result

    def commit(self, files, *, expected):
        self._check_active()
        raise RecordError("WATCH_SNAPSHOT_READ_ONLY")


class _MemoGitStore(_NativeMemo, GitStore):
    pass


class _MemoGitSnapshot(_NativeMemo, GitSnapshot):
    pass


_MEMO_TYPES = (_MemoGitStore, _MemoGitSnapshot)


@contextmanager
def _native_memo_scope(backend):
    if type(backend) in _MEMO_TYPES:
        backend._check_active()
        yield backend
        return
    if type(backend) not in (GitStore, GitSnapshot):
        # Custom native subclasses retain their original overrides and contract.
        yield backend
        return
    reader = (_MemoGitSnapshot if type(backend) is GitSnapshot else _MemoGitStore)(backend)
    try:
        yield reader
    finally:
        reader._active = False
        reader._memo.clear()
        reader._memo_bytes = 0


def _records_head(backend):
    """Read the native records ref, independently of a caller-pinned snapshot."""
    require(isinstance(backend, GitStore), "WATCH_SNAPSHOT_TRUSTED_BACKEND_REQUIRED")
    branch = backend.binding.branch if isinstance(backend, GitSnapshot) else "records"
    head = backend._run("rev-parse", "--verify", "refs/heads/" + branch).stdout.decode("ascii").strip()
    require(bool(SHA.fullmatch(head)), "INVALID_STORE_HEAD")
    return head


def verify_first_parent(backend, commit):
    """Prove original records first-parent membership with bounded output.

    Excluding commit and its ancestors leaves the first-parent path down to
    its first shared ancestor. That first omitted original must be commit
    itself, not the fork point of a merged side branch. Neither command emits
    a history-sized list or accepts an externally supplied lineage summary.
    The native records ref remains the root even for a historical GitSnapshot.
    Binding anchor/reachability checks retain their original independent rules.
    """
    require(isinstance(backend, GitStore), "WATCH_SNAPSHOT_TRUSTED_BACKEND_REQUIRED")
    backend.check_commit(commit)
    head = _records_head(backend)
    if commit == head:
        return
    count = backend._run("rev-list", "--first-parent", "--count", head, "^" + commit).stdout.strip()
    require(count.isdigit() and len(count) <= 20, "WATCH_HISTORY_NATIVE_LINEAGE_INVALID")
    original = backend._run("rev-list", "--first-parent", "--max-count=1",
                            "--skip=" + count.decode("ascii"), head).stdout.decode("ascii").strip()
    require(original == commit, "WATCH_HISTORY_EVENT_OUTSIDE_FIRST_PARENT")


def _path_changes(backend, snapshot, path, count, present_only):
    verify_first_parent(backend, snapshot)
    backend._path(path)
    args = ["log", "--first-parent", "--full-history", "--diff-merges=first-parent",
            "--no-patch", "--max-count=" + str(count), "--format=%H"]
    if present_only:
        args.append("--diff-filter=AMT")
    originals = backend._run(*args, snapshot, "--", path).stdout.decode("ascii").splitlines()
    require(len(originals) <= count and all(SHA.fullmatch(item) for item in originals),
            "WATCH_HISTORY_NATIVE_LINEAGE_INVALID")
    return originals


def first_parent_change(backend, snapshot, path, *, present_only=False):
    """Last original selection change, including deletions and merge changes.

    `present_only` is only for finding the exact pre-deletion pointer original;
    registration always uses the complete change history, including deletions.
    """
    originals = _path_changes(backend, snapshot, path, 1, present_only)
    return originals[0] if originals else None


def first_parent_changes(backend, snapshot, path, *, limit):
    """Bounded complete history for one path; reaching the cap is not absence."""
    require(type(limit) is int and 1 <= limit <= MAX_TREE_ENTRIES, "WATCH_HISTORY_SELECTION_LIMIT")
    originals = _path_changes(backend, snapshot, path, limit + 1, False)
    require(len(originals) <= limit, "WATCH_HISTORY_SELECTION_LIMIT")
    return originals


@contextmanager
def watch_read_scope(backend, snapshot=None):
    require(isinstance(backend, (GitStore, GitHubStore)), "WATCH_SNAPSHOT_TRUSTED_BACKEND_REQUIRED")
    gate = backend
    if type(backend) in _MEMO_TYPES:
        backend._check_active()
        gate = backend._native
    selected = snapshot or backend.head()
    gate.check_commit(selected)
    authority = backend
    if type(backend) is _ReadingScope:
        # This one host-owned facade pins an already admitted room snapshot.
        # Keep its active/reachability checks, then use its original live owner
        # for privacy/ownership checks. Never generally unwrap arbitrary readers.
        authority = backend.owner
        require(isinstance(authority, GitHubStore) and not isinstance(authority, _ReadingScope),
                "WATCH_SNAPSHOT_NATIVE_OWNER_REQUIRED")
        authority.check_commit(selected)
    if isinstance(backend, GitHubStore):
        # Keep this exact backend, including any visibility transition observed
        # before entering the scope. Reusing only its API would lose that latch.
        binding = replace(backend.binding, write_enabled=False)
        with TemporaryDirectory(prefix="dos2-watch-read-") as temporary:
            root = Path(temporary) / "records.git"
            _fetch_market_branch(binding, backend.api, root)
            reader = GitSnapshot(root, binding, selected)
            admission_head = _records_head(reader)
            with _native_memo_scope(reader) as scoped:
                verify_first_parent(scoped, selected)
                yield scoped
                backend.check_commit(selected)
                if authority is not backend:
                    authority.check_commit(selected)
                # An any-parent live check alone cannot prove that a concurrent
                # merge retained this admission's records first-parent lineage.
                require(authority.head() == admission_head, "WATCH_SNAPSHOT_LINEAGE_CHANGED")
    else:
        # Local synthetic stores and already verified offline GitSnapshots keep
        # their original exact-ref/ancestry contract; the application memo is
        # read-only. Remote imports always use the bounded GitSnapshot above.
        with _native_memo_scope(backend) as reader:
            verify_first_parent(reader, selected)
            yield reader
            gate.check_commit(selected)
            verify_first_parent(gate, selected)


def verify_event_parent(backend, commit, snapshot):
    """The native CAS event belongs to records' original first-parent lineage.

    It must also be a single-parent child of its claimed base. A genuine side
    event merged after a retirement is not a valid mainline successor. The
    scope's caller-pinned snapshot can never become a substitute lineage root.
    """
    require(isinstance(backend, GitStore), "WATCH_SNAPSHOT_TRUSTED_BACKEND_REQUIRED")
    backend.check_commit(commit); backend.check_commit(snapshot)
    parents = backend._run("rev-list", "--parents", "-n", "1", commit).stdout.decode("ascii").split()
    require(parents == [commit, snapshot], "WATCH_HISTORY_EVENT_PARENT_MISMATCH")
    verify_first_parent(backend, commit)
