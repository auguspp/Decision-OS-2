"""Narrow retained SH/SZ raw-close adapter; uses existing #10/#12 readers only.

No feed acquisition, generated quote, share-count inference, or trade permission.
The date-only source is conservatively usable after that local day has ended.
"""
from datetime import timedelta, timezone

from decision_kernel.application.market_custody import restore_market_query
from decision_kernel.application.market_relay_inputs import qualified_daily, qualified_trade_calendar
from decision_kernel.application.market_security_inputs import qualified_security_master
from decision_kernel.domain.conditional_odds import day, number, shape, timestamp
from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, identifier, require
from decision_kernel.observations.market_daily import MarketInputError

SHANGHAI = timezone(timedelta(hours=8))


def qualify_close(payload, spec, policy, evaluated_at, registry, store):
    shape(payload, {"schema", "price_id", "calendar_ref", "daily_ref", "master_ref", "exchange", "start", "end"})
    require(payload["schema"] == "odds-retained-close-v1", "ODDS_MARKET_ADAPTER_UNSUPPORTED")
    identifier(payload["price_id"])
    require(spec["security"]["share_class"] in ("ordinary-a", "A") and spec["currency"] == "CNY",
            "ODDS_MARKET_SHARE_CLASS_UNSUPPORTED")
    evaluated = timestamp(evaluated_at)
    restored = {}
    for name in ("calendar", "daily", "master"):
        ref = SourceRef.from_dict(payload[name + "_ref"])
        try:
            restored[name] = restore_market_query(ref, registry, store)
        except MarketInputError as exc:
            raise RecordError(str(exc)) from None
    try:
        days = qualified_trade_calendar(restored["calendar"]["result"], registry=registry,
            exchange=payload["exchange"], start=payload["start"], end=payload["end"])
        rows = qualified_daily(restored["daily"]["result"], registry=registry,
            ts_code=spec["security"]["code"], start=payload["start"], end=payload["end"], calendar=days)
        master = qualified_security_master({"L": restored["master"]["result"]}, registry=registry,
                                           exchange=payload["exchange"])
    except MarketInputError as exc:
        raise RecordError(str(exc)) from None
    # Existing validators have established the eight-digit date shape.
    quote_date = day(payload["end"][:4] + "-" + payload["end"][4:6] + "-" + payload["end"][6:])
    require(quote_date.isoformat() in days, "ODDS_CLOSE_NOT_TRADING_DAY")
    age = (evaluated.astimezone(SHANGHAI).date() - quote_date).days
    require(1 <= age <= policy["max_quote_age_days"], "ODDS_CLOSE_DAY_NOT_FINISHED_OR_STALE")
    selected = [r for r in rows if r["trade_date"] == quote_date.isoformat()]
    require(len(selected) == 1, "ODDS_EXACT_CLOSE_MISSING")
    code = spec["security"]["code"]
    require(code in master["securities"], "ODDS_SECURITY_MASTER_MISSING")
    reference = master["securities"][code]
    require(reference["list_date"] <= quote_date.isoformat() and reference["source_list_status"] == "L"
            and reference["currency"] == spec["currency"], "ODDS_LISTING_OR_CURRENCY_MISMATCH")
    require(0 <= (quote_date - day(spec["capital_structure"]["as_of"])).days <= policy["max_capital_age_days"],
            "ODDS_CAPITAL_ASSUMPTION_STALE")
    clocks = {}
    for name, out in restored.items():
        times = [timestamp(p["captured_at"]) for p in out["result"]["capture"]["pages"]]
        require(times and all(t <= evaluated for t in times), "ODDS_MARKET_CAPTURE_AFTER_EVALUATION")
        if name == "daily":
            # No claimed intraday provider timestamp: the entire quote day must have elapsed.
            require(all(t.astimezone(SHANGHAI).date() > quote_date for t in times),
                    "ODDS_CLOSE_CAPTURE_DAY_NOT_FINISHED")
        if name == "master":
            require(all((evaluated - t).total_seconds() <= policy["max_master_age_days"] * 86400 for t in times),
                    "ODDS_SECURITY_MASTER_STALE")
        clocks[name] = [t.isoformat() for t in times]
    # 23:59:59 is a conservative date-boundary marker, never a provider tick time.
    price = {"schema": "odds-qualified-close-v1", "price_id": payload["price_id"],
        "kind": "retained-relay-raw-close", "security": spec["security"], "currency": spec["currency"],
        "share_count": spec["share_count"], "price": str(number(selected[0]["close"])),
        "as_of": quote_date.isoformat() + "T23:59:59+08:00",
        "timestamp_semantics": "conservative-local-date-boundary-not-source-tick",
        "share_count_qualification": "frozen-research-assumption-not-source-certified"}
    return {"price": price, "status": "qualified-retained-close-for-declared-as-of",
        "registry_snapshot": registry.snapshot, "evaluated_at": evaluated_at, "quote_age_calendar_days": age,
        "source_references": {k: v["reference"] for k, v in restored.items()},
        "query_sha256": {k: digest(encoded(v["result"])) for k, v in restored.items()},
        "capture_times": clocks, "security_reference": reference, "source_requests": 0,
        "scope": "SH_SZ_CNY_RAW_DATED_CLOSE_ONLY_NOT_EXECUTION_LATEST_OR_HISTORICAL_PIT",
        "limitations": ["SOURCE_CODE_NOT_PERMANENT_ISSUER_ID", "SOURCE_SNAPSHOTS_NOT_ATOMIC",
                        "CORPORATE_ACTIONS_AND_FIXED_SHARE_COUNT_REQUIRE_RESEARCH_REVIEW",
                        "NO_LIVE_SUSPENSION_OR_EXECUTABILITY_CERTIFICATION"]}
