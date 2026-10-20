"""Whole Watch replay retains the exact remote backend's authority and budget."""
from dataclasses import replace

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.domain.records import RecordError, StoreUnavailable
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from decision_kernel.infrastructure import watch_snapshot
from decision_kernel.infrastructure.git_snapshot import bundle_from_bare, restored_bundle
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.infrastructure.watch_snapshot import first_parent_changes, verify_event_parent, watch_read_scope
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
