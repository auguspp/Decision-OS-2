"""Strict predicates retained; explicit isolation and original custody/CLI are real consumers."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from decision_kernel.application.market_security_inputs import inspect_security_master, qualified_security_master
from decision_kernel.application.market_group_inputs import inspect_ths_catalog, qualified_ths_catalog
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, MissingObject, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.market_daily import MarketInputError
from test_market_security_inputs import basic_row, result_for
from test_market_group_inputs import catalog_row, catalogue, levels, members, group_input
from test_market_custody import security_payload, MemoryBackend
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR, CODE, captured, CALENDAR

BAD = '600001.SH'
OTHER_GROUP = '881002.TI'


def stocks(rows=None):
    out = security_payload()
    out['reference_policy'] = 'isolate-identified'
    out['master_results'] = {'L': result_for('stock_basic', rows if rows is not None else
        [basic_row(), basic_row(BAD, delisted='20251001')])}
    return out


def groups():
    out = group_input()
    first = catalog_row()
    different = catalog_row(count=2)
    out.update(schema='relay-ths-group-reading-v1', kind='sector', reference_policy='isolate-identified',
        catalog_result=catalogue([first, catalog_row(OTHER_GROUP), different]),
        index_results={first[0]: levels(first[0]), OTHER_GROUP: levels(OTHER_GROUP)},
        member_results={first[0]: members(first[0]), OTHER_GROUP: members(OTHER_GROUP)})
    return out


class ReferenceIsolationTests(unittest.TestCase):
    def assert_quality(self, quality, rows, scope, eligible, quarantined):
        self.assertEqual(quality['source_row_count'], rows)
        self.assertEqual(quality['scope_count'], scope)
        self.assertEqual(quality['eligible_count'], eligible)
        self.assertEqual(quality['quarantined_count'], quarantined)
        self.assertEqual(eligible + quarantined, scope)
        self.assertEqual(eligible + quality['quarantined_row_count'], rows)

    def test_original_strict_master_refuses_same_input_isolation_accounts_for(self):
        data = stocks(); before = encoded(data)
        with self.assertRaisesRegex(MarketInputError, 'MASTER_DELIST_DATE'):
            qualified_security_master(data['master_results'], registry=REGISTRY, exchange='SSE')
        view = read_market_payload(data, REGISTRY)
        self.assert_quality(view['master']['quality'], 2, 2, 1, 1)
        for convention in ('raw', 'qfq'):
            entries = {e['entity_id']: e for e in view[convention]['entries']}
            self.assertIn(entries[CODE]['status'], ('observed', 'partial-history'))
            self.assertEqual(entries[BAD]['status'], 'no-observation-at-date')
            self.assertFalse(any('change_pct' in h for h in entries[BAD]['horizons']))
            self.assertIsNone(entries[BAD]['security_reference'])
            self.assertIsNone(entries[BAD]['qualification']['within_reported_listing_at_end'])
            self.assertEqual(entries[BAD]['qualification']['master']['reason_codes'], ['MASTER_DELIST_DATE'])
            self.assertEqual(view[convention]['scope_count'], 2)
        self.assertEqual(encoded(data), before)

    def test_strict_default_and_explicit_strict_are_byte_equal(self):
        data = security_payload()
        original = read_market_payload(data, REGISTRY)
        self.assertEqual(encoded(original), encoded(read_market_payload({**data, 'reference_policy':'strict'}, REGISTRY)))
        self.assertNotIn('quality', original['master'])
        bad = stocks(); bad.pop('reference_policy')
        with self.assertRaises(MarketInputError): read_market_payload(bad, REGISTRY)

    def test_all_original_bad_value_predicates_quarantine_without_repair(self):
        for i, val in [(1,'777777'), (2,None), (2,' padded'), (3,'SZSE'), (4,'USD'), (5,'D'),
                       (6,None), (6,'20260230'), (7,'20251001'), (7,'bad-date')]:
            row=basic_row(BAD); row[i]=val
            with self.subTest(i=i,val=val):
                view=read_market_payload(stocks([basic_row(), row]),REGISTRY)
                self.assert_quality(view['master']['quality'],2,2,1,1)
                self.assertIsNone(next(e for e in view['raw']['entries'] if e['entity_id']==BAD)['security_reference'])

    def test_all_duplicate_occurrences_quarantined_across_pages_and_statuses(self):
        for identical in (True, False):
            row=basic_row(BAD); other=deepcopy(row)
            if not identical: other[2]='不同名称'
            data=stocks([row,basic_row(),other])
            before=encoded(data)
            out=inspect_security_master(data['master_results'],registry=REGISTRY,exchange='SSE')
            self.assert_quality(out['quality'],3,2,1,1)
            self.assertEqual(out['quality']['duplicate_group_count'],1)
            self.assertIsNone(out['securities'][BAD])
            self.assertEqual(out['qualification'][BAD]['source_row_count'],2)
            self.assertEqual(encoded(data),before)
        data=stocks([basic_row(),basic_row(BAD)])
        data['master_results']['P']=result_for('stock_basic',[basic_row(BAD,status='P')],status='P')
        out=inspect_security_master(data['master_results'],registry=REGISTRY,exchange='SSE')
        self.assert_quality(out['quality'],3,2,1,1)
        self.assertIsNone(out['securities'][BAD])

    def test_canonical_identity_unknown_or_wrong_venue_is_still_global_refusal(self):
        for value in ('unknown','000001.SZ'):
            row=basic_row(BAD);row[0]=value
            with self.subTest(value=value),self.assertRaises(MarketInputError):
                read_market_payload(stocks([basic_row(),row]),REGISTRY)

    def test_incomplete_or_mutated_capture_is_not_partial_market_authority(self):
        data=stocks()
        data['master_results']['L']=result_for('stock_basic',[basic_row(),basic_row(BAD)],max_pages=1)
        with self.assertRaises(MarketInputError): read_market_payload(data,REGISTRY)
        for section in ('use','capture'):
            data=stocks();data['master_results']['L'][section]['parameters']['exchange']='SZSE'
            with self.subTest(section=section),self.assertRaises(MarketInputError): read_market_payload(data,REGISTRY)

    def test_valid_quotes_do_not_bypass_quarantined_master(self):
        data=stocks()
        from test_market_relay_inputs import PRICES
        data['stock_results'][BAD]=captured('daily',[[BAD,r[1],r[2]] for r in PRICES],params={'ts_code':BAD})
        data['factor_results'][BAD]=result_for('adj_factor',[[BAD,r[1],'1'] for r in PRICES],code=BAD)
        view=read_market_payload(data,REGISTRY,entity_id=BAD)
        self.assertEqual(view['raw']['entries'][0]['status'],'no-observation-at-date')
        self.assertFalse(any('change_pct' in h for h in view['raw']['entries'][0]['horizons']))
        self.assertIsNone(view['qfq']['entries'][0]['factor_lineage'])
        self.assertEqual(view['raw']['scope_count'],2)

    def test_pagination_and_selection_retain_denominator_and_version(self):
        data=stocks(); full=read_market_payload(data,REGISTRY)
        for offset in (0,1,2):
            page=read_market_payload(data,REGISTRY,offset=offset,limit=1)
            self.assertEqual(page['master'],full['master'])
            self.assertEqual(page['raw']['scope_count'],2)
            self.assertEqual(page['raw']['source_version'],full['raw']['source_version'])
            self.assertEqual(page['raw']['entries'],full['raw']['entries'][offset:offset+1])

    def test_all_quarantine_and_empty_are_not_missing_scope(self):
        data=stocks([basic_row(BAD,delisted='20250101')]);data['stock_results']={};data['factor_results']={}
        out=read_market_payload(data,REGISTRY)
        self.assertEqual(out['raw']['scope_count'],1);self.assertEqual(out['raw']['observed_count'],0)
        data['master_results']={'L':result_for('stock_basic',[],empty_fields=True)}
        out=read_market_payload(data,REGISTRY)
        self.assertEqual(out['raw']['scope_count'],0);self.assertEqual(out['master']['status_counts'],{'L':0})

    def test_unknown_policy_refused(self):
        for val in (None,True,[],{},'skip-errors'):
            data=stocks(); data['reference_policy']=val
            with self.subTest(val=val),self.assertRaises(MarketInputError): read_market_payload(data,REGISTRY)

    def test_ths_conflict_does_not_select_first_or_last(self):
        data=groups();before=encoded(data)
        with self.assertRaisesRegex(MarketInputError,'GROUP_DUPLICATE_CODE'):
            qualified_ths_catalog(data['catalog_result'],registry=REGISTRY,kind='sector')
        out=read_market_payload(data,REGISTRY)
        self.assert_quality(out['catalogue']['quality'],3,2,1,1)
        bad,good=out['entries']
        self.assertEqual(bad['status'],'no-observation-at-date');self.assertIsNone(bad['classification']);self.assertIsNone(bad['member_snapshot'])
        self.assertIn(good['status'],('observed','partial-history'));self.assertIsNotNone(good['member_snapshot'])
        self.assertEqual(encoded(data),before)

    def test_ths_single_identifiable_bad_metadata_only_limits_own_group(self):
        for i,val in [(1,None),(2,2.5),(3,'HK'),(4,'bad-date'),(5,'N')]:
            row=catalog_row();row[i]=val
            with self.subTest(i=i,val=val):
                result=catalogue([row,catalog_row(OTHER_GROUP)])
                out=inspect_ths_catalog(result,registry=REGISTRY,kind='sector')
                self.assert_quality(out['quality'],2,2,1,1)
                self.assertIsNone(out['groups'][row[0]])

    def test_ths_unidentifiable_and_incomplete_inputs_refuse_shared_scope(self):
        for result in (catalogue([catalog_row('bad-code')]),
                       catalogue([catalog_row(),catalog_row(OTHER_GROUP)],max_pages=1)):
            with self.assertRaises(MarketInputError): inspect_ths_catalog(result,registry=REGISTRY,kind='sector')

    def test_ths_identical_duplicate_is_not_silently_deduplicated(self):
        out=inspect_ths_catalog(catalogue([catalog_row(),catalog_row()]),registry=REGISTRY,kind='sector')
        self.assert_quality(out['quality'],2,1,0,1)

    def test_scope_quality_is_order_independent_without_rewriting_original(self):
        rows=[basic_row(),basic_row(BAD),basic_row(BAD,delisted='20250101')]
        a=inspect_security_master({'L':result_for('stock_basic',rows)},registry=REGISTRY,exchange='SSE')
        b=inspect_security_master({'L':result_for('stock_basic',list(reversed(rows)))},registry=REGISTRY,exchange='SSE')
        self.assertEqual(a['quality'],b['quality']);self.assertEqual(a['qualification'],b['qualification'])

    def test_three_surface_composition_keeps_policy_and_independent_failures(self):
        stock=stocks();stock.pop('schema');sector=groups();sector.pop('schema');sector.pop('kind')
        out=read_market_payload({'schema':'relay-three-market-reading-v1',
                                'surfaces':{'sector':sector,'stock':stock}},REGISTRY)
        self.assertEqual(out['surfaces']['stock']['status'],'read')
        self.assertEqual(out['surfaces']['sector']['status'],'read')
        self.assertEqual(out['surfaces']['concept']['status'],'not-supplied')
        sector['catalog_result']={}
        out=read_market_payload({'schema':'relay-three-market-reading-v1',
                                'surfaces':{'sector':sector,'stock':stock}},REGISTRY)
        self.assertEqual(out['surfaces']['stock']['status'],'read')
        self.assertEqual(out['surfaces']['sector']['status'],'rejected')


class AvailabilityCustodyTests(unittest.TestCase):
    def setUp(self):
        self.backend=MemoryBackend();self.store=VersionedStore(self.backend)
    def fresh(self):return VersionedStore(MemoryBackend(self.backend.shared))
    def save(self,data,**kwargs):
        return retain_market_reading(data,REGISTRY,self.store,fresh_reader=self.fresh,
                                     **{'public_rights_confirmed':True,**kwargs})

    def test_original_manifests_retain_policy_and_all_conflicting_rows(self):
        for data in (stocks(),groups()):
            before=encoded(data); original=read_market_payload(data,REGISTRY)
            saved=self.save(data);restored=read_stored_market(saved['reference'],REGISTRY,self.fresh())
            self.assertEqual(encoded(restored['view']),encoded(original))
            self.assertEqual(encoded(data),before)
            manifest=json.loads(self.store.read_source(SourceRef.from_dict(saved['reference']))['data'])
            self.assertEqual(manifest['reading']['reference_policy'],'isolate-identified')

    def test_no_rights_means_no_write_and_strict_default_does_not_accept_conflict(self):
        head=self.backend.head()
        with self.assertRaisesRegex(RecordError,'PUBLIC_RIGHTS'):self.save(stocks(),public_rights_confirmed=False)
        self.assertEqual(head,self.backend.head())
        data=stocks();data.pop('reference_policy')
        with self.assertRaises(MarketInputError):self.save(data)
        self.assertEqual(head,self.backend.head())

    def test_missing_master_source_degrades_to_unknown_without_http(self):
        saved=self.save(stocks());ref=SourceRef.from_dict(saved['reference'])
        manifest=json.loads(self.store.read_source(ref)['data']);query=next(iter(manifest['queries'].values()))
        # All stored inputs remain required. A missing query is not an empty stock market.
        original=self.backend.read
        def missing(commit,path):
            if path==query['manifest']['path']:raise MissingObject('synthetic missing source')
            return original(commit,path)
        fresh=self.fresh();fresh.backend.read=missing
        with patch('decision_kernel.infrastructure.relay_http.RelayHTTP',side_effect=AssertionError('network forbidden')):
            out=read_stored_market(saved['reference'],REGISTRY,fresh)
        self.assertEqual(out['status'],'partial-restoration')

    def test_real_git_new_process_recovery_after_original_input_removed(self):
        data=stocks()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=root/'records.git';GitStore.init_synthetic(path)
            def fresh():return VersionedStore(GitStore(path))
            store=fresh();saved=retain_market_reading(data,REGISTRY,store,fresh_reader=fresh,public_rights_confirmed=True)
            original=root/'input.json';original.write_bytes(encoded(data))
            env={**os.environ,'PYTHONPATH':str(ROOT_DIR/'src')}
            plain=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.market_relay_cli',
                '--input',str(original),'--registry',str(REGISTRY_PATH),'--entity-id',BAD],
                cwd=root,env=env,text=True,capture_output=True,timeout=20)
            self.assertEqual(plain.returncode,0,plain.stderr)
            original.unlink();ref=root/'root.json';ref.write_bytes(encoded(saved['reference']))
            read=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.market_custody_cli','read',
                '--path',str(path),'--input',str(ref),'--registry',str(REGISTRY_PATH),'--entity-id',BAD],
                cwd=root,env=env,text=True,capture_output=True,timeout=20)
            self.assertEqual(read.returncode,0,read.stderr)
            restored=json.loads(read.stdout)
            self.assertEqual(restored['view'],json.loads(plain.stdout))
            self.assertEqual(restored['view']['raw']['scope_count'],2)
            self.assertEqual(restored['view']['raw']['entries'][0]['status'],'no-observation-at-date')
