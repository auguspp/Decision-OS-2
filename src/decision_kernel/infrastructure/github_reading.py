"""One expiring, read-only GitHub room scope on the existing checked store.

Reuse exact Git reads, bounded caches and the caller's transport. Nothing here
fetches source URLs, writes records, or changes ordinary store/market readers.
"""
from contextlib import contextmanager
from dataclasses import replace
from functools import wraps

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.github_store import (
    GitHubStore, MAX_ANCESTOR_PROOFS, _sha,
)


@contextmanager
def reading_scope(backend, *, fail_closed=False):
    """Release a room only after native privacy and reachability are rechecked."""
    if not isinstance(backend, GitHubStore):
        yield backend  # Local stores keep their existing exact-read contract.
        return
    snapshot = backend.head()
    reader = _ReadingScope(backend, snapshot, fail_closed=fail_closed)
    try:
        try:
            yield reader
        finally:
            if fail_closed:
                reader._active()
        backend.check_commit(snapshot)
    finally:
        reader.active = False


def _terminal_read(method):
    """The original scope type and dispatch remain unchanged."""
    @wraps(method)
    def checked(self, *args, **kwargs):
        self._active()
        try:
            return method(self, *args, **kwargs)
        except RecordError as error:
            self._remember_failure(error)
            raise
    return checked


class _ReadingScope(GitHubStore):
    """Read-only facade; subtype preserves the original market-reader dispatch.

    The owner retains all native transport, immutable caches and private-upgrade
    state. This facade pins only one admission snapshot and expires on exit.
    """
    def __init__(self, owner, snapshot, *, fail_closed=False):
        self.owner, self.snapshot, self.repository = owner, snapshot, owner.repository
        self.binding = replace(owner.binding, write_enabled=False)
        self.api = owner.api
        self.active, self._checked = True, set()
        self.fail_closed, self.terminal_failure = fail_closed, None

    def _active(self):
        if self.terminal_failure is not None:
            raise self.terminal_failure
        require(self.active, "READING_SCOPE_CLOSED")

    def _remember_failure(self, error):
        # Reuse the existing access/scope/transport classification, not a new
        # research policy. PATH_OUTSIDE_STORE is the native byte-read allowlist
        # refusing an operation; ordinary missing objects remain local gaps.
        from decision_kernel.application.market_reading_scope import operation_failure
        if self.fail_closed and self.terminal_failure is None and (
                operation_failure(error) or str(error) == "PATH_OUTSIDE_STORE"):
            self.terminal_failure = error

    @contextmanager
    def component_read_scope(self):
        """Latch failures from an original reader sharing this operation's API."""
        self._active()
        try:
            yield
        except RecordError as error:
            self._remember_failure(error)
            raise
        finally:
            self._active()

    @contextmanager
    def source_read_scope(self, snapshot):
        """Keep the original source scope, including its own preflight failures.

        Its foreign-reference and source-only checks can fail before owner._read.
        Guard that same short-lived instance only for this explicit opt-in; its
        original class, owner, snapshot and native exit/expiry remain in charge.
        """
        self._active()
        with super().source_read_scope(snapshot) as reader:
            if self.fail_closed:
                def guard(method):
                    @wraps(method)
                    def checked(*args, **kwargs):
                        with self.component_read_scope():
                            return method(*args, **kwargs)
                    return checked
                for name in ("check_commit", "read", "paths", "reference", "resolve", "commit"):
                    setattr(reader, name, guard(getattr(reader, name)))
            yield reader

    @_terminal_read
    def head(self):
        self._active()
        return self.snapshot

    @_terminal_read
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

    @_terminal_read
    def _get(self, path):
        self._active()
        return self.owner._get(path)

    @_terminal_read
    def _ancestor(self, base, head):
        self._active()
        self.owner._ancestor(base, head)

    @_terminal_read
    def _tree(self, commit):
        self._active()
        return self.owner._tree(commit)

    @_terminal_read
    def _read(self, commit, path, *, tree=None):
        self._active()
        return self.owner._read(commit, path, tree=tree)

    @_terminal_read
    def live_visibility(self):
        self._active()
        return self.owner.live_visibility()

    @_terminal_read
    def read(self, commit, path):
        return super().read(commit, path)

    @_terminal_read
    def paths(self, commit, prefix=""):
        return super().paths(commit, prefix)

    @_terminal_read
    def resolve(self, reference):
        return super().resolve(reference)

    @_terminal_read
    def reference(self, commit, path):
        return super().reference(commit, path)

    @_terminal_read
    def last_change(self, head, path):
        return super().last_change(head, path)

    def commit(self, files, *, expected):
        raise RecordError("READING_SCOPE_READ_ONLY")
