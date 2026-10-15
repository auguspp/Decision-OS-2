"""Interpret third-party Tushare Relay *schedule rows*, never issuer notices.

Legacy reviewed fields from decision-kernel@cb3bbc87 / 2026-09-29 source
receipts. Missing modify_date, explicit null and a date are three different facts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import re
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import RecordError, SourceInput, require, source_time

API_NAME="disclosure_date"
FIELDS=("ts_code","ann_date","end_date","pre_date","actual_date","modify_date")
MANDATORY=("ts_code","ann_date","end_date","pre_date","actual_date")
MAX_ROWS=100
_TICKER=re.compile(r"[0-9]{6}\.(?:SH|SZ|BJ)\Z")
_DAY=re.compile(r"[0-9]{8}\Z")
_ZONE=ZoneInfo("Asia/Shanghai")


def ticker(value: str) -> str:
    require(isinstance(value,str) and _TICKER.fullmatch(value) is not None,
            "RELAY_DISCLOSURE_TICKER_INVALID")
    return value


def iso_date(value: str)->str:
    require(isinstance(value,str) and _DAY.fullmatch(value) is not None,"RELAY_DATE_INVALID")
    try:
        when=date(int(value[:4]),int(value[4:6]),int(value[6:]))
    except ValueError as exc:
        raise RecordError("RELAY_DATE_INVALID") from exc
    return when.isoformat()


def report_period(value: str) -> str:
    period = iso_date(value)
    require(period[5:] in ("03-31", "06-30", "09-30", "12-31"),
            "RELAY_REPORT_PERIOD_INVALID")
    return period


def _date_field(field: str, raw: object, present: bool) -> dict:
    if not present:
        return {"presence":"missing","value":None}
    if raw is None or raw=="":
        return {"presence":"null","value":None}
    require(isinstance(raw,str),"RELAY_DATE_FIELD_INVALID")
    if field == "modify_date" and not _DAY.fullmatch(raw):
        # The official field is a revision RECORD, not a guaranteed single date.
        require(0 < len(raw) <= 1024 and not any(ord(c) < 32 or ord(c) == 127 for c in raw),
                "RELAY_DATE_FIELD_INVALID")
        return {"presence": "value", "value": raw,
                "interpretation": "unparsed-revision-record"}
    return {"presence":"value","value":iso_date(raw)}


@dataclass(frozen=True)
class ScheduleRow:
    ticker: str
    report_period: str
    source_ann_date: str
    original_fields: tuple[str,...]
    pre_date: dict
    actual_date: dict
    modify_date: dict
    source_row_index: int
    row_hash: str
    temporal_issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScheduleSnapshot:
    ticker: str
    report_period: str
    captured_at: str
    source_mode: str
    observed_count: int
    requested_limit: int
    rows: tuple[ScheduleRow,...]
    response_request_id: str | None = None
    parsed_response_sha256: str = ""
    response_transport: dict | None = None
    provider_identity: str = "THIRD_PARTY_TUSHARE_RELAY_NOT_ISSUER_OR_OFFICIAL_TUSHARE"


def _encoded(value: object)->bytes:
    try:
        return (json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False,
                           allow_nan=False)+"\n").encode("utf-8")
    except (TypeError,ValueError,UnicodeError,RecursionError) as exc:
        raise RecordError("RELAY_ROW_ENCODING_INVALID") from exc


def normalize_disclosure_dates(*, ts_code: str, end_date: str, raw: object,
                               captured_at: str, source_mode: str,
                               limit: int = MAX_ROWS, response_transport: dict | None = None) -> ScheduleSnapshot:
    ticker(ts_code)
    period=report_period(end_date)
    require(type(limit) is int and 1<=limit<=MAX_ROWS,"RELAY_ROW_BUDGET_INVALID")
    source_time(captured_at)
    require(isinstance(captured_at,str) and "T" in captured_at and
            datetime.fromisoformat(captured_at.replace("Z","+00:00")).utcoffset() is not None,
            "RELAY_ACQUIRED_CLOCK_INVALID")
    require(source_mode in ("offline-unverified","live-relay"),"RELAY_SOURCE_MODE_INVALID")
    require(isinstance(raw,dict) and raw.get("api_name") in (None,API_NAME)
            and type(raw.get("code")) is int and raw["code"]==0
            and raw.get("ok", True) is True and raw.get("success", True) is True
            and raw.get("error") in (None,""),
            "RELAY_SOURCE_RESPONSE_NOT_SUCCESS")
    data=raw.get("data")
    require(isinstance(data,dict),"RELAY_ROWS_INVALID")
    fields,items=data.get("fields"),data.get("items")
    require(isinstance(fields,list) and fields and len(fields)<=64
            and all(isinstance(s,str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,63}",s) for s in fields)
            and len(fields)==len(set(fields)) and set(fields)>=set(MANDATORY),
            "RELAY_COLUMNS_INVALID")
    require(isinstance(items,list) and len(items)<=limit,"RELAY_ROWS_LIMIT_EXCEEDED")
    require(type(raw.get("count")) is int and raw["count"]==len(items),"RELAY_COUNT_MISMATCH")
    if raw["count"]==limit:
        # Count==caller limit might mean the provider cut off more rows; do not claim completeness.
        raise RecordError("RELAY_POSSIBLY_TRUNCATED_AT_LIMIT")
    request_id = raw.get("request_id")
    require(request_id is None or isinstance(request_id,str) and len(request_id)<=128
            and (not request_id or re.fullmatch(r"[A-Za-z0-9._-]+",request_id) is not None),
            "RELAY_REQUEST_ID_INVALID")
    if response_transport is not None:
        require(source_mode == "live-relay" and isinstance(response_transport,dict) and
                set(response_transport)=={"http_status","x_request_id","x_cache","wire_sha256"}
                and type(response_transport["http_status"]) is int and response_transport["http_status"]==200
                and isinstance(response_transport["wire_sha256"], str)
                and re.fullmatch(r"[0-9a-f]{64}",response_transport["wire_sha256"]) is not None,
                "RELAY_RESPONSE_RECEIPT_INVALID")
        for key in ("x_request_id","x_cache"):
            value=response_transport[key]
            require(value is None or isinstance(value,str) and
                    re.fullmatch(r"[A-Za-z0-9._-]{1,128}",value) is not None,
                    "RELAY_RESPONSE_RECEIPT_INVALID")
    capture_day = datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(_ZONE).date().isoformat()
    parsed=[]
    for ix,values in enumerate(items):
        require(isinstance(values,list) and len(values)==len(fields),"RELAY_ROW_WIDTH_INVALID")
        row=dict(zip(fields,values))
        require(row["ts_code"]==ts_code,"RELAY_FOREIGN_TICKER")
        require(row["end_date"]==end_date,"RELAY_WRONG_REPORT_PERIOD")
        ann=iso_date(row["ann_date"])
        # No assumption that announced date is later than the period-end: pre-announcements exist.
        pre=_date_field("pre_date",row["pre_date"],True)
        actual=_date_field("actual_date",row["actual_date"],True)
        modify=_date_field("modify_date",row.get("modify_date"),"modify_date" in row)
        issues = []
        if ann > capture_day:
            issues.append("ANNOUNCEMENT_AFTER_CAPTURE")
        if actual["presence"] == "value" and actual["value"] > capture_day:
            issues.append("ACTUAL_DISCLOSURE_AFTER_CAPTURE")
        if actual["presence"] == "value" and actual["value"] < period:
            issues.append("ACTUAL_DISCLOSURE_BEFORE_REPORT_PERIOD_END")
        parsed.append(ScheduleRow(ts_code,period,ann,tuple(fields),pre,actual,modify,
                                  ix,sha256(_encoded(row)).hexdigest(),tuple(issues)))
    return ScheduleSnapshot(ts_code,period,captured_at,source_mode,len(items),limit,
                            tuple(parsed),request_id,sha256(_encoded(raw)).hexdigest(),
                            response_transport)


def view(snapshot: ScheduleSnapshot)->dict:
    return {"api_name":API_NAME,"ticker":snapshot.ticker,
            "report_period":snapshot.report_period,"captured_at":snapshot.captured_at,
            "source_mode":snapshot.source_mode,"observed_count":snapshot.observed_count,
            "source_identity":snapshot.provider_identity,"issuer_pdf_read":False,
            "response_request_id":snapshot.response_request_id,
            "parsed_response_sha256":snapshot.parsed_response_sha256,
            "response_transport":snapshot.response_transport,
            "completeness":"BOUNDED_RELAY_REQUEST_ONLY_NOT_HISTORY_OR_EXCHANGE_VALIDATED",
            "rows":[asdict(z) for z in snapshot.rows]}


def source_for(snapshot: ScheduleSnapshot)->SourceInput:
    """Derived bounded fields only; caller must independently authorize public storage."""
    code=ticker(snapshot.ticker)
    period=snapshot.report_period.replace("-","")
    source_id="relay-disclosure-"+code.replace(".","-").lower()+"-"+period
    rows=[asdict(r) for r in snapshot.rows]
    text=_encoded({"schema":"relay-disclosure-date-v1","rows":rows,
                   "response_request_id":snapshot.response_request_id,
                   "parsed_response_sha256":snapshot.parsed_response_sha256,
                   "response_transport":snapshot.response_transport,
                   "scope":{"ts_code":snapshot.ticker,"end_date":period,"limit":snapshot.requested_limit}}).decode("utf-8")
    limitations=(
        "RELAY_DERIVED_SCHEDULE_NOT_ISSUER_NOTICE_OR_OFFICIAL_TUSHARE",
        "SOURCE_MODE: "+snapshot.source_mode,
        "REQUEST_SCOPE: ticker="+snapshot.ticker+" period="+snapshot.report_period+
          " limit="+str(snapshot.requested_limit),
        "MISSING_VS_NULL_MODIFY_DATE_ARE_NOT_PROOF_OF_NO_REVISION",
        "ACTUAL_DATE_NOT_AN_ACTUAL_PDF_RECEIPT; HISTORICAL_AVAILABLE_AT_UNKNOWN",
        "ZERO_ITEMS_NOT_PROOF_OF_NO_FUTURE_SCHEDULE" if snapshot.observed_count == 0 else "RELAY_SNAPSHOT_ONLY_NOT_COMPLETE_CHANGE_HISTORY",
        "PUBLICATION_RIGHTS_MUST_BE_CONFIRMED_BY_WRITER",
        "DERIVED_FIELDS_ONLY_NOT_ORIGINAL_HTTP_BODY_OR_EXTERNAL_PDF",
        "TEMPORAL_ISSUES_RETAINED_NOT_QUALIFIED_AS_OCCURRED; MODIFY_RECORD_NOT_ALWAYS_A_SINGLE_DATE",
    )
    return SourceInput(source_id=source_id,kind="content",title="Relay 财报预约/实际披露日期 "+code+" "+period,
                       origin="https://pcd.mobcvb.cn/tushare/pro/disclosure_date",text=text,
                       custody="excerpt",entities=(),acquired_at=snapshot.captured_at,
                       report_period=snapshot.report_period,limitations=limitations)
