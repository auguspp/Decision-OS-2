"""Synthetic calendars and prices only; not an exchange or Relay qualification."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from decision_kernel.application.market_radar import (
    membership_overlap, observe_market_surface, read_market_surface,
)
from decision_kernel.observations.market_daily import MarketInputError, observe_daily
from decision_kernel.interfaces import market_cli


def sessions(n=61):
    # Deliberately synthetic, not a public holiday calendar or weekday fallback.
    start = date(2026, 1, 5)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def rows(prices, days=None):
    days = sessions(len(prices)) if days is None else days
    return [{"trade_date": day, "close": str(price)} for day, price in zip(days, prices)]


def daily(**changes):
    params = {"entity_id": "A.SIM", "series": rows(range(100, 161)), "as_of": sessions()[-1],
              "market_sessions": sessions(), "calendar_version": "synthetic-calendar-v1",
              "adjustment": "raw", "source_version": "synthetic-prices-v1",
              "market": "synthetic-market", "currency": "CNY"}
    params.update(changes)
    return params


def benchmark(prices=None, **changes):
    result = {"entity_id": "INDEX.SIM", "series": rows(range(100, 161) if prices is None else prices),
              "source_version": "synthetic-index-v1", "adjustment": "raw",
              "market": "synthetic-market", "currency": "CNY"}
    result.update(changes)
    return result


def surface(**changes):
    params = daily()
    params.pop("entity_id")
    series = params.pop("series")
    params.update(kind="stock", entities={"A.SIM": series}, declared_entities=["A.SIM"],
                  universe_version="synthetic-universe-v1", coverage="partial-universe")
    params.update(changes)
    return params


class DailyObservationTests(unittest.TestCase):
    def test_precise_baseline_and_relative(self):
        result = observe_daily(**daily(series=rows([100, 110, 121]), market_sessions=sessions(3),
                                       as_of=sessions(3)[-1], horizons=(1, 2),
                                       benchmark=benchmark([100, 105, 110])))
        one, two = result["horizons"]
        self.assertEqual(one["baseline_date"], sessions(3)[1])
        self.assertEqual(Decimal(one["change_pct"]), Decimal("10"))
        self.assertEqual(one["relative_status"], "observed")
        self.assertEqual(Decimal(two["change_pct"]), Decimal("21"))
        self.assertEqual(Decimal(two["excess_change_pp"]), Decimal("11"))
        self.assertEqual(result["benchmark"]["source_version"], "synthetic-index-v1")
        self.assertNotIn("series", result["benchmark"])

    def test_defaults_are_calendar_bound_5_20_60(self):
        result = observe_daily(**daily())
        self.assertEqual(result["status"], "observed")
        self.assertEqual([e["sessions"] for e in result["horizons"]], [5, 20, 60])
        for entry in result["horizons"]:
            self.assertEqual(entry["baseline_date"], sessions()[-entry["sessions"] - 1])
        self.assertEqual(result["return_semantics"], "price-ratio-not-total-return")

    def test_missing_session_does_not_stretch_window(self):
        series = daily()["series"]
        del series[-3]
        result = observe_daily(**daily(series=series, horizons=(1, 5)))
        self.assertEqual(result["status"], "partial-history")
        self.assertEqual(result["horizons"][0]["status"], "observed")
        window = result["horizons"][1]
        self.assertEqual(window["baseline_date"], sessions()[-6])
        self.assertEqual(window["missing_dates"], [sessions()[-3]])
        self.assertNotIn("change_pct", window)

    def test_older_gap_only_restricts_dependent_windows(self):
        series = daily()["series"]
        del series[-11]
        result = observe_daily(**daily(series=series))
        self.assertEqual([h["status"] for h in result["horizons"]],
                         ["observed", "missing-market-sessions", "missing-market-sessions"])

    def test_future_rows_ignored_and_insufficient_distinct(self):
        result = observe_daily(**daily(series=rows([100, 110, 121]), market_sessions=sessions(3),
                                       as_of=sessions(2)[-1], horizons=(1, 5)))
        self.assertEqual(Decimal(result["horizons"][0]["change_pct"]), Decimal("10"))
        self.assertEqual(result["horizons"][1]["status"], "insufficient-calendar-history")

    def test_missing_date_cannot_use_stale_end(self):
        result = observe_daily(**daily(series=rows(range(100, 160))))
        self.assertEqual(result["status"], "no-observation-at-date")
        for window in result["horizons"]:
            self.assertIn(sessions()[-1], window["missing_dates"])
            self.assertNotIn("change_pct", window)

    def test_mismatched_benchmark_cannot_claim_excess(self):
        result = observe_daily(**daily(benchmark=benchmark(range(100, 160))))
        self.assertEqual(result["status"], "observed")
        for entry in result["horizons"]:
            self.assertEqual(entry["relative_status"], "benchmark-session-mismatch")
            self.assertNotIn("excess_change_pp", entry)

    def test_benchmark_interior_gap_is_not_endpoint_only_qualification(self):
        base = benchmark()
        del base["series"][-10]
        result = observe_daily(**daily(benchmark=base))
        self.assertEqual([e["relative_status"] for e in result["horizons"]],
                         ["observed", "benchmark-session-mismatch", "benchmark-session-mismatch"])

    def test_benchmark_conventions_are_not_silently_mixed(self):
        for key, value in (("adjustment", "hfq"), ("market", "other-market"), ("currency", "USD")):
            with self.subTest(key=key):
                result = observe_daily(**daily(benchmark=benchmark(**{key: value})))
                self.assertEqual(result["status"], "observed")
                self.assertTrue(all(h["relative_status"] == "benchmark-convention-mismatch" for h in result["horizons"]))
                self.assertNotIn("excess_change_pp", result["horizons"][0])

    def test_invalid_benchmark_rows_do_not_destroy_absolute_observation(self):
        result = observe_daily(**daily(benchmark=benchmark(series=[{"trade_date": sessions()[-1], "close": "NaN"}])))
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["horizons"][0]["relative_status"], "benchmark-invalid-data")

    def test_unversioned_benchmark_is_rejected(self):
        base = benchmark()
        del base["source_version"]
        with self.assertRaisesRegex(MarketInputError, "BENCHMARK_CONTEXT_INVALID"):
            observe_daily(**daily(benchmark=base))

    def test_refuse_duplicate_and_bad_values(self):
        for value in ("NaN", "Infinity", "-1", "0", True, None, {}, "1e1000000", "x"):
            with self.subTest(value=value), self.assertRaisesRegex(MarketInputError, "INVALID_CLOSE"):
                observe_daily(**daily(series=[{"trade_date": sessions()[-1], "close": value}]))
        with self.assertRaisesRegex(MarketInputError, "DUPLICATE_SESSION"):
            observe_daily(**daily(series=daily()["series"] + daily()["series"][:1]))

    def test_duplicate_future_rows_are_rejected(self):
        future = {"trade_date": sessions(62)[-1], "close": "200"}
        with self.assertRaisesRegex(MarketInputError, "DUPLICATE_SESSION"):
            observe_daily(**daily(series=daily()["series"] + [future, future]))

    def test_adjustment_and_version_mandatory(self):
        for key, value in (("adjustment", "unknown"), ("source_version", ""),
                           ("calendar_version", ""), ("market", ""), ("currency", "cny")):
            with self.subTest(key=key), self.assertRaises(MarketInputError):
                observe_daily(**daily(**{key: value}))

    def test_calendar_validates_order_uniqueness_cutoff_and_iso_dates(self):
        for calendar in ([], "2026-01-01", sessions() + sessions()[:1], list(reversed(sessions())),
                         ["20260105"], ["not-a-date"]):
            with self.subTest(calendar=calendar), self.assertRaises(MarketInputError):
                observe_daily(**daily(market_sessions=calendar))
        with self.assertRaisesRegex(MarketInputError, "AS_OF_NOT_MARKET_SESSION"):
            observe_daily(**daily(as_of="2026-12-31"))

    def test_price_on_undeclared_market_day_is_rejected(self):
        calendar = sessions()
        del calendar[10]
        with self.assertRaisesRegex(MarketInputError, "NON_MARKET_SESSION"):
            observe_daily(**daily(market_sessions=calendar))

    def test_invalid_horizons_are_rejected_without_type_errors(self):
        for horizons in ((), [5], (0,), (True,), (5, 5), ("5",), ({},)):
            with self.subTest(horizons=horizons), self.assertRaisesRegex(MarketInputError, "HORIZON_INVALID"):
                observe_daily(**daily(horizons=horizons))

    def test_precision_is_independent_of_ambient_decimal_context(self):
        expected = observe_daily(**daily(benchmark=benchmark()))
        with localcontext() as context:
            context.prec = 2
            actual = observe_daily(**daily(benchmark=benchmark()))
        self.assertEqual(actual, expected)

    def test_unsorted_prices_are_deterministic_without_input_mutation(self):
        params = daily()
        before = deepcopy(params)
        expected = observe_daily(**params)
        self.assertEqual(params, before)
        params["series"].reverse()
        self.assertEqual(observe_daily(**params), expected)

    def test_empty_prices_still_report_all_requested_windows(self):
        result = observe_daily(**daily(series=[]))
        self.assertEqual(result["status"], "no-observation-at-date")
        self.assertEqual(len(result["horizons"]), 3)
        self.assertEqual(len(result["horizons"][-1]["missing_dates"]), 61)


class MarketSurfaceTests(unittest.TestCase):
    def test_three_independent_paths_and_coverage(self):
        for kind, coverage in (("sector", "partial-universe"), ("concept", "partial-universe"), ("stock", "watchlist")):
            result = observe_market_surface(**surface(kind=kind, coverage=coverage))
            self.assertEqual(result["kind"], kind)
            self.assertEqual(result["observed_count"], 1)
            self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_empty_classifications_do_not_gate_stock(self):
        for kind in ("sector", "concept"):
            result = observe_market_surface(**surface(kind=kind, entities={}, declared_entities=[]))
            self.assertEqual(result["status"], "empty-declared-universe")
        self.assertEqual(observe_market_surface(**surface())["observed_count"], 1)

    def test_missing_scope_not_zero_opportunity(self):
        result = observe_market_surface(**surface(entities={}, declared_entities=[]))
        self.assertEqual(result["scope_count"], 0)
        self.assertEqual(result["observed_count"], 0)
        self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_declared_denominator_includes_not_supplied_and_invalid(self):
        payload = surface(declared_entities=["C.SIM", "B.SIM", "A.SIM"],
                          entities={"B.SIM": "invalid", "A.SIM": daily()["series"]})
        result = observe_market_surface(**payload)
        self.assertEqual(result["scope_count"], 3)
        self.assertEqual(result["received_count"], 2)
        self.assertEqual(result["observed_count"], 1)
        self.assertEqual(result["missing_count"], 2)
        a, b, c = result["entries"]
        self.assertEqual(a["entity_id"], "A.SIM")
        self.assertEqual(b["status"], "invalid-data")
        self.assertEqual(b["reason_code"], "SERIES_INVALID")
        self.assertEqual(c["input_status"], "not-supplied")
        self.assertEqual(result["horizon_counts"][0]["missing_count"], 2)

    def test_partial_history_counts_are_per_horizon(self):
        series = daily()["series"]
        del series[-10]
        result = observe_market_surface(**surface(entities={"A.SIM": series}))
        self.assertEqual(result["observed_count"], 1)
        self.assertEqual(result["incomplete_count"], 1)
        self.assertEqual(result["status"], "data-gaps")
        self.assertEqual([h["observed_count"] for h in result["horizon_counts"]], [1, 0, 0])

    def test_empty_input_does_not_bypass_context_validation(self):
        for key, value in (("as_of", "bad"), ("adjustment", "unknown"), ("source_version", ""),
                           ("calendar_version", ""), ("horizons", (True,)), ("benchmark", {})):
            with self.subTest(key=key), self.assertRaises(MarketInputError):
                observe_market_surface(**surface(entities={}, declared_entities=[], **{key: value}))

    def test_universe_validates_duplicates_and_unknown_received_entities(self):
        for declared in (["A.SIM", "A.SIM"], "A.SIM", [None], [], ["B.SIM"]):
            with self.subTest(declared=declared), self.assertRaises(MarketInputError):
                observe_market_surface(**surface(declared_entities=declared))

    def test_membership_overlap_does_not_infer_identity(self):
        self.assertEqual(membership_overlap({"s": ["A", "B"]}, {"c": ["B", "C"]}),
                         [{"left": "s", "right": "c", "members": ["B"], "overlap_count": 1}])
        self.assertEqual(membership_overlap({"s": ["A.SIM"]}, {"c": ["A.OTHER"]}), [])

    def test_membership_validates_both_sides_even_against_empty(self):
        for invalid in ({1: ["A"]}, {"s": "A"}, {"s": ["A", "A"]}, {"s": [{}]}, {"s": [None]}):
            for left, right in ((invalid, {}), ({}, invalid)):
                with self.subTest(left=left, right=right), self.assertRaises(MarketInputError):
                    membership_overlap(left, right)

    def test_membership_snapshot_is_required_and_not_future(self):
        valid = surface(kind="concept", membership={"A.SIM": ["SEC.SIM"]},
                        membership_version="member-snapshot-v1", membership_as_of=sessions()[-1])
        result = observe_market_surface(**valid)
        self.assertEqual(result["membership"]["A.SIM"], ["SEC.SIM"])
        self.assertEqual(result["membership_version"], "member-snapshot-v1")
        for key, value in (("membership_version", None), ("membership_as_of", "2027-01-01"),
                           ("membership_as_of", None), ("membership", {"B.SIM": []})):
            params = {**valid, key: value}
            with self.subTest(key=key), self.assertRaises(MarketInputError):
                observe_market_surface(**params)

    def test_missing_membership_differs_from_known_empty_group(self):
        missing = observe_market_surface(**surface(kind="sector"))
        known = observe_market_surface(**surface(kind="sector", membership={"A.SIM": []},
                                                membership_version="members-v1", membership_as_of=sessions()[-1]))
        self.assertIsNone(missing["membership"])
        self.assertEqual(known["membership"], {"A.SIM": []})

    def test_orphan_membership_context_and_stock_group_members_rejected(self):
        with self.assertRaisesRegex(MarketInputError, "MEMBERSHIP_CONTEXT_WITHOUT_MEMBERS"):
            observe_market_surface(**surface(membership_version="v1"))
        with self.assertRaisesRegex(MarketInputError, "STOCK_MEMBERSHIP_NOT_APPLICABLE"):
            observe_market_surface(**surface(membership={}))

    def test_watchlist_not_sector_universe(self):
        with self.assertRaisesRegex(MarketInputError, "CLASSIFICATION_COVERAGE_INVALID"):
            observe_market_surface(**surface(kind="sector", coverage="watchlist"))

    def test_full_declared_scope_is_not_full_market_discovery(self):
        result = observe_market_surface(**surface(coverage="full-declared-universe", declared_entities=["A.SIM", "B.SIM"]))
        self.assertEqual(result["scope_count"], 2)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_paging_reaches_entire_scope_and_preserves_denominators(self):
        payload = surface(declared_entities=["C.SIM", "A.SIM", "B.SIM"], horizons=[5, 20, 60])
        before = deepcopy(payload)
        pages = [read_market_surface(payload, offset=i, limit=1) for i in range(3)]
        self.assertEqual([p["entries"][0]["entity_id"] for p in pages], ["A.SIM", "B.SIM", "C.SIM"])
        self.assertEqual([p["page"]["next_offset"] for p in pages], [1, 2, None])
        self.assertTrue(all(p["scope_count"] == 3 for p in pages))
        self.assertEqual(payload, before)
        selected = read_market_surface(payload, entity_id="B.SIM")
        self.assertEqual(selected["scope_count"], 3)
        self.assertEqual(selected["entries"][0]["input_status"], "not-supplied")

    def test_missing_filter_result_retains_scope_and_no_discovery_claim(self):
        result = read_market_surface(surface(), entity_id="NONE.SIM")
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["scope_count"], 1)
        self.assertEqual(result["page"]["selected_count"], 0)
        self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_reader_refuses_unknown_fields_invalid_pages_and_scalar_horizons(self):
        for payload in ([], surface(extra=True), surface(horizons="5"), surface(horizons=5)):
            with self.subTest(payload=payload), self.assertRaises(MarketInputError):
                read_market_surface(payload)
        for page in ({"offset": -1}, {"offset": True}, {"limit": 0}, {"limit": 101}, {"limit": True}):
            with self.subTest(page=page), self.assertRaisesRegex(MarketInputError, "PAGE_INVALID"):
                read_market_surface(surface(), **page)


class MarketConsumerTests(unittest.TestCase):
    def run_reader(self, text, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text(text, encoding="utf-8")
            before = path.read_bytes()
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(market_cli.__file__).resolve().parents[2])
            completed = subprocess.run([sys.executable, "-m", "decision_kernel.interfaces.market_cli",
                                        "--input", str(path), *args], cwd=directory, env=env,
                                       capture_output=True, text=True, timeout=20)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["snapshot.json"])
            return completed

    def test_independent_process_readback_three_surfaces(self):
        for kind in ("sector", "concept", "stock"):
            with self.subTest(kind=kind):
                text = json.dumps(surface(kind=kind, declared_entities=["A.SIM", "B.SIM"]))
                first = self.run_reader(text, "--limit", "1")
                second = self.run_reader(text, "--offset", "1", "--limit", "1")
                self.assertEqual(first.returncode, 0, first.stderr)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(json.loads(first.stdout)["page"]["next_offset"], 1)
                result = json.loads(second.stdout)
                self.assertEqual(result["kind"], kind)
                self.assertEqual(result["scope_count"], 2)
                self.assertEqual(result["entries"][0]["input_status"], "not-supplied")

    def test_duplicate_json_nonfinite_and_invalid_json_refused(self):
        for raw, code in (("{\"kind\":\"stock\",\"kind\":\"sector\"}", "DUPLICATE_JSON_KEY"),
                          ("{\"data\":NaN}", "NONFINITE_JSON_VALUE"), ("broken", "INPUT_JSON_INVALID"),
                          ("{\"nested\":{\"a\":1,\"a\":2}}", "DUPLICATE_JSON_KEY")):
            with self.subTest(code=code):
                completed = self.run_reader(raw)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(json.loads(completed.stderr)["error"], code)
                self.assertEqual(completed.stdout, "")

    def test_oversized_input_refused(self):
        completed = self.run_reader(" " * (market_cli.MAX_INPUT_BYTES + 1))
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stderr)["error"], "INPUT_TOO_LARGE")

    def test_overlarge_integer_json_is_safe_error(self):
        completed = self.run_reader('{"large":' + '1' * 5000 + '}')
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stderr)["error"], "INPUT_JSON_INVALID")


if __name__ == "__main__":
    unittest.main()
