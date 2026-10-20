"""Eight public participant views over retained facts; no source execution.

The existing Relay participant reader remains authoritative for forecasts and
visits. Other scalar rows keep source units unknown unless that field already
has a reviewed contract. Names are source labels, never verified identities.
"""
from __future__ import annotations

from copy import deepcopy
import base64
import binascii
from dataclasses import asdict
from datetime import date
from decimal import Decimal, localcontext
import gzip
import json
import re
import zlib

from decision_kernel.domain.records import (
    RecordError, SourceInput, checked_text, digest, encoded, identifier, require,
)
from decision_kernel.observations import relay_participants
from decision_kernel.observations.relay_financial import _cell, _clock

FAMILIES = ("public-seats", "institution-seats", "northbound", "holders-funds",
            "visits", "estimate-revisions", "holder-actions", "locked-capital")
# api: family, company-code field, source participant-label fields, time fields,
# meaning of the row. Do not convert the reported day to publication/PIT time.
CONTRACTS = {
    "hm_detail": ("public-seats", "ts_code", ("hm_name", "hm_orgs"), ("trade_date",),
                  "PUBLIC_SEAT_LABEL_NOT_VERIFIED_PERSON_OR_ACCOUNT"),
    "top_inst": ("institution-seats", "ts_code", ("exalter",), ("trade_date",),
                 "LISTED_SEAT_TRADES_NOT_COMPLETE_INSTITUTION_POSITION"),
    "hk_hold": ("northbound", "ts_code", ("exchange",), ("trade_date",),
                "REPORTED_HOLDING_SNAPSHOT_NOT_DAILY_NET_BUY"),
    "hsgt_top10": ("northbound", "ts_code", ("market_type",), ("trade_date",),
                   "RANKED_TURNOVER_NOT_COMPLETE_HOLDINGS_OR_NET_FLOW"),
    "hkex_shareholding": ("northbound", "ts_code", ("channel",), ("holding_date",),
                         "HKEX_REPORTED_SNAPSHOT_NOT_CURRENT_OR_DAILY_NET_BUY"),
    "top10_holders": ("holders-funds", "ts_code", ("holder_name",), ("ann_date", "end_date"),
                      "REPORTED_HOLDER_SNAPSHOT_NOT_COMPLETE_POSITION"),
    "top10_floatholders": ("holders-funds", "ts_code", ("holder_name",), ("ann_date", "end_date"),
                           "REPORTED_FLOAT_HOLDER_SNAPSHOT_NOT_COMPLETE_POSITION"),
    "fund_portfolio": ("holders-funds", "symbol", ("ts_code",), ("ann_date", "end_date"),
                       "FUND_DISCLOSED_HOLDING_NOT_CURRENT_FULL_POSITION"),
    "stk_surv": ("visits", "ts_code", ("rece_org",), ("surv_date",),
                 "PARTICIPANT_ROWS_NOT_EVENT_COUNT_OR_HOLDINGS"),
    "report_rc": ("estimate-revisions", "ts_code", ("org_name",), ("report_date",),
                  "SELL_SIDE_ESTIMATES_NOT_ISSUER_ACTUALS_OR_CONSENSUS"),
    "research_report": ("estimate-revisions", "ts_code", ("inst_csname",), ("trade_date",),
                        "REPORT_METADATA_NOT_FETCHED_PDF"),
    "stk_holdertrade": ("holder-actions", "ts_code", ("holder_name",),
                        ("ann_date", "begin_date", "close_date"),
                        "DISCLOSED_ACTION_WITH_SOURCE_STAGE_NOT_MOTIVE_OR_COMPLETE_EXECUTION"),
    "repurchase": ("holder-actions", "ts_code", (), ("ann_date", "end_date", "exp_date"),
                   "BUYBACK_PLAN_AND_CUMULATIVE_EXECUTION_ARE_DISTINCT"),
    "share_float": ("locked-capital", "ts_code", ("holder_name",), ("ann_date", "float_date"),
                    "PLANNED_RELEASE_NOT_PROOF_OF_SALE_OR_CASH_ARRIVAL"),
    "placement": ("locked-capital", "ts_code", ("subscriber",),
                  ("ann_date", "issue_date", "listing_date", "lock_end_date"),
                  "ISSUANCE_TERMS_NOT_PROOF_OF_CASH_PAID_OR_UNLOCKED_SALE"),
}
PAGE_SCHEMA = "participant-observation-page-v1"
PART_SCHEMA = "participant-observation-page-part-v1"
MAX_PAGE_ROWS = 10000
MAX_PAGE_BYTES = 8 * 1024 * 1024
# Bound the entire ordinary-JSON SourceInput for the existing 60 KiB host.
PART_BYTES = 24 * 1024
HOST_PAYLOAD_BYTES = 60 * 1024
CAPTURE_PART_BYTES = 36 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
CAPTURE_PART_SCHEMA = "participant-registered-capture-part-v1"
GZIP_PART_SCHEMA = "participant-observation-page-gzip-part-v1"
GZIP_PART_BYTES = 36 * 1024
MAX_GZIP_BYTES = 2 * 1024 * 1024
MAX_PARTS = 64
ESTIMATE_FIELDS = {"op_rt": "ten-thousand-yuan", "op_pr": "ten-thousand-yuan",
                   "tp": "ten-thousand-yuan", "np": "ten-thousand-yuan", "eps": "yuan-per-share"}


def _day(value):
    require(isinstance(value, str), "PARTICIPANT_DATE_INVALID")
    compact = value.replace("-", "")
    require(re.fullmatch(r"[0-9]{8}", compact), "PARTICIPANT_DATE_INVALID")
    try:
        return date(int(compact[:4]), int(compact[4:6]), int(compact[6:])).isoformat()
    except ValueError:
        raise RecordError("PARTICIPANT_DATE_INVALID") from None


def _scalar_rows(rows):
    require(isinstance(rows, list) and len(rows) <= MAX_PAGE_ROWS, "PARTICIPANT_ROWS_BOUND")
    for row in rows:
        require(isinstance(row, dict) and 1 <= len(row) <= 128, "PARTICIPANT_ROW_SHAPE")
        for key, value in row.items():
            require(isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key),
                    "PARTICIPANT_FIELD_INVALID")
            require(value is None or type(value) in (str, int, float, bool), "PARTICIPANT_CELL_INVALID")
            if isinstance(value, str):
                checked_text(value, empty=True, limit=8192)
    require(len(encoded(rows)) <= MAX_PAGE_BYTES, "PARTICIPANT_PAGE_BOUND")


def page_input(value):
    fields = {"schema", "api", "provider", "captured_at", "query", "coverage",
              "physical_page", "rows"}
    require(isinstance(value, dict) and set(value) == fields and value["schema"] == PAGE_SCHEMA,
            "PARTICIPANT_PAGE_SHAPE")
    require(isinstance(value["api"], str) and value["api"] in CONTRACTS, "PARTICIPANT_API_UNSUPPORTED")
    checked_text(value["provider"], limit=256)
    _clock(value["captured_at"])
    require(isinstance(value["query"], dict) and len(value["query"]) <= 32,
            "PARTICIPANT_QUERY_INVALID")
    for key, val in value["query"].items():
        checked_text(key, limit=64)
        require(val is None or type(val) in (str, int, bool), "PARTICIPANT_QUERY_INVALID")
        if isinstance(val, str):
            checked_text(val, empty=True, limit=1024)
    page = value["physical_page"]
    require(isinstance(page, dict) and set(page) == {"page_id", "offset", "row_count", "reported_total",
                "request_complete", "error", "wire_sha256"}, "PARTICIPANT_PHYSICAL_PAGE")
    checked_text(page["page_id"], limit=128)
    require(type(page["offset"]) is int and page["offset"] >= 0
            and type(page["row_count"]) is int and page["row_count"] == len(value["rows"]),
            "PARTICIPANT_PHYSICAL_ROWS")
    require(page["reported_total"] is None or type(page["reported_total"]) is int
            and page["reported_total"] >= page["row_count"], "PARTICIPANT_REPORTED_TOTAL")
    require(type(page["request_complete"]) is bool, "PARTICIPANT_COMPLETION_INVALID")
    require(page["error"] is None or isinstance(page["error"], str)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,95}", page["error"]), "PARTICIPANT_ERROR_INVALID")
    require(not page["request_complete"] or page["error"] is None, "PARTICIPANT_COMPLETION_INVALID")
    require(page["wire_sha256"] is None or isinstance(page["wire_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", page["wire_sha256"]), "PARTICIPANT_WIRE_DIGEST")
    checked_text(value["coverage"], limit=4096)
    _scalar_rows(value["rows"])
    return deepcopy(value)


def page_sources(value, *, source_id, origin, title):
    """Lossless custody pieces of ONE physical response; never new query pages.

    All rows, including rejected non-A securities, remain in the source pieces.
    Splitting changes storage only. Reconstruction requires every part, exact
    order, row counts and the digest of the entire normalized page representation.
    """
    page = page_input(value)
    identifier(source_id)
    require(len(source_id) <= 85, "PARTICIPANT_SOURCE_ID_TOO_LONG")
    header = {key: val for key, val in page.items() if key != "rows"}
    full_digest = digest(encoded(page))
    chunks, current = [], []
    for row in page["rows"]:
        candidate = current + [row]
        if current and len(encoded(candidate)) > PART_BYTES - 4 * 1024:
            chunks.append(current)
            current = []
        current.append(row)
    if current or not chunks:
        chunks.append(current)
    require(len(chunks) <= MAX_PARTS, "PARTICIPANT_PART_BOUND")
    output, offset = [], 0
    for index, rows in enumerate(chunks):
        body = {"schema": PART_SCHEMA, "page": header, "page_sha256": full_digest,
                "part_index": index, "part_count": len(chunks), "row_start": offset, "rows": rows}
        text = encoded(body).decode()
        require(len(text.encode()) <= PART_BYTES, "PARTICIPANT_PART_BYTES")
        source = SourceInput(f"{source_id}-part{index}", "content", title, origin, text,
            custody="excerpt", acquired_at=page["captured_at"], limitations=(
                "STRUCTURED_REPRESENTATION_NOT_ORIGINAL_HTTP_BYTES", page["coverage"],
                "CUSTODY_PART_NOT_NEW_PHYSICAL_PAGE_OR_ADDITIONAL_SOURCE_REQUEST",
                "SOURCE_LABELS_NOT_AUTHENTICATED_PERSONS_OR_COMPLETE_POSITIONS",
                "NO_PUBLICATION_RIGHTS_PIT_OR_RESEARCH_CERTIFICATION"))
        require(len(encoded(asdict(source))) <= HOST_PAYLOAD_BYTES, "PARTICIPANT_HOST_PAYLOAD_BOUND")
        output.append(source)
        offset += len(rows)
    return tuple(output)


def gzip_page_sources(value, *, source_id, origin, title):
    """Bounded gzip custody of the entire canonical structured page.

    Compression preserves every row and the original physical-page controls.
    It does not retain HTTP bytes or create another source request. The existing
    page_sources producer continues to emit its original uncompressed format.
    """
    page = page_input(value)
    identifier(source_id)
    require(len(source_id) <= 85, "PARTICIPANT_SOURCE_ID_TOO_LONG")
    raw = encoded(page)
    require(len(raw) <= MAX_PAGE_BYTES, "PARTICIPANT_PAGE_BOUND")
    packed = gzip.compress(raw, mtime=0)
    require(1 <= len(packed) <= MAX_GZIP_BYTES, "PARTICIPANT_GZIP_BYTES_BOUND")
    chunks = [packed[start:start + GZIP_PART_BYTES] for start in range(0, len(packed), GZIP_PART_BYTES)]
    require(len(chunks) <= MAX_PARTS, "PARTICIPANT_PART_BOUND")
    identity = {"page_sha256": digest(raw), "page_byte_size": len(raw),
                "gzip_sha256": digest(packed), "gzip_byte_size": len(packed)}
    sources = []
    for index, chunk in enumerate(chunks):
        body = {"schema": GZIP_PART_SCHEMA, **identity, "part_index": index,
                "part_count": len(chunks), "byte_start": index * GZIP_PART_BYTES,
                "data": base64.b64encode(chunk).decode("ascii")}
        source = SourceInput(f"{source_id}-part{index}", "content", title, origin, encoded(body).decode(),
            custody="excerpt", acquired_at=page["captured_at"], limitations=(
                "STRUCTURED_REPRESENTATION_NOT_ORIGINAL_HTTP_BYTES", page["coverage"],
                "CUSTODY_PART_NOT_NEW_PHYSICAL_PAGE_OR_ADDITIONAL_SOURCE_REQUEST",
                "SOURCE_LABELS_NOT_AUTHENTICATED_PERSONS_OR_COMPLETE_POSITIONS",
                "NO_PUBLICATION_RIGHTS_PIT_OR_RESEARCH_CERTIFICATION"))
        require(len(encoded(asdict(source))) <= HOST_PAYLOAD_BYTES, "PARTICIPANT_HOST_PAYLOAD_BOUND")
        sources.append(source)
    return tuple(sources)


def join_page(parts):
    require(isinstance(parts, list) and 1 <= len(parts) <= MAX_PARTS, "PARTICIPANT_PART_BOUND")
    if isinstance(parts[0], dict) and parts[0].get("schema") == GZIP_PART_SCHEMA:
        return _join_gzip_page(parts)
    rows, first = [], None
    for index, item in enumerate(parts):
        require(isinstance(item, dict) and set(item) == {"schema", "page", "page_sha256", "part_index",
            "part_count", "row_start", "rows"} and item["schema"] == PART_SCHEMA, "PARTICIPANT_PART_SHAPE")
        require(type(item["part_index"]) is int and item["part_index"] == index
                and type(item["part_count"]) is int and item["part_count"] == len(parts)
                and type(item["row_start"]) is int and item["row_start"] == len(rows),
                "PARTICIPANT_PART_SEQUENCE")
        if first is None:
            first = item
        require(item["page"] == first["page"] and item["page_sha256"] == first["page_sha256"],
                "PARTICIPANT_PART_IDENTITY")
        _scalar_rows(item["rows"])
        rows.extend(item["rows"])
        require(len(rows) <= MAX_PAGE_ROWS, "PARTICIPANT_ROWS_BOUND")
    page = page_input({**first["page"], "rows": rows})
    require(digest(encoded(page)) == first["page_sha256"], "PARTICIPANT_PAGE_DIGEST")
    return page


def _join_gzip_page(parts):
    chunks, offset, first = [], 0, None
    identity = ("page_sha256", "page_byte_size", "gzip_sha256", "gzip_byte_size")
    for index, part in enumerate(parts):
        require(isinstance(part, dict) and set(part) == {"schema", *identity, "part_index",
                "part_count", "byte_start", "data"} and part["schema"] == GZIP_PART_SCHEMA,
                "PARTICIPANT_GZIP_PART_SHAPE")
        require(type(part["part_index"]) is int and part["part_index"] == index
                and type(part["part_count"]) is int and part["part_count"] == len(parts)
                and type(part["byte_start"]) is int and part["byte_start"] == offset,
                "PARTICIPANT_GZIP_PART_SEQUENCE")
        require(type(part["page_byte_size"]) is int and 1 <= part["page_byte_size"] <= MAX_PAGE_BYTES
                and type(part["gzip_byte_size"]) is int and 1 <= part["gzip_byte_size"] <= MAX_GZIP_BYTES
                and all(isinstance(part[key], str) and re.fullmatch(r"[a-f0-9]{64}", part[key])
                        for key in ("page_sha256", "gzip_sha256")), "PARTICIPANT_GZIP_IDENTITY")
        if first is None:
            first = part
        require(all(part[key] == first[key] for key in identity), "PARTICIPANT_GZIP_IDENTITY")
        require(isinstance(part["data"], str) and len(part["data"]) <= 4 * ((GZIP_PART_BYTES + 2) // 3),
                "PARTICIPANT_GZIP_PART_BOUND")
        try:
            chunk = base64.b64decode(part["data"], validate=True)
        except (ValueError, binascii.Error):
            raise RecordError("PARTICIPANT_GZIP_BASE64") from None
        require(base64.b64encode(chunk).decode("ascii") == part["data"]
                and 1 <= len(chunk) <= GZIP_PART_BYTES, "PARTICIPANT_GZIP_BASE64")
        chunks.append(chunk)
        offset += len(chunk)
        require(offset <= first["gzip_byte_size"], "PARTICIPANT_GZIP_BYTES_BOUND")
    packed = b"".join(chunks)
    require(len(packed) == first["gzip_byte_size"] and digest(packed) == first["gzip_sha256"],
            "PARTICIPANT_GZIP_DIGEST")
    try:
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(packed, first["page_byte_size"] + 1)
    except zlib.error:
        raise RecordError("PARTICIPANT_GZIP_ENCODING") from None
    require(len(raw) <= first["page_byte_size"] and decoder.eof and not decoder.unused_data
            and not decoder.unconsumed_tail, "PARTICIPANT_GZIP_DECOMPRESSION_BOUND")
    require(len(raw) == first["page_byte_size"] and digest(raw) == first["page_sha256"],
            "PARTICIPANT_PAGE_DIGEST")
    try:
        value = json.loads(raw.decode("utf-8"))
        canonical = encoded(value)
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("PARTICIPANT_GZIP_JSON") from None
    require(canonical == raw, "PARTICIPANT_GZIP_JSON")
    try:
        return page_input(value)
    except (KeyError, TypeError, RecursionError):
        raise RecordError("PARTICIPANT_PAGE_SHAPE") from None



def registered_capture_sources(raw, *, source_id, origin, title, acquired_at):
    """Losslessly retain one existing capture's original bytes in bounded sources.

    This changes custody only, including for failed captures. All controls,
    original attempts and original query/page boundaries remain inside the exact
    reassembled bytes and are revalidated by the original inspector on reading.
    """
    require(isinstance(raw, bytes) and 1 <= len(raw) <= MAX_CAPTURE_BYTES,
            "PARTICIPANT_CAPTURE_BYTES_BOUND")
    identifier(source_id)
    require(len(source_id) <= 85, "PARTICIPANT_SOURCE_ID_TOO_LONG")
    pieces = [raw[start:start + CAPTURE_PART_BYTES] for start in range(0, len(raw), CAPTURE_PART_BYTES)]
    require(len(pieces) <= MAX_PARTS, "PARTICIPANT_PART_BOUND")
    sources = []
    for index, piece in enumerate(pieces):
        body = {"schema": CAPTURE_PART_SCHEMA, "capture_sha256": digest(raw), "byte_size": len(raw),
                "part_index": index, "part_count": len(pieces), "byte_start": index * CAPTURE_PART_BYTES,
                "data": base64.b64encode(piece).decode("ascii")}
        source = SourceInput(f"{source_id}-part{index}", "content", title, origin, encoded(body).decode(),
            custody="excerpt", acquired_at=acquired_at, limitations=(
                "EXACT_CAPTURE_BYTES_NOT_ORIGINAL_HTTP_OR_SOURCE_AUTHENTICATION",
                "CUSTODY_PART_NOT_ADDITIONAL_QUERY_PAGE_ATTEMPT_OR_EXECUTION_AUTHORITY",
                "REASSEMBLE_ALL_PARTS_AND_RECHECK_ORIGINAL_QUERY_CONTROL_AND_FAILURES"))
        require(len(encoded(asdict(source))) <= HOST_PAYLOAD_BYTES, "PARTICIPANT_HOST_PAYLOAD_BOUND")
        sources.append(source)
    return tuple(sources)


def join_registered_capture(parts):
    require(isinstance(parts, list) and 1 <= len(parts) <= MAX_PARTS, "PARTICIPANT_PART_BOUND")
    pieces, offset, first = [], 0, None
    for index, part in enumerate(parts):
        require(isinstance(part, dict) and set(part) == {"schema", "capture_sha256", "byte_size",
            "part_index", "part_count", "byte_start", "data"} and part["schema"] == CAPTURE_PART_SCHEMA,
            "PARTICIPANT_CAPTURE_PART_SHAPE")
        require(type(part["part_index"]) is int and part["part_index"] == index
                and type(part["part_count"]) is int and part["part_count"] == len(parts)
                and type(part["byte_start"]) is int and part["byte_start"] == offset,
                "PARTICIPANT_CAPTURE_PART_SEQUENCE")
        require(type(part["byte_size"]) is int and 1 <= part["byte_size"] <= MAX_CAPTURE_BYTES
                and isinstance(part["capture_sha256"], str)
                and re.fullmatch(r"[a-f0-9]{64}", part["capture_sha256"]), "PARTICIPANT_CAPTURE_IDENTITY")
        if first is None:
            first = part
        require((part["byte_size"], part["capture_sha256"]) == (first["byte_size"], first["capture_sha256"]),
                "PARTICIPANT_CAPTURE_IDENTITY")
        require(isinstance(part["data"], str) and len(part["data"]) <= CAPTURE_PART_BYTES * 2,
                "PARTICIPANT_CAPTURE_PART_BOUND")
        try:
            raw = base64.b64decode(part["data"], validate=True)
        except (ValueError, binascii.Error):
            raise RecordError("PARTICIPANT_CAPTURE_BASE64") from None
        require(base64.b64encode(raw).decode("ascii") == part["data"]
                and 1 <= len(raw) <= CAPTURE_PART_BYTES, "PARTICIPANT_CAPTURE_BASE64")
        pieces.append(raw)
        offset += len(raw)
        require(offset <= first["byte_size"], "PARTICIPANT_CAPTURE_BYTES_BOUND")
    raw = b"".join(pieces)
    require(len(raw) == first["byte_size"] and digest(raw) == first["capture_sha256"],
            "PARTICIPANT_CAPTURE_DIGEST")
    return raw


def legacy_page(value):
    """Reopen the existing three-family excerpt without recapturing or rewriting it."""
    require(isinstance(value, dict) and value.get("schema") == "relay-participant-excerpt-v1",
            "PARTICIPANT_LEGACY_SCHEMA")
    query = relay_participants.ParticipantQuery.from_dict(value.get("query"))
    page = value.get("page")
    require(isinstance(page, dict) and isinstance(page.get("rows"), list), "PARTICIPANT_LEGACY_PAGE")
    rows = []
    for row in page["rows"]:
        require(isinstance(row, dict) and isinstance(row.get("cells"), dict), "PARTICIPANT_LEGACY_ROW")
        raw = {k: cell["raw"] for k, cell in row["cells"].items() if cell.get("presence") != "missing"}
        require(digest(encoded(raw)) == row["row_sha256"], "PARTICIPANT_LEGACY_ROW_DIGEST")
        rows.append(raw)
    return page_input({"schema": PAGE_SCHEMA, "api": query.api, "provider": "third-party-relay",
        "captured_at": page["captured_at"], "query": asdict(query), "coverage": value["coverage"],
        "physical_page": {"page_id": str(page["offset"]), "offset": page["offset"], "row_count": len(rows),
            "reported_total": page.get("reported_count"), "request_complete": False,
            "error": None, "wire_sha256": (page.get("transport") or {}).get("wire_sha256")}, "rows": rows})


def _scope_matches(api, query, raw):
    # The generic retained table verifies transport/projection, not business row
    # identity. Match the reviewed query dimensions again before admitting rows.
    if query.get("ts_code") is not None and raw.get("ts_code") != query["ts_code"]:
        return False
    for parameter, field in (("period", "end_date"), ("ann_date", "ann_date"),
                              ("report_date", "report_date"),
                              ("trade_date", "surv_date" if api == "stk_surv" else "trade_date")):
        if query.get(parameter) is not None and raw.get(field) != query[parameter]:
            return False
    range_field = {"stk_surv": "surv_date", "stk_holdertrade": "ann_date"}.get(api)
    if range_field and any(query.get(k) is not None for k in ("start_date", "end_date")):
        try:
            day = _day(raw.get(range_field))
            if query.get("start_date") is not None and day < _day(query["start_date"]):
                return False
            if query.get("end_date") is not None and day > _day(query["end_date"]):
                return False
        except RecordError:
            return False
    return True


def normalize_rows(value):
    page = page_input(value)
    api = page["api"]
    family, company_field, participant_fields, time_fields, meaning = CONTRACTS[api]
    accepted, rejected = [], []
    for index, raw in enumerate(page["rows"]):
        position = page["physical_page"]["offset"] + index
        if not _scope_matches(api, page["query"], raw):
            rejected.append({"source_row": position, "row_sha256": digest(encoded(raw)),
                             "reason_code": "PARTICIPANT_ROW_OUTSIDE_QUERY"})
            continue
        company = raw.get(company_field)
        if not isinstance(company, str) or re.fullmatch(r"[0-9]{6}\.(?:SH|SZ|BJ)", company) is None:
            rejected.append({"source_row": position, "row_sha256": digest(encoded(raw)),
                             "reason_code": "PARTICIPANT_COMPANY_UNQUALIFIED"})
            continue
        if api == "hkex_shareholding" and not re.fullmatch(r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ|[489][0-9]{5}\.BJ)", company):
            rejected.append({"source_row": position, "row_sha256": digest(encoded(raw)),
                             "reason_code": "PARTICIPANT_NON_A_SHARE"})
            continue
        dates, temporal_issues = {}, []
        for field in time_fields:
            if raw.get(field) in (None, ""):
                dates[field] = None
            else:
                try:
                    dates[field] = _day(raw[field])
                except RecordError:
                    dates[field] = None
                    temporal_issues.append(field.upper() + "_INVALID")
        day = next((dates[f] for f in time_fields if dates[f] is not None), None)
        # Future plan/lock dates are valid plans, never historical available-at.
        if day and day > _clock(page["captured_at"]).date().isoformat():
            temporal_issues.append("SOURCE_DAY_AFTER_CAPTURE")
        if api == "top_inst" and not isinstance(raw.get("exalter"), str):
            rejected.append({"source_row": position, "row_sha256": digest(encoded(raw)),
                             "reason_code": "PARTICIPANT_SEAT_IDENTITY_MISSING"})
            continue
        if api == "top_inst" and raw["exalter"].strip() not in ("机构专用", "机构专用席位", "机构席位"):
            rejected.append({"source_row": position, "row_sha256": digest(encoded(raw)),
                             "reason_code": "PARTICIPANT_NON_INSTITUTION_SEAT"})
            continue
        labels = [(field, str(raw[field])) for field in participant_fields
                  if raw.get(field) not in (None, "")]
        # Issuer buybacks have no observed external participant: do not invent one.
        participant = ({"participant_id": "participant-" + digest(encoded([page["provider"], api, labels]))[:32],
                        "labels": [{"field": f, "value": v} for f, v in labels],
                        "identity_status": "source-label-not-authenticated-or-cross-source-merged"}
                       if labels and not any(label in ("机构专用", "机构", "机构专用席位", "--")
                                             for _, label in labels) else None)
        period = raw.get("quarter") if api == "report_rc" else raw.get("end_date")
        period = period if isinstance(period, str) and period.strip() else None
        cells = {}
        if api == "report_rc":
            for key, unit in ESTIMATE_FIELDS.items():
                cells[key] = _cell(raw.get(key), "currency/share" if unit == "yuan-per-share" else unit, key in raw)
                cells[key]["unit"] = unit
        basis = {key: raw.get(key) for key in ("quarter", "holder_type", "share_type", "market_type",
                 "exchange", "channel", "holder_name", "in_de", "proc", "reason", "trade_type") if key in raw}
        accepted.append({"family": family, "api": api, "provider": page["provider"],
            "company_id": "security-" + company.replace(".", "-").lower(), "ts_code": company,
            "participant": participant, "source_row": position, "row_sha256": digest(encoded(raw)),
            "reported_day": day, "source_dates": dates, "report_period": period,
            "captured_at": page["captured_at"], "historical_available_at": "NOT_ESTABLISHED",
            "publication_date": dates.get("ann_date") or (dates.get("report_date") if api == "report_rc" else None),
            "temporal_issues": temporal_issues, "cells": cells, "basis": basis,
            "unit_qualification": "EXISTING_REVIEWED_FORECAST_FIELDS" if api == "report_rc" else "UNKNOWN_SOURCE_UNITS",
            "anonymous_participant": bool(labels) and participant is None,
            "interpretation": meaning, "position_inferred": False, "event_count": None,
            "economic_effect": "not-assessed", "raw": deepcopy(raw)})
    return {"api": api, "family": family, "rows": accepted, "rejected_rows": rejected,
            "raw_row_count": len(page["rows"]), "accepted_row_count": len(accepted),
            "physical_page": deepcopy(page["physical_page"]), "coverage": page["coverage"],
            "captured_at": page["captured_at"], "source_requests": 0}


def estimate_changes(rows):
    """Comparable, same-source label / issuer / target-period revisions only.

    Equal-day conflicting observations are ambiguous; no ordering is invented.
    Non-forecast holdings/actions are never subtracted or accumulated here.
    """
    groups, out = {}, []
    for row in rows:
        if row["api"] != "report_rc" or row["participant"] is None or not row["report_period"]:
            continue
        key = encoded([row["provider"], row["company_id"], row["participant"]["participant_id"],
                       row["report_period"]])
        groups.setdefault(key, []).append(row)
    for members in groups.values():
        unique = {r["row_sha256"]: r for r in members}
        ordered = sorted(unique.values(), key=lambda r: (r["reported_day"] or "", r["row_sha256"]))
        dates = {}
        for member in ordered:
            dates[member["reported_day"]] = dates.get(member["reported_day"], 0) + 1
        for previous, current in zip(ordered, ordered[1:]):
            base = {"company_id": current["company_id"], "participant_id": current["participant"]["participant_id"],
                    "target_period": current["report_period"], "from_row": previous["row_sha256"],
                    "to_row": current["row_sha256"], "from_date": previous["reported_day"],
                    "to_date": current["reported_day"], "interpretation": "same-source-label-estimate-not-consensus-or-actual"}
            for field, unit in ESTIMATE_FIELDS.items():
                before, after = previous["cells"][field], current["cells"][field]
                reason = ("PARTICIPANT_REVISION_DATE_AMBIGUOUS" if not previous["reported_day"]
                    or previous["reported_day"] == current["reported_day"]
                    or dates[previous["reported_day"]] > 1 or dates[current["reported_day"]] > 1 else
                    "PARTICIPANT_REVISION_TIME_UNQUALIFIED" if previous["temporal_issues"] or current["temporal_issues"] else
                    "PARTICIPANT_REVISION_VALUE_MISSING" if before["presence"] != "value" or after["presence"] != "value" else None)
                delta = None
                if reason is None:
                    with localcontext() as context:
                        context.prec = 128
                        delta = format(Decimal(after["numeric_text"]) - Decimal(before["numeric_text"]), "f")
                out.append({**base, "metric": field, "unit": unit,
                            "status": "comparable" if reason is None else "unavailable", "reason_code": reason,
                            "delta": delta, "relative_change": None})
    return out
