"""A run is invalidated by a rewritten mainline, not by a later merge.

A run executes the tree GitHub checked out for its own head_sha. Merging into
main afterwards leaves that tree untouched, so it cannot invalidate the run;
what must still be refused is a commit that is no longer reachable from main,
because then the run's provenance was rewritten away.

The cost of the ancestry question is one full-history compare, so these tests
also pin that the question is only asked when it is genuinely open and that the
verdict is not reused after main moves again.
"""
import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.runtime_publication import REPOSITORY, require_code_on_main

CODE = "a" * 40
LATER = "c" * 40
FORGED = "b" * 40
REF = "/repos/" + REPOSITORY + "/git/ref/heads/main"


class Api:
    """Minimal GET model; any route the helper does not need fails the test."""

    def __init__(self, main, ancestry=None):
        self.main = main
        self.ancestry = ancestry or {}
        self.calls = []

    def request(self, method, path):
        assert method == "GET", method
        self.calls.append(path)
        if path == REF:
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": self.main}}
        if "/compare/" in path:
            return self.ancestry[path.split("/compare/")[1].split("?")[0]]
        raise AssertionError("unexpected route: " + path)


def verdict(base=CODE, status="ahead"):
    return {"status": status, "merge_base_commit": {"sha": base}}


def compare_calls(api):
    return [path for path in api.calls if "/compare/" in path]


def test_unchanged_main_never_pays_for_ancestry():
    api = Api(CODE)
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})
    assert api.calls == [REF]


def test_merge_behind_a_running_collection_does_not_invalidate_it():
    # This is the defect: a thirty-three minute capture was discarded because
    # main advanced while the run was finishing, not because its code changed.
    api = Api(LATER, {CODE + "..." + LATER: verdict()})
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})
    assert len(compare_calls(api)) == 1


def test_a_diverged_commit_is_still_refused():
    api = Api(LATER, {CODE + "..." + LATER: verdict(base=FORGED, status="diverged")})
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})


def test_a_mainline_reset_behind_the_run_is_still_refused():
    api = Api(LATER, {CODE + "..." + LATER: verdict(base=LATER, status="behind")})
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})


def test_ahead_status_without_a_matching_merge_base_is_refused():
    # Defence in depth: the base must be the run's own code, not merely an
    # ancestor of it, so an unrelated ahead relationship cannot pass.
    api = Api(LATER, {CODE + "..." + LATER: verdict(base=FORGED)})
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})


@pytest.mark.parametrize("answer", [None, {}, {"status": "ahead"}, "ahead",
    {"status": "ahead", "merge_base_commit": None}])
def test_an_unreadable_ancestry_answer_is_refused_rather_than_guessed(answer):
    api = Api(LATER, {CODE + "..." + LATER: answer})
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})


def test_a_malformed_main_reference_is_refused():
    api = Api(CODE)
    api.request = lambda method, path: {"ref": "refs/heads/other", "object": {}}
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache={})


def test_an_empty_code_identity_is_refused():
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(Api(CODE), "", "FTSHARE_MAIN_MOVED", cache={})


def test_the_verdict_is_reused_while_main_stands_still():
    # The authority check runs again after every bounded unit; a full compare
    # per unit would tax the whole window, so a settled verdict must be reused.
    cache = {}
    api = Api(LATER, {CODE + "..." + LATER: verdict()})
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache=cache)
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache=cache)
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache=cache)
    assert len(compare_calls(api)) == 1
    assert len(api.calls) == 4  # three reference reads, one compare


def test_a_settled_verdict_is_not_reused_after_main_moves_again():
    cache = {}
    api = Api(LATER, {CODE + "..." + LATER: verdict()})
    require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache=cache)
    api.main = "d" * 40
    api.ancestry[CODE + "..." + api.main] = verdict(base=FORGED, status="diverged")
    with pytest.raises(RecordError, match="^FTSHARE_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "FTSHARE_MAIN_MOVED", cache=cache)


def test_the_callers_own_reason_code_is_passed_through():
    api = Api(LATER, {CODE + "..." + LATER: verdict(base=FORGED, status="diverged")})
    with pytest.raises(RecordError, match="^BULK_MAIN_MOVED$"):
        require_code_on_main(api, CODE, "BULK_MAIN_MOVED", cache={})
