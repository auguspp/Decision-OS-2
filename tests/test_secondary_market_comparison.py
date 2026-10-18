"""Finite secondary-source qualification tests; never make network/store calls."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO

from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.secondary_market_comparison import compare_retained_raw_market
from decision_kernel.interfaces.secondary_market_comparison_cli import main

DAY = '2026-09-30'


def capture(provider, *, rows=None):
    row = {'symbol': '600000.SH', 'trade_date': DAY, 'open': '9.1', 'high': '10.2',
           'low': '8.9', 'close': '10.1', 'volume': '123' if provider == 'TUSHARE_RELAY' else '12300',
           'volume_unit': 'LOTS_100_SHARES' if provider == 'TUSHARE_RELAY' else 'SHARES',
           'currency': 'CNY', 'adjustment': 'NONE', 'trade_status': 'TRADING'}
    return {'provider': provider, 'session': DAY, 'acquired_at': '2026-10-01T01:01:00+00:00',
            'source_ref': {'repository': 'auguspp/' + ('Decision-OS-2' if provider == 'TUSHARE_RELAY'
                             else 'decision-kernel'), 'commit': 'a' * 40 if provider == 'TUSHARE_RELAY' else 'b'*40,
                           'path': 'source-views/retained/manifest.json',
                           'sha256': 'c' * 64 if provider == 'TUSHARE_RELAY' else 'd' * 64},
            'rows': [row] if rows is None else rows}


def compare(relay=None, ftshare=None, **kw):
    return compare_retained_raw_market(relay=relay if relay is not None else capture('TUSHARE_RELAY'),
                                      ftshare=ftshare if ftshare is not None else capture('FTSHARE'),
                                      session=kw.get('session', DAY))


class SecondarySourceTests(unittest.TestCase):
    def test_synthetic_ohlcv_normalizes_explicit_lots_only(self):
        out = compare()
        self.assertEqual(out['paired_count'], 1)
        self.assertEqual(out['counts']['PRICE_EQUAL'], 1)
        self.assertEqual(out['details'][0]['volume_comparison'], 'EQUAL')
        self.assertEqual(out['ftshare_backup_admission'], 'NOT_ESTABLISHED')
        self.assertEqual(out['common_upstream_independence'], 'NOT_ESTABLISHED')
        self.assertFalse(out['provenance_verified_by_this_function'])
        self.assertEqual((out['source_requests'], out['writes']), (0, 0))

    def test_price_residual_not_overwritten_or_rounded(self):
        right = capture('FTSHARE')
        right['rows'][0]['close'] = '10.10000001'
        out = compare(ftshare=right)
        self.assertEqual(out['counts']['PRICE_DIFFERENCE'], 1)
        self.assertEqual(out['details'][0]['price_fields_differ'], ['close'])
        self.assertNotIn('10.10000001', json.dumps(out))

    def test_current_close_only_relay_and_full_ftshare_compare_close_without_invention(self):
        relay = capture('TUSHARE_RELAY')
        for field in ('open', 'high', 'low', 'volume', 'volume_unit'):
            relay['rows'][0].pop(field)
        out = compare(relay=relay)
        self.assertEqual(out['counts']['PRICE_EQUAL'], 1)
        self.assertEqual(out['details'][0]['fields_compared'], ['close'])
        self.assertEqual(out['details'][0]['volume_comparison'], 'NOT_ASSESSED')
        self.assertEqual(out['ftshare_backup_admission'], 'NOT_ESTABLISHED')

    def test_partial_ohlcv_cannot_silently_enter_close_only_mode(self):
        right = capture('FTSHARE')
        right['rows'][0].pop('high')
        with self.assertRaises(MarketInputError):
            compare(ftshare=right)

    def test_date_only_overlap_never_becomes_qualified_market(self):
        left, right = capture('TUSHARE_RELAY'), capture('FTSHARE')
        left['rows'] = []
        out = compare(relay=left, ftshare=right)
        self.assertEqual(out['comparison_status'], 'NO_COMPARABLE_PAIRS')
        self.assertEqual((out['primary_only'], out['secondary_only']), (0, 1))
        self.assertEqual(out['paired_count'], 0)

    def test_suspended_or_unknown_stays_in_denominator_without_price_pass(self):
        for status in ('SUSPENDED', 'UNKNOWN'):
            right = capture('FTSHARE')
            right['rows'][0]['trade_status'] = status
            out = compare(ftshare=right)
            self.assertEqual(out['paired_count'], 1)
            self.assertEqual(out['counts']['NON_TRADING_OR_UNKNOWN'], 1)
            self.assertEqual(out['comparison_status'], 'NO_COMPARABLE_PAIRS')

    def test_wrong_identity_or_day_refuses_instead_of_fallback(self):
        for change in (('trade_date', '2026-09-29'), ('symbol', '920202.BJ'),
                       ('currency', 'USD'), ('adjustment', 'QFQ'), ('volume_unit', 'LOTS'),
                       ('open', '10.3'), ('close', None)):
            with self.subTest(change=change):
                right = capture('FTSHARE')
                right['rows'][0][change[0]] = change[1]
                with self.assertRaises(MarketInputError):
                    compare(ftshare=right)

    def test_duplicate_rows_unknown_ref_bad_acquisition_and_float_refuse(self):
        for mutation in ('duplicate', 'ref', 'clock', 'float', 'same_ref'):
            l, r = capture('TUSHARE_RELAY'), capture('FTSHARE')
            if mutation == 'duplicate': r['rows'].append(deepcopy(r['rows'][0]))
            if mutation == 'ref': r['source_ref']['commit'] = 'not-a-sha'
            if mutation == 'clock': r['acquired_at'] = '2026-10-01T01:01:00'
            if mutation == 'float': r['rows'][0]['high'] = 10.2
            if mutation == 'same_ref': r['source_ref'] = deepcopy(l['source_ref'])
            with self.subTest(mutation=mutation), self.assertRaises(MarketInputError):
                compare(relay=l, ftshare=r)

    def test_no_secret_or_values_in_local_cli_stdout(self):
        with tempfile.TemporaryDirectory() as folder:
            l, r = Path(folder)/'relay.json', Path(folder)/'ftshare.json'
            l.write_text(json.dumps(capture('TUSHARE_RELAY')))
            r.write_text(json.dumps(capture('FTSHARE')))
            buf = StringIO()
            with redirect_stdout(buf):
                self.assertEqual(main(['--relay', str(l), '--ftshare', str(r), '--session', DAY]), 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data['counts']['PRICE_EQUAL'], 1)
            self.assertNotIn('10.1', buf.getvalue())
            self.assertNotIn('source-views/retained', buf.getvalue())

    def test_empty_both_cli_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as folder:
            l, r = Path(folder)/'relay.json', Path(folder)/'ftshare.json'
            left, right = capture('TUSHARE_RELAY'), capture('FTSHARE')
            left['rows'] = []; right['rows'] = []
            l.write_text(json.dumps(left)); r.write_text(json.dumps(right))
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['--relay', str(l), '--ftshare', str(r), '--session', DAY]), 2)

    def test_rows_bounded_and_untrusted_types_refuse(self):
        right = capture('FTSHARE')
        right['rows'] = right['rows'] * 33
        with self.assertRaises(MarketInputError): compare(ftshare=right)
        right = capture('FTSHARE'); right['rows'] = 'secret'
        with self.assertRaises(MarketInputError): compare(ftshare=right)


if __name__ == '__main__':
    unittest.main()
