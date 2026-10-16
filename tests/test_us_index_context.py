"""Synthetic US calendars/indices; real capture and CLI, never a live data claim."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.us_index_daily import index_context, observe_us_index

ROOT = Path(__file__).resolve().parents[1]
START, END = '20261012', '20261016'
DAYS = ['2026-10-12', '2026-10-13', '2026-10-15', '2026-10-16']
# The synthetic Wednesday is closed; these are not real exchange-calendar facts.
CALENDAR = [[f'202610{n}', int(n != 14)] for n in range(12, 17)]
ROWS = [{'trade_date': day, 'close': str(value)} for day, value in zip(DAYS, (100, 120, 144, 144))]


def source(api, rows, *, code='SPX', fields=None, params=None, max_pages=100, empty_fields=False):
    # Defer integration imports so the pure observations can run in a small dependency subset.
    from test_market_relay_inputs import REGISTRY, SyntheticOpener
    from decision_kernel.application.research_sources import capture_plan
    from decision_kernel.infrastructure.relay_http import RelayHTTP
    from decision_kernel.infrastructure.relay_retry import RetryPolicy
    fields = (['cal_date', 'is_open'] if api == 'us_tradecal' else ['ts_code', 'trade_date', 'close']) if fields is None else fields
    parameters = {'start_date': START, 'end_date': END, 'limit': '2', 'fields': ','.join(fields),
                  **({} if api == 'us_tradecal' else {'ts_code': code}), **(params or {})}
    request = {'api': api, 'stage': 'baseline', 'purpose': 'synthetic US index reading test',
               'parameters': parameters}
    opener = SyntheticOpener(api, fields, rows, empty_fields)
    client = RelayHTTP(opener=opener, credential='synthetic-key-only')
    result = list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-us-index',
        'requests': [request]}, REGISTRY, client=client, public_rights_confirmed=True,
        policy=RetryPolicy(max_attempts=1, max_wait_seconds=0), max_pages=max_pages,
        clock=lambda: '2026-10-16T02:00:00+00:00'))[0]
    assert client.requests == len(opener.calls)
    return result


def index_rows(code='SPX'):
    return [[code, row['trade_date'].replace('-', ''), row['close']] for row in ROWS]


class IndexObservationTests(unittest.TestCase):
    def observe(self, **kwargs):
        return observe_us_index(**{'entity_id': 'SPX', 'series': ROWS, 'as_of': DAYS[-1],
                                   'market_sessions': DAYS, 'horizons': (1, 3, 5), **kwargs})

    def test_exact_calendar_baselines_and_units(self):
        out = self.observe()
        one, three, five = out['horizons']
        self.assertEqual(out['status'], 'partial-history')
        self.assertEqual(Decimal(one['change_pct']), 0)
        self.assertEqual(Decimal(three['change_pct']), 44)
        self.assertEqual((three['baseline_date'], three['baseline_level'], three['end_level']), (DAYS[0], '100', '144'))
        self.assertEqual(five['status'], 'insufficient-calendar-history')
        self.assertEqual(out['unit'], 'index-points')
        self.assertIsNone(out['currency'])
        self.assertNotIn('adjustment', out)
        self.assertIn('not-certified-total-return', out['return_semantics'])

    def test_missing_intermediate_date_does_not_shrink_window(self):
        out = self.observe(series=[ROWS[0], ROWS[2], ROWS[3]], horizons=(1, 3))
        self.assertEqual(out['horizons'][0]['status'], 'observed')
        self.assertEqual(out['horizons'][1]['missing_dates'], [DAYS[1]])
        self.assertNotIn('change_pct', out['horizons'][1])
        self.assertEqual(out['expected_sessions'], 4)
        self.assertEqual(out['observed_sessions'], 3)

    def test_missing_end_empty_and_short_calendar(self):
        out = self.observe(series=ROWS[:-1], horizons=(1,))
        self.assertEqual(out['status'], 'no-observation-at-date')
        self.assertEqual(out['last_observed_date'], DAYS[-2])
        self.assertEqual(out['horizons'][0]['missing_dates'], [DAYS[-1]])
        empty = self.observe(series=[], horizons=(1,))
        self.assertIsNone(empty['last_observed_date'])
        self.assertEqual(empty['observed_sessions'], 0)
        single = self.observe(series=[ROWS[-1]], market_sessions=[DAYS[-1]], horizons=(1,))
        self.assertEqual(single['status'], 'no-usable-window')

    def test_5_20_60_use_n_plus_one_explicit_sessions(self):
        days = [(date(2026, 1, 1) + timedelta(days=2 * n)).isoformat() for n in range(61)]
        rows = [{'trade_date': day, 'close': str(n + 1)} for n, day in enumerate(days)]
        out = self.observe(series=rows, market_sessions=days, as_of=days[-1], horizons=(5, 20, 60))
        self.assertEqual(out['status'], 'observed')
        self.assertEqual([w['baseline_date'] for w in out['horizons']], [days[55], days[40], days[0]])
        self.assertEqual(Decimal(out['horizons'][2]['change_pct']), 6000)
        gap = self.observe(series=rows[:10] + rows[11:], market_sessions=days,
                           as_of=days[-1], horizons=(5, 20, 60))
        self.assertEqual([w['status'] for w in gap['horizons']], ['observed', 'observed', 'missing-market-sessions'])

    def test_context_validation_even_without_rows(self):
        for horizons in ([], (), (True,), (0,), (-1,), (2521,), (1, 1), tuple(range(1, 14)), ('5',)):
            with self.subTest(horizons=horizons), self.assertRaises(MarketInputError):
                self.observe(series=[], horizons=horizons)
        for days in ('2026-10-16', [], list(reversed(DAYS)), DAYS + [DAYS[-1]], ['20261016'], [True]):
            with self.subTest(days=days), self.assertRaises(MarketInputError):
                self.observe(series=[], market_sessions=days)
        with self.assertRaises(MarketInputError):
            self.observe(as_of='2026-10-15')
        with self.assertRaises(MarketInputError):
            self.observe(entity_id=' SPX')

    def test_reject_bad_duplicate_outside_and_closed_date_rows(self):
        for value in (True, None, [], 'NaN', 'Infinity', '0', '-1', '1e100', '1e-100', '1' * 129):
            with self.subTest(value=value), self.assertRaises(MarketInputError):
                self.observe(series=[{'trade_date': DAYS[-1], 'close': value}])
        for rows in ([ROWS[0], ROWS[0]], [{'trade_date': '2026-10-14', 'close': '100'}],
                     [{'trade_date': '2026-10-17', 'close': '100'}],
                     [{'trade_date': '2026-10-11', 'close': '100'}],
                     [{'trade_date': DAYS[-1], 'close': '100', 'extra': 'x'}], 'bad'):
            with self.subTest(rows=rows), self.assertRaises(MarketInputError):
                self.observe(series=rows)

    def test_decimal_caller_context_and_input_immutability(self):
        rows = deepcopy(list(reversed(ROWS))); before = json.dumps(rows, sort_keys=True)
        rows[0]['close'] = '145'
        before = json.dumps(rows, sort_keys=True)
        expected = self.observe(series=rows)
        with localcontext() as ctx:
            ctx.prec = 2; ctx.traps[Inexact] = True
            self.assertEqual(self.observe(series=rows), expected)
        self.assertEqual(json.dumps(rows, sort_keys=True), before)

    def test_boundary_dates_do_not_generate_future_calendar(self):
        days = ['9999-12-30', '9999-12-31']
        out = self.observe(series=[{'trade_date': days[0], 'close': '10'},
                                   {'trade_date': days[1], 'close': '11'}],
                           market_sessions=days, as_of=days[-1], horizons=(1,))
        self.assertEqual(Decimal(out['horizons'][0]['change_pct']), 10)


class USIndexSourceTests(unittest.TestCase):
    def setUp(self):
        from test_market_relay_inputs import REGISTRY
        from decision_kernel.application.global_index_inputs import read_us_index_context, qualified_us_trade_calendar
        self.registry, self.reader, self.calendar_reader = REGISTRY, read_us_index_context, qualified_us_trade_calendar
        self.payload = {'calendar_result': source('us_tradecal', CALENDAR),
                        'index_results': {'SPX': source('index_global', index_rows())},
                        'declared_indices': ['SPX'], 'start': START, 'end': END, 'horizons': (1, 3, 5)}

    def read(self, **kwargs):
        return self.reader(**{**self.payload, **kwargs}, registry=self.registry)

    def test_current_registry_and_real_capture_lineage(self):
        for api in ('us_tradecal', 'index_global'):
            self.assertTrue(self.registry.lookup(api)['registered_reader_candidate'])
        out = self.read()
        self.assertEqual(out['market_sessions'], DAYS)
        self.assertEqual(out['scope_count'], 1)
        self.assertEqual(out['observed_count'], 1)
        self.assertEqual(out['calendar_lineage']['api'], 'us_tradecal')
        entry = out['entries'][0]
        self.assertEqual(entry['source_qualification'], {'status': 'checked', 'row_count': 4})
        self.assertEqual([p['offset'] for p in entry['source_lineage']['pages']], [0, 2, 4])
        self.assertEqual(Decimal(entry['observation']['horizons'][1]['change_pct']), 44)
        self.assertIsNone(out['currency'])

    def test_shared_us_calendar_must_be_complete_unfiltered_and_valid(self):
        bad_rows = [[], CALENDAR[:-1], CALENDAR[:2] + CALENDAR[3:], CALENDAR + [CALENDAR[0]],
                    [['20261011', 1]] + CALENDAR, [['20260230', 1]] + CALENDAR,
                    [[day, True] for day, _ in CALENDAR], [[day, 1.0] for day, _ in CALENDAR]]
        for rows in bad_rows:
            with self.subTest(rows=rows), self.assertRaises(MarketInputError):
                self.read(calendar_result=source('us_tradecal', rows))
        for params in ({'is_open': '1'}, {'offset': '2'}, {'exchange': 'SSE'}, {'end_date': '20261017'}):
            with self.subTest(params=params), self.assertRaises(MarketInputError):
                self.read(calendar_result=source('us_tradecal', CALENDAR, params=params))
        closed = deepcopy(CALENDAR); closed[-1][1] = 0
        with self.assertRaisesRegex(MarketInputError, 'END_NOT_MARKET_SESSION'):
            self.read(calendar_result=source('us_tradecal', closed))
        with self.assertRaises(MarketInputError):
            self.read(calendar_result=source('us_tradecal', CALENDAR, max_pages=1))

    def test_us_calendar_does_not_require_or_accept_domestic_calendar(self):
        from test_market_relay_inputs import captured, CALENDAR as A_CALENDAR
        self.assertEqual(self.calendar_reader(self.payload['calendar_result'], registry=self.registry,
                                              start=START, end=END), DAYS)
        with self.assertRaises(MarketInputError):
            self.read(calendar_result=captured('trade_cal', A_CALENDAR))
        with patch('decision_kernel.application.market_security_inputs.qualified_security_master', side_effect=AssertionError), \
             patch('decision_kernel.application.market_group_inputs.qualified_ths_catalog', side_effect=AssertionError):
            self.assertEqual(self.read()['observed_count'], 1)

    def test_one_bad_index_never_drops_it_or_blocks_other_indices(self):
        good = self.payload['index_results']['SPX']
        for rows in (index_rows('IXIC'), index_rows('DJI') * 2, [['DJI', '20261014', '100']],
                     [['DJI', '20261017', '100']], [['DJI', '20261016', '0']],
                     [['DJI', '20261016', True]], [['DJI', '20261016', 'NaN']]):
            with self.subTest(rows=rows):
                out = self.read(declared_indices=['SPX', 'DJI', 'RUT'],
                    index_results={'SPX': good, 'DJI': source('index_global', rows, code='DJI')})
                self.assertEqual(out['scope_count'], 3)
                self.assertEqual([e['entity_id'] for e in out['entries']], ['DJI', 'RUT', 'SPX'])
                self.assertEqual(out['observed_count'], 1)
                self.assertEqual(out['source_qualification_counts'],
                                 {'checked': 1, 'checked-empty': 0, 'rejected': 1, 'not-supplied': 1})
                self.assertIsNone(out['entries'][0]['observation'])
                self.assertIsNone(out['entries'][1]['observation'])

    def test_empty_capture_not_missing_and_gaps_are_per_window(self):
        out = self.read(declared_indices=['SPX', 'DJI', 'RUT'], index_results={
            'SPX': source('index_global', [index_rows()[i] for i in (0, 2, 3)]),
            'DJI': source('index_global', [], code='DJI', empty_fields=True)})
        self.assertEqual(out['entries'][0]['source_qualification']['status'], 'checked-empty')
        self.assertEqual(out['entries'][0]['observation']['status'], 'no-observation-at-date')
        self.assertEqual(out['entries'][1]['source_qualification']['status'], 'not-supplied')
        windows = out['entries'][2]['observation']['horizons']
        self.assertEqual(windows[0]['status'], 'observed')
        self.assertEqual(windows[1]['missing_dates'], [DAYS[1]])
        self.assertEqual(out['scope_count'], 3)

    def test_tampered_incomplete_extra_filter_and_missing_fields_rejected(self):
        variants = []
        for key, value in (('complete', False), ('query_key', '0' * 64), ('snapshot', {})):
            bad = deepcopy(self.payload['index_results']['SPX']); bad['capture'][key] = value
            variants.append(bad)
        variants.append(source('index_global', index_rows(), max_pages=1))
        variants.append(source('index_global', index_rows(), params={'trade_date': END}))
        variants.append(source('index_global', [[row[0], row[1]] for row in index_rows()], fields=['ts_code', 'trade_date']))
        for bad in variants:
            with self.subTest(capture=bad['capture'].get('complete')):
                out = self.read(index_results={'SPX': bad})
                self.assertEqual(out['source_qualification_counts']['rejected'], 1)
                self.assertEqual(out['observed_count'], 0)

    def test_scope_horizons_and_page_errors_even_when_scope_empty(self):
        for scope in (['HSI'], ['600519.SH'], ['SPX', 'SPX'], ['SPX', []], 'SPX'):
            with self.subTest(scope=scope), self.assertRaises(MarketInputError):
                self.read(declared_indices=scope)
        with self.assertRaises(MarketInputError):
            self.read(declared_indices=[], index_results=self.payload['index_results'])
        for page in ({'offset': True}, {'offset': -1}, {'limit': False}, {'limit': 0}, {'limit': 101}, {'entity_id': 'DJI'}):
            with self.subTest(page=page), self.assertRaises(MarketInputError):
                self.read(**page)
        with self.assertRaises(MarketInputError):
            self.read(declared_indices=[], index_results={}, horizons=(True,))
        out = self.read(declared_indices=[], index_results={})
        self.assertEqual(out['scope_count'], 0)
        self.assertEqual(out['entries'], [])
        with self.assertRaises(MarketInputError):
            self.read(start='19000101')

    def test_pages_keep_complete_denominator_stable_version_and_inputs(self):
        self.payload['declared_indices'] = ['SPX', 'RUT', 'DJI', 'IXIC']
        before = json.dumps(self.payload, sort_keys=True)
        out = self.read()
        pages = [self.read(offset=i, limit=1) for i in range(4)]
        self.assertEqual([p['entries'][0] for p in pages], out['entries'])
        self.assertEqual({p['view_version'] for p in pages}, {out['view_version']})
        self.assertTrue(all(p['scope_count'] == 4 and p['observed_count'] == 1 for p in pages))
        filtered = self.read(entity_id='SPX')
        self.assertEqual(filtered['scope_count'], 4)
        self.assertEqual(filtered['matched_count'], 1)
        self.assertEqual(filtered['view_version'], out['view_version'])
        self.assertEqual(self.read(offset=100)['entries'], [])
        self.assertIsNone(pages[-1]['pagination']['next_offset'])
        self.assertEqual(json.dumps(self.payload, sort_keys=True), before)

    def test_field_order_and_extra_metadata_not_promoted(self):
        fields = ['close', 'trade_date', 'ts_code', 'vol']
        rows = [[row[2], row[1], row[0], 123] for row in index_rows()]
        out = self.read(index_results={'SPX': source('index_global', rows, fields=fields)})
        self.assertEqual(out['entries'][0]['observation']['horizons'], self.read()['entries'][0]['observation']['horizons'])
        self.assertNotIn('vol', out['entries'][0]['observation'])
        self.assertNotIn('amount', out['entries'][0]['observation'])

    def test_cli_outside_repo_reads_without_source_or_custody_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            path.write_text(json.dumps(self.payload), encoding='utf-8')
            before = path.read_bytes()
            env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
            done = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.global_index_cli',
                '--input', str(path), '--registry', str(ROOT / 'docs/sources/relay-registry.json'),
                '--limit', '1'], cwd=directory, env=env, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr.decode())
            out = json.loads(done.stdout)
            self.assertEqual(out['schema'], 'relay-us-index-context-v1')
            self.assertEqual(out['observed_count'], 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['input.json'])

    def test_cli_rejects_bad_json_shape_and_horizons_without_echo(self):
        bodies = ['{"secret":"DO-NOT-ECHO","secret":"duplicate"}', '{"x":NaN}',
                  json.dumps({**self.payload, 'unexpected': 'DO-NOT-ECHO'}),
                  json.dumps({**self.payload, 'horizons': 'DO-NOT-ECHO'}),
                  json.dumps({**self.payload, 'horizons': [True]})]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'input.json'
            for raw in bodies:
                path.write_text(raw, encoding='utf-8')
                done = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.global_index_cli',
                    '--input', str(path), '--registry', str(ROOT / 'docs/sources/relay-registry.json')],
                    cwd=directory, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                    capture_output=True, timeout=30)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(done.stdout, b'')
                self.assertNotIn(b'DO-NOT-ECHO', done.stderr)
                self.assertNotIn(b'Traceback', done.stderr)
                self.assertEqual(json.loads(done.stderr)['reason_code'], 'GLOBAL_INDEX_INPUT_REJECTED')


if __name__ == '__main__':
    unittest.main()
