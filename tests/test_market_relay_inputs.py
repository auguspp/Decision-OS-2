"""Synthetic registered-capture counterexamples; no provider access."""
import unittest
from decision_kernel.application.market_relay_inputs import (
    MarketRelayError, qualified_trade_calendar, qualified_daily)


def capture(api, params, fields, rows, complete=True):
    return {"api": api, "parameters": {**params, "fields": ",".join(fields)},
            "complete": complete, "error": None,
            "pages": [{"offset": 0, "table": {"fields": fields, "items": rows}}]}


class MarketRelayInputTests(unittest.TestCase):
    def setUp(self):
        self.calendar = capture("trade_cal",
            {"exchange": "SSE", "start_date": "20261012", "end_date": "20261016"},
            ["exchange", "cal_date", "is_open"],
            [["SSE", f"202610{n}", int(n != 14)] for n in (12, 13, 14, 15, 16)])
        self.daily = capture("daily",
            {"ts_code": "600519.SH", "start_date": "20261012", "end_date": "20261016"},
            ["ts_code", "trade_date", "close"],
            [["600519.SH", "20261012", "100"], ["600519.SH", "20261015", "105"]])

    def days(self):
        return qualified_trade_calendar(self.calendar, exchange="SSE",
                                        start="20261012", end="20261016")

    def quotes(self):
        return qualified_daily(self.daily, ts_code="600519.SH", start="20261012",
                               end="20261016", calendar=self.days())

    def test_market_calendar_and_price_gaps(self):
        self.assertEqual(self.days(), ["2026-10-12", "2026-10-13", "2026-10-15", "2026-10-16"])
        self.assertEqual([r["trade_date"] for r in self.quotes()],
                         ["2026-10-12", "2026-10-15"])

    def test_capture_incomplete_refused(self):
        self.daily["complete"] = False
        with self.assertRaisesRegex(MarketRelayError, "CAPTURE_INCOMPLETE"):
            self.quotes()

    def test_missing_calendar_day_refused(self):
        self.calendar["pages"][0]["table"]["items"].pop(1)
        with self.assertRaisesRegex(MarketRelayError, "CALENDAR_GAP"):
            self.days()

    def test_cross_security_and_closed_session_refused(self):
        for code in ("000001.SZ", "600519.SH"):
            self.daily["pages"][0]["table"]["items"] = [[code, "20261014", "100"]]
            with self.assertRaises(MarketRelayError):
                self.quotes()

    def test_invalid_prices_and_duplicate_refused(self):
        for value in ("NaN", "-1", "0", "1e9999"):
            self.daily["pages"][0]["table"]["items"] = [["600519.SH", "20261012", value]]
            with self.assertRaisesRegex(MarketRelayError, "DAILY_CLOSE"):
                self.quotes()
        self.daily["pages"][0]["table"]["items"] = [["600519.SH", "20261012", "1"]] * 2
        with self.assertRaisesRegex(MarketRelayError, "DAILY_ROW"):
            self.quotes()

    def test_duplicate_calendar_exchange_and_flag_refused(self):
        initial = self.calendar["pages"][0]["table"]["items"]
        for row in (["SSE", "20261012", 1], ["SZSE", "20261016", 1],
                    ["SSE", "20261016", "unknown"]):
            self.calendar["pages"][0]["table"]["items"] = initial + [row]
            with self.assertRaises(MarketRelayError):
                self.days()

    def test_invalid_fields_and_offset_refused(self):
        self.daily["pages"][0]["offset"] = 4
        with self.assertRaisesRegex(MarketRelayError, "TABLE_INVALID"):
            self.quotes()
        self.daily["pages"][0]["offset"] = 0
        self.daily["pages"][0]["table"]["fields"] = ["ts_code", "trade_date"]
        with self.assertRaisesRegex(MarketRelayError, "TABLE_INVALID"):
            self.quotes()
