"""All-row diagnostics retain the original security-master acceptance contract.

Every row/value in this file is synthetic; no exchange or supplier is contacted.
"""
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import tempfile
import unittest

from decision_kernel.application.market_security_inputs import (
    MASTER_CHECK_CODES, _master_row_checks, qualified_security_master)
from decision_kernel.application.market_relay_inputs import MarketRelayError, _security, _day, _require
from decision_kernel.application.market_source_check import _master_shape, check_sources
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, Response
from test_market_security_inputs import basic_row, result_for, BASIC_FIELDS
from test_market_source_check import Opener, NOW, START, END, SECRET_TEXT
import test_market_source_check as original


def old_first_failure(row, seen, exchange, status):
    """Original c50796a security predicates, not a second runtime qualifier."""
    try:
        code = _security(row['ts_code'])
        _require(code.endswith('.' + {'SSE': 'SH', 'SZSE': 'SZ'}[exchange])
                 and row['exchange'] == exchange and row['symbol'] == code[:6]
                 and row['list_status'] == status, 'MASTER_IDENTITY')
        _require(row['curr_type'] == 'CNY', 'MASTER_CURRENCY_UNSUPPORTED')
        name = row['name']
        _require(isinstance(name, str) and 0 < len(name) <= 256
                 and name == name.strip() and name.isprintable(), 'MASTER_NAME')
        listed = None if row['list_date'] in (None, '') else _day(row['list_date'])
        delisted = None if row['delist_date'] in (None, '') else _day(row['delist_date'])
        _require(status == 'G' or listed is not None, 'MASTER_LIST_DATE_REQUIRED')
        _require((status == 'D' and delisted is not None and listed <= delisted)
                 or (status != 'D' and delisted is None), 'MASTER_DELIST_DATE')
        _require(code not in seen, 'MASTER_DUPLICATE_SECURITY')
    except MarketRelayError as exc:
        return str(exc)
    return None


class MasterDiagnosticsTests(unittest.TestCase):
    def shape(self, rows, **kwargs):
        result = result_for('stock_basic', rows, **kwargs)
        before = encoded(result)
        out = _master_shape(inspect_result(result, REGISTRY), 'SSE', 'L', '2026-09-10')
        self.assertEqual(before, encoded(result))
        return out, result

    def test_original_order_and_acceptance_on_combined_counterexamples(self):
        for status, listed, delisted, duplicate in product(
                ('L', 'D', 'P', 'G'), (None, '', '19990101', '20270101', 'bad', False),
                (None, '', '20000101', '20300101', 'bad', True), (False, True)):
            row = dict(zip(BASIC_FIELDS, basic_row(status=status, listed=listed, delisted=delisted)))
            seen = {row['ts_code']: None} if duplicate else {}
            checks, _ = _master_row_checks(row, seen, 'SSE', status)
            first = next(('MARKET_RELAY_' + c for c, ok in checks.items() if ok is not True), None)
            with self.subTest(status=status, listed=listed, delisted=delisted, duplicate=duplicate):
                self.assertEqual(first, old_first_failure(row, seen, 'SSE', status))
                self.assertEqual(tuple(checks), MASTER_CHECK_CODES)
        for field, value in (('ts_code', None), ('ts_code', 'bad'), ('symbol', None),
                             ('exchange', 'SZSE'), ('list_status', 'D'), ('name', '\n'),
                             ('name', ' padded '), ('curr_type', 'USD')):
            row = dict(zip(BASIC_FIELDS, basic_row())); row[field] = value
            checks, _ = _master_row_checks(row, {}, 'SSE', 'L')
            first = next(('MARKET_RELAY_' + c for c, ok in checks.items() if ok is not True), None)
            self.assertEqual(first, old_first_failure(row, {}, 'SSE', 'L'))

    def test_full_received_range_counts_multiple_failures_without_dropping_rows(self):
        rows = [basic_row('600001.SH', delisted='20280101'), basic_row('600002.SH')]
        rows[0][2] = ' padded '; rows[0][4] = 'USD'; rows[1][6] = None
        out, result = self.shape(rows)
        self.assertEqual(out['rows_checked'], 2)
        self.assertEqual(out['rows_with_issues'], 2)
        for code in ('MASTER_CURRENCY_UNSUPPORTED', 'MASTER_NAME', 'MASTER_DELIST_DATE', 'MASTER_LIST_DATE_REQUIRED'):
            self.assertEqual(out['reason_counts']['MARKET_RELAY_' + code], 1)
        self.assertEqual(sum(out['reason_counts'].values()), 4)
        with self.assertRaisesRegex(MarketRelayError, '^MARKET_RELAY_MASTER_CURRENCY_UNSUPPORTED$'):
            qualified_security_master({'L': result}, registry=REGISTRY, exchange='SSE')

    def test_date_absence_invalid_and_after_cutoff_are_not_silently_repaired(self):
        values = (None, '', '20260910', '20270910', 'bad', 0, False)
        rows = [basic_row(f'60000{i}.SH', delisted=v) for i, v in enumerate(values)]
        out, _ = self.shape(rows)
        self.assertEqual(out['date_counts']['delist_date'],
                         {'missing': 2, 'invalid': 3, 'on_or_before_end': 1, 'after_end': 1})
        self.assertEqual(out['reason_counts']['MARKET_RELAY_DATE_INVALID'], 3)
        self.assertEqual(out['reason_counts']['MARKET_RELAY_MASTER_DELIST_DATE'], 2)
        self.assertEqual(out['not_assessable_counts']['MARKET_RELAY_MASTER_DELIST_DATE'], 3)
        for field in BASIC_FIELDS:
            self.assertEqual(sum(out['field_types'][field].values()), len(rows))

    def test_missing_invalid_identity_does_not_create_duplicate_or_guessed_dates(self):
        checks, _ = _master_row_checks({'curr_type': 'CNY'}, {}, 'SSE', 'L')
        self.assertFalse(checks['SECURITY_INVALID'])
        self.assertIsNone(checks['MASTER_IDENTITY'])
        self.assertIsNone(checks['MASTER_DUPLICATE_SECURITY'])
        self.assertFalse(checks['MASTER_LIST_DATE_REQUIRED'])
        out = _master_shape({'complete': False, 'pages': [{'table': {'fields': [], 'items': [[]]}}]},
                            'SSE', 'L', '2026-09-10')
        self.assertTrue(all(v['missing'] == 1 for v in out['field_types'].values()))
        self.assertEqual(out['date_counts']['list_date']['missing'], 1)
        self.assertEqual(out['duplicate_identical_rows'] + out['duplicate_different_rows'], 0)

    def test_cross_page_duplicate_different_and_identical_keep_original_rejection(self):
        first = basic_row('600001.SH'); different = deepcopy(first); different[2] = SECRET_TEXT
        out, result = self.shape([first, basic_row('600002.SH'), different, first])
        self.assertEqual(out['rows_checked'], 4)
        self.assertEqual(out['reason_counts']['MARKET_RELAY_MASTER_DUPLICATE_SECURITY'], 2)
        self.assertEqual((out['duplicate_identical_rows'], out['duplicate_different_rows']), (1, 1))
        self.assertNotIn(SECRET_TEXT, json.dumps(out))
        with self.assertRaisesRegex(MarketRelayError, 'MASTER_DUPLICATE_SECURITY'):
            qualified_security_master({'L': result}, registry=REGISTRY, exchange='SSE')

    def test_partial_pages_empty_and_reordered_columns_have_precise_coverage(self):
        rows = [basic_row('600001.SH'), basic_row('600002.SH'), basic_row('600003.SH')]
        partial, _ = self.shape(rows, limit=2, max_pages=1)
        self.assertEqual(partial['coverage'], 'received-pages-only')
        self.assertEqual(partial['rows_checked'], 2)
        normal, _ = self.shape(rows)
        reordered, _ = self.shape([r[::-1] for r in rows], fields=BASIC_FIELDS[::-1])
        self.assertEqual(normal, reordered)
        empty, _ = self.shape([], empty_fields=True)
        self.assertEqual(empty['coverage'], 'complete-capture-not-qualified')
        self.assertEqual(empty['rows_checked'], 0)
        self.assertEqual(sum(empty['reason_counts'].values()), 0)

    def test_valid_reference_shape_is_unchanged(self):
        row = basic_row('600001.SH')
        result = result_for('stock_basic', [row])
        checks, expected = _master_row_checks(dict(zip(BASIC_FIELDS, row)), {}, 'SSE', 'L')
        self.assertTrue(all(v is True for v in checks.values()))
        actual = qualified_security_master({'L': result}, registry=REGISTRY, exchange='SSE')
        self.assertEqual(actual['securities']['600001.SH'], expected)
        self.assertEqual(set(expected), {'ts_code', 'symbol', 'name', 'exchange', 'currency',
                                       'source_list_status', 'list_date', 'delist_date'})

    def test_existing_http_check_and_host_report_all_rows_without_values(self):
        class ContradictoryMaster(Opener):
            def open(self, request, timeout):
                response = super().open(request, timeout)
                if '/stock_basic?' not in request.full_url:
                    return response
                body = json.loads(response.raw)
                fields = body['data']['fields']
                body['data']['items'][0][fields.index('delist_date')] = '20300506'
                body['data']['items'][1][fields.index('curr_type')] = 'USD'
                return Response(request.full_url, body)
        opener = ContradictoryMaster()
        client = RelayHTTP(opener=opener, credential='synthetic-key-only')
        receipts = []
        def checker(**kw):
            original_progress = kw['progress']
            def progress(value):
                receipts.append(value); original_progress(value)
            return check_sources(**{**kw, 'progress': progress}, client=client, clock=lambda: NOW.isoformat())
        with tempfile.TemporaryDirectory() as td:
            event, env = original.MarketCheckHostTests().context(td)
            event['inputs']['selection'] = 'stock'
            out = Path(td) / 'result'
            self.assertEqual(execute(event, env, out, now=NOW, checker=checker, registry_path=REGISTRY_PATH), 2)
            report = json.loads((out / 'result.json').read_text())
            self.assertEqual(report['queries']['master']['reason_code'], 'MARKET_RELAY_MASTER_DELIST_DATE')
            shape = report['queries']['master']['master_shape']
            self.assertEqual(shape['rows_checked'], 2)
            self.assertEqual(shape['rows_with_issues'], 2)
            self.assertEqual(shape['reason_counts']['MARKET_RELAY_MASTER_CURRENCY_UNSUPPORTED'], 1)
            self.assertEqual(shape['date_counts']['delist_date']['after_end'], 1)
            self.assertEqual([api for api, _ in opener.calls], ['trade_cal', 'stock_basic'])
            self.assertEqual([p.name for p in out.iterdir()], ['result.json'])
            for receipt in receipts + [report]:
                for value in (SECRET_TEXT, '600000.SH', '600001.SH', '20300506', '19990101', 'USD',
                              'synthetic-key-only', 'wire_sha256', 'parameters'):
                    self.assertNotIn(value, json.dumps(receipt))


if __name__ == '__main__':
    unittest.main()
