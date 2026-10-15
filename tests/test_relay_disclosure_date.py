"""Tushare Relay 2026-09-29 reviewed *shape* reproduced with synthetic rows."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from decision_kernel.application.relay_disclosure_date import save_schedule
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError,SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.relay_disclosure_date import (
    FIELDS,normalize_disclosure_dates,source_for,view,
)

CAP="2026-10-15T10:00:00+00:00"
TICK="600598.SH"
PERIOD="20260930"


def response(*,fields=None,items=None,count=None,**changes):
    fields=list(FIELDS) if fields is None else fields
    items=[[TICK,"20260924",PERIOD,"20261029",None,None]] if items is None else items
    data={"api_name":"disclosure_date","code":0,"msg":"","data":{"fields":fields,"items":items},
          "count":len(items) if count is None else count}
    data.update(changes)
    return data


def norm(raw=None,*,limit=100,captured=CAP):
    return normalize_disclosure_dates(ts_code=TICK,end_date=PERIOD,
                                      raw=response() if raw is None else raw,
                                      captured_at=captured,source_mode="offline-unverified",limit=limit)


def test_structured_plan_is_not_execution_or_issuer_pdf():
    saved=norm(); x=view(saved)
    assert x["api_name"]=="disclosure_date" and x["ticker"]==TICK
    assert x["report_period"]=="2026-09-30"
    assert not x["issuer_pdf_read"] and x["source_mode"]=="offline-unverified"
    item=x["rows"][0]
    assert item["source_ann_date"]=="2026-09-24"
    assert x["parsed_response_sha256"] and len(x["parsed_response_sha256"])==64
    assert x["response_transport"] is None
    assert item["pre_date"]=={"presence":"value","value":"2026-10-29"}
    assert item["actual_date"]=={"presence":"null","value":None}
    assert item["modify_date"]=={"presence":"null","value":None}
    assert "NOT_ISSUER" in x["source_identity"]


def test_modify_date_missing_null_date_are_distinct():
    missing=response(fields=list(FIELDS[:-1]),items=[[TICK,"20260924",PERIOD,"20261029",None]])
    null=response()
    changed=response(items=[[TICK,"20260924",PERIOD,"20261029",None,"20261025"]])
    assert norm(missing).rows[0].modify_date["presence"]=="missing"
    assert norm(null).rows[0].modify_date["presence"]=="null"
    assert norm(changed).rows[0].modify_date=={"presence":"value","value":"2026-10-25"}
    assert len({norm(x).rows[0].row_hash for x in (missing,null,changed)})==3


def test_actual_date_does_not_auto_become_issuer_document():
    r=response(items=[[TICK,"20260924",PERIOD,"20261029","20261102",None]])
    x=norm(r)
    assert x.rows[0].actual_date["value"]=="2026-11-02"
    assert view(x)["issuer_pdf_read"] is False


def test_authorized_explicit_source_excerpt_save_and_exact_old_read(tmp_path):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"s.git"))
    c=norm()
    with pytest.raises(RecordError,match="RELAY_PUBLICATION_RIGHTS_REQUIRED"):
        save_schedule(store,c,"schedule-one")
    assert store.search("")["total"]==0
    out=save_schedule(store,c,"schedule-one",public_rights_confirmed=True)
    assert out["complete"] and not out["original_pdf_read"]
    ref=SourceRef.from_dict(out["reference"])
    reader=VersionedStore(GitStore(tmp_path/"s.git"))
    old=reader.read_source(ref)
    assert old["availability"]=="stored-representation"
    assert old["original_verified"] is False
    assert old["manifest"]["input"]["report_period"]=="2026-09-30"
    assert old["manifest"]["input"]["published_at"] is None
    assert json.loads(old["data"])["rows"][0]["modify_date"]["presence"]=="null"
    assert save_schedule(store,c,"schedule-one",public_rights_confirmed=True)==out
    updated=norm(response(items=[[TICK,"20260924",PERIOD,"20261029","20261102",None]]))
    second=save_schedule(store,updated,"schedule-two",public_rights_confirmed=True)
    assert second["reference"]!=out["reference"]
    assert json.loads(reader.read_source(ref)["data"])["rows"][0]["actual_date"]["value"] is None
    assert store.search("Relay")["total"]==2


def test_empty_success_still_not_a_global_no_events_conclusion():
    s=norm(response(items=[],count=0))
    assert s.observed_count==0
    src=source_for(s)
    assert "ZERO_ITEMS_NOT_PROOF_OF_NO_FUTURE_SCHEDULE" in src.limitations
    assert view(s)["completeness"]!="FULL_MARKET"


@pytest.mark.parametrize("code",["600598","600598.sh","abc.SH","000000.NY","600598.SH\n",None,True])
def test_ticker_type_boundaries(code):
    with pytest.raises(RecordError,match="RELAY_DISCLOSURE_TICKER_INVALID"):
        normalize_disclosure_dates(ts_code=code,end_date=PERIOD,raw=response(),captured_at=CAP,source_mode="offline-unverified")


@pytest.mark.parametrize("date",["20261301","20260230","2026-09-30","2026093",20260930,None])
def test_report_end_date_validation(date):
    with pytest.raises(RecordError,match="RELAY_DATE_INVALID"):
        normalize_disclosure_dates(ts_code=TICK,end_date=date,raw=response(),captured_at=CAP,source_mode="offline-unverified")


@pytest.mark.parametrize("change,error",[
    ({"api_name":"daily"},"RELAY_SOURCE_RESPONSE_NOT_SUCCESS"),
    ({"code":1},"RELAY_SOURCE_RESPONSE_NOT_SUCCESS"),
    ({"error":"upstream_pool_exhausted"},"RELAY_SOURCE_RESPONSE_NOT_SUCCESS"),
    ({"count":2},"RELAY_COUNT_MISMATCH"),
    ({"data":{"fields":list(FIELDS[:-1]),"items":[[TICK,"20260924",PERIOD,"20261029",None]]}},None),
])
def test_failed_relay_business_status_does_not_become_zero(change,error):
    if error is None:
        assert norm(response(**change)).rows[0].modify_date["presence"]=="missing"
    else:
        with pytest.raises(RecordError,match=error):norm(response(**change))


def test_fields_missing_required_invalid_and_duplicate():
    for fields in (["ts_code","ann_date","end_date","pre_date"],list(FIELDS[:-1])+["ts_code"],
                   ["ts_code","end_date","ann_date","pre_date","actual_date","BAD FIELD"]):
        with pytest.raises(RecordError,match="RELAY_COLUMNS_INVALID"):
            norm(response(fields=fields,items=[]))


def test_wrong_width_wrong_security_wrong_period_cannot_publish():
    bad=[
        (response(items=[[TICK,"20260924",PERIOD,"20261029",None]]),"RELAY_ROW_WIDTH_INVALID"),
        (response(items=[["000001.SZ","20260924",PERIOD,"20261029",None,None]]),"RELAY_FOREIGN_TICKER"),
        (response(items=[[TICK,"20260924","20260630","20261029",None,None]]),"RELAY_WRONG_REPORT_PERIOD"),
    ]
    for raw,error in bad:
        with pytest.raises(RecordError,match=error):norm(raw)


def test_nil_vs_bad_date_and_invalid_source_clock():
    for invalid in ["20260230","2026-10-29",0,True]:
        with pytest.raises(RecordError):norm(response(items=[[TICK,"20260924",PERIOD,invalid,None,None]]))
    with pytest.raises(RecordError):norm(captured="2026-10-15T10:00:00")


def test_exact_limit_cannot_claim_complete_when_page_at_limit():
    with pytest.raises(RecordError,match="RELAY_POSSIBLY_TRUNCATED_AT_LIMIT"):
        norm(response(),limit=1)
    with pytest.raises(RecordError,match="RELAY_ROW_BUDGET_INVALID"):
        norm(response(),limit=0)


def test_changed_source_mode_never_promotes_pdf_to_read():
    s=normalize_disclosure_dates(ts_code=TICK,end_date=PERIOD,raw=response(),captured_at=CAP,
                                 source_mode="live-relay")
    assert s.source_mode=="live-relay" and view(s)["issuer_pdf_read"] is False


def test_response_request_identity_and_parsed_hash_are_source_provenance():
    a=norm(response(request_id="9ebd01f2-raw"))
    b=norm(response(request_id="9ebd01f2-other"))
    assert view(a)["response_request_id"]=="9ebd01f2-raw"
    assert a.parsed_response_sha256 != b.parsed_response_sha256
    assert json.loads(source_for(a).text)["response_request_id"]=="9ebd01f2-raw"
    with pytest.raises(RecordError,match="RELAY_REQUEST_ID_INVALID"):
        norm(response(request_id="Authorization: secret"))


def test_transport_metadata_contract_must_be_verified_before_normalization():
    with pytest.raises(RecordError,match="RELAY_RESPONSE_RECEIPT_INVALID"):
        normalize_disclosure_dates(ts_code=TICK,end_date=PERIOD,raw=response(),captured_at=CAP,
                                   source_mode="live-relay",response_transport={"http_status":200})
