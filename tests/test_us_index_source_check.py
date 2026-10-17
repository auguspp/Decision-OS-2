"""Original Relay/capture/read/host chain; all calendar/level responses are synthetic."""
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from decision_kernel.application import market_source_check as checker
from decision_kernel.application.global_index_inputs import read_us_index_context
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import Response, REGISTRY, REGISTRY_PATH, ROOT_DIR
import test_market_source_check as original

START, END = '20260701', '20261015'
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
SECRET = 'SUPPLIER_SENTINEL_NOT_FOR_PUBLICATION'


class USOpener:
    def __init__(self, faults=None, transient=None):
        self.faults = faults or {}
        self.transient = transient
        self.calls = []

    def open(self, request, timeout):
        parts = urlsplit(request.full_url)
        api = parts.path.rsplit('/', 1)[-1]
        p = {k: v[0] for k, v in parse_qs(parts.query).items()}
        assert api in ('us_tradecal', 'index_global')
        assert request.get_method() == 'GET' and timeout == 30
        assert request.get_header('X-api-key') == 'synthetic-key-only'
        assert 'exchange' not in p and 'is_open' not in p and '__probe' not in p
        key = 'calendar' if api == 'us_tradecal' else p['ts_code']
        self.calls.append((key, api, p))
        fault = self.faults.get(key)
        if fault in ('denied', 'rate'):
            raise HTTPError(request.full_url, 403 if fault == 'denied' else 429,
                            SECRET, {}, BytesIO(b'private transport body'))
        if fault == 'timeout':
            raise URLError(TimeoutError(SECRET))
        if self.transient == key and sum(k == key for k, _, _ in self.calls) == 1:
            raise HTTPError(request.full_url, 504, SECRET, {}, BytesIO(b'{"error":"upstream_timeout"}'))
        lo = datetime.strptime(p['start_date'], '%Y%m%d').date()
        hi = datetime.strptime(p['end_date'], '%Y%m%d').date()
        dates = [(lo + timedelta(days=i)).strftime('%Y%m%d') for i in range((hi-lo).days+1)]
        # Intentionally synthetic: a single closed date, not a real exchange calendar.
        closed = dates[3] if len(dates) > 3 else None
        fields = p['fields'].split(',')
        if key == 'calendar':
            rows = [dict(cal_date=d, is_open=int(d != closed)) for d in dates]
            if fault == 'gap': rows.pop(1)
            if fault == 'duplicate': rows.append(dict(rows[0]))
            if fault == 'boolean': rows[0]['is_open'] = True
            if fault == 'float': rows[0]['is_open'] = 1.0
            if fault == 'closed-end': rows[-1]['is_open'] = 0
        else:
            rows = [dict(ts_code=key, trade_date=d, close=str(9000+i)+'.987654321')
                    for i, d in enumerate(dates) if d != closed]
            if fault == 'missing-middle': rows.pop(-10)
            if fault == 'missing-end': rows.pop()
            if fault == 'duplicate': rows.append(dict(rows[0]))
            if fault == 'code': rows[0]['ts_code'] = SECRET
            if fault == 'closed-date': rows[0]['trade_date'] = closed
            if fault == 'level': rows[0]['close'] = 'NaN'
            if fault == 'boolean': rows[0]['close'] = True
            if fault == 'fields': fields.remove('close')
            if fault in ('two-pages', 'page-bound'):
                total = 5000 if fault == 'two-pages' else 10000
                rows = [dict(ts_code=key, trade_date=dates[0], close=str(i+1)) for i in range(total)]
        if fault == 'empty': rows = []
        offset, limit = int(p.get('offset', 0)), int(p['limit'])
        rows = rows[offset:offset+limit]
        return Response(request.full_url, {'code': 0, 'api_name': api, 'count': len(rows),
            'data': {'fields': fields, 'items': [[r[f] for f in fields] for r in rows]}})


class USIndexSourceCheckTests(unittest.TestCase):
    def run_check(self, faults=None, transient=None, **extra):
        self.opener = USOpener(faults, transient)
        self.client = RelayHTTP(opener=self.opener, credential='synthetic-key-only')
        self.receipts, self.waits = [], []
        return checker.check_sources(**{'registry': REGISTRY, 'exchange': 'US',
            'start': START, 'end': END, 'selection': 'us-indices', 'access_confirmed': True,
            'client': self.client, 'now': NOW, 'clock': lambda: NOW.isoformat(),
            'sleep': self.waits.append, 'progress': self.receipts.append, **extra})

    def test_exact_five_queries_and_actual_existing_reader(self):
        with patch.object(checker, 'read_us_index_context', wraps=read_us_index_context) as reader:
            out = self.run_check()
        self.assertEqual(reader.call_count, 1)
        self.assertEqual(set(reader.call_args.kwargs['index_results']), set(checker.US_INDEX_CODES))
        self.assertEqual(out['status'], 'checked-us-index-context')
        self.assertEqual(out['scope'], {'market': 'US', 'start': START, 'end': END})
        self.assertEqual(set(out['queries']), set(checker.US_INDEX_SLOTS))
        self.assertEqual([k for k, _, _ in self.opener.calls], ['calendar', 'DJI', 'IXIC', 'RUT', 'SPX'])
        self.assertEqual(out['http_attempts'], 5)
        surface = out['surfaces']['us_indices']
        self.assertEqual((surface['scope_count'], surface['observed_count']), (4, 4))
        self.assertEqual(surface['source_qualification_counts'],
                         {'checked': 4, 'checked-empty': 0, 'rejected': 0, 'not-supplied': 0})
        self.assertTrue(all(c['observed'] == 4 and sum(c.values()) == 4
                            for c in surface['window_counts'].values()))

    def test_domestic_qualifiers_and_export_never_invoked(self):
        with ExitStack() as stack:
            for name in ('qualified_trade_calendar', 'qualified_security_master', 'qualified_ths_catalog',
                         'qualified_ths_members', 'read_three_market_surfaces', '_master_shape', '_catalogue_shape'):
                stack.enter_context(patch.object(checker, name, side_effect=AssertionError('not a domestic check')))
            for name in ('source_inputs', 'capture_plan'):
                stack.enter_context(patch('decision_kernel.application.research_sources.'+name,
                                         side_effect=AssertionError('no source export')))
            out = self.run_check()
        self.assertFalse(out['source_refs_created'])
        self.assertFalse(out['supplier_data_retained'])

    def test_every_incremental_receipt_excludes_values_and_lineage(self):
        out = self.run_check()
        for record in self.receipts + [out]:
            text = json.dumps(record)
            for secret in (SECRET, 'synthetic-key-only', '.987654321', 'DJI', 'IXIC', 'RUT', 'SPX',
                           '2026-07-02', 'baseline_level', 'end_level', 'change_pct', 'market_sessions',
                           'view_version', 'wire_sha256', 'query_key', 'parameters', 'items', 'ts_code'):
                self.assertNotIn(secret, text)
        self.receipts[0]['queries']['us_calendar']['status'] = 'mutated'
        self.assertEqual(out['queries']['us_calendar']['status'], 'checked')

    def test_missing_intermediate_keeps_short_window_and_four_declared_indices(self):
        out = self.run_check({'RUT': 'missing-middle'})
        s = out['surfaces']['us_indices']
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(s['observed_count'], 4)
        self.assertEqual(s['window_counts']['5']['observed'], 4)
        for horizon in ('20', '60'):
            self.assertEqual(s['window_counts'][horizon]['observed'], 3)
            self.assertEqual(s['window_counts'][horizon]['missing-market-sessions'], 1)
        self.assertTrue(all(q['status'] == 'checked' for q in out['queries'].values()))

    def test_short_calendar_is_not_full_horizon_success(self):
        out = self.run_check(start='20261001')
        w = out['surfaces']['us_indices']['window_counts']
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(w['5']['observed'], 4)
        self.assertEqual(w['20']['insufficient-calendar-history'], 4)
        self.assertEqual(w['60']['insufficient-calendar-history'], 4)

    def test_missing_end_is_not_shifted_to_latest_received_level(self):
        out = self.run_check({'RUT': 'missing-end'})
        s = out['surfaces']['us_indices']
        self.assertEqual(s['observed_count'], 3)
        self.assertTrue(all(c['missing-market-sessions'] == 1 for c in s['window_counts'].values()))
        self.assertEqual(out['status'], 'completed-with-gaps')

    def test_bad_shared_calendar_keeps_unknown_counts_and_sends_no_index_requests(self):
        for fault in ('empty', 'gap', 'duplicate', 'boolean', 'float', 'closed-end'):
            with self.subTest(fault=fault):
                out = self.run_check({'calendar': fault})
                self.assertEqual(out['http_attempts'], 1)
                self.assertEqual(out['status'], 'completed-with-gaps')
                s = out['surfaces']['us_indices']
                self.assertEqual(s['status'], 'rejected')
                self.assertEqual(s['scope_count'], 4)
                self.assertIsNone(s['observed_count'])
                self.assertIsNone(s['window_counts'])
                self.assertTrue(all(out['queries']['us_'+c.lower()]['status'] == 'not-attempted'
                                    for c in checker.US_INDEX_CODES))

    def test_one_bad_index_is_isolated_and_remains_in_window_denominators(self):
        for fault in ('duplicate', 'code', 'closed-date', 'level', 'boolean', 'fields'):
            with self.subTest(fault=fault):
                out = self.run_check({'DJI': fault})
                self.assertEqual(out['http_attempts'], 5)
                q = out['queries']['us_dji']
                # The generic capture owner rejects scope/field mismatches before
                # retaining a page; other faults reach the market-specific reader.
                early = {'code': 'SOURCE_RETURNED_SCOPE_MISMATCH',
                         'fields': 'SOURCE_REQUESTED_FIELDS_MISSING'}
                self.assertEqual(q['status'], 'capture-incomplete' if fault in early else 'rejected')
                if fault in early:
                    self.assertEqual(q['reason_code'], early[fault])
                    self.assertEqual((q['row_count'], q['page_count']), (0, 0))
                self.assertEqual(q['http_attempts'], 1)
                self.assertEqual(self.waits, [])
                self.assertEqual(out['queries']['us_spx']['status'], 'checked')
                s = out['surfaces']['us_indices']
                self.assertEqual((s['scope_count'], s['observed_count']), (4, 3))
                self.assertTrue(all(c['observed'] == 3 and c['inputs-unavailable'] == 1
                                    for c in s['window_counts'].values()))

    def test_complete_empty_index_is_not_absent_capture(self):
        out = self.run_check({'IXIC': 'empty'})
        self.assertEqual(out['queries']['us_ixic']['status'], 'checked-empty')
        s = out['surfaces']['us_indices']
        self.assertEqual(s['source_qualification_counts']['checked-empty'], 1)
        self.assertEqual(s['source_qualification_counts']['not-supplied'], 0)
        self.assertTrue(all(w['missing-market-sessions'] == 1 and w['inputs-unavailable'] == 0
                            for w in s['window_counts'].values()))
        self.assertEqual(out['status'], 'completed-with-gaps')

    def test_source_refusal_and_rate_limit_stop_remaining_slots(self):
        for key in ('calendar', 'DJI'):
            for fault in ('denied', 'rate'):
                with self.subTest(key=key, fault=fault):
                    out = self.run_check({key: fault})
                    self.assertEqual(out['http_attempts'], 1 if key == 'calendar' else 2)
                    self.assertTrue(out['stopped'])
                    self.assertEqual(out['queries']['us_spx']['reason_code'], 'GLOBAL_SOURCE_STOP')
                    self.assertEqual(self.waits, [])
                    self.assertNotIn(SECRET, json.dumps(self.receipts))

    def test_original_retry_recovery_retains_failed_attempt(self):
        out = self.run_check(transient='calendar')
        self.assertEqual(self.waits, [30])
        self.assertEqual(out['http_attempts'], 6)
        self.assertEqual(out['status'], 'checked-us-index-context')
        self.assertEqual(out['queries']['us_calendar']['last_attempt_failure']['classification'], 'temporary-upstream')

    def test_exhausted_calendar_is_unobserved_not_zero_anomalies(self):
        out = self.run_check({'calendar': 'timeout'})
        self.assertEqual(out['http_attempts'], 4)
        self.assertEqual(self.waits, [30, 30, 30])
        q = out['queries']['us_calendar']
        self.assertEqual((q['status'], q['row_count'], q['page_count']), ('capture-incomplete', 0, 0))
        self.assertEqual(q['last_attempt_failure']['classification'], 'timeout')
        self.assertIsNone(out['surfaces']['us_indices']['observed_count'])
        self.assertEqual(out['queries']['us_dji']['http_attempts'], 0)

    def test_exhausted_index_does_not_stop_independent_siblings(self):
        out = self.run_check({'DJI': 'timeout'})
        self.assertEqual(out['http_attempts'], 8)
        self.assertEqual(out['queries']['us_dji']['status'], 'capture-incomplete')
        self.assertEqual(out['queries']['us_spx']['status'], 'checked')
        self.assertEqual(out['surfaces']['us_indices']['observed_count'], 3)
        self.assertEqual(out['surfaces']['us_indices']['window_counts']['60']['inputs-unavailable'], 1)

    def test_original_pagination_and_page_bound_are_not_relaxed(self):
        for fault, pages, rows in [('two-pages', 2, 5000), ('page-bound', 2, 10000)]:
            with self.subTest(fault=fault):
                out = self.run_check({'DJI': fault})
                q = out['queries']['us_dji']
                self.assertEqual((q['page_count'], q['row_count']), (pages, rows))
                self.assertEqual(q['status'], 'rejected' if fault == 'two-pages' else 'capture-incomplete')
                self.assertEqual(out['queries']['us_spx']['status'], 'checked')
                self.assertEqual(out['surfaces']['us_indices']['scope_count'], 4)

    def test_invalid_access_scope_and_US_date_precede_traffic(self):
        for extra in ({'access_confirmed': False}, {'access_confirmed': 'true'}, {'exchange': 'SSE'},
                      {'selection': 'stock'}, {'selection': 'unknown'}, {'start': '20260101'},
                      {'end': '20261016'}, {'now': NOW.replace(tzinfo=None)},
                      {'now': datetime(2026,10,16,1,tzinfo=timezone.utc)}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.run_check(**extra)
            self.assertEqual(self.opener.calls, [])

    def test_US_day_boundary_handles_winter_summer_and_domestic_unchanged(self):
        for end, before, after in [('20261015', '2026-10-16T03:59:00+00:00', '2026-10-16T04:00:00+00:00'),
                                  ('20260115', '2026-01-16T04:59:00+00:00', '2026-01-16T05:00:00+00:00')]:
            with self.subTest(end=end):
                with self.assertRaises(RecordError):
                    checker.check_scope('US', end, end, datetime.fromisoformat(before), 'us-indices')
                checker.check_scope('US', end, end, datetime.fromisoformat(after), 'us-indices')
                checker.check_scope('SSE', end, end, datetime.fromisoformat(before))

    def test_actual_host_receipt_and_identity_exit_codes(self):
        for faults, expected in [({}, 0), ({'IXIC': 'empty'}, 2), ({'calendar': 'gap'}, 2)]:
            with self.subTest(faults=faults), tempfile.TemporaryDirectory() as td:
                event, env = original.MarketCheckHostTests().context(td)
                event['inputs'].update(selection='us-indices', exchange='US', start=START, end=END)
                client = RelayHTTP(opener=USOpener(faults), credential='synthetic-key-only')
                def run(**kw):
                    return checker.check_sources(**kw, client=client, clock=lambda: NOW.isoformat())
                out = Path(td)/'us-check'
                self.assertEqual(execute(event, env, out, now=NOW, checker=run, registry_path=REGISTRY_PATH), expected)
                self.assertEqual([p.name for p in out.iterdir()], ['result.json'])
                text = (out/'result.json').read_text()
                self.assertNotIn(SECRET, text)
                self.assertNotIn('.987654321', text)
                self.assertEqual(json.loads(text)['identity']['code_sha'], 'a'*40)
                for key, value in [('GITHUB_RUN_ATTEMPT','2'), ('GITHUB_SHA','b'*40),
                                   ('GITHUB_REF','refs/heads/other'), ('GITHUB_EVENT_NAME','schedule')]:
                    with self.assertRaises(RecordError): settings(event, {**env,key:value}, NOW)
                with self.assertRaises(RecordError):
                    settings(event, env, datetime(2026,10,16,1,tzinfo=timezone.utc))

    def test_unknown_reader_error_is_redacted(self):
        with patch.object(checker, 'read_us_index_context', side_effect=RecordError(SECRET)):
            out = self.run_check()
        self.assertEqual(out['surfaces']['us_indices']['reason_code'], 'CHECK_REJECTED')
        self.assertNotIn(SECRET, json.dumps(self.receipts))
        self.assertIsNone(out['surfaces']['us_indices']['window_counts'])

    def test_original_workflow_and_permission_contract_remain(self):
        text = (ROOT_DIR/'.github/workflows/market-source-check.yml').read_text()
        for expected in ('options: [sample, catalogues, stock, us-indices, availability, stock-availability, members, resume-members, session-coverage, stock-history]', 'options: [SSE, SZSE, US, CN]',
                         'default: sample', 'default: SSE', 'contents: read',
                         'group: decision-os-relay-source', '/dos2-market-check/result.json'):
            self.assertIn(expected, text)
        for forbidden in ('schedule:', 'contents: write', 'DECISION_OS_STORE_TOKEN', 'continue-on-error'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(checker.SLOTS, original.SLOTS)
        self.assertEqual(len(checker.SLOTS), 10)


if __name__ == '__main__':
    unittest.main()
