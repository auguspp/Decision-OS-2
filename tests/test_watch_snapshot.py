"""Whole Watch replay retains the exact remote backend's authority and budget."""
from dataclasses import replace

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.domain.records import RecordError, StoreUnavailable
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from decision_kernel.infrastructure import native_git_memo, watch_snapshot
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_snapshot import bundle_from_bare, restored_bundle
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.infrastructure.watch_snapshot import (
    first_parent_change, first_parent_changes, verify_event_parent, verify_first_parent, watch_read_scope,
)
from test_market_snapshot import native, local_fetch, source
from test_news_read_budget import transport
from test_watch_activity import apply, change
from test_watch_history import HISTORY_VERSION, at, build_prefix, setup_watch


def public_origin(model):
    model.binding = replace(model.binding, visibility="public", private_upgrade_allowed=True)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    return model.binding


def test_one_original_backend_and_api_with_final_authority_check(native, monkeypatch):
    model, store = native
    public_origin(model)
    reference = store.put_source("original", source("Synthetic original rows"))
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    backend.head()
    assert backend._observed_private_upgrade
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    before, counts = model.git.head(), []
    with watch_read_scope(backend) as reader:
        assert VersionedStore(reader).read_source(reference)["data"] == "Synthetic original rows"
        counts.append(api._requests)
        assert VersionedStore(reader).read_source(reference)["data"] == "Synthetic original rows"
        assert api._requests == counts[-1]
        with pytest.raises(RecordError, match="READ_ONLY"):
            reader.commit({"submissions/no-write.json": b"{}"}, expected=reader.head())
    assert api._requests > counts[-1] and api._requests == len(model.requests) < MAX_REQUESTS == 512
    assert len(calls) == 1 and calls[0]["api"] is api and not calls[0]["destination"].exists()
    assert model.git.head() == before and model.writes == 0


def test_real_records_lineage_keeps_binding_anchor_policy_and_rejects_a_pinned_side_snapshot(native, monkeypatch):
    model, store = native
    original_anchor = model.binding.anchor
    tree = model.git._run("rev-parse", original_anchor + "^{tree}").stdout.decode().strip()
    before = model.git._run("rev-parse", original_anchor + "^").stdout.decode().strip()
    first = model.git._run("commit-tree", tree, "-p", before,
                           data=b"Synthetic first-parent bridge\n").stdout.decode().strip()
    merged = model.git._run("commit-tree", tree, "-p", first, "-p", original_anchor,
                            data=b"Synthetic valid anchor merge\n").stdout.decode().strip()
    model.git._advance(merged, original_anchor)
    reference = store.put_source("after-merge", source("Synthetic bound successor"))
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    with watch_read_scope(backend) as reader:
        assert VersionedStore(reader).read_source(reference)["data"] == "Synthetic bound successor"
        verify_event_parent(reader, reference.manifest.commit, merged)
        assert reader.binding.anchor == original_anchor
    # The binding marker and original any-ancestor anchor rule remain valid;
    # selecting that second-parent commit as the Watch scope cannot prove its
    # own first-parent membership merely because it becomes reader.head().
    with pytest.raises(RecordError, match="OUTSIDE_FIRST_PARENT"):
        with watch_read_scope(backend, original_anchor):
            pytest.fail("A caller-selected side snapshot must not become its own lineage root")
    assert model.binding.anchor == original_anchor and model.writes == 0
    assert backend.api is api and api._requests < MAX_REQUESTS == 512


def test_final_live_merge_cannot_move_the_admitted_snapshot_to_a_side_parent(native, monkeypatch):
    model, store = native
    reference = store.put_source("original", source("Synthetic current original"))
    admitted = model.git.head()
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    released = []
    def read():
        with watch_read_scope(backend) as reader:
            value = VersionedStore(reader).read_source(reference)
            tree = model.git._run("rev-parse", admitted + "^{tree}").stdout.decode().strip()
            parent = model.git._run("rev-parse", admitted + "^").stdout.decode().strip()
            side = model.git._run("commit-tree", tree, "-p", parent,
                                  data=b"Synthetic changed first parent\n").stdout.decode().strip()
            merged = model.git._run("commit-tree", tree, "-p", side, "-p", admitted,
                                    data=b"Synthetic concurrent merge\n").stdout.decode().strip()
            model.git._advance(merged, admitted)
            return value
    with pytest.raises(RecordError, match="LINEAGE_CHANGED"):
        released.append(read())
    assert released == [] and model.writes == 0 and backend.api is api


def test_pointer_change_budget_never_treats_a_truncated_native_history_as_complete(native):
    model, _ = native
    path = "submissions/synthetic-lineage.json"
    originals = [model.git.commit({path: str(index).encode("ascii")}, expected=model.git.head()) for index in range(3)]
    assert first_parent_changes(model.git, model.git.head(), path, limit=3) == list(reversed(originals))
    with pytest.raises(RecordError, match="SELECTION_LIMIT"):
        first_parent_changes(model.git, model.git.head(), path, limit=2)


@pytest.mark.parametrize("when", ["before-scope", "after-read"])
def test_private_upgrade_latch_is_not_reset_by_a_new_snapshot_reader(native, monkeypatch, when):
    model, store = native
    public_origin(model)
    ref = store.put_source("original", source("Synthetic retained bytes"))
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    backend.head()
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    if when == "before-scope": model.meta["private"] = False
    results = []
    def read():
        with watch_read_scope(backend) as reader:
            result = VersionedStore(reader).read_source(ref)
            model.meta["private"] = False
            return result
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        results.append(read())
    assert not results and model.writes == 0 and backend.api is api
    assert len(calls) == (0 if when == "before-scope" else 1)
    assert all(not call["destination"].exists() for call in calls)


def test_final_reachability_failure_prevents_result(native, monkeypatch):
    model, store = native
    ref = store.put_source("original", source("Synthetic history"))
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    released = []
    def read():
        with watch_read_scope(backend) as reader:
            result = VersionedStore(reader).read_source(ref)
            model.git._run("update-ref", "refs/heads/records", model.before_anchor, model.git.head())
            return result
    with pytest.raises(RecordError): released.append(read())
    assert not released and model.writes == 0


@pytest.mark.parametrize("failure", ["fetch", "size", "tree", "budget"])
def test_snapshot_failures_have_no_remote_write_or_fallback(native, monkeypatch, failure):
    model, local = native
    w = setup_watch(local)
    api, calls = transport(model), []
    backend = GitHubStore(model.binding, api)
    if failure in ("fetch", "size"):
        def fetch(binding, used_api, destination):
            calls.append({"api": used_api, "destination": destination})
            raise StoreUnavailable("MARKET_SNAPSHOT_FETCH_FAILED" if failure == "fetch" else "MARKET_SNAPSHOT_SIZE_LIMIT")
    else:
        fetch = local_fetch(model, calls)
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", fetch)
    if failure == "tree":
        # Exercise the existing GitSnapshot gate; no production limit changes.
        monkeypatch.setattr("decision_kernel.infrastructure.git_snapshot.MAX_TREE_ENTRIES", 1)
    if failure == "budget": api._requests = MAX_REQUESTS
    head = model.git.head()
    with pytest.raises(RecordError, match="SNAPSHOT|REMOTE_REQUEST_LIMIT"):
        apply(WatchActivity(VersionedStore(backend)), {**change(w), "history_version": HISTORY_VERSION}, "blocked")
    assert model.git.head() == head and model.writes == 0 and backend.api is api
    assert len(calls) <= 1 and all(not call["destination"].exists() for call in calls)


def test_untrusted_backend_is_rejected_before_it_can_run_or_write():
    class Untrusted:
        def head(self): pytest.fail("Untrusted reader must not be invoked")
    with pytest.raises(RecordError, match="TRUSTED_BACKEND_REQUIRED"):
        with watch_read_scope(Untrusted()): pytest.fail("Untrusted scope must not yield")


def test_active_reading_facade_rechecks_original_owner_and_expired_facade_never_fetches(native, monkeypatch):
    model, store = native
    public_origin(model)
    source_ref = store.put_source("original", source("Synthetic room bytes"))
    api, calls = transport(model), []
    owner = GitHubStore(model.binding, api)
    monkeypatch.setattr(watch_snapshot, "_fetch_market_branch", local_fetch(model, calls))
    with reading_scope(owner) as facade:
        assert facade.owner is owner and owner._observed_private_upgrade
        future = store.put_source("later", source("After the room admission snapshot"))
        with pytest.raises(RecordError):
            with watch_read_scope(facade, future.manifest.commit):
                pytest.fail("The native Watch reader cannot widen the facade snapshot")
        assert calls == []
        def read():
            with watch_read_scope(facade) as backend:
                result = VersionedStore(backend).read_source(source_ref)
                model.meta["private"] = False
                return result
        released = []
        with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
            released.append(read())
        assert not released and len(calls) == 1 and calls[0]["api"] is api
        # Restore the synthetic fixture only so the existing outer scope can
        # finish its own check; the rejected Watch read has already returned no data.
        model.meta["private"] = True
    count = len(calls)
    with pytest.raises(RecordError, match="READING_SCOPE_CLOSED"):
        with watch_read_scope(facade): pytest.fail("Closed facade must not yield")
    assert len(calls) == count and all(not item["destination"].exists() for item in calls)
    assert owner.api is api and model.writes == 0


def test_native_backup_retains_v2_pointer_and_exact_seal_for_independent_offline_reader(native, tmp_path):
    model, store = native
    data = build_prefix(store, setup_watch(store), versioned=False, count=4)
    service = WatchActivity(store)
    tip = apply(service, {**change(data["watch_ref"], data["refs"][-1], "migrate", at(4)),
                          "history_version": HISTORY_VERSION}, "migrate")
    view = service.read(tip)
    seal = view["sealed_segments"][0]["reference"]
    snapshot = model.git.head()
    binding = replace(model.binding, write_enabled=False)
    bundle = tmp_path / "history.bundle"
    audit = bundle_from_bare(model.git.root, binding, snapshot, bundle)
    assert seal in audit["artifacts"] and audit["watch_activities"][view["event"]["watch_key"]] == tip
    assert audit["watch_activity_replay"].startswith("not-performed")
    with restored_bundle(bundle, binding, snapshot) as backend:
        fresh = WatchActivity(VersionedStore(backend))
        assert fresh.read(tip, include_current_use=False)["event"] == view["event"]
        part = fresh.read_segment(seal)
        assert part["segment"]["events"] == data["refs"]
        assert part["history_validation"]["verified_events"] == 5


def observe_native(monkeypatch):
    original, calls = GitStore._run, []
    def run(self, *args, data=None, index=None, allowed=(0,)):
        calls.append((args, tuple(allowed), data is not None, index is not None))
        return original(self, *args, data=data, index=index, allowed=allowed)
    monkeypatch.setattr(GitStore, "_run", run)
    return calls


def test_scope_memo_reuses_original_bytes_and_nested_queries_but_always_reads_live_head(native, monkeypatch):
    model, _ = native
    path, raw = "submissions/scoped-memo.json", b"original immutable bytes"
    head = model.git.commit({path: raw}, expected=model.git.head())
    calls = observe_native(monkeypatch)
    with watch_read_scope(model.git) as reader:
        assert reader.read(head, path) == raw
        count = len(calls)
        assert reader.read(head, path) == raw and len(calls) == count
        reader.check_commit(head)
        count = len(calls)
        reader.check_commit(head)
        assert [call[0] for call in calls[count:]] == [
            ("rev-parse", "--verify", "--quiet", "refs/heads/records")]
        count = len(calls)
        reader.head(); reader.head()
        assert len(calls) == count + 2
        with watch_read_scope(reader, head) as nested:
            assert nested is reader
            count = len(calls)
            assert nested.read(head, path) == raw and len(calls) == count
        # CompletedProcess is mutable; one caller cannot replace another's bytes.
        command = next(call[0] for call in calls if call[0][:2] == ("cat-file", "blob"))
        first = reader._run(*command)
        first.stdout, first.args = b"caller mutation", ["altered"]
        second = reader._run(*command)
        assert second is not first and second.stdout == raw and second.args != first.args
        with pytest.raises(RecordError, match="READ_ONLY"):
            reader.commit({path: b"cannot publish"}, expected=head)
    assert model.git.head() == head
    with pytest.raises(RecordError, match="SCOPE_CLOSED"):
        reader.read(head, path)
    with pytest.raises(RecordError, match="SCOPE_CLOSED"):
        with watch_read_scope(reader): pytest.fail("Closed views cannot re-enter")
    with watch_read_scope(model.git) as fresh:
        count = len(calls)
        assert fresh is not reader and fresh.read(head, path) == raw
        assert len(calls) == count + 3


def test_exceptional_scope_exit_discards_memo_and_closes_nested_view(native):
    model, _ = native
    head = model.git.head()
    class Interrupted(Exception):
        pass
    with pytest.raises(Interrupted):
        with watch_read_scope(model.git) as reader:
            with watch_read_scope(reader) as nested:
                assert nested.read(head, "store.json")
                raise Interrupted()
    assert not reader._memo and reader._memo_bytes == 0
    with pytest.raises(RecordError, match="SCOPE_CLOSED"):
        nested.head()
    with watch_read_scope(model.git) as fresh:
        assert fresh.read(head, "store.json")


def test_warm_graph_and_path_memo_cannot_hide_a_new_records_first_parent(native):
    model, _ = native
    path = "submissions/warm-lineage.json"
    base = model.git.head()
    original = model.git.commit({path: b"original"}, expected=base)
    with watch_read_scope(model.git, base) as reader:
        verify_first_parent(reader, original)
        assert first_parent_change(reader, original, path) == original
        tree = model.git._run("rev-parse", original + "^{tree}").stdout.decode().strip()
        side = model.git._run("commit-tree", tree, "-p", base,
                              data=b"Synthetic replacement first parent\n").stdout.decode().strip()
        merged = model.git._run("commit-tree", tree, "-p", side, "-p", original,
                                data=b"Synthetic side-parent admission\n").stdout.decode().strip()
        model.git._advance(merged, original)
        reader.check_commit(original)  # Original any-parent reachability still holds.
        with pytest.raises(RecordError, match="OUTSIDE_FIRST_PARENT"):
            verify_first_parent(reader, original)
        with pytest.raises(RecordError, match="OUTSIDE_FIRST_PARENT"):
            first_parent_change(reader, original, path)
        with pytest.raises(RecordError, match="OUTSIDE_FIRST_PARENT"):
            with watch_read_scope(reader, original): pytest.fail("A warm side prefix is not eligible")
    assert model.git.head() == merged


def test_native_memo_never_retains_negative_failure_symbolic_or_input_dependent_queries(native, monkeypatch, tmp_path):
    model, _ = native
    head = model.git.head()
    tree = model.git._run("rev-parse", head + "^{tree}").stdout.decode().strip()
    future = model.git._run("commit-tree", tree, "-p", head,
                            data=b"Synthetic unreachable successor\n").stdout.decode().strip()
    calls = observe_native(monkeypatch)
    with watch_read_scope(model.git) as reader:
        negative = ("merge-base", "--is-ancestor", future, head)
        count = len(calls)
        assert reader._run(*negative, allowed=(0, 1, 128)).returncode == 1
        assert reader._run(*negative, allowed=(0, 1, 128)).returncode == 1
        assert len(calls) == count + 2
        missing = ("cat-file", "-s", "0" * 40)
        count = len(calls)
        for _ in range(2):
            with pytest.raises(StoreUnavailable): reader._run(*missing)
        assert len(calls) == count + 2
        absent = "submissions/not-present.json"
        for command in (("ls-tree", "-z", head, "--", absent),
                        ("log", "-1", "--format=%H", head, "--", absent)):
            count = len(calls)
            assert reader._run(*command).stdout == b""
            assert reader._run(*command).stdout == b""
            assert len(calls) == count + 2
        for command in (("rev-list", "--all", "--count"),
                        ("log", "-1", "--format=%H", "HEAD", "--", "store.json")):
            count = len(calls)
            reader._run(*command); reader._run(*command)
            assert len(calls) == count + 2
        query = ("ls-tree", "-z", head, "--", "store.json")
        reader._run(*query)
        count = len(calls)
        reader._run(*query, allowed=(0, 1))
        reader._run(*query, allowed=(0, 1))
        assert len(calls) == count + 1  # A different allowed result contract has its own entry.
        for options in ({"data": b""}, {"index": str(tmp_path / "unused-index")}):
            count = len(calls)
            reader._run(*query, **options); reader._run(*query, **options)
            assert len(calls) == count + 2
    assert model.git.head() == head


@pytest.mark.parametrize("cap", ["bytes", "entries"])
def test_memo_capacity_exhaustion_keeps_original_queries_and_results(native, monkeypatch, cap):
    model, _ = native
    path, raw = "submissions/memo-cap.json", b"retained original" * 256
    head = model.git.commit({path: raw}, expected=model.git.head())
    if cap == "bytes": monkeypatch.setattr(native_git_memo, "_MEMO_BYTES", 1024)
    else: monkeypatch.setattr(native_git_memo, "MAX_TREE_ENTRIES", 1)
    calls = observe_native(monkeypatch)
    with watch_read_scope(model.git) as reader:
        assert reader.read(head, path) == raw
        count = len(calls)
        assert reader.read(head, path) == raw and len(calls) > count
        assert reader._memo_bytes <= native_git_memo._MEMO_BYTES
        assert len(reader._memo) <= native_git_memo.MAX_TREE_ENTRIES
    assert model.git.head() == head


@pytest.mark.parametrize("cap", ["bytes", "entries"])
def test_memo_capacity_turnover_keeps_recent_queries_and_rereads_evicted_bytes(native, monkeypatch, cap):
    model, _ = native
    originals = [bytes([index]) * 4096 for index in range(4)]
    objects = [model.git._run("hash-object", "-w", "--stdin", data=raw).stdout.decode().strip()
               for raw in originals]
    if cap == "bytes":
        monkeypatch.setattr(native_git_memo, "_MEMO_BYTES", 10000)
    else:
        monkeypatch.setattr(native_git_memo, "MAX_TREE_ENTRIES", 2)
    calls = observe_native(monkeypatch)
    with watch_read_scope(model.git) as reader:
        def read(index):
            result = reader._run("cat-file", "blob", objects[index])
            assert result.stdout == originals[index]
            assert reader._memo_bytes <= native_git_memo._MEMO_BYTES
            assert len(reader._memo) <= native_git_memo.MAX_TREE_ENTRIES

        read(0); read(1)
        # Refresh the hot entry before each new object; a FIFO or permanently
        # full cache would lose it or keep rereading the new working set.
        for index in (2, 3):
            count = len(calls)
            read(0)
            assert len(calls) == count
            read(index)
            assert len(calls) == count + 1
            read(index); read(0)
            assert len(calls) == count + 1
        count = len(calls)
        read(1)  # The old cold entry is fetched afresh, with identical bytes.
        assert len(calls) == count + 1
        read(1)
        assert len(calls) == count + 1
    assert not reader._memo and reader._memo_bytes == 0


@pytest.mark.parametrize("cap", ["total", "entry", "entry_overhead"])
def test_oversized_native_result_does_not_evict_the_current_working_set(native, monkeypatch, cap):
    model, _ = native
    small, large = b"small immutable original", b"large original" * 1024
    if cap == "total":
        monkeypatch.setattr(native_git_memo, "_MEMO_BYTES", 4096)
    elif cap == "entry":
        large *= 8
        assert native_git_memo._MEMO_ENTRY_BYTES < len(large) < native_git_memo._MEMO_BYTES
    else:
        # A body just below 64 KiB still exceeds the complete charged entry size
        # once its immutable query, command and result overhead are included.
        large = b"x" * (64 * 1024 - 1)
        assert len(large) < native_git_memo._MEMO_ENTRY_BYTES < native_git_memo._MEMO_BYTES
    objects = [model.git._run("hash-object", "-w", "--stdin", data=raw).stdout.decode().strip()
               for raw in (small, large)]
    calls = observe_native(monkeypatch)
    with watch_read_scope(model.git) as reader:
        assert reader._run("cat-file", "blob", objects[0]).stdout == small
        retained_bytes, retained_entries = reader._memo_bytes, len(reader._memo)
        count = len(calls)
        for index in range(2):
            assert reader._run("cat-file", "blob", objects[1]).stdout == large
            assert len(calls) == count + index + 1
            assert reader._run("cat-file", "blob", objects[0]).stdout == small
            assert len(calls) == count + index + 1
            assert reader._memo_bytes == retained_bytes
            assert len(reader._memo) == retained_entries
    assert not reader._memo and reader._memo_bytes == 0


def test_memo_keeps_snapshot_path_filter_and_complete_history_count_distinct(native):
    model, _ = native
    path, other = "submissions/memo-history.json", "submissions/memo-other.json"
    base = model.git.head()
    first = model.git.commit({path: b"one", other: b"other"}, expected=base)
    second = model.git.commit({path: b"two"}, expected=first)
    tree = model.git._run("rev-parse", base + "^{tree}").stdout.decode().strip()
    removed = model.git._run("commit-tree", tree, "-p", second,
                             data=b"Synthetic original deletion\n").stdout.decode().strip()
    model.git._advance(removed, second)
    with watch_read_scope(model.git) as reader:
        assert first_parent_change(reader, base, path) is None
        assert first_parent_change(reader, removed, path) == removed
        assert first_parent_change(reader, removed, path, present_only=True) == second
        assert first_parent_change(reader, second, path) == second
        assert first_parent_change(reader, second, other) == first
        rows = first_parent_changes(reader, removed, path, limit=3)
        assert rows == [removed, second, first]
        rows.clear()
        assert first_parent_changes(reader, removed, path, limit=3) == [removed, second, first]
        with pytest.raises(RecordError, match="SELECTION_LIMIT"):
            first_parent_changes(reader, removed, path, limit=2)


def test_custom_native_subclass_keeps_original_overrides_without_a_memo(tmp_path):
    reads = []
    class CustomStore(GitStore):
        def read(self, commit, path):
            reads.append((commit, path))
            return super().read(commit, path)
    custom = CustomStore.init_synthetic(tmp_path / "custom.git")
    with watch_read_scope(custom) as reader:
        assert reader is custom
        head, count = reader.head(), len(reads)
        assert reader.read(head, "store.json") == reader.read(head, "store.json")
        assert len(reads) == count + 2
