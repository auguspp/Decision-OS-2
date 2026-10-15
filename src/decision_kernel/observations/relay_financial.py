"""Eight financial source contracts; no valuation, accounting synthesis, or PIT certification.

Official field references: Tushare docs 33/36/44/79/45/46/80/81. The actual
transport remains the user's third-party Relay, whose live compatibility is separate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import Entity, RecordError, SourceInput, digest, encoded, require, source_time
from decision_kernel.observations.relay_disclosure_date import ticker, report_period, iso_date

# Units describe the referenced field contract, not independent confirmation of Relay units.
COMMON = {"ts_code": "identity", "ann_date": "date", "end_date": "date"}
STATEMENT = {**COMMON, "f_ann_date": "date", "report_type": "code", "comp_type": "code", "update_flag": "code"}
SPECS = {
    "income": (33, "利润表", {**STATEMENT, "basic_eps": "currency/share", "diluted_eps": "currency/share",
        **dict.fromkeys(("total_revenue", "revenue", "oper_cost", "sell_exp", "admin_exp", "fin_exp", "rd_exp",
                        "operate_profit", "total_profit", "income_tax", "n_income", "n_income_attr_p", "minority_gain"), "currency-unit")}),
    "balancesheet": (36, "资产负债表", {**STATEMENT, "total_share": "shares",
        **dict.fromkeys(("money_cap", "accounts_receiv", "inventories", "total_cur_assets", "fix_assets", "total_assets",
                        "st_borr", "lt_borr", "total_cur_liab", "total_liab", "total_hldr_eqy_exc_min_int", "minority_int"), "currency-unit")}),
    "cashflow": (44, "现金流量表", {**STATEMENT,
        **dict.fromkeys(("net_profit", "c_fr_sale_sg", "c_paid_goods_s", "c_paid_to_for_empl", "c_pay_acq_const_fiolta",
                        "n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act", "n_incr_cash_cash_equ", "c_cash_equ_end_period"), "currency-unit")}),
    "fina_indicator": (79, "财务指标", {**COMMON, "update_flag": "code", "eps": "currency/share", "bps": "currency/share",
        "ocfps": "currency/share", "current_ratio": "ratio", "quick_ratio": "ratio", "profit_dedt": "currency-unit",
        **dict.fromkeys(("roe", "roa", "grossprofit_margin", "netprofit_margin", "debt_to_assets", "netprofit_yoy", "or_yoy"), "percent")}),
    "forecast": (45, "业绩预告", {**COMMON, "type": "text", "p_change_min": "percent", "p_change_max": "percent",
        "net_profit_min": "ten-thousand-yuan", "net_profit_max": "ten-thousand-yuan",
        "last_parent_net": "unknown-unit", "first_ann_date": "date", "summary": "text", "change_reason": "text"}),
    "express": (46, "业绩快报", {**COMMON,
        **dict.fromkeys(("revenue", "operate_profit", "total_profit", "n_income", "total_assets", "total_hldr_eqy_exc_min_int"), "yuan"),
        "diluted_eps": "currency/share", "diluted_roe": "percent", "bps": "currency/share", "is_audit": "code", "remark": "text"}),
    "fina_audit": (80, "审计意见", {**COMMON, "audit_result": "text", "audit_fees": "yuan", "audit_agency": "text", "audit_sign": "text"}),
    "fina_mainbz": (81, "主营构成", {"ts_code": "identity", "end_date": "date", "bz_item": "text", "bz_code": "code",
        "bz_sales": "yuan", "bz_profit": "yuan", "bz_cost": "yuan", "curr_type": "currency", "update_flag": "code"}),
}
NUMERIC_UNITS = {"currency/share", "currency-unit", "shares", "ratio", "percent", "ten-thousand-yuan", "yuan", "unknown-unit"}
CAPTURE_SCHEMA = "relay-financial-capture-v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FinancialQuery:
    api: str
    ts_code: str
    period: str
    report_type: str | None = None
    dimension: str | None = None

    def __post_init__(self):
        require(isinstance(self.api, str) and self.api in SPECS, "FINANCIAL_API_INVALID")
        ticker(self.ts_code); report_period(self.period)
        require(self.report_type is None or self.api in ("income", "balancesheet", "cashflow")
                and isinstance(self.report_type, str) and self.report_type in {str(n) for n in range(1, 13)}, "FINANCIAL_REPORT_TYPE_INVALID")
        require(self.dimension is None or self.api == "fina_mainbz" and self.dimension in ("P", "D", "I"), "FINANCIAL_DIMENSION_INVALID")
        require(self.api != "fina_mainbz" or self.dimension is not None, "FINANCIAL_DIMENSION_REQUIRED")

    @classmethod
    def from_dict(cls, obj):
        require(isinstance(obj, dict), "FINANCIAL_QUERY_INVALID")
        try:
            return cls(**obj)
        except TypeError:
            raise RecordError("FINANCIAL_QUERY_INVALID") from None

    def parameters(self, *, offset=0, page_size=100):
        require(type(offset) is int and offset >= 0 and type(page_size) is int and 1 <= page_size <= 100,
                "FINANCIAL_PAGE_INVALID")
        params = {"ts_code": self.ts_code, "period": self.period,
                  "fields": ",".join(SPECS[self.api][2]), "limit": str(page_size)}
        if offset:
            params["offset"] = str(offset)
        if self.report_type is not None:
            params["report_type"] = self.report_type
        if self.dimension is not None:
            params["type"] = self.dimension
        return params

    @property
    def key(self):
        return digest(encoded(asdict(self)))


def build_plan(codes, periods, apis=None, *, dimensions=("P",), report_types=(None,)):
    """Explicit cross-product, not a fixed company quota or hidden all-market query."""
    apis = tuple(SPECS) if apis is None else apis
    for values in (codes, periods, apis, dimensions, report_types):
        require(isinstance(values, (list, tuple)) and bool(values) and all(v is None or isinstance(v, str) for v in values)
                and len(set(values)) == len(values), "FINANCIAL_PLAN_VALUES_INVALID")
    queries = []
    for code in codes:
        for period in periods:
            for api in apis:
                for rt in report_types if api in ("income", "balancesheet", "cashflow") else (None,):
                    for dimension in dimensions if api == "fina_mainbz" else (None,):
                        queries.append(FinancialQuery(api, code, period, rt, dimension))
    return tuple(queries)


def _clock(value):
    source_time(value)
    require(isinstance(value, str) and "T" in value, "FINANCIAL_CAPTURE_TIME_INVALID")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(SHANGHAI)


def _cell(value, unit, present):
    if not present:
        return {"presence": "missing", "raw": None, "unit": unit}
    if value is None or value == "":
        return {"presence": "null" if value is None else "empty", "raw": value, "unit": unit}
    require(type(value) in (str, int, float, bool), "FINANCIAL_CELL_SHAPE_INVALID")
    if isinstance(value, str):
        require(len(value.encode("utf-8")) <= 8192 and "\0" not in value, "FINANCIAL_CELL_TEXT_INVALID")
    out = {"presence": "value", "raw": value, "unit": unit}
    if unit == "date":
        out["iso"] = iso_date(value)
    elif unit in NUMERIC_UNITS:
        require(type(value) is not bool, "FINANCIAL_NUMERIC_INVALID")
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            raise RecordError("FINANCIAL_NUMERIC_INVALID") from None
        require(number.is_finite(), "FINANCIAL_NUMERIC_INVALID")
        # Do not round, turn percentages into ratios, scale money, or calculate single quarters.
        out["numeric_text"] = str(number)
    return out


def normalize_page(query: FinancialQuery, body: object, *, captured_at: str, page_size=100, offset=0):
    query.parameters(page_size=page_size, offset=offset)
    capture_day = _clock(captured_at).date().isoformat()
    require(isinstance(body, dict) and type(body.get("code")) is int and body["code"] == 0
            and body.get("api_name") in (None, query.api) and body.get("ok", True) is True
            and body.get("success", True) is True and body.get("error") in (None, ""), "FINANCIAL_SOURCE_FAILED")
    data = body.get("data")
    require(isinstance(data, dict), "FINANCIAL_DATA_INVALID")
    fields, items = data.get("fields"), data.get("items")
    require(isinstance(fields, list) and 2 <= len(fields) <= 512
            and all(isinstance(f, str) and re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", f) for f in fields)
            and len(fields) == len(set(fields)) and {"ts_code", "end_date"} <= set(fields), "FINANCIAL_FIELDS_INVALID")
    require(isinstance(items, list) and len(items) <= page_size, "FINANCIAL_ROWS_INVALID")
    count = body.get("count")
    require(count is None or type(count) is int and count >= len(items), "FINANCIAL_COUNT_INVALID")
    encoded(body)  # Reject non-finite numbers, including unexpected extra fields.
    contract = SPECS[query.api][2]
    rows = []
    for index, values in enumerate(items):
        require(isinstance(values, list) and len(values) == len(fields), "FINANCIAL_ROW_WIDTH_INVALID")
        raw = dict(zip(fields, values))
        require(raw["ts_code"] == query.ts_code and raw["end_date"] == query.period, "FINANCIAL_SCOPE_MISMATCH")
        cells = {name: _cell(raw.get(name), contract.get(name, "unclassified"), name in raw)
                 for name in dict.fromkeys((*contract, *fields))}
        issues = []
        for name in ("ann_date", "f_ann_date", "first_ann_date"):
            cell = cells.get(name)
            if cell and cell.get("iso", "") > capture_day:
                issues.append(name.upper() + "_AFTER_CAPTURE")
        if cells.get("ann_date", {}).get("presence") != "value":
            issues.append("PUBLICATION_DATE_UNKNOWN")
        rt = str(raw["report_type"]) if raw.get("report_type") is not None else None
        if query.report_type is not None:
            require(rt == query.report_type, "FINANCIAL_REPORT_TYPE_MISMATCH")
        if query.api in ("income", "cashflow"):
            basis = "single-quarter" if rt in ("2", "3", "7", "8") else "cumulative-report-period" if rt in ("1", "4", "5", "6", "9", "10", "12") else "unknown-report-type"
        elif query.api == "balancesheet":
            basis = "period-end-balance"
        else:
            basis = {"forecast": "issuer-forecast-not-actual", "express": "preliminary-results",
                     "fina_indicator": "provider-calculated-mixed-measures", "fina_audit": "audit-metadata-not-audit-report",
                     "fina_mainbz": "requested-segment-dimension-not-total"}[query.api]
        if query.api == "fina_mainbz" and raw.get("bz_code") in ("P", "D", "I"):
            require(raw["bz_code"] == query.dimension, "FINANCIAL_DIMENSION_MISMATCH")
        rows.append({"source_row": offset + index, "row_sha256": digest(encoded(raw)), "cells": cells,
                     "period_basis": basis, "report_type": rt, "temporal_issues": issues,
                     "currency": raw.get("curr_type") or "UNKNOWN", "historical_available_at": "NOT_ESTABLISHED"})
    return {"offset": offset, "captured_at": captured_at, "rows": rows, "row_count": len(rows),
            "reported_count": count, "parsed_response_sha256": digest(encoded(body)),
            "missing_requested_fields": [f for f in contract if f not in fields],
            "source_claims": {k: body[k] for k in ("api_name", "request_id", "provider", "source")
                              if k in body and (body[k] is None or isinstance(body[k], str) and len(body[k]) <= 256)}}


def inspect_capture(capture):
    require(isinstance(capture, dict) and capture.get("schema") == CAPTURE_SCHEMA, "FINANCIAL_CAPTURE_INVALID")
    require(capture.get("error") is None or isinstance(capture["error"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", capture["error"]), "FINANCIAL_CAPTURE_ERROR_INVALID")
    query = FinancialQuery.from_dict(capture.get("query"))
    size = capture.get("page_size")
    query.parameters(page_size=size)
    require(capture.get("source_mode") in ("live-relay", "offline-unverified"), "FINANCIAL_SOURCE_MODE_INVALID")
    pages = capture.get("pages")
    require(isinstance(pages, list), "FINANCIAL_CAPTURE_PAGES_INVALID")
    normalized, offset, seen = [], 0, set()
    for index, page in enumerate(pages):
        require(isinstance(page, dict) and page.get("offset") == offset and type(page.get("offset")) is int,
                "FINANCIAL_PAGE_SEQUENCE_INVALID")
        parsed = normalize_page(query, page.get("body"), captured_at=page.get("captured_at"), page_size=size, offset=offset)
        if normalized:
            require(_clock(parsed["captured_at"]) >= _clock(normalized[-1]["captured_at"]), "FINANCIAL_CAPTURE_TIME_ORDER")
        fingerprint = digest(encoded(page["body"]["data"]))
        require(not parsed["row_count"] or fingerprint not in seen, "FINANCIAL_PAGINATION_STALLED")
        seen.add(fingerprint)
        meta = page.get("transport")
        if meta is not None:
            require(capture["source_mode"] == "live-relay" and isinstance(meta, dict)
                    and set(meta) == {"http_status", "x_request_id", "x_cache", "wire_sha256"}
                    and type(meta["http_status"]) is int and meta["http_status"] == 200 and isinstance(meta["wire_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", meta["wire_sha256"]), "FINANCIAL_TRANSPORT_RECEIPT_INVALID")
            for name in ("x_request_id", "x_cache"):
                require(meta[name] is None or isinstance(meta[name], str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", meta[name]),
                        "FINANCIAL_TRANSPORT_RECEIPT_INVALID")
        parsed["transport"] = meta
        offset += parsed["row_count"]
        require(index == len(pages) - 1 or parsed["row_count"] == size, "FINANCIAL_PAGE_AFTER_SHORT_PAGE")
        normalized.append(parsed)
    complete = bool(normalized) and not capture.get("error") and normalized[-1]["row_count"] < size
    if normalized and max(p["reported_count"] or 0 for p in normalized) > offset:
        complete = False
    hashes = [r["row_sha256"] for p in normalized for r in p["rows"]]
    return {"query": asdict(query), "query_key": query.key, "source_mode": capture["source_mode"],
            "request_complete": complete, "coverage": "PROVIDER_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS",
            "error": capture.get("error"), "page_count": len(normalized), "row_count": offset,
            "distinct_row_versions": len(set(hashes)), "duplicate_rows": len(hashes) - len(set(hashes)),
            "unit_qualification": "FIELD_CONTRACT_ONLY_RELAY_UNITS_NOT_INDEPENDENTLY_VERIFIED", "pages": normalized}


def source_for_page(capture, inspection, page_index):
    """A page-size source avoids one record per cell and does not alter the central store."""
    query = FinancialQuery.from_dict(inspection["query"])
    page = inspection["pages"][page_index]
    body = {"schema": "relay-financial-excerpt-v1", "query": asdict(query), "page": page,
            "source_mode": inspection["source_mode"], "request_complete": inspection["request_complete"],
            "coverage": inspection["coverage"], "unit_qualification": inspection["unit_qualification"]}
    entity = Entity("security-" + query.ts_code.replace(".", "-").lower(), "security", query.ts_code)
    return SourceInput("relay-fin-" + query.key[:32] + "-p" + str(page["offset"]), "content",
        SPECS[query.api][1] + " " + query.ts_code + " " + query.period,
        "https://pcd.mobcvb.cn/tushare/pro/" + query.api, encoded(body).decode(), custody="excerpt",
        entities=(entity,), acquired_at=page["captured_at"], report_period=report_period(query.period),
        limitations=("THIRD_PARTY_RELAY_NOT_OFFICIAL_TUSHARE_OR_ISSUER_PDF",
            "DERIVED_FIELDS_NOT_ORIGINAL_HTTP_BYTES; CAPTURE_DECLARATIONS_NOT_AUTHENTICATION",
            "NO_CURRENCY_CONVERSION_OR_SINGLE_QUARTER_CALCULATION; NO_LATEST_VERSION_SELECTED",
            "UNIT_CONTRACT_NOT_SOURCE_VERIFICATION; MISSING_VALUES_ARE_NOT_ZERO",
            "FORECAST_NOT_ACTUAL; AUDIT_METADATA_NOT_AUDIT_REPORT; NO_PIT_CERTIFICATION",
            "COVERAGE: " + inspection["coverage"], "REQUEST: " + query.key,
            "SOURCE_MODE: " + inspection["source_mode"]))
