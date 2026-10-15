"""Eight source contracts, business semantics, pagination and real synthetic Git consumer."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application.relay_financial_batch import capture_batch, save_capture
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, Conflict
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_financial import capture_query
from decision_kernel.observations.relay_financial import (
    CAPTURE_SCHEMA, SPECS, FinancialQuery, build_plan, inspect_capture, normalize_page,
)

TIME = "2026-10-15T08:00:00+00:00"


def query(api="income", **changes):
    values = dict(api=api, ts_code="000001.SZ", period="20260630")
    if api == "fina_mainbz": values["dimension"] = "P"
    values.update(changes)
    return FinancialQuery(**values)


def body(q=None, rows=None):
    q = q or query()
    values = dict(ts_code=q.ts_code, end_date=q.period, ann_date="20260828", report_type="1")
    contract = SPECS[q.api][2]
    row = {field: values.get(field, None) for field in contract}
    if "revenue" in row: row["revenue"] = 9876543210.25
    if "bz_item" in row: row.update(bz_item="产品甲", bz_code="P", curr_type="CNY", bz_sales=500)
    rows = [row] if rows is None else rows
    fields = list(dict.fromkeys([*contract, *(key for r in rows for key in r)]))
    return {"api_name": q.api, "code": 0, "data": {"fields": fields,
            "items": [[r.get(k) for k in fields] for r in rows]}, "count": len(rows)}


def capture(q=None, payload=None, size=100):
    q = q or query()
    return {"schema": CAPTURE_SCHEMA, "query": asdict(q), "page_size": size,
            "source_mode": "offline-unverified", "error": None,
            "pages": [{"offset": 0, "captured_at": TIME, "body": payload or body(q), "transport": None}]}


@pytest.mark.parametrize("api", tuple(SPECS))
def test_all_eight_endpoints_have_native_period_fields_and_readable_results(api):
    q = query(api)
    params = q.parameters()
    assert params["period"] == "20260630" and "end_date" not in params
    assert params["ts_code"] == "000001.SZ"
    assert set(params["fields"].split(",")) == set(SPECS[api][2])
    if api == "fina_mainbz": assert params["type"] == "P"
    view = inspect_capture(capture(q))
    assert view["request_complete"] and view["row_count"] == 1
    assert "NOT_HISTORY_COMPLETENESS" in view["coverage"]


def test_batch_is_cross_product_not_a_small_company_or_cost_allowance():
    codes = [f"{i:06}.SZ" for i in range(200)]
    result = build_plan(codes, ["20251231", "20260331", "20260630"], dimensions=["P", "D", "I"])
    assert len(result) == 200 * 3 * 10
    assert len({q.key for q in result}) == len(result)


@pytest.mark.parametrize("changes", [dict(api="p_save"), dict(api="daily"), dict(period="20260230"),
    dict(period="20260701"), dict(ts_code="../foo"), dict(report_type="13"),
    dict(api="forecast", report_type="1"), dict(dimension="P")])
def test_invalid_scope_rejected_before_source_or_store(changes):
    with pytest.raises(RecordError): query(**changes)


def test_report_type_is_not_silently_assumed_and_parent_is_not_consolidated():
    q = query(report_type="6")
    p = body(q); fields = p["data"]["fields"]; p["data"]["items"][0][fields.index("report_type")] = "6"
    row = normalize_page(q, p, captured_at=TIME)["rows"][0]
    assert row["report_type"] == "6" and row["period_basis"] == "cumulative-report-period"
    p["data"]["items"][0][fields.index("report_type")] = "1"
    with pytest.raises(RecordError, match="REPORT_TYPE_MISMATCH"): normalize_page(q, p, captured_at=TIME)


def test_single_quarter_cumulative_and_balance_remain_distinct():
    p = body(); f = p["data"]["fields"]; p["data"]["items"][0][f.index("report_type")] = "2"
    assert normalize_page(query(), p, captured_at=TIME)["rows"][0]["period_basis"] == "single-quarter"
    b = query("balancesheet")
    assert normalize_page(b, body(b), captured_at=TIME)["rows"][0]["period_basis"] == "period-end-balance"


def test_forecast_wan_yuan_percentage_eps_and_unknown_unit_not_rescaled():
    q = query("forecast"); p = body(q); f = p["data"]["fields"]
    p["data"]["items"][0][f.index("net_profit_min")] = "123.4500"
    p["data"]["items"][0][f.index("p_change_min")] = -12.5
    row = normalize_page(q, p, captured_at=TIME)["rows"][0]
    assert row["cells"]["net_profit_min"] == {"presence": "value", "raw": "123.4500", "unit": "ten-thousand-yuan", "numeric_text": "123.4500"}
    assert row["cells"]["p_change_min"]["raw"] == -12.5 and row["period_basis"] == "issuer-forecast-not-actual"
    assert SPECS["fina_indicator"][2]["eps"] == "currency/share"
    assert SPECS["forecast"][2]["last_parent_net"] == "unknown-unit"


def test_missing_null_empty_zero_and_extra_fields_are_not_collapsed():
    p = body(); f = p["data"]["fields"]; row = p["data"]["items"][0]
    row[f.index("revenue")] = 0; row[f.index("rd_exp")] = ""
    ix = f.index("minority_gain"); del f[ix]; del row[ix]
    f.append("new_provider_column"); row.append("retained")
    cells = normalize_page(query(), p, captured_at=TIME)["rows"][0]["cells"]
    assert cells["revenue"]["raw"] == 0 and cells["revenue"]["presence"] == "value"
    assert cells["rd_exp"]["presence"] == "empty"
    assert cells["income_tax"]["presence"] == "null"
    assert cells["minority_gain"]["presence"] == "missing"
    assert cells["new_provider_column"]["raw"] == "retained"


@pytest.mark.parametrize("value", [True, "not-a-number", float("inf"), float("nan")])
def test_invalid_financial_numbers_not_coerced_to_zero(value):
    p = body(); p["data"]["items"][0][p["data"]["fields"].index("revenue")] = value
    with pytest.raises(RecordError): normalize_page(query(), p, captured_at=TIME)


def test_future_source_announcement_visible_not_used_as_historical_truth():
    p = body(); p["data"]["items"][0][p["data"]["fields"].index("ann_date")] = "20271015"
    row = normalize_page(query(), p, captured_at=TIME)["rows"][0]
    assert "ANN_DATE_AFTER_CAPTURE" in row["temporal_issues"]
    assert row["historical_available_at"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize("change", [dict(code=False), dict(code=403), dict(success=False),
    dict(api_name="daily"), dict(error="bad"), dict(count=-1)])
def test_business_errors_not_empty_success(change):
    p = body(); p.update(change)
    with pytest.raises(RecordError): normalize_page(query(), p, captured_at=TIME)


def test_same_period_revisions_and_duplicates_retained_without_picking_latest():
    q = query(); p = body(q); values = dict(zip(p["data"]["fields"], p["data"]["items"][0]))
    p = body(q, [values, {**values, "revenue": 999, "update_flag": "1"}, values])
    view = inspect_capture(capture(q, p))
    assert view["row_count"] == 3 and view["distinct_row_versions"] == 2 and view["duplicate_rows"] == 1


class Client:
    last_response_meta = None
    def __init__(self, responses): self.responses = iter(responses); self.calls = []
    def request(self, api, params):
        self.calls.append((api, params))
        reply = next(self.responses)
        if isinstance(reply, Exception): raise reply
        return reply


def test_offset_pagination_terminal_empty_and_actual_attempts():
    client = Client([body(), body(rows=[])])
    result = capture_query(query(), transport=client, page_size=1, clock=lambda: TIME)
    assert len(client.calls) == 2 and client.calls[1][1]["offset"] == "1"
    assert inspect_capture(result)["request_complete"]


def test_repeated_full_page_stops_not_infinite_or_full_coverage():
    client = Client([body(), body()])
    result = capture_query(query(), transport=client, page_size=1, clock=lambda: TIME)
    assert result["error"] == "FINANCIAL_PAGINATION_STALLED"
    assert not inspect_capture(result)["request_complete"] and len(client.calls) == 2


def test_caller_page_guard_retains_partial_data_and_no_success_claim():
    result = capture_query(query(), transport=Client([body()]), page_size=1, max_pages=1, clock=lambda: TIME)
    assert result["pages"] and result["error"] == "FINANCIAL_PAGE_BOUND_REACHED"
    assert not inspect_capture(result)["request_complete"]


def test_declared_more_rows_than_short_page_not_complete():
    p = body(); p["count"] = 20
    result = capture_query(query(), transport=Client([p]), clock=lambda: TIME)
    assert result["error"] == "FINANCIAL_COUNT_SUGGESTS_TRUNCATION"
    assert not inspect_capture(result)["request_complete"]


def test_batch_isolates_per_interface_failure_and_stops_on_shared_denial():
    plan = [query(), query("balancesheet"), query("cashflow")]
    def load(q):
        if q.api == "balancesheet": return capture_query(q, transport=Client([RecordError("RELAY_HTTP_500")]))
        return capture(q)
    out = list(capture_batch(plan, load))
    assert [x["status"] for x in out] == ["captured", "capture-incomplete", "captured"]
    count = []
    def deny(q):
        count.append(q)
        return capture_query(q, transport=Client([RecordError("RELAY_HTTP_429")]))
    out = list(capture_batch(plan, deny))
    assert len(count) == 1 and out[-1]["status"] == "not-attempted"


@pytest.mark.parametrize("api", tuple(SPECS))
def test_all_eight_save_into_existing_store_and_read_by_exact_ref(api, tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "s.git"))
    c = capture(query(api))
    receipt = save_capture(c, lambda: store, "batch-a", public_rights_confirmed=True)
    assert receipt["complete"]
    ref = SourceRef.from_dict(receipt["saved"][0]["reference"])
    view = VersionedStore(GitStore(tmp_path / "s.git")).read_source(ref)
    assert view["availability"] == "stored-representation" and not view["original_verified"]
    data = json.loads(view["data"])
    assert data["query"]["api"] == api
    assert store.search("000001.SZ")["total"] == 1
    assert save_capture(c, lambda: store, "batch-a", public_rights_confirmed=True) == receipt


def test_same_intent_changed_capture_conflicts_and_old_reference_stays_readable(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "s.git"))
    c = capture(); old = save_capture(c, lambda: store, "a", public_rights_confirmed=True)
    newer = deepcopy(c); newer["pages"][0]["captured_at"] = "2026-10-16T08:00:00+00:00"
    out = save_capture(newer, lambda: store, "a", public_rights_confirmed=True)
    assert out["error"] == "SUBMISSION_CONTENT_CONFLICT" and not out["complete"]
    new = save_capture(newer, lambda: store, "b", public_rights_confirmed=True)
    assert new["complete"]
    reference = SourceRef.from_dict(old["saved"][0]["reference"])
    assert store.read_source(reference)["manifest"]["input"]["acquired_at"] == TIME


def test_unknown_write_or_readback_failure_keeps_original_intent_and_known_ref(tmp_path, monkeypatch):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "s.git"))
    original_read = store.read_source
    reads = 0
    def missing_read(ref):
        nonlocal reads
        reads += 1
        if reads > 1: raise RecordError("TEMPORARY_READ_FAILURE")
        return original_read(ref)
    monkeypatch.setattr(store, "read_source", missing_read)
    result = save_capture(capture(), lambda: store, "once", public_rights_confirmed=True)
    assert result["status"] == "saved-reference-unverified" and result["pending_reference"]
    class Unknown:
        def put_source(self, *args): raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    result = save_capture(capture(), lambda: Unknown(), "once", public_rights_confirmed=True)
    assert result["status"] == "unknown-write" and result["last_intent"].startswith("once-")


def test_no_public_rights_or_incomplete_capture_never_opens_writer():
    def forbidden(): raise AssertionError("writer opened")
    with pytest.raises(RecordError, match="PUBLIC_RIGHTS"):
        save_capture(capture(), forbidden, "a")
    c = capture(); c["error"] = "RELAY_HTTP_500"
    with pytest.raises(RecordError, match="INCOMPLETE_CAPTURE"):
        save_capture(c, forbidden, "a", public_rights_confirmed=True)


def test_capture_forged_page_order_or_error_text_rejected():
    c = capture(); c["pages"][0]["offset"] = 1
    with pytest.raises(RecordError, match="PAGE_SEQUENCE"): inspect_capture(c)
    c = capture(); c["error"] = "arbitrary upstream private prose"
    with pytest.raises(RecordError, match="CAPTURE_ERROR"): inspect_capture(c)


def test_large_count_from_earlier_page_survives_later_empty_page():
    first = body(); first["count"] = 20
    result = capture_query(query(), transport=Client([first, body(rows=[])]), page_size=1, clock=lambda: TIME)
    assert result["error"] == "FINANCIAL_COUNT_SUGGESTS_TRUNCATION"
    assert not inspect_capture(result)["request_complete"]


@pytest.mark.parametrize("code", ["rate_limited", "ip_rate_limited", "unauthorized", "forbidden"])
def test_http200_business_denial_stops_shared_batch_without_retry(code):
    calls = []
    def loader(q):
        calls.append(q)
        return capture_query(q, transport=Client([{"code": 1, "error": code}]))
    results = list(capture_batch([query(), query("cashflow")], loader))
    assert len(calls) == 1 and results[1]["status"] == "not-attempted"


def test_batch_cannot_mislabel_a_different_query_capture():
    with pytest.raises(RecordError, match="CAPTURE_SCOPE_MISMATCH"):
        list(capture_batch([query()], lambda _: capture(query("forecast"))))


def test_actual_source_claims_are_preserved_not_promoted_to_authority():
    p = body(); p.update(request_id="req-1", provider="declared-compatibility-pool")
    value = normalize_page(query(), p, captured_at=TIME)
    assert value["source_claims"]["request_id"] == "req-1"
    assert value["source_claims"]["provider"] == "declared-compatibility-pool"


def test_offline_capture_cannot_claim_wire_receipt():
    c = capture()
    c["source_mode"] = "offline-unverified"
    c["pages"][0]["transport"] = {"http_status": 200, "x_request_id": None,
                                 "x_cache": None, "wire_sha256": "0" * 64}
    with pytest.raises(RecordError, match="FINANCIAL_TRANSPORT_RECEIPT_INVALID"):
        inspect_capture(c)
