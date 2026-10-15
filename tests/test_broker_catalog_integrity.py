"""Protect catalog completeness, source attribution, and failure receipts; no live I/O."""
from dataclasses import asdict
from http.client import IncompleteRead
import json
from urllib.parse import urlencode

import pytest

from decision_kernel.application.broker_report_catalog import catalog_view, save_catalog
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, UnknownWrite
from decision_kernel.infrastructure.broker_report_catalog import EastmoneyReportTransport, fetch_catalog
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.broker_report_catalog import ReportScope, normalize_catalog, request_spec
from test_broker_report_catalog import CAPTURE, page, parsed, report, scope
from test_broker_report_catalog_transport import Opener, Reply


def test_provider_pages_do_not_certify_unique_window_completeness():
    rows = [report(title="original") for _ in range(50)] + [report(title="revision")]
    catalog = normalize_catalog(scope(), pages=[page(rows[:50], 51, 1), page(rows[50:], 51, 2)],
                                captured_at=CAPTURE, source_mode="offline-unverified")
    view = catalog_view(catalog)
    assert view["coverage"] == "PROVIDER_PAGES_READ_NOT_CORPUS_COMPLETENESS"
    assert view["window_completeness"] == "NOT_ESTABLISHED"
    assert view["status"] == "provider-pages-read"
    assert view["unique_reports"] == 1 and view["versions"] == 2
    assert view["duplicate_or_variant_rows"] == 50 and view["provider_rows"] == 51
    empty = catalog_view(parsed(rows=[]))
    assert empty["status"] == "provider-empty-result"
    assert empty["window_completeness"] == "NOT_ESTABLISHED"


def test_stored_locator_preserves_broker_and_industry_search(tmp_path):
    root = tmp_path / "store.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    catalog = parsed(rows=[report(orgCode="BKR-42", industryName="电子元件")])
    result = save_catalog(store, catalog, "source-attribution")
    assert result["complete"]
    reader = VersionedStore(GitStore(root))
    ref = SourceRef.from_dict(result["saved"][0]["reference"])
    saved = reader.read_source(ref)
    assert saved["data"] is None and saved["original_verified"] is False
    for keyword in ("券商甲", "BKR-42", "电子元件", "增持"):
        assert reader.search(keyword)["total"] == 1
    assert any("RATING_RAW_NOT_RECOMMENDATION" in item for item in saved["limitations"])


@pytest.mark.parametrize("change,error", [
    ({"code": 403}, "BROKER_PROVIDER_FAILURE"),
    ({"success": False}, "BROKER_PROVIDER_FAILURE"),
    ({"pageNo": 2}, "BROKER_COUNTER_INVALID"),
    ({"data": []}, "BROKER_PAGE_INCOMPLETE"),
])
def test_invalid_first_page_stops_before_another_request(change, error):
    first = page([report(info=str(i)) for i in range(50)], 51, 1)
    first.update(change)
    calls = []
    def transport(spec):
        calls.append(spec)
        assert len(calls) == 1, "source continued after unqualified page"
        return first
    with pytest.raises(RecordError, match=error):
        fetch_catalog(scope(), transport=transport)
    assert len(calls) == 1


def test_bad_row_stops_before_later_pages():
    rows = [report(info=str(i)) for i in range(50)]
    rows[0]["title"] = None
    calls = []
    def transport(spec):
        calls.append(spec)
        assert len(calls) == 1, "source continued after bad row"
        return page(rows, 51, 1)
    with pytest.raises(RecordError, match="BROKER_TITLE_REQUIRED"):
        fetch_catalog(scope(), transport=transport)


def test_denominator_drift_stops_before_third_request():
    replies = [page([report(info=str(i)) for i in range(50)], 101, 1),
               page([report(info=str(i)) for i in range(50, 100)], 151, 2)]
    calls = []
    def transport(spec):
        calls.append(spec)
        assert len(calls) <= 2, "source continued after denominator drift"
        return replies[len(calls)-1]
    with pytest.raises(RecordError, match="BROKER_TOTAL_DRIFT"):
        fetch_catalog(scope(), transport=transport)
    assert len(calls) == 2


def test_future_window_fails_before_source_io():
    future = ReportScope("industry", "9999-10-01", "9999-10-12")
    def denied(*args):
        pytest.fail("future window contacted provider")
    with pytest.raises(RecordError, match="BROKER_FUTURE_WINDOW"):
        fetch_catalog(future, transport=denied)


def test_typed_unknown_write_does_not_depend_on_error_text(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "store.git"))
    calls = []
    def uncertain(*args):
        calls.append(args)
        raise UnknownWrite("LOST_RECEIPT")
    monkeypatch.setattr(store, "put_source", uncertain)
    result = save_catalog(store, parsed(), "unknown-intent")
    assert result["status"] == "unknown-write" and not result["complete"]
    assert result["pending_reference"] is None and len(calls) == 1
    assert result["last_intent"].startswith("unknown-intent-")


def test_reference_survives_extra_readback_failure(tmp_path, monkeypatch):
    root = tmp_path / "store.git"
    store = VersionedStore(GitStore.init_synthetic(root))
    original = store.read_source
    calls = 0
    def read(ref):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StoreUnavailable("READBACK_TEMPORARILY_UNAVAILABLE")
        return original(ref)
    monkeypatch.setattr(store, "read_source", read)
    result = save_catalog(store, parsed(), "preserve-receipt")
    assert not result["complete"] and result["status"] == "readback-unconfirmed"
    assert result["saved_count"] == 0
    ref = SourceRef.from_dict(result["pending_reference"])
    reader = VersionedStore(GitStore(root))
    assert reader.read_source(ref)["availability"] == "external-only"
    before = reader.backend.head()
    replay = save_catalog(reader, parsed(), "preserve-receipt")
    assert replay["complete"] and replay["saved"][0]["reference"] == asdict(ref)
    assert reader.backend.head() == before


@pytest.mark.parametrize("length", ["1", "9999", "not-a-number"])
def test_declared_body_length_mismatch_is_a_source_failure(length):
    spec = request_spec(scope(), 1)
    url = spec[0] + "?" + urlencode(spec[1])
    reply = Reply(json.dumps(page([])).encode(), url,
                  headers={"Content-Type": "application/json", "Content-Length": length})
    with pytest.raises(RecordError, match="BROKER_HTTP_LENGTH_INVALID"):
        EastmoneyReportTransport(opener=Opener(reply))(spec)
    assert reply.closed


def test_incomplete_stream_is_bounded_and_does_not_expose_partial_body():
    spec = request_spec(scope(), 1)
    url = spec[0] + "?" + urlencode(spec[1])
    class Broken(Reply):
        def read(self, *args):
            raise IncompleteRead(b"private-partial-data", 100)
    reply = Broken(b"", url)
    with pytest.raises(RecordError, match="BROKER_HTTP_UNAVAILABLE") as caught:
        EastmoneyReportTransport(opener=Opener(reply))(spec)
    assert "private" not in str(caught.value) and reply.closed
