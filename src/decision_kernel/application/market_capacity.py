"""Counts-only admission for one finite market work unit in the existing store.

The reserve leaves room to retain a bounded attempt and its outcome. It is not
a promise of unlimited history, and an unknown native size is not zero usage.
"""
from __future__ import annotations

import os
from pathlib import Path

from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.versioned_store import MAX_STORE_VERSIONS, VersionedStore
from decision_kernel.domain.records import StoreUnavailable, require
from decision_kernel.infrastructure.git_snapshot import MAX_BUNDLE_BYTES
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import (
    GitHubBinding, MAX_RESPONSE, MAX_TREE_ENTRIES, TREE_RESPONSE_WORK_UNIT_RESERVE,
    github_tree_inventory,
)

WORK_UNIT_RESERVE = {"tree_entries": 160, "version_count": 32, "native_bytes": 8 * 1024 * 1024,
                    "tree_response_bytes_estimate": TREE_RESPONSE_WORK_UNIT_RESERVE}


def _native_bytes(root):
    total = 0
    def failed(error):
        raise error
    try:
        for current, directories, files in os.walk(root, followlinks=False, onerror=failed):
            require(not any((Path(current) / name).is_symlink() for name in directories + files),
                    "MARKET_CAPACITY_SYMLINK")
            for name in files:
                total += (Path(current) / name).stat().st_size
    except OSError:
        raise StoreUnavailable("MARKET_CAPACITY_NATIVE_SIZE_UNAVAILABLE") from None
    return total


def _path_inventory(backend):
    snapshot = backend.head()
    backend.check_commit(snapshot)
    paths = backend.paths(snapshot)
    require(isinstance(paths, list) and all(isinstance(path, str) and path
            and not path.startswith("/") and not path.endswith("/")
            and all(part not in ("", ".", "..") for part in path.split("/")) for path in paths)
            and len(paths) == len(set(paths)), "MARKET_CAPACITY_INVENTORY_INVALID")
    directories = {"/".join(parts[:number]) for path in paths
                   for parts in [path.split("/")] for number in range(1, len(parts))}
    versions = sum((path.startswith("source-views/") and path.endswith("/manifest.json"))
        or (path.startswith("research/cases/") and path.endswith("/record.json"))
        or (path.startswith("research/artifacts/") and path.endswith("/manifest.json")) for path in paths)
    result = {"tree_entries": len(paths) + len(directories), "version_count": versions,
              "native_bytes": _native_bytes(backend.root) if isinstance(backend, GitStore) else None,
              "tree_response_bytes_estimate": None, "tree_response_applicability": "unknown"}
    if isinstance(backend, GitStore):
        files, native_directories = {}, []
        for row in backend._run("ls-tree", "-r", "-t", "-l", "-z", snapshot).stdout.split(b"\0"):
            if not row:
                continue
            meta, path = row.split(b"\t")
            fields = meta.split()
            if fields[1] == b"blob":
                files[path.decode("ascii")] = int(fields[3])
            else:
                native_directories.append(path.decode("ascii"))
        result["tree_entries"] = len(files) + len(native_directories)
        binding = getattr(backend, "binding", None)
        if isinstance(binding, GitHubBinding):
            result.update(github_tree_inventory(binding, files, directories=native_directories),
                          tree_response_applicability="required")
        elif backend.repository.startswith("synthetic-git:"):
            # A real local synthetic store has no GitHub tree response. This
            # exception does not turn an arbitrary in-memory reader into a host.
            result["tree_response_applicability"] = "not-applicable"
    return result


def market_capacity(store):
    """Inspect one pinned inventory and reserve capacity before source admission."""
    require(isinstance(store, VersionedStore), "MARKET_CUSTODY_STORE_REQUIRED")
    with market_read_scope(store) as reader:
        method = getattr(reader.backend, "market_inventory", None)
        require(method is None or callable(method), "MARKET_CAPACITY_INVENTORY_INVALID")
        counts = method() if method is not None else _path_inventory(reader.backend)
        require(isinstance(counts, dict) and set(counts) == {"tree_entries", "version_count", "native_bytes",
                    "tree_response_bytes_estimate", "tree_response_applicability"}
                and all(type(counts[key]) is int and counts[key] >= 0 for key in ("tree_entries", "version_count"))
                and (counts["native_bytes"] is None or type(counts["native_bytes"]) is int
                     and counts["native_bytes"] >= 0)
                and counts["tree_response_applicability"] in ("required", "not-applicable", "unknown")
                and (counts["tree_response_bytes_estimate"] is None
                     or type(counts["tree_response_bytes_estimate"]) is int
                     and counts["tree_response_bytes_estimate"] >= 0), "MARKET_CAPACITY_INVENTORY_INVALID")
        if counts["tree_response_applicability"] == "not-applicable":
            require(isinstance(reader.backend, GitStore) and reader.backend.repository.startswith("synthetic-git:")
                    and counts["tree_response_bytes_estimate"] is None, "MARKET_CAPACITY_INVENTORY_INVALID")
        limits = {"tree_entries": MAX_TREE_ENTRIES, "version_count": MAX_STORE_VERSIONS,
                  "native_bytes": MAX_BUNDLE_BYTES, "tree_response_bytes_estimate": MAX_RESPONSE}
        applicable = {key: bound for key, bound in limits.items() if key != "tree_response_bytes_estimate"
                      or counts["tree_response_applicability"] != "not-applicable"}
        admit = counts["tree_response_applicability"] != "unknown" and all(
            counts[key] is not None and counts[key] + WORK_UNIT_RESERVE[key] <= bound
            for key, bound in applicable.items())
        return {**counts, "limits": limits, "work_unit_reserve": dict(WORK_UNIT_RESERVE),
                "admit_work_unit": admit}
