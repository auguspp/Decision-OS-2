"""Whole-session source composition, counted gaps, exact custody and existing hosts."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from io import BytesIO

import pytest

from decision_kernel.application.market_session_inputs import SESSION_SCHEMA, session_quotes
from decision_kernel.application.market_session_check import check_session_sources, session_scope
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.research_sources import validate_plan, capture_registered
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, digest
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR, Response
from test_market_custody import MemoryBackend

NOW = datetime(2026,10,17,0,tzinfo=timezone.utc)
START, END = '20261012', '20261016'
PRICE, FACTOR = '9876.54321', '1.23456789'


class SessionOpener:
    def __init__(self, count=2):
        self.calls, self.before, self.fail, self.once = [], None, None, False
        self.rows = {}
        for venue, codes in [('SSE',['%06d.SH' % (600000+i) for i in range(count)]),
                             ('SZSE',['000001.SZ','300001.SZ'])]:
            self.rows[('stock_basic',venue)] = [dict(ts_code=c,symbol=c[:6],name='PRIVATE_SOURCE_NAME',
                exchange=venue,curr_type='CNY',list_status='L',list_date='20000101',delist_date=None) for c in codes]
        codes = [r['ts_code'] for (api,_),rows in self.rows.items() for r in rows] + ['830001.BJ']
        for api,field,value in [('daily','close',PRICE),('adj_factor','adj_factor',FACTOR)]:
            self.rows[(api,None)] = [dict(ts_code=c,trade_date=END,**{field:value}) for c in codes]
    def open(self, request, timeout):
        url=urlsplit(request.full_url);api=url.path.rsplit('/',1)[-1]
        p={k:v[0] for k,v in parse_qs(url.query).items()};self.calls.append((api,p))
        if self.before is not None:self.before(api,p)
        key=(api,p.get('exchange'))
        if self.fail in (api,key):
            if self.once:self.fail=None
            raise URLError(TimeoutError('PRIVATE_TIMEOUT_TEXT'))
        if self.fail=='denied' and api=='trade_cal':
            raise HTTPError(request.full_url,403,'PRIVATE_REFUSAL',{},BytesIO(b'private'))
        if api=='trade_cal':
            lo=date.fromisoformat(p['start_date'][:4]+'-'+p['start_date'][4:6]+'-'+p['start_date'][6:])
            hi=date.fromisoformat(p['end_date'][:4]+'-'+p['end_date'][4:6]+'-'+p['end_date'][6:])
            rows=[dict(exchange=p['exchange'],cal_date=(lo+timedelta(days=n)).strftime('%Y%m%d'),
                       is_open=int((lo+timedelta(days=n)).weekday()<5)) for n in range((hi-lo).days+1)]
            rows=self.rows.get(key,rows)
        else:rows=self.rows[key]
        fields=p['fields'].split(',');offset=int(p.get('offset',0));page=rows[offset:offset+int(p['limit'])]
        return Response(request.full_url,{'code':0,'api_name':api,'count':len(rows),
            'data':{'fields':fields,'items':[[r.get(f) for f in fields] for r in page]}})


def capture(opener, api, params, fields, limit=5000, pages=20):
    use=validate_plan({'schema':'research-source-plan-v1','case_id':'synthetic-session',
        'requests':[{'api':api,'stage':'baseline','purpose':'synthetic session test',
        'parameters':{**params,'fields':','.join(sorted(fields)),'limit':str(limit)}}]},REGISTRY)['requests'][0]
    client=RelayHTTP(opener=opener,credential='synthetic-key-only')
    session=RetryingRelay(client,policy=RetryPolicy(max_attempts=1,max_wait_seconds=0),clock=lambda:NOW.isoformat())
    got=capture_registered(use,REGISTRY,session,max_pages=pages)
    return {'case_id':'synthetic-session','use':use,'status':'captured' if got['complete'] else 'capture-incomplete','capture':got}


def payload(opener=None, end=END):
    opener=opener or SessionOpener()
    from decision_kernel.application.market_security_inputs import MASTER_FIELDS
    out={'schema':SESSION_SCHEMA,'start':START,'end':end,'calendar_end':END,
         'calendar_results':{},'master_results':{},'session_results':{},'reference_policy':'isolate-identified'}
    for venue in ('SSE','SZSE'):
        out['calendar_results'][venue]=capture(opener,'trade_cal',{'exchange':venue,'start_date':START,'end_date':END},
                                               {'exchange','cal_date','is_open'})
        out['master_results'][venue]=capture(opener,'stock_basic',{'exchange':venue,'list_status':'L'},MASTER_FIELDS,500)
    for api,field in [('daily','close'),('adj_factor','adj_factor')]:
        out['session_results'][api]=capture(opener,api,{'trade_date':end},{'ts_code','trade_date',field})
    return out


def run_check(opener=None, **extra):
    opener=opener or SessionOpener();receipts=[];waits=[]
    result=check_session_sources(**{'registry':REGISTRY,'exchange':'CN','start':START,'end':END,
        'now':NOW,'access_confirmed':True,'request_context_confirmed':True,
        'client':RelayHTTP(opener=opener,credential='synthetic-key-only'),'progress':receipts.append,
        'sleep':waits.append,'clock':lambda:NOW.isoformat(),**extra})
    return result,opener,receipts,waits


def test_full_cross_section_reconciles_both_venues_and_factor_units():
    data=payload();before=encoded(data);out=read_market_payload(data,REGISTRY)
    assert out['scope_count']==4 and len(out['entries'])==4 and out['qualified_scope_covered']
    assert out['outside_sh_sz']=={'daily':1,'adj_factor':1}
    assert out['units']['factor']=='dimensionless-factor-not-adjusted-price'
    assert all(e['raw']['value']==PRICE and e['factor']['value']==FACTOR for e in out['entries'])
    assert encoded(data)==before and out['source_requests']==0
    assert all(v['matched_count']==2 and sum(v['raw_counts'].values())==v['eligible_count']==2 for v in out['by_exchange'].values())


@pytest.mark.parametrize('api,field',[('daily','close'),('adj_factor','adj_factor')])
@pytest.mark.parametrize('bad',['duplicate','0','-1','NaN','bad',True,None])
def test_bad_identifiable_group_isolated_without_first_last(api,field,bad):
    op=SessionOpener();rows=op.rows[(api,None)]
    if bad=='duplicate':rows.append(deepcopy(rows[0]))
    else:rows[0][field]=bad
    out=read_market_payload(payload(op),REGISTRY)
    entry=next(e for e in out['entries'] if e['entity_id']=='600000.SH')
    assert entry['raw' if api=='daily' else 'factor']['status']=='quarantined'
    assert out['scope_count']==4 and out['by_exchange']['SZSE']['qualified_scope_covered']
    assert not out['qualified_scope_covered']


def test_missing_price_not_halt_or_zero_and_factor_still_observed():
    op=SessionOpener();op.rows[('daily',None)].pop(0)
    out=read_market_payload(payload(op),REGISTRY)
    entry=next(e for e in out['entries'] if e['entity_id']=='600000.SH')
    assert entry['raw']['status']=='missing' and entry['raw']['value'] is None
    assert entry['factor']['status']=='observed'
    assert out['by_exchange']['SSE']['raw_counts']['missing']==1 and out['scope_count']==4


def test_master_quarantine_and_future_listing_retained_not_qualified():
    op=SessionOpener();op.rows[('stock_basic','SSE')][0]['delist_date']='20261020'
    op.rows[('stock_basic','SZSE')][0]['list_date']='20261020'
    out=read_market_payload(payload(op),REGISTRY)
    assert out['scope_count']==4 and not out['strict_reference_passed']
    assert out['by_exchange']['SSE']['reference_quarantined_count']==1
    assert out['by_exchange']['SZSE']['outside_listing_count']==1
    assert {e['availability'] for e in out['entries']}=={'eligible','reference-quarantined','outside-listing'}
    assert all(e['raw'] is None for e in out['entries'] if e['availability']!='eligible')


def test_unknown_syntax_rejects_capture_but_other_source_is_not_missing():
    op=SessionOpener();op.rows[('daily',None)][0]['ts_code']='PRIVATE_UNKNOWN_ID'
    out=read_market_payload(payload(op),REGISTRY)
    assert out['queries']['daily']['status']=='rejected'
    assert out['by_exchange']['SSE']['raw_counts']['input-unavailable']==2
    assert out['by_exchange']['SSE']['raw_counts']['missing']==0
    assert out['by_exchange']['SSE']['factor_counts']['observed']==2
    assert 'PRIVATE_UNKNOWN_ID' not in json.dumps(out)


def test_unreferenced_sh_sz_not_silently_dropped_from_reading():
    op=SessionOpener();op.rows[('daily',None)].append(dict(ts_code='688888.SH',trade_date=END,close=PRICE))
    out=read_market_payload(payload(op),REGISTRY)
    assert out['scope_count']==4 and out['page']['entries_count']==5
    assert out['by_exchange']['SSE']['unreferenced_count']==1 and not out['qualified_scope_covered']
    assert next(e for e in out['entries'] if e['entity_id']=='688888.SH')['availability']=='unreferenced'


@pytest.mark.parametrize('part,key',[('calendar_results','SSE'),('master_results','SSE'),('session_results','daily')])
def test_missing_shared_input_unknown_counts_preserve_other_venue_or_source(part,key):
    data=payload();data[part].pop(key);out=read_market_payload(data,REGISTRY)
    assert not out['qualified_scope_covered']
    if part!='session_results':assert out['by_exchange']['SZSE']['qualified_scope_covered']
    if part=='master_results':assert out['scope_count'] is None and out['by_exchange']['SSE']['scope_count'] is None
    if part=='calendar_results':assert out['by_exchange']['SSE']['raw_counts'] is None
    if part=='session_results':assert out['by_exchange']['SSE']['raw_counts']['input-unavailable']==2


def test_wrong_date_and_partial_capture_do_not_certify_market_coverage():
    for mode in ('date','partial','snapshot'):
        op=SessionOpener()
        if mode=='date':op.rows[('daily',None)][0]['trade_date']='20261015'
        data=payload(op)
        if mode=='partial':data['session_results']['daily']['capture']['complete']=False
        if mode=='snapshot':data['session_results']['daily']['capture']['snapshot']['sha256']='0'*64
        out=read_market_payload(data,REGISTRY)
        assert out['queries']['daily']['status']=='rejected' and not out['qualified_scope_covered']
        assert out['by_exchange']['SSE']['raw_counts']['missing']==0


def test_calendar_lag_is_from_declared_complete_calendar_not_wall_clock():
    op=SessionOpener()
    for api in ('daily','adj_factor'):
        for row in op.rows[(api,None)]:row['trade_date']='20261015'
    out=read_market_payload(payload(op,'20261015'),REGISTRY)
    assert out['by_exchange']['SSE']['session_lag']==1 and out['by_exchange']['SZSE']['session_lag']==1
    assert out['by_exchange']['SSE']['matched_count']==2 and not out['qualified_scope_covered']


def test_whole_scope_paging_entity_query_and_no_input_mutation():
    data=payload();full=read_market_payload(data,REGISTRY)
    a=read_market_payload(data,REGISTRY,limit=1);b=read_market_payload(data,REGISTRY,offset=1,limit=1)
    q=read_market_payload(data,REGISTRY,entity_id='300001.SZ')
    assert a['entries']+b['entries']==full['entries'][:2]
    assert a['view_version']==b['view_version']==q['view_version']==full['view_version']
    assert q['scope_count']==4 and q['page']['selected_count']==1 and a['page']['next_offset']==1


def test_cross_section_paginates_past_5000_without_per_security_requests():
    op=SessionOpener(count=5001);out,_,_,_=run_check(op)
    assert out['status']=='checked-session-coverage' and out['surfaces']['session']['scope_count']==5003
    assert out['queries']['daily']['row_count']==5004 and out['queries']['daily']['page_count']==2
    assert out['queries']['adj_factor']['page_count']==2
    assert [p.get('offset','0') for a,p in op.calls if a=='daily']==['0','5000']
    assert all('ts_code' not in p for a,p in op.calls if a in ('daily','adj_factor'))


def test_real_host_composition_only_six_queries_and_no_returned_values():
    out,op,receipts,_=run_check()
    assert out['status']=='checked-session-coverage' and out['http_attempts']==6
    assert [a for a,p in op.calls]==['trade_cal','trade_cal','stock_basic','stock_basic','daily','adj_factor']
    assert len(out['queries'])==6 and out['surfaces']['session']['scope_count']==4
    for text in map(json.dumps,receipts+[out]):
        assert all(bad not in text for bad in ['PRIVATE_SOURCE_NAME',PRICE,FACTOR,'600000.SH','830001.BJ','synthetic-key-only','source_lineage','wire_sha256'])


def test_generated_requests_are_fixed_before_first_http():
    op=SessionOpener();receipts=[]
    def before(api,params):
        assert len(receipts[-1]['queries'])==6
        assert all(q['request_context']['parameters'] for q in receipts[-1]['queries'].values())
    op.before=before
    run_check(op,progress=receipts.append)


def test_timeout_is_local_and_refusal_stops_all_remaining_traffic():
    op=SessionOpener();op.fail='daily'
    out,_,_,waits=run_check(op)
    assert out['queries']['daily']['status']=='capture-incomplete' and waits==[30,30,30]
    assert out['queries']['adj_factor']['status']=='checked'
    assert out['queries']['daily']['retry_context']['eligible'] is True
    assert out['queries']['daily']['retry_context']['retry_after_seconds']==30
    assert out['surfaces']['session']['by_exchange']['SSE']['raw_counts']['input-unavailable']==2
    op=SessionOpener();op.fail='denied';out,_,_,waits=run_check(op)
    assert out['stopped'] and out['http_attempts']==1 and not waits
    assert out['surfaces']['session']['scope_count'] is None


def test_natural_synthetic_timeout_recovery_preserves_attempt_history():
    op=SessionOpener();op.fail=('stock_basic','SSE');op.once=True
    out,_,_,waits=run_check(op)
    assert out['status']=='checked-session-coverage' and out['http_attempts']==7 and waits==[30]
    assert out['queries']['SSE_master']['last_attempt_failure']['classification']=='timeout'
    assert out['queries']['SSE_master']['retry_context']=={'eligible':False}


def host_data(tmp_path):
    event={'inputs':{'code_sha':'a'*40,'selection':'session-coverage','exchange':'CN','start':START,'end':END,
                     'access_confirmed':'true','request_context_confirmed':'true','member_code':'','resume':''}}
    env=dict(GITHUB_REPOSITORY='auguspp/Decision-OS-2',GITHUB_REF='refs/heads/main',GITHUB_SHA='a'*40,
             GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1',RUNNER_TEMP=str(tmp_path))
    return event,env


@pytest.mark.parametrize('bad',[{'exchange':'SSE'},{'request_context_confirmed':'false'},{'member_code':'885800.TI'},
                               {'resume':'{}'},{'end':'20261017'},{'start':'20200101'}])
def test_invalid_host_input_precedes_source(tmp_path,bad):
    event,env=host_data(tmp_path);event['inputs'].update(bad)
    with pytest.raises(ValueError):settings(event,env,NOW)


def test_existing_check_host_calls_session_consumer_and_only_receipt(tmp_path):
    event,env=host_data(tmp_path);op=SessionOpener()
    checker=lambda **kw:check_session_sources(**kw,client=RelayHTTP(opener=op,credential='synthetic-key-only'),sleep=lambda _:None)
    assert execute(event,env,tmp_path/'out',now=NOW,checker=checker,registry_path=REGISTRY_PATH)==0
    assert {p.name for p in (tmp_path/'out').iterdir()}=={'result.json'}
    receipt=json.loads((tmp_path/'out/result.json').read_bytes());assert receipt['identity']['run_id']=='123'


def test_custody_rights_preflight_idempotence_and_missing_source_not_zero():
    data=payload();backend=MemoryBackend();store=VersionedStore(backend)
    fresh=lambda:VersionedStore(MemoryBackend(backend.shared))
    with pytest.raises(RecordError):retain_market_reading(data,REGISTRY,store,fresh_reader=fresh)
    assert backend.shared['count']==0
    saved=retain_market_reading(data,REGISTRY,store,fresh_reader=fresh,public_rights_confirmed=True)
    assert saved['restored']['view']==read_market_payload(data,REGISTRY)
    head=backend.head();again=retain_market_reading(data,REGISTRY,store,fresh_reader=fresh,public_rights_confirmed=True)
    assert again['reference']==saved['reference'] and backend.head()==head
    from decision_kernel.domain.records import SourceRef
    root=json.loads(store.read_source(SourceRef.from_dict(saved['reference']))['data'])
    key=root['reading']['session_results']['daily']['query'];ref=SourceRef.from_dict(root['queries'][key])
    backend.shared['missing'].add((ref.manifest.commit,ref.manifest.path))
    out=read_stored_market(saved['reference'],REGISTRY,fresh())
    assert out['status']=='partial-restoration' and out['source_requests']==0
    assert out['view']['by_exchange']['SSE']['raw_counts']['input-unavailable']==2
    assert out['view']['by_exchange']['SSE']['factor_counts']['observed']==2


def test_common_cli_then_new_process_only_original_root_ref_after_input_deleted(tmp_path):
    data=payload();source=tmp_path/'input.json';source.write_bytes(encoded(data))
    env={k:v for k,v in os.environ.items() if k not in ('TUSHARE_PROXY_API_KEY','GH_TOKEN','GITHUB_TOKEN','DECISION_OS_STORE_TOKEN')}
    env['PYTHONPATH']=str(ROOT_DIR/'src')
    def cli(name,*args):
        out=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.'+name,*args],cwd=tmp_path,env=env,
                           text=True,capture_output=True,timeout=30)
        assert out.returncode==0,out.stderr
        return json.loads(out.stdout)
    reg=['--registry',str(REGISTRY_PATH)]
    direct=cli('market_relay_cli',*reg,'--input',str(source))
    path=tmp_path/'synthetic.git';GitStore.init_synthetic(path)
    common=[*reg,'--path',str(path)]
    saved=cli('market_custody_cli','save',*common,'--input',str(source),'--public-rights-confirmed')
    ref=tmp_path/'ref.json';ref.write_bytes(encoded(saved['reference']));source.unlink()
    restored=cli('market_custody_cli','read',*common,'--input',str(ref),'--entity-id','300001.SZ')
    assert restored['view']['view_version']==direct['view_version'] and restored['view']['scope_count']==4
    assert restored['source_requests']==0 and len(restored['view']['entries'])==1


def test_empty_capture_is_missing_not_unknown():
    op=SessionOpener();op.rows[('daily',None)]=[]
    out=read_market_payload(payload(op),REGISTRY)
    assert out['queries']['daily']['status']=='checked'
    assert out['by_exchange']['SSE']['raw_counts']['missing']==2
    assert out['by_exchange']['SSE']['raw_counts']['input-unavailable']==0


def test_strict_default_does_not_silently_select_bad_master_rows():
    op=SessionOpener();op.rows[('stock_basic','SSE')][0]['curr_type']='USD'
    data=payload(op);data.pop('reference_policy');out=read_market_payload(data,REGISTRY)
    assert out['scope_count'] is None and out['by_exchange']['SSE']['scope_count'] is None
    assert out['by_exchange']['SZSE']['qualified_scope_covered']


def test_default_host_route_reaches_actual_session_engine(tmp_path,monkeypatch):
    op=SessionOpener();client=RelayHTTP(opener=op,credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.market_session_check.RelayHTTP',lambda:client)
    event,env=host_data(tmp_path)
    assert execute(event,env,tmp_path/'out',now=NOW,registry_path=REGISTRY_PATH)==0
    assert len(op.calls)==6


def test_long_retry_after_retained_without_waiting_or_automatic_resume():
    class LongWait(SessionOpener):
        def open(self,request,timeout):
            if '/daily?' in request.full_url:
                self.calls.append(('daily',{}))
                raise HTTPError(request.full_url,504,'PRIVATE_TIMEOUT',{'Retry-After':'7200'},BytesIO(b'{"error":"upstream_timeout"}'))
            return super().open(request,timeout)
    out,_,_,waits=run_check(LongWait())
    assert not waits and out['queries']['daily']['http_attempts']==1
    assert out['queries']['daily']['retry_context']['retry_after_seconds']==7200
    assert out['queries']['adj_factor']['status']=='checked' and 'recovery' not in out
