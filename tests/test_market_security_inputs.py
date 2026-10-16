"""Synthetic reference/factor data through real capture, qualification and CLI.

Prices, names, statuses and the calendar are test inputs, not market evidence.
"""
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal, Inexact, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from decision_kernel.application.market_security_inputs import (
    MASTER_FIELDS, qualified_security_master, qualified_adj_factors, read_security_stock_surface)
from decision_kernel.application.market_relay_inputs import MarketRelayError, qualified_trade_calendar
from decision_kernel.application.research_sources import capture_plan, source_inputs
from decision_kernel.domain.records import SourceInput, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.market_daily import MarketInputError
from test_market_relay_inputs import (SyntheticOpener, REGISTRY, REGISTRY_PATH,
                                     ROOT_DIR, START, END, CODE, CALENDAR, captured)

BASIC_FIELDS = ['ts_code', 'symbol', 'name', 'exchange', 'curr_type', 'list_status', 'list_date', 'delist_date']
ADJ_FIELDS = ['ts_code', 'trade_date', 'adj_factor']
DAYS = ['2026-10-12', '2026-10-13', '2026-10-15', '2026-10-16']
PRICES = [[CODE, d.replace('-', ''), v] for d, v in zip(DAYS, ('100', '100', '50', '50'))]
FACTORS = [[CODE, d.replace('-', ''), v] for d, v in zip(DAYS, ('1', '1', '2', '2'))]


def basic_row(code=CODE, *, status='L', listed='20000101', delisted=None):
    return [code, code[:6], '合成证券', 'SSE' if code.endswith('.SH') else 'SZSE', 'CNY', status, listed, delisted]


def result_for(api, rows, *, fields=None, params=None, status='L', exchange='SSE', code=CODE,
               limit=2, max_pages=100, empty_fields=False):
    fields = (BASIC_FIELDS if api == 'stock_basic' else ADJ_FIELDS) if fields is None else fields
    scope = {'exchange': exchange, 'list_status': status} if api == 'stock_basic' else {
        'ts_code': code, 'start_date': START, 'end_date': END}
    request = {'api': api, 'stage': 'baseline', 'purpose': 'synthetic security/factor engineering test',
               'parameters': {**scope, 'fields': ','.join(fields), 'limit': str(limit), **(params or {})}}
    opener = SyntheticOpener(api, fields, rows, empty_fields)
    client = RelayHTTP(opener=opener, credential='synthetic-key-only')
    result = list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-security',
        'requests': [request]}, REGISTRY, client=client, public_rights_confirmed=True,
        policy=RetryPolicy(max_attempts=1, max_wait_seconds=0), max_pages=max_pages,
        clock=lambda: '2026-10-16T02:00:00+00:00'))[0]
    assert client.requests == len(opener.calls)
    return result


class SecurityMasterTests(unittest.TestCase):
    def master(self, results=None, **kwargs):
        return qualified_security_master({'L': result_for('stock_basic', [basic_row()])}
            if results is None else results, **{'registry': REGISTRY, 'exchange': 'SSE', **kwargs})

    def test_exact_reference_and_missing_statuses(self):
        out = self.master()
        self.assertEqual(set(BASIC_FIELDS), MASTER_FIELDS)
        self.assertEqual(out['securities'][CODE]['list_date'], '2000-01-01')
        self.assertEqual(out['scope_count'], 1)
        self.assertEqual(out['supplied_statuses'], ['L'])
        self.assertEqual(out['not_supplied_statuses'], ['D', 'P', 'G'])
        self.assertIn('not-issuer', out['identity_semantics'])
        self.assertIn('not-atomic', out['time_semantics'])

    def test_all_supplied_statuses_not_full_historical_market(self):
        rows = {'L': basic_row(), 'D': basic_row('600001.SH', status='D', delisted='20261015'),
                'P': basic_row('600002.SH', status='P'),
                'G': basic_row('600003.SH', status='G', listed=None)}
        out = self.master({s: result_for('stock_basic', [r], status=s) for s, r in rows.items()})
        self.assertEqual(out['scope_count'], 4)
        self.assertEqual(out['not_supplied_statuses'], [])
        self.assertIn('not-full-market', out['scope_semantics'])
        self.assertIsNone(out['securities']['600003.SH']['list_date'])

    def test_multiple_pages_preserve_every_reference(self):
        rows = [basic_row(f'60000{i}.SH') for i in range(5)]
        out = self.master({'L': result_for('stock_basic', rows)})
        self.assertEqual(out['scope_count'], 5)
        self.assertEqual([p['offset'] for p in out['lineage']['L']['pages']], [0, 2, 4])

    def test_partial_page_and_unknown_filters_refused(self):
        for params in ({'ts_code': CODE}, {'market': '主板'}, {'offset': '2'}):
            with self.subTest(params=params), self.assertRaises(MarketRelayError):
                self.master({'L': result_for('stock_basic', [basic_row()], params=params)})
        with self.assertRaises(MarketRelayError):
            self.master({'L': result_for('stock_basic', [basic_row(), basic_row('600001.SH')], max_pages=1)})

    def test_invalid_identity_name_currency_and_dates(self):
        mutations = [(0, 'not-a-stock'), (0, '000001.SZ'), (1, '600001'), (2, ' 证券'),
                     (2, '\n'), (2, None), (3, 'SZSE'), (4, 'USD'), (5, 'D'),
                     (6, None), (6, '20260230'), (6, '2000-01-01'), (7, '20250101')]
        for index, value in mutations:
            row = basic_row(); row[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(MarketRelayError):
                self.master({'L': result_for('stock_basic', [row])})
        for listed, delisted in [('20000101', None), ('20260102', '20260101')]:
            with self.subTest(delisted=delisted), self.assertRaises(MarketRelayError):
                self.master({'D': result_for('stock_basic', [basic_row(status='D', listed=listed,
                    delisted=delisted)], status='D')})

    def test_duplicate_ids_within_or_across_statuses_refused(self):
        for results in ({'L': result_for('stock_basic', [basic_row()] * 2)},
                        {'L': result_for('stock_basic', [basic_row()]),
                         'P': result_for('stock_basic', [basic_row(status='P')], status='P')}):
            with self.subTest(keys=list(results)), self.assertRaises(MarketRelayError):
                self.master(results)

    def test_empty_slice_not_missing_slice_and_reordered_fields(self):
        out = self.master({'L': result_for('stock_basic', [], empty_fields=True)})
        self.assertEqual(out['status_counts'], {'L': 0})
        self.assertEqual(out['not_supplied_statuses'], ['D', 'P', 'G'])
        row = basic_row()
        out = self.master({'L': result_for('stock_basic', [list(reversed(row))], fields=list(reversed(BASIC_FIELDS)))})
        self.assertEqual(out['securities'][CODE]['name'], row[2])

    def test_bad_shape_registry_and_exchange_refused_even_empty(self):
        for results in ({}, [], {'X': {}}, {'L': None}, {'L': {}}):
            with self.subTest(results=results), self.assertRaises(MarketRelayError):
                self.master(results)
        for kwargs in ({'exchange': []}, {'exchange': 'BSE'}, {'registry': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketRelayError):
                self.master(**kwargs)

    def test_no_private_or_unrequested_metadata_in_reference(self):
        row = basic_row() + ['unused']
        # An extra requested nonsensitive field may be retained in the source,
        # but never silently promoted into the canonical security reference.
        out = self.master({'L': result_for('stock_basic', [row], fields=BASIC_FIELDS + ['market'])})
        self.assertNotIn('market', out['securities'][CODE])
        self.assertNotIn('issuer_id', out['securities'][CODE])


class FactorTests(unittest.TestCase):
    def factors(self, result=None, **kwargs):
        return qualified_adj_factors(result_for('adj_factor', FACTORS) if result is None else result,
            **{'registry': REGISTRY, 'ts_code': CODE, 'start': START, 'end': END, 'calendar': DAYS, **kwargs})

    def test_factors_preserve_dates_and_values_without_event_inference(self):
        rows = deepcopy(list(reversed(FACTORS)))
        rows[-1][-1] = '3'  # A decreasing factor is not intrinsically invalid.
        before = encoded(rows)
        out = self.factors(result_for('adj_factor', rows))
        self.assertEqual([r['trade_date'] for r in out], DAYS)
        self.assertEqual(out[0]['adj_factor'], '3')
        self.assertEqual(encoded(rows), before)

    def test_invalid_factor_values_and_wrong_scope(self):
        for value in (0, -1, True, None, 'NaN', 'Infinity', '1e100', '1e-100', '1' * 129):
            with self.subTest(value=value), self.assertRaises(MarketRelayError):
                self.factors(result_for('adj_factor', [[CODE, START, value]]))
        for row in (['000001.SZ', START, '1'], [CODE, '20261014', '1'], [CODE, '20261017', '1']):
            with self.subTest(row=row), self.assertRaises(MarketRelayError):
                self.factors(result_for('adj_factor', [row]))
        with self.assertRaises(MarketRelayError):
            self.factors(result_for('adj_factor', [FACTORS[0]] * 2))

    def test_factor_empty_is_not_constant_one(self):
        self.assertEqual(self.factors(result_for('adj_factor', [], empty_fields=True)), [])
        self.assertEqual(len(self.factors(result_for('adj_factor', FACTORS, empty_fields=True))), 4)

    def test_factor_request_must_bind_whole_exact_interval(self):
        for params in ({'offset': '2'}, {'adj': 'qfq'}, {'trade_date': START}, {'end_date': '20261017'}):
            with self.subTest(params=params), self.assertRaises(MarketRelayError):
                self.factors(result_for('adj_factor', FACTORS, params=params))
        for kwargs in ({'calendar': ['2026-99-99']}, {'calendar': [DAYS[0], DAYS[0]]}, {'ts_code': '600519.sh'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketRelayError):
                self.factors(result_for('adj_factor', []), **kwargs)

    def test_receipt_scope_and_completion_tampering_refused(self):
        for key, value in (('complete', False), ('query_key', '0' * 64), ('snapshot', {})):
            result = result_for('adj_factor', FACTORS); result['capture'][key] = value
            with self.subTest(key=key), self.assertRaises(MarketRelayError):
                self.factors(result)


class SecurityReadingTests(unittest.TestCase):
    def setUp(self):
        self.payload = {'calendar_result': captured('trade_cal', CALENDAR),
            'master_results': {'L': result_for('stock_basic', [basic_row()])},
            'stock_results': {CODE: captured('daily', PRICES)},
            'factor_results': {CODE: result_for('adj_factor', FACTORS)},
            'exchange': 'SSE', 'start': START, 'end': END, 'horizons': (1, 3, 5)}

    def read(self, **kwargs):
        return read_security_stock_surface(**{**self.payload, **kwargs}, registry=REGISTRY)

    def test_split_like_change_raw_minus_half_adjusted_flat(self):
        before = encoded(self.payload)
        out = self.read()
        raw, adj = out['raw']['entries'][0], out['qfq']['entries'][0]
        self.assertEqual(Decimal(raw['horizons'][1]['change_pct']), Decimal(-50))
        self.assertEqual(Decimal(adj['horizons'][1]['change_pct']), Decimal(0))
        self.assertEqual(adj['qualification']['adjustment']['anchor_factor'], '2')
        self.assertEqual(adj['qualification']['adjustment']['factor_change_dates'], ['2026-10-15'])
        self.assertEqual(adj['horizons'][2]['status'], 'insufficient-calendar-history')
        self.assertEqual(encoded(self.payload), before)
        self.assertIn('NOT_IDENTIFIED_CORPORATE_ACTION', ' '.join(out['qualification_limits']))

    def test_missing_anchor_preserves_raw_and_does_not_use_last_factor(self):
        out = self.read(factor_results={CODE: result_for('adj_factor', FACTORS[:-1])})
        self.assertEqual(out['raw']['observed_count'], 1)
        self.assertEqual(out['qfq']['observed_count'], 0)
        q = out['qfq']['entries'][0]['qualification']['adjustment']
        self.assertEqual(q['status'], 'missing-anchor-factor')
        self.assertIsNone(q['anchor_factor'])

    def test_factor_gap_only_blocks_windows_crossing_it(self):
        out = self.read(factor_results={CODE: result_for('adj_factor', FACTORS[:1] + FACTORS[2:])})
        entry = out['qfq']['entries'][0]
        self.assertEqual(entry['horizons'][0]['status'], 'observed')
        self.assertEqual(entry['horizons'][1]['missing_dates'], ['2026-10-13'])
        self.assertEqual(entry['qualification']['adjustment']['price_dates_without_factor'], ['2026-10-13'])
        self.assertEqual(out['raw']['horizon_counts'][1]['observed_count'], 1)

    def test_factor_without_price_never_fills_halted_price(self):
        out = self.read(stock_results={CODE: captured('daily', PRICES[:1] + PRICES[2:])})
        for key in ('raw', 'qfq'):
            self.assertEqual(out[key]['entries'][0]['horizons'][1]['missing_dates'], ['2026-10-13'])
        self.assertEqual(out['qfq']['entries'][0]['qualification']['adjustment']['factor_dates_without_price'], ['2026-10-13'])

    def test_empty_rejected_and_absent_factor_remain_distinct(self):
        for mapping, factor_status, adj_status in (({}, 'not-supplied', 'inputs-unavailable'),
                ({CODE: {}}, 'rejected', 'inputs-unavailable'),
                ({CODE: result_for('adj_factor', [], empty_fields=True)}, 'checked', 'missing-anchor-factor')):
            with self.subTest(status=factor_status):
                out = self.read(factor_results=mapping)
                q = out['qfq']['entries'][0]['qualification']
                self.assertEqual(q['factor']['status'], factor_status)
                self.assertEqual(q['adjustment']['status'], adj_status)
                self.assertEqual(out['raw']['observed_count'], 1)
                self.assertEqual(out['qfq']['scope_count'], 1)

    def test_listing_interval_denies_prelisting_and_delisting_day_prices(self):
        for status, listed, delisted in (('L', '20261013', None), ('D', '20000101', END), ('G', None, None)):
            with self.subTest(status=status):
                master = {status: result_for('stock_basic', [basic_row(status=status, listed=listed, delisted=delisted)], status=status)}
                out = self.read(master_results=master)
                entry = out['raw']['entries'][0]
                self.assertEqual(entry['qualification']['price']['reason_code'], 'MARKET_RELAY_PRICE_OUTSIDE_LISTING')
                self.assertEqual(out['raw']['scope_count'], 1)
                self.assertEqual(out['raw']['observed_count'], 0)

    def test_ipo_and_delisted_rows_not_dropped_or_filled(self):
        for status, listed, delisted, prices in (('L', '20261015', None, PRICES[2:]),
                ('D', '20000101', '20261015', PRICES[:2])):
            with self.subTest(status=status):
                master = {status: result_for('stock_basic', [basic_row(status=status, listed=listed, delisted=delisted)], status=status)}
                out = self.read(master_results=master, stock_results={CODE: captured('daily', prices)})
                self.assertEqual(out['raw']['scope_count'], 1)
                self.assertEqual(out['raw']['entries'][0]['qualification']['price']['status'], 'checked')
                self.assertEqual(out['raw']['entries'][0]['horizons'][1]['status'], 'missing-market-sessions')

    def test_paging_keeps_master_denominator_not_received_price_count(self):
        rows = [basic_row(), basic_row('600000.SH'), basic_row('600001.SH')]
        out = self.read(master_results={'L': result_for('stock_basic', rows)}, limit=1)
        self.assertEqual(out['scope_count'], 3)
        for key in ('raw', 'qfq'):
            self.assertEqual(out[key]['scope_count'], 3)
            self.assertEqual(out[key]['entries'][0]['entity_id'], '600000.SH')
            self.assertEqual(out[key]['page']['next_offset'], 1)
        self.assertEqual(out['qualification_counts']['price'], {'checked': 1, 'rejected': 0, 'not-supplied': 2})
        self.assertEqual(out['master']['status_counts']['L'], 3)

    def test_master_and_factor_revisions_have_separate_versions(self):
        original = self.read()
        rows = deepcopy(FACTORS); rows[-1][-1] = '4'
        changed = self.read(factor_results={CODE: result_for('adj_factor', rows)})
        self.assertEqual(original['raw']['source_version'], changed['raw']['source_version'])
        self.assertNotEqual(original['qfq']['source_version'], changed['qfq']['source_version'])
        self.assertEqual(original['master']['version'], changed['master']['version'])
        reference = basic_row(); reference[2] = '合成更名'
        renamed = self.read(master_results={'L': result_for('stock_basic', [reference])})
        self.assertNotEqual(renamed['raw']['universe_version'], original['raw']['universe_version'])

    def test_extreme_adjusted_range_rejection_preserves_raw(self):
        factors = [[CODE, d.replace('-', ''), '1e99' if d != DAYS[-1] else '1e-99'] for d in DAYS]
        out = self.read(factor_results={CODE: result_for('adj_factor', factors)})
        self.assertEqual(out['raw']['observed_count'], 1)
        self.assertEqual(out['qfq']['entries'][0]['qualification']['adjustment']['reason_code'], 'MARKET_RELAY_ADJUSTED_CLOSE_RANGE')

    def test_parent_decimal_context_does_not_change_calculation(self):
        expected = self.read()
        with localcontext() as ctx:
            ctx.prec = 2; ctx.traps[Inexact] = True
            self.assertEqual(self.read(), expected)

    def test_full_five_twenty_sixty_session_windows_and_local_gap(self):
        from datetime import date, timedelta
        dates = [(date(2026, 7, 1) + timedelta(days=i)).strftime('%Y%m%d') for i in range(61)]
        scope = {'start_date': dates[0], 'end_date': dates[-1]}
        # All-open synthetic calendar; never used as an exchange calendar.
        cal = captured('trade_cal', [['SSE', d, 1] for d in dates], params=scope, limit=20)
        prices = [[CODE, d, '50' if i == 60 else '100'] for i, d in enumerate(dates)]
        factors = [[CODE, d, '2' if i == 60 else '1'] for i, d in enumerate(dates)]
        args = {'calendar_result': cal, 'start': dates[0], 'end': dates[-1], 'horizons': (5, 20, 60),
                'stock_results': {CODE: captured('daily', prices, params=scope, limit=20)},
                'factor_results': {CODE: result_for('adj_factor', factors, params=scope, limit=20)}}
        out = self.read(**args)
        self.assertEqual([Decimal(h['change_pct']) for h in out['qfq']['entries'][0]['horizons']], [0, 0, 0])
        args['factor_results'] = {CODE: result_for('adj_factor', factors[1:], params=scope, limit=20)}
        out = self.read(**args)
        self.assertEqual([h['observed_count'] for h in out['qfq']['horizon_counts']], [1, 1, 0])
        self.assertEqual([h['observed_count'] for h in out['raw']['horizon_counts']], [1, 1, 1])

    def test_szse_reference_calendar_and_prices_are_positive_path(self):
        code = '000001.SZ'
        out = self.read(exchange='SZSE',
            master_results={'L': result_for('stock_basic', [basic_row(code)], exchange='SZSE')},
            calendar_result=captured('trade_cal', [['SZSE', r[1], r[2]] for r in CALENDAR], params={'exchange': 'SZSE'}),
            stock_results={code: captured('daily', [[code, r[1], r[2]] for r in PRICES], params={'ts_code': code})},
            factor_results={code: result_for('adj_factor', [[code, r[1], r[2]] for r in FACTORS], code=code)})
        self.assertEqual(out['raw']['entries'][0]['security_reference']['exchange'], 'SZSE')
        self.assertEqual(Decimal(out['qfq']['entries'][0]['horizons'][1]['change_pct']), 0)

    def test_bad_factor_is_local_to_one_stock_and_counts_survive_paging(self):
        other = '600000.SH'
        out = self.read(master_results={'L': result_for('stock_basic', [basic_row(), basic_row(other)])},
            stock_results={**self.payload['stock_results'], other: captured('daily',
                [[other, r[1], r[2]] for r in PRICES], params={'ts_code': other})},
            factor_results={**self.payload['factor_results'], other: {}}, entity_id=CODE)
        self.assertEqual(out['raw']['observed_count'], 2)
        self.assertEqual(out['qfq']['observed_count'], 1)
        self.assertEqual(out['qualification_counts']['factor'], {'checked': 1, 'rejected': 1, 'not-supplied': 0})
        self.assertEqual(out['qualification_counts']['adjustment']['computed'], 1)
        self.assertEqual(out['qfq']['scope_count'], 2)
        self.assertEqual(out['qfq']['page']['selected_count'], 1)

    def test_empty_daily_result_does_not_become_a_synthetic_price(self):
        out = self.read(stock_results={CODE: captured('daily', [], empty_fields=True)})
        self.assertEqual(out['raw']['received_count'], 1)
        self.assertEqual(out['qfq']['observed_count'], 0)
        self.assertEqual(out['qfq']['entries'][0]['qualification']['adjustment']['status'], 'no-prices')

    def test_source_projection_export_still_uses_existing_custody_contract(self):
        for source_result in (self.payload['master_results']['L'], self.payload['factor_results'][CODE]):
            before = encoded(source_result)
            sources = source_inputs(source_result, REGISTRY)
            self.assertTrue(sources)
            for source in sources:
                self.assertEqual(SourceInput.from_dict(asdict(source)), source)
                self.assertEqual(source.custody, 'excerpt')
            self.assertEqual(encoded(source_result), before)
            self.assertNotIn('synthetic-key-only', encoded([asdict(s) for s in sources]).decode())

    def test_empty_universe_still_validates_calendar_and_forbids_orphans(self):
        empty = {'L': result_for('stock_basic', [], empty_fields=True)}
        out = self.read(master_results=empty, stock_results={}, factor_results={})
        self.assertEqual(out['raw']['status'], 'empty-declared-universe')
        self.assertEqual(out['qfq']['status'], 'empty-declared-universe')
        for kwargs in ({'master_results': empty}, {'calendar_result': {}},
                {'factor_results': {'600000.SH': {}}}, {'stock_results': {'600000.SH': {}}}):
            with self.subTest(keys=list(kwargs)), self.assertRaises(MarketRelayError):
                self.read(**kwargs)

    def test_existing_page_and_horizon_errors_remain(self):
        for kwargs in ({'offset': -1}, {'limit': True}, {'limit': 101}, {'horizons': ()}, {'horizons': (0,)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketInputError):
                self.read(**kwargs)

    def cli(self, payload, *args):
        raw = encoded(payload)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'reading.json'; path.write_bytes(raw)
            env = {**os.environ, 'PYTHONPATH': str(ROOT_DIR / 'src')}
            env.pop('TUSHARE_PROXY_API_KEY', None)
            result = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.market_relay_cli',
                '--input', str(path), '--registry', str(REGISTRY_PATH), *args],
                cwd=directory, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(path.read_bytes(), raw)
            return result

    def test_new_process_dual_reading_without_credentials_or_writes(self):
        result = self.cli({'schema': 'relay-security-stock-reading-v1', **self.payload}, '--entity-id', CODE)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(Decimal(out['qfq']['entries'][0]['horizons'][1]['change_pct']), 0)
        self.assertEqual(out['scope_count'], 1)
        self.assertNotIn('synthetic-key-only', result.stdout)

    def test_cli_never_accepts_user_supplied_full_scope_or_raw_factor_label(self):
        for extra in ({'coverage': 'full-declared-universe'}, {'declared_entities': [CODE]}, {'adjustment': 'hfq'}):
            with self.subTest(extra=extra):
                result = self.cli({'schema': 'relay-security-stock-reading-v1', **self.payload, **extra})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)['error'], 'MARKET_RELAY_PAYLOAD_INVALID')
                self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
