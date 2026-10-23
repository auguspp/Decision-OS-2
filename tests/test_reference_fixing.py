"""Reference fixing receipts and audits; synthetic slices only, no remote reads."""
from dataclasses import asdict
from inspect import signature
import json

import pytest

from decision_kernel.application import hosted_handoff as handoff
from decision_kernel.application import reference_fixing as fixing
from decision_kernel.application.reference_fixing import from_revision, inspect, listing
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceInput, SourceRef
from decision_kernel.domain.research_work import (
    QuestionFinding, ResearchProgress, ResearchQuestion, ResearchRequest,
)
from decision_kernel.infrastructure.git_store import GitStore

AT = "2026-10-23T10:00:00+08:00"
REFERENCE = SourceRef(BlobRef("auguspp/Decision-OS-2", "a" * 40,
                              "source-views/synthetic/" + "b" * 64 + "/manifest.json", "c" * 64))


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "records.git"))


def market_slice(**changes):
    rows = [{"ts_code": "000001.SZ", "trade_date": "20261022", "close": "11.20"},
            {"ts_code": "600000.SH", "trade_date": "20261022", "close": "9.80"}]
    text = json.dumps({"schema": "market-query-custody-v1", "use": {"api": "daily"},
                       "view": {"rows": rows}}, ensure_ascii=False)
    data = dict(source_id="market-query-synthetic", kind="content", title="Retained market query",
                origin="synthetic:fixture", text=text)
    data.update(changes)
    return SourceInput(**data)


def revision_input(ref, **changes):
    data = dict(case_id="case-one", revision_id="r1", kind="quick", body="合成判断：仍需更多证据。",
                mandate="synthetic:mandate", method="synthetic:method", sources=(ref,))
    data.update(changes)
    return RevisionInput(**data)


def checkpoint_inputs(ref):
    request = ResearchRequest(case_id="case-one", kind="quick", mandate="synthetic:mandate",
                              questions=(ResearchQuestion("q1", "哪个证据可以区分两种解释？"),))
    progress = ResearchProgress(body="合成工作底稿。", findings=(QuestionFinding("q1", "answered", "有限回答"),),
                                stop="continue", stop_reason="继续分析", sources=(ref,),
                                limitations=("仅为合成材料",))
    return request, progress


class _Broken:
    def read_source(self, ref):
        raise RecordError("PATH_OUTSIDE_STORE")


def test_listing_is_a_zero_read_receipt_of_content_addressed_citations():
    report = listing((REFERENCE,), scope="test")
    assert report["tier"] == "listing" and report["status"] == "fixed"
    assert report["sources"] == 1 and report["gaps"] == []
    assert report["slices"][0]["reference"]["manifest"]["sha256"] == REFERENCE.manifest.sha256
    # Receipts must survive being stored as JSON and read back unchanged.
    assert report["slices"][0]["reference"]["selection"] == {
        "kind": "all", "start": None, "end": None, "columns": []}
    assert json.loads(json.dumps(report)) == report
    assert listing(())["status"] == "no-references"


def test_inspect_reads_the_retained_slice_and_harvests_only_named_keys(store):
    ref = store.put_source("put-one", market_slice())
    report = inspect(store, (ref,), scope="test")
    assert report["tier"] == "inspect" and report["status"] == "read" and report["gaps"] == []
    row = report["slices"][0]
    assert row["reference"]["manifest"]["sha256"] == ref.manifest.sha256
    assert row["stored_bytes"] and row["stored_sha256"]
    assert row["harvest_basis"] == fixing.HARVEST_BASIS and row["harvest_truncated"] is False
    assert row["observed_dates"] == ["20261022"]
    assert row["observed_securities"] == ["000001.SZ", "600000.SH"]
    assert {"schema=market-query-custody-v1", "api=daily"} <= set(row["observed_markers"])
    assert row["original_verified"] is False
    assert json.loads(json.dumps(report)) == report


def test_inspect_reports_gaps_instead_of_skipping_a_cited_slice():
    report = inspect(_Broken(), (REFERENCE,), scope="test")
    assert report["status"] == "blocked" and report["sources"] == 1
    assert report["slices"] == [] and report["gaps"][0]["problem"] == "PATH_OUTSIDE_STORE"


def test_revision_audit_reads_cited_slices_without_writing_or_selecting_current(store):
    ref = store.put_source("put-one", market_slice())
    saved = store.submit_revision("submit-one", revision_input(ref))
    head = store.backend.head()
    report = from_revision(store, saved)
    assert report["case_id"] == "case-one" and report["revision_kind"] == "quick"
    assert report["revision"] == asdict(saved)
    assert report["slices"][0]["observed_dates"] == ["20261022"]
    assert store.backend.head() == head


def test_checkpoint_receipt_lists_citations_without_reaching_the_store_again(store):
    ref = store.put_source("put-one", market_slice())
    # The receipt tier cannot read at all: no store parameter exists to read with.
    assert "store" not in signature(fixing.listing).parameters
    request, progress = checkpoint_inputs(ref)
    result = ResearchWorkflow(store).checkpoint(request, progress, submission_id="sub-one", revision_id="r1")
    assert result["reference_fixing"] == fixing.listing((ref,), scope="research-checkpoint")
    assert result["reference_fixing"]["tier"] == "listing"
    assert result["reference_fixing"]["sources"] == 1
    assert result["reference_fixing"]["coverage_assertion"] == fixing.COVERAGE_ASSERTION


def test_handoff_receipt_carries_the_adopted_citation_inventory(store):
    ref = store.put_source("put-one", market_slice())
    saved = store.submit_revision("submit-one", revision_input(ref))
    doc = {"schema": handoff.SCHEMA, "kind": "quick", "execution_date": "2026-10-23", "cutoff": AT,
           "research": asdict(saved), "scope": "Synthetic handoff", "gaps": []}
    result = handoff.save(store, doc, "handoff-one")
    assert result["reference_fixing"]["tier"] == "listing"
    assert result["reference_fixing"]["sources"] == 1
    assert result["reference_fixing"]["slices"][0]["reference"]["manifest"]["sha256"] == ref.manifest.sha256
    assert result["reference_fixing"]["gaps"] == []
    assert result["economic_assessment"] == "not-performed"
    # The receipt is written to report.json and compared after reloading it.
    assert json.loads(json.dumps(result)) == result


class _NoSourceRead:
    """Delegates every store call, but reading source bytes would be a regression."""
    def __init__(self, store):
        self._store = store

    def read_source(self, *args, **kwargs):
        raise AssertionError("a handoff receipt must not read source bytes")

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_handoff_receipt_reads_no_source_bytes(store):
    # The union comes from the revision already being read, so the receipt adds
    # no source request; a slice is fixed by its content address, not by re-reading it.
    ref = store.put_source("put-one", market_slice())
    saved = store.submit_revision("submit-one", revision_input(ref))
    doc = {"schema": handoff.SCHEMA, "kind": "quick", "execution_date": "2026-10-23", "cutoff": AT,
           "research": asdict(saved), "scope": "Synthetic handoff", "gaps": []}
    before = store.read_revision(saved)
    result = handoff.save(_NoSourceRead(store), doc, "handoff-one")
    assert result["status"] == "readable"
    assert result["reference_fixing"]["gaps"] == []
    # Saving a handoff never rewrites the revision it adopts.
    assert store.read_revision(saved) == before


def test_harvest_truncates_instead_of_claiming_completeness():
    payload = {"rows": [{"ts_code": "000001.SZ", "trade_date": "20261022"} for _ in range(30000)]}
    dates, codes, markers, truncated = fixing._harvest(payload)
    assert truncated is True and dates and codes and markers == []
