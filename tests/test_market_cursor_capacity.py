"""Finite cursor admission and exact history proofs, with no provider requests."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import market_collection_reading as cursor
from decision_kernel.application.market_collection_custody import (
    COLLECTION_SCHEMA, _source, retain_market_collection,
)
from decision_kernel.application.versioned_store import MAX_STORE_VERSIONS, VersionedStore
from decision_kernel.domain.records import (
    BlobRef, Conflict, MissingObject, RecordError, SHA, SourceRef, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from test_market_collection_custody import collection
from test_market_collection_inputs import CollectionOpener
from test_market_collection_sync import END, memory, plan, run
from test_market_relay_inputs import REGISTRY


class LinearBackend:
    """Append-only synthetic history without copying every previous tree.

    The original VersionedStore still creates and authenticates every source.
    Only the storage fixture differs, so a thousand small control roots do not
    require a quadratic collection of in-memory tree snapshots.
    """
    repository = "synthetic-git:cursor-history"

    def __init__(self, shared=None):
        self.shared = shared if shared is not None else {
            "count": 0, "objects": {}, "body_reads": [], "head_calls": 0,
        }

    def head(self):
        self.shared["head_calls"] += 1
        return f"{self.shared['count']:040x}"

    def check_commit(self, commit):
        if not isinstance(commit, str) or not SHA.fullmatch(commit) or int(commit, 16) > self.shared["count"]:
            raise MissingObject("COMMIT_NOT_REACHABLE")

    def read(self, commit, path):
        self.check_commit(commit)
        for version, raw in reversed(self.shared["objects"].get(path, [])):
            if version <= int(commit, 16):
                if path.endswith("/content.txt"):
                    self.shared["body_reads"].append((commit, path))
                return raw
        raise MissingObject("OBJECT_MISSING")

    def paths(self, commit, prefix=""):
        self.check_commit(commit)
        return sorted(path for path, versions in self.shared["objects"].items()
                      if path.startswith(prefix) and versions[0][0] <= int(commit, 16))

    def last_change(self, head, path):
        self.check_commit(head)
        for version, _ in reversed(self.shared["objects"].get(path, [])):
            if version <= int(head, 16):
                return f"{version:040x}"
        raise MissingObject("OBJECT_MISSING")

    def reference(self, commit, path):
        return BlobRef(self.repository, commit, path, digest(self.read(commit, path)))

    def resolve(self, reference):
        if reference.repository != self.repository:
            raise RecordError("FOREIGN_REPOSITORY")
        raw = self.read(reference.commit, reference.path)
        if digest(raw) != reference.sha256:
            raise RecordError("BLOB_DIGEST_MISMATCH")
        return raw

    def commit(self, files, *, expected):
        if self.head() != expected:
            raise Conflict("HEAD_CHANGED")
        self.shared["count"] += 1
        for path, raw in files.items():
            versions = self.shared["objects"].setdefault(path, [])
            if not versions or versions[-1][1] != raw:
                versions.append((self.shared["count"], raw))
        return self.head()


def linear_store():
    backend = LinearBackend()
    return backend, VersionedStore(backend), lambda: VersionedStore(LinearBackend(backend.shared))


def retain(store, fresh, *, previous=None, attempt="one", generation="initial"):
    value = collection(generation)
    value["attempt"]["id"] = attempt
    return retain_market_collection(value, REGISTRY, store, predecessor=previous,
        fresh_reader=fresh, custody_authorized=True)["reference"]


def put_root(store, *, previous, attempt="changed", owner="synthetic-market"):
    """Retain valid source bytes whose logical parent is deliberately untrusted."""
    value = collection()
    value["plan"]["collection_id"] = owner
    value["attempt"]["id"] = attempt
    source = _source({"schema": COLLECTION_SCHEMA, "registry": deepcopy(REGISTRY.snapshot),
                      "predecessor": previous, "collection": value})
    return asdict(store.put_source("cursor-" + digest(encoded(asdict(source))), source))


def body_size(store, reference):
    return len(store.read_source(SourceRef.from_dict(reference))["data"].encode())


def test_cursor_admission_reserves_two_maximum_roots_and_keeps_unknown_unknown():
    assert cursor.MAX_CURSOR_VERSIONS == MAX_STORE_VERSIONS == 8192
    assert cursor.MAX_CURSOR_BYTES == 512 * 1024 * 1024
    reserve = cursor.CURSOR_WORK_UNIT_RESERVE
    assert reserve == {"node_count": 2, "unique_body_bytes": 2 * cursor.MAX_COLLECTION_ROOT_BYTES}
    counts = {"node_count": cursor.MAX_CURSOR_VERSIONS - reserve["node_count"],
              "unique_body_bytes": cursor.MAX_CURSOR_BYTES - reserve["unique_body_bytes"]}
    result = cursor.market_cursor_capacity(**counts)
    assert result["admit_work_unit"] is True and result["work_unit_reserve"] == reserve
    assert set(result) == {*counts, "limits", "work_unit_reserve", "admit_work_unit"}
    for key in counts:
        assert cursor.market_cursor_capacity(**{**counts, key: counts[key] + 1})["admit_work_unit"] is False
        unknown = cursor.market_cursor_capacity(**{**counts, key: None})
        assert unknown[key] is None and unknown["admit_work_unit"] is False


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_cursor_admission_rejects_invalid_counts(bad):
    with pytest.raises(RecordError, match="ASHARE_CURSOR_CAPACITY_COUNTS"):
        cursor.market_cursor_capacity(node_count=bad, unique_body_bytes=0)


def test_cross_generation_history_exceeds_old_limit_and_reads_each_original_body_once():
    backend, store, fresh = linear_store()
    assert cursor.inspect_market_chain(fresh(), REGISTRY, collection_id="synthetic-market") == {
        "reference": None, "node_count": 0, "unique_body_bytes": 0,
    }
    previous, references, sizes = None, [], []
    for number in range(1026):
        previous = retain(store, fresh, previous=previous, attempt="root-" + str(number),
                          generation="g" + str(number // 342))
        references.append(previous)
        sizes.append(body_size(store, previous))
    writes = backend.shared["count"]
    backend.shared["body_reads"].clear()
    inspected = cursor.inspect_market_chain(fresh(), REGISTRY, collection_id="synthetic-market")
    assert encoded(inspected) == encoded({
        "reference": references[-1], "node_count": 1026, "unique_body_bytes": sum(sizes)})
    assert len(backend.shared["body_reads"]) == 1026
    # Filtering a generation still authenticates its exact boundary parent.
    backend.shared["body_reads"].clear()
    scoped = cursor.inspect_market_chain(fresh(), REGISTRY,
        collection_id="synthetic-market", generation="g2")
    assert encoded(scoped) == encoded({
        "reference": references[-1], "node_count": 342, "unique_body_bytes": sum(sizes[683:])})
    assert len(backend.shared["body_reads"]) == 343
    assert encoded(cursor.latest_market_reference(fresh(), REGISTRY,
        collection_id="synthetic-market", generation="g2")) == encoded(references[-1])
    assert backend.shared["count"] == writes


def test_cursor_rejects_logical_fork_even_when_all_git_commits_are_reachable():
    backend, store, fresh = linear_store()
    first = retain(store, fresh)
    retain(store, fresh, previous=first, attempt="left")
    retain(store, fresh, previous=first, attempt="right")
    writes = backend.shared["count"]
    with pytest.raises(RecordError, match="ASHARE_CURSOR_AMBIGUOUS"):
        cursor.inspect_market_chain(fresh(), REGISTRY, collection_id="synthetic-market")
    assert backend.shared["count"] == writes


@pytest.mark.parametrize(("mutation", "reason"), [
    ("selection", "EXACT_LOCAL_REFERENCE_REQUIRED"),
    ("digest", "BLOB_DIGEST_MISMATCH"),
    ("unreachable", "COMMIT_NOT_REACHABLE"),
    ("before-created", "OBJECT_MISSING"),
    ("foreign-owner", "ASHARE_CURSOR_OWNER_CHANGED"),
])
def test_cached_original_cannot_authenticate_a_different_parent_reference(mutation, reason):
    backend, store, fresh = linear_store()
    first = retain(store, fresh)
    parent = deepcopy(first)
    if mutation == "selection":
        parent["selection"] = {"kind": "lines", "start": 1, "end": 1, "columns": []}
    elif mutation == "digest":
        parent["manifest"]["sha256"] = "f" * 64
    elif mutation == "unreachable":
        parent["manifest"]["commit"] = "f" * 40
    elif mutation == "before-created":
        parent["manifest"]["commit"] = "0" * 40
    else:
        parent = put_root(store, previous=None, owner="different-owner")
    put_root(store, previous=parent)
    writes = backend.shared["count"]
    with pytest.raises(RecordError, match=reason):
        cursor.inspect_market_chain(fresh(), REGISTRY, collection_id="synthetic-market")
    assert backend.shared["count"] == writes


def test_native_parent_at_later_reachable_commit_is_reverified_but_body_counted_once(tmp_path, monkeypatch):
    backend = GitStore.init_synthetic(tmp_path / "cursor.git")
    store = VersionedStore(backend)
    fresh = lambda: VersionedStore(GitStore(backend.root))
    first = retain(store, fresh)
    unrelated = put_root(store, previous=None, owner="different-owner")
    parent = deepcopy(first)
    parent["manifest"]["commit"] = unrelated["manifest"]["commit"]
    last = retain(store, fresh, previous=parent, attempt="later-parent")
    expected = body_size(store, first) + body_size(store, last)
    before = backend.head()
    reads = []
    original_read = backend.read

    def read(commit, path):
        if path.endswith("/content.txt"):
            reads.append((commit, path))
        return original_read(commit, path)

    monkeypatch.setattr(backend, "read", read)
    result = cursor.inspect_market_chain(store, REGISTRY, collection_id="synthetic-market")
    assert encoded(result) == encoded({"reference": last, "node_count": 2, "unique_body_bytes": expected})
    first_body = first["manifest"]["path"].replace("/manifest.json", "/content.txt")
    assert (first["manifest"]["commit"], first_body) in reads
    assert (parent["manifest"]["commit"], first_body) in reads
    assert len(reads) == 3 and backend.head() == before


def test_cursor_byte_limit_rejects_before_returning_a_tip(monkeypatch):
    backend, store, fresh = linear_store()
    first = retain(store, fresh)
    last = retain(store, fresh, previous=first, attempt="two")
    monkeypatch.setattr(cursor, "MAX_CURSOR_BYTES", body_size(store, first) + body_size(store, last) - 1)
    writes = backend.shared["count"]
    with pytest.raises(RecordError, match="ASHARE_CURSOR_BYTE_BOUND"):
        cursor.inspect_market_chain(fresh(), REGISTRY, collection_id="synthetic-market")
    assert backend.shared["count"] == writes


def test_unbounded_registry_cannot_invalidate_fixed_root_reserve_before_store_io():
    backend, store, _ = linear_store()
    registry = deepcopy(REGISTRY)
    registry.snapshot = {"oversized": "x" * cursor.COLLECTION_ROOT_METADATA_BYTES}
    with pytest.raises(RecordError, match="ASHARE_CURSOR_REGISTRY_BOUND"):
        cursor.inspect_market_chain(store, registry, collection_id="synthetic-market")
    assert backend.shared["head_calls"] == 0 and backend.shared["count"] == 0


@pytest.mark.parametrize("dimension", ["node_count", "unique_body_bytes"])
def test_sync_cursor_capacity_rejection_makes_zero_writes_and_zero_source_requests(monkeypatch, dimension):
    backend, store, fresh = memory()
    request = plan(start=END)
    opener = CollectionOpener(start=END, end=END)
    actual_inspection = cursor.inspect_market_chain

    def nearly_full(*args, **kwargs):
        result = actual_inspection(*args, **kwargs)
        limit = cursor.MAX_CURSOR_VERSIONS if dimension == "node_count" else cursor.MAX_CURSOR_BYTES
        result[dimension] = limit - cursor.CURSOR_WORK_UNIT_RESERVE[dimension] + 1
        return result

    monkeypatch.setattr(cursor, "inspect_market_chain", nearly_full)
    with pytest.raises(RecordError, match="^ASHARE_CURSOR_CAPACITY$"):
        run(request, store, fresh, opener, max_queries=1)
    assert backend.shared["count"] == 0 and backend.head() == "0" * 40
    assert opener.calls == []
