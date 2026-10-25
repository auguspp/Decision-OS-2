"""Narrow retained SH/SZ raw-close adapters; use existing #10/#12 readers only.

No feed acquisition, generated quote, share-count inference, or trade permission.
Relay retains its next-day rule. FTShare uses an explicit post-close observation
cutoff, never a fabricated provider tick or execution-ready quote.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime

from decision_kernel.application.market_completion import (
    EOD_COMPLETION_POLICY, EOD_CUTOFF, SHANGHAI, completed_close_clock,
)
from decision_kernel.application.market_custody import market_read_scope, restore_market_query
from decision_kernel.application.market_relay_inputs import qualified_daily, qualified_trade_calendar
from decision_kernel.application.market_security_inputs import qualified_security_master
from decision_kernel.domain.conditional_odds import day, number, shape, timestamp
from decision_kernel.domain.records import RecordError, SourceRef, digest, encoded, identifier, require
from decision_kernel.observations.market_daily import MarketInputError

FTSHARE_CLOSE_SCHEMA = "odds-ftshare-close-v1"
RELAY_CLOSE_SCHEMA = "odds-retained-close-v1"
# Finite existing adapters, not a provider registry. Qualification remains in
# the corresponding source reader; this mapping only binds its public shapes.
CLOSE_PRICE_KINDS = {
    FTSHARE_CLOSE_SCHEMA: "retained-ftshare-raw-close",
    RELAY_CLOSE_SCHEMA: "retained-relay-raw-close",
}


def qualify_close(payload, spec, policy, evaluated_at, registry, store):
    if isinstance(payload, dict) and payload.get("schema") == FTSHARE_CLOSE_SCHEMA:
        return _qualify_ftshare_close(payload, spec, policy, evaluated_at, registry, store)
    return _qualify_relay_close(payload, spec, policy, evaluated_at, registry, store)


def _ftshare_completed(value, quote_date, reason):
    try:
        completed_close_clock(value, quote_date.isoformat(), EOD_COMPLETION_POLICY)
    except RecordError as exc:
        if str(exc) == "FTSHARE_COMPLETION_BEFORE_END":
            raise RecordError(reason) from None
        raise


@contextmanager
def retained_close_read_scope(store):
    """Reuse the existing native source snapshot, with the caller's API budget.

    Research/artifact reads and all writes stay on their original store. This
    is the same source-only dispatch used by the existing MarketRadar reader.
    """
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    from decision_kernel.infrastructure.market_snapshot import GitMarketReader

    scope = getattr(store.backend, "ftshare_close_read_scope", None)
    if scope is not None:
        require(callable(scope), "ODDS_FTSHARE_READ_SCOPE_REQUIRED")
        with scope() as backend:
            require(backend.repository == store.backend.repository,
                    "MARKET_CUSTODY_READ_SCOPE_REPOSITORY_CHANGED")
            yield VersionedStore(backend)
        return
    if isinstance(store.backend, GitHubStore):
        store = VersionedStore(GitMarketReader(replace(store.backend.binding, write_enabled=False),
                                               store.backend.api))
    with market_read_scope(store) as reader:
        yield reader


def _qualify_relay_close(payload, spec, policy, evaluated_at, registry, store):
    """The original adapter and output stay unchanged for historical replay."""
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


def _qualify_ftshare_close(payload, spec, policy, evaluated_at, registry, store):
    """One exact raw close from the original collection and native query owners.

    A missing forward counterpart or unrelated security does not erase this
    close. No full-market/current-serving qualification is inferred from it.
    """
    from decision_kernel.application.ftshare_adoption import ADOPTION_PLAN_SCHEMA
    from decision_kernel.application.ftshare_collection import (
        SINGLE_PLAN_SCHEMA, restore_ftshare_batch, restore_ftshare_collection,
    )
    from decision_kernel.application.ftshare_single import restore_single

    shape(payload, {"schema", "price_id", "collection_ref", "exchange", "quote_date"})
    identifier(payload["price_id"])
    quote_date = day(payload["quote_date"])
    exchange, code = payload["exchange"], spec["security"]["code"]
    require(exchange in ("SSE", "SZSE") and code.endswith(".SH" if exchange == "SSE" else ".SZ"),
            "ODDS_MARKET_SECURITY_EXCHANGE_MISMATCH")
    require(spec["security"]["share_class"] in ("ordinary-a", "A") and spec["currency"] == "CNY",
            "ODDS_MARKET_SHARE_CLASS_UNSUPPORTED")
    evaluated = timestamp(evaluated_at)
    require(isinstance(policy, dict), "ODDS_POLICY_DAY_BOUND")
    for key in ("max_quote_age_days", "max_master_age_days", "max_capital_age_days"):
        require(type(policy.get(key)) is int and 1 <= policy[key] <= 36525, "ODDS_POLICY_DAY_BOUND")
    # An observation acquired at/after this conservative local cutoff is usable
    # the same evening. An earlier response never gains that qualification by
    # waiting until tomorrow. Neither clock is a provider/venue certification.
    age = (evaluated.astimezone(SHANGHAI).date() - quote_date).days
    cutoff = datetime.combine(quote_date, EOD_CUTOFF, SHANGHAI)
    require(0 <= age <= policy["max_quote_age_days"],
            "ODDS_FTSHARE_CLOSE_BEFORE_CUTOFF_OR_STALE")
    _ftshare_completed(evaluated_at, quote_date, "ODDS_FTSHARE_CLOSE_BEFORE_CUTOFF_OR_STALE")
    with retained_close_read_scope(store) as reader:
        try:
            restored = restore_ftshare_collection(payload["collection_ref"], registry, reader)
            collection, context = restored["collection"], restored["context"]
            require(collection.get("source_hold") is None, "ODDS_FTSHARE_SOURCE_HOLD")
            plan = collection["plan"]
            quote_key = quote_date.strftime("%Y%m%d")
            require(plan["start"] <= quote_key <= plan["end"], "ODDS_CLOSE_OUTSIDE_COLLECTION_RANGE")
            require(quote_date.isoformat() in context["sessions"][exchange], "ODDS_CLOSE_NOT_TRADING_DAY")
            reference = context["masters"][exchange]["securities"].get(code)
            require(reference is not None, "ODDS_SECURITY_MASTER_MISSING")
            require(reference["ts_code"] == code and reference["exchange"] == exchange
                and reference["list_date"] <= quote_date.isoformat()
                and reference["source_list_status"] == "L" and reference["currency"] == spec["currency"]
                and (reference["delist_date"] is None or quote_date.isoformat() <= reference["delist_date"]),
                "ODDS_LISTING_OR_CURRENCY_MISMATCH")
            require(0 <= (quote_date - day(spec["capital_structure"]["as_of"])).days
                    <= policy["max_capital_age_days"], "ODDS_CAPITAL_ASSUMPTION_STALE")
            single = plan["schema"] in (ADOPTION_PLAN_SCHEMA, SINGLE_PLAN_SCHEMA)
            matches = [(slot, request) for slot, request in plan["requests"].items()
                if request["parameters"]["adjust_kind"] == "none"
                and code in ([request["parameters"]["symbol"]] if single
                             else request["parameters"]["symbols"])]
            require(len(matches) == 1, "ODDS_EXACT_CLOSE_MISSING")
            slot, request = matches[0]
            unit = collection["units"][slot]
            require(unit["status"] == "complete" and unit.get("reference") is not None,
                    "ODDS_EXACT_CLOSE_MISSING")
            loader = restore_single if single else restore_ftshare_batch
            capture = loader(unit["reference"], reader, request=request)
            normalized = capture["normalized"]
            require(capture["request"] == request and normalized["provider"] == "FTSHARE"
                and normalized["adjust_kind"] == "none" and normalized["adjustment"] == "raw",
                "ODDS_FTSHARE_RAW_CLOSE_REQUIRED")
            selected = [row for row in normalized["series"].get(code, [])
                        if row["trade_date"] == quote_date.isoformat()]
            require(len(selected) == 1, "ODDS_EXACT_CLOSE_MISSING")
            require(number(selected[0]["close"]) > 0, "ODDS_POSITIVE_PRICE_REQUIRED")
            acquired = timestamp(capture["acquired_at"])
            require(acquired <= evaluated, "ODDS_MARKET_CAPTURE_AFTER_EVALUATION")
            _ftshare_completed(capture["acquired_at"], quote_date,
                               "ODDS_FTSHARE_CAPTURE_BEFORE_CLOSE_CUTOFF")
            clocks = {"daily": [acquired.isoformat()]}
            references = {"collection": deepcopy(restored["reference"]), "daily": deepcopy(unit["reference"])}
            hashes = {"daily": capture["wire_sha256"]}
            for family in ("calendar", "master"):
                result = context[family + "_results"][exchange]
                times = [timestamp(page["captured_at"]) for page in result["capture"]["pages"]]
                require(times and all(t <= evaluated for t in times), "ODDS_MARKET_CAPTURE_AFTER_EVALUATION")
                if family == "master":
                    require(all((evaluated - t).total_seconds() <= policy["max_master_age_days"] * 86400
                                for t in times), "ODDS_SECURITY_MASTER_STALE")
                clocks[family] = [t.isoformat() for t in times]
                references[family] = deepcopy(context[family + "_references"][exchange])
                hashes[family] = digest(encoded(result))
        except MarketInputError as exc:
            raise RecordError(str(exc)) from None
    price = {"schema": "odds-qualified-close-v1", "price_id": payload["price_id"],
        "kind": "retained-ftshare-raw-close", "security": deepcopy(spec["security"]),
        "currency": spec["currency"], "share_count": spec["share_count"],
        "price": str(number(selected[0]["close"])), "as_of": cutoff.isoformat(),
        "timestamp_semantics": "conservative-local-post-close-observation-cutoff-not-source-tick",
        "share_count_qualification": "frozen-research-assumption-not-source-certified"}
    return {"price": price, "status": "qualified-retained-close-for-declared-as-of",
        "registry_snapshot": deepcopy(registry.snapshot), "evaluated_at": evaluated_at,
        "quote_age_calendar_days": age, "source_references": references,
        "query_sha256": hashes, "capture_times": clocks, "security_reference": deepcopy(reference),
        "source_query_slot": slot, "native_request": deepcopy(request),
        "native_request_sha256": capture["request_sha256"], "source_requests": 0,
        "freshness": {"status": "within-declared-age-policy", "latest_market_close": "not-certified",
            "max_quote_age_days": policy["max_quote_age_days"], "calendar_through": plan["calendar_end"],
            "completion_policy": EOD_COMPLETION_POLICY,
            "observation_cutoff": cutoff.isoformat()},
        "scope": "SH_SZ_CNY_RAW_DATED_CLOSE_ONLY_NOT_EXECUTION_LATEST_OR_HISTORICAL_PIT",
        "limitations": ["SINGLE_RAW_CLOSE_NOT_FULL_MARKET_OR_HISTORY_WINDOW_QUALIFICATION",
            "SOURCE_CODE_NOT_PERMANENT_ISSUER_ID", "SOURCE_SNAPSHOTS_NOT_ATOMIC",
            "CORPORATE_ACTIONS_AND_FIXED_SHARE_COUNT_REQUIRE_RESEARCH_REVIEW",
            "NO_LIVE_SUSPENSION_OR_EXECUTABILITY_CERTIFICATION"]}
