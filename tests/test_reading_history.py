"""Exact history reuse keeps the original scope and native refusal boundaries."""
from dataclasses import replace
import socket
from types import SimpleNamespace

import pytest

from decision_kernel.application import odds_book
from decision_kernel.domain.records import RecordError, StoreUnavailable
from decision_kernel.infrastructure import github_reading as reading
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.infrastructure.github_reading import _ReadingScope, reading_scope
from decision_kernel.interfaces.store_cli import _PublicReadAPI
from test_reading_scope import retained as retained, transport


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("History reuse tests cannot contact a remote service")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def scene(retained):
    model, local, source, revision = retained
    wire = transport(model)
    owner = remote.GitHubStore(model.binding, wire)
    return SimpleNamespace(model=model, local=local, source=source, revision=revision,
                           wire=wire, owner=owner)


def histories(model):
    return [path for method, path, _ in model.requests if method == "GET" and "/commits?" in path]


def test_parent_nested_and_original_book_scopes_share_only_verified_history(scene):
    owner, model, ref = scene.owner, scene.model, scene.revision
    with reading_scope(owner, fail_closed=True) as parent:
        snapshot = parent.head()
        assert parent.last_change(snapshot, ref.path) == ref.commit
        before = scene.wire._requests
        for _ in range(5):
            assert parent.last_change(snapshot, ref.path) == ref.commit
        assert scene.wire._requests == before
        with reading_scope(parent) as child:
            with reading_scope(child) as grandchild:
                assert grandchild.last_change(snapshot, ref.path) == ref.commit
                assert grandchild.api is parent.api is owner.api
        # The Book keeps its own original owner admission/exit. Only its exact
        # historical answer shares the completed Catalogue/parent proof.
        start = len(model.requests)
        with odds_book._scope(parent, snapshot) as (_, reads):
            assert reads.last_change(snapshot, ref.path) == ref.commit
        live = [path for _, path, _ in model.requests[start:]
                if path == "/repos/" + owner.repository or "/git/ref/" in path]
        assert live == ["/repos/" + owner.repository,
                       "/repos/" + owner.repository + "/git/ref/heads/records/dos2"] * 2
        assert len(histories(model)) == 1
    assert len(owner._reading_history) == 1 and model.writes == 0
    with reading_scope(owner) as next_scope:
        assert next_scope.last_change(snapshot, ref.path) == ref.commit
    assert len(histories(model)) == 1
    before = scene.wire._requests
    for expired in (parent, child, grandchild):
        with pytest.raises(RecordError, match="^READING_SCOPE_CLOSED$"):
            expired.last_change(snapshot, ref.path)
    assert scene.wire._requests == before


def test_ordinary_store_and_fresh_owner_do_not_adopt_another_scopes_memo(scene):
    owner, model, ref = scene.owner, scene.model, scene.revision
    snapshot = model.git.head()
    assert owner.last_change(snapshot, ref.path) == ref.commit
    assert owner.last_change(snapshot, ref.path) == ref.commit
    assert len(histories(model)) == 2 and owner._reading_history == {}
    with reading_scope(owner) as scoped:
        assert scoped.last_change(snapshot, ref.path) == ref.commit
    assert len(histories(model)) == 3
    assert owner.last_change(snapshot, ref.path) == ref.commit
    assert len(histories(model)) == 4
    fresh = remote.GitHubStore(model.binding, scene.wire)
    assert fresh.api is owner.api and fresh._reading_history == {}
    with reading_scope(fresh) as scoped:
        assert scoped.last_change(snapshot, ref.path) == ref.commit
    assert len(histories(model)) == 5


def test_full_head_and_path_keys_do_not_replace_snapshot_admission(scene):
    model, ref = scene.model, scene.revision
    old_head = model.git.head()
    body_path = ref.path.rsplit("/", 1)[0] + "/research.md"
    with reading_scope(scene.owner) as scoped:
        assert scoped.last_change(old_head, ref.path) == ref.commit
        assert scoped.last_change(old_head, body_path) == ref.commit
    new_head = model.git.commit({"submissions/later.json": b"{}\n"}, expected=old_head)
    with reading_scope(scene.owner) as scoped:
        assert scoped.last_change(new_head, ref.path) == ref.commit
    assert len(histories(model)) == len(set(histories(model))) == 3
    # Even though the owner now has a successful answer for the later head,
    # that cache entry cannot authorize a later commit in an earlier scope.
    earlier = _ReadingScope(scene.owner, old_head)
    try:
        with pytest.raises(RecordError, match="^COMMIT_OUTSIDE_BINDING$"):
            earlier.last_change(new_head, ref.path)
        with pytest.raises(RecordError, match="^PATH_OUTSIDE_STORE$"):
            earlier.last_change(old_head, "unexpected/file.json")
    finally:
        earlier.active = False
    assert len(histories(model)) == 3


def test_history_limit_falls_back_without_eviction_or_a_larger_request_budget(scene, monkeypatch):
    assert reading.MAX_READING_HISTORY == 128
    monkeypatch.setattr(reading, "MAX_READING_HISTORY", 1)
    ref, model = scene.revision, scene.model
    body_path = ref.path.rsplit("/", 1)[0] + "/research.md"
    with reading_scope(scene.owner) as scoped:
        head = scoped.head()
        assert scoped.last_change(head, ref.path) == ref.commit
        assert scoped.last_change(head, body_path) == ref.commit
        assert scoped.last_change(head, body_path) == ref.commit
        assert scoped.last_change(head, ref.path) == ref.commit
        assert len(scene.owner._reading_history) == 1
    assert len(histories(model)) == 3
    assert 0 < scene.wire._requests == len(model.requests) < remote.MAX_REQUESTS == 512
    assert model.writes == 0


@pytest.mark.parametrize("damage,reason", [
    ("missing", "RECEIPT_COMMIT_MISSING"),
    ("malformed", "RECEIPT_COMMIT_MISSING"),
    ("invalid-sha", "INVALID_REMOTE_OID"),
    ("before-anchor", "COMMIT_OUTSIDE_BINDING"),
    ("after-head", "COMMIT_OUTSIDE_BINDING"),
    ("changed-content", "RECEIPT_CONTENT_CHANGED"),
])
def test_history_is_remembered_only_after_all_original_checks(retained, damage, reason):
    model, _, _, _ = retained
    path = "submissions/history-proof.json"
    previous = model.git.commit({path: b"old synthetic bytes\n"}, expected=model.git.head())
    head = model.git.commit({path: b"new synthetic bytes\n"}, expected=previous)
    later = model.git.commit({"submissions/after-proof.json": b"{}\n"}, expected=head)
    wire = transport(model)
    owner = remote.GitHubStore(model.binding, wire)
    corrupt = {
        "missing": [], "malformed": {}, "invalid-sha": [{"sha": "not-a-commit"}],
        "before-anchor": [{"sha": model.before_anchor}], "after-head": [{"sha": later}],
        "changed-content": [{"sha": previous}],
    }[damage]
    model.corrupt_response = lambda url, value: corrupt if "/commits?" in url else value
    with reading_scope(owner) as scoped:
        with pytest.raises(RecordError, match="^" + reason + "$"):
            scoped.last_change(head, path)
        assert owner._reading_history == {}
        model.corrupt_response = None
        assert scoped.last_change(head, path) == head
        assert scoped.last_change(head, path) == head
    assert len(histories(model)) == 2 and len(owner._reading_history) == 1
    assert model.writes == 0


@pytest.mark.parametrize("failure", ["authorization", "visibility", "budget"])
def test_api_refusal_blocks_same_and_new_child_warm_hits_without_any_request(retained, failure):
    model, _, _, ref = retained
    model.meta["private"] = False
    binding = replace(model.binding, private_upgrade_allowed=False)
    wire = transport(model)
    api = _PublicReadAPI(wire, binding)
    owner = remote.GitHubStore(binding, api)
    snapshot = model.git.head()
    parent = _ReadingScope(owner, snapshot, fail_closed=False)
    child = _ReadingScope(parent, snapshot, fail_closed=False)
    assert parent.last_change(snapshot, ref.path) == ref.commit
    assert child.last_change(snapshot, ref.path) == ref.commit
    if failure == "authorization":
        error = StoreUnavailable("REMOTE_NOT_AUTHORIZED")
        def denied(path, value):
            raise error
        model.corrupt_response = denied
    elif failure == "visibility":
        model.meta["private"] = True
    else:
        wire._requests = remote.MAX_REQUESTS
    with pytest.raises(RecordError) as refused:
        api.request("GET", "/repos/" + binding.repository)
    assert api._failure is refused.value
    model.corrupt_response = None
    model.meta["private"] = False
    before = (wire._requests, len(model.requests))
    scopes = (parent, child, _ReadingScope(owner, snapshot), _ReadingScope(parent, snapshot))
    try:
        for scoped in scopes:
            with pytest.raises(RecordError) as caught:
                scoped.last_change(snapshot, ref.path)
            assert caught.value is refused.value
            assert (wire._requests, len(model.requests)) == before
        assert len(histories(model)) == 1 and model.writes == 0
    finally:
        for scoped in scopes:
            scoped.active = False


def test_parent_terminal_failure_blocks_cached_descendants_without_api_failure(scene):
    ref, owner = scene.revision, scene.owner
    snapshot = scene.model.git.head()
    parent = _ReadingScope(owner, snapshot, fail_closed=True)
    child = _ReadingScope(parent, snapshot)
    grandchild = _ReadingScope(child, snapshot)
    assert grandchild.last_change(snapshot, ref.path) == ref.commit
    before = scene.wire._requests
    with pytest.raises(RecordError, match="^FOREIGN_REPOSITORY$") as refused:
        parent.resolve(replace(ref, repository="other/repository"))
    assert parent.terminal_failure is refused.value and scene.wire._requests == before
    scopes = (child, grandchild, _ReadingScope(parent, snapshot))
    try:
        for scoped in scopes:
            with pytest.raises(RecordError) as caught:
                scoped.last_change(snapshot, ref.path)
            assert caught.value is refused.value and scene.wire._requests == before
    finally:
        for scoped in (parent, *scopes):
            scoped.active = False


def test_closed_parent_cannot_leave_a_warm_history_child_usable(scene):
    snapshot, ref = scene.model.git.head(), scene.revision
    parent = _ReadingScope(scene.owner, snapshot)
    child = _ReadingScope(parent, snapshot)
    assert child.last_change(snapshot, ref.path) == ref.commit
    parent.active = False
    before = scene.wire._requests
    try:
        with pytest.raises(RecordError, match="^READING_SCOPE_CLOSED$"):
            child.last_change(snapshot, ref.path)
        assert scene.wire._requests == before
    finally:
        child.active = False


@pytest.mark.parametrize("change", ["visibility", "marker", "rewind"])
def test_warm_history_does_not_replace_the_final_native_scope_check(scene, change):
    model, ref = scene.model, scene.revision
    escaped = False
    with pytest.raises(RecordError):
        with reading_scope(scene.owner) as scoped:
            snapshot = scoped.head()
            assert scoped.last_change(snapshot, ref.path) == ref.commit
            if change == "visibility":
                model.meta["private"] = False
            elif change == "marker":
                model.git.commit({"store.json": b"{}\n"}, expected=model.git.head())
            else:
                model.git._run("update-ref", "refs/heads/records/dos2", model.binding.anchor,
                               model.git.head())
            # Like cached bytes, this immutable answer is not an export permit.
            assert scoped.last_change(snapshot, ref.path) == ref.commit
        escaped = True
    assert not escaped and len(histories(model)) == 1 and model.writes == 0
    with pytest.raises(RecordError, match="^READING_SCOPE_CLOSED$"):
        scoped.last_change(snapshot, ref.path)
