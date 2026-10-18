"""Stream whole-market day captures into the existing raw/qfq observations.

The loader supplies original captures or their exact stored reading envelopes.
It owns source/storage access and its errors; this module never fetches, saves,
or invents per-security capture identities. Missing days remain explicit.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib

from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _day, _lineage, _range, _require, _security, _view,
    qualified_trade_calendar,
)
from decision_kernel.application.market_security_inputs import (
    NORMALIZATION, _adjust, _within_listing, inspect_security_master,
    qualified_security_master,
)
from decision_kernel.application.market_session_inputs import session_quotes, _reason
from decision_kernel.application.market_suspension_inputs import (
    EXPLANATIONS, MISSING_EXPLANATIONS, _explanation, session_suspensions,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import encoded, digest
from decision_kernel.observations.market_daily import MarketInputError, observe_daily, observe_endpoints


COLLECTION_READING_SCHEMA = "relay-ashare-collection-reading-v1"
COLLECTION_VIEW_SCHEMA = "relay-ashare-collection-view-v1"
ENDPOINT_COMPARISONS_SCHEMA = "relay-ashare-endpoint-comparisons-v1"
COLLECTION_REQUIRED = {"calendar_results", "master_results", "partitions", "start", "end", "calendar_end"}
PARTITION_APIS = ("daily", "adj_factor", "suspend_d")
HORIZONS = (5, 20, 60)
MAX_COLLECTION_DAYS = 120
MAX_COLLECTION_SECURITIES = 10000
MAX_PARTITION_BYTES = 8 * 1024 * 1024
VALUE_STATES = ("observed", "missing", "quarantined", "input-unavailable")


def _bounded(result):
    _require(isinstance(result, dict) and len(encoded(result)) <= MAX_PARTITION_BYTES,
             "COLLECTION_PARTITION_BOUND")
    return result


def _take(result, action):
    if result is None:
        return None, {"status": "not-supplied", "reason_code": None}
    try:
        _bounded(result)
        value = action()
    except (MarketInputError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return None, {"status": "rejected", "reason_code": _reason(exc)}
    return value, {"status": "checked", "reason_code": None}


def _reading_clock(value, end):
    if value is None:
        return None, None
    _require(isinstance(value, str) and len(value) <= 64, "COLLECTION_READ_AS_OF")
    try:
        clock = datetime.fromisoformat(value)
    except ValueError:
        _require(False, "COLLECTION_READ_AS_OF")
    _require(clock.utcoffset() is not None, "COLLECTION_READ_AS_OF")
    through = clock.astimezone(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    _require(date.fromisoformat(end) <= through, "COLLECTION_READ_AS_OF_BEFORE_END")
    return clock.isoformat(), through.isoformat()


def _fresh_calendar(result, registry, venue):
    """Inspect the proof's OWN original scope, never rewrite an older query."""
    use = result.get("use")
    _require(isinstance(use, dict) and isinstance(use.get("parameters"), dict), "COLLECTION_FRESHNESS_QUERY")
    params = use["parameters"]
    start, end = params.get("start_date"), params.get("end_date")
    lo, hi = _range(start, end)
    _require((date.fromisoformat(hi) - date.fromisoformat(lo)).days < MAX_COLLECTION_DAYS,
             "COLLECTION_FRESHNESS_BOUND")
    days = qualified_trade_calendar(result, registry=registry, exchange=venue, start=start, end=end)
    return {"start": lo, "end": hi, "sessions": days,
        "lineage": _lineage(_view(result, registry, "trade_cal", {"exchange": venue,
            "start_date": start, "end_date": end}, {"exchange", "cal_date", "is_open"}))}


def _value(api, code, day, values, bad, queries):
    if queries[day][api]["status"] != "checked":
        return "input-unavailable", None
    if day in bad[api].get(code, {}):
        return "quarantined", None
    value = values[api].get(code, {}).get(day)
    return ("observed", value) if value is not None else ("missing", None)


def _empty_observation(code, venue, end, convention, reason, versions):
    return {"entity_id": code, "as_of": end, "market": venue, "currency": "CNY",
        "adjustment": convention, "status": "input-unavailable", "reason_code": reason,
        "return_semantics": "price-ratio-not-total-return", "source_version": versions[convention],
        "calendar_version": versions["calendar"], "benchmark": None,
        "horizons": [{"sessions": h, "end_date": end, "status": reason} for h in HORIZONS]}


def _history(code, reference, sessions, queries, values, bad, suspension):
    prices, factors = [], []
    counts = {api: dict.fromkeys(VALUE_STATES, 0) for api in ("daily", "adj_factor")}
    dates = {api: {s: [] for s in VALUE_STATES if s != "observed"} for api in counts}
    explanations = dict.fromkeys(EXPLANATIONS, 0)
    explained_dates = {s: [] for s in EXPLANATIONS if s != "quote-observed"}
    unavailable_suspension, quarantined_suspension = [], []
    outside_listing = []
    for day in sessions:
        if not _within_listing(day, reference):
            outside_listing.append(day)
            continue
        states = {}
        for api, field, target in (("daily", "close", prices), ("adj_factor", "adj_factor", factors)):
            state, value = _value(api, code, day, values, bad, queries)
            states[api] = state
            counts[api][state] += 1
            if state == "observed":
                target.append({"trade_date": day, field: value})
            else:
                dates[api][state].append(day)
        source_ok = queries[day]["suspend_d"]["status"] == "checked"
        evidence = suspension.get(code, {}).get(day)
        if not source_ok:
            unavailable_suspension.append(day)
            evidence = {"status": "input-unavailable"}
        elif evidence is None:
            evidence = {"status": "no-record"}
        elif evidence["status"] == "quarantined":
            quarantined_suspension.append(day)
        explanation = _explanation({"availability": "eligible", "raw": {"status": states["daily"]}}, evidence)
        explanations[explanation] += 1
        if explanation != "quote-observed":
            explained_dates[explanation].append(day)
    explanation = {"status": "diagnosed", "eligible_session_count": sum(explanations.values()),
        "counts": explanations, "dates_by_status": {k: v for k, v in explained_dates.items() if v},
        "missing_quote_count": (None if counts["daily"]["input-unavailable"] else
            sum(explanations[k] for k in MISSING_EXPLANATIONS)),
        "evidence_status": "unavailable" if len(unavailable_suspension) == sum(explanations.values()) and unavailable_suspension
            else "partial" if unavailable_suspension else "checked",
        "evidence_unavailable_dates": unavailable_suspension,
        "evidence_quarantined_day_count": len(quarantined_suspension),
        "evidence_quarantined_dates": quarantined_suspension,
        "all_day_halt_certified": False, "raw_qfq_reclassified": False}
    qualification = {"price": {"status": "partitioned-inputs", "counts": counts["daily"], **dates["daily"]},
        "factor": {"status": "partitioned-inputs", "counts": counts["adj_factor"], **dates["adj_factor"]},
        "outside_listing_dates": outside_listing,
        "within_reported_listing_at_end": _within_listing(sessions[-1], reference)}
    for kind, api in (("price", "daily"), ("factor", "adj_factor")):
        qualification[kind]["quarantined_reasons"] = {d: bad[api][code][d] for d in dates[api]["quarantined"]}
    return prices, factors, qualification, explanation


def _endpoint_observations(code, venue, cutoff, days, reference, prices, factors,
                           queries, values, bad, versions):
    context = {"entity_id": code, "as_of": cutoff, "market_sessions": days,
        "calendar_version": versions["calendar"], "market": venue, "currency": "CNY",
        "horizons": HORIZONS}
    raw = observe_endpoints(**context, series=prices, adjustment="raw", source_version=versions["raw"])
    price_by_day = {row["trade_date"]: row for row in prices}
    factor_by_day = {row["trade_date"]: row for row in factors}
    adjusted_by_day, adjustments = {}, {}
    required_dates = sorted({day for window in raw["horizons"] for day in window.get("required_dates", [])})
    inputs = {}
    for day in required_dates:
        inputs[day] = {kind: _value(api, code, day, values, bad, queries)[0]
            if _within_listing(day, reference) else "outside-listing"
            for kind, api in (("price_state", "daily"), ("factor_state", "adj_factor"))}
    for window in raw["horizons"]:
        required = window.get("required_dates")
        if required is None:
            continue
        # Retain the existing end-anchored factor arithmetic, but give it exactly
        # this comparison's dependencies. An unrelated intermediate or other
        # horizon's invalid adjusted value cannot reject this pair.
        try:
            adjusted, detail = _adjust([price_by_day[d] for d in required if d in price_by_day],
                [factor_by_day[d] for d in required if d in factor_by_day], required, cutoff)
        except MarketInputError as exc:
            adjusted, detail = [], {"status": "rejected", "reason_code": _reason(exc)}
        adjusted_by_day.update((row["trade_date"], row) for row in adjusted)
        adjustments[window["sessions"]] = {
            "normalization": NORMALIZATION, "anchor_date": cutoff,
            "anchor_factor": factor_by_day.get(cutoff, {}).get("adj_factor"),
            "baseline_factor": factor_by_day.get(required[0], {}).get("adj_factor"),
            "missing_factor_dates": [d for d in required if d not in factor_by_day],
            "status": detail["status"],
            **({"reason_code": detail["reason_code"]} if "reason_code" in detail else {})}
    qfq = observe_endpoints(**context, series=[adjusted_by_day[d] for d in sorted(adjusted_by_day)],
        adjustment="qfq", source_version=versions["qfq"])
    for window in qfq["horizons"]:
        adjustment = adjustments.get(window["sessions"])
        if adjustment is not None:
            window["adjustment"] = adjustment
            if adjustment["status"] == "rejected":
                window["status"] = "invalid-endpoint-adjustment"
    raw["endpoint_inputs"] = {day: {"price_state": states["price_state"]} for day, states in inputs.items()}
    qfq["endpoint_inputs"] = inputs
    return {"raw": raw, "qfq": qfq}


def _empty_endpoint_observation(code, venue, cutoff, convention, reason, versions):
    return {"entity_id": code, "as_of": cutoff, "market": venue, "currency": "CNY",
        "adjustment": convention, "status": "input-unavailable", "reason_code": reason,
        "comparison_basis": "calendar-endpoints-v1", "return_semantics": "price-ratio-not-total-return",
        "source_version": versions[convention], "calendar_version": versions["calendar"],
        "endpoint_inputs": {},
        "horizons": [{"sessions": h, "end_date": cutoff, "status": reason} for h in HORIZONS]}


def read_ashare_collection(*, calendar_results, master_results, load_partition, registry,
        start, end, calendar_end, read_as_of=None, freshness_results=None, reference_policy="isolate-identified",
        offset=0, limit=50, entity_id=None):
    """Read at most 120 calendar dates, one original source partition at a time.

    All qualified actual-calendar dates are visited before pagination. The loader
    is called exactly once per date/API, never once per stock. It may return None
    for an explicitly unavailable query. Loader errors propagate so an exhausted
    store budget or access loss cannot become successful partial collection I/O.
    """
    lo, hi = _range(start, end)
    _, through = _range(end, calendar_end)
    _require((date.fromisoformat(through) - date.fromisoformat(lo)).days < MAX_COLLECTION_DAYS,
             "COLLECTION_DATE_BOUND")
    _require(isinstance(registry, RelayRegistry) and callable(load_partition), "COLLECTION_READER_REQUIRED")
    _require(reference_policy in ("strict", "isolate-identified"), "REFERENCE_POLICY")
    _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, "PAGE_INVALID")
    if entity_id is not None:
        _security(entity_id)
    for value in (calendar_results, master_results):
        _require(isinstance(value, Mapping) and value.keys() <= EXCHANGES.keys(), "COLLECTION_REFERENCE_MAP")
    _require(freshness_results is None or isinstance(freshness_results, Mapping)
             and freshness_results.keys() <= EXCHANGES.keys(), "COLLECTION_FRESHNESS_MAP")
    clock, completed_through = _reading_clock(read_as_of, hi)
    calendars, masters, references, queries = {}, {}, {}, {}
    lineage = {"calendars": {}, "masters": {}, "partitions": {}, "freshness_calendars": {}}
    for venue in EXCHANGES:
        result = calendar_results.get(venue)
        calendar, query = _take(result, lambda: qualified_trade_calendar(
            result, registry=registry, exchange=venue, start=start, end=calendar_end))
        queries["calendar:" + venue] = query
        calendars[venue] = calendar
        if calendar is not None:
            lineage["calendars"][venue] = _lineage(_view(result, registry, "trade_cal",
                {"exchange": venue, "start_date": start, "end_date": calendar_end},
                {"exchange", "cal_date", "is_open"}))
        result = master_results.get(venue)
        reader = inspect_security_master if reference_policy == "isolate-identified" else qualified_security_master
        master, query = _take(result, lambda: reader({"L": result}, registry=registry, exchange=venue))
        queries["master:" + venue] = query
        masters[venue] = master
        if master is not None:
            references.update(master["securities"])
            lineage["masters"][venue] = master["lineage"]
    _require(len(references) <= MAX_COLLECTION_SECURITIES, "COLLECTION_SECURITY_BOUND")
    sessions = {v: [d for d in c if d <= hi] if c is not None else None for v, c in calendars.items()}
    session_sets = {v: set(days) if days is not None else None for v, days in sessions.items()}
    cutoffs = {v: days[-1] if days else None for v, days in sessions.items()}
    common_as_of = next(iter(set(cutoffs.values()))) if len(set(cutoffs.values())) == 1 else None
    freshness_calendars = {}
    for venue in EXCHANGES:
        if freshness_results is None:
            freshness_calendars[venue] = ({"start": lo, "end": through, "sessions": calendars[venue]}
                if calendars[venue] is not None else None)
        else:
            result = freshness_results.get(venue)
            proof, query = _take(result, lambda: _fresh_calendar(result, registry, venue))
            queries["freshness:" + venue] = query
            freshness_calendars[venue] = proof
            if proof is not None:
                lineage["freshness_calendars"][venue] = proof["lineage"]
    actual_days = sorted({d for days in sessions.values() if days is not None for d in days})
    values = {api: defaultdict(dict) for api in ("daily", "adj_factor")}
    bad = {api: defaultdict(dict) for api in values}
    suspension, received, day_queries = defaultdict(dict), set(), {}
    identified = set(references)
    outside_calendar = {v: dict.fromkeys(PARTITION_APIS, 0) if session_sets[v] is not None else None for v in EXCHANGES}
    outside_listing = {v: dict.fromkeys(PARTITION_APIS, 0) if masters[v] is not None else None for v in EXCHANGES}
    for day in actual_days:
        compact = day.replace("-", "")
        day_queries[day], lineage["partitions"][day] = {}, {}
        for api in PARTITION_APIS:
            # Source/storage errors deliberately remain owned by the loader.
            result = load_partition(compact, api)
            checked, query = _take(result, lambda: session_suspensions(result, registry, compact)
                if api == "suspend_d" else session_quotes(result, registry, api, compact))
            day_queries[day][api] = query
            queries[compact + ":" + api] = query
            if checked is None:
                query.update(row_count=None, page_count=None, quarantined_count=None, outside_sh_sz_count=None)
                continue
            query.update(row_count=checked["row_count"], page_count=len(checked["lineage"]["pages"]),
                quarantined_count=checked["quarantined_count"], outside_sh_sz_count=checked["outside_sh_sz_count"])
            lineage["partitions"][day][api] = checked["lineage"]
            for code, entry in checked["entries"].items():
                if code.endswith(".BJ"):
                    continue
                received.add(code)
                identified.add(code)
                _require(len(identified) <= MAX_COLLECTION_SECURITIES, "COLLECTION_SECURITY_BOUND")
                venue = next(v for v, suffix in EXCHANGES.items() if code.endswith("." + suffix))
                if session_sets[venue] is not None and day not in session_sets[venue]:
                    outside_calendar[venue][api] += entry["source_row_count"]
                if references.get(code) is not None and not _within_listing(day, references[code]):
                    outside_listing[venue][api] += entry["source_row_count"]
                if api == "suspend_d":
                    suspension[code][day] = entry
                elif entry["status"] == "observed":
                    values[api][code][day] = entry["value"]
                else:
                    bad[api][code][day] = entry["reason_code"]
            # Keep only compact qualified values, exceptions and original lineage;
            # no collection of every original capture is constructed here.
            del checked, result
    known_scope = all(m is not None for m in masters.values())
    scope = len(references) if known_scope else None
    all_codes = sorted(references.keys() | received)
    selected = all_codes if entity_id is None else [c for c in all_codes if c == entity_id]
    selected_codes = set(selected[offset:offset + limit])
    page = {"offset": offset, "limit": limit, "selected_count": len(selected),
        "entries_count": len(all_codes), "next_offset": offset + limit if offset + limit < len(selected) else None,
        "entity_id": entity_id}
    versions = {"calendar": digest(encoded(lineage["calendars"])),
        "universe": digest(encoded({v: m["version"] if m is not None else None for v, m in masters.items()}))}
    for convention, apis in (("raw", ("daily",)), ("qfq", ("daily", "adj_factor"))):
        versions[convention] = digest(encoded({"schema": COLLECTION_VIEW_SCHEMA, "start": lo, "end": hi,
            "calendar": versions["calendar"], "universe": versions["universe"], "anchors": cutoffs,
            "normalization": NORMALIZATION if convention == "qfq" else None,
            "partitions": {d: {a: {"query": day_queries[d][a], "lineage": lineage["partitions"][d].get(a)}
                                  for a in apis} for d in actual_days}}))
    by_venue = {}
    for venue, suffix in EXCHANGES.items():
        master, calendar, days = masters[venue], calendars[venue], sessions[venue]
        refs = master["securities"] if master is not None else {}
        cutoff = cutoffs[venue]
        calendar_ok = cutoff is not None
        latest_by_api = {api: max((d for d in days or [] if day_queries[d][api]["status"] == "checked"), default=None)
                         for api in PARTITION_APIS}
        latest_complete = max((d for d in days or [] if all(day_queries[d][api]["status"] == "checked"
                                                           for api in PARTITION_APIS)), default=None)
        declared_lag = sum(d > cutoff for d in calendar) if calendar_ok else None
        proof = freshness_calendars[venue]
        current_lag = (sum(d > latest_complete for d in proof["sessions"])
            if clock is not None and latest_complete is not None and proof is not None
            and proof["end"] == completed_through and proof["start"] <= latest_complete <= proof["end"] else None)
        by_venue[venue] = {"scope_count": len(refs) if master is not None else None,
            "as_of": cutoff, "requested_end": hi,
            "eligible_count": sum(r is not None and _within_listing(cutoff, r) for r in refs.values()) if master is not None and calendar_ok else None,
            "reference_quarantined_count": sum(r is None for r in refs.values()) if master is not None else None,
            "unreferenced_count": len({c for c in received if c.endswith("." + suffix)} - refs.keys()) if master is not None else None,
            "calendar_status": ("checked-open" if cutoff == hi else "checked-closed-at-requested-end")
                if calendar_ok else "closed-range" if days is not None else "unavailable",
            "calendar_session_count": len(days) if days is not None else None,
            "declared_session_lag": declared_lag, "current_session_lag": current_lag,
            "freshness_calendar_range": {"start": proof["start"], "end": proof["end"]} if proof is not None else None,
            "latest_partition_dates": latest_by_api, "latest_complete_partition_date": latest_complete,
            "outside_calendar_row_counts": outside_calendar[venue],
            "outside_reported_listing_row_counts": outside_listing[venue],
            "horizon_counts": {k: [{"sessions": h, "observed_count": 0,
                "scope_count": len(refs) if master is not None else None} for h in HORIZONS] for k in ("raw", "qfq")},
            "eligible_session_value_counts": {api: dict.fromkeys(VALUE_STATES, 0) for api in values}
                if master is not None and calendar_ok else None,
            "quote_explanation_counts": dict.fromkeys(EXPLANATIONS, 0) if master is not None and calendar_ok else None}
    surfaces = {k: {"kind": "stock", "as_of": common_as_of, "as_of_by_exchange": cutoffs,
        "requested_end": hi, "market": "SH/SZ", "currency": "CNY", "adjustment": k,
        "calendar_version": versions["calendar"], "source_version": versions[k], "universe_version": versions["universe"],
        "coverage": "declared-current-L-snapshots", "scope_count": scope, "known_scope_count": len(references),
        "received_count": 0, "observed_count": 0, "missing_count": None, "incomplete_count": None,
        "horizon_counts": [{"sessions": h, "scope_count": scope, "observed_count": 0,
            "missing_count": None, "relative_observed_count": 0, "relative_missing_count": scope} for h in HORIZONS],
        "entries": [], "page": dict(page), "discovery_claim": "not-established-by-supplied-universe"}
        for k in ("raw", "qfq")}
    endpoint_surfaces = {k: {"kind": "stock", "as_of": common_as_of, "as_of_by_exchange": cutoffs,
        "requested_end": hi, "market": "SH/SZ", "currency": "CNY", "adjustment": k,
        "calendar_version": versions["calendar"], "source_version": versions[k], "universe_version": versions["universe"],
        "coverage": "declared-current-L-snapshots", "scope_count": scope, "known_scope_count": len(references),
        "observed_count": 0, "missing_count": None, "incomplete_count": None,
        "horizon_counts": [{"sessions": h, "scope_count": scope, "observed_count": 0,
            "missing_count": None} for h in HORIZONS], "entries": [], "page": dict(page)} for k in surfaces}
    complete = dict.fromkeys(surfaces, 0)
    endpoint_complete = dict.fromkeys(endpoint_surfaces, 0)
    entry_hash = hashlib.sha256()
    endpoint_entry_hash = hashlib.sha256()
    for code in all_codes:
        venue = next(v for v, suffix in EXCHANGES.items() if code.endswith("." + suffix))
        master, days, cutoff = masters[venue], sessions[venue], cutoffs[venue]
        reference = references.get(code)
        availability = ("reference-unavailable" if master is None else "unreferenced" if code not in references
            else "reference-quarantined" if reference is None else "calendar-unavailable" if days is None
            else "no-market-session-in-range" if cutoff is None else "outside-listing" if not _within_listing(cutoff, reference)
            else "eligible")
        if availability in ("eligible", "outside-listing"):
            prices, factors, qualification, explanation = _history(code, reference, days, day_queries, values, bad, suspension)
            try:
                adjusted, adjustment = _adjust(prices, factors, days, cutoff)
            except MarketInputError as exc:
                adjusted, adjustment = [], {"status": "rejected", "reason_code": _reason(exc)}
            qualification["adjustment"] = adjustment
            observations = {k: observe_daily(entity_id=code, series=series, as_of=cutoff,
                market_sessions=days, calendar_version=versions["calendar"], adjustment=k,
                source_version=versions[k], market=venue, currency="CNY", horizons=HORIZONS)
                for k, series in (("raw", prices), ("qfq", adjusted))}
            endpoint_observations = _endpoint_observations(code, venue, cutoff, days, reference,
                prices, factors, day_queries, values, bad, versions)
            supplied = {"raw": bool(prices), "qfq": bool(adjusted)}
        else:
            qualification = {"price": {"status": "unavailable"}, "factor": {"status": "unavailable"},
                "adjustment": {"status": "inputs-unavailable"}, "within_reported_listing_at_end": None}
            explanation = {"status": "not-assessed", "counts": None, "missing_quote_count": None,
                "evidence_status": "not-assessed", "evidence_quarantined_day_count": None}
            observations = {k: _empty_observation(code, venue, cutoff, k, availability, versions) for k in surfaces}
            endpoint_observations = {k: _empty_endpoint_observation(code, venue, cutoff, k, availability, versions)
                                     for k in surfaces}
            supplied = dict.fromkeys(surfaces, False)
        qualification["availability"] = availability
        qualification["master"] = master.get("qualification", {}).get(code) if master is not None else None
        if code in references and explanation["counts"] is not None:
            for status, count in explanation["counts"].items():
                by_venue[venue]["quote_explanation_counts"][status] += count
            for api, kind in (("daily", "price"), ("adj_factor", "factor")):
                for status, count in qualification[kind]["counts"].items():
                    by_venue[venue]["eligible_session_value_counts"][api][status] += count
        for convention, observed in observations.items():
            observed.update(input_status="supplied" if supplied[convention] else "not-supplied",
                security_reference=reference, qualification=qualification,
                history_quote_explanation=explanation,
                lineage_representation="original-whole-market-day-queries-in-collection-source-lineage")
            if code in references:
                surface = surfaces[convention]
                surface["received_count"] += int(supplied[convention])
                surface["observed_count"] += int(observed["status"] in ("observed", "partial-history"))
                complete[convention] += int(observed["status"] == "observed")
                for i, window in enumerate(observed["horizons"]):
                    seen = int(window["status"] == "observed")
                    surface["horizon_counts"][i]["observed_count"] += seen
                    by_venue[venue]["horizon_counts"][convention][i]["observed_count"] += seen
            entry_hash.update(encoded(observed))
            if code in selected_codes:
                surfaces[convention]["entries"].append(observed)
        for convention, observed in endpoint_observations.items():
            observed.update(security_reference=reference, availability=availability,
                lineage_representation="original-whole-market-day-queries-in-collection-source-lineage")
            if code in references:
                surface = endpoint_surfaces[convention]
                seen = [window["status"] == "observed" for window in observed["horizons"]]
                surface["observed_count"] += int(any(seen))
                endpoint_complete[convention] += int(all(seen))
                for i, qualified in enumerate(seen):
                    surface["horizon_counts"][i]["observed_count"] += int(qualified)
            endpoint_entry_hash.update(encoded(observed))
            if code in selected_codes:
                endpoint_surfaces[convention]["entries"].append(observed)
    for convention, surface in surfaces.items():
        surface["missing_count"] = scope - surface["observed_count"] if known_scope else None
        surface["incomplete_count"] = scope - complete[convention] if known_scope else None
        for window in surface["horizon_counts"]:
            window["missing_count"] = scope - window["observed_count"] if known_scope else None
        surface["status"] = "scope-unknown" if not known_scope else "empty-declared-universe" if not scope else (
            "no-market-sessions" if not actual_days and all(c is not None for c in calendars.values())
            else "data-gaps" if surface["incomplete_count"] else "observed")
    for convention, surface in endpoint_surfaces.items():
        surface["missing_count"] = scope - surface["observed_count"] if known_scope else None
        surface["incomplete_count"] = scope - endpoint_complete[convention] if known_scope else None
        for window in surface["horizon_counts"]:
            window["missing_count"] = scope - window["observed_count"] if known_scope else None
        surface["status"] = "scope-unknown" if not known_scope else "empty-declared-universe" if not scope else (
            "no-market-sessions" if not actual_days and all(c is not None for c in calendars.values())
            else "comparison-gaps" if surface["incomplete_count"] else "observed")
    endpoints = {"schema": ENDPOINT_COMPARISONS_SCHEMA, "comparison_basis": "calendar-endpoints-v1",
        "normalization": NORMALIZATION, "daily_path_certified": False, **endpoint_surfaces}
    freshness = {"read_as_of": clock, "completed_day_through": completed_through, "calendar_through": through,
        "status": "unknown" if clock is None or any(v["current_session_lag"] is None for v in by_venue.values())
            else "stale" if any(v["current_session_lag"] for v in by_venue.values()) else "current-through-completed-day",
        "basis": "explicit-complete-calendars-through-prior-Asia-Shanghai-day"}
    master_summary = {v: None if m is None else {k: m[k] for k in (
        "scope_count", "supplied_statuses", "not_supplied_statuses", "status_counts", "version",
        "scope_semantics", "time_semantics", "identity_semantics")}
        for v, m in masters.items()}
    out = {"schema": COLLECTION_VIEW_SCHEMA, "start": lo, "requested_end": hi, "as_of": common_as_of,
        "as_of_by_exchange": cutoffs, "calendar_through": through,
        "scope_count": scope, "known_scope_count": len(references), "reference_policy": reference_policy,
        "master": master_summary, "by_exchange": by_venue, "freshness": freshness,
        "qualification_counts": {"raw": surfaces["raw"]["horizon_counts"], "qfq": surfaces["qfq"]["horizon_counts"]},
        "normalization": NORMALIZATION, "queries": queries, "source_lineage": lineage,
        "actual_partition_dates": actual_days, "source_requests": 0,
        "qualification_limits": ["SH_SZ_CURRENT_L_RETRIEVAL_SNAPSHOTS_NOT_HISTORICAL_OR_ALL_A_SHARES",
            "BJ_ROWS_COUNTED_OUTSIDE_SUPPORTED_SCOPE; UNREFERENCED_SH_SZ_NEVER_QUALIFIED",
            "DAY_PARTITIONS_NOT_FABRICATED_PER_STOCK_CAPTURES; LINEAGE_NOT_SOURCEREF",
            "NO_PRICE_OR_FACTOR_FILL; HALT_SUPPORT_NOT_FULL_DAY_CERTIFICATION",
            "END_ANCHORED_FACTOR_RATIO_NOT_TOTAL_RETURN_OR_CORPORATE_ACTION_CERTIFICATION",
            "RESTORED_SOURCE_AND_WINDOW_USABILITY_SEPARATE; NO_UPTIME_OR_DISCOVERY_CERTIFICATION"]}
    endpoint_context = {key: value for key, value in endpoints.items() if key not in endpoint_surfaces}
    endpoint_context.update({kind: {key: value for key, value in surface.items() if key not in ("entries", "page")}
                            for kind, surface in endpoint_surfaces.items()})
    out["view_version"] = digest(encoded({"context": out, "entries_sha256": entry_hash.hexdigest(),
        "endpoint_comparisons": endpoint_context, "endpoint_entries_sha256": endpoint_entry_hash.hexdigest()}))
    out.update(surfaces)
    out["endpoint_comparisons"] = endpoints
    return out


def read_collection_payload(*, calendar_results, master_results, partitions, registry,
        start, end, calendar_end, read_as_of=None, freshness_results=None, reference_policy="isolate-identified",
        offset=0, limit=50, entity_id=None):
    """Small explicit in-memory adapter; stored collections use a streaming loader."""
    lo, hi = _range(start, end)
    _require(isinstance(partitions, Mapping) and len(partitions) <= MAX_COLLECTION_DAYS,
             "COLLECTION_PARTITION_MAP")
    for day, units in partitions.items():
        _require(lo <= _day(day) <= hi and isinstance(units, Mapping)
                 and units.keys() <= set(PARTITION_APIS), "COLLECTION_PARTITION_MAP")
    out = read_ashare_collection(calendar_results=calendar_results, master_results=master_results,
        load_partition=lambda day, api: partitions.get(day, {}).get(api), registry=registry,
        start=start, end=end, calendar_end=calendar_end, read_as_of=read_as_of,
        freshness_results=freshness_results, reference_policy=reference_policy,
        offset=offset, limit=limit, entity_id=entity_id)
    if all(out["queries"]["calendar:" + v]["status"] == "checked" for v in EXCHANGES):
        _require(set(partitions) <= {d.replace("-", "") for d in out["actual_partition_dates"]},
                 "COLLECTION_NON_MARKET_PARTITION")
    return out
