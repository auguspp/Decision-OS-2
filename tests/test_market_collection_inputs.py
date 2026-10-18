"""Original day captures, streaming composition and real window consumers.

The calendar/quotes are synthetic. These tests establish engineering semantics,
not source health, retention rights or production market coverage.
"""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application.market_collection_inputs import (
    COLLECTION_READING_SCHEMA, COLLECTION_VIEW_SCHEMA, read_ashare_collection,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_security_inputs import MASTER_FIELDS, FACTOR_FIELDS
from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.observations.market_daily import MarketInputError
from test_market_relay_inputs import REGISTRY
from test_market_session_coverage import SessionOpener, capture


START, END = "20260701", "20261016"
HOLIDAYS = {"20260810", "20261007"}  # Deliberately synthetic exchange closures.


def compact(value):
    return value.strftime("%Y%m%d")


def calendar_days(start, end):
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    return [lo + timedelta(days=n) for n in range((hi - lo).days + 1)]


def iso(value):
    return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()


class CollectionOpener(SessionOpener):
    def __init__(self, *, count=2, start=START, end=END, through=None):
        super().__init__(count=count)
        self.start, self.end, self.through = start, end, through or end
        self.edits, self.loader_calls = {}, []
        self.suspensions = {}
        self.days = [compact(d) for d in calendar_days(iso(start), iso(end))
                     if d.weekday() < 5 and compact(d) not in HOLIDAYS]
        self.index = {d: n for n, d in enumerate(self.days)}
        for venue in ("SSE", "SZSE"):
            self.rows["trade_cal", venue] = [dict(exchange=venue, cal_date=compact(d),
                is_open=int(d.weekday() < 5 and compact(d) not in HOLIDAYS))
                for d in calendar_days(iso(start), iso(self.through))]

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        api = url.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        if api in ("daily", "adj_factor", "suspend_d"):
            day = params["trade_date"]
            if api == "suspend_d":
                rows = deepcopy(self.suspensions.get(day, []))
            else:
                field = "close" if api == "daily" else "adj_factor"
                n = self.index[day]
                value = ("20" if n < 30 else "10") if api == "daily" else ("1" if n < 30 else "2")
                refs = [r for (source, _), entries in self.rows.items() if source == "stock_basic" for r in entries]
                rows = [dict(ts_code=r["ts_code"], trade_date=day, **{field: value})
                        for r in refs if r["list_date"] <= day]
                rows.append(dict(ts_code="830001.BJ", trade_date=day, **{field: value}))
            if (day, api) in self.edits:
                rows = self.edits[day, api](rows)
            self.rows[api, None] = rows
        return super().open(request, timeout)

    def references(self):
        calendars, masters = {}, {}
        for venue in ("SSE", "SZSE"):
            calendars[venue] = capture(self, "trade_cal", {"exchange": venue,
                "start_date": self.start, "end_date": self.through}, {"exchange", "cal_date", "is_open"})
            masters[venue] = capture(self, "stock_basic", {"exchange": venue, "list_status": "L"}, MASTER_FIELDS, 500)
        return calendars, masters

    def load(self, day, api):
        self.loader_calls.append((day, api))
        fields = SUSPENSION_FIELDS if api == "suspend_d" else FACTOR_FIELDS if api == "adj_factor" else {
            "ts_code", "trade_date", "close"}
        return capture(self, api, {"trade_date": day}, fields, 5000, 2)

    def read(self, *, loader=None, calendars=None, masters=None, **kwargs):
        if calendars is None or masters is None:
            current_calendars, current_masters = self.references()
            calendars = current_calendars if calendars is None else calendars
            masters = current_masters if masters is None else masters
        return read_ashare_collection(calendar_results=calendars, master_results=masters,
            load_partition=loader or self.load, registry=REGISTRY, start=self.start,
            end=self.end, calendar_end=self.through, **kwargs)


def entry(view, code="600000.SH", kind="raw"):
    return next(e for e in view[kind]["entries"] if e["entity_id"] == code)


def change(rows, code, **fields):
    return [{**r, **fields} if r["ts_code"] == code else r for r in rows]


def test_real_day_captures_produce_original_raw_and_qfq_windows_once_per_partition():
    op = CollectionOpener()
    view = op.read(read_as_of="2026-10-17T12:00:00+08:00")
    assert view["schema"] == COLLECTION_VIEW_SCHEMA and view["scope_count"] == 4
    assert view["raw"]["observed_count"] == view["qfq"]["observed_count"] == 4
    assert [Decimal(w["change_pct"]) for w in entry(view)["horizons"]] == [0, 0, -50]
    assert [Decimal(w["change_pct"]) for w in entry(view, kind="qfq")["horizons"]] == [0, 0, 0]
    assert len(op.loader_calls) == 3 * len(op.days) == len(set(op.loader_calls))
    assert {d for d, _ in op.loader_calls} == set(op.days)
    assert not {d for d, _ in op.loader_calls} & HOLIDAYS
    assert view["freshness"]["status"] == "current-through-completed-day"
    assert all(v["current_session_lag"] == 0 for v in view["by_exchange"].values())
    assert all(q["outside_sh_sz_count"] == 1 for slot, q in view["queries"].items()
               if slot.endswith((":daily", ":adj_factor")))
    assert len(view["source_lineage"]["partitions"]) == len(op.days)
    assert all(p["daily"]["parameters"].keys() == {"trade_date", "fields", "limit"}
               for p in view["source_lineage"]["partitions"].values())
    assert view["source_requests"] == 0


def test_bad_factor_is_date_and_security_local_without_changing_raw_or_peer():
    op = CollectionOpener()
    day = op.days[-12]
    op.edits[day, "adj_factor"] = lambda rows: change(rows, "600000.SH", adj_factor="0")
    view = op.read()
    assert all(w["status"] == "observed" for w in entry(view)["horizons"])
    qfq = entry(view, kind="qfq")
    assert [w["status"] for w in qfq["horizons"]] == ["observed", "missing-market-sessions", "missing-market-sessions"]
    assert qfq["qualification"]["factor"]["quarantined"] == [iso(day)]
    assert qfq["qualification"]["factor"]["quarantined_reasons"][iso(day)] == "MARKET_RELAY_FACTOR_VALUE"
    assert view["by_exchange"]["SSE"]["eligible_session_value_counts"]["adj_factor"]["quarantined"] == 1
    assert all(w["status"] == "observed" for w in entry(view, "000001.SZ", "qfq")["horizons"])
    assert view["freshness"]["status"] == "unknown"


def test_endpoints_survive_missing_intermediate_prices_and_out_of_range_intermediate_adjustment():
    op = CollectionOpener()
    missing, extreme = op.days[-3], op.days[-4]
    op.edits[missing, "daily"] = lambda rows: [row for row in rows if row["ts_code"] != "600000.SH"]
    op.edits[extreme, "daily"] = lambda rows: change(rows, "600000.SH", close="1e99")
    op.edits[extreme, "adj_factor"] = lambda rows: change(rows, "600000.SH", adj_factor="1e99")
    view = op.read()
    comparisons = view["endpoint_comparisons"]
    assert not comparisons["daily_path_certified"]
    assert all(w["status"] == "missing-market-sessions" for w in entry(view)["horizons"])
    assert entry(view, kind="qfq")["qualification"]["adjustment"]["status"] == "rejected"
    assert [Decimal(w["change_pct"]) for w in entry(comparisons)["horizons"]] == [0, 0, -50]
    assert [Decimal(w["change_pct"]) for w in entry(comparisons, kind="qfq")["horizons"]] == [0, 0, 0]
    for kind in ("raw", "qfq"):
        assert view[kind]["incomplete_count"] == 1 and view[kind]["status"] == "data-gaps"
        assert comparisons[kind]["incomplete_count"] == 0 and comparisons[kind]["scope_count"] == 4
        assert all(w["observed_count"] == 4 for w in comparisons[kind]["horizon_counts"])
        assert iso(missing) not in entry(comparisons, kind=kind)["endpoint_inputs"]
        assert iso(extreme) not in entry(comparisons, kind=kind)["endpoint_inputs"]
    assert view["freshness"]["status"] == "unknown" and view["source_requests"] == 0
    assert len(op.loader_calls) == len(set(op.loader_calls)) == 3 * len(op.days)


def test_bad_endpoint_factors_and_adjusted_range_only_limit_dependent_security_and_horizon():
    op = CollectionOpener()
    baseline20, baseline60 = op.days[-21], op.days[-61]
    op.edits[baseline60, "adj_factor"] = lambda rows: change(rows, "600000.SH", adj_factor="0")
    op.edits[baseline20, "daily"] = lambda rows: change(rows, "600000.SH", close="1e99")
    op.edits[baseline20, "adj_factor"] = lambda rows: change(rows, "600000.SH", adj_factor="1e99")
    view = op.read()
    comparisons = view["endpoint_comparisons"]
    assert all(w["status"] == "observed" for w in entry(comparisons)["horizons"])
    qfq = entry(comparisons, kind="qfq")
    assert qfq["status"] == "partial-comparisons"
    assert [w["status"] for w in qfq["horizons"]] == [
        "observed", "invalid-endpoint-adjustment", "missing-endpoint-observations"]
    assert qfq["endpoint_inputs"][iso(baseline60)]["factor_state"] == "quarantined"
    assert qfq["horizons"][2]["adjustment"]["missing_factor_dates"] == [iso(baseline60)]
    assert qfq["horizons"][1]["adjustment"]["reason_code"] == "MARKET_RELAY_ADJUSTED_CLOSE_RANGE"
    assert all(w["adjustment"]["anchor_date"] == iso(END) for w in qfq["horizons"])
    assert all(w["status"] == "observed" for w in entry(comparisons, "000001.SZ", "qfq")["horizons"])
    assert [w["observed_count"] for w in comparisons["qfq"]["horizon_counts"]] == [4, 3, 3]


@pytest.mark.parametrize("api", ["daily", "adj_factor", "suspend_d"])
def test_missing_last_partition_preserves_known_scope_and_truthful_freshness(api):
    op = CollectionOpener()
    def load(day, requested):
        return None if day == END and requested == api else op.load(day, requested)
    view = op.read(loader=load, read_as_of="2026-10-17T12:00:00+08:00")
    assert view["scope_count"] == 4 and view["queries"][END + ":" + api]["status"] == "not-supplied"
    assert view["freshness"]["status"] == "stale"
    assert all(v["current_session_lag"] == 1 for v in view["by_exchange"].values())
    assert view["as_of"] == iso(END)  # A lost last partition never moves the price anchor back.
    if api == "daily":
        assert entry(view)["qualification"]["price"]["input-unavailable"] == [iso(END)]
        assert entry(view)["history_quote_explanation"]["missing_quote_count"] is None
    else:
        assert all(w["status"] == "observed" for w in entry(view)["horizons"])
    if api == "adj_factor":
        assert all(w["status"] == "missing-market-sessions" for w in entry(view, kind="qfq")["horizons"])
    if api == "suspend_d":
        assert entry(view)["history_quote_explanation"]["evidence_unavailable_dates"] == [iso(END)]
        assert all(w["status"] == "observed" for w in entry(view, kind="qfq")["horizons"])
    comparisons = view["endpoint_comparisons"]
    for kind in ("raw", "qfq"):
        expected = "missing-endpoint-observations" if api == "daily" or api == "adj_factor" and kind == "qfq" else "observed"
        assert all(w["status"] == expected and w["end_date"] == iso(END)
                   for w in entry(comparisons, kind=kind)["horizons"])
    if api == "adj_factor":
        assert all(w["adjustment"]["status"] == "missing-anchor-factor"
                   for w in entry(comparisons, kind="qfq")["horizons"])


def test_whole_capture_failure_is_unknown_but_complete_empty_is_known_missing():
    op = CollectionOpener()
    op.edits[END, "daily"] = lambda rows: []
    empty = op.read()
    assert entry(empty)["qualification"]["price"]["missing"] == [iso(END)]
    assert entry(empty)["history_quote_explanation"]["missing_quote_count"] == 1
    op.edits[END, "daily"] = lambda rows: change(rows, "600000.SH", trade_date="20261015")
    bad = op.read()
    assert bad["queries"][END + ":daily"]["status"] == "rejected"
    assert entry(bad)["qualification"]["price"]["input-unavailable"] == [iso(END)]
    assert entry(bad)["history_quote_explanation"]["missing_quote_count"] is None


def test_new_listing_halt_and_conflict_are_explained_without_window_reclassification():
    op = CollectionOpener()
    op.rows["stock_basic", "SSE"][1]["list_date"] = op.days[-11]
    missing = op.days[-3]
    op.edits[missing, "daily"] = lambda rows: [r for r in rows if r["ts_code"] != "600000.SH"]
    op.suspensions[missing] = [dict(ts_code="600000.SH", trade_date=missing, suspend_type="S", suspend_timing=None)]
    op.suspensions[END] = [dict(ts_code="000001.SZ", trade_date=END, suspend_type="S", suspend_timing=None)]
    view = op.read()
    assert entry(view)["history_quote_explanation"]["counts"]["suspension-supported-missing"] == 1
    assert not entry(view)["history_quote_explanation"]["all_day_halt_certified"]
    assert all(w["status"] == "missing-market-sessions" for w in entry(view)["horizons"])
    assert [w["status"] for w in entry(view, "600001.SH")["horizons"]] == ["observed", "missing-market-sessions", "missing-market-sessions"]
    assert entry(view, "000001.SZ")["history_quote_explanation"]["counts"]["quote-suspension-conflict"] == 1
    assert all(w["status"] == "observed" for w in entry(view, "000001.SZ")["horizons"])
    assert view["scope_count"] == 4


def test_calendar_gaps_and_missing_master_do_not_borrow_other_venue_or_zero_scope():
    op = CollectionOpener()
    op.rows["trade_cal", "SSE"].pop(3)
    view = op.read()
    assert view["scope_count"] == 4 and view["by_exchange"]["SSE"]["calendar_status"] == "unavailable"
    assert entry(view)["qualification"]["availability"] == "calendar-unavailable"
    assert entry(view, "000001.SZ")["status"] == "observed"
    calendars, masters = CollectionOpener().references()
    del masters["SSE"]
    missing = CollectionOpener().read(calendars=calendars, masters=masters)
    assert missing["scope_count"] is None and missing["known_scope_count"] == 2
    assert missing["raw"]["missing_count"] is None and missing["raw"]["incomplete_count"] is None
    assert missing["raw"]["page"]["entries_count"] == 4
    assert entry(missing)["qualification"]["availability"] == "reference-unavailable"
    for kind in ("raw", "qfq"):
        assert entry(view["endpoint_comparisons"], kind=kind)["availability"] == "calendar-unavailable"
        assert entry(view["endpoint_comparisons"], "000001.SZ", kind)["status"] == "observed"
        surface = missing["endpoint_comparisons"][kind]
        assert surface["scope_count"] is None and surface["known_scope_count"] == 2
        assert surface["missing_count"] is None and surface["incomplete_count"] is None
        assert surface["observed_count"] == 2 and all(w["missing_count"] is None for w in surface["horizon_counts"])


def test_weekend_requested_end_uses_actual_exchange_session_without_a_price_fallback():
    op = CollectionOpener(end="20261018")
    view = op.read(read_as_of="2026-10-19T09:00:00+08:00")
    assert view["requested_end"] == "2026-10-18" and view["as_of"] == "2026-10-16"
    assert all(e["as_of"] == "2026-10-16" for e in view["raw"]["entries"])
    assert all(v["calendar_status"] == "checked-closed-at-requested-end" for v in view["by_exchange"].values())
    assert view["freshness"]["status"] == "current-through-completed-day"
    assert not any(d in ("20261017", "20261018") for d, _ in op.loader_calls)


def test_closed_range_and_different_venue_cutoff_remain_explicit():
    op = CollectionOpener(start="20261017", end="20261018")
    closed = op.read(read_as_of="2026-10-19T09:00:00+08:00")
    assert closed["scope_count"] == 4 and closed["raw"]["status"] == "no-market-sessions"
    assert closed["as_of"] is None and op.loader_calls == []
    op = CollectionOpener()
    op.rows["trade_cal", "SSE"][-1]["is_open"] = 0
    different = op.read()
    assert different["as_of"] is None
    assert different["as_of_by_exchange"] == {"SSE": "2026-10-15", "SZSE": "2026-10-16"}
    assert different["by_exchange"]["SSE"]["outside_calendar_row_counts"]["daily"] == 2
    assert entry(different)["horizons"][0]["end_date"] == "2026-10-15"
    assert entry(different, "000001.SZ")["horizons"][0]["end_date"] == "2026-10-16"
    endpoints = different["endpoint_comparisons"]
    assert endpoints["raw"]["as_of"] is None
    assert entry(endpoints)["horizons"][0]["end_date"] == "2026-10-15"
    assert entry(endpoints, "000001.SZ")["horizons"][0]["end_date"] == "2026-10-16"


def test_old_calendar_zero_lag_is_not_present_freshness_and_fresh_proof_counts_real_sessions():
    old = CollectionOpener().read(read_as_of="2026-10-20T09:00:00+08:00")
    assert old["by_exchange"]["SSE"]["declared_session_lag"] == 0
    assert old["freshness"]["status"] == "unknown"
    current = CollectionOpener(through="20261019").read(read_as_of="2026-10-20T09:00:00+08:00")
    assert current["freshness"]["status"] == "stale"
    assert current["by_exchange"]["SSE"]["current_session_lag"] == 1


def test_old_version_uses_separate_current_calendar_with_its_original_distinct_scope():
    prior = CollectionOpener().read()
    fresh = CollectionOpener(start="20261015", end="20261019").references()[0]
    before = encoded(fresh)
    view = CollectionOpener().read(freshness_results=fresh, read_as_of="2026-10-20T09:00:00+08:00")
    assert encoded(fresh) == before
    assert view["as_of"] == "2026-10-16" and view["freshness"]["status"] == "stale"
    assert view["by_exchange"]["SSE"]["current_session_lag"] == 1
    assert view["by_exchange"]["SSE"]["freshness_calendar_range"] == {"start": "2026-10-15", "end": "2026-10-19"}
    assert view["source_lineage"]["freshness_calendars"]["SSE"]["parameters"]["start_date"] == "20261015"
    for kind in ("raw", "qfq"):
        assert view[kind]["source_version"] == prior[kind]["source_version"]
        assert entry(view, kind=kind)["horizons"] == entry(prior, kind=kind)["horizons"]


def test_missing_or_noncovering_current_freshness_proof_does_not_destroy_old_windows():
    for fresh in ({}, CollectionOpener(start="20261019", end="20261019").references()[0]):
        view = CollectionOpener().read(freshness_results=fresh, read_as_of="2026-10-20T09:00:00+08:00")
        assert view["freshness"]["status"] == "unknown"
        assert view["by_exchange"]["SSE"]["current_session_lag"] is None
        assert entry(view)["status"] == entry(view, kind="qfq")["status"] == "observed"


def test_pagination_filter_and_inline_dispatch_keep_same_counts_versions_and_input_bytes():
    op = CollectionOpener(start="20261012")
    calendars, masters = op.references()
    partitions = {d: {api: op.load(d, api) for api in ("daily", "adj_factor", "suspend_d")} for d in op.days}
    payload = {"schema": COLLECTION_READING_SCHEMA, "calendar_results": calendars, "master_results": masters,
        "partitions": partitions, "start": op.start, "end": op.end, "calendar_end": op.through}
    before = encoded(payload)
    first = read_market_payload(payload, REGISTRY, limit=1)
    second = read_market_payload(payload, REGISTRY, offset=1, limit=1)
    selected = read_market_payload(payload, REGISTRY, entity_id="600000.SH")
    absent = read_market_payload(payload, REGISTRY, entity_id="999999.SZ")
    assert encoded(payload) == before
    assert first["view_version"] == second["view_version"] == selected["view_version"] == absent["view_version"]
    assert all(v["raw"]["horizon_counts"] == first["raw"]["horizon_counts"] for v in (second, selected, absent))
    assert first["raw"]["page"] == first["qfq"]["page"] and first["raw"]["page"]["next_offset"] == 1
    assert absent["raw"]["entries"] == [] and absent["scope_count"] == 4
    stream = op.read(calendars=calendars, masters=masters, loader=lambda d, a: partitions[d][a], limit=1)
    assert stream == first


def test_endpoint_denominator_keeps_quarantine_new_listing_and_off_page_result_identity(monkeypatch):
    from decision_kernel.application import market_collection_inputs as composition

    op = CollectionOpener()
    op.rows["stock_basic", "SSE"].append(deepcopy(op.rows["stock_basic", "SSE"][0]))
    op.rows["stock_basic", "SSE"][1]["list_date"] = op.days[-11]
    op.edits[END, "daily"] = lambda rows: [*rows, {"ts_code": "600099.SH", "trade_date": END, "close": "10"}]
    calendars, masters = op.references()
    partitions = {day: {api: op.load(day, api) for api in ("daily", "adj_factor", "suspend_d")} for day in op.days}
    def read(**page):
        return op.read(calendars=calendars, masters=masters, loader=lambda day, api: partitions[day][api], **page)
    first, second, absent = read(limit=1), read(limit=1, offset=1), read(entity_id="999999.SZ")
    isolated, listed = read(entity_id="600000.SH"), read(entity_id="600001.SH")
    assert first["view_version"] == second["view_version"] == absent["view_version"] == isolated["view_version"]
    for kind in ("raw", "qfq"):
        surface = first["endpoint_comparisons"][kind]
        assert surface["scope_count"] == 4 and surface["observed_count"] == 3
        assert surface["missing_count"] == 1 and surface["incomplete_count"] == 2
        assert [w["observed_count"] for w in surface["horizon_counts"]] == [3, 2, 2]
        assert surface["horizon_counts"] == second["endpoint_comparisons"][kind]["horizon_counts"]
        assert surface["page"]["entries_count"] == 5 and surface["page"] == first[kind]["page"]
        assert absent["endpoint_comparisons"][kind]["entries"] == []
        assert entry(isolated["endpoint_comparisons"], kind=kind)["availability"] == "reference-quarantined"
        assert [w["status"] for w in entry(listed["endpoint_comparisons"], "600001.SH", kind)["horizons"]] == [
            "observed", "missing-endpoint-observations", "missing-endpoint-observations"]
    # A purpose change outside the selected page must affect result identity even
    # when all input identities, old strict results and visible entries are equal.
    original = composition.observe_endpoints
    def changed_purpose(**kwargs):
        result = original(**kwargs)
        if kwargs["entity_id"] == "600001.SH":
            result["comparison_basis"] = "synthetic-alternate-purpose"
        return result
    monkeypatch.setattr(composition, "observe_endpoints", changed_purpose)
    changed = read(limit=1)
    assert changed["raw"] == first["raw"] and changed["qfq"] == first["qfq"]
    assert changed["endpoint_comparisons"] == first["endpoint_comparisons"]
    assert changed["view_version"] != first["view_version"]


def test_inline_non_market_partition_cannot_hide_unconsumed_source_input():
    op = CollectionOpener(start="20261012", end="20261018")
    calendars, masters = op.references()
    payload = {"schema": COLLECTION_READING_SCHEMA, "calendar_results": calendars, "master_results": masters,
        "partitions": {"20261017": {"daily": None}}, "start": op.start, "end": op.end, "calendar_end": op.through}
    with pytest.raises(MarketInputError, match="COLLECTION_NON_MARKET_PARTITION"):
        read_market_payload(payload, REGISTRY)


def test_loader_budget_error_propagates_and_invalid_plan_calls_no_loader():
    op = CollectionOpener(start="20261012")
    calls = []
    def exhausted(day, api):
        calls.append((day, api))
        raise RecordError("REMOTE_REQUEST_BUDGET")
    with pytest.raises(RecordError, match="REMOTE_REQUEST_BUDGET"):
        op.read(loader=exhausted)
    assert calls == [("20261012", "daily")]
    for changes in ({"limit": True}, {"entity_id": "830001.BJ"}, {"read_as_of": "2026-10-17"}):
        with pytest.raises(MarketInputError):
            op.read(loader=exhausted, **changes)
    assert calls == [("20261012", "daily")]


def test_extreme_adjusted_value_only_rejects_qfq_dependency():
    op = CollectionOpener(start="20261001")
    for day in op.days:
        op.edits[day, "daily"] = lambda rows: change(rows, "600000.SH", close="1e99")
        factor = "1e-99" if day == END else "1e99"
        op.edits[day, "adj_factor"] = lambda rows, value=factor: change(rows, "600000.SH", adj_factor=value)
    view = op.read()
    assert entry(view)["horizons"][0]["status"] == "observed"
    assert entry(view, kind="qfq")["qualification"]["adjustment"]["status"] == "rejected"
    assert entry(view, "000001.SZ", "qfq")["horizons"][0]["status"] == "observed"


def test_more_than_5000_securities_across_multiple_day_partitions_without_per_stock_calls():
    op = CollectionOpener(count=5001, start="20260720")
    view = op.read(limit=1)
    assert view["scope_count"] == 5003 and view["raw"]["page"]["selected_count"] == 5003
    assert len(op.days) >= 61
    assert all(w["observed_count"] == 5003 for kind in ("raw", "qfq") for w in view[kind]["horizon_counts"])
    assert len(op.loader_calls) == 3 * len(op.days)
    assert all(q["page_count"] == 2 for slot, q in view["queries"].items() if slot.endswith((":daily", ":adj_factor")))
    assert len(encoded(view)) < 2 * 1024 * 1024
