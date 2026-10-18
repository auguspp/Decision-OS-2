"""Bounded FTShare daily batch qualification; no network, filling or adjustment.

Protocol: FTShare-Lab/FTShare-skill at
d2233464fc26b6d3c885f60543e316565361bd83, stock-candlesticks-batch.
This reader checks the supplied response against a declared request. It cannot
authenticate a publisher, prove calendar completeness, or grant custody rights.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.parse import urlencode

from decision_kernel.domain.records import SourceInput, digest, encoded
from decision_kernel.observations.market_daily import MarketInputError, _price

ENDPOINT = "https://market.ft.tech/gateway/api/v2/market/data/stock-candlesticks/batch"
PROVIDER = "FTSHARE"
MAX_SYMBOLS = 100
MAX_CALENDAR_DAYS = 119
MAX_WIRE_BYTES = 2 * 1024 * 1024
SHANGHAI = timezone(timedelta(hours=8))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRICE_FIELDS = ("open", "high", "low", "close")


class FTShareDailyError(MarketInputError):
    """Stable refusal code; no credentials, provider prose or source values."""


def _require(ok: bool, code: str) -> None:
    if not ok:
        raise FTShareDailyError("FTSHARE_DAILY_" + code)


def _symbol(value: object) -> str:
    _require(isinstance(value, str)
             and bool(re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", value)), "SYMBOL_INVALID")
    return value


def _timestamp(value: object) -> tuple[int, datetime]:
    if isinstance(value, str):
        _require(bool(re.fullmatch(r"[0-9]{1,16}", value)), "TIMESTAMP_INVALID")
        value = int(value)
    _require(type(value) is int and 0 <= value <= 253402271999999, "TIMESTAMP_INVALID")
    try:
        when = (EPOCH + timedelta(milliseconds=value)).astimezone(SHANGHAI)
    except (ValueError, OverflowError):
        raise FTShareDailyError("FTSHARE_DAILY_TIMESTAMP_INVALID") from None
    return value, when


def _bounds(since: object, until: object) -> tuple[datetime, datetime]:
    # Request integers are stricter than response integer-or-digits timestamps.
    _require(type(since) is int and type(until) is int, "RANGE_INVALID")
    _, lo = _timestamp(since)
    _, hi = _timestamp(until)
    _require(lo.time() == time(0) and hi.time() == time(23, 59, 59, 999000),
             "WHOLE_SHANGHAI_DAYS_REQUIRED")
    _require(1 <= (hi.date() - lo.date()).days + 1 <= MAX_CALENDAR_DAYS, "RANGE_INVALID")
    return lo, hi


def batch_request(*, symbols, since: int, until: int, adjust_kind: str) -> dict:
    """Canonical internal request identity; symbols are a repeated wire parameter.

    The transport maps ``since``/``until`` to ``since_ts_millis`` and
    ``until_ts_millis`` and encodes with ``doseq=True``. No limit or pagination
    argument is sent: the official endpoint declares a whole-range response.
    """
    _require(isinstance(symbols, (tuple, list)) and 1 <= len(symbols) <= MAX_SYMBOLS,
             "SYMBOLS_BOUND")
    codes = [_symbol(symbol) for symbol in symbols]
    _require(len(set(codes)) == len(codes), "DUPLICATE_REQUEST_SYMBOL")
    _require(isinstance(adjust_kind, str) and adjust_kind in ("none", "forward"),
             "ADJUST_KIND_INVALID")
    _bounds(since, until)
    return {"method": "GET", "url": ENDPOINT,
            "parameters": {"symbols": sorted(codes), "interval_unit": "day",
                           "adjust_kind": adjust_kind, "since": since, "until": until}}


def _prices(row: Mapping) -> dict[str, str]:
    prices = {}
    for field in PRICE_FIELDS:
        value = row.get(field)
        _require(isinstance(value, str) and value == value.strip(), "PRICE_TYPE")
        try:
            prices[field] = _price(value)
        except MarketInputError:
            raise FTShareDailyError("FTSHARE_DAILY_PRICE_INVALID") from None
    _require(prices["low"] <= min(prices["open"], prices["close"])
             <= max(prices["open"], prices["close"]) <= prices["high"], "OHLC_INVALID")
    return {field: str(prices[field]) for field in PRICE_FIELDS}


def _turnover(value: object) -> str:
    _require(isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip(),
             "TURNOVER_INVALID")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise FTShareDailyError("FTSHARE_DAILY_TURNOVER_INVALID") from None
    _require(amount.is_finite() and amount >= 0 and -99 <= amount.adjusted() <= 99,
             "TURNOVER_INVALID")
    return str(amount)


def validate_batch(payload, *, symbols, since: int, until: int, adjust_kind: str) -> dict:
    """Qualify raw or provider-forward daily bars, retaining explicit absences.

    Identity, range, timestamp or duplicate errors reject the batch. A row with
    an identifiable day but invalid OHLC/volume/turnover is quarantined. Neither
    an empty symbol nor a quarantined row is evidence of a suspension. Forward
    prices remain one provider's same-request convention, without inferred PIT
    factors, cross-request stitching or total-return claims.
    """
    request = batch_request(symbols=symbols, since=since, until=until, adjust_kind=adjust_kind)
    codes = request["parameters"]["symbols"]
    lo, hi = _bounds(since, until)
    _require(isinstance(payload, Mapping) and set(payload) == {"code", "message", "data"},
             "ENVELOPE_INVALID")
    _require(type(payload["code"]) is int and payload["code"] == 200
             and payload["message"] == "success", "RESPONSE_NOT_SUCCESS")
    data = payload["data"]
    _require(isinstance(data, list) and len(data) <= len(codes), "DATA_INVALID")
    batches = {}
    for pair in data:
        _require(isinstance(pair, list) and len(pair) == 2, "PAIR_INVALID")
        code = _symbol(pair[0])
        _require(code in codes, "UNREQUESTED_SYMBOL")
        _require(code not in batches, "DUPLICATE_RESPONSE_SYMBOL")
        _require(isinstance(pair[1], list), "SERIES_INVALID")
        _require(len(pair[1]) <= (hi.date() - lo.date()).days + 1, "SYMBOL_ROW_LIMIT")
        batches[code] = pair[1]
    # Explicit [symbol, []] is distinguishable from an omitted/truncated symbol.
    _require(set(batches) == set(codes), "MISSING_RESPONSE_SYMBOL")
    series, quarantined, empty = {}, [], []
    total = 0
    for code in codes:
        accepted, seen = [], set()
        if not batches[code]:
            empty.append(code)
        for row in batches[code]:
            total += 1
            _require(isinstance(row, Mapping), "ROW_INVALID")
            if "symbol" in row:
                _require(_symbol(row["symbol"]) == code, "ROW_SYMBOL_MISMATCH")
            opened, opening = _timestamp(row.get("ts_millis_open"))
            closed, closing = _timestamp(row.get("ts_millis"))
            _require(since <= opened <= closed <= until, "ROW_TIME_OUT_OF_RANGE")
            _require(opening.date() == closing.date(), "ROW_TIME_DAY_MISMATCH")
            day = opening.date().isoformat()
            _require(day not in seen, "DUPLICATE_DATE")
            seen.add(day)  # Bad OHLC must not hide a duplicate date.
            try:
                prices = _prices(row)
                volume = row.get("volume")
                _require(type(volume) is int and 0 <= volume < 10**30, "VOLUME_INVALID")
                turnover = _turnover(row.get("turnover"))
            except FTShareDailyError as exc:
                quarantined.append({"symbol": code, "trade_date": day, "reason": str(exc)})
                continue
            accepted.append({"trade_date": day, **prices, "volume": volume,
                             "volume_unit": "UNKNOWN", "turnover": turnover,
                             "turnover_unit": "CNY", "ts_millis_open": opened,
                             "ts_millis": closed, "trade_status": "UNKNOWN"})
        series[code] = sorted(accepted, key=lambda row: row["trade_date"])
    limits = ["RESPONSE_VALIDATION_NOT_PROVIDER_AUTHENTICATION_OR_CUSTODY",
              "DECLARED_RANGE_NOT_TRADING_CALENDAR_COMPLETENESS",
              "SYNTAX_AND_RESPONSE_IDENTITY_NOT_SECURITY_MASTER",
              "EMPTY_OR_QUARANTINED_ROWS_NOT_SUSPENSION",
              "VOLUME_UNIT_NOT_CONFIRMED", "PRICE_RATIO_NOT_TOTAL_RETURN"]
    if adjust_kind == "forward":
        limits.extend(["PROVIDER_FORWARD_SAME_REQUEST_NOT_POINT_IN_TIME_FACTORS",
                       "NO_FORWARD_SERIES_STITCHING_OR_FACTOR_RECONSTRUCTION"])
    else:
        limits.append("UNADJUSTED_PRICE_NOT_CORPORATE_ACTION_NEUTRAL")
    return {"schema": "ftshare-qualified-daily-v1", "provider": PROVIDER,
            "request": request, "request_sha256": digest(encoded(request)),
            "symbols": codes, "series": series,
            "adjust_kind": adjust_kind,
            "adjustment": "raw" if adjust_kind == "none" else "provider-forward",
            "currency": "CNY", "return_semantics": "price-ratio-not-total-return",
            "counts": {"requested_symbols": len(codes), "returned_symbols": len(batches),
                       "total_rows": total, "observed_rows": total - len(quarantined),
                       "quarantined_rows": len(quarantined)},
            "missing": {"empty_symbols": empty,
                        "no_qualified_rows": [code for code in codes if not series[code]],
                        "calendar_sessions": "NOT_ASSESSED"},
            "quarantined": sorted(quarantined, key=lambda row: (row["symbol"], row["trade_date"])),
            "limits": limits}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _nonfinite(_value):
    raise FTShareDailyError("FTSHARE_DAILY_NONFINITE_JSON")


def capture_batch(raw: bytes, *, symbols, since: int, until: int,
                  adjust_kind: str, acquired_at: str) -> dict:
    """Retain exact UTF-8 response-body bytes plus their qualified projection.

    The caller supplies the complete HTTP body. Hashes bind the bytes and the
    declared request; they are not an HTTP transport or publisher attestation.
    No credential, URL override or HTTP header is accepted here.
    """
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_WIRE_BYTES, "WIRE_BYTES_INVALID")
    _require(isinstance(acquired_at, str) and 1 <= len(acquired_at) <= 80
             and "T" in acquired_at, "ACQUISITION_CLOCK_INVALID")
    try:
        when = datetime.fromisoformat(acquired_at)
        _require(when.tzinfo is not None and when.utcoffset() is not None,
                 "ACQUISITION_CLOCK_INVALID")
    except ValueError:
        raise FTShareDailyError("FTSHARE_DAILY_ACQUISITION_CLOCK_INVALID") from None
    try:
        body = raw.decode("utf-8")
        payload = json.loads(body, object_pairs_hook=_json_object, parse_constant=_nonfinite,
                             parse_float=str)
    except FTShareDailyError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise FTShareDailyError("FTSHARE_DAILY_WIRE_JSON_INVALID") from None
    view = validate_batch(payload, symbols=symbols, since=since, until=until,
                          adjust_kind=adjust_kind)
    return {"schema": "ftshare-daily-capture-v1", "provider": PROVIDER,
            "request": view["request"], "request_sha256": view["request_sha256"],
            "wire_sha256": digest(raw), "wire_bytes": len(raw), "body_utf8": body,
            "acquired_at": acquired_at, "normalized": view,
            "transport_attested": False}


def source_for_capture(capture: dict, *, source_id: str | None = None,
                       representation: str = "wire") -> SourceInput:
    """Build an existing SourceInput; actual store/write authority stays outside.

    ``wire`` preserves the received UTF-8 body byte for byte. ``normalized`` is
    explicitly an excerpt and cannot be presented as an original response.
    Existing SourceInput size limits still apply; this does not split originals.
    """
    _require(representation in ("wire", "normalized"), "REPRESENTATION_INVALID")
    _require(isinstance(capture, dict), "CAPTURE_INVALID")
    try:
        request = capture["request"]
        params = request["parameters"]
        body = capture["body_utf8"]
        _require(isinstance(body, str), "CAPTURE_INVALID")
        rebuilt = capture_batch(body.encode("utf-8"), symbols=params["symbols"],
            since=params["since"], until=params["until"], adjust_kind=params["adjust_kind"],
            acquired_at=capture["acquired_at"])
    except FTShareDailyError:
        raise
    except (KeyError, TypeError, UnicodeError):
        raise FTShareDailyError("FTSHARE_DAILY_CAPTURE_INVALID") from None
    _require(capture == rebuilt, "CAPTURE_INTEGRITY_MISMATCH")
    view = rebuilt["normalized"]
    limitations = [*view["limits"], "PROVIDER_FTSHARE_NOT_ISSUER",
                   "HTTP_RESPONSE_BODY_ONLY_HEADERS_NOT_RETAINED",
                   "SCHEMA_FTSHARE_DAILY_CAPTURE_V1",
                   "WIRE_SHA256:" + rebuilt["wire_sha256"],
                   "REQUEST_SHA256:" + rebuilt["request_sha256"]]
    custody = "provided"
    if representation == "normalized":
        body = encoded({"schema": "ftshare-normalized-daily-excerpt-v1",
                        "provider": PROVIDER, "wire_sha256": rebuilt["wire_sha256"],
                        "acquired_at": rebuilt["acquired_at"], "qualified": view}).decode("utf-8")
        custody = "excerpt"
        limitations.append("NORMALIZED_EXCERPT_NOT_ORIGINAL_HTTP_BODY")
    lo, hi = _bounds(params["since"], params["until"])
    if source_id is None:
        source_id = ("ftshare-daily-" + params["adjust_kind"] + "-"
                     + rebuilt["request_sha256"][:16] + "-" + rebuilt["wire_sha256"][:16]
                     + "-" + representation)
    wire_params = {({"since": "since_ts_millis", "until": "until_ts_millis"}.get(key, key)): value
                   for key, value in params.items()}
    # The existing Store reserves ``dataset`` for CSV. Preserve this original
    # JSON response as content instead of changing that public store contract.
    return SourceInput(source_id=source_id, kind="content",
        title="FTShare " + view["adjustment"] + " daily bars " + lo.date().isoformat()
              + " to " + hi.date().isoformat(),
        origin=ENDPOINT + "?" + urlencode(wire_params, doseq=True), text=body, custody=custody,
        acquired_at=rebuilt["acquired_at"],
        report_period=lo.date().isoformat() + "/" + hi.date().isoformat(),
        limitations=tuple(limitations))
