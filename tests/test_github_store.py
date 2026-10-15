"""Synthetic GitHub responses backed by real Git; not a hosted/private acceptance run."""
from copy import deepcopy
from dataclasses import asdict, replace
import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Conflict, Entity, IntegrityError, MissingObject, RecordError, RevisionInput,
    SourceInput, StoreUnavailable, UnknownWrite, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubStore


class GitAPI:
    """Models only the documented responses; Git itself owns content, history and CAS."""
    def __init__(self, root):
        self.git = GitStore.init_synthetic(root)
        initial = self.git.head()
        self.binding = GitHubBinding("example/private-store", 1234, "records/dos2", initial, "test-store", True)
        anchor = self.git.commit({"store.json": self.binding.marker()}, expected=initial)
        self.binding = replace(self.binding, anchor=anchor)
        self.before_anchor = initial
        self.meta = dict(id=1234, full_name=self.binding.repository, private=True, archived=False, default_branch="main")
        self.requests, self.writes = [], 0
        self.before_write = None
        self.after_write = None
        self.corrupt_response = None

    def request(self, method, path, payload=None):
        self.requests.append((method, path, deepcopy(payload)))
        if method == "POST":
            assert path == "/graphql"
            input = payload["variables"]["input"]
            assert input["branch"] == {"id": "REF_synthetic_data_branch"}
            assert set(input["fileChanges"]) == {"additions"}
            self.writes += 1
            if self.before_write:
                self.before_write()
            files = {row["path"]: base64.b64decode(row["contents"], validate=True)
                     for row in input["fileChanges"]["additions"]}
            try:
                commit = self.git.commit(files, expected=input["expectedHeadOid"])
            except Conflict:
                return {"data": {"createCommitOnBranch": None}, "errors": [{"message": "synthetic private error"}]}
            value = {"data": {"createCommitOnBranch": {"commit": {"oid": commit}}}}
            if self.after_write:
                return self.after_write(value)
            return value
        assert method == "GET"
        prefix = "/repos/" + self.binding.repository
        assert path == prefix or path.startswith(prefix + "/"), path
        suffix = path[len(prefix):].lstrip("/")
        url = urlsplit(suffix)
        query = parse_qs(url.query)
        route = url.path
        if not route:
            value = dict(self.meta)
        elif route.startswith("git/ref/heads/"):
            value = {"ref": "refs/heads/" + self.binding.branch, "node_id": "REF_synthetic_data_branch",
                     "object": {"type": "commit", "sha": self.git.head()}}
        elif route.startswith("compare/"):
            base, head = route[8:].split("...")
            is_ancestor = self.git._run("merge-base", "--is-ancestor", base, head, allowed=(0, 1, 128)).returncode == 0
            value = dict(status="ahead" if is_ancestor else "diverged", behind_by=0 if is_ancestor else 1,
                         merge_base_commit={"sha": base if is_ancestor else "0" * 40})
        elif route.startswith("git/commits/"):
            sha = route.rsplit("/", 1)[1]
            tree = self.git._run("rev-parse", sha + "^{tree}").stdout.decode().strip()
            parents = self.git._run("rev-list", "--parents", "-n", "1", sha).stdout.decode().split()[1:]
            value = dict(sha=sha, tree={"sha": tree}, parents=[{"sha": p, "url": "not-followed"} for p in parents])
        elif route.startswith("git/trees/"):
            sha = route.rsplit("/", 1)[1]
            rows = []
            for raw in self.git._run("ls-tree", "-r", "-z", sha).stdout.split(b"\0"):
                if not raw:
                    continue
                meta, name = raw.split(b"\t")
                mode, kind, blob = meta.decode().split()
                size = int(self.git._run("cat-file", "-s", blob).stdout)
                rows.append(dict(path=name.decode(), mode=mode, type=kind, sha=blob, size=size))
            value = dict(sha=sha, truncated=False, tree=rows)
        elif route.startswith("git/blobs/"):
            sha = route.rsplit("/", 1)[1]
            raw = self.git._run("cat-file", "blob", sha).stdout
            value = dict(sha=sha, encoding="base64", size=len(raw), content=base64.encodebytes(raw).decode())
        elif route == "commits":
            original = self.git.last_change(query["sha"][0], query["path"][0])
            value = [{"sha": original}]
        else:
            raise AssertionError(path)
        return self.corrupt_response(path, value) if self.corrupt_response else value


@pytest.fixture
def api(tmp_path):
    return GitAPI(tmp_path / "remote-model.git")


@pytest.fixture
def backend(api):
    return GitHubStore(api.binding, api)


def source(text="铜价仅为合成材料，不是真实市场数据。", **changes):
    values = dict(source_id="copper", kind="content", title="合成产业观察", origin="synthetic:example", text=text,
                  entities=(Entity("metal-cu", "commodity", "铜", ("CU",), (("spec", "合成规格"),)),))
    return SourceInput(**{**values, **changes})


def test_shared_source_revision_and_pagination_roundtrip(api, backend):
    store = VersionedStore(backend)
    first = store.put_source("source-one", source())
    assert first.manifest.repository == api.binding.repository
    revision = RevisionInput("case-cu", "r1", "quick", "合成研究正文", "工程用例", "合成方法", (first,))
    receipt = store.submit_revision("revision-one", revision)
    assert store.current("case-cu") == receipt
    assert store.read_revision(receipt)["body"] == revision.body
    second = store.put_source("source-two", source("新的合成铜价记录"))
    page = store.search("ＣＵ", limit=1)
    store.put_source("source-three", source("第三份合成记录"))
    last = store.search("cu", limit=1, cursor=page["next_cursor"])
    assert page["total"] == last["total"] == 2
    assert last["next_cursor"] is None
    assert store.read_source(first)["data"] == source().text
    assert store.read_source(second)["data"] == "新的合成铜价记录"
    assert store.read_revision(receipt)["record"]["human_acceptance"] == "not-confirmed"
    posts = [p[2]["variables"]["input"] for p in api.requests if p[0] == "POST"]
    assert all(p["expectedHeadOid"] and p["branch"] == {"id": "REF_synthetic_data_branch"} for p in posts)


def test_replay_uses_original_commit_and_no_new_write(api, backend):
    store = VersionedStore(backend)
    first = store.put_source("same-intent", source())
    store.put_source("another-intent", source("changed"))
    writes = api.writes
    assert store.put_source("same-intent", source()) == first
    assert api.writes == writes
    with pytest.raises(Conflict, match="SUBMISSION_CONTENT_CONFLICT"):
        store.put_source("same-intent", source("different"))


def test_native_cas_race_keeps_winner_and_requires_reconciliation(api, backend):
    store = VersionedStore(backend)
    def race():
        api.git.commit({"submissions/winner.json": b"{}\n"}, expected=api.git.head())
    api.before_write = race
    with pytest.raises(UnknownWrite, match="RECONCILIATION"):
        store.put_source("loser", source())
    assert api.git.read(api.git.head(), "submissions/winner.json") == b"{}\n"
    with pytest.raises(MissingObject):
        api.git.read(api.git.head(), "submissions/loser.json")
    assert api.writes == 1


def test_response_lost_after_publish_replays_same_intent(api, backend):
    store = VersionedStore(backend)
    def lose(value):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    api.after_write = lose
    with pytest.raises(UnknownWrite):
        store.put_source("lost-response", source())
    published = api.git.head()
    api.after_write = None
    recovered = VersionedStore(GitHubStore(api.binding, api)).put_source("lost-response", source())
    assert recovered.manifest.commit == published
    assert api.writes == 1


def test_failure_before_send_does_not_publish(api, backend):
    initial = api.git.head()
    def deny():
        raise StoreUnavailable("REMOTE_NOT_AUTHORIZED")
    api.before_write = deny
    with pytest.raises(StoreUnavailable, match="NOT_AUTHORIZED"):
        VersionedStore(backend).put_source("not-written", source())
    assert api.git.head() == initial


def test_readback_failure_is_not_saved_success(api, backend):
    def wrong_receipt(value):
        return {"data": {"createCommitOnBranch": {"commit": {"oid": api.binding.anchor}}}}
    api.after_write = wrong_receipt
    with pytest.raises(UnknownWrite, match="RECONCILIATION"):
        VersionedStore(backend).put_source("readback-lost", source())
    api.after_write = None
    result = VersionedStore(backend).put_source("readback-lost", source())
    assert result.manifest.commit != api.binding.anchor and api.writes == 1


def test_old_predecessor_and_existing_revision_are_not_overwritten(backend):
    store = VersionedStore(backend)
    ref = store.put_source("src", source())
    first = store.submit_revision("r1", RevisionInput("case", "one", "full", "first", "m", "method", (ref,)))
    store.submit_revision("r2", RevisionInput("case", "two", "full", "second", "m", "method", (ref,), first))
    with pytest.raises(Conflict, match="STALE_PREDECESSOR"):
        store.submit_revision("r3", RevisionInput("case", "three", "full", "bad", "m", "method", (ref,), first))
    current = store.current("case")
    with pytest.raises(Conflict, match="REVISION_ALREADY_EXISTS"):
        store.submit_revision("r4", RevisionInput("case", "one", "full", "bad", "m", "method", (ref,), current))
    assert store.read_revision(first)["body"] == "first"


@pytest.mark.parametrize("change", [
    {"private": False}, {"id": 999}, {"id": True}, {"full_name": "other/private-store"},
    {"archived": True}, {"default_branch": "records/dos2"},
])
def test_native_repository_metadata_checked_again_after_initialization(api, backend, change):
    ref = VersionedStore(backend).put_source("s", source())
    api.meta.update(change)
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        backend.resolve(ref.manifest)  # Cached immutable bytes must not bypass fresh privacy checks.


def test_readonly_binding_prevents_post(api):
    backend = GitHubStore(replace(api.binding, write_enabled=False), api)
    with pytest.raises(RecordError, match="REMOTE_READ_ONLY"):
        VersionedStore(backend).put_source("x", source())
    assert not api.writes


def test_stale_head_stops_before_post(api, backend):
    expected = backend.head()
    api.git.commit({"submissions/next.json": b"{}\n"}, expected=expected)
    with pytest.raises(Conflict, match="HEAD_CHANGED"):
        backend.commit({"submissions/x.json": b"{}\n"}, expected=expected)
    assert not api.writes


def test_pre_anchor_history_is_not_a_store_reference(api, backend):
    with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
        backend.read(api.before_anchor, "store.json")


def test_foreign_commit_fork_is_not_reachable(api, backend):
    expected = backend.head()
    api.git.commit({"submissions/fork.json": b"{}\n"}, expected=expected)
    fork = api.git.head()
    api.git._run("update-ref", "refs/heads/records", expected, fork)
    api.git.commit({"submissions/other.json": b"{}\n"}, expected=expected)
    with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
        backend.read(fork, "submissions/fork.json")


def test_binding_change_rejected_without_silent_rebind(api, backend):
    api.git.commit({"store.json": b"{}\n"}, expected=api.git.head())
    with pytest.raises(RecordError, match="STORE_MARKER_MISMATCH"):
        backend.head()


@pytest.mark.parametrize("path", ["store.json", ".github/workflows/run.yml", "research/cases/case/decision.json",
                                  "source-views/../../outside", "submissions/.git/config"])
def test_write_surface_cannot_mutate_binding_or_unrelated_files(api, backend, path):
    with pytest.raises(RecordError):
        backend.commit({path: b"unauthorized"}, expected=backend.head())
    assert not api.writes


def test_existing_source_cannot_be_replaced_by_direct_backend_write(api, backend):
    ref = VersionedStore(backend).put_source("s", source())
    with pytest.raises(RecordError, match="IMMUTABLE_RECORD_CONFLICT"):
        backend.commit({ref.manifest.path: b"new"}, expected=backend.head())


@pytest.mark.parametrize("damage", ["truncated", "symlink", "duplicate", "bad-oid", "invalid-base64", "size", "wrong-ref"])
def test_untrusted_remote_metadata_and_bytes_rejected(api, damage):
    def corrupt(path, value):
        if "/git/trees/" in path:
            if damage == "truncated": value["truncated"] = True
            elif damage == "symlink": value["tree"][0]["mode"] = "120000"
            elif damage == "duplicate": value["tree"].append(dict(value["tree"][0]))
        if "/git/blobs/" in path:
            if damage == "bad-oid": value["content"] = base64.b64encode(b"x" * value["size"]).decode()
            elif damage == "invalid-base64": value["content"] = "!!!"
            elif damage == "size": value["size"] += 1
        if "/git/ref/" in path and damage == "wrong-ref": value["ref"] = "refs/heads/main"
        return value
    api.corrupt_response = corrupt
    with pytest.raises(RecordError):
        GitHubStore(api.binding, api)


def test_resolve_rejects_wrong_repository_and_digest(api, backend):
    ref = VersionedStore(backend).put_source("s", source()).manifest
    with pytest.raises(RecordError, match="FOREIGN_REPOSITORY"):
        backend.resolve(replace(ref, repository="other/repo"))
    with pytest.raises(IntegrityError, match="BLOB_DIGEST_MISMATCH"):
        backend.resolve(replace(ref, sha256="0" * 64))


@pytest.mark.parametrize("changes", [
    {"repository": "https://evil.test/owner/repo"}, {"repository_id": True}, {"repository_id": 0},
    {"branch": "main"}, {"branch": "records/../main"}, {"anchor": "main"},
    {"store_id": "../x"}, {"write_enabled": "true"}, {"token": "not-configuration"},
])
def test_binding_shape_rejects_source_driven_authority(api, changes):
    with pytest.raises(RecordError):
        GitHubBinding.from_dict({**asdict(api.binding), **changes})


def test_write_response_with_unrelated_change_not_success(api, backend):
    def alter(value):
        commit = value["data"]["createCommitOnBranch"]["commit"]["oid"]
        # Returning a later commit, even a valid reachable one, is not this transaction's receipt.
        later = api.git.commit({"submissions/unrelated.json": b"{}\n"}, expected=commit)
        return {"data": {"createCommitOnBranch": {"commit": {"oid": later}}}}
    api.after_write = alter
    with pytest.raises(UnknownWrite):
        VersionedStore(backend).put_source("s", source())


def test_shared_cli_private_binding_roundtrip_and_readonly(api, tmp_path, monkeypatch, capsys):
    import argparse
    from decision_kernel.interfaces import store_cli
    parser = argparse.ArgumentParser()
    store_cli.configure(parser.add_subparsers())
    binding_file = tmp_path / "binding.json"
    binding_file.write_bytes(encoded(asdict(api.binding)))
    input_file = tmp_path / "input.json"
    input_file.write_bytes(encoded(asdict(source())))
    monkeypatch.setattr(store_cli.GitHubAPI, "from_environment", lambda: api)
    common = ["store", "--binding", str(binding_file)]
    assert store_cli.execute(parser.parse_args(common + ["put-source", "--submission-id", "cli-one", "--input", str(input_file)])) == 0
    receipt = json.loads(capsys.readouterr().out)
    ref_file = tmp_path / "ref.json"
    ref_file.write_bytes(encoded(receipt))
    binding_file.write_bytes(encoded(asdict(replace(api.binding, write_enabled=False))))
    assert store_cli.execute(parser.parse_args(common + ["read-source", "--ref", str(ref_file)])) == 0
    assert json.loads(capsys.readouterr().out)["data"] == source().text
    writes = api.writes
    assert store_cli.execute(parser.parse_args(common + ["put-source", "--submission-id", "cli-two", "--input", str(input_file)])) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "REMOTE_READ_ONLY"
    assert api.writes == writes
    assert store_cli.execute(parser.parse_args(common + ["init-synthetic"])) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "REMOTE_INITIALIZATION_NOT_SUPPORTED"
