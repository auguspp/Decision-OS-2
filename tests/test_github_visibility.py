"""Explicit visibility selection on the existing adapter; synthetic GitHub, real Git."""
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, RevisionInput, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubStore
from test_github_store import GitAPI, source


@pytest.fixture
def public_api(tmp_path):
    api = GitAPI(tmp_path / "public-model.git")
    binding = replace(api.binding, visibility="public")
    anchor = api.git.commit({"store.json": binding.marker()}, expected=api.git.head())
    api.binding = replace(binding, anchor=anchor)
    api.meta["private"] = False
    return api


def test_private_default_preserves_existing_marker_bytes():
    binding = GitHubBinding("example/private-store", 1234, "records", "a" * 40, "example")
    assert binding.visibility == "private" and binding.write_enabled is False
    old = dict(schema="github-store-binding-v1", project="decision-os-2", mode="private",
               repository=binding.repository, repository_id=1234, branch="records", store_id="example")
    from decision_kernel.domain.records import encoded
    assert binding.marker() == encoded(old)
    assert replace(binding, write_enabled=True).marker() == binding.marker()
    value = asdict(binding)
    del value["visibility"]
    assert GitHubBinding.from_dict(value) == binding


@pytest.mark.parametrize("visibility", ["", "Public", "internal", None, True, 1, [], {}])
def test_invalid_visibility_is_not_an_opt_in(visibility):
    with pytest.raises(RecordError, match="INVALID_DATA_VISIBILITY"):
        GitHubBinding("example/store", 1, "records", "a" * 40, "example", visibility=visibility)


def test_public_shared_roundtrip_and_original_reference(public_api):
    store = VersionedStore(GitHubStore(public_api.binding, public_api))
    first = store.put_source("public-source", source())
    revision = RevisionInput("public-case", "one", "quick", "合成研究，不是实际委托。",
                             "工程验证", "合成方法", (first,))
    saved = store.submit_revision("public-revision", revision)
    store.put_source("next-source", source("第二版合成材料"))
    reopened = VersionedStore(GitHubStore(replace(public_api.binding, write_enabled=False), public_api))
    assert reopened.read_source(first)["data"] == source().text
    assert reopened.current("public-case") == saved
    assert reopened.read_revision(saved)["body"] == revision.body
    assert reopened.search("ＣＵ")["total"] == 2
    assert saved.repository == public_api.binding.repository
    assert json.loads(public_api.binding.marker())["mode"] == "public"


def test_public_is_still_read_only_without_write_authorization(public_api):
    backend = GitHubStore(replace(public_api.binding, write_enabled=False), public_api)
    with pytest.raises(RecordError, match="REMOTE_READ_ONLY"):
        VersionedStore(backend).put_source("not-authorized", source())
    assert public_api.writes == 0


def test_private_config_cannot_open_public_repo_even_with_same_identity(public_api):
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        GitHubStore(replace(public_api.binding, visibility="private"), public_api)
    assert public_api.writes == 0


def test_public_config_does_not_bypass_private_marker(tmp_path):
    api = GitAPI(tmp_path / "marker-model.git")
    api.meta["private"] = False
    with pytest.raises(RecordError, match="STORE_MARKER_MISMATCH"):
        GitHubStore(replace(api.binding, visibility="public"), api)
    assert api.writes == 0


@pytest.mark.parametrize("operation", ["read", "write"])
def test_public_visibility_is_rechecked_after_open(public_api, operation):
    backend = GitHubStore(public_api.binding, public_api)
    public_api.meta["private"] = True
    with pytest.raises(RecordError, match="PUBLIC_BINDING_MISMATCH"):
        if operation == "read":
            backend.read(public_api.binding.anchor, "store.json")
        else:
            backend.commit({"submissions/no-write.json": b"{}\n"}, expected=public_api.git.head())
    assert public_api.writes == 0


def test_public_visibility_is_rechecked_immediately_before_post(public_api):
    backend = GitHubStore(public_api.binding, public_api)
    calls = 0
    def visibility_change(path, value):
        nonlocal calls
        if path == "/repos/" + public_api.binding.repository:
            calls += 1
            if calls == 2:
                value["private"] = True
        return value
    public_api.corrupt_response = visibility_change
    with pytest.raises(RecordError, match="PUBLIC_BINDING_MISMATCH"):
        backend.commit({"submissions/no-write.json": b"{}\n"}, expected=public_api.git.head())
    assert calls == 2 and public_api.writes == 0


@pytest.mark.parametrize("change", [
    {"id": 999}, {"id": True}, {"full_name": "other/store"},
    {"archived": True}, {"default_branch": "records/dos2"}, {"private": None}, {"private": 0},
])
def test_public_does_not_relax_repository_identity_or_default_branch(public_api, change):
    public_api.meta.update(change)
    with pytest.raises(RecordError, match="PUBLIC_BINDING_MISMATCH"):
        GitHubStore(public_api.binding, public_api)
    assert public_api.writes == 0


def test_public_lost_response_recovers_same_intent_without_repost(public_api):
    store = VersionedStore(GitHubStore(public_api.binding, public_api))
    def lost(_value):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    public_api.after_write = lost
    with pytest.raises(UnknownWrite):
        store.put_source("recover-me", source())
    published = public_api.git.head()
    public_api.after_write = None
    recovered = VersionedStore(GitHubStore(public_api.binding, public_api)).put_source("recover-me", source())
    assert recovered.manifest.commit == published and public_api.writes == 1
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.put_source("recover-me", source("不同内容"))
    assert public_api.writes == 1


def test_public_native_cas_keeps_concurrent_winner(public_api):
    store = VersionedStore(GitHubStore(public_api.binding, public_api))
    def concurrent_write():
        public_api.git.commit({"submissions/winner.json": b"{}\n"}, expected=public_api.git.head())
    public_api.before_write = concurrent_write
    with pytest.raises(UnknownWrite, match="RECONCILIATION"):
        store.put_source("loser", source())
    assert public_api.git.read(public_api.git.head(), "submissions/winner.json") == b"{}\n"
    assert public_api.writes == 1

 
def test_public_origin_upgrade_preserves_marker_and_original_research_references(public_api):
    from decision_kernel.domain.records import encoded
    original = public_api.binding
    upgraded = replace(original, private_upgrade_allowed=True)
    assert upgraded.marker() == original.marker()
    writer = VersionedStore(GitHubStore(upgraded, public_api))
    first = writer.put_source("legacy-before-private", source("synthetic published before upgrade"))
    before = writer.submit_revision("legacy-quick", RevisionInput(
        "public-then-private", "one", "quick", "synthetic research",
        "synthetic method", "test-only", (first,)))
    current = public_api.git.head()
    assert json.loads(public_api.git.read(current, "store.json"))["mode"] == "public"
    public_api.meta["private"] = True
    reader = VersionedStore(GitHubStore(replace(upgraded, write_enabled=False), public_api))
    assert reader.read_source(first)["data"] == "synthetic published before upgrade"
    assert reader.read_revision(before)["body"] == "synthetic research"
    assert reader.current("public-then-private") == before
    assert reader.backend.live_visibility() == "private"
    assert public_api.git.read(current, "store.json") == original.marker()
    assert reader.backend.binding.marker() == original.marker()
    assert encoded(reader.read_revision(before)) == encoded(writer.read_revision(before))


def test_private_upgrade_allows_authorized_private_write_and_blocks_live_reversal(public_api):
    upgraded = replace(public_api.binding, private_upgrade_allowed=True)
    public_api.meta["private"] = True
    backend = GitHubStore(upgraded, public_api)
    store = VersionedStore(backend)
    first = store.put_source("private-created", source("synthetic private retained rows"))
    assert store.read_source(first)["data"] == "synthetic private retained rows"
    before = public_api.writes
    public_api.meta["private"] = False
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        backend.read(first.manifest.commit, first.manifest.path)
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        backend.commit({"submissions/no-write.json": b"{}\\n"}, expected=public_api.git.head())
    assert public_api.writes == before


@pytest.mark.parametrize("invalid", [True, None, 1, "true", [], {}])
def test_private_upgrade_requires_explicit_boolean_policy(public_api, invalid):
    with pytest.raises(RecordError, match="INVALID_PRIVATE_UPGRADE_POLICY"):
        replace(public_api.binding, private_upgrade_allowed=invalid)


def test_private_upgrade_is_not_allowed_for_native_private_markers(public_api):
    with pytest.raises(RecordError, match="INVALID_PRIVATE_UPGRADE_POLICY"):
        replace(public_api.binding, visibility="private", private_upgrade_allowed=True)


def test_private_upgrade_does_not_suppress_id_marker_or_branch_checks(public_api):
    upgraded = replace(public_api.binding, private_upgrade_allowed=True)
    public_api.meta["private"] = True
    with pytest.raises(RecordError, match="PUBLIC_BINDING_MISMATCH"):
        GitHubStore(replace(upgraded, repository_id=9999), public_api)
    with pytest.raises(RecordError, match="STORE_MARKER_MISMATCH"):
        GitHubStore(replace(upgraded, store_id="foreign"), public_api)
    with pytest.raises(RecordError, match="REMOTE_REF_MISMATCH"):
        GitHubStore(replace(upgraded, branch="records/other"), public_api)
