"""Bounded broker report catalog; HTTP data here is synthetic, not live source evidence."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from decision_kernel.application.broker_report_catalog import catalog_view, save_catalog
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.broker_report_catalog import (
    ReportScope, normalize_catalog, request_spec, source_for,
)


CAPTURE="2026-10-15T06:00:00+00:00"


def scope(kind="industry",**kwargs):
    return ReportScope(kind,"2026-10-01","2026-10-12",**kwargs)


def report(info="RB123",published="2026-10-07 08:00:01",**changes):
    return {"infoCode":info,"title":"PCB行业观察","publishDate":published,"orgSName":"券商甲",
            "industryName":"电子","ratingName":"增持","column":"002001002X",
            **changes}


def page(rows=None,total=None,number=1):
    rows=[report()] if rows is None else rows
    total=len(rows) if total is None else total
    return {"success":True,"code":0,"pageNo":number,"hits":total,
            "TotalPage":(total+49)//50,"data":rows}


def parsed(kind="industry", rows=None, **scope_args):
    return normalize_catalog(scope(kind,**scope_args),pages=[page(rows)],
                             captured_at=CAPTURE,source_mode="offline-unverified")


@pytest.mark.parametrize("kind,route,qtype",[
    ("industry","list","1"),("strategy","jg","2"),("macro","jg","3"),("morning","jg","4")])
def test_native_four_kind_route_and_params(kind,route,qtype):
    url,params=request_spec(scope(kind),1)
    assert url=="https://reportapi.eastmoney.com/report/"+route
    assert params["qType"]==qtype and params["pageSize"]=="50"
    assert params["beginTime"]=="2026-10-01" and params["endTime"]=="2026-10-12"
    assert params["pageNo"]=="1"


@pytest.mark.parametrize("bad",[
    {"kind":"stock"},{"begin":"2026-02-30"},{"end":"2026-08-01"},
    {"end":"2026-10-02", "begin":"2026-10-12"},{"max_pages":0},{"max_pages":5},
    {"max_pages":True},{"industry_code":"123ABC"},{"kind":"macro","industry_code":"99"},
])
def test_scope_rejects_unbounded_or_mislabeled_queries(bad):
    p=dict(kind="industry",begin="2026-10-01",end="2026-10-12")
    p.update(bad)
    with pytest.raises(RecordError):ReportScope(**p)


def test_offline_catalog_and_external_only_reference(tmp_path):
    c=parsed(rows=[report(column=None)])
    assert c.provider_rows==1 and c.provider_pages==1
    assert c.reports[0].type_status=="REQUEST_ONLY_NOT_INDEPENDENTLY_VERIFIED"
    assert c.reports[0].publication_date=="2026-10-07"
    assert c.reports[0].publication_raw.endswith("08:00:01")
    assert catalog_view(c,keyword="pcb")["matched"]==1
    assert catalog_view(c,keyword="没有")["matched"]==0
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"original.git"))
    receipt=save_catalog(store,c,"broker-batch-a")
    assert receipt["complete"] and receipt["saved_count"]==1
    raw_ref=receipt["saved"][0]["reference"]
    from decision_kernel.domain.records import SourceRef
    ref=SourceRef.from_dict(raw_ref)
    view=VersionedStore(GitStore(tmp_path/"original.git")).read_source(ref)
    assert view["data"] is None and view["availability"]=="external-only"
    assert view["original_verified"] is False
    assert view["manifest"]["input"]["published_at"]=="2026-10-07"
    assert "HISTORICAL_AVAILABLE_AT_UNKNOWN" in " ".join(view["limitations"])
    assert view["manifest"]["input"]["origin"].startswith("https://data.eastmoney.com/report/")
    assert save_catalog(store,c,"broker-batch-a")==receipt
    assert store.search("PCB")["total"]==1


def test_same_provider_id_new_version_retains_old_exact_reference(tmp_path):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"s.git"))
    a=parsed(rows=[report(column=None,title="原版")]);b=parsed(rows=[report(column=None,title="更新版")])
    old=save_catalog(store,a,"batch-first")
    new=save_catalog(store,b,"batch-next")
    assert old["complete"] and new["complete"]
    assert store.search("更新版")["total"]==1 and store.search("原版")["total"]==1
    from decision_kernel.domain.records import SourceRef
    ref1=SourceRef.from_dict(old["saved"][0]["reference"])
    ref2=SourceRef.from_dict(new["saved"][0]["reference"])
    assert ref1.manifest.commit!=ref2.manifest.commit
    assert store.read_source(ref1)["manifest"]["input"]["title"]=="原版"


def test_identity_prefers_info_code_else_opaque_encoded_token():
    first=parsed(rows=[report(column=None,infoCode="A01",encodeUrl="ENC")])
    second=parsed(rows=[report(column=None,infoCode=None,encodeUrl="ENC")])
    assert first.reports[0].identity_kind=="infoCode"
    assert second.reports[0].identity_kind=="encodeUrl"
    assert first.reports[0].report_key!=second.reports[0].report_key
    assert "encodeUrl=ENC" in second.reports[0].detail_url


def test_duplicate_native_row_preserved_as_reference_location():
    sample=report(column=None)
    rows=[sample for _ in range(51)]
    p1=page(rows[:50],total=51,number=1)
    p2=page(rows[50:],total=51,number=2)
    c=normalize_catalog(scope(),pages=[p1,p2],captured_at=CAPTURE,source_mode="offline-unverified")
    assert c.provider_rows==51 and c.duplicate_rows==50
    assert len(c.reports)==1 and c.reports[0].page_numbers[0]==1
    assert c.reports[0].page_numbers[-1]==2


def test_same_report_id_different_row_versions_preserved():
    data=[report(column=None,title="A") for _ in range(50)]+[report(column=None,title="B")]
    c=normalize_catalog(scope(),pages=[page(data[:50],51,1),page(data[50:],51,2)],
                        captured_at=CAPTURE,source_mode="offline-unverified")
    assert c.provider_rows==51 and len(c.reports)==2
    assert {r.title for r in c.reports}=={"A","B"}


def test_type_conflict_visible_but_not_savable(tmp_path):
    c=parsed("strategy",rows=[report(column="00200100100")])
    assert c.reports[0].type_status=="COLUMN_CONFLICT"
    assert catalog_view(c)["excluded_type_conflict"]==1
    assert catalog_view(c)["matched"]==0
    s=VersionedStore(GitStore.init_synthetic(tmp_path/"s.git"))
    with pytest.raises(RecordError,match="BROKER_CONFLICTING_TYPE_CANNOT_SAVE"):
        save_catalog(s,c,"blocked")
    assert s.search("")["total"]==0


def test_kind_unverified_not_mislabeled_as_source_confirmed():
    c=parsed("morning",rows=[report(column=None)])
    x=source_for(c.reports[0],c)
    assert x.custody=="external" and x.text is None
    assert any("REQUEST_ONLY_NOT_INDEPENDENTLY_VERIFIED" in z for z in x.limitations)


def test_empty_window_is_explicit_not_source_failure(tmp_path):
    empty=parsed(rows=[])
    assert empty.provider_rows==0 and empty.provider_pages==1
    assert catalog_view(empty)["status"]=="provider-empty-result"
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"s.git"))
    assert save_catalog(store,empty,"empty")["saved_count"]==0
    assert store.search("")["total"]==0


@pytest.mark.parametrize("change,error",[
    ({"hits":0},"BROKER_PAGE_INCOMPLETE"),({"pageNo":2},"BROKER_COUNTER_INVALID"),
    ({"TotalPage":2},"BROKER_COUNTER_MISMATCH"),({"code":403},"BROKER_PROVIDER_FAILURE"),
    ({"data":[]},"BROKER_PAGE_INCOMPLETE"),({"success":False},"BROKER_PROVIDER_FAILURE"),
    ({"hits":251,"TotalPage":6},"BROKER_PAGE_BUDGET_EXCEEDED"),
])
def test_incoherent_provider_page_never_becomes_empty_catalog(change,error):
    p=page();p.update(change)
    with pytest.raises(RecordError,match=error):
        normalize_catalog(scope(),pages=[p],captured_at=CAPTURE,source_mode="offline-unverified")


@pytest.mark.parametrize("row,error",[
    ({"title":None},"BROKER_TITLE_REQUIRED"),
    ({"infoCode":None,"encodeUrl":None},"BROKER_REPORT_ID_REQUIRED"),
    ({"infoCode":"bad&"},"BROKER_REPORT_ID_INVALID"),
    ({"publishDate":"2026-10-12 00:99:01"},"BROKER_PUBLICATION_DATE_INVALID"),
    ({"publishDate":"2026-10-18"},"BROKER_PUBLICATION_OUTSIDE_WINDOW"),
    ({"encodeUrl":"?h=1"},"BROKER_ENCODED_URL_INVALID"),
    ({"title":"bad\x00text"},"BROKER_FIELD_INVALID"),
])
def test_corrupt_report_row_rejected(row,error):
    values=report(**row)
    with pytest.raises(RecordError,match=error):parsed(rows=[values])


def test_missing_page_is_not_a_completed_catalog():
    many=[report(info=str(i),column=None) for i in range(51)]
    p=page(many[:50],51,1)
    with pytest.raises(RecordError,match="BROKER_CATALOG_INCOMPLETE"):
        normalize_catalog(scope(),pages=[p],captured_at=CAPTURE,source_mode="offline-unverified")


def test_requested_pages_must_cover_reported_total():
    with pytest.raises(RecordError,match="BROKER_PAGE_BUDGET_EXCEEDED"):
        normalize_catalog(scope(max_pages=1),pages=[page([report(info=str(i),column=None) for i in range(50)],total=51)],
                          captured_at=CAPTURE,source_mode="offline-unverified")


def test_industry_code_wrong_provider_row_rejected():
    with pytest.raises(RecordError,match="BROKER_INDUSTRY_FILTER_MISMATCH"):
        parsed(rows=[report(industryCode="998")],industry_code="111")


def test_query_is_not_source_acquisition_or_keyword_crawl():
    c=parsed(rows=[report()])
    assert catalog_view(c,keyword="券商甲")["matched"]==1
    assert catalog_view(c,keyword="电子")["matched"]==1
    with pytest.raises(RecordError):catalog_view(c,keyword="x"*300)


def test_partial_write_preserves_known_receipts(tmp_path,monkeypatch):
    c=parsed(rows=[report(info="A",column=None),report(info="B",column=None)])
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"partial.git"))
    initial=store.put_source
    count=0
    def put(*args):
        nonlocal count
        count+=1
        if count==2:raise RecordError("REMOTE_WRITE_UNCONFIRMED")
        return initial(*args)
    monkeypatch.setattr(store,"put_source",put)
    out=save_catalog(store,c,"series")
    assert not out["complete"] and out["status"]=="unknown-write"
    assert len(out["saved"])==1 and out["saved_count"]==1
    assert out["last_intent"].startswith("series-")
    assert store.search("")["total"]==1


def test_external_reference_does_not_infer_pdf_authentication():
    c=parsed(rows=[report(column=None)])
    source=source_for(c.reports[0],c)
    assert source.origin.startswith("https://data.eastmoney.com/report/")
    assert source.text is None and source.published_at=="2026-10-07"
    assert any("ORIGINAL_PDF_NOT_READ" in z for z in source.limitations)
    assert not any("TUSHARE" in z for z in source.limitations)


def test_known_oversized_save_batch_refused_before_any_side_effect(tmp_path):
    c=parsed(rows=[report(info=f"unique-{n}",column=None) for n in range(13)])
    store=VersionedStore(GitStore.init_synthetic(tmp_path/"bounded.git"))
    before=store.backend.head()
    with pytest.raises(RecordError,match="BROKER_SAVE_BATCH_BUDGET_EXCEEDED"):
        save_catalog(store,c,"oversized")
    assert store.backend.head()==before
    assert store.search("")["total"]==0
