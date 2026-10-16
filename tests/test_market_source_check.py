"""Real Relay/market code with synthetic HTTP, including non-publication counterexamples."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from io import BytesIO
from unittest.mock import patch

from decision_kernel.application.market_source_check import check_sources, SLOTS
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import Response, REGISTRY, REGISTRY_PATH, ROOT_DIR

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
START, END = '20260901', '20260910'
SECRET_TEXT = 'UNPUBLISHED_PROVIDER_VALUE_987654'


class Opener:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.bad = None
        self.empty = None
        self.transient = False

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        api = url.path.rsplit('/', 1)[-1]
        p = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append((api, p))
        if self.fail == api:
            raise HTTPError(request.full_url, 403, SECRET_TEXT, {}, BytesIO(b'forbidden private body'))
        if self.transient and len(self.calls) == 1:
            raise HTTPError(request.full_url, 504, SECRET_TEXT, {}, BytesIO(b'{"error":"upstream_timeout"}'))
        fields = p['fields'].split(',')
        code = p.get('ts_code')
        dates = ['202609%02d' % n for n in range(1, 11)]
        if api == 'trade_cal':
            rows = [dict(exchange=p['exchange'], cal_date=d, is_open=1) for d in dates]
        elif api == 'stock_basic':
            rows = [dict(ts_code='600000.SH', symbol='600000', name=SECRET_TEXT, exchange=p['exchange'],
                         list_status='L', curr_type='USD' if self.bad == api else 'CNY',
                         list_date='19990101', delist_date=None),
                    dict(ts_code='600001.SH', symbol='600001', name='OTHER_' + SECRET_TEXT, exchange=p['exchange'],
                         list_status='L', curr_type='CNY', list_date='19990101', delist_date=None)]
        elif api == 'ths_index':
            rows = [dict(ts_code=('880001.TI' if p['type'] == 'I' else '885001.TI'), name=SECRET_TEXT,
                         count=1, exchange='A', list_date='19990101', type='R' if self.bad == api else p['type'])]
        elif api == 'ths_member':
            rows = [dict(ts_code=code, con_code='600000.SH', con_name=SECRET_TEXT)]
        else:
            rows = [dict(ts_code=code, trade_date=d, close='9876.54321', adj_factor='1.23456789') for d in dates]
            if self.bad == api:
                rows[-1]['trade_date'] = '20260911'
        if self.empty == api:
            rows = []
        offset = int(p.get('offset', 0))
        rows = rows[offset:offset + int(p['limit'])]
        return Response(request.full_url, {'code': 0, 'api_name': api, 'count': len(rows),
            'data': {'fields': fields, 'items': [[r[f] for f in fields] for r in rows]}})


class MarketSourceCheckTests(unittest.TestCase):
    def setUp(self):
        self.opener = Opener()
        self.client = RelayHTTP(opener=self.opener, credential='synthetic-key-only')
        self.progress, self.waits = [], []

    def run_check(self, **extra):
        return check_sources(**{'registry': REGISTRY, 'exchange': 'SSE', 'start': START, 'end': END,
            'access_confirmed': True, 'client': self.client, 'now': NOW,
            'progress': self.progress.append, 'sleep': self.waits.append,
            'clock': lambda: NOW.isoformat(), **extra})

    def test_real_composition_returns_only_diagnostics(self):
        out = self.run_check()
        self.assertEqual(out['status'], 'checked-sample')
        self.assertEqual(out['http_attempts'], 10)
        self.assertEqual(set(out['queries']), set(SLOTS))
        self.assertEqual(out['surfaces']['stock']['scope_count'], 2)
        self.assertEqual(out['surfaces']['stock']['observed_count'], 1)
        for record in self.progress + [out]:
            text = json.dumps(record)
            for banned in [SECRET_TEXT, '600000.SH', '880001.TI', '9876.54321', '1.23456789',
                           'synthetic-key-only', 'wire_sha256', 'query_key', 'parameters', 'items']:
                self.assertNotIn(banned, text)
        selected = [p['ts_code'] for api, p in self.opener.calls if api == 'daily']
        self.assertEqual(selected, ['600000.SH'])
        self.assertFalse(out['source_refs_created'])

    def test_authorization_and_invalid_scope_precede_any_request(self):
        for bad in [dict(access_confirmed=False), dict(access_confirmed='true'), dict(exchange='BJ'),
                    dict(end='20261016'), dict(start='20260101'), dict(start='20260230'),
                    dict(start=END, end=START), dict(now=NOW.replace(tzinfo=None))]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.run_check(**bad)
        self.assertEqual(self.opener.calls, [])

    def test_source_auth_refusal_stops_following_requests(self):
        self.opener.fail = 'trade_cal'
        out = self.run_check()
        self.assertEqual(len(self.opener.calls), 1)
        self.assertTrue(out['stopped'])
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(out['queries']['master']['status'], 'not-attempted')
        self.assertNotIn(SECRET_TEXT, json.dumps(out))

    def test_existing_retry_wait_is_not_shortened(self):
        self.opener.transient = True
        out = self.run_check()
        self.assertEqual(self.waits, [30])
        self.assertEqual(out['http_attempts'], 11)
        self.assertEqual(out['status'], 'checked-sample')

    def test_bad_stock_master_does_not_gate_groups(self):
        self.opener.bad = 'stock_basic'
        out = self.run_check()
        self.assertEqual(out['queries']['master']['reason_code'], 'MARKET_RELAY_MASTER_CURRENCY_UNSUPPORTED')
        self.assertEqual(out['surfaces']['stock']['status'], 'rejected')
        self.assertEqual(out['surfaces']['sector']['observed_count'], 1)
        self.assertEqual(out['surfaces']['concept']['observed_count'], 1)
        self.assertNotIn('daily', [api for api, p in self.opener.calls])

    def test_empty_is_not_nonempty_success_or_zero_opportunities(self):
        self.opener.empty = 'daily'
        out = self.run_check()
        self.assertEqual(out['queries']['stock_daily']['status'], 'checked-empty')
        self.assertEqual(out['surfaces']['stock']['observed_count'], 0)
        self.assertEqual(out['status'], 'completed-with-gaps')

    def test_invalid_daily_does_not_drop_declared_denominator(self):
        self.opener.bad = 'daily'
        out = self.run_check()
        self.assertEqual(out['queries']['stock_daily']['status'], 'rejected')
        self.assertEqual(out['surfaces']['stock']['scope_count'], 2)
        self.assertEqual(out['surfaces']['sector']['observed_count'], 1)

    def test_unknown_exception_text_never_leaves_check(self):
        with patch('decision_kernel.application.market_source_check.qualified_security_master',
                   side_effect=RecordError(SECRET_TEXT)):
            out = self.run_check()
        self.assertEqual(out['queries']['master']['reason_code'], 'CHECK_REJECTED')
        self.assertNotIn(SECRET_TEXT, json.dumps(self.progress))

    def test_group_rejection_does_not_gate_independent_stock(self):
        self.opener.bad = 'ths_index'
        out = self.run_check()
        self.assertEqual(out['queries']['sector_catalog']['status'], 'rejected')
        self.assertEqual(out['surfaces']['stock']['observed_count'], 1)
        self.assertEqual(out['surfaces']['sector']['status'], 'rejected')

    def test_missing_calendar_still_checks_members_without_prices(self):
        self.opener.empty = 'trade_cal'
        out = self.run_check()
        self.assertEqual(out['queries']['calendar']['status'], 'rejected')
        self.assertEqual(out['queries']['sector_members']['status'], 'checked')
        self.assertEqual(out['queries']['stock_daily']['status'], 'not-attempted')
        self.assertEqual(out['queries']['sector_daily']['status'], 'not-attempted')

    def test_denial_in_dependent_query_stops_sibling_requests(self):
        self.opener.fail = 'daily'
        out = self.run_check()
        self.assertEqual(len(self.opener.calls), 5)
        self.assertEqual(out['queries']['stock_factor']['reason_code'], 'GLOBAL_SOURCE_STOP')
        self.assertEqual(out['queries']['sector_members']['reason_code'], 'GLOBAL_SOURCE_STOP')

    def test_no_data_export_or_store_calls(self):
        with patch('decision_kernel.application.research_sources.source_inputs', side_effect=AssertionError('no export')):
            self.assertEqual(self.run_check()['status'], 'checked-sample')
        with self.assertRaisesRegex(RecordError, 'SOURCE_PUBLIC_RIGHTS_REQUIRED'):
            list(capture_plan({}, REGISTRY))


class MarketCheckHostTests(unittest.TestCase):
    def context(self, root):
        event = {'inputs': {'code_sha': 'a' * 40, 'exchange': 'SSE', 'start': START,
                            'end': END, 'access_confirmed': 'true'}}
        env = dict(GITHUB_REPOSITORY='auguspp/Decision-OS-2', GITHUB_REF='refs/heads/main',
            GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_SHA='a' * 40, GITHUB_RUN_ID='123',
            GITHUB_RUN_ATTEMPT='1', RUNNER_TEMP=str(root))
        return event, env

    def test_invalid_host_identity_refused_before_checker(self):
        event, env = self.context('/tmp')
        for key, value in [('GITHUB_REF', 'refs/heads/other'), ('GITHUB_SHA', 'b' * 40),
                           ('GITHUB_EVENT_NAME', 'schedule'), ('GITHUB_REPOSITORY', 'other/repo'),
                           ('GITHUB_RUN_ATTEMPT', '2')]:
            with self.subTest(key=key), self.assertRaises(RecordError):
                settings(event, {**env, key: value}, NOW)
        event['inputs']['url'] = 'https://example.invalid'
        with self.assertRaises(RecordError):
            settings(event, env, NOW)

    def test_actual_host_writes_only_engineering_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            event, env = self.context(td)
            client = RelayHTTP(opener=Opener(), credential='synthetic-key-only')
            def checker(**kw):
                return check_sources(**kw, client=client, clock=lambda: NOW.isoformat())
            out = Path(td) / 'check'
            self.assertEqual(execute(event, env, out, now=NOW, checker=checker, registry_path=REGISTRY_PATH), 0)
            self.assertEqual([p.name for p in out.iterdir()], ['result.json'])
            text = (out/'result.json').read_text()
            self.assertNotIn(SECRET_TEXT, text)
            self.assertNotIn('600000.SH', text)
            self.assertEqual(json.loads(text)['identity']['code_sha'], 'a' * 40)

    def test_interruption_retains_progress_without_error_prose(self):
        with tempfile.TemporaryDirectory() as td:
            event, env = self.context(td)
            def broken(**kw):
                kw['progress']({'status': 'in-progress', 'http_attempts': 1})
                raise RuntimeError(SECRET_TEXT)
            out = Path(td) / 'check'
            self.assertEqual(execute(event, env, out, now=NOW, checker=broken, registry_path=REGISTRY_PATH), 2)
            data = (out/'result.json').read_text()
            self.assertNotIn(SECRET_TEXT, data)
            self.assertEqual(json.loads(data)['status'], 'interrupted')
            self.assertEqual(json.loads(data)['http_attempts'], 1)

    def test_workflow_is_manual_read_only_and_uploads_one_file(self):
        text = (ROOT_DIR/'.github/workflows/market-source-check.yml').read_text()
        self.assertIn('workflow_dispatch:', text)
        self.assertIn('group: decision-os-relay-source', text)
        self.assertIn('contents: read', text)
        for bad in ['contents: write', 'schedule:', 'pull_request:', 'DECISION_OS_STORE_TOKEN', 'continue-on-error']:
            self.assertNotIn(bad, text)
        self.assertIn('/dos2-market-check/result.json', text)
        self.assertNotIn('/dos2-market-check/\n', text)
