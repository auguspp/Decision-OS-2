"""Source catalog bounded contract: old CNINFO semantics, new VersionedStore consumer."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path

import pytest

from decision_kernel.application.source_catalog import catalog_view, save_catalog
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.cninfo_catalog import normalize_catalog, resolve_org, source_for
from decision_kernel.interfaces.cninfo_catalog_cli import main

CODE = "600036"
ORG = "old-official-id"
START = "2026-10-01"
END = "2026-10-07"
CAPTURED = "2026-10-15T09:00:00+00:00"


def ms(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def row(i="123", **changes):
    return {**{"announcementId": i, "secCode": CODE, "orgId": ORG,
               "announcementTitle": "<em>2026年</em>第三季度报告", "announcementTypeName": "定期报告",
               "announcementTime": ms("2026-10-03T11:04:05.012+08:00"),
               "adjunctUrl": "finalpage/2026-10-03/" + i + ".PDF"}, **changes}


def envelope(rows=None, *, total=None):
    rows = rows if rows is not None else [row()]
    return {"captured_at": CAPTURED, "org_rows": [{"code": CODE, "orgId": ORG}],
            "pages": [{"totalAnnouncement": len(rows) if total is None else total,
                       "announcements": rows}]}


def normalize(data=None, **kw):
    raw = envelope() if data is None else data
    return normalize_catalog(stock_code=CODE, start_date=START, end_date=END,
                             captured_at=raw["captured_at"], org_rows=raw["org_rows"],
                             pages=raw["pages"], source_mode="offline-unverified", **kw)


def test_ported_org_resolution_and_official_catalog_timestamp_and_locator():
    c = normalize()
    assert c.org_id == ORG and c.total == 1 and c.page_size == 30
    assert c.announcements[0].title == "2026年第三季度报告"
    assert c.announcements[0].published_at == "2026-10-03T11:04:05.012+08:00"
    assert c.announcements[0].pdf_locator == "https://static.cninfo.com.cn/finalpage/2026-10-03/123.PDF"
    s = source_for(c.announcements[0], c)
    assert s.custody == "external" and s.text is None and s.kind == "content"
    assert s.entities[0].kind == "security" and s.entities[0].entity_id == "cn-a-600036"
    assert s.report_period is None and s.published_at == c.announcements[0].published_at
    assert s.acquired_at == CAPTURED
    assert any("offline-unverified" in x for x in s.limitations)
    assert catalog_view(c)["original_pdf_acquired"] is False


@pytest.mark.parametrize("org_rows", [[], [{"code": CODE, "orgId": "a"}, {"code": CODE, "orgId": "b"}],
                                    [{"code": "000001", "orgId": ORG}],
                                    [{"code": CODE, "orgId": "https://wrong"}],
                                    [{"code": 600036, "orgId": ORG}]])
def test_org_must_be_an_exact_authoritative_mapping(org_rows):
    with pytest.raises(RecordError):
        resolve_org(org_rows, CODE)


def test_observed_org_search_object_wrapper_also_works():
    assert resolve_org({"stockList": [{"code": CODE, "orgId": ORG}]}, CODE) == ORG


@pytest.mark.parametrize("change,error", [
    ({"secCode": "000001"}, "CNINFO_FOREIGN_SECURITY"),
    ({"orgId": "wrong-org"}, "CNINFO_FOREIGN_ORG"),
    ({"announcementTime": None}, "CNINFO_PUBLICATION_TIME_INVALID"),
    ({"announcementTime": 1723}, "CNINFO_PUBLICATION_TIME_INVALID"),
    ({"announcementTime": ms("2026-09-30T21:00:00+08:00")}, "CNINFO_PUBLICATION_OUTSIDE_WINDOW"),
    ({"adjunctUrl": "https://evil.invalid/finalpage/2026-10-03/123.PDF"}, "CNINFO_PDF_LOCATOR_INVALID"),
    ({"adjunctUrl": "../../finalpage/2026-10-03/123.PDF"}, "CNINFO_PDF_LOCATOR_INVALID"),
    ({"adjunctUrl": "http://static.cninfo.com.cn/finalpage/2026-10-03/123.PDF"}, "CNINFO_PDF_LOCATOR_INVALID"),
    ({"adjunctUrl": "finalpage/2026-10-03/123.PDF?token=secret"}, "CNINFO_PDF_LOCATOR_INVALID"),
    ({"announcementTitle": "<script>not-catalog</script>"}, "CNINFO_TITLE_MARKUP_INVALID"),
])
def test_foreign_invalid_and_unsafe_rows_never_become_sources(change, error):
    with pytest.raises(RecordError, match=error):
        normalize(envelope([row(**change)]))


def test_empty_is_only_valid_with_coherent_verified_shape_not_transport_failures():
    c = normalize(envelope([], total=0))
    assert c.total == 0 and catalog_view(c)["status"] == "complete-empty-catalog"
    with pytest.raises(RecordError, match="CNINFO_PAGE_INCOMPLETE"):
        normalize(envelope([], total=1))


def test_rejected_duplicates_counts_and_incomplete_pagination():
    with pytest.raises(RecordError, match="CNINFO_DUPLICATE_ANNOUNCEMENT"):
        normalize(envelope([row(), row()]))
    data = envelope([row("123")], total=31)
    with pytest.raises(RecordError, match="CNINFO_PAGE_INCOMPLETE"):
        normalize(data)
    first = [row(str(i)) for i in range(100, 130)]
    data = envelope(first, total=31)
    with pytest.raises(RecordError, match="CNINFO_PAGINATION_INCOMPLETE"):
        normalize(data)
    data["pages"].append({"totalAnnouncement": 32, "announcements": [row("130")]})
    with pytest.raises(RecordError, match="CNINFO_TOTAL_CHANGED"):
        normalize(data)
    data["pages"][1]["totalAnnouncement"] = 31
    assert normalize(data).total == 31


def test_budget_window_and_duplicate_batch_identity_protection():
    with pytest.raises(RecordError, match="CNINFO_DATE_WINDOW_INVALID"):
        normalize_catalog(stock_code=CODE, start_date=START, end_date="2026-12-01",
                          captured_at=CAPTURED, org_rows=envelope()["org_rows"],
                          pages=envelope()["pages"], source_mode="offline-unverified")
    with pytest.raises(RecordError, match="CNINFO_TOTAL_OR_PAGE_LIMIT"):
        normalize(envelope([], total=121))
    with pytest.raises(RecordError, match="CNINFO_PAGE_BUDGET_INVALID"):
        normalize(max_pages=99)


def test_versioned_store_exact_locator_read_and_replay(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "source.git"))
    c = normalize()
    first = save_catalog(store, c, "catalog-oct-1")
    assert first["complete"] and first["saved_count"] == 1
    ref = first["saved"][0]["reference"]
    from decision_kernel.domain.records import SourceRef
    observed = VersionedStore(GitStore(store.backend.root)).read_source(SourceRef.from_dict(ref))
    assert observed["data"] is None and observed["availability"] == "external-only"
    assert observed["original_verified"] is False
    assert observed["manifest"]["input"]["origin"].startswith("https://static.cninfo.com.cn/")
    assert store.search("2026年第三季度报告")["total"] == 1
    before = store.backend.head()
    assert save_catalog(store, c, "catalog-oct-1") == first
    assert store.backend.head() == before
    later = normalize(envelope([row(announcementTitle="2026年第三季度报告（更正）")]))
    changed = save_catalog(store, later, "catalog-oct-2")
    assert changed["complete"] and store.backend.head() != before
    assert VersionedStore(GitStore(store.backend.root)).read_source(SourceRef.from_dict(ref)) == observed
    assert store.search("更正")["total"] == 1


def test_partial_failure_retains_actual_refs_and_stops_at_original_intent(tmp_path):
    catalog = normalize(envelope([row("123"), row("124")]))
    base = VersionedStore(GitStore.init_synthetic(tmp_path / "partial.git"))

    class Failing:
        def __init__(self): self.calls = 0
        def put_source(self, *args):
            self.calls += 1
            if self.calls == 2:
                raise RecordError("REMOTE_NOT_AUTHORIZED")
            return base.put_source(*args)
        def read_source(self, ref): return base.read_source(ref)

    fake = Failing()
    result = save_catalog(fake, catalog, "catalog-partial")
    assert result["status"] == "partial-failure" and result["saved_count"] == 1
    assert result["complete"] is False and fake.calls == 2
    assert base.search("")["total"] == 1
    assert result["saved"][0]["announcement_id"] == "123"
    assert result["last_intent"].startswith("catalog-partial-")


def test_unknown_write_does_not_auto_retry_or_change_submission():
    class Unknown:
        called = 0
        def put_source(self, *args):
            self.called += 1
            raise RecordError("REMOTE_WRITE_UNCONFIRMED")
        def read_source(self, ref): raise AssertionError("must not claim readback")
    obj = Unknown()
    report = save_catalog(obj, normalize(), "catalog-unknown")
    assert obj.called == 1 and report["status"] == "unknown-write"
    assert report["saved"] == [] and not report["complete"]


def test_cli_offline_inspect_then_save_and_replay(tmp_path, capsys):
    path = tmp_path / "pages.json"
    path.write_text(json.dumps(envelope(), ensure_ascii=False), encoding="utf-8")
    common = ["--stock", CODE, "--start", START, "--end", END, "--pages", str(path)]
    assert main(common) == 0
    read_only = json.loads(capsys.readouterr().out)
    assert read_only["total"] == 1 and read_only["source_mode"] == "offline-unverified"
    git = GitStore.init_synthetic(tmp_path / "old.git")
    args = common + ["--save", "--batch-id", "approved-one", "--local-synthetic-store", str(git.root)]
    assert main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["storage"]["complete"] and result["storage"]["saved_count"] == 1
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert len([x for x in GitStore(git.root).paths(git.head()) if x.startswith("submissions/")]) == 1


def test_cli_rejects_missing_writer_and_default_readonly_binding(tmp_path, capsys):
    path = tmp_path / "pages.json"
    path.write_text(json.dumps(envelope(), ensure_ascii=False), encoding="utf-8")
    common = ["--stock", CODE, "--start", START, "--end", END, "--pages", str(path)]
    assert main(common + ["--save"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "CNINFO_SAVE_NEEDS_EXPLICIT_WRITER_AND_BATCH"
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({"repository":"auguspp/Decision-OS-2", "repository_id":1420041744,
                                   "branch":"records", "anchor":"a"*40, "store_id":"runtime",
                                   "visibility":"public", "write_enabled":False}), encoding="utf-8")
    assert main(common + ["--save", "--batch-id", "approved-two", "--binding", str(binding)]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "REMOTE_READ_ONLY"


def test_cli_rejects_broken_json_before_any_store_write(tmp_path, capsys):
    bad = tmp_path / "corrupt.json"
    bad.write_text('{"captured_at":"x","captured_at":"y"}', encoding="utf-8")
    assert main(["--stock", CODE, "--start", START, "--end", END,
                 "--pages", str(bad)]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "CNINFO_INPUT_UNREADABLE"
