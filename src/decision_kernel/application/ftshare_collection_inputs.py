"""Exact FTShare collections in the normal A-share reader; no source requests.

Whole-range raw and forward captures remain separate native queries. Forward
closes go directly to the existing observation core, without invented factors,
end-anchor claims or stitching prices from different adjustment requests.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib

from decision_kernel.application.ftshare_adoption import ADOPTION_PLAN_SCHEMA
from decision_kernel.application.market_collection_inputs import (
    _empty_endpoint_observation, _empty_observation, _reading_clock,
)
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.market_relay_inputs import _range
from decision_kernel.application.market_security_inputs import _within_listing
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded, require
from decision_kernel.observations.market_daily import MarketInputError, observe_daily, observe_endpoints

VIEW_SCHEMA = "ashare-collection-view-v2"
ENDPOINT_SCHEMA = "ashare-endpoint-comparisons-v2"
NORMALIZATION = "provider-supplied-forward-close-single-request-v1"
HORIZONS = (5, 20, 60)
VENUES = {"SSE": "SH", "SZSE": "SZ"}
KINDS = {"none": "raw", "forward": "qfq"}


def _reason(exc):
    from decision_kernel.application.market_collection import _reason as safe_reason
    return safe_reason(exc)


def _surface(kind, *, scope, page, cutoffs, versions, endpoint=False):
    result = {"kind": "stock", "as_of": next(iter(set(cutoffs.values())))
        if len(set(cutoffs.values())) == 1 else None,
        "as_of_by_exchange": cutoffs, "market": "SH/SZ", "currency": "CNY",
        "adjustment": kind, "calendar_version": versions["calendar"],
        "universe_version": versions["universe"], "source_version": versions[kind],
        "coverage": "declared-current-L-snapshots", "scope_count": scope,
        "known_scope_count": scope, "observed_count": 0, "missing_count": scope,
        "incomplete_count": scope, "entries": [], "page": dict(page),
        "horizon_counts": [{"sessions": h, "scope_count": scope,
            "observed_count": 0, "missing_count": scope} for h in HORIZONS]}
    if not endpoint:
        result.update(received_count=0, discovery_claim="not-established-by-supplied-universe")
        for window in result["horizon_counts"]:
            window.update(relative_observed_count=0, relative_missing_count=scope)
    return result


def _read_ftshare_inputs(collection, context, load_batch, *, offset=0, limit=50,
                         entity_id=None, read_as_of=None):
    """Private composition over the custody owner's independently checked inputs.

    ``load_batch`` restores the original native request, body and qualified capture;
    it is supplied by trusted Python code, never by a reading JSON document.
    Only compact close series are retained after each bounded batch is visited.
    """
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100,
            "PAGE_INVALID")
    from decision_kernel.application.ftshare_daily import _symbol
    if entity_id is not None:
        _symbol(entity_id)
    plan, units = collection["plan"], collection["units"]
    adopted = plan.get("schema") == ADOPTION_PLAN_SCHEMA
    lineage_key = "single_queries" if adopted else "batches"
    start, end = _range(plan["start"], plan["end"])
    codes = sorted(context["scope_symbols"])
    require(codes == sorted(set(codes)) and len(codes) <= 10000, "FTSHARE_READING_SCOPE")
    references = {code: value for master in context["masters"].values()
                  for code, value in master["securities"].items()}
    require(set(codes) == set(references), "FTSHARE_READING_SCOPE")
    sessions = context["sessions"]
    cutoffs = {venue: days[-1] if days else None for venue, days in sessions.items()}
    require(set(sessions) == set(VENUES), "FTSHARE_READING_CALENDARS")
    selected = codes if entity_id is None else [code for code in codes if code == entity_id]
    page = {"offset": offset, "limit": limit, "selected_count": len(selected),
        "entries_count": len(codes), "next_offset": offset + limit
        if offset + limit < len(selected) else None, "entity_id": entity_id}
    selected_codes = set(selected[offset:offset + limit])
    values = {kind: {} for kind in KINDS.values()}
    bad = {kind: {} for kind in KINDS.values()}
    owners = {kind: {} for kind in KINDS.values()}
    sources, lineage = {}, {"provider": "FTSHARE", "calendars": deepcopy(context["calendar_references"]),
        "masters": deepcopy(context["master_references"]), lineage_key: {}}
    if adopted:
        lineage["adoption"] = deepcopy(plan["adoption"])
    for role in ("calendar", "master"):
        for venue, reference in context[role + "_references"].items():
            sources[role + ":" + venue] = {"status": "restored", "reference": deepcopy(reference),
                "provider": "TUSHARE_RELAY", "reason_code": None}
    for slot, request in sorted(plan["requests"].items()):
        params = request["parameters"]
        kind = KINDS[params["adjust_kind"]]
        requested_codes = [params["symbol"]] if adopted else params["symbols"]
        for code in requested_codes:
            require(code in references and code not in owners[kind], "FTSHARE_READING_DUPLICATE_SERIES")
            owners[kind][code] = slot
        unit = units[slot]
        sources[slot] = {"status": "not-retained", "reference": deepcopy(unit.get("reference")),
            "provider": "FTSHARE", "reason_code": unit.get("reason_code")}
        if unit["status"] != "complete":
            continue
        try:
            capture = load_batch(unit["reference"], request=request)
            require(capture["request"] == request, "FTSHARE_READING_REQUEST_CHANGED")
        except StoreUnavailable:
            raise
        except (RecordError, MarketInputError) as exc:
            sources[slot].update(status="unavailable", reason_code=_reason(exc))
            continue
        normalized = capture["normalized"]
        require(normalized["provider"] == "FTSHARE"
            and normalized["adjust_kind"] == params["adjust_kind"]
            and set(normalized["series"]) == set(requested_codes), "FTSHARE_READING_CAPTURE_CHANGED")
        sources[slot].update(status="restored", reason_code=None)
        lineage[lineage_key][slot] = {"provider": "FTSHARE", "request": deepcopy(request),
            "request_sha256": capture["request_sha256"], "wire_sha256": capture["wire_sha256"],
            "acquired_at": capture["acquired_at"], "reference": deepcopy(unit["reference"]),
            "adjustment": normalized["adjustment"]}
        for code, rows in normalized["series"].items():
            values[kind][code] = [{"trade_date": row["trade_date"], "close": row["close"]} for row in rows]
        for item in normalized["quarantined"]:
            bad[kind].setdefault(item["symbol"], {})[item["trade_date"]] = item["reason"]
    versions = {"calendar": digest(encoded(lineage["calendars"])),
        "universe": digest(encoded({v: m["version"] for v, m in context["masters"].items()}))}
    for adjustment, kind in KINDS.items():
        slots = {slot: {"source": sources[slot], "lineage": lineage[lineage_key].get(slot),
            "request": request} for slot, request in plan["requests"].items()
            if request["parameters"]["adjust_kind"] == adjustment}
        versions[kind] = digest(encoded({"provider": "FTSHARE", "schema": VIEW_SCHEMA,
            "calendar": versions["calendar"], "universe": versions["universe"],
            "normalization": NORMALIZATION if kind == "qfq" else "unadjusted-source-close",
            "start": plan["start"], "end": plan["end"], "queries": slots}))
    surfaces = {kind: _surface(kind, scope=len(codes), page=page, cutoffs=cutoffs, versions=versions)
                for kind in KINDS.values()}
    endpoints = {kind: _surface(kind, scope=len(codes), page=page, cutoffs=cutoffs,
                              versions=versions, endpoint=True) for kind in KINDS.values()}
    hashes = {purpose: hashlib.sha256() for purpose in ("daily", "endpoints")}
    by_venue = {venue: {"scope_count": len(context["masters"][venue]["securities"]),
        "as_of": cutoffs[venue], "calendar_session_count": len(sessions[venue]),
        "reference_quarantined_count": sum(value is None for value in context["masters"][venue]["securities"].values()),
        "horizon_counts": {kind: [{"sessions": h, "observed_count": 0} for h in HORIZONS]
            for kind in KINDS.values()}} for venue in VENUES}
    for code in codes:
        venue = next(v for v, suffix in VENUES.items() if code.endswith("." + suffix))
        days, cutoff, reference = sessions[venue], cutoffs[venue], references[code]
        availability = ("reference-quarantined" if reference is None else "no-market-session-in-range"
            if cutoff is None else "outside-listing" if not _within_listing(cutoff, reference) else "eligible")
        for kind in KINDS.values():
            slot = owners[kind].get(code)
            input_status = sources[slot]["status"] if slot is not None else "not-requested"
            rows = values[kind].get(code, [])
            outside = [] if reference is None else [row["trade_date"] for row in rows
                                                   if not _within_listing(row["trade_date"], reference)]
            prices = [] if reference is None else [row for row in rows
                                                  if _within_listing(row["trade_date"], reference)]
            reason = None
            if reference is not None and cutoff is not None:
                try:
                    args = {"entity_id": code, "series": prices, "as_of": cutoff,
                        "market_sessions": days, "calendar_version": versions["calendar"],
                        "source_version": versions[kind], "adjustment": kind,
                        "market": venue, "currency": "CNY", "horizons": HORIZONS}
                    daily, endpoint = observe_daily(**args), observe_endpoints(**args)
                except MarketInputError as exc:
                    reason = _reason(exc)
            if reference is None or cutoff is None or reason is not None:
                why = reason or availability
                daily = _empty_observation(code, venue, cutoff, kind, why, versions)
                endpoint = _empty_endpoint_observation(code, venue, cutoff, kind, why, versions)
            qualification = {"availability": availability,
                "master": context["masters"][venue].get("qualification", {}).get(code),
                "input": {"status": input_status, "query_slot": slot,
                    "quarantined_dates": deepcopy(bad[kind].get(code, {})), "reason_code": reason},
                "outside_listing_dates": outside,
                "factor": {"status": "not-provided", "reconstructed": False},
                "adjustment": {"status": "provider-supplied-forward" if kind == "qfq" else "unadjusted",
                    "normalization": NORMALIZATION if kind == "qfq" else "unadjusted-source-close",
                    "anchor_status": "not-established-by-provider-response", "factor_reconstructed": False}}
            supplied = input_status == "restored" and bool(prices) and reason is None
            daily.update(input_status="supplied" if supplied else "not-supplied",
                security_reference=deepcopy(reference), qualification=qualification,
                history_quote_explanation={"status": "not-assessed", "evidence_status": "not-supplied",
                    "all_day_halt_certified": False, "raw_qfq_reclassified": False},
                lineage_representation="original-ftshare-whole-range-single-query" if adopted
                    else "original-ftshare-whole-range-batch-query",
                source_query_slot=slot)
            endpoint.update(security_reference=deepcopy(reference), availability=availability,
                qualification=qualification, source_query_slot=slot,
                lineage_representation="original-ftshare-whole-range-single-query" if adopted
                    else "original-ftshare-whole-range-batch-query")
            for purpose, observed, surface in (("daily", daily, surfaces[kind]),
                                               ("endpoints", endpoint, endpoints[kind])):
                seen = [window["status"] == "observed" for window in observed["horizons"]]
                surface["observed_count"] += int(any(seen))
                surface["missing_count"] -= int(any(seen))
                surface["incomplete_count"] -= int(all(seen))
                for index, accepted in enumerate(seen):
                    surface["horizon_counts"][index]["observed_count"] += int(accepted)
                    surface["horizon_counts"][index]["missing_count"] -= int(accepted)
                    if purpose == "daily":
                        by_venue[venue]["horizon_counts"][kind][index]["observed_count"] += int(accepted)
                if purpose == "daily":
                    surface["received_count"] += int(supplied)
                hashes[purpose].update(encoded(observed))
                if code in selected_codes:
                    surface["entries"].append(observed)
    for family in (surfaces, endpoints):
        for surface in family.values():
            surface["status"] = "empty-declared-universe" if not codes else "no-market-sessions" if not any(sessions.values()) else (
                ("comparison-gaps" if family is endpoints else "data-gaps")
                if surface["incomplete_count"] else "observed")
            surface["requested_end"] = end
    clock, through = _reading_clock(read_as_of, end)
    scope_requested = (not adopted or all(set(owners[kind]) == set(context["eligible_symbols"])
                                         for kind in KINDS.values()))
    complete = scope_requested and all(source["status"] == "restored" for source in sources.values())
    calendar_through = date(int(plan["calendar_end"][:4]), int(plan["calendar_end"][4:6]),
                            int(plan["calendar_end"][6:])).isoformat()
    for venue in VENUES:
        cutoff = cutoffs[venue]
        lag = sum(cutoff < day <= through for day in context["calendars"][venue]) if (
            clock is not None and cutoff is not None and complete and calendar_through >= through) else None
        by_venue[venue]["current_session_lag"] = lag
    lags = [venue["current_session_lag"] for venue in by_venue.values()]
    freshness = {"read_as_of": clock, "completed_day_through": through,
        "calendar_through": calendar_through,
        "status": "unknown" if any(lag is None for lag in lags) else "stale" if any(lags)
            else "current-through-completed-day",
        "basis": "original-verified-calendars-through-prior-Asia-Shanghai-day"}
    comparison = {"schema": ENDPOINT_SCHEMA, "comparison_basis": "calendar-endpoints-v1",
        "normalization": NORMALIZATION, "daily_path_certified": False, **endpoints}
    view = {"schema": VIEW_SCHEMA, "provider": "FTSHARE", "start": start,
        "requested_end": end, "as_of": surfaces["raw"]["as_of"], "as_of_by_exchange": cutoffs,
        "scope_count": len(codes), "known_scope_count": len(codes), "by_exchange": by_venue,
        "master": {venue: {key: deepcopy(value) for key, value in master.items()
            if key not in ("securities", "qualification", "lineage")} for venue, master in context["masters"].items()},
        "qualification_counts": {kind: surfaces[kind]["horizon_counts"] for kind in KINDS.values()},
        "normalization": NORMALIZATION, "source_lineage": lineage, "queries": sources,
        "freshness": freshness, "source_requests": 0,
        "source_hold": deepcopy(collection.get("source_hold")),
        "qualification_limits": ["DECLARED_CURRENT_L_REFERENCE_SNAPSHOT_NOT_HISTORICAL_UNIVERSE",
            "PROVIDER_FORWARD_SINGLE_REQUEST_NOT_END_ANCHORED_FACTOR_RECONSTRUCTION",
            "NO_FORWARD_SERIES_STITCHING_OR_PRICE_FILL", "NO_SUSPENSION_OR_TOTAL_RETURN_CERTIFICATION",
            "RESTORED_SOURCES_AND_WINDOWS_NOT_LONG_TERM_UPTIME_CERTIFICATION"]}
    view["view_version"] = digest(encoded({"context": view,
        "daily_sha256": hashes["daily"].hexdigest(), "endpoints_sha256": hashes["endpoints"].hexdigest()}))
    view.update(**surfaces, endpoint_comparisons=comparison)
    return view, sources


def read_ftshare_collection_version(reference, registry, store, *, offset=0, limit=50,
                                    entity_id=None, read_as_of=None):
    """Restore a pinned FTShare root and consume its actual retained native queries."""
    from decision_kernel.application.ftshare_collection import (
        restore_ftshare_batch, restore_ftshare_collection,
    )
    from decision_kernel.application.ftshare_single import restore_single
    with market_read_scope(store) as reader:
        restored = restore_ftshare_collection(reference, registry, reader)
        collection, context = restored["collection"], restored["context"]
        adopted = collection["plan"]["schema"] == ADOPTION_PLAN_SCHEMA
        loader = restore_single if adopted else restore_ftshare_batch
        view, sources = _read_ftshare_inputs(collection, context,
            lambda ref, *, request: loader(ref, reader, request=request),
            offset=offset, limit=limit, entity_id=entity_id, read_as_of=read_as_of)
        unavailable = sum(source["status"] != "restored" for source in sources.values())
        scope_requested = (not adopted or all({request["parameters"]["symbol"]
            for request in collection["plan"]["requests"].values()
            if request["parameters"]["adjust_kind"] == adjustment} == set(context["eligible_symbols"])
            for adjustment in KINDS))
        complete = scope_requested and all(unit["status"] == "complete" for unit in collection["units"].values())
        status = "complete" if complete else "partial"
        return {"status": "partial-restoration" if unavailable else "restored",
            "reference": deepcopy(restored["reference"]), "selected_reference": deepcopy(restored["reference"]),
            "serving": "current-complete" if complete else "current-partial",
            "latest_attempt": {"status": status, "basis": "retained-query-states",
                "reserved_requests": collection["reserved_requests"], "updated_at": collection["updated_at"],
                "source_hold": deepcopy(collection.get("source_hold")),
                **({"adoption": "four-original-captures-three-price-series", "source_requests": 0,
                    "historical_source_requests": 4, "cumulative_source_requests": 6}
                   if adopted else {})},
            "latest_requested_end": collection["plan"]["end"], "selected_requested_end": collection["plan"]["end"],
            "query_count": len(sources), "unavailable_queries": unavailable, "sources": sources,
            "view": view, "reading_error": None, "source_requests": 0,
            "original_http_restored": False, "ftshare_response_bodies_restored": not unavailable,
            "execution_diagnostics": "retained-ftshare-collection-units",
            "qualification": "original-response-bodies-and-calendar-windows-not-provider-uptime-certification"}
