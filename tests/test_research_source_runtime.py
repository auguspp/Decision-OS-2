"""Explicit workflow/installed CLI and captured evidence integrity; no production data."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from decision_kernel.application.research_sources import source_inputs, inspect_result, capture_plan
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_retry import RetryPolicy, RetryingRelay
from decision_kernel.interfaces.relay_research_cli import main
from decision_kernel.interfaces import relay_source_runtime as host
from test_research_sources import registry, request, plan, http, body, busy, STAMP, ROOT_DIR


def captured():
    c,_=http(body())
    return list(capture_plan(plan(),registry(),client=c,public_rights_confirmed=True,clock=lambda:STAMP))[0]


@pytest.mark.parametrize('mutation',['query','key','stage','offset','time','table','fields','scope','transport','complete','terminal'])
def test_export_revalidates_identity_page_and_projection(mutation):
    item=captured(); c=item['capture']; p=c['pages'][0]
    if mutation=='query': c['parameters']['ts_code']='000001.SZ'
    if mutation=='key': c['query_key']='a'*64
    if mutation=='stage': item['use']['stage']='not-a-stage'
    if mutation=='offset': p['offset']=5
    if mutation=='time': p['captured_at']='2026-10-16'
    if mutation=='table': p['table']['private']='do not store'
    if mutation=='fields': p['table']['fields'][-1]='content'
    if mutation=='scope': p['table']['items'][0][0]='000001.SZ'
    if mutation=='transport': p['transport']['wire_sha256']='fake'
    if mutation=='complete': c['complete']=False
    if mutation=='terminal': c['pages'].append(deepcopy(p));c['pages'][-1]['offset']=1
    with pytest.raises((RecordError,ValueError)):
        source_inputs(item,registry())


def test_export_does_not_promote_edited_diagnostic_prose_into_source():
    item=captured();item['capture']['pages'][0]['attempts'].append({'message':'private diagnostic'})
    assert 'private diagnostic' not in source_inputs(item,registry())[0].text
    assert inspect_result(item,registry())['row_count']==1


def test_plan_cli_outside_cwd_and_capture_export_no_network(tmp_path,monkeypatch):
    monkeypatch.delenv('TUSHARE_PROXY_API_KEY',raising=False)
    p=tmp_path/'plan.json';p.write_text(json.dumps(plan()))
    output=tmp_path/'planned.json'
    args=['plan','--plan',str(p),'--registry',str(ROOT_DIR/'docs/sources/relay-registry.json'),'--output',str(output)]
    run=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.relay_research_cli',*args],
        cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(ROOT_DIR/'src')},capture_output=True,text=True,timeout=20)
    assert run.returncode==0,run.stderr
    assert json.loads(output.read_text())['requests'][0]['stage']=='baseline'
    first=output.read_bytes();assert main(args)==2 and output.read_bytes()==first
    raw=tmp_path/'capture.jsonl';item=captured();raw.write_text(json.dumps(item)+'\n'+json.dumps(item)+'\n')
    exported=tmp_path/'source-inputs.jsonl'
    assert main(['export','--capture',str(raw),'--registry',str(ROOT_DIR/'docs/sources/relay-registry.json'),
                 '--output',str(exported)])==0
    sources=[json.loads(s) for s in exported.read_text().splitlines()]
    assert len(sources)==1 and sources[0]['custody']=='excerpt'
    assert not list(tmp_path.glob('*.git'))


def test_runtime_uses_plan_on_initial_research_and_never_writes_store(tmp_path,monkeypatch):
    monkeypatch.chdir(ROOT_DIR)
    sha='a'*40
    event={'inputs':{'operation':'capture','code_sha':sha,'family':'research',
        'source_plan':json.dumps(plan()),'public_rights_confirmed':True,'max_pages':'7'}}
    env={'GITHUB_REPOSITORY':host.REPO,'GITHUB_REF':'refs/heads/main',
        'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':sha,'RUNNER_TEMP':str(tmp_path)}
    calls=[]
    def run(cmd,**kw): calls.append(cmd);return SimpleNamespace(returncode=0)
    out=tmp_path/'source'
    assert host.execute('prepare',event,env,out,run=run)==0
    assert calls[-1][3]=='plan' and '--live' not in calls[-1]
    assert host.execute('capture',event,env,out,run=run)==0
    assert calls[-1][3]=='capture' and '--live' in calls[-1]
    assert calls[-1][-2:]==['--max-pages','7']
    assert json.loads((out/'result.json').read_text())['records_written'] is False
    assert all('save' not in c and '--binding' not in c for c in calls)
    changed=deepcopy(event);changed['inputs']['source_plan']=json.dumps(plan(request(api='daily_basic')))
    with pytest.raises(ValueError,match='PLAN_CHANGED'):host.execute('capture',changed,env,out,run=run)


@pytest.mark.parametrize('bad',[{'codes':'000001.SZ'},{'periods':'20251231'},{'apis':'income'},
    {'source_plan':'{"schema":1,"schema":2}'},{'max_pages':'0'}])
def test_runtime_research_scope_not_mixed_or_unvalidated(tmp_path,monkeypatch,bad):
    monkeypatch.chdir(ROOT_DIR);sha='a'*40
    event={'inputs':{'operation':'plan','code_sha':sha,'family':'research','source_plan':json.dumps(plan()),**bad}}
    env={'GITHUB_REPOSITORY':host.REPO,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_SHA':sha}
    with pytest.raises((RecordError,ValueError)):host.settings(event,env)


@pytest.mark.parametrize('retry_after,expected',[('60',[60]),('0',[30]),('bad',[])])
def test_header_wait_does_not_shortcut_thirty_seconds(retry_after,expected):
    client,opener=http(busy(headers={'Retry-After':retry_after}),body())
    waits=[]
    out=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,sleep=waits.append))
    assert waits==expected and out[0]['capture']['complete']==bool(expected)


def test_valid_429_wait_resumes_same_request():
    client,opener=http(busy(429,'rate_limited',{'Retry-After':'45'}),body())
    waits=[]
    out=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,sleep=waits.append))
    assert waits==[45] and out[0]['capture']['complete']
    assert opener.calls[0].full_url==opener.calls[1].full_url


def test_attempt_limit_pending_keeps_failure_and_original_params():
    client,opener=http(busy(),busy())
    waits=[]
    out=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,
        policy=RetryPolicy(2,180),sleep=waits.append))
    c=out[0]['capture']
    assert waits==[30] and len(c['attempts'])==2 and c['pending_retry']['retry_after_seconds']==30
    assert c['next_parameters']=={k:str(v) for k,v in request()['parameters'].items()}


def test_deadline_saves_pending_without_sending_late_retry():
    client,opener=http(busy())
    waits=[];session=RetryingRelay(client,sleep=waits.append,monotonic=lambda:0,deadline=40)
    with pytest.raises(RecordError):session.request('income',{'ts_code':'600519.SH','period':'20241231','fields':'ts_code','limit':'5'})
    assert len(opener.calls)==1 and not waits and session.pending_retry['retry_after_seconds']==30


def test_typed_financial_capture_inherits_same_page_queue_recovery():
    from decision_kernel.infrastructure.relay_financial import capture_query
    from decision_kernel.observations.relay_financial import FinancialQuery, inspect_capture
    q=FinancialQuery('income','600519.SH','20241231')
    b={'code':0,'data':{'fields':['ts_code','end_date','revenue'],'items':[['600519.SH','20241231',123]]},'count':1}
    client,opener=http(busy(),b);waits=[]
    result=capture_query(q,transport=RetryingRelay(client,sleep=waits.append),clock=lambda:STAMP)
    assert waits==[30] and inspect_capture(result)['request_complete']
    assert len(result['pages'][0]['request_attempts'])==2


@pytest.mark.parametrize('field',['abstr','author_name','fund_visitors'])
def test_generic_reader_cannot_request_report_text_or_visitor_lists(field):
    from decision_kernel.application.research_sources import validate_plan
    with pytest.raises(RecordError):validate_plan(plan(request(fields='ts_code,'+field)),registry())


def test_generic_locator_cannot_archive_private_or_signed_target():
    from decision_kernel.application.research_sources import _table
    params={'ts_code':'600519.SH','fields':'ts_code,url','limit':'100'}
    b={'code':0,'data':{'fields':['ts_code','url'],'items':[['600519.SH','https://example.org/a?token=secret']]}}
    with pytest.raises(RecordError):_table('research_report',params,b)
