"""Exact local trees behind the public console's original bounded GitHub API.

Real Git owns every synthetic record, tree and branch change. Only the HTTP
transport is modeled; no production request, source fetch or write occurs.
"""
from copy import deepcopy
from dataclasses import asdict, replace
from http.client import IncompleteRead
import argparse
import json
import shutil
import subprocess
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded,
)
from decision_kernel.infrastructure.git_ancestry import LocalGitAncestry
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.git_tree import LocalGitTree
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.interfaces import reading_page, store_cli
from test_github_store import GitAPI
from test_github_transport import Response


def git(root, *args, data=None):
    result = subprocess.run(
        ["git", "--git-dir=" + str(root), *args], input=data,
        capture_output=True, check=True, timeout=30,
        env={**GitStore._env(), "GIT_NO_LAZY_FETCH": "1"})
    return result.stdout


class Wire:
    """Keep the real API's response limits, errors and request accounting."""
    def __init__(self, model):
        self.model, self.calls = model, []
        self.tree_error = None
        self.responses = {}

    def open(self, request, timeout):
        assert timeout == 30
        assert request.method == "GET", "the local mirror must never send a write"
        path = request.full_url.removeprefix(remote.API_ROOT)
        self.calls.append(path)
        if "/git/trees/" in path and self.tree_error is not None:
            raise self.tree_error
        value = (deepcopy(self.responses[path]) if path in self.responses else
                 self.model.request(request.method, path))
        return Response(encoded(value), request.full_url)


@pytest.fixture
def scene(tmp_path, monkeypatch):
    model = GitAPI(tmp_path / "native-model.git")
    model.binding = replace(model.binding, repository="example/public-store",
                            visibility="public", write_enabled=False)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta.update(full_name=model.binding.repository, private=False)
    model.git.repository = model.binding.repository
    monkeypatch.setattr("decision_kernel.application.versioned_store._now",
                        lambda: "2026-01-03T01:00:00Z")
    store = VersionedStore(model.git)
    source = store.put_source("fixture-source", SourceInput(
        "fixture-source", "content", "Synthetic retained evidence",
        "synthetic:local-tree", "Original synthetic source bytes."))
    revision = store.submit_revision("fixture-research", RevisionInput(
        "fixture-case", "r1", "full", "Original synthetic research body.",
        "Engineering tree fixture", "synthetic", (source,)))
    snapshot = model.git.head()
    mirror = tmp_path / "local-mirror.git"
    shutil.copytree(model.git.root, mirror)
    reader = LocalGitTree(mirror, max_entries=remote.MAX_TREE_ENTRIES,
                          max_bytes=remote.MAX_RESPONSE)
    calls, native_read = [], reader.read

    def read(tree_sha):
        calls.append(tree_sha)
        return native_read(tree_sha)

    monkeypatch.setattr(reader, "read", read)
    wire = remote.GitHubAPI("synthetic-read-only-tree-test")
    wire._opener = Wire(model)
    binding_file = tmp_path / "read-binding.json"
    binding_file.write_bytes(encoded(asdict(model.binding)))
    return SimpleNamespace(model=model, store=store, binding=model.binding,
        binding_file=binding_file, snapshot=snapshot, source=source, revision=revision,
        mirror=mirror, reader=reader, local_calls=calls, wire=wire, opener=wire._opener,
        prefix="/repos/" + model.binding.repository)


def public_api(scene):
    return store_cli._PublicReadAPI(scene.wire, scene.binding, tree_reader=scene.reader)


def tree_oid(scene, commit=None):
    return git(scene.model.git.root, "rev-parse", (commit or scene.snapshot) + "^{tree}").decode().strip()


def tree_path(scene, tree=None):
    return scene.prefix + "/git/trees/" + (tree or tree_oid(scene)) + "?recursive=1"


def recursive_calls(scene):
    return [path for path in scene.opener.calls if "/git/trees/" in path]


def lose_object(scene, oid):
    # The mirror is an independent temporary copy. Never remove native objects.
    (scene.mirror / "objects" / oid[:2] / oid[2:]).unlink()


def copy_new_objects(scene):
    root = scene.model.git.root / "objects"
    for source in root.rglob("*"):
        target = scene.mirror / "objects" / source.relative_to(root)
        if source.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def parse_command(scene, *command):
    parser = argparse.ArgumentParser()
    store_cli.configure(parser.add_subparsers(dest="command", required=True))
    return parser.parse_args(["store", "--binding", str(scene.binding_file), *command])


def room_args(scene, tmp_path):
    plan, output = tmp_path / "room-plan.json", tmp_path / "room.html"
    plan.write_bytes(encoded({"as_of": "2026-01-03T03:00:00Z", "catalogue": {"limit": 1},
                              "reference": asdict(scene.revision)}))
    return parse_command(scene, "research-room", "--input", str(plan), "--out", str(output)), output


@pytest.mark.parametrize("operation", ["catalogue", "resume", "room"])
def test_real_readers_succeed_when_full_recursive_http_is_unavailable(
        scene, tmp_path, monkeypatch, capsys, operation):
    scene.opener.tree_error = IncompleteRead(b"synthetic truncated tree", 100)
    monkeypatch.setattr(remote.GitHubAPI, "from_environment", lambda: scene.wire)
    output = None
    if operation == "catalogue":
        args = parse_command(scene, "research-catalogue", "--limit", "1")
    elif operation == "resume":
        args = parse_command(scene, "resume-research", "--case-id", "fixture-case")
    else:
        args, output = room_args(scene, tmp_path)
    assert store_cli.execute(args, ancestry=LocalGitAncestry(scene.mirror),
        tree_reader=scene.reader, read_binding=scene.binding) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    result = json.loads(captured.out)
    if operation == "catalogue":
        assert result["snapshot"] == scene.snapshot
        assert result["items"][0]["reference"] == asdict(scene.revision)
    elif operation == "resume":
        assert result["research"]["reference"] == asdict(scene.revision)
        assert result["source_page"]["items"][0]["view"]["data"] == "Original synthetic source bytes."
    else:
        assert result["catalogue"]["snapshot"] == scene.snapshot
        assert result["readings"][0]["reference"] == asdict(scene.revision)
        assert "Original synthetic research body." in output.read_text(encoding="utf-8")
    assert scene.local_calls and recursive_calls(scene) == []
    assert any("/git/commits/" in path for path in scene.opener.calls)
    assert any("/git/blobs/" in path for path in scene.opener.calls)
    assert 0 < scene.wire._requests == len(scene.opener.calls) < remote.MAX_REQUESTS == 512
    assert scene.model.git.head() == scene.snapshot and scene.model.writes == 0


def test_only_a_matching_native_commit_authorizes_the_exact_tree(scene):
    api, tree = public_api(scene), tree_oid(scene)
    api.request("GET", scene.prefix + "/git/commits/" + scene.snapshot)
    before = scene.wire._requests
    scene.opener.tree_error = IncompleteRead(b"not used", 100)
    value = api.request("GET", tree_path(scene, tree))
    assert value["sha"] == tree and value["truncated"] is False
    assert scene.local_calls == [tree]
    assert scene.wire._requests == before and recursive_calls(scene) == []
    assert api.rate_limits is scene.wire.rate_limits


@pytest.mark.parametrize("observation", [
    "none", "mismatched-commit", "invalid-tree", "symbolic-ref", "query", "foreign-repository",
])
def test_unobserved_or_mismatched_native_metadata_cannot_enable_a_local_tree(scene, observation):
    api, tree = public_api(scene), tree_oid(scene)
    path = scene.prefix + "/git/commits/" + scene.snapshot
    metadata = {"sha": scene.snapshot, "tree": {"sha": tree}}
    if observation == "mismatched-commit":
        metadata["sha"] = "a" * 40
    elif observation == "invalid-tree":
        metadata["tree"]["sha"] = "HEAD"
    elif observation == "symbolic-ref":
        path = scene.prefix + "/git/commits/records"
    elif observation == "query":
        path += "?untrusted=1"
    elif observation == "foreign-repository":
        path = "/repos/example/elsewhere/git/commits/" + scene.snapshot
    if observation != "none":
        scene.opener.responses[path] = metadata
        api.request("GET", path)
    scene.opener.tree_error = IncompleteRead(b"expected remote fallback", 100)
    with pytest.raises(StoreUnavailable, match="^REMOTE_RESPONSE_UNAVAILABLE$"):
        api.request("GET", tree_path(scene, tree))
    assert scene.local_calls == []
    assert recursive_calls(scene) == [tree_path(scene, tree)]


@pytest.mark.parametrize("metadata", [None, []])
def test_nondict_native_commit_keeps_the_shared_validation_failure(scene, metadata):
    scene.opener.responses[scene.prefix + "/git/commits/" + scene.binding.anchor] = metadata
    with pytest.raises(RecordError, match="^REMOTE_COMMIT_MISMATCH$"):
        remote.GitHubStore(scene.binding, public_api(scene))
    assert scene.local_calls == [] and recursive_calls(scene) == []


def test_a_full_observed_tree_set_falls_back_without_growing_or_resetting_budget(scene):
    api, tree = public_api(scene), tree_oid(scene)
    # Seed the actual capacity boundary without consuming the native session's
    # entire HTTP allowance merely to prepare this independent cache-limit case.
    api._native_trees.update(f"{number:040x}" for number in range(remote.MAX_REQUESTS))
    assert tree not in api._native_trees
    before = scene.wire._requests
    api.request("GET", scene.prefix + "/git/commits/" + scene.snapshot)
    assert api.request("GET", tree_path(scene, tree))["sha"] == tree
    assert len(api._native_trees) == remote.MAX_REQUESTS == 512
    assert tree not in api._native_trees and scene.local_calls == []
    assert scene.wire._requests == before + 2
    assert recursive_calls(scene) == [tree_path(scene, tree)]


@pytest.mark.parametrize("missing", ["root", "descendant", "blob"])
def test_incomplete_mirror_falls_back_once_to_the_same_bounded_api(scene, missing):
    target = tree_oid(scene)
    if missing == "root":
        absent = target
    else:
        path = "research" if missing == "descendant" else scene.revision.path
        absent = git(scene.model.git.root, "rev-parse", scene.snapshot + ":" + path).decode().strip()
    lose_object(scene, absent)
    api = public_api(scene)
    backend = remote.GitHubStore(scene.binding, api)
    result = ResearchReader(VersionedStore(backend)).resume("fixture-case")
    assert result["research"]["reference"] == asdict(scene.revision)
    assert result["source_page"]["items"][0]["view"]["data"] == "Original synthetic source bytes."
    assert recursive_calls(scene).count(tree_path(scene, target)) == 1
    assert scene.wire._requests == len(scene.opener.calls) < remote.MAX_REQUESTS
    assert api.rate_limits is scene.wire.rate_limits
    assert scene.model.writes == 0


@pytest.mark.parametrize("failure,code", [
    ("incomplete", "REMOTE_RESPONSE_UNAVAILABLE"),
    ("unauthorized", "REMOTE_NOT_AUTHORIZED"),
])
def test_local_miss_preserves_native_failure_and_sticky_denial(scene, failure, code):
    lose_object(scene, tree_oid(scene))
    scene.opener.tree_error = (IncompleteRead(b"synthetic partial response", 100)
        if failure == "incomplete" else HTTPError(
            remote.API_ROOT + tree_path(scene), 401, "synthetic denial", {}, None))
    api = public_api(scene)
    with pytest.raises(StoreUnavailable, match="^" + code + "$") as error:
        remote.GitHubStore(scene.binding, api)
    assert recursive_calls(scene) == [tree_path(scene)]
    before, local_before = scene.wire._requests, len(scene.local_calls)
    scene.opener.tree_error = None
    with pytest.raises(StoreUnavailable) as repeated:
        api.request("GET", scene.prefix)
    assert repeated.value is error.value
    assert scene.wire._requests == before == len(scene.opener.calls)
    assert len(scene.local_calls) == local_before


def test_fallback_does_not_reset_the_original_512_request_budget(scene):
    api = public_api(scene)
    api.request("GET", scene.prefix + "/git/commits/" + scene.snapshot)
    lose_object(scene, tree_oid(scene))
    scene.wire._requests = remote.MAX_REQUESTS - 1
    assert api.request("GET", tree_path(scene))["sha"] == tree_oid(scene)
    assert scene.wire._requests == remote.MAX_REQUESTS == 512
    with pytest.raises(StoreUnavailable, match="^REMOTE_REQUEST_LIMIT$"):
        api.request("GET", scene.prefix)
    assert recursive_calls(scene) == [tree_path(scene)]
    assert scene.wire._requests == remote.MAX_REQUESTS + 1


def test_a_later_native_head_uses_its_exact_tree_even_with_a_stale_local_ref(scene):
    api = public_api(scene)
    backend = remote.GitHubStore(scene.binding, api, ancestry=LocalGitAncestry(scene.mirror))
    newer = scene.store.submit_revision("fixture-newer", RevisionInput(
        "fixture-case", "r2", "full", "Newer synthetic research body.",
        "Engineering tree fixture", "synthetic", (scene.source,), scene.revision))
    head = scene.model.git.head()
    copy_new_objects(scene)  # Objects advance; the mirror's branch deliberately does not.
    assert git(scene.mirror, "rev-parse", "refs/heads/records").decode().strip() == scene.snapshot
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    result = ResearchReader(VersionedStore(backend)).resume("fixture-case")
    assert result["research"]["reference"] == asdict(newer)
    assert result["research"]["body"] == "Newer synthetic research body."
    assert tree_oid(scene, head) in scene.local_calls
    assert recursive_calls(scene) == []
    assert scene.model.writes == 0


def test_independent_nested_stores_share_the_same_accelerated_api_and_native_checks(scene):
    api = public_api(scene)
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    first = remote.GitHubStore(scene.binding, api)
    first_count = scene.wire._requests
    nested = remote.GitHubStore(replace(scene.binding, write_enabled=False), first.api)
    assert nested is not first and nested.api is first.api is api
    assert ResearchReader(VersionedStore(nested)).resume("fixture-case")["research"]["reference"] == asdict(scene.revision)
    assert scene.wire._requests > first_count
    assert recursive_calls(scene) == []
    assert scene.opener.calls.count(scene.prefix) >= 2
    assert scene.wire._requests == len(scene.opener.calls)


def test_market_reader_native_admission_uses_the_same_local_tree_wrapper(scene):
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader

    api = public_api(scene)
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    fetched = []

    def fetch(binding, shared_api, destination):
        assert binding == scene.binding and shared_api is api
        fetched.append(destination)
        shutil.copytree(scene.model.git.root, destination)

    reader = GitMarketReader(scene.binding, api, fetch=fetch)
    with reader.market_read_scope():
        assert reader.head() == scene.snapshot
        ref = scene.source.manifest
        assert reader.read(ref.commit, ref.path) == scene.model.git.read(ref.commit, ref.path)
    assert fetched and scene.local_calls and recursive_calls(scene) == []
    assert scene.opener.calls.count(scene.prefix) >= 2
    assert scene.wire._requests == len(scene.opener.calls)
    assert scene.model.writes == 0


def test_native_public_reversal_stops_even_an_already_observed_local_tree(scene):
    api = public_api(scene)
    backend = remote.GitHubStore(scene.binding, api)
    before, local_before = scene.wire._requests, len(scene.local_calls)
    scene.model.meta["private"] = True
    with pytest.raises(RecordError, match="^LOCAL_READ_PUBLIC_STORE_ONLY$") as error:
        backend.head()
    assert scene.wire._requests == before + 1
    assert len(scene.local_calls) == local_before
    scene.model.meta["private"] = False
    with pytest.raises(RecordError) as repeated:
        api.request("GET", tree_path(scene))
    assert repeated.value is error.value
    assert scene.wire._requests == before + 1 and len(scene.local_calls) == local_before


def test_public_reversal_after_mirrored_room_render_releases_no_html_or_stdout(
        scene, tmp_path, monkeypatch, capsys):
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    monkeypatch.setattr(remote.GitHubAPI, "from_environment", lambda: scene.wire)
    native_render = reading_page.render_page
    rendered = []

    def render(value):
        result = native_render(value)
        rendered.append(value)
        scene.model.meta["private"] = True
        return result

    monkeypatch.setattr(reading_page, "render_page", render)
    args, output = room_args(scene, tmp_path)
    assert store_cli.execute(args, tree_reader=scene.reader, read_binding=scene.binding) == 2
    captured = capsys.readouterr()
    assert rendered and not output.exists() and captured.out == ""
    assert json.loads(captured.err) == {"error": "LOCAL_READ_PUBLIC_STORE_ONLY"}
    assert scene.local_calls and recursive_calls(scene) == []


def add_submission_entry(scene, name, mode, kind, oid):
    """Construct native trees, including entries ordinary worktrees cannot hold."""
    root, head = scene.model.git.root, scene.model.git.head()
    subtree = git(root, "rev-parse", head + ":submissions").decode().strip()
    rows = git(root, "ls-tree", "-z", subtree)
    changed = git(root, "mktree", "-z", data=rows +
        f"{mode} {kind} {oid}\t{name}\0".encode()).decode().strip()
    rows = git(root, "ls-tree", "-z", head).split(b"\0")
    rows = [row for row in rows if row and row.split(b"\t", 1)[1] != b"submissions"]
    rows.append(f"040000 tree {changed}\tsubmissions".encode())
    tree = git(root, "mktree", "-z", data=b"\0".join(rows) + b"\0").decode().strip()
    commit = git(root, "commit-tree", tree, "-p", head,
                 "-m", "Synthetic complete-tree boundary").decode().strip()
    git(root, "update-ref", "refs/heads/records", commit, head)
    copy_new_objects(scene)
    return commit, tree


def test_empty_directories_survive_and_count_against_the_shared_whole_tree_limit(scene, monkeypatch):
    empty = git(scene.model.git.root, "mktree", data=b"").decode().strip()
    head, tree = add_submission_entry(scene, "retained-empty", "040000", "tree", empty)
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    backend = remote.GitHubStore(scene.binding, public_api(scene))
    entries = backend._tree(head)
    assert "submissions/retained-empty" not in entries
    assert "submissions/retained-empty" in backend._tree_directories[head]
    assert backend._last_tree_directories == (head, backend._tree_directories[head])
    actual = [row for row in git(scene.model.git.root, "ls-tree", "-r", "-t", "-z", tree).split(b"\0") if row]
    assert len(entries) + len(backend._tree_directories[head]) == len(actual)
    monkeypatch.setattr(remote, "MAX_TREE_ENTRIES", len(actual) - 1)
    with pytest.raises(RecordError, match="^REMOTE_TREE_LIMIT$"):
        remote.GitHubStore(scene.binding, public_api(scene))
    assert recursive_calls(scene) == []


@pytest.mark.parametrize("mode,kind", [("100755", "blob"), ("120000", "blob"), ("160000", "commit")])
def test_nonregular_entries_are_preserved_for_the_original_store_to_refuse(scene, mode, kind):
    oid = (scene.snapshot if kind == "commit" else
           git(scene.model.git.root, "hash-object", "-w", "--stdin", data=b"synthetic\n").decode().strip())
    add_submission_entry(scene, "forbidden.json", mode, kind, oid)
    scene.opener.tree_error = IncompleteRead(b"must not download a tree", 100)
    with pytest.raises(RecordError, match="^REGULAR_REMOTE_BLOB_REQUIRED$"):
        remote.GitHubStore(scene.binding, public_api(scene))
    assert scene.local_calls and recursive_calls(scene) == []
    assert scene.model.writes == 0
