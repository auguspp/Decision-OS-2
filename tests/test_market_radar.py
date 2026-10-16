"""Market surfaces: no Relay requests or custody writes in this engineering slice."""
import unittest

from decision_kernel.application.market_radar import membership_overlap, observe_market_surface
from decision_kernel.observations.market_daily import MarketInputError, observe_daily


def rows(*prices):
    return [{"trade_date": f"2026-10-{10+i:02}", "close": str(price)}
            for i, price in enumerate(prices)]


class DailyObservationTests(unittest.TestCase):
    def test_precise_baseline_and_relative(self):
        output = observe_daily(entity_id="000001.SZ", series=rows(100, 110, 121),
                               as_of="2026-10-12", horizons=(1, 2), adjustment="raw",
                               source_version="fixture-v1", benchmark=rows(100, 105, 110))
        one, two = output["horizons"]
        self.assertEqual(one["baseline_date"], "2026-10-11")
        self.assertEqual(one["change_pct"], "10.0")
        self.assertEqual(one["relative_status"], "observed")
        self.assertEqual(two["change_pct"], "21.00")
        self.assertEqual(two["excess_change_pp"], "11.00")

    def test_future_rows_ignored_and_insufficient_distinct(self):
        output = observe_daily(entity_id="abc", series=rows(100, 110, 121),
                               as_of="2026-10-11", horizons=(1, 5), adjustment="qfq",
                               source_version="fixture-v1")
        self.assertEqual(output["horizons"][0]["change_pct"], "10.0")
        self.assertEqual(output["horizons"][1]["status"], "insufficient-history")

    def test_missing_date_cannot_use_stale_end(self):
        output = observe_daily(entity_id="abc", series=rows(100, 110),
                               as_of="2026-10-12", adjustment="raw", source_version="v1")
        self.assertEqual(output["status"], "no-observation-at-date")

    def test_mismatched_benchmark_cannot_claim_excess(self):
        output = observe_daily(entity_id="abc", series=rows(100, 105, 110),
                               as_of="2026-10-12", horizons=(2,), adjustment="raw",
                               source_version="v1", benchmark=rows(100, 102))
        self.assertEqual(output["horizons"][0]["relative_status"], "benchmark-session-mismatch")
        self.assertNotIn("excess_change_pp", output["horizons"][0])

    def test_refuse_duplicate_and_bad_values(self):
        bad = rows(100, 101) + [rows(102)[0]]
        for series in (bad, [{"trade_date": "2026-10-10", "close": "NaN"}],
                       [{"trade_date": "2026-10-10", "close": "-1"}]):
            with self.assertRaises(MarketInputError):
                observe_daily(entity_id="abc", series=series, as_of="2026-10-10",
                              adjustment="raw", source_version="v1")

    def test_adjustment_and_version_mandatory(self):
        with self.assertRaises(MarketInputError):
            observe_daily(entity_id="abc", series=rows(100), as_of="2026-10-10",
                          adjustment="unknown", source_version="v1")


class MarketSurfaceTests(unittest.TestCase):
    def test_three_independent_paths_and_coverage(self):
        for kind, coverage in (("sector", "partial-universe"), ("concept", "partial-universe"),
                               ("stock", "watchlist")):
            result = observe_market_surface(kind=kind, as_of="2026-10-11",
                                           entities={"Z": rows(100, 110)}, adjustment="raw",
                                           source_version="fixture-v1", universe_version="fixture-2026",
                                           coverage=coverage)
            self.assertEqual(result["kind"], kind)
            self.assertEqual(result["observed_count"], 1)
            self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_missing_scope_not_zero_opportunity(self):
        result = observe_market_surface(kind="stock", as_of="2026-10-11",
                                       entities={}, adjustment="raw", source_version="v1",
                                       universe_version="u1", coverage="partial-universe")
        self.assertEqual(result["scope_count"], 0)
        self.assertEqual(result["observed_count"], 0)
        self.assertEqual(result["discovery_claim"], "not-established-by-supplied-universe")

    def test_membership_overlap_does_not_infer_identity(self):
        self.assertEqual(membership_overlap({"s": ["A", "B"]}, {"c": ["B", "C"]}),
                         [{"left": "s", "right": "c", "members": ["B"], "overlap_count": 1}])

    def test_watchlist_not_sector_universe(self):
        with self.assertRaises(MarketInputError):
            observe_market_surface(kind="sector", as_of="2026-10-11", entities={},
                                   adjustment="raw", source_version="v1",
                                   universe_version="v1", coverage="watchlist")


if __name__ == "__main__":
    unittest.main()
