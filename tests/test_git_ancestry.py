"""Ancestry from a local object graph, and the store's decision to use it.

The store asks one question before every read: is the fixed anchor reachable from
this head? `GET /compare` answers it by enumerating the commits in between, which
on 2026-10-23 was 1127 of them at 7-47.5 s against a 30 s timeout. A local graph
answers the same question in under a second, but only if it refuses to guess.
These tests pin the refusal, because a graph that answered "no" instead of "ask
the remote" would turn a stale mirror into a false accusation against the store.
"""
import os
import subprocess

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.git_ancestry import AncestryUnavailable, LocalGitAncestry
from decision_kernel.infrastructure.github_store import GitHubBinding, GitHubStore

ANCHOR = "a" * 40


def git(path, *args):
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def commit(path, message):
    git(path, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "--allow-empty", "-m", message)
    return git(path, "rev-parse", "HEAD")


@pytest.fixture
def mirror(tmp_path):
    """A repository with three linear commits: [first, second, third]."""
    path = tmp_path / "mirror"
    path.mkdir()
    git(path, "init", "-q")
    return path


def test_answers_reachability_from_the_graph_alone(mirror):
    first = commit(mirror, "first")
    second = commit(mirror, "second")
    third = commit(mirror, "third")
    graph = LocalGitAncestry(mirror)
    assert graph.available() is True
    assert graph.is_ancestor(first, third) is True
    assert graph.is_ancestor(second, third) is True
    assert graph.is_ancestor(third, third) is True


def test_reports_a_stranger_as_no_rather_than_as_a_failure(mirror):
    first = commit(mirror, "first")
    commit(mirror, "second")
    git(mirror, "checkout", "-q", "--orphan", "aside")
    stranger = commit(mirror, "unrelated")
    graph = LocalGitAncestry(mirror)
    # Both commits are present, so the graph may answer, and the answer is no.
    assert graph.is_ancestor(first, stranger) is False
    assert graph.is_ancestor(stranger, first) is False
    assert graph.stats()["answered"] == 2


def test_refuses_a_missing_commit_instead_of_calling_it_unreachable(mirror):
    first = commit(mirror, "first")
    graph = LocalGitAncestry(mirror)
    with pytest.raises(AncestryUnavailable) as refused:
        graph.is_ancestor(first, "b" * 40)
    assert refused.value.reason == "LOCAL_ANCESTRY_OBJECT_MISSING"
    # A refusal must never be counted as an answer: they mean opposite things.
    assert graph.stats() == {"schema": "local-git-ancestry-v1", "answered": 0, "declined": 1}


def test_reports_a_directory_that_holds_no_object_graph(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert LocalGitAncestry(plain).available() is False


def test_refuses_a_name_that_is_not_a_commit_id(mirror):
    graph = LocalGitAncestry(mirror)
    for bad in ("HEAD", "a" * 39, "a" * 41, "A" * 40, "refs/heads/main", ""):
        with pytest.raises(RecordError):
            graph.is_ancestor(bad, "b" * 40)


def test_resolves_a_ref_only_to_report_where_the_mirror_stopped(mirror):
    head = commit(mirror, "first")
    graph = LocalGitAncestry(mirror)
    assert graph.ref_commit("HEAD") == head
    assert graph.ref_commit("refs/heads/does-not-exist") is None


class Stub:
    """One local-graph answer, or one refusal, with no subprocess involved."""

    def __init__(self, answer):
        self.answer, self.asked, self.declined = answer, [], 0

    def is_ancestor(self, base, head):
        self.asked.append((base, head))
        if isinstance(self.answer, Exception):
            self.declined += 1
            raise self.answer
        return self.answer

    def stats(self):
        return {"schema": "local-git-ancestry-v1", "answered": len(self.asked) - self.declined,
                "declined": self.declined}


class API:
    def __init__(self, base):
        self.base, self.paths = base, []

    def request(self, method, path, **_):
        self.paths.append(path)
        return {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": self.base}}


def store(ancestry):
    """Bind only what `_ancestor` touches, so the test never needs a real remote."""
    bound = object.__new__(GitHubStore)
    bound.binding = type("B", (), {"anchor": ANCHOR})()
    bound.repository = "auguspp/Decision-OS-2"
    bound._prefix = "/repos/auguspp/Decision-OS-2"
    bound._ancestor_proofs = set()
    bound._ancestry = ancestry
    bound._ancestry_sources = {"remote_compare": 0, "local_graph": 0, "fallbacks": 0}
    bound.api = API(ANCHOR)
    return bound


def test_the_graph_answers_and_the_compare_endpoint_is_never_called():
    graph = Stub(True)
    bound = store(graph)
    bound._ancestor(ANCHOR, "c" * 40)
    assert bound.api.paths == []
    assert graph.asked == [(ANCHOR, "c" * 40)]
    assert bound.ancestry_receipt() == {"schema": "github-store-ancestry-receipt-v1",
                                        "local_enabled": True, "remote_compare": 0,
                                        "local_graph": 1, "fallbacks": 0}


def test_a_refusal_falls_back_to_the_remote_and_still_succeeds():
    bound = store(Stub(AncestryUnavailable("LOCAL_ANCESTRY_OBJECT_MISSING")))
    bound._ancestor(ANCHOR, "c" * 40)
    assert bound.api.paths == ["/repos/auguspp/Decision-OS-2/compare/" + ANCHOR + "..." + "c" * 40
                               + "?per_page=1"]
    assert bound.ancestry_receipt()["fallbacks"] == 1
    assert bound.ancestry_receipt()["remote_compare"] == 1


def test_a_denial_from_the_graph_is_the_same_refusal_as_from_the_remote():
    bound = store(Stub(False))
    with pytest.raises(RecordError) as refused:
        bound._ancestor(ANCHOR, "c" * 40)
    assert str(refused.value) == "COMMIT_OUTSIDE_BINDING"
    # A denial is an answer, so the store must not spend a request double-checking
    # it: both paths refuse with the same code, and the receipt says which one spoke.
    assert bound.api.paths == []
    assert bound.ancestry_receipt()["local_graph"] == 1


def test_without_a_graph_behaviour_is_exactly_the_previous_contract():
    bound = store(None)
    bound._ancestor(ANCHOR, "c" * 40)
    assert len(bound.api.paths) == 1
    assert bound.ancestry_receipt() == {"schema": "github-store-ancestry-receipt-v1",
                                        "local_enabled": False, "remote_compare": 1,
                                        "local_graph": 0, "fallbacks": 0}


def test_a_proved_pair_is_never_asked_twice():
    graph = Stub(True)
    bound = store(graph)
    for _ in range(3):
        bound._ancestor(ANCHOR, "c" * 40)
    assert graph.asked == [(ANCHOR, "c" * 40)]


def test_the_store_rejects_an_accelerator_that_cannot_answer():
    binding = GitHubBinding.from_dict({
        "repository": "auguspp/Decision-OS-2", "repository_id": 1420041744, "branch": "records",
        "anchor": ANCHOR, "store_id": "decision-os-runtime", "visibility": "public",
        "write_enabled": False, "private_upgrade_allowed": True})
    with pytest.raises(RecordError) as refused:
        GitHubStore(binding, API(ANCHOR), ancestry=object())
    assert str(refused.value) == "INVALID_LOCAL_ANCESTRY"
