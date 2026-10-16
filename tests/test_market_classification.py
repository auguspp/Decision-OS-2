"""Classification failures remain failures; diagnosis never returns supplier rows."""
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
import tempfile
import unittest

from decision_kernel.application.market_group_inputs import (
    CLASSIFICATION_CODES, _classification_checks, qualified_ths_catalog,
    read_three_market_surfaces,
)
from decision_kernel.application.market_relay_inputs import MarketRelayError
from decision_kernel.application.market_source_check import _catalogue_shape, check_sources
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute
from test_market_diagnostics import CatalogueOpener
from test_market_group_inputs import catalogue, catalog_row, group_input, stock_input, SECTOR
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH
from test_market_source_check import NOW, START, END, SECRET_TEXT
import test_market_source_check as original

PREFIX = 'MARKET_RELAY_'


class MarketClassificationTests(unittest.TestCase):
    def shape(self, result, kind='I'):
        return _catalogue_shape(inspect_result(result, REGISTRY), kind)['classification']

    def test_specific_codes_and_first_failure_order(self):
        for column, value, reason in ((3, 'HK', 'GROUP_EXCHANGE_MISMATCH'),
                                      (5, 'N', 'GROUP_TYPE_MISMATCH'),
                                      (0, 'bad-code', 'GROUP_CODE')):
            row = catalog_row(); row[column] = value
            result = catalogue([row]); before = encoded(result)
            with self.subTest(reason=reason), self.assertRaisesRegex(MarketRelayError, '^'+PREFIX+reason+'$'):
                qualified_ths_catalog(result, registry=REGISTRY, kind='sector')
            self.assertEqual(encoded(result), before)
        result = catalogue([catalog_row(), catalog_row()])
        with self.assertRaisesRegex(MarketRelayError, '^'+PREFIX+'GROUP_DUPLICATE_CODE$'):
            qualified_ths_catalog(result, registry=REGISTRY, kind='sector')

    def test_shared_predicates_preserve_original_acceptance(self):
        for code, exchange, kind, duplicate in product(
                (SECTOR, 'invalid', None, [], False), ('A', 'HK', None), ('I', 'N', None), (True, False)):
            row = {'ts_code': code, 'exchange': exchange, 'type': kind}
            seen = {SECTOR: {}} if duplicate else {}
            checks = dict(_classification_checks(row, seen, 'I'))
            expected = code == SECTOR and not duplicate and exchange == 'A' and kind == 'I'
            with self.subTest(code=code, exchange=exchange, kind=kind, duplicate=duplicate):
                self.assertEqual(tuple(checks), CLASSIFICATION_CODES)
                self.assertIs(all(checks.values()), expected)
                self.assertTrue(all(type(v) is bool for v in checks.values()))

    def test_counts_cover_all_rows_and_overlapping_failures(self):
        first = catalog_row(count=1.0); first[3], first[5] = 'HK', 'N'
        changed = deepcopy(first); changed[1] = SECRET_TEXT
        invalid = catalog_row(); invalid[0] = 'private-invalid-id'
        missing = catalog_row('881002.TI'); missing[3], missing[5] = None, ''
        result = catalogue([first, deepcopy(first), changed, invalid, missing], limit=2)
        before = encoded(result)
        out = self.shape(result)
        self.assertEqual(out['rows_checked'], 5)
        self.assertEqual(out['rows_with_issues'], 5)
        self.assertEqual(out['reason_counts'], dict(zip(
            (PREFIX+c for c in CLASSIFICATION_CODES), (1, 2, 4, 4))))
        self.assertEqual(out['duplicate_identical_rows'], 1)
        self.assertEqual(out['duplicate_different_rows'], 1)
        self.assertEqual(out['missing_exchange_rows'], 1)
        self.assertEqual(out['missing_type_rows'], 1)
        self.assertEqual(encoded(result), before)
        for value in (SECRET_TEXT, SECTOR, 'private-invalid-id', '20000101', 'HK'):
            self.assertNotIn(value, json.dumps(out))

    def test_duplicate_detection_spans_pages_and_compares_raw_types(self):
        rows = [catalog_row(count=1), catalog_row('881002.TI'),
                catalog_row(count=1.0), catalog_row(count=1)]
        result = catalogue(rows, limit=2)
        out = self.shape(result)
        self.assertEqual(out['rows_checked'], 4)
        self.assertEqual(out['reason_counts'][PREFIX+'GROUP_DUPLICATE_CODE'], 2)
        self.assertEqual(out['duplicate_identical_rows'], 1)
        self.assertEqual(out['duplicate_different_rows'], 1)
        self.assertEqual(out['rows_with_issues'], 2)
        self.assertEqual(out['coverage'], 'complete-capture-not-qualified')

    def test_invalid_identifiers_are_not_duplicate_candidates(self):
        rows = [catalog_row(), catalog_row()]
        rows[0][0] = rows[1][0] = None
        out = self.shape(catalogue(rows))
        self.assertEqual(out['reason_counts'][PREFIX+'GROUP_CODE'], 2)
        self.assertEqual(out['reason_counts'][PREFIX+'GROUP_DUPLICATE_CODE'], 0)

    def test_incomplete_and_empty_counters_do_not_certify_complete_scope(self):
        result = catalogue([catalog_row(), catalog_row('881002.TI'), catalog_row('881003.TI')],
                           limit=2, max_pages=1)
        out = self.shape(result)
        self.assertEqual(out['coverage'], 'received-pages-only')
        self.assertEqual(out['rows_checked'], 2)
        with self.assertRaises(MarketRelayError):
            qualified_ths_catalog(result, registry=REGISTRY, kind='sector')
        empty = catalogue([], empty_fields=True)
        out = self.shape(empty)
        self.assertEqual(out['rows_checked'], 0)
        self.assertEqual(out['rows_with_issues'], 0)
        self.assertEqual(sum(out['reason_counts'].values()), 0)
        self.assertEqual(qualified_ths_catalog(empty, registry=REGISTRY, kind='sector')['scope_count'], 0)

    def test_predicates_do_not_mutate_seen_map_or_source(self):
        result = catalogue([catalog_row(count=1.0)])
        before = encoded(result)
        self.assertEqual(self.shape(result)['rows_with_issues'], 0)
        qualified_ths_catalog(result, registry=REGISTRY, kind='sector')
        self.assertEqual(encoded(result), before)
        seen = {SECTOR: {'name': SECRET_TEXT}}
        copied = deepcopy(seen)
        _classification_checks({'ts_code': SECTOR, 'type': 'I', 'exchange': 'A'}, seen, 'I')
        self.assertEqual(seen, copied)

    def run_check(self, changes, selection='catalogues', progress=None):
        opener = CatalogueOpener(changes)
        client = RelayHTTP(opener=opener, credential='synthetic-key-only')
        out = check_sources(registry=REGISTRY, exchange='SSE', start=START, end=END,
            access_confirmed=True, selection=selection, client=client, now=NOW,
            clock=lambda: NOW.isoformat(), progress=progress)
        return out, opener

    def test_targeted_expected_type_is_owned_by_each_request(self):
        receipts = []
        out, opener = self.run_check({'type': 'N'}, progress=receipts.append)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(out['status'], 'completed-with-gaps')
        self.assertEqual(out['queries']['sector_catalog']['reason_code'], PREFIX+'GROUP_TYPE_MISMATCH')
        self.assertEqual(out['queries']['concept_catalog']['status'], 'checked')
        for slot, expected in (('sector_catalog', 1), ('concept_catalog', 0)):
            shape = out['queries'][slot]['catalogue_shape']['classification']
            self.assertEqual(shape['reason_counts'][PREFIX+'GROUP_TYPE_MISMATCH'], expected)
            self.assertEqual(shape['reason_counts'][PREFIX+'GROUP_DUPLICATE_CODE'], 0)
        for value in (SECRET_TEXT, '880001.TI', '885001.TI', 'wire_sha256', 'synthetic-key-only'):
            self.assertNotIn(value, json.dumps(receipts))

    def test_new_refusals_keep_independent_stock_and_concept(self):
        bad = group_input(); row = catalog_row(); row[3] = None
        bad['catalog_result'] = catalogue([row])
        out = read_three_market_surfaces(surfaces={'sector': bad,
            'concept': group_input('concept'), 'stock': stock_input()}, registry=REGISTRY)
        self.assertEqual(out['surfaces']['sector']['reason_code'], PREFIX+'GROUP_EXCHANGE_MISMATCH')
        self.assertEqual(out['surfaces']['concept']['view']['observed_count'], 1)
        self.assertEqual(out['surfaces']['stock']['view']['raw']['observed_count'], 1)
        out, _ = self.run_check({'type': 'N'}, selection='sample')
        self.assertEqual(out['surfaces']['stock']['observed_count'], 1)
        self.assertEqual(out['surfaces']['concept']['observed_count'], 1)

    def test_host_persists_only_fixed_counter_keys_after_rejection(self):
        with tempfile.TemporaryDirectory() as td:
            event, env = original.MarketCheckHostTests().context(td)
            event['inputs']['selection'] = 'catalogues'
            client = RelayHTTP(opener=CatalogueOpener({'exchange': SECRET_TEXT, 'type': None}),
                               credential='synthetic-key-only')
            def checker(**kw):
                return check_sources(**kw, client=client, clock=lambda: NOW.isoformat())
            dest = Path(td)/'result'
            self.assertEqual(execute(event, env, dest, now=NOW, checker=checker, registry_path=REGISTRY_PATH), 2)
            self.assertEqual([p.name for p in dest.iterdir()], ['result.json'])
            text = (dest/'result.json').read_text()
            self.assertNotIn(SECRET_TEXT, text)
            for item in json.loads(text)['queries'].values():
                counts = item['catalogue_shape']['classification']
                self.assertEqual(counts['rows_with_issues'], 1)
                self.assertEqual(counts['missing_type_rows'], 1)
                self.assertEqual(counts['missing_exchange_rows'], 0)
                self.assertEqual(set(counts['reason_counts']), {PREFIX+c for c in CLASSIFICATION_CODES})
                self.assertEqual(item['reason_code'], PREFIX+'GROUP_EXCHANGE_MISMATCH')


if __name__ == '__main__':
    unittest.main()
