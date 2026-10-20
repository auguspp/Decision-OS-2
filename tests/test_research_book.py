"""Original revisions in the normal Room, with no Book I/O or new truth."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application import research_book, reading_room, versioned_store
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.domain.records import BlobRef, MissingObject, RevisionInput, SourceInput, encoded
from decision_kernel.domain.research_work import (
    WORK_MARKER, QuestionFinding, ResearchChallenge, ResearchProgress, ResearchQuestion, ResearchRequest,
)
from test_news_delivery import store

AT = "2026-10-16T12:00:00Z"
LITERAL = "</script><img src=x onerror=alert(1)>"


def book_versions(store, monkeypatch, *, legacy=True):
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-16T10:00:00Z")
    source = store.put_source("book-source", SourceInput("book-source", "content", "合成来源",
        "synthetic:research-book", "原证据第一行。\n原证据第二行。\n", limitations=("Synthetic only.",)))
    request = ResearchRequest("book-synthetic", "full", "合成经营委托 " + LITERAL,
        (ResearchQuestion("q1", "原问题一"), ResearchQuestion("q2", "原问题二")))
    progress = ResearchProgress("合成原判断第一行。\n合成原判断第二行。\n" + LITERAL + "\n",
        (QuestionFinding("q2", "open", "原尚待核算", (0,), "acquired-unanalysed", "核算已得资料"),
         QuestionFinding("q1", "answered", "原限定判断", (0,))), "continue", "还有原问题",
        sources=(source,), challenges=(ResearchChallenge("q2", "先记录的挑战", "unresolved", "尚待核实", (0,)),
                                      ResearchChallenge("q1", "后记录的挑战 " + LITERAL, "retained-with-reason", "原回应", (0,))),
        limitations=("仅为合成研究，不是真实投资结论。",), source_changes="原资料变化声明")
    first = ResearchWorkflow(store).checkpoint(request, progress, submission_id="book-r1", revision_id="r1")
    monkeypatch.setattr(versioned_store, "_now", lambda: "2026-10-16T10:30:00Z")
    second = ResearchWorkflow(store).checkpoint(replace(request, predecessor=BlobRef(**first["reference"])),
        replace(progress, body="合成修订第一行。\n合成修订第二行。\n" + LITERAL + "\n", source_changes="本版没有新增源调用的原声明"),
        submission_id="book-r2", revision_id="r2")
    if legacy:
        store.submit_revision("book-legacy", RevisionInput("z-legacy", "r1", "quick", "旧正文没有规范工作底稿。\n",
            "原旧委托", "synthetic", unresolved=("原旧未决项",)))
    return first["reference"], second["reference"], json.loads(encoded(asdict(source)))


def entry(result, reference):
    return next(e for e in result["research_book"]["entries"] if e["reference"] == reference)


def test_original_findings_order_questions_evidence_and_missing_fields_with_no_extra_reads(store, monkeypatch):
    old, current, source = book_versions(store, monkeypatch)
    plan = {"as_of": AT, "reference": old}
    calls = []
    revision, read_source = store.read_revision, store.read_source
    def read(ref):
        calls.append(("revision", asdict(ref)));return revision(ref)
    def source_read(ref):
        calls.append(("source", asdict(ref)));return read_source(ref)
    monkeypatch.setattr(store, "read_revision", read)
    monkeypatch.setattr(store, "read_source", source_read)
    before = store.backend.head()
    result = ReadingRoom(store).build(plan)
    with_book = deepcopy(calls);calls.clear()
    monkeypatch.setattr(research_book, "project_research", lambda *a, **k: None)
    without = ReadingRoom(store).build(plan)
    assert calls == with_book and result["readings"] == without["readings"]
    assert store.backend.head() == before
    book = result["research_book"]
    assert book["catalogue"]["total_cases"] == 2 and book["catalogue"]["returned"] == 2
    assert book["explicit_versions"] == 1 and len(book["entries"]) == 3
    value = entry(result, current)
    assert value["sources"] == [source] and "body" not in value and "body" not in value["workpaper"]
    assert [x["question_id"] for x in value["workpaper"]["questions"]] == ["q1", "q2"]
    assert [x["question_id"] for x in value["workpaper"]["findings"]] == ["q2", "q1"]
    assert [x["argument"] for x in value["workpaper"]["challenges"]] == ["先记录的挑战", "后记录的挑战 " + LITERAL]
    assert value["workpaper"]["findings"][0]["next_action"] == "核算已得资料"
    assert value["standard_company_id"] is value["research_fact_cutoff"] is value["strongest_challenge"] is None
    assert value["workpaper"]["stop"] == "continue" and entry(result, old)["origin"] == "explicit-reference"


def test_legacy_and_invalid_workpapers_keep_original_bodies_and_exact_routes(store, monkeypatch):
    _, current, _ = book_versions(store, monkeypatch)
    invalid = store.submit_revision("book-malformed", RevisionInput("bad-workpaper", "r1", "quick",
        "Malformed but retained original.\n" + WORK_MARKER + "{broken}\n", "原委托", "synthetic", unresolved=("Synthetic malformed appendix.",)))
    result = ReadingRoom(store).build({"as_of": AT, "reference": asdict(invalid)})
    bad = entry(result, asdict(invalid))
    assert bad["workpaper_status"] == "invalid" and bad["workpaper"] is None
    reading = next(r for r in result["readings"] if r["reference"] == asdict(invalid))
    assert reading["status"] == "readable" and "{broken}" in reading["body"]
    assert reading["open_request"]["payload"]["reference"] == asdict(invalid)
    legacy = next(e for e in result["research_book"]["entries"] if e["case_id"] == "z-legacy")
    assert legacy["workpaper_status"] == "not-recorded" and legacy["workpaper"] is None
    assert legacy["recorded_unresolved"] == ["原旧未决项"] and entry(result, current)["status"] == "readable"


def test_missing_original_source_is_not_retried_by_book_and_body_stays_readable(store, monkeypatch):
    _, current, source = book_versions(store, monkeypatch, legacy=False)
    original, calls = store.read_source, []
    def missing(ref):
        calls.append(asdict(ref))
        if encoded(asdict(ref)) == encoded(source):
            raise MissingObject("SYNTHETIC_MISSING")
        return original(ref)
    monkeypatch.setattr(store, "read_source", missing)
    result = ReadingRoom(store).build({"as_of": AT})
    assert result["readings"][0]["status"] == "readable"
    assert result["catalogue"]["items"][0]["source_page"]["items"][0]["status"] == "missing"
    assert entry(result, current)["sources"] == [source]
    assert entry(result, current)["source_reading"] == "references-only-no-additional-source-read"
    assert result["research_book"]["source_requests"] == result["research_book"]["extra_revision_reads"] == 0


def test_book_budget_never_changes_original_body_admission_or_rewrites_original(store, monkeypatch):
    _, current, _ = book_versions(store, monkeypatch, legacy=False)
    model = ReadingRoom(store)
    detail = model._research(current)
    detail.pop("book_projection")
    monkeypatch.setattr(reading_room, "MAX_BODIES_BYTES", len(encoded(detail)))
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 300)
    result = model.build({"as_of": AT})
    assert result["readings"][0]["status"] == "readable" and result["readings"][0]["body"] == detail["body"]
    assert result["research_book"]["status"] == "not-included"
    assert result["readings"][0]["open_request"]["payload"]["reference"] == current


def test_no_room_capacity_for_book_preserves_the_complete_preexisting_model(store, monkeypatch):
    old, _, _ = book_versions(store, monkeypatch)
    plan = {"as_of": AT, "reference": old}
    first = ReadingRoom(store).build(plan)
    base = {k: v for k, v in first.items() if k != "research_book"}
    monkeypatch.setattr(reading_room, "MAX_VIEW_BYTES", len(encoded(base)))
    exact = ReadingRoom(store).build(plan)
    assert exact == base and "research_book" not in exact
    assert all(r["status"] == "readable" for r in exact["readings"])


def test_original_snapshot_page_explicit_reference_and_review_clock_never_claim_pit(store, monkeypatch):
    old, current, _ = book_versions(store, monkeypatch)
    first = ReadingRoom(store).build({"as_of": "2026-10-01T00:00:00Z", "reference": old, "catalogue": {"limit": 1}})
    store.submit_revision("book-added-after-page", RevisionInput("a-new", "r1", "quick", "New synthetic case.", "Synthetic", "synthetic", unresolved=("Synthetic only.",)))
    following = ReadingRoom(store).build(first["next_plan"])
    assert first["research_book"]["catalogue"]["snapshot"] == following["research_book"]["catalogue"]["snapshot"]
    assert first["research_book"]["catalogue"]["total_cases"] == following["research_book"]["catalogue"]["total_cases"] == 2
    assert first["research_book"]["catalogue"]["offset"] == 0 and following["research_book"]["catalogue"]["offset"] == 1
    assert entry(first, current)["research_fact_cutoff"] is None
    assert entry(first, old)["origin"] == entry(following, old)["origin"] == "explicit-reference"
    assert first["time_basis"] == "as-of-is-review-clock-not-research-cutoff-or-PIT"
    assert entry(first, current)["stored_at"] > first["plan"]["as_of"]


def test_empty_catalogue_and_failed_card_preserve_original_denominator(store):
    empty = ReadingRoom(store).build({"as_of": AT})
    assert empty["research_book"]["catalogue"]["total_cases"] == 0 and empty["research_book"]["entries"] == []
    store.backend.commit({"research/cases/no-current/unused.txt": b"Synthetic abandoned case.\n"}, expected=store.backend.head())
    broken = ReadingRoom(store).build({"as_of": AT})
    assert broken["research_book"]["catalogue"]["total_cases"] == 1
    assert broken["research_book"]["entries"][0]["status"] == "missing"
    assert broken["readings"] == []
