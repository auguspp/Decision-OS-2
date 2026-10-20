"""Bounded immutable native Git results inside one admitted read-only scope.

Watch and market readers retain their own admission, live authority and path
contracts. Call sites choose fixed diagnostics; query policy and limits are shared.
"""
from contextlib import contextmanager
from subprocess import CompletedProcess

from decision_kernel.domain.records import RecordError, SHA, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_snapshot import GitSnapshot
from decision_kernel.infrastructure.github_store import MAX_TREE_ENTRIES


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
    def __init__(self, native, *, closed_error, read_only_error):
        self._native, self.root, self.repository = native, native.root, native.repository
        self._closed_error, self._read_only_error = closed_error, read_only_error
        if type(native) is GitSnapshot:
            self.binding, self._snapshot = native.binding, native._snapshot
        self._memo, self._memo_bytes, self._active = {}, 0, True

    def _check_active(self):
        require(self._active, self._closed_error)

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
        raise RecordError(self._read_only_error)


class _MemoGitStore(_NativeMemo, GitStore):
    pass


class _MemoGitSnapshot(_NativeMemo, GitSnapshot):
    pass


_MEMO_TYPES = (_MemoGitStore, _MemoGitSnapshot)


@contextmanager
def native_git_memo_scope(backend, *, closed_error, read_only_error):
    if type(backend) in _MEMO_TYPES:
        backend._check_active()
        yield backend
        return
    if type(backend) not in (GitStore, GitSnapshot):
        # Custom native subclasses retain their original overrides and contract.
        yield backend
        return
    reader = (_MemoGitSnapshot if type(backend) is GitSnapshot else _MemoGitStore)(backend,
        closed_error=closed_error, read_only_error=read_only_error)
    try:
        yield reader
    finally:
        reader._active = False
        reader._memo.clear()
        reader._memo_bytes = 0


