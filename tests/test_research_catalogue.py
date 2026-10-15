"""Catalogue pagination/budgets over a modeled index; real store use tested separately."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from decision_kernel.application import research_catalogue as catalogue
from decision_kernel.domain.records import RecordError, StoreUnavailable

A, B = "a" * 40, "b" * 40


class Index:
    repository = "synthetic-index"

    def __init__(self, cases=()):
        self.now = A
        self.history = {A: ["research/cases/" + case + "/current.json" for case in cases]}
        self.reads = []
        self.denied = False

    def head(self):
        return self.now

    def check_commit(self, snapshot):
        self.reads.append(snapshot)
        if self.denied:
            raise StoreUnavailable("not authorized")
        if snapshot not in self.history:
            raise RecordError("not reachable")

    def paths(self, snapshot, prefix):
        assert prefix == "research/cases/"
        return self.history[snapshot]


class Cards(catalogue.ResearchCatalogue):
    def __init__(self, index):
        super().__init__(SimpleNamespace(backend=index))
        self.visited = []
        self.overrides = {}

    def _item(self, case_id, snapshot):
        self.visited.append((case_id, snapshot))
        return {"case_id": case_id, "status": "readable", "reference": {"case": case_id, "snapshot": snapshot},
                **self.overrides.get(case_id, {})}


def test_empty_snapshot_is_not_zero_candidates_or_unexecuted_tasks():
    index = Index()
    result = Cards(index).page()
    assert result["status"] == "no-saved-research" and result["total_cases"] == 0
    assert result["next_cursor"] is None and result["items"] == []
    assert result["task_execution"] == "not-inferred-from-records"
    assert result["market_quiet"] == "not-assessed" and result["persistence"] == "not-written"


def test_pagination_pins_snapshot_and_deduplicates_historical_case_paths():
    index = Index(("z", "a", "m"))
    index.history[A] += ["research/cases/a/revisions/old/research.md", "source-views/not-a-case/manifest.json"]
    reader = Cards(index)
    first = reader.page(limit=1)
    cursor = deepcopy(first["next_cursor"])
    index.now = B
    index.history[B] = index.history[A] + ["research/cases/new/current.json"]
    rest = reader.page(limit=8, cursor=cursor)
    assert first["total_cases"] == 3 and [r["case_id"] for r in first["items"]] == ["a"]
    assert [r["case_id"] for r in rest["items"]] == ["m", "z"] and rest["remaining"] == 0
    assert rest["snapshot"] == A and rest["next_cursor"] is None
    assert reader.visited == [("a", A), ("m", A), ("z", A)] and cursor == first["next_cursor"]
    assert index.reads == [A, A]


@pytest.mark.parametrize("limit", [0, -1, 9, True, 1.5, "4", None])
def test_limits_are_explicit_not_silently_coerced(limit):
    with pytest.raises(RecordError, match="INVALID_RESEARCH_CATALOGUE_LIMIT"):
        Cards(Index(("a",))).page(limit=limit)


@pytest.mark.parametrize("change", [
    {"schema": "other"}, {"repository": "foreign"}, {"snapshot": "main"},
    {"snapshot": None}, {"offset": True}, {"offset": -1}, {"offset": 5}, {"unexpected": 1},
])
def test_cursor_is_not_a_reusable_foreign_or_unbounded_authority(change):
    index = Index(("a", "b"))
    reader = Cards(index)
    cursor = reader.page(limit=1)["next_cursor"]
    with pytest.raises(RecordError):
        reader.page(cursor={**cursor, **change})


def test_revoked_access_on_later_page_fails_not_empty_result():
    index = Index(("a", "b"))
    reader = Cards(index)
    first = reader.page(limit=1)
    index.denied = True
    with pytest.raises(StoreUnavailable):
        reader.page(cursor=first["next_cursor"])


def test_page_budget_preserves_unreturned_case_at_next_offset(monkeypatch):
    monkeypatch.setattr(catalogue, "MAX_CARD_BYTES", 280)
    reader = Cards(Index(("a", "b", "c")))
    reader.overrides = {case: {"padding": "x" * 80} for case in ("a", "b", "c")}
    first = reader.page(limit=3)
    assert first["returned"] == 1 and first["next_cursor"]["offset"] == 1
    second = reader.page(limit=3, cursor=first["next_cursor"])
    assert second["items"][0]["case_id"] == "b" and second["next_cursor"]["offset"] == 2


def test_oversized_display_card_keeps_exact_ref_not_truncated_research(monkeypatch):
    monkeypatch.setattr(catalogue, "MAX_CARD_BYTES", 260)
    reader = Cards(Index(("a",)))
    reader.overrides["a"] = {"mandate": "x" * 1000}
    result = reader.page()
    item = result["items"][0]
    assert result["status"] == "partial-page" and item["status"] == "not-included"
    assert item["reference"] == {"case": "a", "snapshot": A} and "mandate" not in item
    assert result["remaining"] == 0 and result["next_cursor"] is None


def test_mixed_cards_do_not_claim_full_page_readability():
    reader = Cards(Index(("bad", "good")))
    reader.overrides["bad"] = {"status": "invalid", "error": "RESEARCH_VALIDATION_FAILED"}
    result = reader.page()
    assert result["status"] == "partial-page" and result["returned"] == 2
    assert result["items"][1]["status"] == "readable"
    assert result["human_selection"] == "not-recorded-by-read"


def test_excess_case_budget_fails_instead_of_claiming_truncated_complete_list(monkeypatch):
    monkeypatch.setattr(catalogue, "MAX_CASES", 1)
    with pytest.raises(RecordError, match="RESEARCH_CATALOGUE_CASE_LIMIT"):
        Cards(Index(("a", "b"))).page()
