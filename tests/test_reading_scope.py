"""Bounded room reads over real Git + original GitHubStore/API; wire is synthetic."""
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, StoreUnavailable, encoded
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.interfaces import reading_commands, research_runtime
from test_github_store import GitAPI, source


class Response:
    status = 200
    headers = {}

    def __init__(self, raw, url):
        self.raw, self.url = raw, url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self):
        return self.url

    def read(self, size):
        return self.raw[:size]


def transport(model):
    class Wire:
        def open(self, request, timeout):
            assert timeout == 30 and request.method == "GET"
            value = model.request("GET", request.full_url.removeprefix(remote.API_ROOT))
            return Response(encoded(value), request.full_url)
    api = remote.GitHubAPI("synthetic-room-budget-token")
    api._opener = Wire()
    return api


@pytest.fixture
def retained(tmp_path):
    model = GitAPI(tmp_path / "room-model.git")
    model.binding = replace(model.binding, visibility="public", private_upgrade_allowed=True,
                            write_enabled=False)
    anchor = model.git.commit({"store.json": model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository = model.binding.repository
    store = VersionedStore(model.git)
    ref = store.put_source("room-source", source())
    revision = store.submit_revision("room-revision", RevisionInput("room-case", "v1", "quick",
        "Original synthetic finding.", "Synthetic mandate", "synthetic", (ref,),
        unresolved=("Not assessed",)))
    model.requests.clear()
    return model, store, ref, revision


def test_repeated_exact_dependencies_share_original_budget_without_raising_it(retained):
    model, local, ref, _ = retained
    original = local.read_source(ref)
    api = transport(model)
    ordinary = VersionedStore(remote.GitHubStore(model.binding, api))
    with pytest.raises(StoreUnavailable, match="^REMOTE_REQUEST_LIMIT$"):
        for _ in range(200):
            assert ordinary.read_source(ref) == original
    assert api._requests == 513 and remote.MAX_REQUESTS == 512
    api = transport(model)
    owner = remote.GitHubStore(model.binding, api)
    with reading_scope(owner) as backend:
        reader = VersionedStore(backend)
        for _ in range(200):
            assert reader.read_source(ref) == original
        assert backend.api is owner.api and not backend.binding.write_enabled
    assert 0 < api._requests < 512 and model.writes == 0
    assert len(owner._trees) <= 32 and len(owner._blobs) <= 128
    assert not hasattr(owner, "_checked")


def test_snapshot_pins_reads_while_allowing_legitimate_later_append(retained):
    model, local, ref, revision = retained
    owner = remote.GitHubStore(model.binding, transport(model))
    with reading_scope(owner) as backend:
        snapshot = backend.head()
        newer = local.submit_revision("room-next", RevisionInput("room-case", "v2", "quick",
            "New synthetic finding.", "Synthetic mandate", "synthetic", (ref,), predecessor=revision))
        assert backend.head() == snapshot and VersionedStore(backend).current("room-case") == revision
        with pytest.raises(RecordError, match="COMMIT_OUTSIDE_BINDING"):
            backend.check_commit(newer.commit)
    assert owner.head() == newer.commit and VersionedStore(owner).current("room-case") == newer


@pytest.mark.parametrize("change", ["privacy", "repository", "archive", "marker", "rewind", "budget"])
def test_exit_native_checks_prevent_result_escape_and_expire_reader(retained, change):
    model, _, ref, _ = retained
    api = transport(model)
    owner = remote.GitHubStore(model.binding, api)
    escaped = False
    with pytest.raises(RecordError):
        with reading_scope(owner) as backend:
            assert backend.resolve(ref.manifest)
            if change == "privacy": model.meta["private"] = False
            elif change == "repository": model.meta["id"] = 999
            elif change == "archive": model.meta["archived"] = True
            elif change == "marker": model.git.commit({"store.json": b"{}"}, expected=model.git.head())
            elif change == "rewind": model.git._run("update-ref", "refs/heads/records", model.binding.anchor, model.git.head())
            else: api._requests = remote.MAX_REQUESTS
        escaped = True
    assert not escaped and model.writes == 0
    with pytest.raises(RecordError, match="READING_SCOPE_CLOSED"):
        backend.resolve(ref.manifest)


def test_scope_never_grants_writes_or_lifetime_to_nested_source_readers(retained):
    model, _, ref, _ = retained
    owner = remote.GitHubStore(model.binding, transport(model))
    with reading_scope(owner) as backend:
        with backend.source_read_scope(ref.manifest.commit) as source_backend:
            assert source_backend.resolve(ref.manifest) == backend.resolve(ref.manifest)
            with pytest.raises(RecordError, match="SOURCE_SCAN_READ_ONLY"):
                source_backend.commit({}, expected=backend.head())
        with pytest.raises(RecordError): source_backend.resolve(ref.manifest)
        with pytest.raises(RecordError, match="READING_SCOPE_READ_ONLY"):
            backend.commit({"submissions/no.json": b"{}"}, expected=backend.head())
        assert isinstance(backend, remote.GitHubStore)  # Original market dispatch is preserved.
    for call in (backend.head, lambda: backend.paths(ref.manifest.commit, "source-views/"),
                 lambda: backend.reference(ref.manifest.commit, ref.manifest.path), backend.live_visibility):
        with pytest.raises(RecordError, match="READING_SCOPE_CLOSED"): call()
    assert model.writes == 0


@pytest.mark.parametrize("damage", ["foreign", "digest", "pre-anchor"])
def test_scoped_reads_keep_original_identity_and_integrity_checks(retained, damage):
    model, _, ref, _ = retained
    owner = remote.GitHubStore(model.binding, transport(model))
    bad = replace(ref.manifest, **{"foreign": {"repository": "other/repository"},
        "digest": {"sha256": "0" * 64}, "pre-anchor": {"commit": model.before_anchor}}[damage])
    with reading_scope(owner) as backend:
        with pytest.raises(RecordError): backend.resolve(bad)
        assert backend.resolve(ref.manifest)


def test_failed_scope_does_not_leave_owner_in_a_checked_state(retained):
    model, _, ref, _ = retained
    owner = remote.GitHubStore(model.binding, transport(model))
    with pytest.raises(ValueError, match="synthetic component failure"):
        with reading_scope(owner) as backend:
            raise ValueError("synthetic component failure")
    with pytest.raises(RecordError, match="READING_SCOPE_CLOSED"): backend.head()
    model.meta["private"] = False
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"): owner.resolve(ref.manifest)


def test_local_and_remote_normal_room_are_equal_and_do_not_write(retained, tmp_path):
    model, local, _, revision = retained
    plan = {"as_of": "2026-10-18T00:00:00Z", "catalogue": {"limit": 1}, "reference": asdict(revision)}
    expected = reading_commands.perform(local, {"plan": plan}, public=False)
    api = transport(model)
    inputs = {"operation": "reading-room", "payload": encoded({"plan": plan}).decode(),
              "code_sha": "a" * 40, "submission_id": "", "expected_head": ""}
    report = research_runtime.run_command(model.binding, api=api, inputs=inputs, code_sha="a" * 40,
        output=tmp_path / "normal", run_id="synthetic", attempt="1")
    assert report["status"] == "completed" and report["result"] == expected
    assert report["write"] == "not-requested" and report["repository_visibility"] == "private"
    assert report["research_executed"] is False and model.writes == 0 and api._requests < 512
    assert (tmp_path / "normal/reading-room.html").is_file()
    assert report["result"]["readings"][0]["body"] == "Original synthetic finding."


def test_host_exports_no_private_input_model_or_html_when_scope_exit_rejects(retained, tmp_path, monkeypatch):
    model, _, _, revision = retained
    original = reading_commands.ReadingRoom.build
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs)
        model.meta["private"] = False
        return result
    monkeypatch.setattr(reading_commands.ReadingRoom, "build", reverse)
    inputs = {"operation": "reading-room", "payload": encoded({"plan": {
        "as_of": "2026-10-18T00:00:00Z", "reference": asdict(revision)}}).decode(),
        "code_sha": "a" * 40, "submission_id": "", "expected_head": ""}
    out = tmp_path / "reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(model.binding, api=transport(model), inputs=inputs,
            code_sha="a" * 40, output=out)
    report = json.loads((out / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report
    assert sorted(p.name for p in out.iterdir()) == ["report.json"] and model.writes == 0
