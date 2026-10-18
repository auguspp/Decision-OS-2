"""Finite offline FTShare vs Tushare Relay unadjusted-price diagnostics.

Inputs MUST have been authenticated and qualified by their existing readers first.
This component does not authenticate SourceRefs, acquire data, certify independent
upstreams, reconcile failed collection queries, or approve a backup provider.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
import re

from .market_daily import MarketInputError, _price, trade_day

PROVIDERS = ("TUSHARE_RELAY", "FTSHARE")
PRICE_FIELDS = ("open", "high", "low", "close")
BASE_ROW_FIELDS = frozenset(("symbol", "trade_date", "close", "currency", "adjustment", "trade_status"))
EXTRA_ROW_FIELDS = frozenset(("open", "high", "low", "volume", "volume_unit"))
CAPTURE_FIELDS = frozenset(("provider", "source_ref", "acquired_at", "session", "rows"))
REF_FIELDS = frozenset(("repository", "commit", "path", "sha256"))
ARTIFACT_FIELDS = frozenset(("kind", "run_id", "attempt", "artifact_id", "artifact_sha256"))
PURPOSES = ("trading-rows", "quoted-values")
MAX_ROWS = 32


def _reject(code: str) -> None:
    raise MarketInputError("SECONDARY_SOURCE_" + code)


def _ref(value: object) -> None:
    # Caller independently verifies the exact source; a plausible locator is not proof.
    if not isinstance(value, Mapping) or set(value) not in (REF_FIELDS, REF_FIELDS | ARTIFACT_FIELDS):
        _reject("REFERENCE_REQUIRED")
    if (not isinstance(value["repository"], str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"])
        or not isinstance(value["commit"], str)
        or not re.fullmatch(r"[a-f0-9]{40}", value["commit"])
        or not isinstance(value["sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
        or not isinstance(value["path"], str)
        or not 0 < len(value["path"]) <= 512
        or value["path"].startswith("/")
        or any(p in ("", ".", "..") for p in value["path"].split("/"))
        or not value["path"].isascii() or not value["path"].isprintable()
        or "\\" in value["path"]):
        _reject("REFERENCE_INVALID")
    if "kind" in value:
        if (value["kind"] != "github-actions-artifact"
            or any(type(value[k]) is not int or not 1 <= value[k] < 10**18
                   for k in ("run_id", "attempt", "artifact_id"))
            or not isinstance(value["artifact_sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", value["artifact_sha256"])):
            _reject("ARTIFACT_REFERENCE_INVALID")


def _capture(value: object, provider: str, session: str) -> dict[str, dict]:
    if not isinstance(value, Mapping) or set(value) != CAPTURE_FIELDS:
        _reject("CAPTURE_FIELDS")
    if value["provider"] != provider or value["session"] != session:
        _reject("PROVIDER_OR_SESSION")
    _ref(value["source_ref"])
    when = value["acquired_at"]
    if not isinstance(when, str) or not 1 <= len(when) <= 64:
        _reject("ACQUISITION_CLOCK")
    try:
        parsed = datetime.fromisoformat(when)
    except ValueError:
        _reject("ACQUISITION_CLOCK")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _reject("ACQUISITION_CLOCK")
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        _reject("ROWS_BOUND")
    found: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) not in (BASE_ROW_FIELDS, BASE_ROW_FIELDS | EXTRA_ROW_FIELDS):
            _reject("ROW_FIELDS")
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", symbol):
            _reject("SECURITY_IDENTITY")
        try:
            day = trade_day(row["trade_date"])
        except MarketInputError:
            _reject("ROW_SESSION")
        if day.isoformat() != session:
            _reject("ROW_SESSION")
        if symbol in found:
            _reject("DUPLICATE_SECURITY")
        if row["currency"] != "CNY" or row["adjustment"] != "NONE":
            _reject("RAW_CNY_ONLY")
        if row["trade_status"] not in ("TRADING", "SUSPENDED", "UNKNOWN"):
            _reject("TRADE_STATUS")
        expanded = set(row) == BASE_ROW_FIELDS | EXTRA_ROW_FIELDS
        numbers = {}
        fields = PRICE_FIELDS if expanded else ("close",)
        for field in fields:
            # No binary-float coercion: retain each original provider decimal.
            if not isinstance(row[field], str):
                _reject("PRICE_TYPE")
            try:
                numbers[field] = _price(row[field])
            except MarketInputError:
                _reject("PRICE_INVALID")
        normalized_volume = None
        if expanded:
            if row["volume_unit"] != ("LOTS_100_SHARES" if provider == "TUSHARE_RELAY" else "SHARES"):
                _reject("VOLUME_UNIT")
            if not (numbers["low"] <= min(numbers["open"], numbers["close"])
                    <= max(numbers["open"], numbers["close"]) <= numbers["high"]):
                _reject("OHLC_INVALID")
            raw_vol = row["volume"]
            if not isinstance(raw_vol, str) or len(raw_vol) > 128:
                _reject("VOLUME_INVALID")
            try:
                volume = Decimal(raw_vol)
            except (ArithmeticError, ValueError):
                _reject("VOLUME_INVALID")
            if not volume.is_finite() or volume < 0 or not -99 <= volume.adjusted() <= 99:
                _reject("VOLUME_INVALID")
            # An exponent shift is exact and independent of ambient precision/traps.
            parts = volume.as_tuple()
            normalized_volume = (Decimal((parts.sign, parts.digits, parts.exponent + 2))
                                 if provider == "TUSHARE_RELAY" else volume)
        found[symbol] = {"prices": numbers, "volume_shares": normalized_volume,
                         "trade_status": row["trade_status"]}
    return found


def compare_retained_raw_market(*, relay: object, ftshare: object, session: str,
                                purpose: str = "trading-rows") -> dict:
    """Compare at most 32 original close pairs, optionally full OHLCV; no prices in output.

    Never uses a provider's claimed 5/20/60 returns or adjusts/fills corporate actions.
    Missing rows remain missing, and even exact agreement grants no daily admission.
    """
    try:
        trade_day(session)
    except MarketInputError:
        _reject("SESSION_INVALID")
    if not isinstance(purpose, str) or purpose not in PURPOSES:
        _reject("PURPOSE_INVALID")
    primary = _capture(relay, "TUSHARE_RELAY", session)
    secondary = _capture(ftshare, "FTSHARE", session)
    if relay["source_ref"] == ftshare["source_ref"]:
        _reject("SAME_SOURCE_REFERENCE")
    shared = sorted(set(primary) & set(secondary))
    details = []
    for symbol in shared:
        a, b = primary[symbol], secondary[symbol]
        trading_known = a["trade_status"] == b["trade_status"] == "TRADING"
        if purpose == "trading-rows" and not trading_known:
            details.append({"symbol": symbol, "status": "NON_TRADING_OR_UNKNOWN",
                            "fields_compared": [], "price_fields_differ": [],
                            "volume_comparison": "NOT_ASSESSED",
                            "trading_status_confirmed": False})
            continue
        comparable = [field for field in PRICE_FIELDS
                      if field in a["prices"] and field in b["prices"]]
        different = [field for field in comparable if a["prices"][field] != b["prices"][field]]
        volume = ("NOT_ASSESSED" if a["volume_shares"] is None or b["volume_shares"] is None
                  else "EQUAL" if a["volume_shares"] == b["volume_shares"] else "DIFFERENCE")
        details.append({"symbol": symbol, "status": "PRICE_DIFFERENCE" if different else "PRICE_EQUAL",
                        "fields_compared": comparable, "price_fields_differ": different,
                        "volume_comparison": volume, "trading_status_confirmed": trading_known})
    counts = {code: sum(x["status"] == code for x in details)
              for code in ("PRICE_EQUAL", "PRICE_DIFFERENCE", "NON_TRADING_OR_UNKNOWN")}
    return {"schema": "secondary-market-raw-comparison-v1", "session": session,
            "purpose": purpose, "trading_eligibility_granted": False,
            "provider_pair": list(PROVIDERS),
            "primary_count": len(primary), "secondary_count": len(secondary),
            "paired_count": len(shared), "primary_only": len(primary) - len(shared),
            "secondary_only": len(secondary) - len(shared), "counts": counts,
            "details": details,
            "comparison_status": "NO_COMPARABLE_PAIRS" if not counts["PRICE_EQUAL"] + counts["PRICE_DIFFERENCE"]
                                 else "DIAGNOSTIC_ONLY",
            "provenance_verified_by_this_function": False,
            "common_upstream_independence": "NOT_ESTABLISHED",
            "ftshare_backup_admission": "NOT_ESTABLISHED",
            "market_canonical_changed": False,
            "source_requests": 0, "writes": 0}
