"""Synthetic THS cases through the existing real Registry/HTTP/capture contracts.

Names, dates, indices and security lists are artificial, not vendor evidence.
"""
from copy import deepcopy
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from decision_kernel.application.market_group_inputs import (
    qualified_ths_catalog, qualified_ths_members, read_ths_group_surface, read_three_market_surfaces)
from decision_kernel.application.market_relay_inputs import MarketRelayError
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.market_daily import MarketInputError
from test_market_relay_inputs import (SyntheticOpener, REGISTRY, REGISTRY_PATH, ROOT_DIR,
                                     CODE, START, END, CALENDAR, PRICES, captured)

SECTOR, CONCEPT = '881001.TI', '885001.TI'
CAT_FIELDS = ['ts_code', 'name', 'count', 'exchange', 'list_date', 'type']
MEM_FIELDS = ['ts_code', 'con_code', 'con_name']
LEVEL_FIELDS = ['ts_code', 'trade_date', 'close']
BASIC_FIELDS = ['ts_code', 'symbol', 'name', 'exchange', 'curr_type', 'list_status', 'list_date', 'delist_date']
DAYS = [r[1] for r in PRICES]


def capture(api, rows, params, *, fields=None, max_pages=100, empty_fields=False, limit=2):
    fields = fields or {'ths_index': CAT_FIELDS, 'ths_member': MEM_FIELDS, 'ths_daily': LEVEL_FIELDS,
        'stock_basic': BASIC_FIELDS}[api]
    request = {'api': api, 'stage': 'baseline', 'purpose': 'synthetic group engineering test',
        'parameters': {'fields': ','.join(fields), 'limit': str(limit), **params}}
    opener = SyntheticOpener(api, fields, rows, empty_fields)
    result = list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-groups',
        'requests': [request]}, REGISTRY, client=RelayHTTP(opener=opener, credential='synthetic-key-only'),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        max_pages=max_pages, clock=lambda: '2026-10-16T02:00:00+00:00'))[0]
    return result


def catalog_row(code=SECTOR, kind='sector', count=1):
    return [code, '合成同名分类', count, 'A', '20000101', 'I' if kind == 'sector' else 'N']


def catalogue(rows=None, kind='sector', **kwargs):
    code = SECTOR if kind == 'sector' else CONCEPT
    return capture('ths_index', [catalog_row(code, kind)] if rows is None else rows,
                   {'exchange': 'A', 'type': 'I' if kind == 'sector' else 'N'}, **kwargs)


def members(code=SECTOR, rows=None, **kwargs):
    return capture('ths_member', [[code, CODE, '合成股票']] if rows is None else rows, {'ts_code': code}, **kwargs)


def levels(code=SECTOR, rows=None, **kwargs):
    return capture('ths_daily', [[code, r[1], r[2]] for r in PRICES] if rows is None else rows,
                   {'ts_code': code, 'start_date': START, 'end_date': END}, **kwargs)


def group_input(kind='sector'):
    code = SECTOR if kind == 'sector' else CONCEPT
    return {'catalog_result': catalogue(kind=kind), 'member_results': {code: members(code)},
        'index_results': {code: levels(code)}, 'calendar_result': captured('trade_cal', CALENDAR),
        'exchange': 'SSE', 'start': START, 'end': END, 'horizons': (1, 3, 5)}


def stock_input():
    return {'master_results': {'L': capture('stock_basic', [[CODE, CODE[:6], '合成股票', 'SSE',
                'CNY', 'L', '20000101', None]], {'exchange': 'SSE', 'list_status': 'L'})},
        'stock_results': {CODE: captured('daily', PRICES)}, 'factor_results': {},
        'calendar_result': captured('trade_cal', CALENDAR), 'exchange': 'SSE', 'start': START,
        'end': END, 'horizons': (1, 3)}


class GroupQualificationTests(unittest.TestCase):
    def test_distinct_namespaces_even_for_same_name_and_raw_code(self):
        sector = qualified_ths_catalog(catalogue(), registry=REGISTRY, kind='sector')
        concept = qualified_ths_catalog(catalogue([catalog_row(kind='concept')], kind='concept'),
                                        registry=REGISTRY, kind='concept')
        self.assertNotEqual(sector['groups'][SECTOR]['entity_id'], concept['groups'][SECTOR]['entity_id'])
        self.assertIsNone(sector['effective_at'])
        self.assertIn('not-historical', sector['semantics'])

    def test_all_pages_and_empty_are_not_full_market(self):
        rows = [catalog_row(f'88100{i}.TI') for i in range(5)]
        out = qualified_ths_catalog(catalogue(rows), registry=REGISTRY, kind='sector')
        self.assertEqual(out['scope_count'], 5)
        self.assertEqual([p['offset'] for p in out['lineage']['pages']], [0, 2, 4])
        out = qualified_ths_catalog(catalogue([], empty_fields=True), registry=REGISTRY, kind='sector')
        self.assertEqual(out['scope_count'], 0)

    def test_catalogue_wrong_type_exchange_names_dates_and_counts(self):
        for i, val in ((0, '801001.SI'), (0, ''), (1, '\n'), (1, None), (1, 'a'*257),
                       (2, True), (2, -1), (2, 'NaN'), (2, '1.2'), (2, 2.0), (3, 'HK'),
                       (4, '20260230'), (5, 'N')):
            row = catalog_row(); row[i] = val
            with self.subTest(i=i, val=val), self.assertRaises(MarketRelayError):
                qualified_ths_catalog(catalogue([row]), registry=REGISTRY, kind='sector')
        for kind in ('stock', [], None):
            with self.subTest(kind=kind), self.assertRaises(MarketRelayError):
                qualified_ths_catalog(catalogue(), registry=REGISTRY, kind=kind)

    def test_count_unknown_zero_and_string_are_distinct(self):
        for count, expected in ((None, None), ('', None), (0, 0), ('3', 3)):
            out = qualified_ths_catalog(catalogue([catalog_row(count=count)]), registry=REGISTRY, kind='sector')
            self.assertEqual(out['groups'][SECTOR]['reported_member_count'], expected)

    def test_duplicate_catalogue_and_incomplete_pages_refused(self):
        for result in (catalogue([catalog_row()] * 2),
                       catalogue([catalog_row(), catalog_row('881002.TI')], max_pages=1)):
            with self.assertRaises(MarketRelayError):
                qualified_ths_catalog(result, registry=REGISTRY, kind='sector')

    def test_narrowing_filters_and_nonzero_origin_refused(self):
        for extra in ({'ts_code': SECTOR}, {'name': '合成同名分类'}, {'offset': '2'}, {'type': 'N'}):
            result = capture('ths_index', [catalog_row()], {'exchange': 'A', 'type': 'I', **extra})
            with self.subTest(extra=extra), self.assertRaises(MarketRelayError):
                qualified_ths_catalog(result, registry=REGISTRY, kind='sector')

    def test_reordered_fields_and_bad_registry_even_empty(self):
        out = qualified_ths_catalog(catalogue([list(reversed(catalog_row()))], fields=list(reversed(CAT_FIELDS))),
                                    registry=REGISTRY, kind='sector')
        self.assertEqual(out['groups'][SECTOR]['name'], '合成同名分类')
        with self.assertRaises(MarketRelayError):
            qualified_ths_catalog(catalogue([]), registry=None, kind='sector')

    def test_member_snapshot_has_no_invented_effective_date(self):
        out = qualified_ths_members(members(), registry=REGISTRY, ts_code=SECTOR)
        self.assertEqual(out['members'], {CODE: '合成股票'})
        self.assertIsNone(out['effective_at'])
        self.assertEqual(out['lineage']['pages'][0]['captured_at'], '2026-10-16T02:00:00+00:00')
        self.assertNotIn('weight', out)

    def test_bj_metadata_does_not_enable_bj_stock_reader(self):
        out = qualified_ths_members(members(rows=[[SECTOR, '920001.BJ', '合成北交所']]), registry=REGISTRY, ts_code=SECTOR)
        self.assertIn('920001.BJ', out['members'])
        from decision_kernel.application.market_relay_inputs import _security
        with self.assertRaises(MarketRelayError):
            _security('920001.BJ')

    def test_member_duplicates_wrong_identity_and_names_refused(self):
        for rows in ([[SECTOR, CODE, '合成']] * 2, [[CONCEPT, CODE, '合成']],
                     [[SECTOR, '600519.sh', '合成']], [[SECTOR, '00700.HK', '合成']],
                     [[SECTOR, CODE, '']], [[SECTOR, CODE, None]]):
            with self.subTest(rows=rows), self.assertRaises(MarketRelayError):
                qualified_ths_members(members(rows=rows), registry=REGISTRY, ts_code=SECTOR)

    def test_extra_dates_and_missing_fields_do_not_qualify_member_history(self):
        result = members(rows=[[SECTOR, CODE, '合成', '20200101', None]], fields=MEM_FIELDS + ['in_date', 'out_date'])
        out = qualified_ths_members(result, registry=REGISTRY, ts_code=SECTOR)
        self.assertIsNone(out['effective_at'])
        result = capture('ths_member', [[SECTOR, CODE]], {'ts_code': SECTOR}, fields=MEM_FIELDS[:2])
        with self.assertRaises(MarketRelayError):
            qualified_ths_members(result, registry=REGISTRY, ts_code=SECTOR)
        result = capture('ths_member', [[SECTOR, CODE, '合成']], {'ts_code': SECTOR, 'trade_date': END})
        with self.assertRaises(MarketRelayError):
            qualified_ths_members(result, registry=REGISTRY, ts_code=SECTOR)

    def test_member_empty_distinct_from_absent(self):
        out = qualified_ths_members(members(rows=[], empty_fields=True), registry=REGISTRY, ts_code=SECTOR)
        self.assertEqual(out['members'], {})
        self.assertEqual(out['row_count'], 0)
        with self.assertRaises(MarketRelayError):
            qualified_ths_members({}, registry=REGISTRY, ts_code=SECTOR)

    def test_capture_receipts_and_context_remain_enforced(self):
        for kind in ('complete', 'snapshot', 'offset', 'hash'):
            bad = members()
            if kind == 'complete': bad['capture']['complete'] = False
            elif kind == 'snapshot': bad['capture']['snapshot'] = {}
            elif kind == 'offset': bad['capture']['pages'][0]['offset'] = 1
            else: bad['capture']['pages'][0]['transport']['wire_sha256'] = 'invalid'
            with self.subTest(kind=kind), self.assertRaises(MarketRelayError):
                qualified_ths_members(bad, registry=REGISTRY, ts_code=SECTOR)

    def test_shared_registry_admission_not_reimplemented(self):
        for api in ('ths_index', 'ths_member', 'ths_daily'):
            self.assertTrue(REGISTRY.lookup(api)['registered_reader_candidate'])



class GroupReadingTests(unittest.TestCase):
    def setUp(self):
        self.payload = group_input()

    def read(self, **kwargs):
        return read_ths_group_surface(registry=REGISTRY, kind='sector', **{**self.payload, **kwargs})

    def test_capture_to_calendar_bound_index_reading_without_currency_invention(self):
        before = encoded(self.payload)
        out = self.read()
        self.assertEqual(out['scope_count'], 1)
        self.assertEqual(Decimal(out['entries'][0]['horizons'][1]['change_pct']), Decimal(4))
        self.assertEqual(out['index_unit'], 'index-points-not-currency')
        self.assertEqual(out['currency'], 'XXX')
        self.assertEqual(out['adjustment'], 'raw')
        self.assertIsNone(out['membership'])
        self.assertIsNone(out['membership_as_of'])
        self.assertIsNone(out['entries'][0]['member_snapshot']['effective_at'])
        self.assertEqual(encoded(self.payload), before)

    def test_members_failure_not_a_price_gate(self):
        for mapping, status in (({}, 'not-supplied'), ({SECTOR: {}}, 'rejected'),
                                ({SECTOR: members(rows=[])}, 'checked')):
            out = self.read(member_results=mapping)
            self.assertEqual(out['observed_count'], 1)
            self.assertEqual(out['entries'][0]['qualification']['members']['status'], status)

    def test_prices_missing_or_bad_not_a_membership_gate(self):
        for mapping, status in (({}, 'not-supplied'), ({SECTOR: {}}, 'rejected'),
                                ({SECTOR: levels(rows=[])}, 'checked')):
            out = self.read(index_results=mapping)
            self.assertEqual(out['observed_count'], 0)
            self.assertEqual(out['scope_count'], 1)
            entry = out['entries'][0]
            self.assertEqual(entry['qualification']['index']['status'], status)
            self.assertEqual(entry['member_snapshot']['row_count'], 1)

    def test_catalogue_count_discrepancy_is_not_silent_member_loss(self):
        out = self.read(catalog_result=catalogue([catalog_row(count=9)]))
        entry = out['entries'][0]
        self.assertEqual(entry['qualification']['members']['catalogue_count_comparison'], 'differs-between-captures')
        self.assertEqual(entry['member_snapshot']['row_count'], 1)
        self.assertEqual(out['observed_count'], 1)

    def test_whole_catalogue_denominator_and_paging(self):
        rows = [catalog_row(), catalog_row('881002.TI'), catalog_row('881003.TI')]
        out = self.read(catalog_result=catalogue(rows), limit=1, offset=1)
        self.assertEqual(out['scope_count'], 3)
        self.assertEqual(out['entries'][0]['entity_id'], 'ths-a-sector:881002.TI')
        self.assertEqual(out['page']['next_offset'], 2)
        self.assertEqual(out['qualification_counts']['index'], {'checked': 1, 'rejected': 0, 'not-supplied': 2})
        selected = self.read(catalog_result=catalogue(rows), entity_id='ths-a-sector:' + SECTOR)
        self.assertEqual(selected['scope_count'], 3)
        self.assertEqual(selected['page']['selected_count'], 1)

    def test_group_failures_are_local(self):
        out = self.read(catalog_result=catalogue([catalog_row(), catalog_row('881002.TI')]),
            index_results={SECTOR: {}, '881002.TI': levels('881002.TI')},
            member_results={SECTOR: members(), '881002.TI': {}})
        self.assertEqual(out['observed_count'], 1)
        self.assertEqual(out['qualification_counts']['index']['rejected'], 1)
        self.assertEqual(out['qualification_counts']['members']['rejected'], 1)

    def test_benchmark_level_ratio_and_percentage_point_excess(self):
        code = '883001.TI'
        out = self.read(benchmark={'ts_code': code, 'result': levels(code, [[code, d, v] for d, v in zip(DAYS, ('100','100','101','102'))])})
        window = out['entries'][0]['horizons'][1]
        self.assertEqual(Decimal(window['benchmark_change_pct']), 2)
        self.assertEqual(Decimal(window['excess_change_pp']), 2)
        self.assertEqual(window['relative_status'], 'observed')

    def test_bad_or_gapped_benchmark_only_restricts_relative(self):
        for benchmark in ({}, {'ts_code': '000300.SH', 'result': {}}, {'ts_code': SECTOR, 'result': {}}):
            out = self.read(benchmark=benchmark)
            self.assertEqual(out['observed_count'], 1)
            self.assertEqual(out['benchmark_qualification']['status'], 'rejected')
            self.assertEqual(out['horizon_counts'][1]['relative_observed_count'], 0)
        out = self.read(benchmark={'ts_code': SECTOR, 'result': levels(rows=[])})
        self.assertEqual(out['entries'][0]['horizons'][1]['relative_status'], 'benchmark-session-mismatch')

    def test_source_versions_separate_index_and_members(self):
        first = self.read()
        changed = self.read(member_results={SECTOR: members(rows=[[SECTOR, '600000.SH', '其他合成']])})
        self.assertEqual(first['source_version'], changed['source_version'])
        self.assertEqual(first['universe_version'], changed['universe_version'])
        self.assertNotEqual(first['reported_membership_version'], changed['reported_membership_version'])
        price_changed = self.read(index_results={SECTOR: {}})
        self.assertEqual(first['reported_membership_version'], price_changed['reported_membership_version'])

    def test_calendar_scope_orphan_and_existing_page_guards(self):
        for kwargs in ({'calendar_result': {}}, {'exchange': 'BSE'}, {'exchange': []},
                       {'member_results': {CONCEPT: {}}}, {'index_results': {CONCEPT: {}}},
                       {'member_results': []}, {'limit': True}, {'offset': -1}, {'horizons': ()}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketInputError):
                self.read(**kwargs)

    def test_empty_catalogue_still_requires_qualified_calendar(self):
        args = {'catalog_result': catalogue([], empty_fields=True), 'index_results': {}, 'member_results': {}}
        self.assertEqual(self.read(**args)['status'], 'empty-declared-universe')
        with self.assertRaises(MarketRelayError): self.read(**args, calendar_result={})

    def test_index_history_can_precede_catalogue_retrieval_but_is_not_membership_history(self):
        out = self.read()
        self.assertEqual(out['entries'][0]['horizons'][1]['baseline_date'], '2026-10-12')
        self.assertIsNone(out['entries'][0]['member_snapshot']['effective_at'])
        self.assertIn('CAPTURE_CLOCK_NOT_EFFECTIVE_MEMBERSHIP_DATE_OR_PIT_CERTIFICATION', out['qualification_limits'])

    def test_full_five_twenty_sixty_windows_and_local_price_gap(self):
        from datetime import date, timedelta
        dates = [(date(2026, 7, 1)+timedelta(days=i)).strftime('%Y%m%d') for i in range(61)]
        params = {'start_date': dates[0], 'end_date': dates[-1]}
        args = {'start': dates[0], 'end': dates[-1], 'horizons': (5,20,60),
            'calendar_result': captured('trade_cal', [['SSE', d, 1] for d in dates], params=params, limit=100)}
        rows = [[SECTOR, d, '100' if i < 60 else '110'] for i,d in enumerate(dates)]
        args['index_results'] = {SECTOR: capture('ths_daily', rows, {'ts_code': SECTOR, **params}, limit=100)}
        out = self.read(**args)
        self.assertEqual([Decimal(w['change_pct']) for w in out['entries'][0]['horizons']], [10,10,10])
        args['index_results'] = {SECTOR: capture('ths_daily', rows[1:], {'ts_code': SECTOR, **params}, limit=100)}
        self.assertEqual([h['observed_count'] for h in self.read(**args)['horizon_counts']], [1,1,0])


class ThreeSurfaceTests(unittest.TestCase):
    def test_failed_and_missing_groups_do_not_gate_independent_stocks(self):
        value = {'sector': {}, 'stock': stock_input()}
        before = encoded(value)
        out = read_three_market_surfaces(surfaces=value, registry=REGISTRY)
        self.assertEqual(out['surfaces']['sector']['status'], 'rejected')
        self.assertEqual(out['surfaces']['concept']['status'], 'not-supplied')
        self.assertEqual(out['surfaces']['stock']['view']['raw']['observed_count'], 1)
        self.assertEqual(out['membership_overlap']['status'], 'insufficient-reported-membership')
        self.assertEqual(encoded(value), before)

    def test_all_three_and_literal_page_overlap_keep_snapshot_limits(self):
        out = read_three_market_surfaces(surfaces={'sector': group_input(), 'concept': group_input('concept'),
                                                  'stock': stock_input()}, registry=REGISTRY)
        self.assertTrue(all(s['status']=='read' for s in out['surfaces'].values()))
        overlap = out['membership_overlap']
        self.assertEqual(overlap['status'], 'literal-page-intersection')
        self.assertEqual(overlap['pairs'], [{'left': 'ths-a-sector:'+SECTOR, 'right': 'ths-a-concept:'+CONCEPT,
                                            'members': [CODE], 'overlap_count': 1}])
        self.assertIn('not-same-time', overlap['semantics'])
        self.assertIn('membership_version', overlap['context']['sector'])

    def test_overlap_is_only_for_selected_pages_and_does_not_imply_complete_coverage(self):
        sector = group_input()
        sector['catalog_result'] = catalogue([catalog_row(), catalog_row('881002.TI')])
        out = read_three_market_surfaces(surfaces={'sector': sector, 'concept': group_input('concept')},
                                         registry=REGISTRY, offset=1, limit=1)
        self.assertEqual(out['surfaces']['sector']['view']['scope_count'], 2)
        self.assertEqual(out['membership_overlap']['context']['sector']['unknown_page_memberships'], 1)
        self.assertEqual(out['membership_overlap']['status'], 'insufficient-reported-membership')

    def test_whole_sector_calendar_failure_does_not_block_concept(self):
        sector = group_input(); sector['calendar_result'] = {}
        out = read_three_market_surfaces(surfaces={'sector': sector, 'concept': group_input('concept')}, registry=REGISTRY)
        self.assertEqual(out['surfaces']['sector']['status'], 'rejected')
        self.assertEqual(out['surfaces']['concept']['view']['observed_count'], 1)

    def test_unknown_nested_keys_cannot_override_registry_or_kind(self):
        for extra in ({'kind':'stock'}, {'registry': None}, {'offset': 1}, {'coverage':'full-declared-universe'}):
            out = read_three_market_surfaces(surfaces={'sector': {**group_input(), **extra}, 'stock': stock_input()}, registry=REGISTRY)
            self.assertEqual(out['surfaces']['sector']['status'], 'rejected')
            self.assertEqual(out['surfaces']['stock']['status'], 'read')
        for kwargs in ({'surfaces': []}, {'surfaces': {'unknown': {}}}, {'limit': 0}, {'entity_id': CODE}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketRelayError):
                read_three_market_surfaces(**{'surfaces': {}, 'registry': REGISTRY, **kwargs})

    def run_cli(self, payload, *args):
        raw = encoded(payload)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'groups.json'; path.write_bytes(raw)
            env = {**os.environ, 'PYTHONPATH': str(ROOT_DIR/'src')}
            env.pop('TUSHARE_PROXY_API_KEY',None)
            result = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.market_relay_cli',
                '--input', str(path), '--registry', str(REGISTRY_PATH), *args], cwd=directory,
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(path.read_bytes(), raw)
            return result

    def test_new_process_group_route_without_credentials_or_writes(self):
        result = self.run_cli({'schema':'relay-ths-group-reading-v1', 'kind':'sector', **group_input()},
                              '--entity-id', 'ths-a-sector:'+SECTOR)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['observed_count'], 1)
        self.assertNotIn('synthetic-key-only',result.stdout)

    def test_new_process_combined_route_delivers_independent_channels(self):
        result = self.run_cli({'schema':'relay-three-market-reading-v1',
                              'surfaces': {'sector': {}, 'concept': group_input('concept'), 'stock': stock_input()}}, '--limit', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)['surfaces']
        self.assertEqual(out['sector']['status'], 'rejected')
        self.assertEqual(out['stock']['view']['raw']['observed_count'], 1)
        self.assertEqual(out['concept']['view']['page']['selected_count'], 1)

    def test_new_schema_strictness_keeps_original_json_protection(self):
        for extra in ({'coverage':'full-declared-universe'}, {'declared_entities':[SECTOR]}, {'horizons':None}):
            result = self.run_cli({'schema':'relay-ths-group-reading-v1','kind':'sector',**group_input(),**extra})
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout,'')
            self.assertIn('error',json.loads(result.stderr))
        result = self.run_cli({'schema':'relay-three-market-reading-v1','surfaces': {},'horizons':[5]})
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
