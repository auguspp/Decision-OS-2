"""Actual store inventory, conservative admission, and separate query/store caps."""
from copy import deepcopy

import pytest

from decision_kernel.application.market_capacity import WORK_UNIT_RESERVE, market_capacity
from decision_kernel.application.store_audit import audit_store
from decision_kernel.application.versioned_store import (
    MAX_QUERY_VERSIONS, MAX_SCAN_VERSIONS, MAX_STORE_VERSIONS, VersionedStore,
)
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.infrastructure.git_snapshot import MAX_BUNDLE_BYTES
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import MAX_TREE_ENTRIES
from test_market_custody import MemoryBackend


def test_inventory_counts_directories_and_all_version_families_without_source_rows():
    backend = MemoryBackend()
    version = "a" * 64
    files = ["store.json", "source-views/source/identity.json",
        f"source-views/source/{version}/manifest.json", f"source-views/source/{version}/content.txt",
        "research/cases/case/revisions/revision/record.json", "research/cases/case/revisions/revision/research.md",
        "research/cases/case/current.json", f"research/artifacts/artifact/{version}/manifest.json",
        f"research/artifacts/artifact/{version}/content.bin",
        f"research/watch-activities/{version}/current.json", "submissions/intent.json"]
    backend.shared["trees"][backend.head()] = {path: b"private source rows never returned" for path in files}
    result = market_capacity(VersionedStore(backend))
    assert result["tree_entries"] == 25 and result["version_count"] == 3
    assert result["native_bytes"] is None and result["admit_work_unit"] is False
    assert set(result) == {"tree_entries", "version_count", "native_bytes", "limits", "work_unit_reserve", "admit_work_unit"}
    assert result["limits"] == {"tree_entries": 32768, "version_count": 8192, "native_bytes": 64 * 1024 * 1024}
    assert result["work_unit_reserve"] == {"tree_entries": 160, "version_count": 32, "native_bytes": 8 * 1024 * 1024}
    assert b"private source rows" not in encoded(result) and b"source-views" not in encoded(result)


@pytest.mark.parametrize("dimension", ["tree_entries", "version_count", "native_bytes"])
def test_one_unit_reserve_admits_exact_boundary_and_blocks_one_more(dimension):
    limits = {"tree_entries": MAX_TREE_ENTRIES, "version_count": MAX_STORE_VERSIONS, "native_bytes": MAX_BUNDLE_BYTES}
    counts = {key: bound - WORK_UNIT_RESERVE[key] for key, bound in limits.items()}

    class InventoryBackend(MemoryBackend):
        def market_inventory(self):
            return deepcopy(counts)

    store = VersionedStore(InventoryBackend())
    assert market_capacity(store)["admit_work_unit"] is True
    counts[dimension] += 1
    assert market_capacity(store)["admit_work_unit"] is False


@pytest.mark.parametrize("change", [
    {"native_bytes": False}, {"version_count": -1}, {"tree_entries": "0"}, {"extra": "private location"},
])
def test_invalid_inventory_is_not_converted_to_zero_capacity(change):
    class InventoryBackend(MemoryBackend):
        def market_inventory(self):
            return {"tree_entries": 1, "version_count": 0, "native_bytes": 1, **change}

    with pytest.raises(RecordError, match="INVENTORY_INVALID"):
        market_capacity(VersionedStore(InventoryBackend()))


def test_local_git_inventory_matches_native_tree_and_real_bare_bytes(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / "capacity.git")
    store = VersionedStore(backend)
    store.put_source("one", SourceInput(source_id="capacity", kind="content", title="Synthetic capacity",
        origin="https://example.invalid/capacity", text="retained input", custody="provided"))
    rows = [row for row in backend._run("ls-tree", "-r", "-t", "-z", backend.head()).stdout.split(b"\0") if row]
    size = sum(path.stat().st_size for path in backend.root.rglob("*") if path.is_file())
    result = market_capacity(store)
    assert result["tree_entries"] == len(rows) == 9
    assert result["version_count"] == 1 and result["native_bytes"] == size > 0
    assert result["admit_work_unit"] is True


def test_1025_valid_source_versions_keep_flat_1024_cap_but_scan_only_32_each_time():
    assert MAX_QUERY_VERSIONS == 1024 and MAX_SCAN_VERSIONS == 32 and MAX_STORE_VERSIONS == 8192
    backend = MemoryBackend()
    store = VersionedStore(backend)
    at_limit = None
    for number in range(1025):
        store.put_source("capacity-" + str(number), SourceInput(source_id="capacity-source", kind="content",
            title="Synthetic capacity version", origin="https://example.invalid/capacity",
            text="version " + str(number), custody="provided"))
        if number == 1023:
            at_limit = backend.head()
    # The original flat query and historical cursor still retain their 1024 cap.
    old = store.search("version", cursor={"snapshot": at_limit, "offset": 0,
        "query_sha256": digest(encoded({"query": "version", "entity_id": None}))})
    assert old["total"] == 1024
    with pytest.raises(RecordError, match="QUERY_VERSION_LIMIT_EXCEEDED"):
        store.search("version")
    cursor, references, scanned, snapshot = None, set(), [], backend.head()
    while True:
        page = store.search("version", cursor=cursor, scan_limit=32, limit=100)
        assert page["snapshot"] == snapshot and page["scan"]["total_versions"] == 1025
        assert page["scan"]["scanned_versions"] <= 32
        scanned.append(page["scan"]["scanned_versions"])
        for item in page["items"]:
            reference = encoded(item["reference"])
            assert reference not in references
            references.add(reference)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(references) == 1025 and scanned == [32] * 32 + [1]


def test_audit_stops_above_8192_total_versions_before_loading_any_body():
    backend = MemoryBackend()
    paths = {}
    # All three version families contribute to the same hard audit bound.
    for number in range(2731):
        version = f"{number:064x}"
        paths[f"source-views/source/{version}/manifest.json"] = b"must not load"
        paths[f"research/cases/case/revisions/revision-{number}/record.json"] = b"must not load"
        paths[f"research/artifacts/artifact/{version}/manifest.json"] = b"must not load"
    backend.shared["trees"][backend.head()] = paths
    reads = []
    original_read = backend.read
    def read(commit, path):
        reads.append(path)
        return original_read(commit, path)
    backend.read = read
    with pytest.raises(RecordError, match="^AUDIT_VERSION_LIMIT$"):
        audit_store(VersionedStore(backend), backend.head())
    assert len(paths) == 8193 and reads == []
