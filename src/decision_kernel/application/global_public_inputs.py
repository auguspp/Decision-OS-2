"""Bounded supplied originals for four fixed global context families.

This input is a representation supplied by the caller, not an authenticated
HTTP execution or a source-access/publication grant. No source I/O happens here.
Original capture clocks qualify dates; the reading wall clock never does.
"""
from copy import deepcopy
from decimal import Decimal
import re
from xml.etree import ElementTree as ET

from decision_kernel.domain.records import MAX_BYTES, RecordError, checked_text, digest, encoded, require
from decision_kernel.observations.global_public import (
    AUTHORITY, FAMILIES, LEGACY_VERSION, VERSION, _qualify_date, clock,
    parse, plan, series_info, summarize,
)

PUBLIC_READING_SCHEMA = "global-public-reading-v1"
PUBLIC_VIEW_SCHEMA = "global-public-view-v1"
PUBLIC_REQUIRED = {"schema", "capture_version", "family", "as_of_date", "started_at",
                   "finished_at", "execution_complete", "records"}
RECORD_FIELDS = {"index", "spec", "state", "http_status", "requested_at", "received_at",
                 "headers", "body", "bytes", "sha256", "error_type"}
SAFE_HEADERS = {"content-type", "content-length", "content-encoding", "date", "last-modified",
                "etag", "retry-after"}
QUALIFICATION = "supplied-original-representation-not-authenticated-HTTP-execution"


def _headers(value):
    require(isinstance(value, dict) and set(value) <= SAFE_HEADERS, "GP_HEADERS")
    for item in value.values():
        checked_text(item, empty=True, limit=2048)
        require("\r" not in item and "\n" not in item, "GP_HEADERS")


def _input_clock(value):
    try:
        return clock(value)
    except OverflowError:
        raise RecordError("GP_CLOCK_INVALID") from None


def validate_public_input(payload, *, retained=False):
    """Validate every planned receipt, including failures and unattempted slots.

    A retained recipe uses an exact original-body slot instead of inline text.
    It otherwise has precisely the same dates, plan and finite receipt states.
    """
    require(isinstance(payload, dict) and PUBLIC_REQUIRED <= payload.keys()
            <= PUBLIC_REQUIRED | {"date_policy"}
            and payload.get("schema") == PUBLIC_READING_SCHEMA, "GP_INPUT_SCHEMA")
    require(len(encoded(payload)) <= MAX_BYTES, "MARKET_CUSTODY_INPUT_BOUND")
    family, version = payload["family"], payload["capture_version"]
    require(isinstance(family, str) and family in FAMILIES, "GP_FAMILY")
    require(version in (LEGACY_VERSION, VERSION), "GP_CAPTURE_VERSION")
    require((version == VERSION) == ("date_policy" in payload), "GP_DATE_POLICY")
    first, finish = _input_clock(payload["started_at"]), _input_clock(payload["finished_at"])
    require(first <= finish, "GP_CAPTURE_TIME")
    try:
        _qualify_date(version, family, payload["as_of_date"], payload["started_at"], payload.get("date_policy"))
        specs = plan(family, payload["as_of_date"])
    except OverflowError:
        raise RecordError("GP_DATE") from None
    require(type(payload["execution_complete"]) is bool and isinstance(payload["records"], list)
            and len(payload["records"]) == len(specs), "GP_PLAN")
    stopped = False
    for index, (spec, record) in enumerate(zip(specs, payload["records"], strict=True)):
        require(isinstance(record, dict) and set(record) == RECORD_FIELDS
                and type(record["index"]) is int and record["index"] == index
                and record["spec"] == spec, "GP_RECORD_SCOPE")
        state, status = record["state"], record["http_status"]
        require(isinstance(state, str), "GP_REQUEST_STATE")
        if state in {"NOT_ATTEMPTED", "NOT_ATTEMPTED_SERVICE_STOP"}:
            require(all(record[k] is None for k in ("requested_at", "received_at", "body", "bytes",
                    "sha256", "http_status", "error_type")) and record["headers"] == {}
                    and (state != "NOT_ATTEMPTED_SERVICE_STOP" or stopped), "GP_NOT_ATTEMPTED")
            require(not payload["execution_complete"] or state != "NOT_ATTEMPTED", "GP_FALSE_COMPLETE")
        else:
            require(not stopped and state in {"REQUEST_IN_PROGRESS", "HTTP_RESPONSE", "TRANSPORT_UNAVAILABLE"},
                    "GP_REQUEST_STATE")
            require(first <= _input_clock(record["requested_at"]) <= finish, "GP_REQUEST_TIME")
            if state == "REQUEST_IN_PROGRESS":
                require(not payload["execution_complete"] and all(record[k] is None for k in
                    ("received_at", "http_status", "body", "bytes", "sha256", "error_type"))
                    and record["headers"] == {}, "GP_IN_PROGRESS")
            else:
                require(_input_clock(record["requested_at"]) <= _input_clock(record["received_at"]) <= finish,
                        "GP_RESPONSE_TIME")
                _headers(record["headers"])
                if state == "HTTP_RESPONSE":
                    require(type(status) is int and 100 <= status <= 599 and record["error_type"] is None,
                            "GP_RESPONSE")
                    require(type(record["bytes"]) is int and 0 < record["bytes"] <= MAX_BYTES
                            and isinstance(record["sha256"], str)
                            and re.fullmatch(r"[a-f0-9]{64}", record["sha256"]), "GP_RAW_IDENTITY")
                    require(record["headers"].get("content-encoding", "identity").lower() in {"", "identity"},
                            "GP_ENCODING")
                    length = record["headers"].get("content-length")
                    require(length is None or re.fullmatch(r"[0-9]{1,10}", length)
                            and int(length) == record["bytes"], "GP_BODY_LENGTH")
                    if retained:
                        require(record["body"] == {"source": index}, "GP_BODY_SLOT")
                    else:
                        raw = checked_text(record["body"]).encode("utf-8")
                        require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], "GP_RAW_IDENTITY")
                else:
                    require(record["body"] is None and record["bytes"] is None and record["sha256"] is None
                            and (status is None or type(status) is int and 100 <= status <= 599)
                            and isinstance(record["error_type"], str)
                            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", record["error_type"]), "GP_TRANSPORT")
        stopped = stopped or status in (401, 403, 429) or state == "TRANSPORT_UNAVAILABLE"
    return payload


def capture_identity(payload):
    """Identity binds the original receipt and raw digest, not current custody."""
    recipe = deepcopy(payload)
    for record in recipe["records"]:
        if record["state"] == "HTTP_RESPONSE":
            record["body"] = {"sha256": record["sha256"], "bytes": record["bytes"]}
    return digest(encoded(recipe))


def _json_value(value):
    # The original pure decoder retains Decimal. The standard source model uses
    # exact decimal text, never a round-trip through binary floating point.
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def public_view(payload, bodies, *, unavailable=(), offset=0, limit=50, entity_id=None):
    """Interpret an already validated receipt and independently checked bodies."""
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "PAGE_INVALID")
    declared = tuple(series_info(payload["family"]))
    require(entity_id is None or isinstance(entity_id, str) and entity_id in declared, "GP_ENTITY_SCOPE")
    tables, outcomes = [], []
    unavailable = set(unavailable)
    for record in payload["records"]:
        spec, status, state = record["spec"], record["http_status"], record["state"]
        outcome = {"index": record["index"], "id": spec["id"], "request_state": state,
                   "http_status": status, "status": state, "source_url": spec["url"],
                   "source_rows": None, "selected_rows": 0}
        if record["index"] in unavailable:
            outcome["status"] = "RETAINED_ORIGINAL_UNAVAILABLE"
        elif state == "HTTP_RESPONSE" and status == 200:
            try:
                rows, count, meta = parse(bodies[record["index"]], spec)
                outcome.update(status="ROWS_NORMALIZED" if rows else "EMPTY_WINDOW_NOT_NO_CHANGE",
                               source_rows=count, selected_rows=len(rows), metadata=_json_value(meta))
                tables.extend(rows)
            except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, ET.ParseError, OSError, RecursionError):
                outcome["status"] = "SOURCE_TABLE_UNCONFIRMED"
        elif status is not None:
            outcome["status"] = ("RESPONSE_UNAVAILABLE_HTTP_" if state == "TRANSPORT_UNAVAILABLE" else "HTTP_") + str(status)
        outcomes.append(outcome)
    # Two Treasury month originals feed every tenor. Losing either original
    # leaves the common path unknown; an older surviving month cannot become a
    # replacement latest reading. Single-instrument families remain separable.
    if payload["family"] == "treasury" and unavailable:
        tables = []
    try:
        observations = summarize(tables, payload["family"], payload["as_of_date"])
    except ArithmeticError:
        raise RecordError("GP_ARITHMETIC_UNAVAILABLE") from None
    entries = [{"entity_id": row["symbol"], **row} for row in observations]
    available = sum(row["value"] is not None for row in entries)
    complete = (payload["execution_complete"] and available == len(declared)
                and all(item["status"] == "ROWS_NORMALIZED" for item in outcomes))
    result = {"schema": PUBLIC_VIEW_SCHEMA, "family": payload["family"],
              "capture_version": payload["capture_version"], "capture_sha256": capture_identity(payload),
              "captured_from": payload["started_at"], "captured_through": payload["finished_at"],
              "execution_complete": payload["execution_complete"],
              "start": payload["records"][0]["spec"]["begin"], "as_of": payload["as_of_date"],
              "date_policy": deepcopy(payload.get("date_policy")), "declared_entities": list(declared),
              "scope_count": len(declared), "available_values": available,
              "missing_values": len(declared) - available, "outcomes": outcomes,
              "entries": entries, "status": "AVAILABLE" if complete else "PARTIAL" if available else "UNAVAILABLE",
              "source_qualification": QUALIFICATION, "authority": deepcopy(AUTHORITY),
              "source_requests": 0, "coverage": "SELECTED_SOURCE_DATES_NOT_FULL_GLOBAL_MARKETS",
              "vintage": "CURRENT_RETRIEVAL_OF_DATED_ROWS_NOT_HISTORICAL_AS_KNOWN_VINTAGE",
              "qualification_limits": ["Original capture dates do not prove publication or natural updates.",
                  "Receipts and body digests do not authenticate source access or grant redistribution.",
                  "Only actual returned dates are compared; missing latest values stay unknown.",
                  "Futures rollover/settlement/spot identity is not established; interval returns are not computed."]}
    result["view_version"] = digest(encoded(result))
    selected = [row for row in entries if entity_id is None or row["entity_id"] == entity_id]
    result["entries"] = selected[offset:offset + limit]
    next_offset = offset + len(result["entries"])
    result["pagination"] = {"offset": offset, "limit": limit, "entity_id": entity_id,
                            "matched_count": len(selected), "returned": len(result["entries"]),
                            "next_offset": next_offset if next_offset < len(selected) else None}
    return result


def read_global_public_payload(payload, *, offset=0, limit=50, entity_id=None):
    validate_public_input(payload)
    bodies = {record["index"]: record["body"].encode("utf-8") for record in payload["records"]
              if record["state"] == "HTTP_RESPONSE"}
    return public_view(payload, bodies, offset=offset, limit=limit, entity_id=entity_id)
