"""Runtime revocation before native CAS, using synthetic responses backed by Git.

The trusted hook does not authorize reads/writes by itself, retry a mutation, or
change the shared store's post-send unknown-effect contract.
"""
from dataclasses import asdict, replace

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Conflict, RecordError, StoreUnavailable, UnknownWrite
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubStore
from test_github_store import GitAPI, source


@pytest.fixture
def api(tmp_path):
    return GitAPI(tmp_path / "write-guard.git")


@pytest.mark.parametrize("invalid", [False, "runtime-approval", {}])
def test_guard_must_be_a_trusted_callable_before_native_requests(api, invalid):
    with pytest.raises(RecordError, match="^INVALID_BEFORE_WRITE_GUARD$"):
        GitHubStore(api.binding, api, before_write=invalid)
    assert api.requests == [] and api.writes == 0
    with pytest.raises(RecordError, match="^INVALID_GITHUB_BINDING$"):
        GitHubBinding.from_dict({**asdict(api.binding), "before_write": invalid})


@pytest.mark.parametrize("denial", [RecordError, StoreUnavailable])
def test_revocation_after_initial_reads_stops_before_send_without_retry(api, denial):
    revoked, calls = False, []
    refused = denial("RUNTIME_APPROVAL_REVOKED")

    def guard():
        calls.append("checked")
        if revoked:
            raise refused

    backend = GitHubStore(api.binding, api, before_write=guard)
    initial = backend.head()
    assert backend.read(initial, "store.json") == api.binding.marker()
    assert calls == []  # Initial admission and ordinary reads do not invoke it.
    revoked = True
    with pytest.raises(denial) as caught:
        VersionedStore(backend).put_source("revoked-source", source())
    assert caught.value is refused and not isinstance(caught.value, UnknownWrite)
    assert calls == ["checked"]
    assert api.writes == 0 and not any(method == "POST" for method, *_ in api.requests)
    assert api.git.head() == initial
    assert "submissions/revoked-source.json" not in api.git.paths(initial)


def test_guard_runs_after_last_binding_check_and_directly_before_one_cas(api):
    checks, events = [], []

    def guard():
        assert api.requests[-1] == ("GET", "/repos/" + api.binding.repository, None)
        checks.append(len(api.requests))
        events.append("guard")

    def sending():
        assert len(api.requests) == checks[-1] + 1
        assert api.requests[-1][:2] == ("POST", "/graphql")
        events.append("post")

    backend = GitHubStore(api.binding, api, before_write=guard)
    initial = api.git.head()
    api.before_write = sending
    committed = backend.commit({"submissions/approved.json": b"{}\n"}, expected=initial)
    assert events == ["guard", "post"] and api.writes == 1
    assert committed != initial and api.git.read(committed, "submissions/approved.json") == b"{}\n"
    payload = next(payload for method, _, payload in api.requests if method == "POST")
    assert payload["variables"]["input"]["expectedHeadOid"] == initial
    assert payload["variables"]["input"]["branch"] == {"id": "REF_synthetic_data_branch"}


def test_revocation_at_final_binding_read_is_not_missed_by_early_admission(api):
    checks, repository_reads, revoked = [], 0, False

    def change_approval(path, value):
        nonlocal repository_reads, revoked
        if path == "/repos/" + api.binding.repository:
            repository_reads += 1
            if repository_reads == 2:
                revoked = True  # The request preparation already completed.
        return value

    def guard():
        checks.append(revoked)
        if revoked:
            raise RecordError("RUNTIME_APPROVAL_REVOKED")

    backend = GitHubStore(api.binding, api, before_write=guard)
    initial = api.git.head()
    api.corrupt_response = change_approval
    with pytest.raises(RecordError, match="^RUNTIME_APPROVAL_REVOKED$"):
        backend.commit({"submissions/revoked.json": b"{}\n"}, expected=initial)
    assert checks == [True] and repository_reads == 2
    assert api.writes == 0 and api.git.head() == initial


@pytest.mark.parametrize("failure", ["read-only", "stale-head", "invalid-set", "last-binding"])
def test_store_rejections_never_call_the_runtime_guard(api, failure):
    calls = []
    binding = replace(api.binding, write_enabled=False) if failure == "read-only" else api.binding
    backend = GitHubStore(binding, api, before_write=lambda: calls.append("checked"))
    initial = api.git.head()
    files = {"submissions/rejected.json": b"{}\n"}
    reason = "REMOTE_READ_ONLY"
    if failure == "stale-head":
        api.git.commit({"submissions/other.json": b"{}\n"}, expected=initial)
        reason = "HEAD_CHANGED"
    elif failure == "invalid-set":
        files, reason = {}, "INVALID_WRITE_SET"
    elif failure == "last-binding":
        seen = 0

        def lose_binding(path, value):
            nonlocal seen
            if path == "/repos/" + api.binding.repository:
                seen += 1
                if seen == 2:
                    value["private"] = False
            return value

        api.corrupt_response, reason = lose_binding, "PRIVATE_BINDING_MISMATCH"
    before = api.git.head()
    with pytest.raises(RecordError, match="^" + reason + "$"):
        backend.commit(files, expected=initial)
    assert calls == [] and api.writes == 0 and api.git.head() == before


def test_existing_intent_can_be_read_back_after_revocation_without_new_write(api):
    calls, revoked = [], False

    def guard():
        calls.append("checked")
        if revoked:
            raise RecordError("RUNTIME_APPROVAL_REVOKED")

    store = VersionedStore(GitHubStore(api.binding, api, before_write=guard))
    original = store.put_source("same-source", source())
    revoked = True
    assert store.put_source("same-source", source()) == original
    assert calls == ["checked"] and api.writes == 1
    with pytest.raises(Conflict, match="^SUBMISSION_CONTENT_CONFLICT$"):
        store.put_source("same-source", source("A different synthetic value"))
    assert calls == ["checked"] and api.writes == 1


def test_post_send_uncertainty_remains_unknown_and_is_never_retried(api):
    calls = []
    backend = GitHubStore(api.binding, api, before_write=lambda: calls.append("checked"))

    def lose_response(value):
        raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")

    api.after_write = lose_response
    initial = api.git.head()
    with pytest.raises(UnknownWrite, match="^REMOTE_WRITE_UNCONFIRMED$"):
        backend.commit({"submissions/lost.json": b"{}\n"}, expected=initial)
    assert calls == ["checked"] and api.writes == 1
    assert api.git.head() != initial
    assert api.git.read(api.git.head(), "submissions/lost.json") == b"{}\n"


@pytest.mark.parametrize("options", [{}, {"before_write": None}])
def test_unconfigured_shared_callers_keep_original_cas_and_readback(api, options):
    backend = GitHubStore(api.binding, api, **options)
    store = VersionedStore(backend)
    saved = store.put_source("ordinary-source", source())
    assert store.read_source(saved)["data"] == source().text
    assert store.put_source("ordinary-source", source()) == saved
    assert api.writes == 1
