"""The v1 single-stock route retains actual bodies, close and exact Store identity."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Inexact, localcontext
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application.ftshare_daily import FTShareDailyError, validate_batch
from decision_kernel.application.ftshare_single import (
    CAPTURE_SCHEMA, ENDPOINT, VIEW_SCHEMA, capture_single, restore_single,
    single_request, source_for_single_capture, validate_single,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Selection, SourceRef, digest, encoded
from test_market_custody import MemoryBackend


def millis(day, clock="00:00:00.000"):
    stamp = datetime.fromisoformat(day + "T" + clock + "+08:00")
    return (stamp - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)


SCOPE = {"symbol": "000001.SZ", "since": millis("2026-10-14"),
         "until": millis("2026-10-16", "23:59:59.999"), "adjust_kind": "none"}
CLOCK = "2026-10-19T02:00:00+08:00"


def bar(day="2026-10-16"):
    return {"open": "12.00", "high": "12.30", "low": "11.90", "close": "12.10",
            "volume": 123456, "turnover": "1493817.60",
            "ts_millis_open": millis(day, "09:30:00.000"),
            "ts_millis": millis(day, "15:00:00.000")}


def captured(payload=None, **changes):
    raw = json.dumps([bar()] if payload is None else payload, indent=2).encode() + b"\n"
    return capture_single(raw, acquired_at=CLOCK, **(SCOPE | changes))


@pytest.mark.parametrize("wrapped", [None, {"code": 0}, {"code": 200},
                                    {"code": 0, "message": None},
                                    {"code": 200, "message": "success"}])
def test_bare_and_observed_envelopes_share_rows_but_retain_the_actual_v1_original(wrapped):
    payload = [bar()] if wrapped is None else {**wrapped, "data": [bar()]}
    capture = captured(payload)
    view = capture["normalized"]
    assert json.loads(capture["body_utf8"]) == payload
    assert capture["schema"] == CAPTURE_SCHEMA and view["schema"] == VIEW_SCHEMA
    assert capture["request"] == view["request"] == single_request(**SCOPE)
    assert view["request"]["url"] == ENDPOINT
    assert view["request"]["parameters"]["symbol"] == SCOPE["symbol"]
    assert "symbols" not in view["request"]["parameters"]
    assert capture["wire_sha256"] == digest(capture["body_utf8"].encode())
    assert capture["wire_bytes"] == len(capture["body_utf8"].encode())
    assert capture["request_sha256"] == digest(encoded(single_request(**SCOPE)))
    assert capture["transport_attested"] is False
    assert view["series"][SCOPE["symbol"]][0]["close"] == "12.10"


@pytest.mark.parametrize("payload", [
    {}, {"data": []}, {"code": 200}, {"code": "200", "data": []},
    {"code": True, "data": []}, {"code": 403, "data": []},
    {"code": 200, "data": {"items": []}}, {"code": 200, "data": [], "total": 0},
    {"code": 200, "data": [], "message": {}}, None, "[]", (bar(),),
    {"code": 200, "message": "success", "data": [["000001.SZ", [bar()]]]},
])
def test_refuses_failed_or_ambiguous_envelopes_and_v2_pairs(payload):
    with pytest.raises(FTShareDailyError):
        validate_single(payload, **SCOPE)


def test_close_decimal_and_row_quarantine_reuse_existing_daily_semantics():
    exact = "12.123456789012345678901234567890123456789"
    good, bad = bar("2026-10-14"), bar()
    good["close"] = exact
    good["latest"] = "999.00"  # The route continues to consume close, never latest.
    bad["close"] = "99.00"
    rows = [bad, good]
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        view = validate_single(rows, **SCOPE)
    batch = validate_batch({"code": 200, "message": "success", "data": [[SCOPE["symbol"], rows]]},
        symbols=[SCOPE["symbol"]], **{k: v for k, v in SCOPE.items() if k != "symbol"})
    assert view["series"] == batch["series"]
    assert view["quarantined"] == batch["quarantined"]
    assert view["counts"] == batch["counts"]
    assert view["series"][SCOPE["symbol"]][0]["close"] == exact
    assert view["counts"]["quarantined_rows"] == 1
    assert view["missing"]["calendar_sessions"] == "NOT_ASSESSED"


@pytest.mark.parametrize("rows", [
    [bar(), bar()], [bar() | {"symbol": "600000.SH"}],
    [bar("2026-10-13")], [bar() | {"ts_millis": millis("2026-10-17")}],
])
def test_identity_date_and_duplicate_failures_are_not_partial_success(rows):
    with pytest.raises(FTShareDailyError):
        validate_single(rows, **SCOPE)


def test_empty_forward_and_raw_views_keep_the_existing_absence_and_adjustment_limits():
    empty = validate_single([], **SCOPE)
    assert empty["counts"]["observed_rows"] == 0
    assert empty["missing"]["empty_symbols"] == [SCOPE["symbol"]]
    raw, forward = captured(), captured(adjust_kind="forward")
    assert raw["normalized"]["series"] == forward["normalized"]["series"]
    assert raw["request_sha256"] != forward["request_sha256"]
    assert forward["normalized"]["adjustment"] == "provider-forward"
    assert "NO_FORWARD_SERIES_STITCHING_OR_FACTOR_RECONSTRUCTION" in forward["normalized"]["limits"]


@pytest.mark.parametrize("changes", [
    {"symbol": "000001"}, {"symbol": "920202.BJ"}, {"symbol": ["000001.SZ"]},
    {"adjust_kind": "backward"}, {"since": True}, {"since": SCOPE["since"] + 1},
    {"since": millis("2026-06-01")}, {"until": SCOPE["until"] - 1},
])
def test_single_request_retains_existing_finite_daily_scope(changes):
    with pytest.raises(FTShareDailyError):
        single_request(**(SCOPE | changes))


@pytest.mark.parametrize("raw", [
    b"\xff", b"[", b'{"code":0,"code":200,"data":[]}', b'[NaN]', b'[Infinity]', b"",
])
def test_capture_refuses_ambiguous_or_invalid_wire_json(raw):
    with pytest.raises(FTShareDailyError):
        capture_single(raw, acquired_at=CLOCK, **SCOPE)


@pytest.mark.parametrize("clock", ["2026-10-19", "2026-10-19T02:00:00", "bad", None])
def test_capture_requires_an_actual_timezone_aware_acquisition_clock(clock):
    with pytest.raises(FTShareDailyError):
        capture_single(b"[]", acquired_at=clock, **SCOPE)


def test_source_input_preserves_v1_body_url_and_labels_a_normalized_excerpt():
    capture = captured({"code": 0, "message": None, "data": [bar()]})
    wire = source_for_single_capture(capture)
    assert wire.text.encode() == capture["body_utf8"].encode()
    assert wire.custody == "provided" and wire.kind == "content"
    parsed = urlsplit(wire.origin)
    assert parsed.scheme + "://" + parsed.netloc + parsed.path == ENDPOINT
    assert parse_qs(parsed.query) == {"symbol": [SCOPE["symbol"]], "interval_unit": ["day"],
        "adjust_kind": ["none"], "since_ts_millis": [str(SCOPE["since"])],
        "until_ts_millis": [str(SCOPE["until"])]}
    assert "SCHEMA_FTSHARE_SINGLE_DAILY_CAPTURE_V1" in wire.limitations
    excerpt = source_for_single_capture(capture, representation="normalized")
    assert excerpt.custody == "excerpt" and excerpt.origin == wire.origin
    assert json.loads(excerpt.text)["qualified"] == capture["normalized"]
    assert "NORMALIZED_EXCERPT_NOT_ORIGINAL_HTTP_BODY" in excerpt.limitations
    assert excerpt.source_id != wire.source_id


@pytest.mark.parametrize("part", ["body_utf8", "wire_sha256", "wire_bytes", "request",
                                  "normalized", "request_sha256", "schema", "transport_attested"])
def test_source_input_rebuild_rejects_mutated_capture_identity_or_prices(part):
    capture = captured()
    if part == "request":
        capture[part]["url"] = ENDPOINT.replace("/v1/", "/v2/")
    elif part == "normalized":
        capture[part]["series"][SCOPE["symbol"]][0]["close"] = "999.00"
    elif part == "wire_bytes":
        capture[part] += 1
    elif part == "body_utf8":
        capture[part] += " "
    elif part == "transport_attested":
        capture[part] = 0  # Numeric equality must not replace the actual boolean.
    else:
        capture[part] = "changed"
    with pytest.raises(FTShareDailyError, match="FTSHARE_SINGLE_CAPTURE_INTEGRITY_MISMATCH$"):
        source_for_single_capture(capture)


def retained(source=None):
    capture = captured()
    backend = MemoryBackend()
    reference = VersionedStore(backend).put_source("synthetic-single-source",
        source if source is not None else source_for_single_capture(capture))
    return capture, reference, VersionedStore(MemoryBackend(backend.shared))


def test_independent_store_reader_restores_original_v1_capture_and_exact_close():
    capture, reference, reader = retained()
    restored = restore_single(asdict(reference), reader, request=capture["request"])
    assert restored == capture
    assert restored["normalized"]["series"][SCOPE["symbol"]][0]["close"] == "12.10"


@pytest.mark.parametrize("change", ["url", "symbol", "adjust_kind", "extra", "interval_unit"])
def test_restore_refuses_request_substitution(change):
    capture, reference, reader = retained()
    request = deepcopy(capture["request"])
    if change == "url":
        request["url"] = ENDPOINT.replace("/v1/", "/v2/")
    elif change == "extra":
        request["parameters"]["limit"] = 1
    else:
        request["parameters"][change] = {"symbol": "600000.SH", "adjust_kind": "forward",
                                         "interval_unit": "week"}[change]
    with pytest.raises(FTShareDailyError):
        restore_single(reference, reader, request=request)


@pytest.mark.parametrize("kind", ["excerpt", "wrong-origin", "wrong-limitations", "selection", "foreign"])
def test_restore_does_not_accept_excerpt_or_relabelled_original(kind):
    capture = captured()
    source = source_for_single_capture(capture, representation="normalized" if kind == "excerpt" else "wire")
    if kind == "wrong-origin":
        source = replace(source, origin=source.origin.replace("/v1/", "/v2/"))
    if kind == "wrong-limitations":
        source = replace(source, limitations=())
    _, reference, reader = retained(source)
    if kind == "selection":
        reference = SourceRef(reference.manifest, Selection("lines", 1, 1))
    if kind == "foreign":
        reference = SourceRef(replace(reference.manifest, repository="other/repository"))
    with pytest.raises(FTShareDailyError):
        restore_single(reference, reader, request=capture["request"])
