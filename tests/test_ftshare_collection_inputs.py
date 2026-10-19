"""Synthetic same-request forward prices through the normal calendar/window core."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import json

import pytest

from decision_kernel.application.ftshare_collection_inputs import (
    NORMALIZATION, VIEW_SCHEMA, _read_ftshare_inputs,
)
from decision_kernel.application.ftshare_daily import batch_request, capture_batch
from decision_kernel.domain.records import RecordError, StoreUnavailable, digest, encoded

CN = timezone(timedelta(hours=8))
CODES = ["000001.SZ", "600000.SH", "600001.SH"]
CLOCK = "2026-10-18T12:00:00+08:00"


def millis(day, closing=False):
    stamp = datetime.combine(date.fromisoformat(day), time(23, 59, 59, 999000) if closing else time(0), CN)
    return int(stamp.timestamp() * 1000)


def reference(label):
    return {"manifest": {"repository": "auguspp/Decision-OS-2", "commit": "a" * 40,
        "path": "source-views/" + label + "/" + "b" * 64 + "/manifest.json", "sha256": "c" * 64},
        "selection": {"kind": "all", "start": None, "end": None, "columns": []}}


def inputs(*, edit=None, count_days=None):
    start, end = date(2026, 7, 1), date(2026, 10, 16)
    days = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)
            if (start + timedelta(days=n)).weekday() < 5]
    if count_days is not None:
        days = days[-count_days:]
        start = date.fromisoformat(days[0])
    context = {"scope_symbols": CODES[:], "eligible_symbols": CODES[:2], "quarantined_symbols": CODES[2:],
        "sessions": {v: days[:] for v in ("SSE", "SZSE")},
        "calendars": {v: days[:] for v in ("SSE", "SZSE")}, "masters": {},
        "calendar_references": {v: reference("calendar-" + v.lower()) for v in ("SSE", "SZSE")},
        "master_references": {v: reference("master-" + v.lower()) for v in ("SSE", "SZSE")}}
    for venue, suffix in (("SSE", ".SH"), ("SZSE", ".SZ")):
        securities = {code: ({"ts_code": code, "symbol": code[:6], "name": "Synthetic",
            "exchange": venue, "currency": "CNY", "source_list_status": "L",
            "list_date": "2000-01-01", "delist_date": None} if code != CODES[2] else None)
            for code in CODES if code.endswith(suffix)}
        context["masters"][venue] = {"securities": securities, "scope_count": len(securities),
            "version": digest(encoded(securities)), "qualification": {}}
    plan = {"start": start.strftime("%Y%m%d"), "end": "20261016", "calendar_end": "20261017", "requests": {}}
    captures, units = {}, {}
    for adjust in ("none", "forward"):
        slot = adjust + ":000"
        request = batch_request(symbols=CODES[:2], since=millis(start.isoformat()),
            until=millis(end.isoformat(), True), adjust_kind=adjust)
        plan["requests"][slot] = request
        data = []
        for code in CODES[:2]:
            rows = []
            for index, day in enumerate(days):
                price = "7" if adjust == "forward" else "20" if index < 30 else "10"
                row = {"open": price, "high": price, "low": price, "close": price,
                    "volume": 100, "turnover": "1000", "ts_millis_open": millis(day),
                    "ts_millis": millis(day, True)}
                if edit is not None:
                    row = edit(adjust, code, day, row, days)
                if row is not None:
                    rows.append(row)
            data.append([code, rows])
        captures[slot] = capture_batch(json.dumps({"code": 200, "message": "success", "data": data}).encode(),
            symbols=CODES[:2], since=request["parameters"]["since"], until=request["parameters"]["until"],
            adjust_kind=adjust, acquired_at="2026-10-18T01:00:00+00:00")
        units[slot] = {"status": "complete", "request_count": 1,
            "reference": reference("ftshare-daily-" + adjust), "reason_code": None}
    return {"plan": plan, "units": units}, context, captures


def read(bundle, **kwargs):
    collection, context, captures = bundle
    calls = []
    def load(ref, *, request):
        slot = request["parameters"]["adjust_kind"] + ":000"
        calls.append(slot)
        return captures[slot]
    view, sources = _read_ftshare_inputs(collection, context, load, read_as_of=CLOCK, **kwargs)
    return view, sources, calls


def entry(view, *, kind="qfq", code="000001.SZ", endpoint=False):
    source = view["endpoint_comparisons"] if endpoint else view
    return next(row for row in source[kind]["entries"] if row["entity_id"] == code)


def test_direct_forward_prices_need_no_factors_and_keep_all_scope_in_denominator():
    view, sources, calls = read(inputs())
    assert view["schema"] == VIEW_SCHEMA and view["normalization"] == NORMALIZATION
    assert view["scope_count"] == 3 and view["qfq"]["observed_count"] == 2
    assert view["qfq"]["missing_count"] == 1 and view["qfq"]["incomplete_count"] == 1
    assert entry(view, kind="raw")["horizons"][-1]["change_pct"] == "-50.0"
    assert [window["change_pct"] for window in entry(view)["horizons"]] == ["0", "0", "0"]
    assert entry(view)["qualification"]["factor"] == {"status": "not-provided", "reconstructed": False}
    assert entry(view)["qualification"]["adjustment"]["anchor_status"] == "not-established-by-provider-response"
    assert entry(view, endpoint=True)["horizons"][-1]["baseline_value"] == "7"
    assert calls == ["forward:000", "none:000"] and len(sources) == 6
    assert view["source_requests"] == 0
    assert view["freshness"]["status"] == "current-through-completed-day"


def test_intermediate_raw_gap_keeps_endpoints_and_forward_window_independent():
    bundle = inputs(edit=lambda adjust, code, day, row, days:
        None if adjust == "none" and code == CODES[0] and day == days[-3] else row)
    view, _, _ = read(bundle)
    assert entry(view, kind="raw")["horizons"][0]["status"] == "missing-market-sessions"
    assert entry(view, kind="raw", endpoint=True)["horizons"][0]["status"] == "observed"
    assert entry(view)["horizons"][0]["status"] == "observed"


def test_source_hold_remains_visible_without_discarding_retained_daily_observations():
    bundle = inputs()
    hold = {"reason_code": "FTSHARE_OBSERVED_QUOTA_EXHAUSTED", "set_at": "2026-10-18T01:00:00+00:00"}
    bundle[0]["source_hold"] = hold
    view, _, _ = read(bundle)
    assert view["source_hold"] == hold
    assert view["source_hold"] is not hold
    assert view["raw"]["observed_count"] == view["qfq"]["observed_count"] == 2
    assert view["source_requests"] == 0


def test_quarantined_forward_end_never_falls_back_to_raw_or_rebuilds_factor():
    bundle = inputs(edit=lambda adjust, code, day, row, days:
        {**row, "close": "0"} if adjust == "forward" and code == CODES[0] and day == days[-1] else row)
    view, _, _ = read(bundle)
    assert entry(view, kind="raw")["status"] == "observed"
    assert entry(view)["status"] == "no-observation-at-date"
    assert entry(view, endpoint=True)["horizons"][0]["status"] == "missing-endpoint-observations"
    assert entry(view)["qualification"]["input"]["quarantined_dates"]
    assert view["qfq"]["scope_count"] == 3


def test_unretained_native_query_stays_missing_without_hiding_the_counterpart():
    bundle = inputs()
    bundle[0]["units"]["none:000"].update(status="failed", reference=None, reason_code="FTSHARE_HTTP_503")
    view, sources, calls = read(bundle)
    assert calls == ["forward:000"]
    assert sources["none:000"]["status"] == "not-retained"
    assert view["raw"]["observed_count"] == 0 and view["qfq"]["observed_count"] == 2
    assert view["freshness"]["status"] == "unknown"


def test_page_and_unknown_security_do_not_change_versions_or_full_counts():
    bundle = inputs()
    full, _, _ = read(bundle)
    page, _, _ = read(bundle, limit=1, offset=1)
    empty, _, _ = read(bundle, entity_id="600999.SH")
    assert full["view_version"] == page["view_version"] == empty["view_version"]
    assert page["raw"]["page"] == page["qfq"]["page"] == page["endpoint_comparisons"]["qfq"]["page"]
    assert page["raw"]["entries"][0]["entity_id"] == CODES[1]
    assert not empty["raw"]["entries"] and empty["raw"]["scope_count"] == 3
    assert empty["raw"]["observed_count"] == full["raw"]["observed_count"]


def test_few_received_rows_cannot_manufacture_a_trading_calendar_window():
    view, _, _ = read(inputs(count_days=3))
    assert all(window["status"] == "insufficient-calendar-history" for window in entry(view)["horizons"])
    assert view["qfq"]["observed_count"] == 0


def test_two_forward_requests_for_one_security_are_rejected_not_stitched():
    collection, context, captures = inputs()
    collection["plan"]["requests"]["forward:001"] = deepcopy(collection["plan"]["requests"]["forward:000"])
    collection["units"]["forward:001"] = deepcopy(collection["units"]["forward:000"])
    with pytest.raises(RecordError, match="FTSHARE_READING_DUPLICATE_SERIES"):
        read((collection, context, captures))


def test_old_context_calendar_does_not_certify_current_freshness():
    bundle = inputs()
    bundle[0]["plan"]["calendar_end"] = "20261016"
    view, _, _ = read(bundle)
    assert view["freshness"]["status"] == "unknown"
    assert view["qfq"]["observed_count"] == 2


def test_calendar_covering_future_days_still_certifies_the_completed_day():
    bundle = inputs()
    bundle[0]["plan"]["calendar_end"] = "20261020"
    for venue in ("SSE", "SZSE"):
        # Explicit synthetic open-day evidence, including days after this read's
        # completed-day cutoff. Those future sessions cannot create lag now.
        bundle[1]["calendars"][venue] += ["2026-10-18", "2026-10-19", "2026-10-20"]
        bundle[1]["calendar_references"][venue] = reference("future-calendar-" + venue.lower())
    view, _, _ = read(bundle)
    assert view["freshness"]["calendar_through"] == "2026-10-20"
    assert view["freshness"]["completed_day_through"] == "2026-10-17"
    assert view["freshness"]["status"] == "current-through-completed-day"
    assert {v["current_session_lag"] for v in view["by_exchange"].values()} == {0}


def test_calendar_lag_counts_only_completed_real_sessions_for_each_exchange():
    bundle = inputs()
    bundle[0]["plan"]["calendar_end"] = "20261020"
    bundle[1]["calendars"]["SSE"] += ["2026-10-17", "2026-10-18", "2026-10-19"]
    bundle[1]["calendars"]["SZSE"] += ["2026-10-18", "2026-10-19"]
    view, _, _ = read(bundle)
    assert view["freshness"]["status"] == "stale"
    assert view["by_exchange"]["SSE"]["current_session_lag"] == 1
    assert view["by_exchange"]["SZSE"]["current_session_lag"] == 0


def test_source_failure_is_isolated_but_global_store_failure_propagates():
    collection, context, captures = inputs()
    def load(_ref, *, request):
        if request["parameters"]["adjust_kind"] == "forward":
            raise RecordError("FTSHARE_CAPTURE_INTEGRITY")
        return captures["none:000"]
    view, sources = _read_ftshare_inputs(collection, context, load, read_as_of=CLOCK)
    assert sources["forward:000"]["status"] == "unavailable"
    assert view["raw"]["observed_count"] == 2 and view["qfq"]["observed_count"] == 0
    def failed(_ref, **_kwargs):
        raise StoreUnavailable("REMOTE_BUDGET")
    with pytest.raises(StoreUnavailable, match="REMOTE_BUDGET"):
        _read_ftshare_inputs(collection, context, failed, read_as_of=CLOCK)


def test_source_changes_affect_only_the_dependent_series_version():
    original, _, _ = read(inputs())
    changed, _, _ = read(inputs(edit=lambda adjust, code, day, row, days:
        {**row, "turnover": "1001"} if adjust == "none" else row))
    assert original["qfq"]["source_version"] == changed["qfq"]["source_version"]
    assert original["raw"]["source_version"] != changed["raw"]["source_version"]
    assert original["view_version"] != changed["view_version"]


def test_normal_room_dispatch_uses_ftshare_verifier_and_preserves_partial_and_reference(monkeypatch):
    import decision_kernel.application.ftshare_collection_inputs as consumer
    from decision_kernel.application.reading_market import open_market
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    from tempfile import TemporaryDirectory
    from pathlib import Path
    view, sources, _ = read(inputs())
    ref = reference("ftshare-collection-test")
    observed = []
    def restore(reference, registry, store, **kwargs):
        observed.append((reference, registry, kwargs))
        return {"status": "restored", "reference": ref, "selected_reference": ref,
            "serving": "current-complete", "latest_attempt": {"status": "complete"},
            "latest_requested_end": "20261016", "selected_requested_end": "20261016",
            "query_count": 6, "unavailable_queries": 0, "sources": sources, "view": view,
            "source_requests": 0, "reading_error": None}
    monkeypatch.setattr(consumer, "read_ftshare_collection_version", restore)
    with TemporaryDirectory() as folder:
        store = VersionedStore(GitStore.init_synthetic(Path(folder) / "store.git"))
        result = open_market(store, {"reference": ref, "limit": 1}, registry=object(), read_as_of=CLOCK)
    assert observed and result["view"]["view"]["schema"] == VIEW_SCHEMA
    assert result["partial"] is True and result["reference"] == ref


def single_inputs(*, full_scope=False):
    from decision_kernel.application.ftshare_adoption import ADOPTION_PLAN_SCHEMA, kernel_price_requests
    from decision_kernel.application.ftshare_single import capture_single
    collection, context, batches = inputs()
    plan = collection["plan"]
    plan.update(schema=ADOPTION_PLAN_SCHEMA, requests=kernel_price_requests(),
                adoption={"evidence_reference": reference("ftshare-kernel-evidence")})
    captures, collection["units"] = {}, {}
    for role, request in plan["requests"].items():
        parameters = request["parameters"]
        original = json.loads(batches[parameters["adjust_kind"] + ":000"]["body_utf8"])
        rows = next(rows for code, rows in original["data"] if code == parameters["symbol"])
        captures[role] = capture_single(encoded({"code": 0, "data": rows}),
            **{key: parameters[key] for key in ("symbol", "since", "until", "adjust_kind")},
            acquired_at="2026-10-19T12:00:00+00:00")
        collection["units"][role] = {"status": "complete", "request_count": 0,
            "reference": reference("ftshare-single-" + role), "reason_code": None}
    if full_scope:
        master = context["masters"]["SSE"]
        for number in range(600002, 605225):
            code = str(number) + ".SH"
            master["securities"][code] = {**master["securities"]["600000.SH"],
                "ts_code": code, "symbol": str(number)}
        master["securities"]["605224.SH"] = None
        master["scope_count"] = len(master["securities"])
        master["version"] = digest(encoded(master["securities"]))
        securities = {code: value for master in context["masters"].values()
                      for code, value in master["securities"].items()}
        context.update(scope_symbols=sorted(securities),
            eligible_symbols=sorted(code for code, value in securities.items() if value is not None),
            quarantined_symbols=sorted(code for code, value in securities.items() if value is None))
    return collection, context, captures


def read_single(bundle, **kwargs):
    collection, context, captures = bundle
    def load(_ref, *, request):
        return next(capture for capture in captures.values() if capture["request"] == request)
    return _read_ftshare_inputs(collection, context, load, read_as_of=CLOCK, **kwargs)[0]


def test_kernel_single_routes_keep_independent_counterpart_gap_and_native_lineage():
    view = read_single(single_inputs())
    assert view["raw"]["observed_count"] == 2 and view["qfq"]["observed_count"] == 1
    assert entry(view, kind="raw", code="600000.SH")["status"] == "observed"
    sh_forward = entry(view, code="600000.SH")
    assert sh_forward["input_status"] == "not-supplied"
    assert all(window["status"] != "observed" for window in sh_forward["horizons"])
    assert view["freshness"]["status"] == "unknown"
    assert len(view["source_lineage"]["single_queries"]) == 3 and "batches" not in view["source_lineage"]
    assert all(row["request"]["url"].endswith("/api/v1/market/data/stock-candlesticks")
        and "symbol" in row["request"]["parameters"] and "symbols" not in row["request"]["parameters"]
        for row in view["source_lineage"]["single_queries"].values())
    assert entry(view)["lineage_representation"] == "original-ftshare-whole-range-single-query"


def test_kernel_full_5226_denominator_and_missing_windows_survive_page_and_entity_selection():
    bundle = single_inputs(full_scope=True)
    view = read_single(bundle, limit=1)
    selected = read_single(bundle, entity_id="600000.SH")
    assert len(bundle[1]["quarantined_symbols"]) == 2 and len(bundle[1]["eligible_symbols"]) == 5224
    assert view["scope_count"] == selected["scope_count"] == 5226
    for kind, observed, missing in (("raw", 2, 5224), ("qfq", 1, 5225)):
        assert [row["observed_count"] for row in view[kind]["horizon_counts"]] == [observed] * 3
        assert [row["missing_count"] for row in view[kind]["horizon_counts"]] == [missing] * 3
        assert view[kind]["horizon_counts"] == selected[kind]["horizon_counts"]
        assert len(view[kind]["entries"]) == len(selected[kind]["entries"]) == 1
    assert view["view_version"] == selected["view_version"]
    assert view["freshness"]["status"] == "unknown" and view["source_requests"] == 0
    assert {venue["current_session_lag"] for venue in view["by_exchange"].values()} == {None}
    assert len(encoded(view)) < 2 * 1024 * 1024
