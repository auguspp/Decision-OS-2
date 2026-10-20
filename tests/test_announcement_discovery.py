"""Synthetic boundary tests; not historical discovery or source availability claims."""
from copy import deepcopy
from datetime import datetime
from http.client import IncompleteRead
import json
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from decision_kernel.domain.records import RecordError
from decision_kernel.observations.announcement_discovery import (
    TERMS, assemble, compare_versions, normalize_query, normalize_row, query_form, window,
)
from decision_kernel.infrastructure.announcement_discovery import capture

START, END, AT = "2026-10-16", "2026-10-19", "2026-10-19T18:20:00+08:00"


def row(n=1, code="600999", title="测试公司前三季度业绩预告"):
    return {"secCode": code, "orgId": "org-test", "secName": "测试公司", "announcementId": str(n),
        "announcementTitle": title, "announcementTime": int(datetime(2026, 10, 16, 18, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()*1000),
        "adjunctUrl": "finalpage/2026-10-16/test-"+str(n)+".pdf"}


def page(total, rows):
    return {"totalAnnouncement": total, "announcements": rows}


@pytest.mark.parametrize("term", TERMS)
def test_every_family_is_unseeded(term):
    form = query_form(term, START, END, 1)
    assert form["stock"] == form["secid"] == ""
    assert form["searchkey"] == term
    assert "600487" not in str(form)


@pytest.mark.parametrize("start,end", [(END, START), ("2026-10-01", END), ("bad", END), (START, "2026-02-30")])
def test_bad_window(start, end):
    with pytest.raises(RecordError):
        window(start, end)


@pytest.mark.parametrize("n", [0, -1, 5, 1000, True, "1"])
def test_page_bound(n):
    with pytest.raises(RecordError):
        query_form(TERMS[0], START, END, n)


@pytest.mark.parametrize("code", ["600999", "000999", "300999", "688999", "920999"])
def test_no_exchange_or_stock_whitelist(code):
    assert normalize_row(row(code=code), START, END)["stock_code"] == code


@pytest.mark.parametrize("field,value,reason", [
    ("secCode", "x", "CNINFO_STOCK_CODE_REQUIRED"), ("secCode", None, "CNINFO_STOCK_CODE_REQUIRED"),
    ("orgId", "../x", "DISCOVERY_ID_INVALID"), ("announcementId", "../x", "DISCOVERY_ID_INVALID"),
    ("announcementTime", 1, "CNINFO_PUBLICATION_TIME_INVALID"),
    ("announcementTime", True, "CNINFO_PUBLICATION_TIME_INVALID"),
    ("announcementTitle", "<script>x</script>", "CNINFO_TITLE_MARKUP_INVALID"),
    ("adjunctUrl", "https://evil.example/a.pdf", "CNINFO_PDF_LOCATOR_INVALID"),
    ("adjunctUrl", "finalpage/2026-10-16/../x.pdf", "CNINFO_PDF_LOCATOR_INVALID")])
def test_invalid_row_is_gap_not_empty_success(field, value, reason):
    raw = row(); raw[field] = value
    result = normalize_query(TERMS[0], [page(1, [raw])], start=START, end=END)
    assert not result["complete_query"] and result["rejected_rows"] == 1
    assert result["rejected_row_details"] == [{"page_number": 1, "row_number": 1, "reason_code": reason}]


def test_highlight_removed_without_executing_html():
    assert normalize_row(row(title="<em>业绩</em>增长"), START, END)["title"] == "业绩增长"


def test_partial_pages_keep_good_rows():
    result = normalize_query(TERMS[0], [page(1000, [row(n) for n in range(30)])], start=START, end=END)
    assert len(result["entries"]) == 30 and not result["complete_query"]
    assert "PAGINATION_INCOMPLETE" in result["gaps"]


def test_total_change_and_duplicates_cannot_certify_complete():
    pages = [page(31, [row(n) for n in range(30)]), page(30, [row(0)])]
    result = normalize_query(TERMS[0], pages, start=START, end=END)
    assert "TOTAL_CHANGED_DURING_PAGING" in result["gaps"]
    assert "DUPLICATE_ROWS_WITHIN_QUERY" in result["gaps"]
    assert not result["complete_query"]


@pytest.mark.parametrize("empty", [[], None])
def test_actual_empty_query_not_market_quiet(empty):
    def transport(url, form):
        return page(0, empty)
    doc = capture(start=START, end=END, transport_factory=lambda: transport, clock=lambda: AT)
    assert doc["all_declared_queries_complete"] and doc["unique_versions"] == 0
    assert not doc["complete_market_coverage"] and not doc["research_executed"]


def test_maximum_four_pages_per_family_28_total():
    calls = []
    def transport(url, form):
        calls.append(form)
        start = (int(form["pageNum"])-1)*30
        return page(500, [row(n) for n in range(start, start+30)])
    doc = capture(start=START, end=END, transport_factory=lambda: transport, clock=lambda: AT)
    assert len(calls) == 28 and doc["unique_versions"] == 120
    assert not doc["all_declared_queries_complete"]


@pytest.mark.parametrize("code", ["401", "403", "429"])
def test_refusal_stops_all_other_keywords(code):
    calls = []
    def transport(url, form):
        calls.append(form)
        raise RecordError("CNINFO_HTTP_"+code)
    doc = capture(start=START, end=END, transport_factory=lambda: transport, clock=lambda: AT)
    assert len(calls) == 1 and not doc["all_declared_queries_complete"]
    assert all(q["gaps"] == ["NOT_ATTEMPTED_AFTER_SOURCE_REFUSAL"] for q in doc["queries"][1:])


def test_cross_query_dedup_and_capture_clock_not_event_increment():
    queries = [normalize_query(t, [page(1, [row()])], start=START, end=END) for t in TERMS]
    prior = assemble(queries, start=START, end=END, acquired_at=AT)
    current = deepcopy(prior); current["acquired_at"] = "2026-10-20T18:20:00+08:00"
    assert prior["unique_versions"] == 1 and len(prior["entries"][0]["matched_terms"]) == 7
    assert compare_versions(current, prior)[0]["increment"] == "same-version"
    assert compare_versions(current)[0]["increment"] == "baseline-unknown"
    current["entries"][0] = {**normalize_row(row(title="测试公司业绩预告更正"), START, END), "matched_terms": ["更正"]}
    assert compare_versions(current, prior)[0]["increment"] == "changed-metadata-version"


def test_partial_transport_does_not_delete_earlier_pages():
    def factory():
        def transport(url, form):
            if form["pageNum"] == "2":
                raise RecordError("CNINFO_TRANSPORT_UNAVAILABLE")
            return page(31, [row(n) for n in range(30)])
        return transport
    doc = capture(start=START, end=END, transport_factory=factory, clock=lambda: AT)
    assert doc["unique_versions"] == 30
    assert all(q["pages_read"] == 1 and q["status"] == "partial" for q in doc["queries"])


def test_rejection_positions_retain_original_pages_and_good_rows_without_raw_content():
    first = [row(n) for n in range(30)]
    first[3] = row(3, title="<script>private-rejected-title</script>")
    second = [row(30, code="private-rejected-code"), row(31)]
    result = normalize_query(TERMS[0], [page(32, first), page(32, second)], start=START, end=END)
    assert result["rows_read"] == 32 and result["rejected_rows"] == 2
    assert len(result["entries"]) == 30 and not result["complete_query"]
    assert result["rejected_row_details"] == [
        {"page_number": 1, "row_number": 4, "reason_code": "CNINFO_TITLE_MARKUP_INVALID"},
        {"page_number": 2, "row_number": 1, "reason_code": "CNINFO_STOCK_CODE_REQUIRED"},
    ]
    assert "private-rejected" not in json.dumps(result)
    assert result["gaps"] == ["INVALID_ROWS_RETAINED_AS_GAP"]


def test_rejection_details_cover_only_the_existing_four_by_thirty_scope():
    pages = [page(120, [None] * 30) for _ in range(4)]
    result = normalize_query(TERMS[0], pages, start=START, end=END)
    assert result["rows_read"] == result["rejected_rows"] == len(result["rejected_row_details"]) == 120
    assert result["rejected_row_details"][-1] == {
        "page_number": 4, "row_number": 30, "reason_code": "DISCOVERY_ROW_INVALID"}
    assert not result["complete_query"] and result["entries"] == []


def test_unrecognized_exception_message_cannot_become_source_diagnostic(monkeypatch):
    def reject(*args):
        raise RecordError("private-error-message")
    monkeypatch.setattr("decision_kernel.observations.announcement_discovery.normalize_row", reject)
    result = normalize_query(TERMS[0], [page(1, [row()])], start=START, end=END)
    assert result["rejected_row_details"][0]["reason_code"] == "DISCOVERY_ROW_INVALID"
    assert "private-error-message" not in json.dumps(result)


def test_old_snapshot_without_row_diagnostics_remains_a_frozen_comparison_input():
    queries = [normalize_query(t, [page(1, [row()])], start=START, end=END) for t in TERMS]
    current = assemble(queries, start=START, end=END, acquired_at=AT)
    prior = deepcopy(current)
    for query in prior["queries"]:
        del query["rejected_row_details"]
    frozen_prior = deepcopy(prior)
    assert compare_versions(current, prior)[0]["increment"] == "same-version"
    assert prior == frozen_prior
    assert current["entries"] == prior["entries"]
    assert all(q["rejected_row_details"] == [] for q in current["queries"])


def test_partial_cninfo_http_body_preserves_prior_pages_in_normal_discovery():
    from decision_kernel.infrastructure.cninfo_catalog import ANNOUNCEMENTS, OfficialCatalogTransport
    from test_cninfo_catalog_transport import FakeResponse

    calls, responses = [], []
    class BrokenResponse(FakeResponse):
        def read(self, limit):
            raise IncompleteRead(b"private-source-fragment", 100)
    class Opener:
        def open(self, request, timeout):
            form = parse_qs(request.data.decode())
            calls.append(form)
            if form["pageNum"] == ["1"]:
                response = FakeResponse(ANNOUNCEMENTS, json.dumps(page(31, [row(n) for n in range(30)])).encode())
            else:
                response = BrokenResponse(ANNOUNCEMENTS)
            responses.append(response)
            return response
    doc = capture(start=START, end=END,
        transport_factory=lambda: OfficialCatalogTransport(opener=Opener()), clock=lambda: AT)
    assert len(calls) == doc["requests_attempted"] == 14  # Two original pages per keyword, no retry.
    assert not doc["source_hold"] and not doc["all_declared_queries_complete"]
    assert doc["unique_versions"] == 30 and all(response.closed for response in responses)
    assert all(q["pages_read"] == 1 and q["rows_read"] == 30 and q["status"] == "partial"
        and "CNINFO_TRANSPORT_UNAVAILABLE" in q["gaps"] for q in doc["queries"])
    assert "private-source-fragment" not in json.dumps(doc)
