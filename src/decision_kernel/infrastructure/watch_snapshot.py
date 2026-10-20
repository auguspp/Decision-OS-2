"""Whole-prefix Watch replay on the original bounded native records snapshot.

The existing GitHubStore owns all permission checks, its private-upgrade latch
and its one API budget. A failed fetch/check never falls back to another reader.
"""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from decision_kernel.domain.records import SHA, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_snapshot import GitSnapshot
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_TREE_ENTRIES
from decision_kernel.infrastructure.github_reading import _ReadingScope
from decision_kernel.infrastructure.market_snapshot import _fetch_market_branch


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
    selected = snapshot or backend.head()
    backend.check_commit(selected)
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
            verify_first_parent(reader, selected)
            yield reader
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
        verify_first_parent(backend, selected)
        yield backend
        backend.check_commit(selected)
        verify_first_parent(backend, selected)


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
