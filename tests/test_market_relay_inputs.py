"""Synthetic market data through the real registry/HTTP/capture/reading contracts.

The calendar deliberately has a synthetic closed Wednesday. No real provider,
exchange calendar, source rights or production custody is certified by these tests.
"""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from decision_kernel.application.market_relay_inputs import (
    MarketRelayError, qualified_trade_calendar, qualified_daily, read_relay_stock_surface)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_plan, source_inputs
from decision_kernel.domain.records import SourceInput, digest, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.market_daily import MarketInputError

ROOT_DIR = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT_DIR / 'docs/sources/relay-registry.json'
REGISTRY = RelayRegistry(REGISTRY_PATH)
START, END, CODE = '20261012', '20261016', '600519.SH'
FIELDS = {'trade_cal': ['exchange', 'cal_date', 'is_open'],
          'daily': ['ts_code', 'trade_date', 'close']}
CALENDAR = [['SSE', f'202610{n}', int(n != 14)] for n in (12, 13, 14, 15, 16)]
PRICES = [[CODE, f'202610{n}', str(100 + n - 12)] for n in (12, 13, 15, 16)]


class Response:
    status = 200

    def __init__(self, url, body):
        self.url, self.raw = url, encoded(body)
        self.headers = {'Content-Type': 'application/json', 'Content-Length': str(len(self.raw))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.raw[:limit]


class SyntheticOpener:
    def __init__(self, api, fields, rows, empty_fields=False):
        self.api, self.fields, self.rows = api, fields, rows
        self.empty_fields, self.calls = empty_fields, []

    def open(self, request, timeout):
        assert request.full_url.startswith(ROOT + '/' + self.api + '?')
        assert request.get_method() == 'GET' and timeout == 30
        assert request.get_header('X-api-key') == 'synthetic-key-only'
        params = parse_qs(urlsplit(request.full_url).query)
        assert '__probe' not in params
        self.calls.append(request.full_url)
        offset, limit = int(params.get('offset', ['0'])[0]), int(params['limit'][0])
        rows = deepcopy(self.rows[offset:offset + limit])
        return Response(request.full_url, {'code': 0, 'api_name': self.api, 'count': len(rows),
            'data': {'fields': [] if not rows and self.empty_fields else self.fields, 'items': rows}})


def captured(api, rows, *, fields=None, params=None, limit=2, empty_fields=False):
    fields = FIELDS[api] if fields is None else fields
    scope = {'exchange': 'SSE'} if api == 'trade_cal' else {'ts_code': CODE}
    parameters = {**scope, 'start_date': START, 'end_date': END, 'limit': str(limit),
                  'fields': ','.join(fields), **(params or {})}
    request = {'api': api, 'stage': 'baseline', 'purpose': 'synthetic market contract test',
               'parameters': parameters}
    opener = SyntheticOpener(api, fields, rows, empty_fields)
    client = RelayHTTP(opener=opener, credential='synthetic-key-only')
    result = list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-market',
        'requests': [request]}, REGISTRY, client=client, public_rights_confirmed=True,
        policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: '2026-10-16T02:00:00+00:00'))[0]
    assert client.requests == len(opener.calls)
    return result


class MarketRelayInputTests(unittest.TestCase):
    def setUp(self):
        self.calendar = captured('trade_cal', CALENDAR)
        self.daily = captured('daily', PRICES)

    def days(self, value=None, **kwargs):
        return qualified_trade_calendar(self.calendar if value is None else value,
            **{'registry': REGISTRY, 'exchange': 'SSE', 'start': START, 'end': END, **kwargs})

    def quotes(self, value=None, **kwargs):
        return qualified_daily(self.daily if value is None else value,
            **{'registry': REGISTRY, 'ts_code': CODE, 'start': START, 'end': END,
               'calendar': self.days(), **kwargs})

    def test_actual_capture_pages_and_price_gaps(self):
        self.assertEqual(self.days(), ['2026-10-12', '2026-10-13', '2026-10-15', '2026-10-16'])
        self.assertEqual([p['offset'] for p in self.daily['capture']['pages']], [0, 2, 4])
        value = captured('daily', [PRICES[0], PRICES[2]])
        self.assertEqual([r['trade_date'] for r in self.quotes(value)], ['2026-10-12', '2026-10-15'])

    def test_incomplete_flag_cannot_override_pagination(self):
        for key, value in (('complete', False), ('error', 'SOURCE_PAGE_BOUND_REACHED')):
            with self.subTest(key=key):
                bad = deepcopy(self.daily)
                bad['capture'][key] = value
                with self.assertRaises(MarketRelayError):
                    self.quotes(bad)
        bad = deepcopy(self.daily)
        bad['capture']['pages'].pop()  # Full final page is not a terminal page.
        with self.assertRaises(MarketRelayError):
            self.quotes(bad)

    def test_missing_calendar_day_refused(self):
        with self.assertRaisesRegex(MarketRelayError, 'CALENDAR_GAP'):
            self.days(captured('trade_cal', CALENDAR[:1] + CALENDAR[2:]))

    def test_cross_security_and_closed_session_refused(self):
        for row in ([CODE, '20261014', '100'], ['000001.SZ', START, '100']):
            with self.subTest(row=row), self.assertRaises(MarketRelayError):
                self.quotes(captured('daily', [row]))

    def test_invalid_prices_and_duplicate_refused(self):
        for value in ('NaN', '-1', '0', '1e9999', '1e-100', True, None, [], '1' * 129):
            with self.subTest(value=value), self.assertRaises(MarketRelayError):
                self.quotes(captured('daily', [[CODE, START, value]]))
        with self.assertRaises(MarketRelayError):
            self.quotes(captured('daily', [PRICES[0], PRICES[0]]))

    def test_calendar_exchange_duplicate_and_flag_refused(self):
        for row in (['SSE', START, 1], ['SZSE', END, 1], ['SSE', END, 'unknown'], ['SSE', END, True]):
            with self.subTest(row=row), self.assertRaises(MarketRelayError):
                self.days(captured('trade_cal', CALENDAR + [row]))

    def test_invalid_fields_and_page_offset_refused(self):
        bad = deepcopy(self.daily)
        bad['capture']['pages'][0]['offset'] = 4
        with self.assertRaises(MarketRelayError):
            self.quotes(bad)
        bad = captured('daily', [[CODE, START]], fields=['ts_code', 'trade_date'])
        with self.assertRaisesRegex(MarketRelayError, 'FIELDS_NOT_REQUESTED'):
            self.quotes(bad)

    def test_legitimate_empty_and_fieldless_terminal(self):
        self.assertEqual(self.quotes(captured('daily', [], empty_fields=True)), [])
        out = self.quotes(captured('daily', PRICES, empty_fields=True))
        self.assertEqual(len(out), 4)

    def test_returned_field_order_is_not_assumed(self):
        rows = [[row[2], row[0], row[1]] for row in PRICES]
        out = self.quotes(captured('daily', rows, fields=['close', 'ts_code', 'trade_date']))
        self.assertEqual(out, self.quotes())

    def test_nonzero_capture_offset_never_means_complete_scope(self):
        bad = captured('daily', PRICES, params={'offset': '2'})
        with self.assertRaisesRegex(MarketRelayError, 'START_AT_ZERO'):
            self.quotes(bad)

    def test_unknown_filter_cannot_silently_narrow_scope(self):
        for api, key, value in (('trade_cal', 'is_open', '1'), ('daily', 'trade_date', START),
                                ('daily', 'adj', 'qfq')):
            target = captured(api, CALENDAR if api == 'trade_cal' else PRICES,
                              params={key: value})
            with self.subTest(key=key), self.assertRaisesRegex(MarketRelayError, 'QUERY_SCOPE'):
                (self.days if api == 'trade_cal' else self.quotes)(target)

    def test_invalid_identity_and_calendar_even_for_empty_prices(self):
        empty = captured('daily', [], empty_fields=True)
        for code in ('not-a-stock', '600519.sh', '600519.SH,000001.SZ', '920001.BJ', None, []):
            with self.subTest(code=code), self.assertRaisesRegex(MarketRelayError, 'SECURITY_INVALID'):
                self.quotes(empty, ts_code=code)
        for days in (['2026-99-99'], [None], [[]], '2026-10-12',
                     ['2026-10-16', '2026-10-12'], ['2026-10-12'] * 2, ['2026-10-11']):
            with self.subTest(days=days), self.assertRaisesRegex(MarketRelayError, 'CALENDAR_INVALID'):
                self.quotes(empty, calendar=days)

    def test_bad_shapes_have_stable_refusals(self):
        for bad in ([], {}, {'use': None}, {'use': {'api': 'daily', 'parameters': None}}):
            with self.subTest(bad=bad), self.assertRaises(MarketRelayError):
                self.quotes(bad)
        for key, value in (('pages', [None]), ('parameters', None), ('query_key', '0' * 64),
                           ('snapshot', {}), ('source_identity', 'ISSUER')):
            bad = deepcopy(self.daily)
            bad['capture'][key] = value
            with self.subTest(key=key), self.assertRaises(MarketRelayError):
                self.quotes(bad)

    def test_tampered_receipt_and_reported_count_refused(self):
        for kind in ('hash', 'clock', 'count', 'after_terminal'):
            bad = deepcopy(self.daily)
            pages = bad['capture']['pages']
            if kind == 'hash':
                pages[0]['transport']['wire_sha256'] = 'not-a-hash'
            elif kind == 'clock':
                pages[1]['captured_at'] = '2026-10-15T01:00:00+00:00'
            elif kind == 'count':
                pages[-1]['table']['reported_count'] = 900
            else:
                pages.insert(0, deepcopy(pages[-1]))
                pages[0]['offset'] = 0
            with self.subTest(kind=kind), self.assertRaises(MarketRelayError):
                self.quotes(bad)

    def test_dates_ranges_and_max_date_do_not_overflow(self):
        for start in ('2026-10-12', '20260230', None, '２０２６１０１２'):
            with self.subTest(start=start), self.assertRaises(MarketRelayError):
                self.days(start=start)
        with self.assertRaisesRegex(MarketRelayError, 'RANGE'):
            self.days(start=END, end=START)
        value = captured('trade_cal', [['SSE', '99991231', 1]],
                         params={'start_date': '99991231', 'end_date': '99991231'})
        self.assertEqual(self.days(value, start='99991231', end='99991231'), ['9999-12-31'])

    def test_closed_calendar_not_missing_and_empty_calendar_refused(self):
        self.assertEqual(self.days(captured('trade_cal', [[r[0], r[1], 0] for r in CALENDAR])), [])
        with self.assertRaisesRegex(MarketRelayError, 'CALENDAR_EMPTY'):
            self.days(captured('trade_cal', [], empty_fields=True))

    def test_registry_is_required_and_source_export_reuses_existing_shape(self):
        with self.assertRaisesRegex(MarketRelayError, 'REGISTRY_REQUIRED'):
            self.days(registry=None)
        sources = source_inputs(self.daily, REGISTRY)
        self.assertEqual(len(sources), 3)
        for source in sources:
            self.assertEqual(SourceInput.from_dict(asdict(source)), source)
            self.assertEqual(source.custody, 'excerpt')
            self.assertIn('projection', json.loads(source.text)['page']['table']['representation'])
        self.assertNotIn('synthetic-key-only', encoded([asdict(s) for s in sources]).decode())


class MarketRelayConsumerTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'calendar_result': captured('trade_cal', CALENDAR),
            'stock_results': {CODE: captured('daily', PRICES)}, 'exchange': 'SSE',
            'start': START, 'end': END, 'declared_entities': [CODE],
            'coverage': 'watchlist', 'horizons': (1, 3, 5)}

    def read(self, **kwargs):
        return read_relay_stock_surface(registry=REGISTRY, **{**self.payload, **kwargs})

    def test_capture_to_calendar_windows_and_raw_lineage(self):
        before = encoded(self.payload)
        out = self.read()
        self.assertEqual(out['scope_count'], 1)
        self.assertEqual(out['entries'][0]['horizons'][1]['change_pct'], '4.00')
        self.assertEqual(out['entries'][0]['horizons'][2]['status'], 'insufficient-calendar-history')
        self.assertEqual(out['adjustment'], 'raw')
        self.assertEqual(out['entries'][0]['source_qualification']['status'], 'raw-input-checked')
        self.assertEqual(len(out['entries'][0]['source_lineage']['view_sha256']), 64)
        self.assertEqual(encoded(self.payload), before)
        self.assertIn('CAPTURE_VIEW_HASH_NOT_SOURCEREF_OR_PUBLISHER_AUTHENTICATION', out['qualification_limits'])

    def test_rejected_absent_and_empty_retained_in_paging_denominator(self):
        self.payload['declared_entities'] = ['600000.SH', CODE, '600520.SH', '600521.SH']
        self.payload['stock_results']['600000.SH'] = {}  # Rejected, not quietly dropped.
        self.payload['stock_results']['600520.SH'] = captured('daily', [], params={'ts_code': '600520.SH'})
        out = self.read(limit=1)
        self.assertEqual(out['scope_count'], 4)
        self.assertEqual(out['source_qualification_counts'], {'supplied': 3, 'accepted': 2, 'rejected': 1, 'not_supplied': 1})
        self.assertEqual(out['entries'][0]['source_qualification']['status'], 'rejected')
        self.assertEqual(out['page']['next_offset'], 1)
        out = self.read(entity_id='600521.SH')
        self.assertEqual(out['entries'][0]['source_qualification']['status'], 'not-supplied')
        self.assertEqual(out['scope_count'], 4)
        self.assertEqual(self.read(entity_id='600520.SH')['entries'][0]['status'], 'no-observation-at-date')

    def test_source_change_changes_version_without_overwriting_input(self):
        first = self.read()
        changed = deepcopy(PRICES)
        changed[-1][-1] = '106'
        second = self.read(stock_results={CODE: captured('daily', changed)})
        self.assertNotEqual(first['source_version'], second['source_version'])
        self.assertEqual(first['universe_version'], second['universe_version'])

    def test_cannot_claim_full_universe_or_cross_exchange(self):
        for kwargs in ({'coverage': 'full-declared-universe'}, {'exchange': 'SZSE'},
                       {'declared_entities': ['000001.SZ']}, {'declared_entities': [CODE, CODE]},
                       {'stock_results': {'000001.SZ': {}}}, {'declared_entities': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketRelayError):
                self.read(**kwargs)

    def test_empty_scope_still_validates_shared_calendar(self):
        out = self.read(declared_entities=[], stock_results={})
        self.assertEqual(out['status'], 'empty-declared-universe')
        self.assertEqual(out['discovery_claim'], 'not-established-by-supplied-universe')
        with self.assertRaises(MarketRelayError):
            self.read(calendar_result={}, declared_entities=[], stock_results={})
        with self.assertRaisesRegex(MarketRelayError, 'END_NOT_MARKET_SESSION'):
            self.read(calendar_result=captured('trade_cal', [[r[0], r[1], 0] for r in CALENDAR]))

    def test_page_and_horizon_rules_are_existing_consumer_rules(self):
        for kwargs in ({'offset': -1}, {'limit': 101}, {'limit': True}, {'horizons': (0,)}, {'horizons': ()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketInputError):
                self.read(**kwargs)

    def run_cli(self, raw, *args):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'market.json'
            path.write_bytes(raw)
            env = {**os.environ, 'PYTHONPATH': str(ROOT_DIR / 'src')}
            env.pop('TUSHARE_PROXY_API_KEY', None)
            result = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.market_relay_cli',
                '--input', str(path), '--registry', str(REGISTRY_PATH), *args],
                cwd=directory, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(path.read_bytes(), raw)
            return result

    def test_new_process_cli_reads_without_credentials_or_input_writes(self):
        raw = encoded({'schema': 'relay-stock-reading-v1', **self.payload})
        result = self.run_cli(raw, '--entity-id', CODE, '--limit', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out['entries'][0]['entity_id'], CODE)
        self.assertEqual(out['page']['selected_count'], 1)
        self.assertEqual(out['entries'][0]['horizons'][1]['change_pct'], '4.00')
        self.assertNotIn('synthetic-key-only', result.stdout)

    def test_cli_rejects_bad_json_schema_and_oversize(self):
        for raw in (b'{"x":1,"x":2}', b'NaN', b'{"n":' + b'1' * 5000 + b'}',
                    b'{}', b'\xff', b'[]', b'x' * (2 * 1024 * 1024 + 1)):
            with self.subTest(size=len(raw)):
                result = self.run_cli(raw)
                self.assertEqual(result.returncode, 2)
                self.assertIn('error', json.loads(result.stderr))
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
