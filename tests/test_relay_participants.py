"""Three source contracts via existing batch/storage; synthetic bodies, no live traffic."""
from copy import deepcopy
from dataclasses import asdict
from functools import partial
import json

import pytest

from decision_kernel.application.relay_financial_batch import capture_batch, save_capture
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_financial import capture_query
from decision_kernel.observations import relay_participants as sources

TIME = "2026-10-16T00:00:00+00:00"


def query(api="report_rc", **changes):
    return sources.ParticipantQuery(**{"api": api, "ts_code": "000001.SZ", "date": "20261015", **changes})


def body(q=None, rows=None):
    q = q or query()
    values = {"ts_code": q.ts_code, sources.ROW_DAY[q.api]: q.date,
        "name": "合成公司", "org_name": "合成机构甲", "quarter": "2027Q4",
        "report_title": "合成盈利预测", "eps": "1.2300", "np": "-123.4500", "pe": 15,
        "rece_org": "合成机构甲", "rece_place": "会议室", "rece_mode": "1", "org_type": "基金",
        "inst_csname": "合成机构甲", "title": "合成报告", "url": "https://example.org/report/a.pdf"}
    row = {k: values.get(k) for k in sources.SPECS[q.api][2]}
    rows = [row] if rows is None else rows
    fields = list(dict.fromkeys([*sources.SPECS[q.api][2], *(k for row in rows for k in row)]))
    return {"code": 0, "api_name": q.api, "data": {"fields": fields,
        "items": [[r.get(k) for k in fields] for r in rows]}, "count": len(rows)}


def capture(q=None, payload=None, size=100):
    q = q or query()
    return {"schema": sources.CAPTURE_SCHEMA, "query": asdict(q), "page_size": size,
        "source_mode": "offline-unverified", "error": None,
        "pages": [{"offset": 0, "captured_at": TIME, "body": payload or body(q), "transport": None}]}


def parsed(q=None, payload=None):
    q = q or query()
    return sources.normalize_page(q, payload or body(q), captured_at=TIME)


def fetch(q, **kwargs):
    return capture_query(q, query_type=sources.ParticipantQuery, schema=sources.CAPTURE_SCHEMA,
                         normalizer=sources.normalize_page, failure_details=True, **kwargs)


def save(c, factory, batch="batch-a", **kwargs):
    return save_capture(c, factory, batch, inspector=sources.inspect_capture,
                        source_builder=sources.source_for_page, **kwargs)


class Client:
    last_response_meta = None
    last_error_meta = {"http_status": 504, "classification": "temporary-gateway"}
    def __init__(self, replies): self.replies, self.calls = iter(replies), []
    def request(self, api, params):
        self.calls.append((api, params))
        response = next(self.replies)
        if isinstance(response, Exception): raise response
        return response


@pytest.mark.parametrize("api,request_day,row_day", [
    ("report_rc", "report_date", "report_date"), ("research_report", "trade_date", "trade_date"),
    ("stk_surv", "trade_date", "surv_date")])
def test_three_sources_preserve_distinct_date_scope(api, request_day, row_day):
    q = query(api)
    params = q.parameters()
    assert params[request_day] == q.date and "period" not in params
    assert row_day in params["fields"].split(",")
    assert sources.inspect_capture(capture(q))["request_complete"]
    sources.validate_request(api, params)
    assert "abstr" not in params["fields"] and "fund_visitors" not in params["fields"]


def test_batch_supports_explicit_many_codes_dates_without_quota():
    plan = sources.build_plan([f"{i:06}.SZ" for i in range(150)], ["20261001", "20261015"])
    assert len(plan) == len({q.key for q in plan}) == 900


@pytest.mark.parametrize("changes", [{"api":"p_get"}, {"api":"daily"}, {"date":"20260230"},
    {"date":"2026-10-15"}, {"ts_code":"../token"}, {"ts_code":"AAPL.US"}])
def test_bad_queries_fail_before_http(changes):
    with pytest.raises(RecordError): query(**changes)


@pytest.mark.parametrize("changes", [{"__probe":"1"}, {"fields":"ts_code,abstr"},
    {"limit":"101"}, {"offset":"-1"}, {"offset":"1e2"}, {"period":"20260630"}, {"limit":True}])
def test_request_shape_fields_and_paging_are_fixed(changes):
    with pytest.raises(RecordError): sources.validate_request("report_rc", {**query().parameters(), **changes})


def test_estimate_target_period_and_units_are_not_actuals_or_consensus():
    row = parsed()["rows"][0]
    assert row["cells"]["np"] == {"presence":"value", "raw":"-123.4500",
        "unit":"ten-thousand-yuan", "numeric_text":"-123.4500"}
    assert row["cells"]["eps"]["unit"] == "yuan-per-share"
    assert row["cells"]["quarter"]["raw"] == "2027Q4"
    assert row["historical_available_at"] == "NOT_ESTABLISHED"
    assert "NOT_ISSUER_ACTUALS" in sources.inspect_capture(capture())["source_interpretation"]


def test_missing_null_empty_zero_remain_distinct():
    p = body(); fields, values = p["data"]["fields"], p["data"]["items"][0]
    values[fields.index("op_rt")] = 0
    values[fields.index("rating")] = ""
    i = fields.index("roe"); del fields[i]; del values[i]
    cells = parsed(payload=p)["rows"][0]["cells"]
    assert cells["op_rt"]["presence"] == "value" and cells["op_rt"]["raw"] == 0
    assert cells["op_pr"]["presence"] == "null" and cells["rating"]["presence"] == "empty"
    assert cells["roe"]["presence"] == "missing"


def test_same_report_estimate_revision_not_silently_latest():
    p = body(); row = dict(zip(p["data"]["fields"], p["data"]["items"][0]))
    p = body(rows=[row, {**row,"np":"100.00"}, row])
    view = sources.inspect_capture(capture(payload=p))
    assert (view["row_count"], view["distinct_row_versions"], view["duplicate_rows"]) == (3,2,1)
    assert len({r["grouping_key"] for r in view["pages"][0]["rows"]}) == 1


def test_visitors_are_not_events_holdings_or_publication_dates():
    q=query("stk_surv"); p=body(q); row=dict(zip(p["data"]["fields"],p["data"]["items"][0]))
    c=capture(q, body(q,[row,{**row,"rece_org":"合成机构乙"},row]))
    v=sources.inspect_capture(c)
    assert v["row_count"] == 3 and v["event_count"] is None
    assert len({r["grouping_key"] for r in v["pages"][0]["rows"]}) == 2
    source=sources.source_for_page(c,v,0)
    assert source.published_at is None and source.report_period is None
    assert "PUBLICATION_DATE_NOT_SUPPLIED" in source.text


def test_missing_institution_never_gets_a_fabricated_identity():
    p=body(); f=p["data"]["fields"]; p["data"]["items"][0][f.index("org_name")]=None
    assert parsed(payload=p)["rows"][0]["grouping_key"] is None


@pytest.mark.parametrize("url", ["http://example.org/a", "https://u:p@example.org/a", "https://127.0.0.1/a",
    "https://10.1.1.1/a", "file:///tmp/a", "https://example.org/a?token=secret", "https://example.org/a?sig=abc"])
def test_private_or_credential_urls_are_not_stored_as_public_metadata(url):
    q=query("research_report"); p=body(q); p["data"]["items"][0][p["data"]["fields"].index("url")]=url
    with pytest.raises(RecordError): parsed(q,p)


def test_valid_locator_is_only_a_locator_and_optional_file_name_unclassified():
    q=query("research_report"); p=body(q)
    p["data"]["fields"].append("file_name"); p["data"]["items"][0].append("example.pdf")
    row=parsed(q,p)["rows"][0]
    assert row["cells"]["url"]["fetched"] is False
    assert not sources.inspect_capture(capture(q,p))["pdf_read"]


@pytest.mark.parametrize("api,extra", [("research_report","abstr"), ("research_report","author"),
    ("stk_surv","fund_visitors"), ("stk_surv","content"), ("report_rc","author_name")])
def test_unrequested_text_and_personal_columns_rejected(api,extra):
    q=query(api); p=body(q)
    p["data"]["fields"].append(extra); p["data"]["items"][0].append("unrequested")
    with pytest.raises(RecordError,match="FIELDS_INVALID"): parsed(q,p)


@pytest.mark.parametrize("mutation", ["error", "wrong-api", "wrong-security", "wrong-date", "duplicate-column", "bool-number", "extra-envelope", "nested-data"])
def test_invalid_source_does_not_become_empty_or_qualify_for_save(mutation):
    p=body(); f=p["data"]["fields"]; r=p["data"]["items"][0]
    if mutation=="error": p["code"]=False
    if mutation=="wrong-api": p["api_name"]="daily"
    if mutation=="wrong-security": r[f.index("ts_code")]="600519.SH"
    if mutation=="wrong-date": r[f.index("report_date")]="20261014"
    if mutation=="duplicate-column": f[-1]=f[0]
    if mutation=="bool-number": r[f.index("eps")]=True
    if mutation=="extra-envelope": p["password"]="not-allowed"
    if mutation=="nested-data": p["data"]["secret"]="not-allowed"
    with pytest.raises(RecordError): parsed(payload=p)


def test_page_termination_repeat_and_partial_failure_use_existing_capture():
    client=Client([body(),body(rows=[])])
    c=fetch(query(), transport=client, page_size=1, clock=lambda:TIME)
    assert sources.inspect_capture(c)["request_complete"] and client.calls[1][1]["offset"]=="1"
    c=fetch(query(), transport=Client([body(),body()]), page_size=1, clock=lambda:TIME)
    assert c["error"]=="FINANCIAL_PAGINATION_STALLED"
    client=Client([body(),RecordError("RELAY_HTTP_504")])
    c=fetch(query(),transport=client,page_size=1,clock=lambda:TIME)
    assert len(c["pages"])==1 and c["failure"]["classification"]=="temporary-gateway"
    assert not sources.inspect_capture(c)["request_complete"]


def test_rate_failure_stops_shared_batch_not_just_one_api():
    calls=[]
    def load(q):
        calls.append(q); return fetch(q,transport=Client([RecordError("RELAY_HTTP_429")]))
    plan=sources.build_plan(["000001.SZ"],["20261015"])
    values=list(capture_batch(plan,load,query_type=sources.ParticipantQuery,inspector=sources.inspect_capture))
    assert len(calls)==1 and [v["status"] for v in values]==["capture-incomplete","not-attempted","not-attempted"]


@pytest.mark.parametrize("api", tuple(sources.SPECS))
def test_real_synthetic_git_roundtrip_and_old_version_read(api,tmp_path):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/'s.git'))
    c=capture(query(api))
    out=save(c,lambda:store,public_rights_confirmed=True)
    assert out["complete"]
    ref=SourceRef.from_dict(out["saved"][0]["reference"])
    read=VersionedStore(GitStore(tmp_path/'s.git')).read_source(ref)
    assert read["availability"]=="stored-representation" and not read["original_verified"]
    assert store.search("000001.SZ")["total"]==1
    assert save(c,lambda:store,public_rights_confirmed=True)==out
    newer=deepcopy(c); newer["pages"][0]["captured_at"]="2026-10-17T00:00:00+00:00"
    assert save(newer,lambda:store,public_rights_confirmed=True)["status"]=="save-failed"
    assert save(newer,lambda:store,"new-version",public_rights_confirmed=True)["complete"]
    assert store.read_source(ref)["manifest"]["input"]["acquired_at"]==TIME


def test_rights_and_partial_capture_refused_before_writer():
    def writer(): pytest.fail("writer opened")
    with pytest.raises(RecordError,match="PUBLIC_RIGHTS"): save(capture(),writer)
    c=capture();c["error"]="RELAY_HTTP_500"
    with pytest.raises(RecordError,match="INCOMPLETE_CAPTURE"): save(c,writer,public_rights_confirmed=True)


def test_unknown_write_and_readback_failure_preserve_intent_and_reference(tmp_path,monkeypatch):
    class Unknown:
        def put_source(self,*a):raise UnknownWrite("REMOTE_WRITE_UNCONFIRMED")
    out=save(capture(),lambda:Unknown(),public_rights_confirmed=True)
    assert out["status"]=="unknown-write" and out["last_intent"].startswith("batch-a-")
    store=VersionedStore(GitStore.init_synthetic(tmp_path/'s.git'))
    read=store.read_source;calls=[]
    def fail(ref):
        calls.append(ref)
        if len(calls)>1:raise RecordError("TEMPORARY_READ_FAILURE")
        return read(ref)
    monkeypatch.setattr(store,"read_source",fail)
    out=save(capture(),lambda:store,public_rights_confirmed=True)
    assert out["status"]=="saved-reference-unverified" and out["pending_reference"]
    assert read(SourceRef.from_dict(out["pending_reference"]))["data"]
