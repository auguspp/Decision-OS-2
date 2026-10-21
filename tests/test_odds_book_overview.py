"""One real bounded Book traversal; stable scope, original records and gaps."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import odds_book, odds_book_overview as overview
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.odds_watch import OddsWatch
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, StoreUnavailable
from decision_kernel.infrastructure.github_store import GitHubStore
from test_conditional_odds import context
from test_github_store import GitAPI
from test_odds_book import saved_pair


def add_full(store, case_id="other", revision_id="r1", predecessor=None):
    return store.submit_revision(case_id + "-" + revision_id, RevisionInput(
        case_id, revision_id, "full", "Synthetic saved Full", "Synthetic mandate",
        "synthetic-method", predecessor=predecessor, unresolved=("Unresolved evidence",)))


def test_same_spec_results_across_real_pages_preserve_original_refs_and_research_versions(context, monkeypatch):
    store, research, spec, alias, first, second = saved_pair(context)
    newer = add_full(store, "case", "r2", research)
    other = add_full(store)
    before, original, native_pages, backends = store.backend.head(), odds_book.read_book, [], []

    def observed(selected_store, query):
        backends.append(selected_store.backend)
        page = original(selected_store, query)
        native_pages.append(deepcopy(page))
        return page

    monkeypatch.setattr(odds_book, "read_book", observed)
    monkeypatch.setattr(ConditionalOdds, "compute", lambda *a, **k: pytest.fail("Overview cannot calculate"))
    monkeypatch.setattr(OddsWatch, "read_watch", lambda *a, **k: pytest.fail("Overview cannot replay Watch"))
    result = overview.read_overview(store, {"limit": 2}, page_limit=4)
    assert len(native_pages) == 3 and all(backend is store.backend for backend in backends)
    assert result["pages"] == native_pages and store.backend.head() == before
    assert result["snapshot"] == result["query"]["snapshot"] == before
    assert result["scan"] == {"start": 0, "end": 6, "total_candidates": 6, "at_end": True,
                              "complete_from_start": True, "unread_prefix": 0, "unread_tail": 0}
    assert result["counts"] == {"pages": 3, "scanned_candidates": 6, "returned_items": 6,
                                "filtered_candidates": 0, "readable_items": 6, "failed_items": 0,
                                "omitted_items": 0}
    assert result["status"] == "readable" and result["content_complete"] is True
    groups = {(row["research"]["case_id"], row["research"]["revision_id"]): row
              for row in result["research_relations"]}
    historical = groups["case", "r1"]
    assert historical["research"]["at_snapshot"] == {"status": "historical", "reference": asdict(newer)}
    assert historical["coverage"] == "this-read-range-only"
    assert historical["saved_odds"] == "present-in-this-read-not-current-certification"
    same = historical["spec_groups"][0]
    assert {tuple(sorted(ref.items())) for ref in same["original_references"]} == {
        tuple(sorted(spec.items())), tuple(sorted(alias.items()))}
    assert {row["price"]["price"] for row in same["results"]} == {"80", "100"}
    assert {row["reference"]["path"] for row in same["results"]} == {first["path"], second["path"]}
    assert all(row["comparison_scope"] == "same-spec-saved-results-in-this-read-not-latest-price"
               for row in same["results"])
    assert groups["other", "r1"]["research"]["reference"] == asdict(other)
    for key in (("other", "r1"), ("case", "r2")):
        assert groups[key]["saved_odds"] == "not-found-in-complete-scan-of-known-layouts"
    assert all(page["coverage"] == "this-page-only" for page in result["pages"])
    assert result["semantic_replay"] == "not-requested" and result["persistence"] == "not-written"


def test_filtered_empty_pages_keep_denominator_and_tail_does_not_prove_prefix(context):
    store, *_ = saved_pair(context)
    other = add_full(store)
    query = {"research_ref": asdict(other), "scan_limit": 1}
    original = deepcopy(query)
    first = overview.read_overview(store, query, page_limit=4)
    assert query == original
    assert first["counts"]["pages"] == first["counts"]["scanned_candidates"] == 4
    assert first["counts"]["returned_items"] == 0 and first["counts"]["filtered_candidates"] == 4
    assert first["scan"]["total_candidates"] == 5 and first["scan"]["unread_tail"] == 1
    assert first["content_complete"] is True and first["scan"]["complete_from_start"] is False
    assert first["status"] == "partial" and first["research_relations"] == []
    cursor = first["next_cursor"]
    assert cursor["research_ref"] == asdict(other)
    tail = overview.read_overview(store, {"cursor": cursor}, page_limit=4)
    assert tail["query"]["research_ref"] == asdict(other)
    assert tail["counts"]["pages"] == 1 and tail["scan"]["at_end"] is True
    assert tail["scan"]["unread_prefix"] == 4 and tail["scan"]["complete_from_start"] is False
    assert tail["content_complete"] is True and tail["status"] == "partial"
    assert tail["research_relations"][0]["saved_odds"] == "not-observed-in-this-read"
    end = overview.read_overview(store, {"cursor": {**cursor, "offset": 5}}, page_limit=4)
    assert end["counts"]["pages"] == 1 and end["counts"]["scanned_candidates"] == 0
    assert end["scan"]["at_end"] is True and end["scan"]["unread_prefix"] == 5
    assert end["scan"]["complete_from_start"] is False and end["status"] == "partial"


def test_new_store_head_between_pages_does_not_enter_pinned_overview(context, monkeypatch):
    store, research, *_ = saved_pair(context)
    original, requests = odds_book.read_book, []
    before = store.backend.head()

    def changed(selected_store, query):
        requests.append(deepcopy(query))
        page = original(selected_store, query)
        if len(requests) == 1:
            add_full(store, "case", "r2", research)
        return page

    monkeypatch.setattr(odds_book, "read_book", changed)
    result = overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert store.backend.head() != before
    assert result["snapshot"] == before and result["scan"]["total_candidates"] == 4
    assert all(request["snapshot"] == before for request in requests[1:])
    assert all(page["snapshot"] == before for page in result["pages"])
    assert result["research_relations"][0]["research"]["at_snapshot"] == {
        "status": "current", "reference": asdict(research)}


def test_complete_scan_with_corrupt_candidate_keeps_failure_and_does_not_certify_absence(context):
    store, _, _, _, first, _ = saved_pair(context)
    add_full(store)
    path = first["path"].rsplit("/", 1)[0] + "/content.bin"
    store.backend.commit({path: b"synthetic corrupted snapshot"}, expected=store.backend.head())
    result = overview.read_overview(store, {"limit": 2}, page_limit=4)
    assert result["scan"]["complete_from_start"] is True and result["content_complete"] is False
    assert result["counts"]["failed_items"] == 1 and result["status"] == "partial"
    failed = [row for page in result["pages"] for row in page["items"] if row["status"] == "invalid"]
    assert len(failed) == 1 and failed[0]["reference"] == first
    other = next(row for row in result["research_relations"] if row["research"]["case_id"] == "other")
    assert other["saved_odds"] == "not-observed-in-this-read"
    assert ConditionalOdds(store).read(first)["recalculation"] == "matches"


def test_card_capacity_omission_survives_all_pages_and_has_its_own_count(context, monkeypatch):
    store, *_ = saved_pair(context)
    monkeypatch.setattr(odds_book, "MAX_CARD_BYTES", 20)
    result = overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert result["scan"]["complete_from_start"] is True and result["content_complete"] is False
    assert result["counts"]["omitted_items"] == 4 and result["counts"]["failed_items"] == 0
    assert all(row["reference"] and row["status"] == "not-included"
               for page in result["pages"] for row in page["items"])
    assert result["research_relations"] == [] and result["status"] == "partial"


@pytest.mark.parametrize("value", [None, True, 0, 5, "4", [], {}])
def test_page_limit_preflight_rejects_before_native_io(value, monkeypatch):
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: pytest.fail("Unexpected native read"))
    with pytest.raises(RecordError, match="ODDS_BOOK_OVERVIEW_PAGE_LIMIT"):
        overview.read_overview(object(), {}, page_limit=value)


def test_caller_pages_are_not_an_accepted_query_or_read_receipt(monkeypatch):
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: pytest.fail("Unexpected native read"))
    with pytest.raises(RecordError, match="ODDS_BOOK_PLAN_INVALID"):
        overview.read_overview(object(), {"pages": [], "complete_from_start": True})


@pytest.mark.parametrize("damage", ["snapshot", "denominator", "gap", "filter", "duplicate"])
def test_inconsistent_second_native_page_discards_earlier_success_and_stops(context, monkeypatch, damage):
    store, *_ = saved_pair(context)
    original, calls, first_path = odds_book.read_book, [], []

    def altered(selected_store, query):
        calls.append(deepcopy(query))
        page = original(selected_store, query)
        if len(calls) == 1:
            first_path.append(page["items"][0]["path"])
        if len(calls) == 2:
            if damage == "snapshot": page["snapshot"] = "f" * 40
            elif damage == "denominator": page["scan"]["total_candidates"] += 1
            elif damage == "gap": page["scan"]["start"] -= 1
            elif damage == "filter": page["next_cursor"]["research_ref"] = {"forged": "filter"}
            else: page["items"][0]["path"] = first_path[0]
        return page

    monkeypatch.setattr(odds_book, "read_book", altered)
    with pytest.raises(StoreUnavailable, match="ODDS_BOOK_OVERVIEW_PAGE_MISMATCH"):
        overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert len(calls) == 2


def test_later_native_terminal_preserves_exception_and_never_attempts_a_third_page(context, monkeypatch):
    store, *_ = saved_pair(context)
    original, calls = odds_book.read_book, []
    failure = StoreUnavailable("REMOTE_REQUEST_LIMIT")

    def fail_second(selected_store, query):
        calls.append(query)
        if len(calls) == 2:
            raise failure
        return original(selected_store, query)

    monkeypatch.setattr(odds_book, "read_book", fail_second)
    with pytest.raises(StoreUnavailable) as caught:
        overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert caught.value is failure and len(calls) == 2


def test_real_native_backend_api_and_final_visibility_are_shared_across_pages(tmp_path, monkeypatch):
    api = GitAPI(tmp_path / "overview-native.git")
    backend = GitHubStore(api.binding, api)
    store = VersionedStore(backend)
    for name in ("a", "b", "c"):
        add_full(store, name)
    original, calls = odds_book.read_book, []

    def observed(selected_store, query):
        assert selected_store is store and selected_store.backend is backend and backend.api is api
        calls.append(query)
        return original(selected_store, query)

    monkeypatch.setattr(odds_book, "read_book", observed)
    before, head = len(api.requests), api.git.head()
    result = overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert len(calls) == 3 and result["scan"]["complete_from_start"] is True
    assert api.git.head() == head and all(method == "GET" for method, _, _ in api.requests[before:])
    calls.clear()

    def reversed_visibility(selected_store, query):
        page = observed(selected_store, query)
        if len(calls) == 1:
            api.meta["private"] = False
        return page

    monkeypatch.setattr(odds_book, "read_book", reversed_visibility)
    with pytest.raises(RecordError, match="PRIVATE_BINDING_MISMATCH"):
        overview.read_overview(store, {"limit": 1}, page_limit=4)
    assert len(calls) == 2
