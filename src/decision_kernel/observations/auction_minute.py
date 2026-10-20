"""An exact 09:39–09:40 point on the original complete auction cohort.

Pure observations, with no transport, clock owner, ranking or cross-source
return. Input qualification and exact-root restoration belong to the caller.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone

from decision_kernel.domain.records import digest, encoded, require
from decision_kernel.observations.market_daily import MarketInputError, trade_day
from decision_kernel.observations.opening_auction import CODE, GROUPS, PROFILE, number, text

VIEW_SCHEMA = "ftshare-auction-minute-view-v1"
PROVIDER = "FTSHARE_STOCK_MINUTES_BATCH"
ENDPOINT = "https://market.ft.tech/gateway/api/v2/market/data/stock_minutes/batch"
BATCH_SIZE, MAX_BATCHES = 20, 16
ZONE = timezone(timedelta(hours=8))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ADJUSTMENT = "OMITTED_OFFICIAL_NONE_DEFAULT"


def session_window(market_session):
    day = trade_day(market_session)
    def ms(hour, minute):
        delta = datetime.combine(day, time(hour, minute), tzinfo=ZONE) - EPOCH
        value = (delta.days * 86400 + delta.seconds) * 1000
        require(value > 0, "MINUTE_REQUEST_WINDOW")
        return value
    return {"since_ts_millis": ms(9, 30), "until_ts_millis": ms(9, 41),
            "target_open_ms": ms(9, 39), "target_end_ms": ms(9, 40)}


def minute_request(symbols, market_session):
    require(isinstance(symbols, list) and 1 <= len(symbols) <= BATCH_SIZE
            and all(isinstance(s, str) and CODE.fullmatch(s) for s in symbols)
            and len(set(symbols)) == len(symbols), "MINUTE_REQUEST_SYMBOLS")
    window = session_window(market_session)
    return {"method": "GET", "url": ENDPOINT, "parameters": {
        "symbols": list(symbols), "interval_value": 1, "limit": 1000,
        "since_ts_millis": window["since_ts_millis"], "until_ts_millis": window["until_ts_millis"]}}


def cohort_rows(seed):
    """Use a restored auction view, never a page or optional later close join."""
    require(isinstance(seed, dict) and seed.get("schema") == "relay-auction-context-v1"
            and seed.get("profile") == PROFILE and type(seed.get("cohort_denominator")) is int
            and isinstance(seed.get("cohort_rows"), list)
            and seed["cohort_denominator"] == len(seed["cohort_rows"]), "MINUTE_AUCTION_COHORT_REQUIRED")
    rows, identities, members = [], set(), set()
    for row in seed["cohort_rows"]:
        require(isinstance(row, dict) and row.get("group") in GROUPS
                and isinstance(row.get("cohort_id"), str) and row["cohort_id"] not in members,
                "MINUTE_AUCTION_COHORT_IDENTITY")
        symbol = row.get("entity_id")
        require(symbol is None or isinstance(symbol, str) and CODE.fullmatch(symbol)
                and symbol not in identities, "MINUTE_AUCTION_COHORT_IDENTITY")
        members.add(row["cohort_id"])
        if symbol is not None:
            identities.add(symbol)
        rows.append({key: deepcopy(row[key]) for key in (
            "entity_id", "cohort_id", "name", "group", "status", "auction_price",
            "auction_pre_close", "gap_pct", "prior_turnover_pct", "prior_row_indices")})
    return rows


def minute_plan(seed, reference):
    rows = cohort_rows(seed)
    symbols = [row["entity_id"] for row in rows if row["entity_id"] is not None]
    day = seed["market_session"]
    window = session_window(day)
    return {"schema": "ftshare-auction-minute-plan-v1", "provider": PROVIDER,
        "auction_reference": deepcopy(reference), "cohort_version": seed["cohort_version"],
        "market_session": day, "cohort_denominator": len(rows), **window,
        "requested_adjust": ADJUSTMENT, "symbol_encoding": "REPEATED_QUERY_PARAMETERS",
        "batch_budget": MAX_BATCHES,
        "batches": [minute_request(symbols[i:i + BATCH_SIZE], day)
                    for i in range(0, min(len(symbols), MAX_BATCHES * BATCH_SIZE), BATCH_SIZE)],
        "unrequested_symbols": symbols[MAX_BATCHES * BATCH_SIZE:]}


def observe_symbol(item, plan):
    result = {"status": "MINUTE_ITEMS_UNAVAILABLE", "close_0940": None, "row_index": None,
        "source_ts_millis": None, "source_ts_millis_open": None, "source_close": None,
        "reported_total": deepcopy(item.get("total")), "returned_count": None,
        "return_count_consistent": False, "timestamp_semantics": "EXACT_ONE_MINUTE_BOUNDARY_REQUIRED"}
    items = item.get("items")
    if not isinstance(items, list) or len(items) > 1000:
        return result
    result.update(returned_count=len(items), return_count_consistent=(
        type(item.get("total")) is int and item["total"] == len(items)))
    matches = [(index, row) for index, row in enumerate(items) if isinstance(row, dict)
               and type(row.get("ts_millis")) is int and row["ts_millis"] == plan["target_end_ms"]]
    result["status"] = "TARGET_MINUTE_AMBIGUOUS" if matches else "TARGET_MINUTE_NOT_RETURNED"
    if len(matches) != 1:
        return result
    index, row = matches[0]
    result.update(row_index=index, source_ts_millis=row["ts_millis"], source_close=deepcopy(row.get("close")),
                  source_ts_millis_open=deepcopy(row.get("ts_millis_open")))
    if type(row.get("ts_millis_open")) is not int or row["ts_millis_open"] != plan["target_open_ms"]:
        result["status"] = "TARGET_MINUTE_TIME_UNQUALIFIED"
        return result
    try:
        close = number(row.get("close"))
        require(close > 0, "MINUTE_CLOSE_POSITIVE")
    except (MarketInputError, ValueError):
        result["status"] = "TARGET_CLOSE_INVALID"
        return result
    result.update(close_0940=text(close), status="EXACT_0940_CLOSE_OBSERVED",
        timestamp_semantics="PROVIDER_0939_OPEN_0940_CLOSE_BOUNDARY_NOT_RECEIPT_TIME")
    return result


def minute_observation(seed, plan, receipt, responses, *, unavailable=(), offset=0, limit=50, entity_id=None):
    """Join all original members first; a page cannot change any denominator."""
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
            "PAGE_INVALID")
    require(entity_id is None or isinstance(entity_id, str) and CODE.fullmatch(entity_id), "MINUTE_ENTITY_INVALID")
    by_symbol, unexpected = {}, []
    for record in receipt["records"]:
        index, names = record["index"], record["request"]["parameters"]["symbols"]
        if index in unavailable or record["outcome"] != "RESPONSE_SAVED":
            state = "ORIGINAL_UNAVAILABLE" if index in unavailable else record["outcome"]
            for name in names:
                by_symbol[name] = {"status": state, "close_0940": None, "request_index": index}
            continue
        data = responses[index]
        counts = Counter(row.get("symbol") for row in data
                         if isinstance(row, dict) and isinstance(row.get("symbol"), str))
        unexpected.extend({"request_index": index, "row_index": i, "reason": "UNREQUESTED_OR_INVALID_IDENTITY"}
                          for i, row in enumerate(data) if not isinstance(row, dict) or row.get("symbol") not in names)
        for name in names:
            if counts[name] != 1:
                value = {"status": "SYMBOL_AMBIGUOUS" if counts[name] else "SYMBOL_NOT_RETURNED", "close_0940": None}
            else:
                value = observe_symbol(next(row for row in data if isinstance(row, dict) and row.get("symbol") == name), plan)
            by_symbol[name] = {**value, "request_index": index, "source_sha256": record["sha256"],
                               "requested_at": record["requested_at"], "received_at": record["received_at"]}
    rows = []
    for original in cohort_rows(seed):
        name = original["entity_id"]
        state = ("COHORT_IDENTITY_UNRESOLVED" if name is None else "REQUEST_BUDGET_NOT_REQUESTED"
                 if name in plan["unrequested_symbols"] else "NOT_REQUESTED_AFTER_SOURCE_STOP")
        rows.append({**original, "minute": deepcopy(by_symbol.get(name, {"status": state, "close_0940": None}))})
    count = sum(row["minute"]["close_0940"] is not None for row in rows)
    groups = {key: {"denominator": sum(row["group"] == key for row in rows),
                    "observed_0940": sum(row["group"] == key and row["minute"]["close_0940"] is not None for row in rows)}
              for key in GROUPS}
    result = {"schema": VIEW_SCHEMA, "provider": PROVIDER, "market_session": plan["market_session"],
        "auction_reference": deepcopy(plan["auction_reference"]), "cohort_version": plan["cohort_version"],
        "cohort_denominator": len(rows), "observed_0940": count, "cohort_rows": rows, "groups": groups,
        "status": ("EMPTY_RETURNED_COHORT" if not rows else "DATED_MINUTE_OBSERVATIONS" if count == len(rows)
                   else "DATED_MINUTE_OBSERVATIONS_WITH_GAPS" if count else "MINUTE_INPUT_UNAVAILABLE_NOT_QUIET"),
        "coverage_gaps": bool(seed["coverage_gaps"] or count != len(rows)),
        "seed_context": {key: deepcopy(seed[key]) for key in (
            "profile", "previous_session", "prior_temperature", "coverage", "coverage_gaps", "timeliness")},
        "acquisition": {"started_at": receipt["started_at"], "finished_at": receipt["finished_at"],
            "provided_attempt_count": len(receipt["records"]), "source_stop": receipt["stopped"],
            "capture_timing": "HISTORICAL_OR_LATE_NOT_0940_DISCOVERY"},
        "target_open_time": plan["market_session"] + "T09:39:00+08:00",
        "target_close_time": plan["market_session"] + "T09:40:00+08:00",
        "unrequested_symbols": deepcopy(plan["unrequested_symbols"]), "unexpected_rows": unexpected,
        "price_basis": ADJUSTMENT, "units": {"close_0940": "provider_security_price_no_conversion",
                                                "auction_price": "CNY_from_original_Relay"},
        "cross_provider_return": "NOT_COMPUTED", "intraday_leadership": "NOT_ESTABLISHED",
        "economic_result": "NOT_EVALUATED", "source_requests": 0,
        "limitations": ["SUPPLIED_RECEIPT_NOT_AUTHENTICATED_SOURCE_EXECUTION_OR_RIGHTS",
            "SDK_RAW_IS_DECODED_JSON_NOT_ORIGINAL_WIRE_BYTES",
            "EXACT_POINT_NOT_INTRADAY_PATH_TRADABILITY_OR_ODDS",
            "RETURNED_SEED_COHORT_NOT_ALL_MARKET_OR_NATURAL_UPDATE_PROOF"],
        "human_attention_authority": "NONE", "research_authority": "NONE", "investment_authority": "NONE"}
    result["view_version"] = digest(encoded(result))
    selected = [row for row in rows if entity_id is None or row["entity_id"] == entity_id]
    result["entries"] = deepcopy(selected[offset:offset + limit])
    result["pagination"] = {"offset": offset, "limit": limit, "entity_id": entity_id,
        "matched_count": len(selected), "returned": len(result["entries"]),
        "next_offset": offset + limit if offset + limit < len(selected) else None}
    return result
