"""Validated Store rows may use local bytes without changing native authority."""
from dataclasses import replace
from http.client import IncompleteRead
import base64
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.domain.records import BlobRef, RecordError, StoreUnavailable, digest
from decision_kernel.infrastructure.git_ancestry import LocalGitAncestry
from decision_kernel.infrastructure.git_tree import LocalGitTree
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.interfaces import store_cli
from test_local_tree_mirror import (
    copy_new_objects, git, lose_object, parse_command, recursive_calls,
    room_args, scene as scene,
)


@pytest.fixture(params=[False, True], ids=["one-shot", "batch"])
def blobs(scene, monkeypatch, request):
    if request.param:
        scene.reader = LocalGitTree(scene.mirror, max_entries=remote.MAX_TREE_ENTRIES,
            max_bytes=remote.MAX_RESPONSE, reuse_processes=True)
    scene.blob_calls = []
    native_read = scene.reader.read_blob

    def read(oid, expected_size):
        scene.blob_calls.append((oid, expected_size))
        return native_read(oid, expected_size)

    monkeypatch.setattr(scene.reader, "read_blob", read)
    try:
        yield scene
    finally:
        scene.reader.close()


def public_api(scene):
    return store_cli._PublicReadAPI(scene.wire, scene.binding,
        tree_reader=scene.reader, blob_reader=scene.reader)


def backend_for(scene):
    return remote.GitHubStore(scene.binding, public_api(scene),
                             ancestry=LocalGitAncestry(scene.mirror))


def blob_path(scene, oid):
    return scene.prefix + "/git/blobs/" + oid


def blob_calls(scene):
    return [path for path in scene.opener.calls if "/git/blobs/" in path]


@pytest.mark.parametrize("operation", ["catalogue", "resume", "room"])
def test_real_consumers_keep_exact_output_and_remove_only_blob_gets(
        blobs, tmp_path, monkeypatch, capsys, operation):
    scene = blobs
    monkeypatch.setattr(remote.GitHubAPI, "from_environment", lambda: scene.wire)
    scene.opener.tree_error = IncompleteRead(b"tree must already be local", 100)
    output = None
    if operation == "catalogue":
        args = parse_command(scene, "research-catalogue", "--limit", "1")
    elif operation == "resume":
        args = parse_command(scene, "resume-research", "--case-id", "fixture-case")
    else:
        args, output = room_args(scene, tmp_path)
    options = {"ancestry": LocalGitAncestry(scene.mirror),
               "tree_reader": scene.reader, "read_binding": scene.binding}
    assert store_cli.execute(args, **options) == 0
    original = capsys.readouterr()
    assert original.err == ""
    original_html = output.read_bytes() if output else None
    if output:
        output.unlink()  # A second independent export must not overwrite the first.
    baseline = list(scene.opener.calls)
    baseline_blobs = [path for path in baseline if "/git/blobs/" in path]
    assert baseline_blobs and scene.blob_calls == []
    native_open = scene.opener.open

    def no_remote_body(request, timeout):
        assert "/git/blobs/" not in request.full_url, "complete mirror attempted remote blob GET"
        return native_open(request, timeout)

    monkeypatch.setattr(scene.opener, "open", no_remote_body)
    assert store_cli.execute(args, blob_reader=scene.reader, **options) == 0
    accelerated = capsys.readouterr()
    assert accelerated.err == ""
    assert json.loads(accelerated.out) == json.loads(original.out)
    if output:
        assert output.read_bytes() == original_html
    native = scene.opener.calls[len(baseline):]
    assert native == [path for path in baseline if "/git/blobs/" not in path]
    assert len(native) == len(baseline) - len(baseline_blobs)
    assert scene.prefix in native
    assert any("/git/ref/heads/" in path for path in native)
    assert any("/git/commits/" in path for path in native)
    assert scene.blob_calls and recursive_calls(scene) == []
    assert scene.reader.blob_stats()["answered"] == len(scene.blob_calls)
    assert scene.wire._requests == len(scene.opener.calls) < remote.MAX_REQUESTS
    assert scene.model.git.head() == scene.snapshot and scene.model.writes == 0


@pytest.mark.parametrize("field,value,code", [
    ("mode", "120000", "REGULAR_REMOTE_BLOB_REQUIRED"),
    ("size", -1, "REGULAR_REMOTE_BLOB_REQUIRED"),
])
def test_store_validates_the_complete_tree_before_requesting_its_blob(
        blobs, monkeypatch, field, value, code):
    scene = blobs
    native_read = scene.reader.read
    oid = git(scene.model.git.root, "rev-parse", scene.revision.commit + ":" + scene.revision.path).decode().strip()

    def invalid_row(tree_sha):
        result = native_read(tree_sha)
        for row in result["tree"]:
            if row["path"] == scene.revision.path:
                row[field] = value
        return result

    monkeypatch.setattr(scene.reader, "read", invalid_row)
    with pytest.raises(RecordError, match="^" + code + "$"):
        backend_for(scene)
    assert oid not in [sha for sha, _ in scene.blob_calls]
    assert blob_calls(scene) == []


@pytest.mark.parametrize("failure,code", [
    ("size", "REMOTE_BLOB_INVALID"),
    ("bytes", "REMOTE_BLOB_OID"),
    ("reference-digest", "BLOB_DIGEST_MISMATCH"),
])
def test_local_answers_still_pass_shared_blob_and_reference_integrity_checks(
        blobs, monkeypatch, failure, code):
    scene = blobs
    backend = backend_for(scene)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    native_read = scene.reader.read_blob

    def altered(oid, expected_size):
        result = native_read(oid, expected_size)
        if oid == row["sha"]:
            if failure == "size":
                result["size"] += 1
            elif failure == "bytes":
                result["content"] = base64.b64encode(b"x" * expected_size).decode("ascii")
        return result

    monkeypatch.setattr(scene.reader, "read_blob", altered)
    ref = replace(scene.revision, sha256="0" * 64) if failure == "reference-digest" else scene.revision
    with pytest.raises(RecordError, match="^" + code + "$"):
        backend.resolve(ref)
    assert (row["sha"], row["size"]) in scene.blob_calls
    assert blob_calls(scene) == []


def test_direct_get_and_foreign_repository_never_attempt_local_blob_lookup(blobs):
    scene = blobs
    backend = backend_for(scene)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    api = backend.api
    local_before, calls_before = len(scene.blob_calls), len(scene.opener.calls)
    direct_path = blob_path(scene, row["sha"])
    direct = api.request("GET", direct_path)
    foreign_path = "/repos/example/elsewhere/git/blobs/" + row["sha"]
    scene.opener.responses[foreign_path] = direct
    assert api.read_tree_blob("example/elsewhere", row) == direct
    assert scene.opener.calls[calls_before:] == [direct_path, foreign_path]
    assert len(scene.blob_calls) == local_before
    assert api.rate_limits is scene.wire.rate_limits


def test_missing_local_blob_falls_back_once_after_the_tree_has_been_validated(blobs):
    scene = blobs
    backend = backend_for(scene)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    expected = scene.model.git.resolve(scene.revision)
    lose_object(scene, row["sha"])
    assert backend.resolve(scene.revision) == expected
    assert blob_calls(scene) == [blob_path(scene, row["sha"])]
    assert scene.reader.blob_stats()["declined"] == 1
    assert backend.api._delegate is scene.wire
    assert scene.wire._requests == len(scene.opener.calls)
    assert scene.model.writes == 0


@pytest.mark.parametrize("failure,code", [
    ("incomplete", "REMOTE_RESPONSE_UNAVAILABLE"),
    ("unauthorized", "REMOTE_NOT_AUTHORIZED"),
])
def test_fallback_keeps_native_failure_and_cannot_be_healed_by_a_repaired_mirror(
        blobs, monkeypatch, failure, code):
    scene = blobs
    backend = backend_for(scene)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    lose_object(scene, row["sha"])
    target = blob_path(scene, row["sha"])
    native_open = scene.opener.open

    def unavailable(request, timeout):
        if request.full_url == remote.API_ROOT + target:
            scene.opener.calls.append(target)
            if failure == "incomplete":
                raise IncompleteRead(b"synthetic incomplete blob", 100)
            raise HTTPError(request.full_url, 401, "synthetic refusal", {}, None)
        return native_open(request, timeout)

    monkeypatch.setattr(scene.opener, "open", unavailable)
    with pytest.raises(StoreUnavailable, match="^" + code + "$") as refused:
        backend.resolve(scene.revision)
    assert blob_calls(scene) == [target]
    before, local_before = scene.wire._requests, len(scene.blob_calls)
    copy_new_objects(scene)
    monkeypatch.setattr(scene.opener, "open", native_open)
    with pytest.raises(StoreUnavailable) as repeated:
        backend.api.read_tree_blob(scene.binding.repository, row)
    assert repeated.value is refused.value
    assert scene.wire._requests == before == len(scene.opener.calls)
    assert len(scene.blob_calls) == local_before


def test_local_fallback_and_next_live_check_share_the_original_request_budget(blobs):
    scene = blobs
    backend = backend_for(scene)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    lose_object(scene, row["sha"])
    scene.wire._requests = remote.MAX_REQUESTS - 1
    assert backend.api.read_tree_blob(scene.binding.repository, row)["sha"] == row["sha"]
    assert blob_calls(scene) == [blob_path(scene, row["sha"])]
    assert scene.wire._requests == remote.MAX_REQUESTS == 512
    before, local_before = len(scene.opener.calls), len(scene.blob_calls)
    with pytest.raises(StoreUnavailable, match="^REMOTE_REQUEST_LIMIT$"):
        backend.resolve(scene.revision)
    assert scene.wire._requests == remote.MAX_REQUESTS + 1
    assert len(scene.opener.calls) == before and len(scene.blob_calls) == local_before
    assert backend.api.rate_limits is scene.wire.rate_limits


@pytest.mark.parametrize("missing_history", [False, True])
def test_stale_mirror_ref_never_replaces_historical_bytes_with_current_same_path(
        blobs, missing_history):
    scene = blobs
    path = "submissions/synthetic-history.json"
    original, current = b'{"synthetic":"original"}\n', b'{"synthetic":"current"}\n'
    first = scene.model.git.commit({path: original}, expected=scene.snapshot)
    latest = scene.model.git.commit({path: current}, expected=first)
    copy_new_objects(scene)
    assert git(scene.mirror, "rev-parse", "refs/heads/records").decode().strip() == scene.snapshot
    backend = backend_for(scene)
    old_row = backend._tree(first)[path]
    if missing_history:
        lose_object(scene, old_row["sha"])
    old_ref = BlobRef(scene.binding.repository, first, path, digest(original))
    new_ref = BlobRef(scene.binding.repository, latest, path, digest(current))
    assert backend.resolve(old_ref) == original
    assert backend.resolve(new_ref) == current
    assert blob_calls(scene) == ([blob_path(scene, old_row["sha"])] if missing_history else [])
    assert scene.model.git.resolve(old_ref) == original
    assert scene.model.git.head() == latest and scene.model.writes == 0


def test_native_visibility_refusal_latches_before_an_already_available_local_blob(blobs):
    scene = blobs
    backend = backend_for(scene)
    assert backend.resolve(scene.revision) == scene.model.git.resolve(scene.revision)
    row = backend._tree(scene.revision.commit)[scene.revision.path]
    scene.model.meta["private"] = True
    with pytest.raises(RecordError, match="^LOCAL_READ_PUBLIC_STORE_ONLY$") as refused:
        backend.head()
    before, local_before = scene.wire._requests, len(scene.blob_calls)
    scene.model.meta["private"] = False
    with pytest.raises(RecordError) as repeated:
        backend.api.read_tree_blob(scene.binding.repository, row)
    assert repeated.value is refused.value
    assert scene.wire._requests == before and len(scene.blob_calls) == local_before
