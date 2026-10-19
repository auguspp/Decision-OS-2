"""FTShare v1 single-stock daily bodies with their actual request identity.

The existing daily validator owns close, Decimal, dates and quarantined rows.
An in-memory adapter reuses those rules; neither the retained original nor its
request is rewritten as a v2 batch response. No source access occurs here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime
import json
from urllib.parse import urlencode

from decision_kernel.application.ftshare_daily import (
    FTShareDailyError, MAX_WIRE_BYTES, PROVIDER, _bounds, _json_object,
    _nonfinite, _symbol, validate_batch,
)
from decision_kernel.application.market_custody import _read_source, market_read_scope
from decision_kernel.domain.records import Selection, SourceInput, SourceRef, digest, encoded

ENDPOINT = "https://market.ft.tech/gateway/api/v1/market/data/stock-candlesticks"
VIEW_SCHEMA = "ftshare-qualified-single-daily-v1"
CAPTURE_SCHEMA = "ftshare-single-daily-capture-v1"


class FTShareSingleError(FTShareDailyError):
    """Stable single-route refusal without supplier prose or source values."""


def _require(ok, code):
    if not ok:
        raise FTShareSingleError("FTSHARE_SINGLE_" + code)


def single_request(*, symbol, since: int, until: int, adjust_kind: str = "none") -> dict:
    """One SH/SZ stock, whole Shanghai days, no limit or implicit pagination."""
    _symbol(symbol)
    _bounds(since, until)
    _require(isinstance(adjust_kind, str) and adjust_kind in ("none", "forward"),
             "ADJUST_KIND_INVALID")
    return {"method": "GET", "url": ENDPOINT,
            "parameters": {"symbol": symbol, "interval_unit": "day",
                           "adjust_kind": adjust_kind, "since": since, "until": until}}


def validate_single(payload, *, symbol, since: int, until: int,
                    adjust_kind: str = "none") -> dict:
    """Accept the documented bare array and Kernel's observed code/data array.

    Envelopes have exactly code/data and an optional null or string message.
    Only integer 0/200 are success. Nested items, string codes and v2 pairs are
    not another interpretation of this route. The original message stays in
    the original body, never in a public error or normalized price record.
    """
    request = single_request(symbol=symbol, since=since, until=until, adjust_kind=adjust_kind)
    if isinstance(payload, Mapping):
        _require(set(payload) in ({"code", "data"}, {"code", "data", "message"}),
                 "ENVELOPE_INVALID")
        _require(type(payload["code"]) is int and payload["code"] in (0, 200),
                 "RESPONSE_NOT_SUCCESS")
        _require("message" not in payload or payload["message"] is None
                 or isinstance(payload["message"], str), "ENVELOPE_INVALID")
        rows = payload["data"]
    else:
        rows = payload
    _require(isinstance(rows, list), "DATA_INVALID")
    # This wrapper is only a call into shared row validation, never an original.
    view = validate_batch({"code": 200, "message": "success", "data": [[symbol, rows]]},
                          symbols=[symbol], since=since, until=until, adjust_kind=adjust_kind)
    view.update(schema=VIEW_SCHEMA, request=request, request_sha256=digest(encoded(request)))
    return view


def capture_single(raw: bytes, *, symbol, since: int, until: int,
                   adjust_kind: str = "none", acquired_at: str) -> dict:
    """Bind exact UTF-8 HTTP body bytes to the declared v1 request and view."""
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_WIRE_BYTES, "WIRE_BYTES_INVALID")
    _require(isinstance(acquired_at, str) and 1 <= len(acquired_at) <= 80
             and "T" in acquired_at, "ACQUISITION_CLOCK_INVALID")
    try:
        _require(datetime.fromisoformat(acquired_at).utcoffset() is not None,
                 "ACQUISITION_CLOCK_INVALID")
    except ValueError:
        raise FTShareSingleError("FTSHARE_SINGLE_ACQUISITION_CLOCK_INVALID") from None
    try:
        body = raw.decode("utf-8")
        payload = json.loads(body, object_pairs_hook=_json_object, parse_constant=_nonfinite,
                             parse_float=str)
    except FTShareDailyError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise FTShareSingleError("FTSHARE_SINGLE_WIRE_JSON_INVALID") from None
    view = validate_single(payload, symbol=symbol, since=since, until=until, adjust_kind=adjust_kind)
    return {"schema": CAPTURE_SCHEMA, "provider": PROVIDER,
            "request": view["request"], "request_sha256": view["request_sha256"],
            "wire_sha256": digest(raw), "wire_bytes": len(raw), "body_utf8": body,
            "acquired_at": acquired_at, "normalized": view, "transport_attested": False}


def source_for_single_capture(capture: dict, *, source_id: str | None = None,
                              representation: str = "wire") -> SourceInput:
    """Existing SourceInput custody: original v1 body or clearly labeled excerpt."""
    _require(representation in ("wire", "normalized"), "REPRESENTATION_INVALID")
    _require(isinstance(capture, dict), "CAPTURE_INVALID")
    try:
        params = capture["request"]["parameters"]
        body = capture["body_utf8"]
        _require(isinstance(body, str), "CAPTURE_INVALID")
        rebuilt = capture_single(body.encode("utf-8"), symbol=params["symbol"],
            since=params["since"], until=params["until"], adjust_kind=params["adjust_kind"],
            acquired_at=capture["acquired_at"])
    except FTShareDailyError:
        raise
    except (KeyError, TypeError, UnicodeError):
        raise FTShareSingleError("FTSHARE_SINGLE_CAPTURE_INVALID") from None
    _require(encoded(capture) == encoded(rebuilt), "CAPTURE_INTEGRITY_MISMATCH")
    view = rebuilt["normalized"]
    limitations = [*view["limits"], "PROVIDER_FTSHARE_NOT_ISSUER",
                   "HTTP_RESPONSE_BODY_ONLY_HEADERS_NOT_RETAINED",
                   "SCHEMA_FTSHARE_SINGLE_DAILY_CAPTURE_V1",
                   "WIRE_SHA256:" + rebuilt["wire_sha256"],
                   "REQUEST_SHA256:" + rebuilt["request_sha256"]]
    custody = "provided"
    if representation == "normalized":
        body = encoded({"schema": "ftshare-normalized-single-daily-excerpt-v1",
                        "provider": PROVIDER, "wire_sha256": rebuilt["wire_sha256"],
                        "acquired_at": rebuilt["acquired_at"], "qualified": view}).decode("utf-8")
        custody = "excerpt"
        limitations.append("NORMALIZED_EXCERPT_NOT_ORIGINAL_HTTP_BODY")
    lo, hi = _bounds(params["since"], params["until"])
    if source_id is None:
        source_id = ("ftshare-single-daily-" + params["adjust_kind"] + "-"
                     + rebuilt["request_sha256"][:16] + "-" + rebuilt["wire_sha256"][:16]
                     + "-" + representation)
    wire_params = {({"since": "since_ts_millis", "until": "until_ts_millis"}.get(key, key)): value
                   for key, value in params.items()}
    return SourceInput(source_id=source_id, kind="content",
        title="FTShare " + view["adjustment"] + " single-stock daily bars "
              + lo.date().isoformat() + " to " + hi.date().isoformat(),
        origin=ENDPOINT + "?" + urlencode(wire_params), text=body, custody=custody,
        acquired_at=rebuilt["acquired_at"],
        report_period=lo.date().isoformat() + "/" + hi.date().isoformat(),
        limitations=tuple(limitations))


def restore_single(reference, store, *, request):
    """Read the canonical original SourceInput and rebuild it without source I/O."""
    _require(isinstance(request, dict) and isinstance(request.get("parameters"), dict),
             "REQUEST_INVALID")
    params = request["parameters"]
    _require(set(params) == {"symbol", "interval_unit", "adjust_kind", "since", "until"},
             "REQUEST_INVALID")
    expected_request = single_request(symbol=params["symbol"], since=params["since"],
                                     until=params["until"], adjust_kind=params["adjust_kind"])
    _require(request == expected_request, "REQUEST_CHANGED")
    with market_read_scope(store) as reader:
        ref = reference if isinstance(reference, SourceRef) else SourceRef.from_dict(reference)
        _require(ref.selection == Selection() and ref.manifest.repository == reader.backend.repository,
                 "EXACT_LOCAL_REFERENCE_REQUIRED")
        _, stored = _read_source(reader, ref)
        capture = capture_single(stored["data"].encode("utf-8"), symbol=params["symbol"],
            since=params["since"], until=params["until"], adjust_kind=params["adjust_kind"],
            acquired_at=stored["manifest"]["input"]["acquired_at"])
        expected = source_for_single_capture(capture)
        _require(encoded({**stored["manifest"]["input"], "text": stored["data"]})
                 == encoded(asdict(expected)), "SOURCE_CHANGED")
        return capture
