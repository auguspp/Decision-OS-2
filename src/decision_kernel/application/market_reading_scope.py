"""Reuse one original pinned market backend and preserve its operation failure.

This facade owns no API, transport, request budget or source acquisition. The
original surrounding scope owns admission and the final native binding check.
"""
from contextlib import contextmanager

from decision_kernel.domain.records import RecordError, StoreUnavailable, require


def operation_failure(error):
    """Original access, scope and transport limits terminate the whole room."""
    code = str(error)
    return isinstance(error, StoreUnavailable) or code in {
        "PRIVATE_BINDING_MISMATCH", "PUBLIC_BINDING_MISMATCH", "PRIVATE_UPGRADE_REVERSED",
        "COMMIT_OUTSIDE_BINDING", "COMMIT_NOT_REACHABLE", "FOREIGN_REPOSITORY",
        "STORE_MARKER_MISMATCH", "STORE_BINDING_REQUIRED", "COMPLETE_SNAPSHOT_REQUIRED",
        "EXTERNAL_OBJECTS_FORBIDDEN", "DEDICATED_BARE_STORE_REQUIRED", "INVALID_SNAPSHOT",
        "INVALID_SNAPSHOT_TREE", "MARKET_CUSTODY_READ_SCOPE_REQUIRED",
        "MARKET_CUSTODY_READ_SCOPE_REPOSITORY_CHANGED",
    } or code.startswith(("REMOTE_", "MARKET_SNAPSHOT_", "SNAPSHOT_", "READING_SCOPE_", "SOURCE_SCAN_"))


class SourceReads:
    """Latch the original failure around an already pinned original backend.

    Like the existing close-structure consumer, nested market scopes retain this
    read-only facade. The enclosing market_read_scope owns admission and its
    final native check. This consumer preserves the original exception object.
    """
    def __init__(self, backend):
        self.owner, self.failure, self.active = backend, None, True
        self.repository = backend.repository

    def check(self):
        if self.failure is not None:
            raise self.failure
        require(self.active, "READING_SCOPE_CLOSED")

    @contextmanager
    def market_read_scope(self):
        self.check()
        yield self
        self.check()

    def __getattr__(self, name):
        if name not in ("head", "check_commit", "read", "paths", "reference", "resolve", "last_change"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._read(name, *args, **kwargs)

    def _read(self, name, *args, **kwargs):
        self.check()
        try:
            return getattr(self.owner, name)(*args, **kwargs)
        except RecordError as error:
            if operation_failure(error):
                self.failure = error
            raise

    def commit(self, files, *, expected):
        raise RecordError("READING_SCOPE_READ_ONLY")
