"""Qualify bounded, supplied FTShare minute bytes and their original receipt.

No HTTP, SDK, credentials, request allowance or acquisition is introduced here.
An exact saved auction root must be restored before this input can be consumed.
"""
from copy import deepcopy
from datetime import datetime, time, timedelta
import re

from decision_kernel.application.relay_registry import _json
from decision_kernel.application.research_agenda import clock
from decision_kernel.domain.records import MAX_BYTES, RecordError, Selection, SourceRef, digest, encoded, require
from decision_kernel.observations.auction_minute import (
    MAX_BATCHES, ZONE, minute_request, minute_plan, minute_observation,
)
from decision_kernel.observations.market_daily import trade_day

INPUT_SCHEMA = "ftshare-auction-minute-reading-v1"
QUALIFICATION = "supplied-minute-originals-and-receipt-not-source-access-or-execution-authentication"
STOP_CODES = {"AUTHENTICATION_FAILED", "ENTITLEMENT_DENIED", "RATE_LIMITED", "TRANSPORT_FAILURE",
    "PARSE_FAILURE", "HTTP_REJECTED", "PROVIDER_REJECTED", "AUTHENTICATION_UNAVAILABLE",
    "CREDENTIAL_REFLECTION_REJECTED", "SOURCE_REQUEST_FAILED", "RESPONSE_TOO_LARGE",
    "REDIRECT_REJECTED", "ENCODING_REJECTED", "RESPONSE_LENGTH_MISMATCH"}
TRANSPORT_CODES = STOP_CODES - {"PARSE_FAILURE", "HTTP_REJECTED", "PROVIDER_REJECTED",
                               "AUTHENTICATION_FAILED", "ENTITLEMENT_DENIED", "RATE_LIMITED"}


def input_clock(value):
    try:
        return clock(value)
    except (RecordError, OverflowError):
        raise RecordError("MINUTE_CLOCK_INVALID") from None


def response_status(status, raw):
    """Recorded provider failures are data, distinct from today's store failure."""
    if status != 200:
        return {401: "AUTHENTICATION_FAILED", 403: "ENTITLEMENT_DENIED", 429: "RATE_LIMITED"}.get(status, "HTTP_REJECTED"), None
    try:
        body = _json(raw)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return "PARSE_FAILURE", None
    if not isinstance(body, dict) or type(body.get("code")) is not int:
        return "PARSE_FAILURE", None
    if body["code"] not in (0, 200):
        return {401: "AUTHENTICATION_FAILED", 403: "ENTITLEMENT_DENIED", 429: "RATE_LIMITED"}.get(body["code"], "PROVIDER_REJECTED"), None
    data = body.get("data")
    if not isinstance(data, list) or data and not any(isinstance(row, dict)
            and isinstance(row.get("symbol"), str) and isinstance(row.get("items"), list) for row in data):
        return "PARSE_FAILURE", None
    return "RESPONSE_SAVED", data


def response_record(index, request, raw, *, http_status, requested_at, received_at):
    """Adapt caller-supplied original UTF-8 bytes, not a decoded SDK object."""
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "MINUTE_ORIGINAL_BYTES_REQUIRED")
    require(type(http_status) is int and 100 <= http_status <= 599, "MINUTE_HTTP_STATUS")
    try:
        body = raw.decode("utf-8")
    except UnicodeError:
        raise RecordError("MINUTE_ORIGINAL_UTF8_REQUIRED") from None
    outcome, _ = response_status(http_status, raw)
    return {"index": index, "request": deepcopy(request), "state": "HTTP_RESPONSE",
        "requested_at": requested_at, "received_at": received_at, "http_status": http_status,
        "body": body, "bytes": len(raw), "sha256": digest(raw), "outcome": outcome}


def validate_input(payload, *, retained=False):
    require(isinstance(payload, dict) and set(payload) == {"schema", "auction_reference", "market_session",
        "started_at", "finished_at", "records", "stopped"} and payload["schema"] == INPUT_SCHEMA,
        "MINUTE_INPUT_SCHEMA")
    require(len(encoded(payload)) <= MAX_BYTES, "MINUTE_INPUT_BOUND")
    ref = SourceRef.from_dict(payload["auction_reference"])
    require(ref.selection == Selection(), "MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED")
    target = trade_day(payload["market_session"])
    begin, finish = input_clock(payload["started_at"]), input_clock(payload["finished_at"])
    require(begin <= finish and finish - begin <= timedelta(minutes=10)
            and begin >= datetime.combine(target, time(9, 41), tzinfo=ZONE), "MINUTE_CAPTURE_CLOCK")
    records = payload["records"]
    require(isinstance(records, list) and len(records) <= MAX_BATCHES, "MINUTE_RECORD_BOUND")
    require(payload["stopped"] is None or isinstance(payload["stopped"], str)
            and payload["stopped"] in STOP_CODES, "MINUTE_STOP_REASON")
    last, stopped, seen = begin, None, set()
    responses = {}
    for index, record in enumerate(records):
        common = {"index", "request", "state", "requested_at", "received_at", "outcome"}
        require(isinstance(record, dict) and type(record.get("index")) is int and record["index"] == index
                and isinstance(record.get("outcome"), str)
                and stopped is None, "MINUTE_RECORD_ORDER_OR_STOP")
        request = record.get("request")
        require(isinstance(request, dict) and isinstance(request.get("parameters"), dict), "MINUTE_REQUEST")
        expected = minute_request(request["parameters"].get("symbols"), payload["market_session"])
        # Canonical encoding distinguishes bool / integer / float parameters.
        require(encoded(request) == encoded(expected), "MINUTE_REQUEST")
        names = request["parameters"]["symbols"]
        require(not seen.intersection(names), "MINUTE_DUPLICATE_REQUEST_SYMBOL")
        seen.update(names)
        asked, got = input_clock(record.get("requested_at")), input_clock(record.get("received_at"))
        require(last <= asked <= got <= finish, "MINUTE_REQUEST_CLOCK")
        last = got
        if record.get("state") == "HTTP_RESPONSE":
            require(set(record) == common | {"http_status", "body", "bytes", "sha256"}
                    and type(record["http_status"]) is int and 100 <= record["http_status"] <= 599
                    and type(record["bytes"]) is int and 0 < record["bytes"] <= MAX_BYTES
                    and isinstance(record["sha256"], str) and re.fullmatch(r"[a-f0-9]{64}", record["sha256"]),
                    "MINUTE_RESPONSE_RECORD")
            if retained:
                require(record["body"] == {"source": index} and type(record["body"].get("source")) is int,
                        "MINUTE_SOURCE_SLOT")
                require(record["outcome"] in STOP_CODES | {"RESPONSE_SAVED"}, "MINUTE_RESPONSE_OUTCOME")
            else:
                require(isinstance(record["body"], str), "MINUTE_ORIGINAL_TEXT_REQUIRED")
                raw = record["body"].encode("utf-8")
                require(len(raw) == record["bytes"] and digest(raw) == record["sha256"], "MINUTE_ORIGINAL_CHANGED")
                outcome, data = response_status(record["http_status"], raw)
                require(record["outcome"] == outcome, "MINUTE_RESPONSE_OUTCOME")
                responses[index] = data
        else:
            require(record.get("state") == "TRANSPORT_FAILURE" and set(record) == common
                    and record["outcome"] in TRANSPORT_CODES, "MINUTE_TRANSPORT_RECORD")
        if record["outcome"] != "RESPONSE_SAVED":
            stopped = record["outcome"]
    require(stopped == payload["stopped"], "MINUTE_STOP_REASON")
    require(sum(r["bytes"] for r in records if r["state"] == "HTTP_RESPONSE") <= MAX_BYTES,
            "MINUTE_CUSTODY_TOTAL_BOUND")
    return responses


def capture_identity(payload):
    value = deepcopy(payload)
    for record in value["records"]:
        record.pop("body", None)
    return digest(encoded(value))


def qualify_plan(payload, seed):
    plan = minute_plan(seed, payload["auction_reference"])
    require(payload["market_session"] == plan["market_session"], "MINUTE_SEED_SESSION")
    records = payload["records"]
    require(len(records) <= len(plan["batches"]) and all(
        encoded(record["request"]) == encoded(plan["batches"][index]) for index, record in enumerate(records)),
        "MINUTE_ORIGINAL_COHORT_REQUEST_ORDER")
    require(len(records) == len(plan["batches"]) or payload["stopped"] is not None,
            "MINUTE_UNEXPLAINED_UNATTEMPTED_BATCH")
    return plan


def minute_view(payload, seed, *, retained=False, bodies=None, unavailable=(), offset=0, limit=50, entity_id=None):
    responses = validate_input(payload, retained=retained)
    plan = qualify_plan(payload, seed)
    if retained:
        for record in payload["records"]:
            if record["state"] != "HTTP_RESPONSE" or record["index"] in unavailable:
                continue
            raw = bodies[record["index"]]
            require(type(raw) is bytes and len(raw) == record["bytes"]
                    and digest(raw) == record["sha256"], "MINUTE_ORIGINAL_CHANGED")
            state, data = response_status(record["http_status"], raw)
            require(state == record["outcome"], "MINUTE_RESPONSE_OUTCOME")
            responses[record["index"]] = data
    return minute_observation(seed, plan, payload, responses, unavailable=unavailable,
                              offset=offset, limit=limit, entity_id=entity_id)
