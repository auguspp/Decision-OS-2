"""One expiring, read-only GitHub room scope on the existing checked store.

Reuse exact Git reads, bounded caches and the caller's transport. Nothing here
fetches source URLs, writes records, or changes ordinary store/market readers.
"""
from contextlib import contextmanager
from dataclasses import replace

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.github_store import (
    GitHubStore, MAX_ANCESTOR_PROOFS, _sha,
)


@contextmanager
def reading_scope(backend):
    """Release a room only after native privacy and reachability are rechecked."""
    if not isinstance(backend, GitHubStore):
        yield backend  # Local stores keep their existing exact-read contract.
        return
    snapshot = backend.head()
    reader = _ReadingScope(backend, snapshot)
    try:
        yield reader
        backend.check_commit(snapshot)
    finally:
        reader.active = False


class _ReadingScope(GitHubStore):
    """Read-only facade; subtype preserves the original market-reader dispatch.

    The owner retains all native transport, immutable caches and private-upgrade
    state. This facade pins only one admission snapshot and expires on exit.
    """
    def __init__(self, owner, snapshot):
        self.owner, self.snapshot, self.repository = owner, snapshot, owner.repository
        self.binding = replace(owner.binding, write_enabled=False)
        self.api = owner.api
        self.active, self._checked = True, set()

    def _active(self):
        require(self.active, "READING_SCOPE_CLOSED")

    def head(self):
        self._active()
        return self.snapshot

    def check_commit(self, commit):
        self._active()
        _sha(commit)
        if commit in self._checked:
            return
        self.owner._ancestor(self.binding.anchor, commit)
        self.owner._ancestor(commit, self.snapshot)
        self.owner._marker(commit)
        if len(self._checked) < MAX_ANCESTOR_PROOFS:
            self._checked.add(commit)

    def _get(self, path):
        self._active()
        return self.owner._get(path)

    def _ancestor(self, base, head):
        self._active()
        self.owner._ancestor(base, head)

    def _tree(self, commit):
        self._active()
        return self.owner._tree(commit)

    def _read(self, commit, path, *, tree=None):
        self._active()
        return self.owner._read(commit, path, tree=tree)

    def live_visibility(self):
        self._active()
        return self.owner.live_visibility()

    def commit(self, files, *, expected):
        raise RecordError("READING_SCOPE_READ_ONLY")
