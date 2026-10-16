"""Targeted non-publishing diagnosis through existing Relay and market contracts."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from decision_kernel.application.market_source_check import check_sources
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import Response, REGISTRY, REGISTRY_PATH
from test_market_source_check import Opener, NOW, START, END, SECRET_TEXT
import test_market_source_check as original


class CatalogueOpener(Opener):
    """Alter synthetic wire values before the real capture, not inspected results."""
    def __init__(self, changes=None):
        super().__init__()
        self.changes = changes or {}

    def open(self, request, timeout):
        response = super().open(request, timeout)
        body = json.loads(response.raw)
        if body['api_name'] == 'ths_index':
            fields = body['data']['fields']
            for row in body['data']['items']:
                for field, value in self.changes.items():
                    row[fields.index(field)] = value
        return Response(request.full_url, body)


class MarketDiagnosticTests(unittest.TestCase):
    def run_check(self, opener=None, **extra):
        self.opener = opener or CatalogueOpener()
        self.receipts, self.waits = [], []
        client = RelayHTTP(opener=self.opener, credential='synthetic-key-only')
        return check_sources(registry=REGISTRY, exchange='SSE', start=START, end=END,
            access_confirmed=True, selection=extra.pop('selection', 'catalogues'), client=client,
            now=NOW, progress=self.receipts.append, sleep=self.waits.append,
            clock=lambda: NOW.isoformat(), **extra)

    def test_targeted_selection_executes_exactly_two_catalogues(self):
        with patch('decision_kernel.application.market_source_check.read_three_market_surfaces',
                   side_effect=AssertionError('targeted check must not compose samples')):
            out = self.run_check()
        self.assertEqual(out['status'], 'checked-catalogues')
        self.assertEqual(set(out['queries']), {'sector_catalog', 'concept_catalog'})
        self.assertEqual([(a, p['type']) for a, p in self.opener.calls], [('ths_index', 'I'), ('ths_index', 'N')])
        self.assertEqual(out['http_attempts'], 2)
        self.assertEqual(out['surfaces'], {})
        self.assertEqual(out['selection'], 'catalogues')

    def test_count_error_is_diagnosable_without_coercion(self):
        out = self.run_check(CatalogueOpener({'count': 1.5}))
        self.assertEqual(out['status'], 'completed-with-gaps')
        for item in out['queries'].values():
            self.assertEqual(item['reason_code'], 'MARKET_RELAY_GROUP_COUNT')
            self.assertEqual(item['status'], 'rejected')
            self.assertEqual(item['catalogue_shape']['field_types']['count']['number'], 1)
            self.assertEqual(item['catalogue_shape']['integral_numeric_counts'], 0)
            self.assertEqual(item['catalogue_shape']['fractional_numeric_counts'], 1)

    def test_integral_numeric_count_is_accepted_without_rewriting_diagnostics(self):
        for selection, expected in (('catalogues', 'checked-catalogues'), ('sample', 'checked-sample')):
            with self.subTest(selection=selection):
                out = self.run_check(CatalogueOpener({'count': 1.0}), selection=selection)
                self.assertEqual(out['status'], expected)
                for slot in ('sector_catalog', 'concept_catalog'):
                    item = out['queries'][slot]
                    self.assertEqual(item['status'], 'checked')
                    self.assertIsNone(item['reason_code'])
                    self.assertEqual(item['catalogue_shape']['field_types']['count']['number'], 1)
                    self.assertEqual(item['catalogue_shape']['integral_numeric_counts'], 1)
                self.assertNotIn(SECRET_TEXT, json.dumps(self.receipts))

    def test_name_error_and_traits_are_diagnosable_without_trimming(self):
        out = self.run_check(CatalogueOpener({'name': '\t' + SECRET_TEXT + ' '}))
        for item in out['queries'].values():
            self.assertEqual(item['reason_code'], 'MARKET_RELAY_GROUP_NAME')
            self.assertEqual(item['catalogue_shape']['padded_names'], 1)
            self.assertEqual(item['catalogue_shape']['nonprintable_names'], 1)
            self.assertEqual(item['catalogue_shape']['field_types']['name']['string'], 1)
        self.assertNotIn(SECRET_TEXT, json.dumps(self.receipts))

    def test_structure_counters_partition_rows_and_keep_values_private(self):
        for count, kind in [(None, 'null'), (True, 'boolean'), (7, 'integer'), ('7', 'string'), (7.5, 'number')]:
            with self.subTest(kind=kind):
                out = self.run_check(CatalogueOpener({'count': count}))
                for item in out['queries'].values():
                    shape = item['catalogue_shape']
                    self.assertEqual(shape['field_types']['count'][kind], 1)
                    for buckets in shape['field_types'].values():
                        self.assertEqual(sum(buckets.values()), item['row_count'])
                for banned in (SECRET_TEXT, '880001.TI', '885001.TI', '19990101',
                               'wire_sha256', 'query_key', 'x_request_id', 'synthetic-key-only'):
                    self.assertNotIn(banned, json.dumps(self.receipts))

    def test_legal_empty_catalogues_do_not_become_nonempty_success(self):
        opener = CatalogueOpener(); opener.empty = 'ths_index'
        out = self.run_check(opener)
        self.assertEqual(out['status'], 'completed-with-gaps')
        for item in out['queries'].values():
            self.assertEqual(item['status'], 'checked-empty')
            self.assertEqual(item['row_count'], 0)
            self.assertTrue(all(sum(v.values()) == 0 for v in item['catalogue_shape']['field_types'].values()))

    def test_unknown_selection_refused_before_credential_or_traffic(self):
        from decision_kernel.application.market_source_check import check_sources
        client = RelayHTTP(opener=Opener(), credential='')
        for value in ('all', [], None, False):
            with self.subTest(value=value), self.assertRaisesRegex(RecordError, 'MARKET_CHECK_SELECTION'):
                check_sources(registry=REGISTRY, exchange='SSE', start=START, end=END,
                    access_confirmed=True, selection=value, client=client, now=NOW)
        self.assertEqual(client.requests, 0)

    def test_transport_classification_preserved_without_error_prose(self):
        from urllib.error import URLError
        class Unavailable(Opener):
            def open(self, request, timeout):
                self.calls.append('attempt')
                raise URLError(TimeoutError(SECRET_TEXT))
        out = self.run_check(Unavailable())
        self.assertEqual(out['http_attempts'], 8)
        self.assertEqual(self.waits, [30] * 6)
        for item in out['queries'].values():
            self.assertEqual(item['reason_code'], 'RELAY_HTTP_UNAVAILABLE')
            self.assertEqual(item['last_attempt_failure'],
                {'classification': 'timeout', 'http_status': None, 'business_error': None})
        self.assertNotIn(SECRET_TEXT, json.dumps(self.receipts))

    def test_recovered_failure_does_not_override_final_query_success(self):
        opener = CatalogueOpener(); opener.transient = True
        out = self.run_check(opener)
        first = out['queries']['sector_catalog']
        self.assertEqual(first['status'], 'checked')
        self.assertIsNone(first['reason_code'])
        self.assertEqual(first['last_attempt_failure'], {'classification': 'temporary-upstream',
            'http_status': 504, 'business_error': 'upstream_timeout'})
        self.assertIsNone(out['queries']['concept_catalog']['last_attempt_failure'])
        self.assertEqual(out['status'], 'checked-catalogues')
        self.assertEqual(self.waits, [30])

    def test_targeted_auth_refusal_stops_second_catalogue(self):
        opener = CatalogueOpener(); opener.fail = 'ths_index'
        out = self.run_check(opener)
        self.assertEqual(out['http_attempts'], 1)
        self.assertEqual(out['queries']['sector_catalog']['last_attempt_failure']['classification'], 'auth-or-entitlement')
        self.assertEqual(out['queries']['concept_catalog']['reason_code'], 'GLOBAL_SOURCE_STOP')
        self.assertTrue(out['stopped'])

    def test_failure_projection_has_closed_keys_and_enums(self):
        from decision_kernel.application.market_source_check import _last_attempt_failure
        capture = {'pages': [], 'attempts': [{'failure': {
            'classification': SECRET_TEXT, 'http_status': True, 'business_error': SECRET_TEXT,
            'x_request_id': SECRET_TEXT, 'message': SECRET_TEXT, 'secret': SECRET_TEXT}}]}
        self.assertEqual(_last_attempt_failure(capture),
            {'classification': 'unclassified', 'http_status': None, 'business_error': None})
        self.assertIsNone(_last_attempt_failure({'pages': [], 'attempts': []}))
        self.assertNotIn(SECRET_TEXT, json.dumps(_last_attempt_failure(capture)))

    def test_unknown_code_is_still_masked_in_targeted_check(self):
        with patch('decision_kernel.application.market_source_check.qualified_ths_catalog',
                   side_effect=RecordError('MARKET_RELAY_GROUP_COUNT_' + SECRET_TEXT)):
            out = self.run_check()
        for item in out['queries'].values():
            self.assertEqual(item['reason_code'], 'CHECK_REJECTED')
        self.assertNotIn(SECRET_TEXT, json.dumps(self.receipts))

    def test_host_selection_is_optional_and_targeted_result_is_written(self):
        with tempfile.TemporaryDirectory() as td:
            event, env = original.MarketCheckHostTests().context(td)
            self.assertEqual(settings(event, env, NOW)['selection'], 'sample')
            event['inputs']['selection'] = 'catalogues'
            opener = CatalogueOpener()
            client = RelayHTTP(opener=opener, credential='synthetic-key-only')
            def checker(**kw):
                return check_sources(**kw, client=client, clock=lambda: NOW.isoformat())
            out = Path(td) / 'targeted'
            self.assertEqual(execute(event, env, out, now=NOW, checker=checker, registry_path=REGISTRY_PATH), 0)
            self.assertEqual([p.name for p in out.iterdir()], ['result.json'])
            receipt = json.loads((out / 'result.json').read_text())
            self.assertEqual(receipt['status'], 'checked-catalogues')
            self.assertEqual(receipt['http_attempts'], 2)
            self.assertEqual(receipt['identity']['run_id'], '123')
            self.assertNotIn(SECRET_TEXT, json.dumps(receipt))
            event['inputs']['selection'] = 'other'
            with self.assertRaisesRegex(RecordError, 'MARKET_CHECK_SELECTION'):
                settings(event, env, NOW)
