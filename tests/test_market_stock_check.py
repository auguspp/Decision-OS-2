"""Independent stock checks through the original transport, reader and manual host."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from decision_kernel.application.market_source_check import check_sources, STOCK_SLOTS
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_source_check import Opener, NOW, START, END, SECRET_TEXT
import test_market_source_check as original


class MarketStockCheckTests(unittest.TestCase):
    def run_check(self, opener=None, **extra):
        self.opener = opener or Opener()
        self.waits, self.receipts = [], []
        self.client = RelayHTTP(opener=self.opener, credential='synthetic-key-only')
        return check_sources(**{'registry': REGISTRY, 'exchange': 'SSE', 'start': START, 'end': END,
            'access_confirmed': True, 'selection': 'stock', 'client': self.client,
            'now': NOW, 'sleep': self.waits.append, 'progress': self.receipts.append,
            'clock': lambda: NOW.isoformat(), **extra})

    def test_only_four_stock_slots_and_full_master_denominator(self):
        out = self.run_check()
        self.assertEqual(out['status'], 'checked-stock-sample')
        self.assertEqual(set(out['queries']), set(STOCK_SLOTS))
        self.assertEqual([a for a, p in self.opener.calls], ['trade_cal', 'stock_basic', 'daily', 'adj_factor'])
        self.assertEqual(out['http_attempts'], 4)
        self.assertEqual(out['surfaces'], {'stock': {'status': 'read', 'scope_count': 2, 'observed_count': 1}})
        selected = [p['ts_code'] for a, p in self.opener.calls if a == 'daily']
        self.assertEqual(selected, ['600000.SH'])
        self.assertIn('not-candidates', out['sample_policy'])

    def test_no_ths_qualification_or_diagnostic_work_is_invoked(self):
        with ExitStack() as stack:
            for name in ('qualified_ths_catalog', 'qualified_ths_members', '_group_levels', '_catalogue_shape'):
                stack.enter_context(patch('decision_kernel.application.market_source_check.'+name,
                    side_effect=AssertionError('stock-only must not inspect THS')))
            self.assertEqual(self.run_check()['status'], 'checked-stock-sample')
        opener = Opener(); opener.fail = 'ths_index'
        self.assertEqual(self.run_check(opener)['status'], 'checked-stock-sample')

    def test_bad_master_is_not_a_watchlist_fallback(self):
        opener = Opener(); opener.bad = 'stock_basic'
        out = self.run_check(opener)
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(out['queries']['master']['reason_code'], 'MARKET_RELAY_MASTER_CURRENCY_UNSUPPORTED')
        self.assertEqual(out['queries']['stock_daily']['status'], 'not-attempted')
        self.assertEqual(out['queries']['stock_factor']['status'], 'not-attempted')
        self.assertEqual(out['surfaces']['stock']['status'], 'rejected')
        self.assertEqual(out['http_attempts'], 2)

    def test_empty_master_is_distinct_from_missing_or_failed(self):
        opener = Opener(); opener.empty = 'stock_basic'
        out = self.run_check(opener)
        self.assertEqual(out['queries']['master']['status'], 'checked-empty')
        self.assertEqual(out['surfaces']['stock']['scope_count'], 0)
        self.assertEqual(out['surfaces']['stock']['observed_count'], 0)
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(out['http_attempts'], 2)

    def test_bad_calendar_does_not_request_prices_or_factors(self):
        opener = Opener(); opener.empty = 'trade_cal'
        out = self.run_check(opener)
        self.assertEqual(out['queries']['calendar']['status'], 'rejected')
        self.assertEqual(out['queries']['master']['status'], 'checked')
        self.assertEqual(out['http_attempts'], 2)
        self.assertEqual(out['queries']['stock_factor']['status'], 'not-attempted')

    def test_empty_or_bad_quotes_and_factors_keep_scope_and_fail_sample(self):
        for api in ('daily', 'adj_factor'):
            for fault in ('empty', 'bad'):
                with self.subTest(api=api, fault=fault):
                    opener = Opener(); setattr(opener, fault, api)
                    out = self.run_check(opener)
                    self.assertEqual(out['http_attempts'], 4)
                    self.assertEqual(out['status'], 'completed-with-gaps')
                    self.assertEqual(out['surfaces']['stock']['scope_count'], 2)
                    self.assertEqual(out['surfaces']['stock']['observed_count'], 0)
                    slot = 'stock_daily' if api == 'daily' else 'stock_factor'
                    self.assertEqual(out['queries'][slot]['status'], 'checked-empty' if fault == 'empty' else 'rejected')

    def test_terminal_refusal_stops_only_with_existing_global_stop(self):
        for api, attempts in (('stock_basic', 2), ('daily', 3)):
            with self.subTest(api=api):
                opener = Opener(); opener.fail = api
                out = self.run_check(opener)
                self.assertEqual(out['http_attempts'], attempts)
                self.assertTrue(out['stopped'])
                self.assertEqual(out['queries']['stock_factor']['reason_code'], 'GLOBAL_SOURCE_STOP')
                self.assertEqual(out['status'], 'completed-with-gaps')
                self.assertEqual(self.waits, [])

    def test_existing_retry_is_reused_and_transient_recovery_is_explicit(self):
        opener = Opener(); opener.transient = True
        out = self.run_check(opener)
        self.assertEqual(self.waits, [30])
        self.assertEqual(out['http_attempts'], 5)
        self.assertEqual(out['status'], 'checked-stock-sample')
        self.assertEqual(out['queries']['calendar']['last_attempt_failure']['classification'], 'temporary-upstream')
        self.assertIsNone(out['queries']['master']['last_attempt_failure'])

    def test_master_transport_failure_retains_safe_reason_without_any_ths_traffic(self):
        class MasterTimeout(Opener):
            def open(self, request, timeout):
                if '/stock_basic?' in request.full_url:
                    self.calls.append(('stock_basic', {}))
                    raise URLError(TimeoutError(SECRET_TEXT))
                return super().open(request, timeout)
        out = self.run_check(MasterTimeout())
        self.assertEqual(out['http_attempts'], 5)
        self.assertEqual(self.waits, [30, 30, 30])
        self.assertEqual(out['queries']['master']['status'], 'capture-incomplete')
        self.assertEqual(out['queries']['master']['reason_code'], 'RELAY_HTTP_UNAVAILABLE')
        self.assertEqual(out['queries']['master']['last_attempt_failure']['classification'], 'timeout')
        self.assertEqual(out['queries']['stock_daily']['status'], 'not-attempted')
        self.assertNotIn(SECRET_TEXT, json.dumps(self.receipts))

    def test_access_and_scope_still_precede_credentials_and_traffic(self):
        client = RelayHTTP(opener=Opener(), credential='')
        for extra in ({'access_confirmed': False}, {'end': '20261016'}, {'exchange': 'BJ'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.run_check(client=client, **extra)
        self.assertEqual(client.requests, 0)

    def test_stock_host_writes_only_safe_receipt_and_requires_original_identity(self):
        with tempfile.TemporaryDirectory() as td:
            event, env = original.MarketCheckHostTests().context(td)
            event['inputs']['selection'] = 'stock'
            self.assertEqual(settings(event, env, NOW)['selection'], 'stock')
            client = RelayHTTP(opener=Opener(), credential='synthetic-key-only')
            def checker(**kw):
                return check_sources(**kw, client=client, clock=lambda: NOW.isoformat())
            out = Path(td)/'stock'
            self.assertEqual(execute(event, env, out, now=NOW, checker=checker, registry_path=REGISTRY_PATH), 0)
            self.assertEqual([p.name for p in out.iterdir()], ['result.json'])
            text = (out/'result.json').read_text()
            for banned in (SECRET_TEXT, '600000.SH', '9876.54321', '1.23456789', 'synthetic-key-only', 'wire_sha256'):
                self.assertNotIn(banned, text)
            result = json.loads(text)
            self.assertEqual(result['identity']['attempt'], '1')
            self.assertEqual(result['status'], 'checked-stock-sample')
            self.assertFalse(result['source_refs_created'])
            with self.assertRaises(RecordError):
                settings(event, {**env, 'GITHUB_RUN_ATTEMPT': '2'}, NOW)
            with self.assertRaises(RecordError):
                settings(event, {**env, 'GITHUB_SHA': 'b'*40}, NOW)

    def test_workflow_adds_choice_without_new_permission_or_entrypoint(self):
        text = (ROOT_DIR/'.github/workflows/market-source-check.yml').read_text()
        self.assertIn('options: [sample, catalogues, stock, us-indices, availability, stock-availability, members, resume-members, session-coverage]', text)
        self.assertIn('default: sample', text)
        self.assertIn('decision_kernel.interfaces.market_source_check_cli', text)
        self.assertIn('contents: read', text)
        for forbidden in ('contents: write', 'schedule:', 'DECISION_OS_STORE_TOKEN', 'continue-on-error'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
