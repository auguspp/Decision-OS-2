"""Real Git-backed catalogue and host integration; synthetic research, no live capture."""
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from test_research_runtime import host, invoke, request, source, work


def seed(host, tmp_path, case_id="example", ref=None):
    if ref is None:
        ref = invoke(host, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]
    payload = work(ref, request=request(case_id=case_id))
    result = invoke(host, tmp_path / (case_id + "-save"), "checkpoint", payload, case_id)
    return ref, payload, result["result"]["reference"]


def test_empty_live_shape_does_not_infer_unexecuted_tasks_or_quiet_market(host, tmp_path):
    before = host.git.head()
    result = invoke(host, tmp_path / "catalogue", "catalogue", {})
    assert result["write"] == "not-requested" and result["status"] == "completed"
    assert result["result"]["status"] == "no-saved-research"
    assert result["result"]["task_execution"] == "not-inferred-from-records"
    assert host.writes == 0 and host.git.head() == before


def test_saved_research_discovered_without_case_id_then_opened_by_exact_reference(host, tmp_path):
    ref, payload, original = seed(host, tmp_path)
    count, before = host.writes, host.git.head()
    page = invoke(host, tmp_path / "catalogue", "catalogue", {})["result"]
    row, = page["items"]
    assert row["reference"] == original and row["mandate"] == payload["request"]["mandate"]
    assert row["continuation"]["actions"][0]["next_action"] == "继续核算"
    assert row["source_page"]["items"][0]["status"] == "stored-representation"
    assert row["body"] == "not-included-use-exact-open-payload"
    assert "data" not in row["source_page"]["items"][0]
    assert page["human_selection"] == "not-recorded-by-read"
    opened = invoke(host, tmp_path / "open", row["open_operation"], row["open_payload"])["result"]
    assert opened["previous_context"]["research"]["reference"] == original
    assert opened["previous_context"]["research"]["body"].startswith("合成部分成果")
    assert host.writes == count and host.git.head() == before


def test_catalogue_cursor_does_not_mix_later_cases_or_later_current_revisions(host, tmp_path):
    ref, _, _ = seed(host, tmp_path, "a")
    _, later, b_original = seed(host, tmp_path, "b", ref)
    page = invoke(host, tmp_path / "first-page", "catalogue", {"limit": 1})["result"]
    later["request"]["predecessor"], later["revision_id"] = b_original, "r2"
    newer = invoke(host, tmp_path / "b-newer", "checkpoint", later, "b-next")["result"]["reference"]
    seed(host, tmp_path, "c", ref)
    before, count = host.git.head(), host.writes
    tail = invoke(host, tmp_path / "second-page", "catalogue", {"cursor": page["next_cursor"]})["result"]
    assert tail["snapshot"] == page["snapshot"] and tail["total_cases"] == 2
    assert [row["case_id"] for row in tail["items"]] == ["b"]
    assert tail["items"][0]["reference"] == b_original and tail["next_cursor"] is None
    fresh = invoke(host, tmp_path / "fresh", "catalogue", {})["result"]
    assert fresh["total_cases"] == 3 and fresh["items"][1]["reference"] == newer
    assert host.writes == count and host.git.head() == before


def test_broken_current_pointer_and_missing_revision_do_not_hide_good_sibling(host, tmp_path):
    seed(host, tmp_path, "good")
    host.git.commit({
        "research/cases/bad/current.json": encoded({"revision_path": "foreign/path"}),
        "research/cases/missing/revisions/r1/research.md": b"orphan synthetic body",
    }, expected=host.git.head())
    page = invoke(host, tmp_path / "catalogue", "catalogue", {})["result"]
    assert page["status"] == "partial-page" and page["total_cases"] == 3
    assert {row["case_id"]: row["status"] for row in page["items"]} == {
        "bad": "invalid", "good": "readable", "missing": "missing"}
    assert page["remaining"] == 0 and page["next_cursor"] is None


def test_ordinary_report_keeps_raw_unresolved_without_inventing_structured_findings(host, tmp_path):
    ref = invoke(host, tmp_path / "source", "put-source", source(), "source")["result"]["reference"]
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    original = store.submit_revision("ordinary", RevisionInput(
        "ordinary", "r1", "quick", "合成普通报告", "明确原委托", "legacy-new-store-method",
        (SourceRef.from_dict(ref),), unresolved=("有意义的原未决问题",)))
    row, = invoke(host, tmp_path / "catalogue", "catalogue", {})["result"]["items"]
    assert not row["structured_workpaper"] and row["continuation"] is None
    assert row["recorded_unresolved"] == ["有意义的原未决问题"] and row["open_operation"] == "resume"
    assert row["reference"] == asdict(original)


def test_source_failure_is_local_and_additional_sources_use_original_source_cursor(host, tmp_path, monkeypatch):
    ref, payload, original = seed(host, tmp_path)
    second = invoke(host, tmp_path / "s2", "put-source", source(source_id="second"), "second")["result"]["reference"]
    payload["request"]["predecessor"], payload["revision_id"] = original, "r2"
    payload["progress"]["sources"].append(second)
    current = invoke(host, tmp_path / "r2", "checkpoint", payload, "second-revision")["result"]["reference"]
    original_read = VersionedStore.read_source
    def deny_first(self, source_ref):
        if asdict(source_ref) == ref:
            raise StoreUnavailable("do not reveal private service details")
        return original_read(self, source_ref)
    monkeypatch.setattr(VersionedStore, "read_source", deny_first)
    page = invoke(host, tmp_path / "catalogue", "catalogue", {})["result"]
    row, = page["items"]
    assert row["status"] == "readable" and row["source_page"]["items"][0]["status"] == "unavailable"
    assert row["source_page"]["remaining"] == 1 and row["reference"] == current
    assert row["source_next_cursor"]["reference"] == current
    assert "private service details" not in json.dumps(row)
    next_source = invoke(host, tmp_path / "next-source", "resume", {"cursor": row["source_next_cursor"]})["result"]
    assert next_source["source_page"]["items"][0]["reference"] == second


@pytest.mark.parametrize("payload", [{"query": "unsupported"}, {"case_id": "example"}, {"repository": "other"}])
def test_catalogue_payload_does_not_expand_runtime_authority(host, tmp_path, payload):
    with pytest.raises(RecordError, match="INVALID_CATALOGUE_PAYLOAD"):
        invoke(host, tmp_path / "catalogue", "catalogue", payload)
    assert host.writes == 0
