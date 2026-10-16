"""Three reviewed Relay source contracts, not consensus, issuer facts or holdings.

Tushare field contracts 292/415/275 and archived Relay metadata; actual Relay
compatibility is checked separately. Original values and ambiguous identity stay visible.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ipaddress
import re
from urllib.parse import parse_qsl, urlsplit

from decision_kernel.domain.records import Entity, RecordError, SourceInput, digest, encoded, require
from decision_kernel.observations.relay_disclosure_date import ticker, iso_date
from decision_kernel.observations.relay_financial import _cell, _clock
from decision_kernel.observations import relay_financial

CAPTURE_SCHEMA = "relay-participant-capture-v1"
# Do not request report abstracts, research text or lists of individual visitors.
SPECS = {
    "report_rc": (292, "券商盈利预测", {
        **dict.fromkeys(("ts_code", "name", "report_title", "report_type", "classify", "org_name", "quarter", "rating"), "text"),
        "report_date": "date", **dict.fromkeys(("op_rt", "op_pr", "tp", "np"), "ten-thousand-yuan"),
        "eps": "yuan-per-share", "pe": "ratio", "ev_ebitda": "ratio",
        "rd": "unknown-unit", "roe": "unknown-unit", "max_price": "unknown-unit", "min_price": "unknown-unit",
    }),
    "research_report": (415, "历史研报定位", {
        **dict.fromkeys(("ts_code", "name", "title", "report_type", "inst_csname", "ind_name"), "text"),
        "trade_date": "date", "url": "locator",
    }),
    "stk_surv": (275, "机构调研记录", {
        **dict.fromkeys(("ts_code", "name", "rece_place", "rece_mode", "rece_org", "org_type"), "text"),
        "surv_date": "date",
    }),
}
REQUEST_DAY = {"report_rc": "report_date", "research_report": "trade_date", "stk_surv": "trade_date"}
ROW_DAY = {"report_rc": "report_date", "research_report": "trade_date", "stk_surv": "surv_date"}
IDENTITY_FIELDS = {
    "report_rc": ("ts_code", "report_date", "org_name", "report_title", "quarter"),
    "research_report": ("ts_code", "trade_date", "inst_csname", "title"),
    "stk_surv": ("ts_code", "surv_date", "rece_place", "rece_mode", "rece_org"),
}


@dataclass(frozen=True)
class ParticipantQuery:
    api: str
    ts_code: str
    date: str

    def __post_init__(self):
        require(isinstance(self.api, str) and self.api in SPECS, "PARTICIPANT_API_INVALID")
        ticker(self.ts_code); iso_date(self.date)

    @classmethod
    def from_dict(cls, value):
        require(isinstance(value, dict), "PARTICIPANT_QUERY_INVALID")
        try:
            return cls(**value)
        except TypeError:
            raise RecordError("PARTICIPANT_QUERY_INVALID") from None

    def parameters(self, *, offset=0, page_size=100):
        require(type(offset) is int and offset >= 0 and type(page_size) is int and 1 <= page_size <= 100,
                "PARTICIPANT_PAGE_INVALID")
        params = {"ts_code": self.ts_code, REQUEST_DAY[self.api]: self.date,
                  "fields": ",".join(SPECS[self.api][2]), "limit": str(page_size)}
        if offset:
            params["offset"] = str(offset)
        return params

    @property
    def key(self):
        return digest(encoded(asdict(self)))


def validate_request(api, parameters):
    """Validate before the shared transport can send the existing secret."""
    require(api in SPECS and isinstance(parameters, dict), "PARTICIPANT_REQUEST_INVALID")
    required = {"ts_code", REQUEST_DAY[api], "fields", "limit"}
    require(required <= parameters.keys() <= required | {"offset"}, "PARTICIPANT_REQUEST_INVALID")
    require(all(isinstance(v, str) for v in parameters.values()), "PARTICIPANT_REQUEST_INVALID")
    require(re.fullmatch(r"[1-9][0-9]{0,2}", parameters["limit"]) and
            re.fullmatch(r"0|[1-9][0-9]{0,8}", parameters.get("offset", "0")), "PARTICIPANT_PAGE_INVALID")
    q = ParticipantQuery(api, parameters["ts_code"], parameters[REQUEST_DAY[api]])
    require(parameters == q.parameters(offset=int(parameters.get("offset", "0")),
                                      page_size=int(parameters["limit"])), "PARTICIPANT_REQUEST_INVALID")


def build_plan(codes, dates, apis=None):
    apis = tuple(SPECS) if apis is None else apis
    for values in (codes, dates, apis):
        require(isinstance(values, (tuple, list)) and values and all(isinstance(v, str) for v in values)
                and len(values) == len(set(values)), "PARTICIPANT_PLAN_INVALID")
    return tuple(ParticipantQuery(api, code, day) for code in codes for day in dates for api in apis)


def _locator(value):
    # Retain a locator only. Never follow it; deny credential-bearing/private targets.
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        require(url.scheme == "https" and host and url.username is None and url.password is None
                and url.port in (None, 443) and not url.fragment and host.isascii()
                and "." in host and not host.endswith((".local", ".internal", ".localhost")), "PARTICIPANT_LOCATOR_UNSAFE")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        require(address is None or address.is_global, "PARTICIPANT_LOCATOR_UNSAFE")
        sensitive = ("token", "secret", "password", "signature", "authorization", "api_key", "apikey")
        require(not any(k.lower() in {"key", "sig", "sign", "auth"} or any(s in k.lower() for s in sensitive)
                        for k, _ in parse_qsl(url.query, keep_blank_values=True)), "PARTICIPANT_LOCATOR_UNSAFE")
    except ValueError:
        raise RecordError("PARTICIPANT_LOCATOR_UNSAFE") from None



def metadata_envelope(body):
    """Project envelope metadata only; retain all control flags and the unmodified data table.

    Unknown root values may contain text or secrets. Do not archive them, even if
    the data rows are useful. Do not silently drop unknown data columns instead.
    """
    require(isinstance(body, dict), "PARTICIPANT_ENVELOPE_INVALID")
    control = {"data", "code", "api_name", "count", "ok", "success", "error"}
    result = {k: v for k, v in body.items() if k in control}
    for key in ("request_id", "provider", "source"):
        value = body.get(key)
        if key in body and (value is None or isinstance(value, str) and len(value) <= 256):
            result[key] = value
    # Free-form success prose is not needed to interpret a structured response.
    for key in ("msg", "message"):
        if key in body and body[key] in (None, "", "ok", "OK", "success"):
            result[key] = body[key]
    return result, {"schema": "relay-metadata-envelope-projection-v1",
                    "omitted_root_fields": len(body) - len(result),
                    "original_body_retained": False}


def normalize_page(query, body, *, captured_at, page_size=100, offset=0):
    require(isinstance(query, ParticipantQuery), "PARTICIPANT_QUERY_INVALID")
    query.parameters(offset=offset, page_size=page_size)
    day = _clock(captured_at).date().isoformat()
    require(isinstance(body, dict) and type(body.get("code")) is int and body["code"] == 0
            and body.get("api_name") in (None, query.api) and body.get("ok", True) is True
            and body.get("success", True) is True and body.get("error") in (None, ""), "PARTICIPANT_SOURCE_FAILED")
    data = body.get("data")
    require(isinstance(data, dict) and set(data) <= {"fields", "items"}, "PARTICIPANT_DATA_INVALID")
    fields, items = data.get("fields"), data.get("items")
    spec, date_field = SPECS[query.api][2], ROW_DAY[query.api]
    # file_name is in the official historical-report example but absent from its field table.
    allowed = set(spec) | ({"file_name"} if query.api == "research_report" else set())
    require(isinstance(fields, list) and all(isinstance(f, str) for f in fields)
            and len(fields) == len(set(fields)) and {"ts_code", date_field} <= set(fields)
            and set(fields) <= allowed, "PARTICIPANT_FIELDS_INVALID")
    require(isinstance(items, list) and len(items) <= page_size, "PARTICIPANT_ROWS_INVALID")
    count = body.get("count")
    require(count is None or type(count) is int and count >= len(items), "PARTICIPANT_COUNT_INVALID")
    encoded(body)
    # An unrequested full text or credential-like envelope must not get archived as metadata.
    require(set(body) <= {"data", "code", "msg", "message", "ok", "success", "error", "api_name", "count",
                         "request_id", "provider", "source"}, "PARTICIPANT_ENVELOPE_INVALID")
    for key in ("msg", "message", "request_id", "provider", "source"):
        require(body.get(key) is None or isinstance(body[key], str) and len(body[key]) <= 256,
                "PARTICIPANT_ENVELOPE_INVALID")
    rows = []
    for i, values in enumerate(items):
        require(isinstance(values, list) and len(values) == len(fields), "PARTICIPANT_ROW_WIDTH_INVALID")
        raw = dict(zip(fields, values))
        require(raw["ts_code"] == query.ts_code and raw[date_field] == query.date, "PARTICIPANT_SCOPE_MISMATCH")
        cells = {}
        for name in dict.fromkeys((*spec, *fields)):
            unit, value = spec.get(name, "text"), raw.get(name)
            cell = _cell(value, "currency/share" if unit == "yuan-per-share" else unit, name in raw)
            cell["unit"] = unit
            if cell["presence"] == "value" and unit in ("text", "locator"):
                require(isinstance(value, str), "PARTICIPANT_TEXT_INVALID")
                if unit == "locator":
                    _locator(value)
                    cell["fetched"] = False
            cells[name] = cell
        present = all(isinstance(raw.get(k), str) and raw[k].strip() for k in IDENTITY_FIELDS[query.api])
        identity = digest(encoded([query.api, [raw[k] for k in IDENTITY_FIELDS[query.api]]])) if present else None
        issues = ["SOURCE_DAY_AFTER_CAPTURE"] if iso_date(query.date) > day else []
        if query.api == "stk_surv":
            issues.append("PUBLICATION_DATE_NOT_SUPPLIED_BY_CONTRACT")
        rows.append({"source_row": offset + i, "row_sha256": digest(encoded(raw)), "cells": cells,
                     "grouping_key": identity, "identity_qualification": "CANDIDATE_COMPOSITE_NOT_SOURCE_UNIQUE_ID",
                     "historical_available_at": "NOT_ESTABLISHED", "temporal_issues": issues})
    return {"offset": offset, "captured_at": captured_at, "rows": rows, "row_count": len(rows),
            "reported_count": count, "parsed_response_sha256": digest(encoded(body)),
            "missing_requested_fields": [k for k in spec if k not in fields]}


def inspect_capture(capture):
    view = relay_financial.inspect_capture(capture, query_type=ParticipantQuery, schema=CAPTURE_SCHEMA,
                                          normalizer=normalize_page)
    for original, page in zip(capture["pages"], view["pages"]):
        projection = original.get("response_projection")
        if projection is not None:
            require(isinstance(projection, dict) and set(projection) ==
                    {"schema", "omitted_root_fields", "original_body_retained"} and
                    projection["schema"] == "relay-metadata-envelope-projection-v1" and
                    type(projection["omitted_root_fields"]) is int and projection["omitted_root_fields"] >= 0 and
                    projection["original_body_retained"] is False, "PARTICIPANT_PROJECTION_INVALID")
            page["response_projection"] = dict(projection)
    view.update(source_interpretation={"report_rc": "SELL_SIDE_ESTIMATES_NOT_ISSUER_ACTUALS_OR_CONSENSUS",
        "research_report": "REPORT_METADATA_NOT_FETCHED_PDF", "stk_surv": "PARTICIPANT_ROWS_NOT_EVENT_COUNT_OR_HOLDINGS"}[view["query"]["api"]],
        pdf_read=False, event_count=None)
    return view


def source_for_page(capture, inspection, page_index):
    q = ParticipantQuery.from_dict(inspection["query"])
    page = inspection["pages"][page_index]
    entity = Entity("security-" + q.ts_code.replace(".", "-").lower(), "security", q.ts_code)
    text = encoded({"schema": "relay-participant-excerpt-v1", "query": asdict(q), "page": page,
                    "source_mode": inspection["source_mode"], "coverage": inspection["coverage"],
                    "source_interpretation": inspection["source_interpretation"]}).decode()
    return SourceInput("relay-part-" + q.key[:32] + "-p" + str(page["offset"]), "content",
        SPECS[q.api][1] + " " + q.ts_code + " " + q.date, "https://pcd.mobcvb.cn/tushare/pro/" + q.api,
        text, custody="excerpt", entities=(entity,), acquired_at=page["captured_at"],
        published_at=iso_date(q.date) if q.api != "stk_surv" else None,
        limitations=("THIRD_PARTY_RELAY_NOT_ISSUER_OR_OFFICIAL_TUSHARE",
            "DERIVED_FIELDS_NOT_ORIGINAL_HTTP_BYTES; CAPTURE_DECLARATIONS_NOT_AUTHENTICATION",
            inspection["source_interpretation"], "UNITS_FROM_FIELD_CONTRACT_NOT_INDEPENDENTLY_VERIFIED",
            "GROUPS_NOT_VERIFIED_REPORT_OR_EVENT_IDENTITIES; DUPLICATE_VERSIONS_RETAINED",
            "NO_PIT_OR_PUBLICATION_RIGHTS_CERTIFICATION; NO_URL_FETCH_OR_AUTOMATIC_RESEARCH",
            "COVERAGE: " + inspection["coverage"], "SOURCE_MODE: " + inspection["source_mode"]))
