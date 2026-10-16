"""Synthetic source custody, exact restoration and real local Git/subprocess consumers."""
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

from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market, restore_market_query, read_market_payload,
    _manifest_source, QUERY_SCHEMA, READING_SCHEMA, _prepare_query,
)
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, Conflict, MissingObject, RecordError, Selection, SourceInput, SourceRef,
    UnknownWrite, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.market_daily import MarketInputError
from test_market_relay_inputs import (REGISTRY, REGISTRY_PATH, ROOT_DIR, CODE, START, END,
                                     CALENDAR, PRICES, SyntheticOpener, captured)


def extra(api, fields, rows, params):
    request = {'api': api, 'stage': 'baseline', 'purpose': 'synthetic custody test',
               'parameters': {**params, 'fields': ','.join(fields), 'limit': '100'}}
    return list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-market',
        'requests': [request]}, REGISTRY,
        client=RelayHTTP(opener=SyntheticOpener(api, fields, rows), credential='synthetic-key-only'),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: '2026-10-16T02:00:00+00:00'))[0]


def raw_payload():
    return {'schema': 'relay-stock-reading-v1', 'calendar_result': captured('trade_cal', CALENDAR, limit=100),
        'stock_results': {CODE: captured('daily', PRICES, limit=100)}, 'exchange': 'SSE',
        'start': START, 'end': END, 'declared_entities': [CODE, '600001.SH'],
        'coverage': 'partial-universe', 'horizons': [1, 3, 5]}


def security_payload():
    out = raw_payload();out['schema'] = 'relay-security-stock-reading-v1'
    out.pop('declared_entities');out.pop('coverage')
    fields = ['ts_code','symbol','name','exchange','curr_type','list_status','list_date','delist_date']
    out['master_results'] = {'L': extra('stock_basic', fields,
        [[CODE, CODE[:6], '合成证券', 'SSE', 'CNY', 'L', '20000101', None]], {'exchange':'SSE','list_status':'L'})}
    out['factor_results'] = {CODE: extra('adj_factor', ['ts_code','trade_date','adj_factor'],
        [[CODE, r[1], '1'] for r in PRICES], {'ts_code':CODE,'start_date':START,'end_date':END})}
    return out


def group_payload(kind='sector'):
    code = '885001.TI' if kind == 'sector' else '885002.TI'
    typ = 'I' if kind == 'sector' else 'N'
    return {'schema': 'relay-ths-group-reading-v1', 'kind': kind,
        'catalog_result': extra('ths_index', ['ts_code','name','count','exchange','list_date','type'],
            [[code,'合成分类',1,'A',None,typ]], {'exchange':'A','type':typ}),
        'index_results': {code: extra('ths_daily', ['ts_code','trade_date','close'],
            [[code,r[1],r[2]] for r in PRICES], {'ts_code':code,'start_date':START,'end_date':END})},
        'member_results': {code: extra('ths_member', ['ts_code','con_code','con_name'],
            [[code,CODE,'合成证券']], {'ts_code':code})},
        'calendar_result': raw_payload()['calendar_result'], 'exchange':'SSE',
        'start':START,'end':END,'horizons':[1,3,5]}


def three_payload():
    surfaces = {'sector': group_payload(), 'concept':group_payload('concept'), 'stock':security_payload()}
    return {'schema': 'relay-three-market-reading-v1', 'surfaces':
            {k:{f:v for f,v in p.items() if f not in ('schema','kind')} for k,p in surfaces.items()}}


class MemoryBackend:
    """Only a fault-injectable fixture. Production writes always use the existing store."""
    repository = 'synthetic-git:market-custody-test'
    def __init__(self, shared=None):
        self.shared = shared if shared is not None else {'head':'0'*40,'trees':{'0'*40:{}},'count':0,'missing':set()}
    def head(self): return self.shared['head']
    def check_commit(self, commit):
        if commit not in self.shared['trees']: raise MissingObject('OBJECT_MISSING')
    def read(self, commit, path):
        if (commit,path) in self.shared['missing']: raise MissingObject('OBJECT_MISSING')
        try: return self.shared['trees'][commit][path]
        except KeyError: raise MissingObject('OBJECT_MISSING') from None
    def paths(self, commit, prefix=''): return [p for p in self.shared['trees'][commit] if p.startswith(prefix)]
    def last_change(self, head, path):
        raw = self.read(head,path)
        return next(k for k,v in self.shared['trees'].items() if v.get(path) == raw)
    def reference(self, commit, path): return BlobRef(self.repository,commit,path,digest(self.read(commit,path)))
    def resolve(self, ref):
        if ref.repository != self.repository: raise RecordError('FOREIGN_REPOSITORY')
        raw = self.read(ref.commit,ref.path)
        if digest(raw) != ref.sha256: raise RecordError('BLOB_DIGEST_MISMATCH')
        return raw
    def commit(self, files, *, expected):
        if self.head() != expected: raise Conflict('HEAD_CHANGED')
        self.shared['count'] += 1
        head = f"{self.shared['count']:040x}"
        self.shared['trees'][head] = {**self.shared['trees'][expected],**files}
        self.shared['head'] = head
        return head


class CustodyTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend();self.store = VersionedStore(self.backend)
    def fresh(self): return VersionedStore(MemoryBackend(self.backend.shared))
    def save(self, payload=None, **kwargs):
        return retain_market_reading(raw_payload() if payload is None else payload, REGISTRY,self.store,
            **{'fresh_reader':self.fresh,'public_rights_confirmed':True,**kwargs})
    def read(self, ref, **kwargs): return read_stored_market(ref,REGISTRY,self.fresh(),**kwargs)
    def root(self, ref): return json.loads(self.store.read_source(SourceRef.from_dict(ref))['data'])
    def put_manifest(self, schema, data):
        return asdict(self.store.put_source('tamper-'+digest(encoded(data)),_manifest_source(schema,data)))

    def test_all_existing_routes_roundtrip_without_mutation(self):
        for payload in (raw_payload(), security_payload(), group_payload(), three_payload()):
            with self.subTest(schema=payload['schema']):
                before = encoded(payload);saved = self.save(payload)
                self.assertEqual(saved['status'],'stored-and-independently-restored')
                self.assertEqual(encoded(saved['restored']['view']),encoded(read_market_payload(payload,REGISTRY)))
                self.assertEqual(encoded(payload),before)
                self.assertFalse(saved['restored']['original_http_restored'])
                self.assertEqual(saved['restored']['source_requests'],0)

    def test_page_exports_are_not_duplicated_in_query_or_reading_manifest(self):
        saved=self.save();root=self.root(saved['reference'])
        self.assertNotIn('table', encoded(root).decode())
        for ref in root['queries'].values():
            query=self.root(ref)
            self.assertNotIn('items',encoded(query).decode())
            restored=restore_market_query(ref,REGISTRY,self.fresh())
            self.assertEqual(restored['execution_diagnostics'],'not-retained')
            self.assertEqual(restored['result']['capture']['pages'][0]['attempts'],[])

    def test_repeated_identical_save_uses_existing_journal(self):
        first=self.save();head=self.backend.head();second=self.save()
        self.assertEqual(first['reference'],second['reference'])
        self.assertEqual(self.backend.head(),head)

    def test_source_revision_preserves_old_reference_and_changes_new_version(self):
        first=self.save();payload=raw_payload();rows=deepcopy(PRICES);rows[-1][-1]='120'
        payload['stock_results'][CODE]=captured('daily',rows,limit=100)
        second=self.save(payload)
        self.assertNotEqual(first['reference'],second['reference'])
        self.assertEqual(self.read(first['reference'])['view'],first['restored']['view'])
        self.assertNotEqual(first['restored']['view']['source_version'],second['restored']['view']['source_version'])

    def test_rights_and_real_fresh_reader_required_before_first_write(self):
        for kw in ({'public_rights_confirmed':False},{'public_rights_confirmed':'true'},
                   {'fresh_reader':lambda:self.store},{'fresh_reader':lambda:VersionedStore(self.backend)}):
            with self.subTest(kw=kw), self.assertRaises(RecordError): self.save(**kw)
            self.assertEqual(self.backend.shared['count'],0)

    def test_all_supplied_captures_preflight_before_any_write(self):
        payload=raw_payload();payload['stock_results'][CODE]['capture']['complete']=False
        with self.assertRaises((RecordError,MarketInputError)):self.save(payload)
        self.assertEqual(self.backend.shared['count'],0)
        payload=raw_payload();payload['stock_results'][CODE]['use']['api']='p_list'
        with self.assertRaises((RecordError,MarketInputError)):self.save(payload)
        self.assertEqual(self.backend.shared['count'],0)

    def test_unknown_write_stops_then_same_intent_reconciles(self):
        original=self.backend.commit;calls=[]
        def uncertain(files,*,expected):
            calls.append(files);value=original(files,expected=expected)
            if len(calls)==1:raise UnknownWrite('REF_UPDATE_UNCONFIRMED')
            return value
        with patch.object(self.backend,'commit',side_effect=uncertain):
            with self.assertRaises(UnknownWrite):self.save()
        self.assertEqual(len(calls),1)
        self.assertFalse(any('Retained market reading' in v.decode() for v in self.backend.shared['trees'][self.backend.head()].values()))
        saved=self.save();self.assertEqual(saved['status'],'stored-and-independently-restored')
        count=self.backend.shared['count'];self.save();self.assertEqual(self.backend.shared['count'],count)

    def test_page_failure_preserves_denominator_and_other_queries(self):
        saved=self.save();root=self.root(saved['reference'])
        query_ref=next(ref for ref in root['queries'].values() if self.root(ref)['use']['api']=='daily')
        page=self.root(query_ref)['pages'][0]['reference']['manifest']
        self.backend.shared['missing'].add((page['commit'],page['path']))
        out=self.read(saved['reference'])
        self.assertEqual(out['status'],'partial-restoration');self.assertEqual(out['unavailable_queries'],1)
        self.assertEqual(out['view']['scope_count'],2)
        self.assertEqual(out['view']['source_qualification_counts'],{'supplied':1,'accepted':0,'rejected':1,'not_supplied':1})
        self.assertEqual(next(x for x in out['sources'] if x['status']=='unavailable')['reason_code'],'OBJECT_MISSING')

    def test_lost_sector_catalogue_does_not_block_concept_or_stock(self):
        saved=self.save(three_payload());root=self.root(saved['reference'])
        ref=next(v for v in root['queries'].values() if self.root(v)['use']['parameters'].get('type')=='I')
        m=ref['manifest'];self.backend.shared['missing'].add((m['commit'],m['path']))
        out=self.read(saved['reference'])
        self.assertEqual(out['view']['surfaces']['sector']['status'],'rejected')
        self.assertEqual(out['view']['surfaces']['concept']['status'],'read')
        self.assertEqual(out['view']['surfaces']['stock']['status'],'read')
        self.assertEqual(out['status'],'partial-restoration')

    def test_empty_valid_source_and_absent_stock_stay_distinct(self):
        payload=raw_payload();payload['stock_results'][CODE]=captured('daily',[],empty_fields=True,limit=100)
        saved=self.save(payload);out=self.read(saved['reference'])
        self.assertEqual(out['status'],'restored');self.assertEqual(out['view']['scope_count'],2)
        self.assertEqual(out['view']['source_qualification_counts']['accepted'],1)
        self.assertEqual(out['view']['source_qualification_counts']['not_supplied'],1)

    def test_registry_drift_refuses_query_without_source_fetch(self):
        saved=self.save();ref=next(iter(self.root(saved['reference'])['queries'].values()))
        registry=deepcopy(REGISTRY);registry.snapshot['sha256']='0'*64
        with self.assertRaisesRegex(RecordError,'REGISTRY_CHANGED'):restore_market_query(ref,registry,self.fresh())

    def test_whole_reference_only_and_query_substitution_rejected(self):
        saved=self.save();root=self.root(saved['reference']);ref=next(iter(root['queries'].values()))
        altered=deepcopy(ref);altered['selection']=asdict(Selection('lines',1,1))
        with self.assertRaisesRegex(RecordError,'WHOLE_SOURCE'):restore_market_query(altered,REGISTRY,self.fresh())
        keys=list(root['queries']);root['queries'][keys[0]]=root['queries'][keys[1]]
        out=self.read(self.put_manifest(READING_SCHEMA,root))
        self.assertEqual(out['unavailable_queries'],1)
        self.assertIn('MARKET_CUSTODY_QUERY_CHANGED',[x['reason_code'] for x in out['sources']])

    def test_page_order_duplicate_foreign_and_digest_are_rechecked(self):
        payload=raw_payload();payload['calendar_result']=captured('trade_cal',CALENDAR,limit=2)
        saved=self.save(payload);ref=next(r for r in self.root(saved['reference'])['queries'].values()
                                        if self.root(r)['use']['api']=='trade_cal')
        base=self.root(ref)
        for kind in ('order','duplicate','digest','foreign','omitted'):
            data=deepcopy(base)
            if kind=='order':data['pages'].reverse()
            elif kind=='duplicate':data['pages'][1]=deepcopy(data['pages'][0])
            elif kind=='digest':data['pages'][0]['source_sha256']='0'*64
            elif kind=='foreign':data['pages'][0]['reference']['manifest']['repository']='synthetic-git:other'
            else:data['pages'].pop()
            changed=self.put_manifest(QUERY_SCHEMA,data)
            with self.subTest(kind=kind),self.assertRaises((RecordError,MarketInputError)):
                restore_market_query(changed,REGISTRY,self.fresh())

    def test_bound_and_invalid_recipe_are_not_silently_accepted(self):
        for payload in ({}, {'schema':'relay-three-market-reading-v1','surfaces':{'sector':{}}}):
            with self.subTest(payload=payload),self.assertRaises((RecordError,MarketInputError)):self.save(payload)
        saved=self.save();root=self.root(saved['reference'])
        root['queries']['0'*64]=next(iter(root['queries'].values()))
        with self.assertRaisesRegex(RecordError,'UNUSED_QUERY'):self.read(self.put_manifest(READING_SCHEMA,root))
        for kw in ({'limit':True},{'offset':-1},{'limit':101}):
            with self.subTest(kw=kw),self.assertRaises(RecordError):self.read(saved['reference'],**kw)

    def test_paging_preserves_unpaged_counts(self):
        saved=self.save();out=self.read(saved['reference'],limit=1)
        self.assertEqual(out['view']['scope_count'],2);self.assertEqual(len(out['view']['entries']),1)
        self.assertEqual(out['view']['page']['next_offset'],1)
        out=self.read(saved['reference'],entity_id=CODE)
        self.assertEqual(out['view']['scope_count'],2);self.assertEqual(out['view']['entries'][0]['entity_id'],CODE)

    def test_same_calendar_is_retained_once_across_three_channels(self):
        saved=self.save(three_payload());root=self.root(saved['reference'])
        self.assertEqual(sum(self.root(r)['use']['api']=='trade_cal' for r in root['queries'].values()),1)
        out=saved['restored'];self.assertEqual(out['query_count'],10)
        self.assertEqual(out['view']['membership_overlap']['pairs'][0]['overlap_count'],1)

    def test_arbitrary_execution_diagnostics_never_published(self):
        payload=raw_payload();payload['stock_results'][CODE]['capture']['pages'][0]['attempts']=[{'secret':'NOT_FOR_ARCHIVE'}]
        saved=self.save(payload)
        blobs=self.backend.shared['trees'][self.backend.head()].values()
        self.assertFalse(any(b'NOT_FOR_ARCHIVE' in b for b in blobs))
        self.assertEqual(saved['status'],'stored-and-independently-restored')


class LocalGitConsumerTests(unittest.TestCase):
    def test_new_process_uses_only_stored_refs_without_capture_files_or_relay_key(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store_path=root/'records.git';GitStore.init_synthetic(store_path)
            source_path=root/'reading.json';source_path.write_bytes(encoded(raw_payload()))
            env={**os.environ,'PYTHONPATH':str(ROOT_DIR/'src')};env.pop('TUSHARE_PROXY_API_KEY',None)
            cli=[sys.executable,'-m','decision_kernel.interfaces.market_custody_cli']
            common=['--path',str(store_path),'--registry',str(REGISTRY_PATH)]
            saved=subprocess.run([*cli,'save',*common,'--input',str(source_path),'--public-rights-confirmed'],
                                 cwd=temp,env=env,text=True,capture_output=True,timeout=30)
            self.assertEqual(saved.returncode,0,saved.stderr)
            result=json.loads(saved.stdout);reference_path=root/'ref.json';reference_path.write_bytes(encoded(result['reference']))
            source_path.unlink()  # Original capture gone: no temporary-file dependency.
            before=GitStore(store_path).head()
            out=subprocess.run([*cli,'read',*common,'--input',str(reference_path),'--limit','1'],
                               cwd=temp,env=env,text=True,capture_output=True,timeout=30)
            self.assertEqual(out.returncode,0,out.stderr);view=json.loads(out.stdout)
            self.assertEqual(view['status'],'restored');self.assertEqual(view['view']['scope_count'],2)
            self.assertEqual(GitStore(store_path).head(),before)
            self.assertNotIn('synthetic-key-only',out.stdout)

    def test_cli_denies_missing_rights_before_opening_any_store(self):
        env={**os.environ,'PYTHONPATH':str(ROOT_DIR/'src')}
        result=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.market_custody_cli','save',
            '--path','/does-not-exist','--registry',str(REGISTRY_PATH),'--input','/also-missing'],
            env=env,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stderr)['error'],'MARKET_CUSTODY_PUBLIC_RIGHTS_REQUIRED')




class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.sha = 'a'*40
        self.binding = {'repository':'auguspp/Decision-OS-2','repository_id':1420041744,
            'branch':'records','anchor':'b'*40,'store_id':'synthetic-test-binding',
            'write_enabled':False,'visibility':'public'}
        self.ref = {'manifest':{'repository':'auguspp/Decision-OS-2','commit':'c'*40,
            'path':'source-views/market-reading-test/'+'d'*64+'/manifest.json','sha256':'e'*64}}
    def event(self, operation='read', payload=None):
        return {'inputs':{'operation':operation,'code_sha':self.sha,
            'payload':json.dumps(self.ref if payload is None else payload),'public_rights_confirmed':operation=='save'}}
    def env(self, root):
        return {'GITHUB_REPOSITORY':'auguspp/Decision-OS-2','GITHUB_REF':'refs/heads/main',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':self.sha,'RUNNER_TEMP':str(root),
            'TUSHARE_PROXY_API_KEY':'not-forwarded','DECISION_OS_STORE_TOKEN':'synthetic-store-token-only'}
    def invoke(self, event, env, out, run):
        from decision_kernel.interfaces.market_custody_cli import run_runtime
        with patch('decision_kernel.interfaces.market_custody_cli._load',return_value=self.binding):
            return run_runtime(event,env,out,run=run)
    def test_read_host_is_readonly_argv_and_preserves_partial_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out';calls=[]
            def run(cmd,**kw):
                calls.append((cmd,kw))
                self.assertNotIn('TUSHARE_PROXY_API_KEY',kw['env'])
                self.assertNotIn('shell',kw)
                self.assertFalse(json.loads((out/'binding.json').read_text())['write_enabled'])
                return subprocess.CompletedProcess(cmd,2,'{"status":"partial-restoration"}','')
            self.assertEqual(self.invoke(self.event(),self.env(temp),out,run),2)
            self.assertEqual(len(calls),1)
            self.assertEqual(json.loads((out/'result.json').read_text())['exit_code'],2)
    def test_save_host_removes_diagnostics_before_any_artifact(self):
        payload=raw_payload();payload['stock_results'][CODE]['capture']['pages'][0]['attempts']=[{'private':'DO_NOT_PUBLISH_DIAGNOSTICS'}]
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out'
            def run(cmd,**kw):
                self.assertTrue(json.loads((out/'binding.json').read_text())['write_enabled'])
                self.assertIn('--public-rights-confirmed',cmd)
                self.assertNotIn('DO_NOT_PUBLISH_DIAGNOSTICS',(out/'input.json').read_text())
                return subprocess.CompletedProcess(cmd,0,'{"status":"stored-and-independently-restored"}','')
            self.assertEqual(self.invoke(self.event('save',payload),self.env(temp),out,run),0)
    def test_wrong_owner_ref_event_sha_rights_and_payload_do_not_run(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as temp:
            variations=[]
            for key,value in [('GITHUB_REPOSITORY','other/repo'),('GITHUB_REF','refs/heads/other'),
                              ('GITHUB_EVENT_NAME','push'),('GITHUB_SHA','b'*40)]:
                env=self.env(temp);env[key]=value;variations.append((self.event(),env))
            event=self.event('save',raw_payload());event['inputs']['public_rights_confirmed']=False
            variations.append((event,self.env(temp)))
            event=self.event();event['inputs']['payload']='x'*60001;variations.append((event,self.env(temp)))
            event=self.event();event['inputs']['binding']='attacker-target';variations.append((event,self.env(temp)))
            for i,(event,env) in enumerate(variations):
                run=Mock();out=Path(temp)/str(i)
                with self.subTest(i=i),self.assertRaises(RecordError):self.invoke(event,env,out,run)
                run.assert_not_called();self.assertFalse(out.exists())
    def test_timeout_preserves_unknown_attempt_not_unexecuted(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out'
            def timeout(cmd,**kw):raise subprocess.TimeoutExpired(cmd,600)
            with self.assertRaises(subprocess.TimeoutExpired):self.invoke(self.event(),self.env(temp),out,timeout)
            self.assertEqual(json.loads((out/'result.json').read_text())['status'],'in-progress-or-unknown')
    def test_page_selection_cannot_limit_saved_scope(self):
        from unittest.mock import Mock
        event=self.event('save',raw_payload());event['inputs']['limit']='1'
        with tempfile.TemporaryDirectory() as temp,self.assertRaisesRegex(RecordError,'SAVE_NOT_PAGE'):
            self.invoke(event,self.env(temp),Path(temp)/'out',Mock())
    def test_workflow_separates_read_write_and_has_no_source_credentials_or_schedule(self):
        text=(ROOT_DIR/'.github/workflows/market-custody.yml').read_text()
        self.assertIn('workflow_dispatch:',text)
        self.assertNotIn('schedule:',text);self.assertNotIn('TUSHARE_PROXY_API_KEY',text)
        self.assertIn('group: dos2-store-runtime-${{ github.repository }}',text)
        self.assertIn('contents: read',text);self.assertIn('contents: write',text)
        self.assertEqual(text.count('persist-credentials: false'),2)
        self.assertNotIn('${{ inputs.payload }}',text)


if __name__=='__main__': unittest.main()
