"""Real local Git and synthetic research; no external acquisition or model calls."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application.research_reader import ResearchReader
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    IntegrityError, MissingObject, RecordError, RevisionInput, Selection,
    SourceInput, SourceRef, StoreUnavailable,
)
from decision_kernel.infrastructure.git_store import GitStore


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / "research.git"))


def source(store, name, text="合成证据，不是市场数据。", **kwargs):
    return store.put_source("put-" + name, SourceInput(
        source_id=name, kind="content", title="合成来源 " + name,
        origin="synthetic:" + name, text=text, **kwargs))


def save(store, refs=(), *, case="case", revision="r1", kind="full", predecessor=None):
    return store.submit_revision(case + "-" + revision, RevisionInput(
        case, revision, kind, "合成正文：证据尚不足，不宣称研究完成。", "仅工程验证", "合成方法",
        tuple(refs), predecessor, ("仍需核实经营假设",)))


@pytest.mark.parametrize("kind", ["quick", "full"])
def test_restores_same_contract_for_both_research_kinds(store, kind, monkeypatch):
    ref = source(store, "one", acquired_at="2026-10-15", published_at="2026-10-14")
    saved = save(store, [ref], kind=kind)
    head = store.backend.head()
    monkeypatch.setattr(store.backend, "commit", lambda *a, **kw: pytest.fail("read attempted a write"))
    result = ResearchReader(store).resume("case")
    assert result["research"] == store.read_revision(saved)
    assert result["research"]["record"]["input"]["kind"] == kind
    assert result["research"]["record"]["input"]["unresolved"] == ["仍需核实经营假设"]
    assert result["research"]["record"]["human_acceptance"] == "not-confirmed"
    item = result["source_page"]["items"][0]
    assert item["reference"] == asdict(ref) and item["status"] == "stored-representation"
    assert item["view"]["manifest"]["input"]["published_at"] == "2026-10-14"
    assert result["research_assessment"] == "not-performed"
    assert result["source_use_acceptance"]["status"] == "not-demonstrated"
    assert result["source_use_acceptance"].get("method") is None
    assert result["execution_authority"] == "not-granted-by-read"
    assert result["next_cursor"] is None and store.backend.head() == head


def test_cursor_pins_revision_across_new_writer_and_json_roundtrip(store):
    refs = [source(store, name) for name in ("one", "two", "three")]
    old = save(store, refs, kind="quick")
    first = ResearchReader(store).resume("case", source_limit=1)
    newer = save(store, [refs[0]], revision="r2", predecessor=old)
    cursor = json.loads(json.dumps(first["next_cursor"]))
    reopened = VersionedStore(GitStore(store.backend.root))
    last = ResearchReader(reopened).resume(cursor=cursor, source_limit=8)
    assert last["research"]["reference"] == asdict(old)
    assert last["research"]["record"]["input"]["kind"] == "quick"
    assert [row["index"] for row in last["source_page"]["items"]] == [1, 2]
    assert last["source_page"]["coverage"] == "this-page-only"
    assert last["next_cursor"] is None
    assert ResearchReader(reopened).resume("case")["research"]["reference"] == asdict(newer)


def test_exact_previous_revision_and_its_source_never_follow_latest(store):
    first_source = source(store, "same", "first version")
    old = save(store, [first_source])
    new_source = store.put_source("updated-source", SourceInput("same", "content", "updated", "synthetic:same", "second version"))
    current = save(store, [new_source], revision="r2", predecessor=old)
    view = ResearchReader(store).resume(reference=old)
    assert view["source_page"]["items"][0]["view"]["data"] == "first version"
    assert view["research"]["record"]["input"]["predecessor"] is None
    assert ResearchReader(store).resume("case")["research"]["record"]["input"]["predecessor"] == asdict(old)
    assert store.current("case") == current


def test_no_sources_preserves_unresolved_not_research_completion(store):
    save(store)
    view = ResearchReader(store).resume("case")
    assert view["source_page"] == dict(total=0, offset=0, returned=0, remaining=0,
                                       end_of_list=True, coverage="this-page-only", items=[])
    assert view["research"]["record"]["input"]["unresolved"]
    assert view["research_assessment"] == "not-performed"
    assert view["source_use_acceptance"].get("method") is None


def test_external_locator_does_not_claim_stored_original(store):
    ref = source(store, "external", None, custody="external", limitations=("原文未保管",))
    save(store, [ref])
    item = ResearchReader(store).resume("case")["source_page"]["items"][0]
    assert item["status"] == "external-only" and item["view"]["data"] is None
    assert not item["view"]["original_verified"]
    assert item["view"]["limitations"] == ["原文未保管"]


def test_original_line_and_dataset_selections_preserved(store):
    lines = source(store, "lines", "ignored\nselected\nignored\n")
    selected = SourceRef(lines.manifest, Selection("lines", 2, 2))
    data = store.put_source("table", SourceInput("table", "dataset", "合成表", "synthetic:table", "name,value\na,1\nb,2\n"))
    rows = SourceRef(data.manifest, Selection("rows", 2, 2, ("value",)))
    save(store, [selected, rows])
    items = ResearchReader(store).resume("case")["source_page"]["items"]
    assert items[0]["view"]["data"] == "selected\n"
    assert items[1]["view"]["data"] == [{"value": "2"}]
    assert items[1]["reference"] == asdict(rows)


@pytest.mark.parametrize("error,status,code", [
    (MissingObject, "missing", "SOURCE_OBJECT_MISSING"),
    (StoreUnavailable, "unavailable", "SOURCE_UNAVAILABLE"),
    (IntegrityError, "invalid", "SOURCE_INTEGRITY_FAILED"),
    (RecordError, "invalid", "SOURCE_VALIDATION_FAILED"),
])
def test_source_failure_isolated_and_private_error_not_echoed(store, monkeypatch, error, status, code):
    refs = [source(store, n) for n in ("bad", "good")]
    save(store, refs)
    read = store.read_source
    def broken(ref):
        if ref == refs[0]:
            raise error("secret response should never appear")
        return read(ref)
    monkeypatch.setattr(store, "read_source", broken)
    result = ResearchReader(store).resume("case")
    first, second = result["source_page"]["items"]
    assert (first["status"], first["error"], first["view"]) == (status, code, None)
    assert second["status"] == "stored-representation" and result["research"]["body"]
    assert "secret response" not in json.dumps(result)


def test_programming_failure_is_not_hidden_as_source_absence(store, monkeypatch):
    save(store, [source(store, "one")])
    def broken(ref):
        raise RuntimeError("programming error")
    monkeypatch.setattr(store, "read_source", broken)
    with pytest.raises(RuntimeError):
        ResearchReader(store).resume("case")


def test_broken_research_body_fails_without_falling_back(store, monkeypatch):
    ref = save(store)
    save(store, revision="r2", predecessor=ref)
    read = store.backend.read
    def corrupt(commit, path):
        raw = read(commit, path)
        return b"bad body" if commit == ref.commit and path.endswith("research.md") else raw
    monkeypatch.setattr(store.backend, "read", corrupt)
    with pytest.raises(IntegrityError, match="BLOB_DIGEST_MISMATCH"):
        ResearchReader(store).resume(reference=ref)


def test_missing_case_is_not_successful_empty_research(store):
    with pytest.raises(MissingObject, match="RESEARCH_NOT_FOUND"):
        ResearchReader(store).resume("absent")


def test_foreign_store_reference_is_rejected(store, tmp_path):
    other = VersionedStore(GitStore.init_synthetic(tmp_path / "other.git"))
    ref = save(other)
    with pytest.raises(RecordError, match="FOREIGN_REPOSITORY"):
        ResearchReader(store).resume(reference=ref)


def test_non_research_path_rejected_before_read(store, monkeypatch):
    ref = source(store, "source").manifest
    monkeypatch.setattr(store, "read_revision", lambda ref: pytest.fail("unrelated file read"))
    with pytest.raises(RecordError, match="RESEARCH_REVISION_REFERENCE_REQUIRED"):
        ResearchReader(store).resume(reference=ref)


@pytest.mark.parametrize("limit", [0, 9, -1, True, 1.5, "2"])
def test_bad_limits_rejected_before_backend_access(store, monkeypatch, limit):
    monkeypatch.setattr(store, "current", lambda case: pytest.fail("invalid request read"))
    with pytest.raises(RecordError, match="INVALID_SOURCE_PAGE_LIMIT"):
        ResearchReader(store).resume("case", source_limit=limit)


@pytest.mark.parametrize("options", [{}, {"case_id": "case", "cursor": {}},
                                      {"reference": "not a reference"}, {"case_id": "../case"}])
def test_invalid_selectors(store, options):
    with pytest.raises(RecordError):
        ResearchReader(store).resume(**options)


@pytest.mark.parametrize("change", [
    {"schema": "unknown"}, {"source_offset": -1}, {"source_offset": True},
    {"source_offset": 100}, {"reference": None}, {"extra": "not accepted"},
    {"reference": {"commit": "bad"}},
])
def test_cursor_validation(store, change):
    refs = [source(store, n) for n in ("one", "two")]
    save(store, refs)
    cursor = ResearchReader(store).resume("case", source_limit=1)["next_cursor"]
    cursor.update(change)
    with pytest.raises(RecordError):
        ResearchReader(store).resume(cursor=cursor)


def test_aggregate_source_page_budget_makes_progress_without_truncation(store, monkeypatch):
    import decision_kernel.application.research_reader as module
    refs = [source(store, n, "x" * 3000) for n in ("one", "two")]
    save(store, refs)
    first_item = ResearchReader(store).resume("case", source_limit=1)["source_page"]["items"][0]
    from decision_kernel.domain.records import encoded
    monkeypatch.setattr(module, "MAX_SOURCE_PAGE_BYTES", len(encoded(first_item)) + 200)
    first = ResearchReader(store).resume("case")
    assert first["source_page"]["returned"] == 1 and first["next_cursor"]["source_offset"] == 1
    second = ResearchReader(store).resume(cursor=first["next_cursor"])
    assert second["source_page"]["items"][0]["view"]["data"] == "x" * 3000
    assert second["next_cursor"] is None


def test_oversized_source_explicitly_omitted_not_silently_truncated(store, monkeypatch):
    import decision_kernel.application.research_reader as module
    refs = [source(store, "large", "x" * 5000), source(store, "small", "tiny")]
    save(store, refs)
    monkeypatch.setattr(module, "MAX_SOURCE_PAGE_BYTES", 4000)
    result = ResearchReader(store).resume("case")
    large, small = result["source_page"]["items"]
    assert large["status"] == "not-included" and large["view"] is None
    assert large["reference"] == asdict(refs[0])
    assert large["error"] == "SOURCE_VIEW_EXCEEDS_PAGE_LIMIT"
    assert small["view"]["data"] == "tiny" and result["source_page"]["end_of_list"]
    assert result["research_assessment"] == "not-performed"


def test_cursor_can_be_replayed_without_writes_and_keeps_duplicate_uses(store):
    ref = source(store, "one", "one\ntwo\n")
    save(store, [ref, SourceRef(ref.manifest, Selection("lines", 2, 2))])
    first = ResearchReader(store).resume("case", source_limit=1)
    head = store.backend.head()
    reader = ResearchReader(store)
    page = reader.resume(cursor=first["next_cursor"])
    assert page == reader.resume(cursor=deepcopy(first["next_cursor"]))
    assert page["source_page"]["items"][0]["view"]["data"] == "two\n"
    assert store.backend.head() == head
