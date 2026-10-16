"""Exact native proof -> same failed query only; real capture/HTTP and host composition."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
import zipfile

import pytest

from decision_kernel.application.market_member_recovery import (
    load_recovery, member_context, recovery_request, selector,
)
from decision_kernel.application.market_source_check import check_sources
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.research_receipts import download_archive
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, Response, ROOT_DIR
from test_market_source_check import Opener, SECRET_TEXT
from test_research_source_use import NativeAPI, CODE, REPO

NOW = datetime(2026, 10, 16, 5, tzinfo=timezone.utc)
LATER = NOW + timedelta(hours=1)
START, END, TARGET = '20260701', '20261015', '885800.TI'


class MemberOpener:
    def __init__(self, fault=None, count=2):
        self.calls, self.fault, self.count = [], fault, count
        self.before = None

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        api = url.path.rsplit('/', 1)[-1]
        p = {k:v[0] for k,v in parse_qs(url.query).items()}
        self.calls.append((api, p))
        assert api == 'ths_member'  # Any batch/reference/price traffic is a test failure.
        if self.before is not None:
            self.before(p)
        offset = int(p.get('offset', 0))
        bad = self.fault == 'timeout' or self.fault == 'page-two-timeout' and offset > 0
        if bad:
            raise URLError(TimeoutError(SECRET_TEXT))
        if self.fault in ('denied', 'rate', 'long-wait', 'invalid-wait'):
            status = 403 if self.fault == 'denied' else 429 if self.fault == 'rate' else 504
            headers = {'Retry-After': '7200' if self.fault == 'long-wait' else 'bad'}
            raise HTTPError(request.full_url, status, SECRET_TEXT, headers, BytesIO(b'{}'))
        fields = p['fields'].split(',')
        rows = [dict(ts_code=p['ts_code'], con_code=f'6{i:05d}.SH', con_name=SECRET_TEXT)
                for i in range(offset, min(self.count, offset+int(p['limit'])))]
        if self.fault == 'wrong-code' and rows:
            rows[0]['ts_code'] = '885801.TI'
        if self.fault == 'duplicate' and len(rows) > 1:
            rows[1] = dict(rows[0])
        return Response(request.full_url, {'code':0, 'api_name':api, 'count':len(rows),
            'data':{'fields':fields, 'items':[[r[f] for f in fields] for r in rows]}})


def check(opener=None, **extra):
    opener = opener or MemberOpener()
    client = RelayHTTP(opener=opener, credential='synthetic-key-only')
    receipts, waits = [], []
    result = check_sources(**{'registry':REGISTRY, 'exchange':'SSE', 'start':START, 'end':END,
        'selection':'members', 'member_code':TARGET, 'request_context_confirmed':True,
        'access_confirmed':True, 'client':client, 'now':NOW, 'sleep':waits.append,
        'clock':lambda:NOW.isoformat(), 'progress':receipts.append, **extra})
    return result, opener, receipts, waits


class MarketNative(NativeAPI):
    def __init__(self):
        super().__init__()
        self.peer_rows = [{'id':200,'run_attempt':1,'status':'in_progress'}]
        self.history_total = None

    def add_market(self, report, ident=100, conclusion='failure'):
        report = deepcopy(report)
        report['identity'] = {'repo':REPO, 'code_sha':CODE, 'run_id':str(ident),
                              'attempt':'1', 'started_at':NOW.isoformat()}
        proof = self.add({'result.json':report}, family='relay', ident=ident, conclusion=conclusion)
        run = self.replies[f'/actions/runs/{ident}/attempts/1']
        run.update(path='.github/workflows/market-source-check.yml', created_at='2026-10-16T00:00:00Z')
        self.replies[f'/actions/runs/{ident}'] = deepcopy(run)
        self.replies[f'/actions/artifacts/{ident}']['name'] = f'dos2-market-check-{ident}-1'
        self.replies[f'/actions/runs/{ident}/attempts/1/jobs?per_page=100']['jobs'][0]['name'] = 'check'
        self.replies[f'/actions/runs/{ident}/artifacts?per_page=100'] = {
            'total_count':1, 'artifacts':[self.replies[f'/actions/artifacts/{ident}']]}
        self.peer_rows.append({'id':ident, 'status':'completed', 'run_attempt':1})
        return {k:proof[k] for k in ('run_id','run_attempt','artifact_id')} | {'slot':'members'}

    def request(self, method, path, payload=None):
        if '/actions/workflows/market-source-check.yml/runs?' in path:
            assert method == 'GET' and payload is None and 'created=%3E%3D' in path
            self.calls.append(path)
            return {'total_count':len(self.peer_rows) if self.history_total is None else self.history_total,
                    'workflow_runs':deepcopy(self.peer_rows)}
        return super().request(method,path,payload)


def origin(report=None):
    api = MarketNative()
    if report is None:
        report = check(MemberOpener('timeout'))[0]
    selected = api.add_market(report)
    return api, selected


def load(api, selected, **extra):
    return load_recovery(api.reader(), selected, **{'registry':REGISTRY,'exchange':'SSE',
        'start':START,'end':END,'now':LATER,'current_run':'200',**extra})


def host_context(tmp_path, selection='resume-members', selected=None):
    event = {'inputs':{'code_sha':CODE,'exchange':'SSE','start':START,'end':END,
        'access_confirmed':'true','selection':selection, 'request_context_confirmed':'true',
        'member_code':TARGET if selection == 'members' else '',
        'resume':json.dumps(selected or {'run_id':100,'run_attempt':1,'artifact_id':100,'slot':'members'})
                 if selection == 'resume-members' else ''}}
    env = dict(GITHUB_REPOSITORY=REPO,GITHUB_REF='refs/heads/main',GITHUB_EVENT_NAME='workflow_dispatch',
        GITHUB_SHA=CODE,GITHUB_RUN_ID='200',GITHUB_RUN_ATTEMPT='1',RUNNER_TEMP=str(tmp_path))
    return event,env


def host(tmp_path, api, selected, opener=None, checker=None):
    event,env = host_context(tmp_path,selected=selected)
    opener = opener or MemberOpener()
    client = RelayHTTP(opener=opener,credential='synthetic-key-only')
    run = checker or (lambda **kw: check_sources(**kw,client=client,sleep=lambda _:None,clock=lambda:LATER.isoformat()))
    out = tmp_path/'check'
    result = execute(event,env,out,now=LATER,checker=run,registry_path=REGISTRY_PATH,native_reader=api.reader())
    return result,json.loads((out/'result.json').read_bytes()),opener


def test_one_explicit_query_no_reference_prices_or_source_rows():
    out,opener,receipts,_ = check()
    assert out['status'] == 'checked-members' and out['http_attempts'] == 1
    assert set(out['queries']) == {'members'} and out['surfaces']['members']['row_count'] == 2
    assert out['queries']['members']['request_context'] == member_context(TARGET,REGISTRY)
    assert out['queries']['members']['retry_context'] == {'eligible':False}
    for text in map(json.dumps,receipts):
        for banned in (SECRET_TEXT,'600000.SH','synthetic-key-only','wire_sha256','capture.jsonl'):
            assert banned not in text
    assert [a for a,_ in opener.calls] == ['ths_member']


def test_request_context_exists_before_first_http():
    receipts=[]
    opener=MemberOpener()
    def before(p):
        latest=receipts[-1]['queries']['members']
        assert latest['status']=='in-progress' and latest['request_context']['parameters']==p
    opener.before=before
    check(opener,progress=receipts.append)


def test_default_old_calls_remain_count_only_and_optional_context_only_members():
    client=RelayHTTP(opener=Opener(),credential='synthetic-key-only')
    kwargs=dict(registry=REGISTRY,exchange='SSE',start='20260901',end='20260910',access_confirmed=True,
                selection='sample',client=client,now=NOW,sleep=lambda _:None)
    old=check_sources(**kwargs)
    assert 'request_context_confirmed' not in old and 'request_context' not in json.dumps(old)
    new=check_sources(**kwargs,request_context_confirmed=True)
    assert {k for k,v in new['queries'].items() if 'request_context' in v} == {'sector_members','concept_members'}
    assert '600000.SH' not in json.dumps(new)


@pytest.mark.parametrize('fault,count,status', [('duplicate',2,'rejected'),('wrong-code',2,'capture-incomplete'),(None,0,'checked-empty')])
def test_empty_invalid_members_not_nonempty_success(fault,count,status):
    result=check(MemberOpener(fault,count))[0]
    assert result['status']=='completed-with-gaps' and result['queries']['members']['status']==status
    assert result['queries']['members']['retry_context']=={'eligible':False}


def test_timeout_recovery_reuses_original_request_not_batch(tmp_path):
    original,opener,_,waits=check(MemberOpener('timeout'))
    before=deepcopy(original)
    assert len(opener.calls)==4 and waits==[30,30,30]
    api,selected=origin(original)
    code,receipt,after=host(tmp_path,api,selected)
    assert code==0 and receipt['status']=='checked-members'
    assert len(after.calls)==1 and after.calls[0]==opener.calls[0]
    assert receipt['recovery']['source_query_recovered'] is True
    assert receipt['recovery']['original_batch_revalidated'] is False
    assert receipt['recovery']['parent']['source_pages_reused'] is False
    assert original==before and original['status']=='completed-with-gaps'


def test_partial_capture_restarts_exact_whole_query_from_zero(tmp_path):
    report,opener,_,_=check(MemberOpener('page-two-timeout',5001))
    assert report['queries']['members']['row_count']==5000 and report['queries']['members']['page_count']==1
    api,selected=origin(report)
    code,out,after=host(tmp_path,api,selected,MemberOpener(count=5001))
    assert code==0 and [p.get('offset','0') for _,p in after.calls]==['0','5000']
    assert out['queries']['members']['row_count']==5001
    assert out['recovery']['parent']['previous_received_rows']==5000
    assert out['recovery']['parent']['source_pages_reused'] is False


@pytest.mark.parametrize('fault',['denied','rate','invalid-wait'])
def test_terminal_source_refusal_has_no_recovery(fault):
    api,sel=origin(check(MemberOpener(fault))[0])
    with pytest.raises((RecordError,ValueError)):
        load(api,sel)


def test_long_retry_after_is_not_shortened_by_new_run(tmp_path):
    report,_,_,waits=check(MemberOpener('long-wait'))
    assert waits==[] and report['queries']['members']['retry_context']['retry_after_seconds']==7200
    api,sel=origin(report)
    code,out,opener=host(tmp_path,api,sel)
    assert code==2 and out['http_attempts']==0 and not opener.calls
    assert load(api,sel,now=NOW+timedelta(hours=3))['member_code']==TARGET


@pytest.mark.parametrize('mutation', ['no-context','no-consent','checked','interrupted','stopped','chain',
    'scope','foreign-code','fields','offset','key','registry','pages','identity','eligible','delay'])
def test_invalid_or_legacy_origin_refused_before_relay(tmp_path,mutation):
    report=check(MemberOpener('timeout'))[0]
    item=report['queries']['members']
    if mutation=='no-context':item.pop('request_context')
    elif mutation=='no-consent':report.pop('request_context_confirmed')
    elif mutation=='checked':item['status']='checked'
    elif mutation=='interrupted':report['status']='interrupted'
    elif mutation=='stopped':report['stopped']=True
    elif mutation=='chain':report['recovery']={}
    elif mutation=='scope':report['scope']['exchange']='SZSE'
    elif mutation=='foreign-code':item['request_context']['parameters']['ts_code']='885801.TI'
    elif mutation=='fields':item['request_context']['parameters']['fields']='ts_code,con_code,con_name,token'
    elif mutation=='offset':item['request_context']['parameters']['offset']='5000'
    elif mutation=='key':item['request_context']['query_key']='0'*64
    elif mutation=='registry':item['request_context']['registry_sha256']='0'*64
    elif mutation=='pages':item['request_context']['max_pages']=20
    elif mutation=='eligible':item['retry_context']['eligible']=False
    elif mutation=='delay':item['retry_context']['retry_after_seconds']=0
    api,sel=origin(report)
    if mutation=='identity':
        api.replies['/actions/runs/100/attempts/1']['head_sha']='f'*40
    code,out,opener=host(tmp_path,api,sel)
    assert code==2 and out['status']=='resume-unavailable' and out['http_attempts']==0 and not opener.calls


@pytest.mark.parametrize('which',['digest','expired','fork','path','job','latest-attempt','history','peer-unsettled','missing-peer'])
def test_native_proof_and_history_guard(which):
    api,sel=origin()
    meta=api.replies['/actions/artifacts/100'];run=api.replies['/actions/runs/100/attempts/1']
    if which=='digest':meta['digest']='sha256:'+'0'*64
    elif which=='expired':meta['expired']=True
    elif which=='fork':meta['workflow_run']['head_repository_id']=999
    elif which=='path':run['path']='.github/workflows/research-runtime.yml'
    elif which=='job':api.replies['/actions/runs/100/attempts/1/jobs?per_page=100']['jobs'][0]['name']='source'
    elif which=='latest-attempt':api.replies['/actions/runs/100']['run_attempt']=2
    elif which=='history':api.history_total=999
    elif which=='peer-unsettled':api.peer_rows.append({'id':150,'status':'in_progress','run_attempt':1})
    elif which=='missing-peer':
        api.peer_rows.append({'id':150,'status':'completed','run_attempt':1})
        api.replies['/actions/runs/150/artifacts?per_page=100']={'total_count':0,'artifacts':[]}
    with pytest.raises((RecordError,ValueError)):
        load(api,sel)


def test_second_consumption_and_interrupted_consumption_refused(tmp_path):
    api,sel=origin()
    _,out,_=host(tmp_path,api,sel,checker=lambda **_: (_ for _ in ()).throw(RuntimeError(SECRET_TEXT)))
    assert out['status']=='interrupted' and out['recovery']['parent']['run_id']==100
    assert SECRET_TEXT not in json.dumps(out)
    api.add_market(out,ident=150)
    with pytest.raises(RecordError,match='ALREADY_CONSUMED'):
        load(api,sel)


def test_unrelated_completed_or_queued_peer_does_not_replay_it():
    api,sel=origin()
    api.add_market(check()[0],ident=120,conclusion='success')
    api.peer_rows.append({'id':210,'status':'queued','run_attempt':1})
    assert load(api,sel)['member_code']==TARGET


def test_market_receipt_not_admitted_as_research_capture():
    api,sel=origin()
    with pytest.raises(RecordError,match='RUN_MISMATCH'):
        api.reader().read({k:sel[k] for k in ('run_id','run_attempt','artifact_id')})


@pytest.mark.parametrize('bad',[{'run_id':True},{'run_attempt':2},{'slot':'stock_daily'},{'url':'https://bad.invalid'}])
def test_selector_is_closed_and_contains_no_network_url(bad):
    with pytest.raises(RecordError):
        selector({'run_id':100,'run_attempt':1,'artifact_id':100,'slot':'members',**bad})


@pytest.mark.parametrize('bad',[{'request_context_confirmed':False},{'member_code':'885801.TI'},
    {'resume':'{"run_id":100,"run_id":101}'},{'selection':'us-indices'}, {'request_context_confirmed':1}])
def test_host_rejects_ambiguous_or_unapproved_recovery(tmp_path,bad):
    event,env=host_context(tmp_path);event['inputs'].update(bad)
    with pytest.raises((RecordError,ValueError)):
        settings(event,env,LATER)


def test_explicit_read_token_never_inherits_relay_or_store_credentials(monkeypatch):
    monkeypatch.setenv('DECISION_OS_STORE_TOKEN','store-do-not-use')
    monkeypatch.setenv('TUSHARE_PROXY_API_KEY','relay-do-not-use')
    monkeypatch.setenv('GITHUB_TOKEN','unselected-do-not-use')
    def run(command,**kwargs):
        assert kwargs['env']['GH_TOKEN']=='synthetic-actions-read-only'
        assert not ({'DECISION_OS_STORE_TOKEN','TUSHARE_PROXY_API_KEY','GITHUB_TOKEN'} & kwargs['env'].keys())
        assert 'shell' not in kwargs
        kwargs['stdout'].write(b'synthetic')
        return type('Done',(),{'returncode':0})()
    monkeypatch.setattr('decision_kernel.infrastructure.research_receipts.subprocess.run',run)
    assert download_archive(REPO,100,token='synthetic-actions-read-only')==b'synthetic'


def test_workflow_uses_original_host_and_only_explicit_read_permission():
    text=(ROOT_DIR/'.github/workflows/market-source-check.yml').read_text()
    assert 'actions: read' in text and "inputs.selection == 'resume-members' && github.token" in text
    assert 'group: decision-os-relay-source' in text and 'retention-days: 7' in text
    assert 'cancel-in-progress: false' in text and text.count('path: ${{ runner.temp }}/dos2-market-check/result.json')==1
    assert all(x not in text for x in ('contents: write','actions: write','schedule:','DECISION_OS_STORE_TOKEN'))
