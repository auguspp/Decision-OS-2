"""Boundary regressions for offline source diagnostics; synthetic inputs only."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from decimal import Inexact, localcontext
from io import StringIO

from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.secondary_market_comparison import compare_retained_raw_market
from decision_kernel.interfaces.secondary_market_comparison_cli import main
from test_secondary_market_comparison import capture, DAY


def compare(relay=None, ftshare=None, purpose='trading-rows'):
    return compare_retained_raw_market(
        relay=relay if relay is not None else capture('TUSHARE_RELAY'),
        ftshare=ftshare if ftshare is not None else capture('FTSHARE'),
        session=DAY, purpose=purpose)


class SecondaryMarketBoundaryTests(unittest.TestCase):
    def test_volume_is_exact_even_under_low_precision_and_traps(self):
        left, right = capture('TUSHARE_RELAY'), capture('FTSHARE')
        left['rows'][0]['volume'] = '123.41'
        right['rows'][0]['volume'] = '12349'
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = True
            out = compare(relay=left, ftshare=right)
        self.assertEqual(out['details'][0]['volume_comparison'], 'DIFFERENCE')

    def test_quoted_values_can_compare_without_inventing_trading_status(self):
        left, right = capture('TUSHARE_RELAY'), capture('FTSHARE')
        for data in (left, right):
            data['rows'][0]['trade_status'] = 'UNKNOWN'
        out = compare(relay=left, ftshare=right, purpose='quoted-values')
        self.assertEqual(out['counts']['PRICE_EQUAL'], 1)
        self.assertFalse(out['details'][0]['trading_status_confirmed'])
        self.assertFalse(out['trading_eligibility_granted'])
        self.assertEqual(compare(relay=left, ftshare=right)['comparison_status'], 'NO_COMPARABLE_PAIRS')
        with self.assertRaises(MarketInputError):
            compare(purpose='unknown')

    def test_artifact_reference_is_distinct_from_git_source_path(self):
        right = capture('FTSHARE')
        right['source_ref'].update(kind='github-actions-artifact', run_id=123,
            attempt=1, artifact_id=456, artifact_sha256='e' * 64)
        out = compare(ftshare=right)
        self.assertFalse(out['provenance_verified_by_this_function'])
        for key, value in [('run_id', True), ('artifact_id', -1), ('artifact_sha256', 'bad')]:
            wrong = deepcopy(right)
            wrong['source_ref'][key] = value
            with self.subTest(key=key), self.assertRaises(MarketInputError):
                compare(ftshare=wrong)

    def test_invalid_file_and_duplicate_json_return_safe_errors(self):
        for raw in ('{"provider":"FTSHARE","provider":"TUSHARE_RELAY"}',
                    '{"secret":"do-not-echo",BROKEN}', '{"x":NaN}', '[]'):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'private-filename'
                path.write_text(raw)
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(['--relay', str(path), '--ftshare', str(path), '--session', DAY]), 2)
                self.assertEqual(out.getvalue(), '')
                self.assertEqual(json.loads(err.getvalue())['reason_code'], 'SECONDARY_SOURCE_INPUT_REJECTED')
                self.assertNotIn('do-not-echo', err.getvalue())
                self.assertNotIn('private-filename', err.getvalue())

    def test_bounded_file_read_and_symlink_ancestors(self):
        from decision_kernel.interfaces.secondary_market_comparison_cli import MAX_FILE_BYTES, _read
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            large = root / 'large'
            large.write_bytes(b' ' * (MAX_FILE_BYTES + 1))
            with self.assertRaises(ValueError):
                _read(large)
            original = root / 'actual'
            original.mkdir()
            (original / 'data').write_text('{}')
            link = root / 'link'
            link.symlink_to(original, target_is_directory=True)
            with self.assertRaises(ValueError):
                _read(link / 'data')

