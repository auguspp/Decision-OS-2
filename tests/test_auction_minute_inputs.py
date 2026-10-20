"""Given original minute bytes: exact fields, clocks and complete seed groups."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
import json

import pytest

from decision_kernel.application.auction_minute_inputs import (
    INPUT_SCHEMA, minute_view, response_record, validate_input,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.domain.records import BlobRef, RecordError, SourceRef, encoded
from decision_kernel.observations.auction_minute import minute_plan, minute_request, session_window
from decision_kernel.observations.opening_auction import GROUPS
from test_auction_inputs import ROOT, REGISTRY, POOL_FIELDS, auction_payload, capture

START = "2026-10-20T01:41:00+00:00"
FINISH = "2026-10-20T01:43:00+00:00"
DUMMY_REF = asdict(SourceRef(BlobRef("synthetic-git:minute-input", "a" * 40,
                                    "source-views/synthetic/" + "c" * 64 + "/manifest.json", "b" * 64)))


def seed_payload(symbol_count=8):
    result = auction_payload(close=False)
    rows = deepcopy(result["pool_result"]["capture"]["pages"][0]["table"]["items"])
    rows.append(["20261019", "830001.BJ", "合成北交所", None, "20", 1, "U"])
    for index in range(max(0, symbol_count - 8)):
        rows.append(["20261019", str(601000 + index) + ".SH", "合成扩展" + str(index), None, "20", 1, "U"])
    result["pool_result"] = capture("limit_list_d", POOL_FIELDS, rows, {"trade_date": "20261019"}, limit=500)
    return result


def minute_payload(seed=None, reference=None):
    seed = read_market_payload(seed_payload(), REGISTRY) if seed is None else seed
    reference = DUMMY_REF if reference is None else reference
    plan = minute_plan(seed, reference)
    records = []
    for index, request in enumerate(plan["batches"]):
        body = {"code": 0, "data": [{"symbol": name, "total": 999, "items": [{
            "ts_millis": plan["target_end_ms"], "ts_millis_open": plan["target_open_ms"], "close": "101.25"}]}
            for name in request["parameters"]["symbols"]]}
        asked = datetime.fromisoformat(START) + timedelta(seconds=index * 2)
        records.append(response_record(index, request, json.dumps(body, ensure_ascii=False, indent=2).encode(),
            http_status=200, requested_at=asked.isoformat(), received_at=(asked + timedelta(seconds=1)).isoformat()))
    return {"schema": INPUT_SCHEMA, "auction_reference": deepcopy(reference), "market_session": seed["market_session"],
            "started_at": START, "finished_at": FINISH, "records": records, "stopped": None}


def replace_body(payload, body, *, index=0, status=200):
    record = payload["records"][index]
    raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
    payload["records"][index] = response_record(index, record["request"], raw, http_status=status,
        requested_at=record["requested_at"], received_at=record["received_at"])
    payload["stopped"] = None if payload["records"][index]["outcome"] == "RESPONSE_SAVED" else payload["records"][index]["outcome"]


def test_full_five_groups_bj_unknown_and_navigation_are_original_scope():
    seed = read_market_payload(seed_payload(), REGISTRY)
    payload = minute_payload(seed)
    before = encoded(payload)
    result = minute_view(payload, seed, limit=1)
    filtered = minute_view(payload, seed, entity_id="830001.BJ", limit=1)
    assert result["cohort_denominator"] == 9 and result["observed_0940"] == 8
    assert set(result["groups"]) == set(GROUPS) and all(g["denominator"] for g in result["groups"].values())
    assert filtered["entries"][0]["group"] == "STATIC_EXCLUDED"
    assert filtered["entries"][0]["minute"]["status"] == "EXACT_0940_CLOSE_OBSERVED"
    assert result["cohort_rows"] == filtered["cohort_rows"] and result["groups"] == filtered["groups"]
    assert result["view_version"] == filtered["view_version"] and len(result["entries"]) == 1
    assert next(r for r in result["cohort_rows"] if r["entity_id"] is None)["minute"]["status"] == "COHORT_IDENTITY_UNRESOLVED"
    assert result["cross_provider_return"] == "NOT_COMPUTED" and result["source_requests"] == 0
    assert result["acquisition"]["capture_timing"] == "HISTORICAL_OR_LATE_NOT_0940_DISCOVERY"
    assert all(not r["minute"]["return_count_consistent"] for r in filtered["entries"])
    assert encoded(payload) == before


def test_twenty_twenty_seventeen_plan_and_original_321st_remains_unrequested():
    seed = read_market_payload(seed_payload(57), REGISTRY)
    plan = minute_plan(seed, DUMMY_REF)
    assert [len(r["parameters"]["symbols"]) for r in plan["batches"]] == [20, 20, 17]
    assert plan["cohort_denominator"] == 58 and plan["symbol_encoding"] == "REPEATED_QUERY_PARAMETERS"
    seed = read_market_payload(seed_payload(321), REGISTRY)
    payload = minute_payload(seed)
    result = minute_view(payload, seed)
    assert len(payload["records"]) == 16 and result["cohort_denominator"] == 322
    assert result["observed_0940"] == 320 and len(result["unrequested_symbols"]) == 1
    excluded = next(r for r in result["cohort_rows"] if r["entity_id"] in result["unrequested_symbols"])
    assert excluded["minute"]["status"] == "REQUEST_BUDGET_NOT_REQUESTED"
    assert sum(g["denominator"] for g in result["groups"].values()) == 322


@pytest.mark.parametrize("kind,expected", [("symbol", "SYMBOL_AMBIGUOUS"), ("minute", "TARGET_MINUTE_AMBIGUOUS"),
    ("missing", "SYMBOL_NOT_RETURNED"), ("foreign", "EXACT_0940_CLOSE_OBSERVED")])
def test_duplicate_even_identical_and_foreign_rows_never_change_the_cohort(kind, expected):
    seed = read_market_payload(seed_payload(), REGISTRY)
    payload = minute_payload(seed)
    body = json.loads(payload["records"][0]["body"])
    name = body["data"][0]["symbol"]
    if kind == "symbol":
        body["data"].append(deepcopy(body["data"][0]))
    elif kind == "minute":
        body["data"][0]["items"].append(deepcopy(body["data"][0]["items"][0]))
    elif kind == "missing":
        body["data"].pop(0)
    else:
        body["data"].append({"symbol": "699999.SH", "items": []})
    replace_body(payload, body)
    result = minute_view(payload, seed)
    assert next(r for r in result["cohort_rows"] if r["entity_id"] == name)["minute"]["status"] == expected
    assert result["cohort_denominator"] == 9 and all(r["entity_id"] != "699999.SH" for r in result["cohort_rows"])
    assert bool(result["unexpected_rows"]) is (kind == "foreign")


@pytest.mark.parametrize("field,value,expected", [
    ("ts_millis", "1792460400000", "TARGET_MINUTE_NOT_RETURNED"),
    ("ts_millis", True, "TARGET_MINUTE_NOT_RETURNED"),
    ("ts_millis_open", 0, "TARGET_MINUTE_TIME_UNQUALIFIED"),
    ("ts_millis_open", "1792460340000", "TARGET_MINUTE_TIME_UNQUALIFIED"),
    ("close", "0", "TARGET_CLOSE_INVALID"), ("close", "-1", "TARGET_CLOSE_INVALID"),
    ("close", "NaN", "TARGET_CLOSE_INVALID"), ("close", "1e13", "TARGET_CLOSE_INVALID"),
    ("close", True, "TARGET_CLOSE_INVALID"),
])
def test_only_strict_exact_boundaries_and_positive_bounded_close_qualify(field, value, expected):
    seed = read_market_payload(seed_payload(), REGISTRY)
    payload = minute_payload(seed)
    body = json.loads(payload["records"][0]["body"])
    name = body["data"][0]["symbol"]
    body["data"][0]["items"][0][field] = value
    replace_body(payload, body)
    result = minute_view(payload, seed)
    row = next(r for r in result["cohort_rows"] if r["entity_id"] == name)
    assert row["minute"]["status"] == expected and row["minute"]["close_0940"] is None
    assert result["observed_0940"] == 7


def test_request_identity_uses_unique_symbols_exact_window_and_strict_integer_parameters():
    payload = minute_payload()
    for field, value in (("interval_value", True), ("limit", 1000.0), ("until_ts_millis", 1), ("adjust_kind", "none")):
        bad = deepcopy(payload)
        bad["records"][0]["request"]["parameters"][field] = value
        with pytest.raises(RecordError, match="MINUTE_REQUEST"):
            validate_input(bad)
    with pytest.raises(RecordError, match="MINUTE_REQUEST_SYMBOLS"):
        minute_request(["600001.SH", "600001.SH"], "2026-10-20")
    with pytest.raises(RecordError, match="MINUTE_REQUEST_WINDOW"):
        session_window("0001-02-01")
    window = session_window("2026-10-20")
    assert window["until_ts_millis"] - window["since_ts_millis"] == 660000
    assert window["target_end_ms"] - window["target_open_ms"] == 60000


@pytest.mark.parametrize("kind", ["early", "too-long", "reverse", "utc-overflow", "missing-prefix", "changed-original", "cohort-order"])
def test_receipt_clocks_bytes_and_seed_plan_cannot_be_rewritten(kind):
    seed = read_market_payload(seed_payload(21), REGISTRY)
    payload = minute_payload(seed)
    if kind == "early":
        payload["started_at"] = "2026-10-20T09:40:00+08:00"
    elif kind == "too-long":
        payload["finished_at"] = "2026-10-20T09:51:01+08:00"
    elif kind == "reverse":
        payload["records"][1]["requested_at"] = START
    elif kind == "utc-overflow":
        payload["started_at"] = "0001-01-01T00:00:00+00:01"
    elif kind == "missing-prefix":
        payload["records"].pop()
    elif kind == "changed-original":
        payload["records"][0]["body"] += " "
    else:
        payload["records"][0]["request"]["parameters"]["symbols"].reverse()
    with pytest.raises(RecordError, match="MINUTE_"):
        minute_view(payload, seed)


@pytest.mark.parametrize("status,code", [(403, "ENTITLEMENT_DENIED"), (429, "RATE_LIMITED"), (401, "AUTHENTICATION_FAILED")])
def test_old_source_refusal_stays_a_receipt_and_unattempted_peers_stay_visible(status, code):
    seed = read_market_payload(seed_payload(21), REGISTRY)
    payload = minute_payload(seed)
    replace_body(payload, {"message": "synthetic original refusal"}, status=status)
    payload["records"] = payload["records"][:1]
    result = minute_view(payload, seed)
    assert result["acquisition"]["source_stop"] == code and result["observed_0940"] == 0
    assert result["cohort_denominator"] == 22 and result["source_requests"] == 0
    states = [r["minute"]["status"] for r in result["cohort_rows"]]
    assert states.count(code) == 20 and states.count("NOT_REQUESTED_AFTER_SOURCE_STOP") == 1
    bad = deepcopy(payload)
    bad["records"].append(minute_payload(seed)["records"][1])
    with pytest.raises(RecordError, match="MINUTE_RECORD_ORDER_OR_STOP"):
        minute_view(bad, seed)


def test_transport_failure_has_no_invented_body_and_sdk_decoded_raw_is_not_bytes():
    seed = read_market_payload(seed_payload(), REGISTRY)
    payload = minute_payload(seed)
    record = payload["records"][0]
    payload["records"] = [{key: deepcopy(record[key]) for key in ("index", "request", "requested_at", "received_at")}]
    payload["records"][0].update(state="TRANSPORT_FAILURE", outcome="TRANSPORT_FAILURE")
    payload["stopped"] = "TRANSPORT_FAILURE"
    assert minute_view(payload, seed)["observed_0940"] == 0
    with pytest.raises(RecordError, match="MINUTE_ORIGINAL_BYTES_REQUIRED"):
        response_record(0, record["request"], json.loads(record["body"]), http_status=200,
                        requested_at=START, received_at=FINISH)
