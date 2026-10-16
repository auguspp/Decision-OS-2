"""Synthetic quotes through the real Relay, ordinary reading and custody contracts."""
from copy import deepcopy
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market, market_reading_projection,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_relay_inputs import MarketRelayError
from decision_kernel.application.shibor_inputs import (
    SHIBOR_TENORS, SHIBOR_FIELDS, SHIBOR_READING_SCHEMA, _quote, _difference,
)
from decision_kernel.application.research_sources import capture_plan, parameters_for, _table
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR, SyntheticOpener
from test_market_custody import MemoryBackend

START, END = '20261012', '20261016'
FIELDS = ['date', *SHIBOR_TENORS]
ROWS = [[START, '1.00', '1.10', '1.20', '1.30', '1.40', '1.50', '1.60', '1.70'],
        ['20261013', '1.10', '1.20', '1.30', '1.40', '1.50', '1.60', '1.70', '1.80'],
        [END, '1.25', '1.10', '1.00', '0.90', '1.45', '1.55', '1.65', '1.85']]


def captured(rows=None, *, fields=None, params=None, limit=2, max_pages=100):
    fields = FIELDS if fields is None else fields
    rows = deepcopy(ROWS if rows is None else rows)
    request = {'api': 'shibor', 'stage': 'baseline', 'purpose': 'synthetic Shibor contract',
        'parameters': {'start_date': START, 'end_date': END, 'fields': ','.join(fields),
                       'limit': str(limit), **(params or {})}}
    client = RelayHTTP(opener=SyntheticOpener('shibor', fields, rows), credential='synthetic-key-only')
    return list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-shibor',
        'requests': [request]}, REGISTRY, client=client, public_rights_confirmed=True,
        policy=RetryPolicy(max_attempts=1, max_wait_seconds=0), max_pages=max_pages,
        clock=lambda: '2026-10-16T02:00:00+00:00'))[0]


def payload(result=None, **changes):
    return {'schema': SHIBOR_READING_SCHEMA, 'rate_result': captured() if result is None else result,
            'start': START, 'end': END, 'baseline_dates': [START, '20261013'], **changes}


class ShiborReadingTests(unittest.TestCase):
    def read(self, data=None, **kwargs):
        return read_market_payload(payload() if data is None else data, REGISTRY, **kwargs)

    def test_exact_unit_sign_and_no_price_division(self):
        out = self.read()
        self.assertEqual(set(FIELDS), SHIBOR_FIELDS)
        self.assertEqual((out['scope_count'], out['quote_unit'], out['currency']), (8, 'percent', 'CNY'))
        self.assertEqual(out['entries'][0]['comparisons'][0]['change_pp'], '0.25')
        self.assertEqual(out['entries'][0]['comparisons'][0]['change_bp'], '25.00')
        self.assertEqual(out['entries'][2]['comparisons'][0]['change_bp'], '-20.00')
        self.assertEqual(out['entries'][1]['comparisons'][0]['change_bp'], '0.00')
        self.assertEqual(out['entries'][0]['comparisons'][0]['elapsed_calendar_days'], 4)
        self.assertNotIn('change_pct', encoded(out).decode())
        for counts in out['comparison_counts'].values():
            self.assertEqual(counts['observed'], 8)
            self.assertEqual(sum(counts.values()), 8)

    def test_signed_zero_rates_and_invalid_cells_not_positive_stock_prices(self):
        for value in (0, 0.0, '-0.25', -0.25, '-0', '+1.25'):
            with self.subTest(value=value):
                self.assertEqual(_quote(value)['status'], 'observed')
        for value in (True, False, [], {}, 'NaN', 'Infinity', '1e999', '1,000', ' 1', '1%', 'private-marker'):
            with self.subTest(type=type(value).__name__):
                out = _quote(value)
                self.assertEqual(out, {'status': 'invalid-value', 'rate_pct': None})
                self.assertNotIn('private-marker', encoded(out).decode())
        rows = deepcopy(ROWS); rows[0][1] = 0; rows[-1][1] = '-0.25'
        self.assertEqual(self.read(payload(captured(rows)))['entries'][0]['comparisons'][0]['change_bp'], '-25.00')

    def test_decimal_context_independence_and_inexact_refusal(self):
        expected = self.read()
        with localcontext() as ctx:
            ctx.prec = 2; ctx.rounding = ROUND_DOWN; ctx.traps[Inexact] = True
            self.assertEqual(self.read(), expected)
        out = _difference(_quote('1e128'), _quote('1e-128'))
        self.assertEqual(out, {'status': 'arithmetic-unavailable', 'change_pp': None, 'change_bp': None})

    def test_missing_intermediate_date_is_not_an_endpoint_gap(self):
        out = self.read()
        self.assertNotIn('2026-10-14', out['received_dates'])
        self.assertEqual(out['comparison_counts']['2026-10-12']['observed'], 8)
        self.assertIn('not-trading-session-windows', out['comparison_semantics'])
        self.assertNotIn('market_sessions', out)

    def test_exact_baseline_missing_no_nearest_quote_substitution(self):
        out = self.read(payload(baseline_dates=['20261014', START]))
        self.assertEqual(out['baseline_dates'], ['2026-10-12', '2026-10-14'])
        self.assertEqual(out['comparison_counts']['2026-10-14']['missing-quote'], 8)
        self.assertEqual(out['entries'][0]['comparisons'][1]['baseline']['status'], 'date-not-returned')
        self.assertIsNone(out['entries'][0]['comparisons'][1]['change_bp'])

    def test_missing_end_does_not_advance_to_latest_received_date(self):
        out = self.read(payload(captured(ROWS[:-1])))
        self.assertEqual(out['as_of'], '2026-10-16')
        self.assertEqual(out['end_quote_counts']['date-not-returned'], 8)
        self.assertTrue(all(v['missing-quote'] == 8 for v in out['comparison_counts'].values()))

    def test_missing_and_invalid_cells_are_local_and_preserve_denominator(self):
        rows = deepcopy(ROWS); rows[0][2] = None; rows[-1][3] = 'private-marker'; rows[-1][4] = ''
        out = self.read(payload(captured(rows)))
        self.assertEqual(out['comparison_counts']['2026-10-12'],
            {'observed': 5, 'missing-quote': 2, 'invalid-quote': 1, 'arithmetic-unavailable': 0})
        self.assertEqual(out['comparison_counts']['2026-10-13']['observed'], 6)
        self.assertEqual(out['end_quote_counts'],
            {'observed': 6, 'missing-value': 1, 'invalid-value': 1, 'date-not-returned': 0})
        self.assertNotIn('private-marker', encoded(out).decode())

    def test_whole_received_cell_counts_include_unselected_dates(self):
        rows = deepcopy(ROWS) + [['20261014', 'bad', *['1'] * 7]]
        out = self.read(payload(captured(rows)))
        counts = out['entries'][0]['received_cell_counts']
        self.assertEqual(counts, {'observed': 3, 'missing-value': 0, 'invalid-value': 1})
        self.assertEqual(out['comparison_counts']['2026-10-12']['observed'], 8)
        self.assertEqual(sum(counts.values()), out['source_qualification']['row_count'])

    def test_empty_is_complete_capture_without_quotes(self):
        out = self.read(payload(captured([])))
        self.assertEqual(out['source_qualification']['status'], 'capture-checked-empty')
        self.assertEqual(len(out['entries']), 8)
        self.assertEqual(out['end_quote_counts']['date-not-returned'], 8)
        self.assertEqual(out['entries'][0]['received_cell_counts'],
                         {'observed': 0, 'missing-value': 0, 'invalid-value': 0})
        self.assertTrue(all(e['status'] == 'missing-quote' for e in out['curve_spreads']))

    def test_date_or_duplicate_refuses_entire_capture_including_off_baseline(self):
        variants = [ROWS + [ROWS[0]], ROWS + [['20260230', *['1'] * 8]],
                    ROWS + [['20261017', *['1'] * 8]], ROWS + [['20261011', *['1'] * 8]],
                    ROWS + [['2026-10-14', *['1'] * 8]]]
        for rows in variants:
            with self.subTest(date=rows[-1][0]), self.assertRaises(MarketRelayError):
                self.read(payload(captured(rows)))

    def test_scope_fields_and_capture_snapshot_refused(self):
        for params in ({'date': START}, {'start_date': '20261013'}, {'offset': '2'}, {'ts_code': 'x'}):
            with self.subTest(params=params), self.assertRaises((RecordError, MarketRelayError)):
                self.read(payload(captured(params=params)))
        with self.assertRaises(MarketRelayError):
            self.read(payload(captured([r[:-1] for r in ROWS], fields=FIELDS[:-1])))
        bad = captured(); bad['capture']['snapshot'] = {}
        with self.assertRaises(MarketRelayError): self.read(payload(bad))
        with self.assertRaises(MarketRelayError): self.read(payload(captured(max_pages=1)))

    def test_caller_bounds_and_foreign_schema_do_not_reach_view(self):
        for changes in ({'baseline_dates': []}, {'baseline_dates': '20261012'},
                        {'baseline_dates': [START] * 13}, {'baseline_dates': [START, START]},
                        {'baseline_dates': [END]}, {'baseline_dates': ['20261011']},
                        {'baseline_dates': [None]}, {'start': '20000101'},
                        {'horizons': [5]}, {'schema': 'wrong'}, {'extra': 'private-marker'}):
            with self.subTest(changes=changes), self.assertRaises((MarketRelayError, ValueError)):
                self.read(payload(**changes))
        for kwargs in ({'offset': True}, {'limit': 0}, {'limit': 101}, {'entity_id': '2y'}, {'entity_id': []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MarketRelayError): self.read(**kwargs)

    def test_curve_spreads_exact_same_date_not_discount_curve(self):
        out = self.read()
        end = [s for s in out['curve_spreads'] if s['date'] == '2026-10-16']
        self.assertEqual([s['spread_bp'] for s in end], ['20.00', '60.00'])
        self.assertTrue(all(s['short_tenor'] == 'on' for s in end))
        rows = deepcopy(ROWS); rows[-1][1] = None
        out = self.read(payload(captured(rows)))
        self.assertTrue(all(s['spread_bp'] is None for s in out['curve_spreads'] if s['date'] == '2026-10-16'))
        self.assertEqual(out['comparison_counts']['2026-10-12']['observed'], 7)

    def test_paging_and_filter_leave_global_counts_and_version_stable(self):
        data = payload(); before = encoded(data); whole = self.read(data)
        pages = [self.read(data, offset=n, limit=3) for n in (0, 3, 6)]
        self.assertEqual([e for p in pages for e in p['entries']], whole['entries'])
        self.assertIsNone(pages[-1]['pagination']['next_offset'])
        one = self.read(data, entity_id='1y', limit=1)
        empty = self.read(data, offset=100)
        for view in [*pages, one, empty]:
            for key in ('scope_count', 'comparison_counts', 'end_quote_counts', 'view_version', 'curve_spreads'):
                self.assertEqual(view[key], whole[key])
        self.assertEqual(one['matched_count'], 1)
        self.assertEqual(empty['entries'], [])
        self.assertEqual(encoded(data), before)

    def test_reordered_source_columns_and_multipage_have_same_numbers(self):
        out = self.read(payload(captured([list(reversed(r)) for r in ROWS], fields=list(reversed(FIELDS)))))
        self.assertEqual(out['entries'], self.read()['entries'])
        self.assertEqual(out['source_qualification']['page_count'], 2)
        self.assertEqual([p['offset'] for p in out['source_lineage']['pages']], [0, 2])


class ShiborFieldAdmissionTests(unittest.TestCase):
    def test_exception_is_exact_api_and_does_not_change_parameter_names(self):
        normal = {'start_date': START, 'end_date': END, 'fields': ','.join(FIELDS), 'limit': '2'}
        checked, _ = parameters_for(REGISTRY, 'shibor', normal)
        self.assertEqual(checked, normal)
        for api in ('daily', 'us_tycr', 'hibor', 'shibor_quote'):
            with self.subTest(api=api), self.assertRaisesRegex(RecordError, 'SAFE_FIELDS'):
                parameters_for(REGISTRY, api, normal)
        with self.assertRaisesRegex(RecordError, 'PARAMETER_NOT_ALLOWED'):
            parameters_for(REGISTRY, 'shibor', {**normal, '1w': '1'})

    def test_request_and_response_reject_unknown_digits_and_unsafe_names(self):
        for field in ('2y', '01w', '1W', '1w ', '1w;drop', '1w/api_key', '', '１w', None, 1):
            with self.subTest(field=field):
                params = {'start_date': START, 'end_date': END, 'fields': 'date,' + str(field), 'limit': '2'}
                with self.assertRaises(RecordError): parameters_for(REGISTRY, 'shibor', params)
                with self.assertRaises(RecordError):
                    _table('shibor', {**params, 'fields': 'date,on'},
                           {'code': 0, 'data': {'fields': ['date', field], 'items': []}})
        for field in ('api_key', 'secret', 'full_text', 'email', 'on,on'):
            with self.subTest(field=field), self.assertRaises(RecordError):
                parameters_for(REGISTRY, 'shibor', {'date': START, 'fields': 'date,' + field, 'limit': '2'})

    def test_response_exception_does_not_expand_other_apis_or_export_extra_columns(self):
        body = {'code': 0, 'data': {'fields': ['date', '1w'], 'items': [[START, '1']]}}
        with self.assertRaisesRegex(RecordError, 'FIELDS_INVALID'):
            _table('daily', {'fields': 'date', 'limit': '2'}, body)
        out = _table('shibor', {'fields': 'date', 'limit': '2'}, body)
        self.assertEqual(out['fields'], ['date'])
        self.assertEqual(out['items'], [[START]])
        self.assertEqual(out['omitted_unrequested_columns'], 1)


class ShiborCustodyTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend(); self.store = VersionedStore(self.backend)
    def fresh(self): return VersionedStore(MemoryBackend(self.backend.shared))
    def save(self, data=None, **kwargs):
        return retain_market_reading(payload() if data is None else data, REGISTRY, self.store,
            **{'fresh_reader': self.fresh, 'public_rights_confirmed': True, **kwargs})
    def read(self, ref, **kwargs): return read_stored_market(ref, REGISTRY, self.fresh(), **kwargs)

    def test_original_root_formats_roundtrip_and_projection(self):
        data = payload(); before = encoded(data); saved = self.save(data)
        self.assertEqual((saved['query_count'], saved['page_count']), (1, 2))
        self.assertEqual(saved['restored']['view'], read_market_payload(data, REGISTRY))
        restored = self.read(saved['reference'], entity_id='1y')
        self.assertEqual(restored['view']['scope_count'], 8)
        self.assertEqual(restored['source_requests'], 0)
        self.assertFalse(restored['original_http_restored'])
        root = json.loads(self.store.read_source(SourceRef.from_dict(saved['reference']))['data'])
        self.assertEqual(root['schema'], 'market-reading-custody-v1')
        query = json.loads(self.store.read_source(SourceRef.from_dict(next(iter(root['queries'].values()))))['data'])
        self.assertEqual(query['schema'], 'market-query-custody-v1')
        projected = market_reading_projection(data, REGISTRY)
        self.assertEqual(read_market_payload(projected, REGISTRY), saved['restored']['view'])
        self.assertTrue(all(not p['attempts'] for p in projected['rate_result']['capture']['pages']))
        self.assertEqual(encoded(data), before)

    def test_missing_source_page_returns_unknown_view_without_network(self):
        saved = self.save()
        root = json.loads(self.store.read_source(SourceRef.from_dict(saved['reference']))['data'])
        query = json.loads(self.store.read_source(SourceRef.from_dict(next(iter(root['queries'].values()))))['data'])
        blob = query['pages'][0]['reference']['manifest']
        self.backend.shared['missing'].add((blob['commit'], blob['path']))
        with patch.object(RelayHTTP, '__init__', side_effect=AssertionError('no network')):
            out = self.read(saved['reference'])
        self.assertEqual(out['status'], 'partial-restoration')
        self.assertEqual(out['unavailable_queries'], 1)
        self.assertIsNone(out['view'])
        self.assertEqual(out['source_requests'], 0)

    def test_rights_and_incomplete_preflight_before_any_write(self):
        for rights in (False, None, 'true', 1):
            with self.subTest(rights=rights), self.assertRaises(RecordError):
                self.save(public_rights_confirmed=rights)
        with self.assertRaises(MarketRelayError): self.save(payload(captured(max_pages=1)))
        self.assertEqual(self.backend.shared['count'], 0)

    def test_empty_and_missing_cell_retain_original_unknown_not_zero(self):
        for rows in ([], [[START, None, *['1'] * 7], ROWS[-1]]):
            with self.subTest(rows=len(rows)):
                saved = self.save(payload(captured(rows)))
                view = self.read(saved['reference'])['view']
                self.assertIsNone(view['entries'][0]['comparisons'][0]['change_bp'])

    def test_unknown_write_reconciles_original_intent_without_duplicate_sources(self):
        data = payload(); commit = self.backend.commit; raised = False
        def unknown(files, *, expected):
            nonlocal raised
            result = commit(files, expected=expected)
            if not raised:
                raised = True
                raise UnknownWrite('SYNTHETIC_LOST_ACK')
            return result
        with patch.object(self.backend, 'commit', side_effect=unknown), self.assertRaises(UnknownWrite):
            self.save(data)
        saved = self.save(data); count = self.backend.shared['count']
        again = self.save(data)
        self.assertEqual(saved['reference'], again['reference'])
        self.assertEqual(self.backend.shared['count'], count)

    def test_revision_does_not_move_existing_exact_ref(self):
        first = self.save(); rows = deepcopy(ROWS); rows[-1][1] = '9.0'
        second = self.save(payload(captured(rows)))
        self.assertNotEqual(first['reference'], second['reference'])
        self.assertEqual(self.read(first['reference'])['view']['entries'][0]['end_quote']['rate_pct'], '1.25')


class ShiborCLIRecoveryTests(unittest.TestCase):
    def test_real_git_new_process_only_root_ref_without_input_or_credentials(self):
        env = {k: v for k, v in os.environ.items() if not any(x in k.upper() for x in ('TOKEN', 'KEY', 'SECRET'))}
        env['PYTHONPATH'] = str(ROOT_DIR / 'src')
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp); src = folder / 'input.json'; src.write_bytes(encoded(payload()))
            def cli(module, *args):
                run = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.' + module, *map(str, args)],
                    capture_output=True, text=True, cwd=folder, env=env, timeout=45)
                self.assertEqual(run.returncode, 0, run.stderr)
                return json.loads(run.stdout)
            original = cli('market_relay_cli', '--input', src, '--registry', REGISTRY_PATH)
            GitStore.init_synthetic(folder / 'records.git')
            saved = cli('market_custody_cli', 'save', '--input', src, '--registry', REGISTRY_PATH,
                        '--path', folder / 'records.git', '--public-rights-confirmed')
            ref = folder / 'ref.json'; ref.write_bytes(encoded(saved['reference'])); src.unlink()
            before = GitStore(folder / 'records.git').head()
            out = cli('market_custody_cli', 'read', '--input', ref, '--registry', REGISTRY_PATH,
                      '--path', folder / 'records.git', '--entity-id', 'on')
            self.assertEqual(out['view']['view_version'], original['view_version'])
            self.assertEqual(out['view']['scope_count'], 8)
            self.assertEqual(out['view']['entries'][0]['comparisons'][0]['change_bp'], '25.00')
            self.assertEqual(out['source_requests'], 0)
            self.assertEqual(GitStore(folder / 'records.git').head(), before)

    def test_cli_invalid_input_has_stable_error_without_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / 'bad.json'; src.write_text('{"schema":"private-marker"}')
            run = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.market_relay_cli',
                '--input', str(src), '--registry', str(REGISTRY_PATH)], cwd=tmp,
                env={**os.environ, 'PYTHONPATH': str(ROOT_DIR / 'src')}, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 2)
            self.assertNotIn('private-marker', run.stderr + run.stdout)
            self.assertNotIn('Traceback', run.stderr)


if __name__ == '__main__': unittest.main()
