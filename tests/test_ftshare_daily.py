"""FTShare daily protocol ownership: retire with its batch reader, not issue closure."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Inexact, localcontext
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application.ftshare_daily import (
    ENDPOINT, FTShareDailyError, batch_request, capture_batch, source_for_capture,
    validate_batch,
)


def millis(day, clock="00:00:00.000"):
    stamp = datetime.fromisoformat(day + "T" + clock + "+08:00")
    return (stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


SINCE = millis("2026-10-14")
UNTIL = millis("2026-10-16", "23:59:59.999")
SYMBOLS = ["600000.SH", "000001.SZ"]
SCOPE = {"symbols": SYMBOLS, "since": SINCE, "until": UNTIL, "adjust_kind": "none"}


def bar(day="2026-10-16"):
    return {"open": "12.00", "high": "12.30", "low": "11.90", "close": "12.10",
            "volume": 123456, "turnover": "1493817.60",
            "ts_millis_open": millis(day, "09:30:00.000"),
            "ts_millis": millis(day, "15:00:00.000")}


def payload():
    return {"code": 200, "message": "success",
            "data": [["600000.SH", []],
                     ["000001.SZ", [bar(), bar("2026-10-14")]]]}


def capture():
    raw = json.dumps(payload(), indent=2).encode() + b"\n"
    return capture_batch(raw, acquired_at="2026-10-18T23:00:00+08:00", **SCOPE)


def test_preserves_absence_and_unknown_trade_status_without_filling_dates():
    view = validate_batch(payload(), **SCOPE)
    assert view["symbols"] == ["000001.SZ", "600000.SH"]
    rows = view["series"]["000001.SZ"]
    assert [row["trade_date"] for row in rows] == ["2026-10-14", "2026-10-16"]
    assert rows[0]["close"] == "12.10"
    assert rows[0]["turnover"] == "1493817.60"
    assert rows[0]["turnover_unit"] == "CNY"
    assert rows[0]["volume"] == 123456 and rows[0]["volume_unit"] == "UNKNOWN"
    assert rows[0]["trade_status"] == "UNKNOWN"
    assert view["series"]["600000.SH"] == []
    assert view["missing"]["empty_symbols"] == ["600000.SH"]
    assert view["missing"]["calendar_sessions"] == "NOT_ASSESSED"
    assert view["counts"] == {"requested_symbols": 2, "returned_symbols": 2,
                              "total_rows": 2, "observed_rows": 2, "quarantined_rows": 0}


def test_normalization_is_order_independent_and_prices_ignore_ambient_decimal_precision():
    original = payload()
    number = "12.123456789012345678901234567890123456789"
    original["data"][1][1][0]["close"] = number
    original["data"][1][1][0]["ts_millis"] = str(millis("2026-10-16", "15:00:00.000"))
    original["data"][1][1][0]["symbol"] = "000001.SZ"
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        view = validate_batch(original, **SCOPE)
    assert view["series"]["000001.SZ"][1]["close"] == number
    shuffled = deepcopy(original)
    shuffled["data"].reverse()
    shuffled["data"][0][1].reverse()
    assert validate_batch(shuffled, **(SCOPE | {"symbols": list(reversed(SYMBOLS))})) == view
    json.dumps(view, allow_nan=False)  # Actual consumer-facing output is JSON safe.


def test_forward_keeps_same_request_convention_separate_from_raw_and_pit():
    raw = validate_batch(payload(), **SCOPE)
    forward = validate_batch(payload(), **(SCOPE | {"adjust_kind": "forward"}))
    assert raw["adjustment"] == "raw"
    assert forward["adjustment"] == "provider-forward"
    assert raw["request_sha256"] != forward["request_sha256"]
    assert forward["return_semantics"] == "price-ratio-not-total-return"
    assert "PROVIDER_FORWARD_SAME_REQUEST_NOT_POINT_IN_TIME_FACTORS" in forward["limits"]
    assert "NO_FORWARD_SERIES_STITCHING_OR_FACTOR_RECONSTRUCTION" in forward["limits"]
    assert raw["series"] == forward["series"]  # No factor reconstruction or price rewrite.


@pytest.mark.parametrize("changes,reason", [
    ({"symbols": []}, "SYMBOLS_BOUND"),
    ({"symbols": ["000001.SZ"] * 101}, "SYMBOLS_BOUND"),
    ({"symbols": ["000001.SZ", "000001.SZ"]}, "DUPLICATE_REQUEST_SYMBOL"),
    ({"symbols": "000001.SZ"}, "SYMBOLS_BOUND"),
    ({"symbols": ["600000.SSE"]}, "SYMBOL_INVALID"),
    ({"symbols": ["600000.sh"]}, "SYMBOL_INVALID"),
    ({"symbols": [" 600000.SH"]}, "SYMBOL_INVALID"),
    ({"symbols": ["６０００００.SH"]}, "SYMBOL_INVALID"),
    ({"since": str(SINCE)}, "RANGE_INVALID"),
    ({"since": True}, "RANGE_INVALID"),
    ({"until": UNTIL + 1}, "WHOLE_SHANGHAI_DAYS_REQUIRED"),
    ({"since": SINCE + 1}, "WHOLE_SHANGHAI_DAYS_REQUIRED"),
    ({"since": UNTIL - 999}, "WHOLE_SHANGHAI_DAYS_REQUIRED"),
    ({"until": millis("2026-10-13", "23:59:59.999")}, "RANGE_INVALID"),
    ({"since": millis("2026-06-19")}, "RANGE_INVALID"),
    ({"adjust_kind": "backward"}, "ADJUST_KIND_INVALID"),
])
def test_request_identity_bounds_are_strict(changes, reason):
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_" + reason + "$" ):
        batch_request(**(SCOPE | changes))


def test_request_accepts_exact_119_days_and_100_symbols_without_limit():
    last = datetime(2026, 10, 16)
    first = (last - timedelta(days=118)).date().isoformat()
    request = batch_request(symbols=[f"{n:06}.SH" for n in range(100)],
                            since=millis(first), until=UNTIL, adjust_kind="none")
    assert len(request["parameters"]["symbols"]) == 100
    assert request["url"] == ENDPOINT and request["method"] == "GET"
    assert set(request["parameters"]) == {"symbols", "interval_unit", "adjust_kind", "since", "until"}


@pytest.mark.parametrize("replacement,reason", [
    ({"code": 0}, "RESPONSE_NOT_SUCCESS"),
    ({"code": 200.0}, "RESPONSE_NOT_SUCCESS"),
    ({"message": "partial"}, "RESPONSE_NOT_SUCCESS"),
    ({"data": {"items": [], "next": 1}}, "DATA_INVALID"),
    ({"has_more": True}, "ENVELOPE_INVALID"),
    ({"data": [["000001.SZ", []]]}, "MISSING_RESPONSE_SYMBOL"),
    ({"data": [["000001.SZ", []], ["000001.SZ", []]]}, "DUPLICATE_RESPONSE_SYMBOL"),
    ({"data": [["000001.SZ", []], ["600001.SH", []]]}, "UNREQUESTED_SYMBOL"),
    ({"data": [["000001.SZ", [], 1]]}, "PAIR_INVALID"),
])
def test_envelope_or_batch_truncation_is_not_partial_success(replacement, reason):
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_" + reason + "$" ):
        validate_batch(payload() | replacement, **SCOPE)


@pytest.mark.parametrize("changes,reason", [
    ({"ts_millis": True}, "TIMESTAMP_INVALID"),
    ({"ts_millis": 1792134000000.0}, "TIMESTAMP_INVALID"),
    ({"ts_millis": "1792134000000.0"}, "TIMESTAMP_INVALID"),
    ({"ts_millis": "tomorrow"}, "TIMESTAMP_INVALID"),
    ({"ts_millis_open": None}, "TIMESTAMP_INVALID"),
    ({"ts_millis": 10**30}, "TIMESTAMP_INVALID"),
    ({"ts_millis_open": millis("2026-10-13", "09:30:00.000")}, "ROW_TIME_OUT_OF_RANGE"),
    ({"ts_millis_open": millis("2026-10-15", "09:30:00.000")}, "ROW_TIME_DAY_MISMATCH"),
    ({"ts_millis_open": millis("2026-10-16", "16:00:00.000")}, "ROW_TIME_OUT_OF_RANGE"),
    ({"symbol": "600000.SH"}, "ROW_SYMBOL_MISMATCH"),
    ({"symbol": "000001.SZSE"}, "SYMBOL_INVALID"),
])
def test_unreliable_time_or_security_identity_rejects_whole_batch(changes, reason):
    data = payload()
    data["data"][1][1][0].update(changes)
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_" + reason + "$" ):
        validate_batch(data, **SCOPE)


def test_duplicate_day_is_rejected_even_when_first_ohlc_would_be_quarantined():
    data = payload()
    data["data"][1][1] = [bar(), bar()]
    data["data"][1][1][0]["close"] = "NaN"
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_DUPLICATE_DATE$"):
        validate_batch(data, **SCOPE)


@pytest.mark.parametrize("changes,reason", [
    ({"close": "NaN"}, "PRICE_INVALID"),
    ({"close": "Infinity"}, "PRICE_INVALID"),
    ({"close": "-12"}, "PRICE_INVALID"),
    ({"close": "0"}, "PRICE_INVALID"),
    ({"close": "1e100"}, "PRICE_INVALID"),
    ({"close": 12.1}, "PRICE_TYPE"),
    ({"close": None}, "PRICE_TYPE"),
    ({"close": " 12.1"}, "PRICE_TYPE"),
    ({"low": "12.20"}, "OHLC_INVALID"),
    ({"high": "11.80"}, "OHLC_INVALID"),
    ({"volume": True}, "VOLUME_INVALID"),
    ({"volume": -1}, "VOLUME_INVALID"),
    ({"volume": 12.0}, "VOLUME_INVALID"),
    ({"volume": "123456"}, "VOLUME_INVALID"),
    ({"turnover": "NaN"}, "TURNOVER_INVALID"),
    ({"turnover": "-1"}, "TURNOVER_INVALID"),
    ({"turnover": 1493817.6}, "TURNOVER_INVALID"),
])
def test_invalid_numeric_row_is_quarantined_at_its_day_only(changes, reason):
    data = payload()
    data["data"][1][1][0].update(changes)
    view = validate_batch(data, **SCOPE)
    assert [row["trade_date"] for row in view["series"]["000001.SZ"]] == ["2026-10-14"]
    assert view["quarantined"] == [{"symbol": "000001.SZ", "trade_date": "2026-10-16",
                                    "reason": "FTSHARE_DAILY_" + reason}]
    assert view["counts"]["total_rows"] == 2
    assert view["counts"]["observed_rows"] == view["counts"]["quarantined_rows"] == 1
    assert view["missing"]["empty_symbols"] == ["600000.SH"]


def test_zero_volume_turnover_are_values_and_not_suspension_evidence():
    data = payload()
    data["data"][1][1][0].update(volume=0, turnover="0.00")
    view = validate_batch(data, **SCOPE)
    assert view["counts"]["quarantined_rows"] == 0
    assert view["series"]["000001.SZ"][1]["trade_status"] == "UNKNOWN"


def test_wire_source_retains_exact_bytes_and_bound_request_without_claiming_transport():
    result = capture()
    raw = json.dumps(payload(), indent=2).encode() + b"\n"
    assert result["wire_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["wire_bytes"] == len(raw)
    assert result["transport_attested"] is False
    source = source_for_capture(result)
    assert source.custody == "provided" and source.text.encode() == raw
    assert "WIRE_SHA256:" + result["wire_sha256"] in source.limitations
    assert "REQUEST_SHA256:" + result["request_sha256"] in source.limitations
    params = parse_qs(urlsplit(source.origin).query)
    assert params["symbols"] == sorted(SYMBOLS)
    assert params["since_ts_millis"] == [str(SINCE)]
    assert params["until_ts_millis"] == [str(UNTIL)]
    assert "since" not in params and "until" not in params
    assert "limit" not in params


def test_unquoted_wire_decimal_is_preserved_without_binary_float_round_trip():
    number = "12.123456789012345678901234567890123456789"
    raw = json.dumps(payload()).encode().replace(b'"close": "12.10"',
                                                 b'"close": ' + number.encode(), 1)
    result = capture_batch(raw, acquired_at="2026-10-18T23:00:00+08:00", **SCOPE)
    assert result["normalized"]["series"]["000001.SZ"][1]["close"] == number
    assert source_for_capture(result).text.encode() == raw


def test_normalized_source_explicitly_records_excerpt_custody():
    result = capture()
    source = source_for_capture(result, representation="normalized")
    assert source.custody == "excerpt"
    assert "NORMALIZED_EXCERPT_NOT_ORIGINAL_HTTP_BODY" in source.limitations
    stored = json.loads(source.text)
    assert stored["schema"] == "ftshare-normalized-daily-excerpt-v1"
    assert stored["provider"] == "FTSHARE"
    assert stored["wire_sha256"] == result["wire_sha256"]
    assert stored["qualified"] == result["normalized"]


@pytest.mark.parametrize("representation", ["wire", "normalized"])
def test_existing_store_accepts_json_as_content_and_fresh_reader_recovers_it(representation):
    from decision_kernel.application.versioned_store import VersionedStore
    from test_market_custody import MemoryBackend
    backend = MemoryBackend()
    writer = VersionedStore(backend)
    source = source_for_capture(capture(), representation=representation)
    assert source.kind == "content"  # Existing dataset semantics are CSV-only.
    reference = writer.put_source("synthetic-ftshare-source", source)
    reader = VersionedStore(MemoryBackend(backend.shared))
    restored = reader.read_source(reference)
    assert restored["availability"] == "stored-representation"
    assert restored["data"].encode() == source.text.encode()


@pytest.mark.parametrize("field", ["wire_sha256", "request", "normalized"])
def test_source_builder_revalidates_capture_and_rejects_modified_projection(field):
    result = capture()
    if field == "wire_sha256":
        result[field] = "0" * 64
    elif field == "request":
        result[field]["url"] = "https://untrusted.invalid/batch"
    else:
        result[field]["series"]["000001.SZ"][0]["close"] = "999.00"
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_CAPTURE_INTEGRITY_MISMATCH$"):
        source_for_capture(result)


@pytest.mark.parametrize("raw,reason", [
    (b"\xff", "WIRE_JSON_INVALID"),
    (b'{"code": 200, "message": "success", "data": [], "code": 400}', "DUPLICATE_JSON_KEY"),
    (b'{"code": 200, "message": "success", "data": NaN}', "NONFINITE_JSON"),
    (b'{"code": 200, "message": "success", "data": [', "WIRE_JSON_INVALID"),
])
def test_capture_refuses_corrupt_ambiguous_or_nonstandard_json(raw, reason):
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_" + reason + "$" ):
        capture_batch(raw, acquired_at="2026-10-18T23:00:00+08:00", **SCOPE)


@pytest.mark.parametrize("clock", ["2026-10-18", "2026-10-18T23:00:00", "invalid"])
def test_capture_requires_timezone_aware_acquisition_time(clock):
    with pytest.raises(FTShareDailyError, match="FTSHARE_DAILY_ACQUISITION_CLOCK_INVALID$"):
        capture_batch(json.dumps(payload()).encode(), acquired_at=clock, **SCOPE)
