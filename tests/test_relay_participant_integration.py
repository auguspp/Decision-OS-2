"""Shared HTTPS/CLI/runtime consumers, exact references, no live business requests."""
from dataclasses import asdict
from functools import partial
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.error import HTTPError

import pytest

from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT, READ_APIS
from decision_kernel.interfaces.relay_participant_cli import main
from decision_kernel.interfaces import relay_source_runtime as runtime
from test_relay_participants import query, body, capture, sources, Client, fetch

ROOTDIR=Path(__file__).resolve().parents[1]
SHA='a'*40

class Reply(BytesIO):
    status=200
    def __init__(self,raw,url):
        super().__init__(raw);self.url=url
        self.headers={'Content-Type':'application/json','Content-Length':str(len(raw)), 'X-Cache':'HIT'}
    def geturl(self):return self.url

class Opener:
    def __init__(self,response):self.response=response;self.calls=[]
    def open(self,request,timeout):
        self.calls.append(request)
        if isinstance(self.response,Exception):raise self.response
        return self.response

@pytest.mark.parametrize('api',tuple(sources.SPECS))
def test_shared_http_three_real_request_builders_only_exact_declared_fields(api):
    q=query(api);params=q.parameters();url=ROOT+'/'+api+'?'+urlencode(params)
    op=Opener(Reply(json.dumps(body(q)).encode(),url));t=RelayHTTP(opener=op,credential='synthetic-secret')
    assert t.request(api,params)['count']==1
    assert len(op.calls)==1 and op.calls[0].full_url==url and op.calls[0].get_method()=='GET'
    assert op.calls[0].get_header('X-api-key')=='synthetic-secret'
    assert t.last_response_bytes and t.last_response_meta['x_cache']=='HIT'

@pytest.mark.parametrize('changes',[{'__probe':'1'}, {'fields':'ts_code,content'}, {'limit':'1000'}, {'url':'https://evil.example'}])
def test_rejected_request_never_exposes_secret(changes):
    op=Opener(None);t=RelayHTTP(opener=op,credential='synthetic-secret')
    with pytest.raises(RecordError):t.request('stk_surv',{**query('stk_surv').parameters(),**changes})
    assert not op.calls


def test_safe_failure_classification_remains_distinct_and_single_attempt():
    op=Opener(HTTPError(ROOT,503,'private',{},BytesIO(b'{"error":"data_source_unavailable","message":"private"}')))
    t=RelayHTTP(opener=op,credential='synthetic-secret')
    c=fetch(query(),transport=t)
    assert c['error']=='RELAY_HTTP_503' and c['failure']['classification']=='source-unavailable'
    assert len(op.calls)==1 and 'private' not in json.dumps(c)


def test_registry_admission_and_private_scope_are_not_changed_by_enabled_names():
    r=RelayRegistry(ROOTDIR/'docs/sources/relay-registry.json')
    assert len(READ_APIS)==12
    for api in sources.SPECS:
        item=r.lookup(api)
        assert item['shared_relay_client_admits_api'] and item['live_enabled'] is True
        assert item['scoped_business_evidence'] is None
        assert r.check_parameters(api,query(api).parameters())['gateway_requirements_satisfied']
    assert not r.lookup('p_get')['shared_relay_client_admits_api']
    assert not r.lookup('daily')['shared_relay_client_admits_api']


def write_capture(path):
    values=[{'query':asdict(query(api)),'capture':capture(query(api)),'status':'captured'} for api in sources.SPECS]
    path.write_text(''.join(json.dumps(v,ensure_ascii=False)+'\n' for v in values))


def test_plan_is_three_api_crossproduct_offline_and_no_overwrite(tmp_path,capsys,monkeypatch):
    monkeypatch.delenv('TUSHARE_PROXY_API_KEY',raising=False)
    out=tmp_path/'plan'
    args=['plan','--codes','000001.SZ','600519.SH','--dates','20261015','20260930','--output',str(out)]
    assert main(args)==0
    rows=[json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows)==12 and all('date' in x and 'period' not in x for x in rows)
    before=out.read_bytes();assert main(args)==2 and out.read_bytes()==before


def test_capture_save_and_inspect_use_same_engine_no_new_source_during_save(tmp_path,capsys,monkeypatch):
    from decision_kernel.interfaces import relay_financial_cli as shared
    p=tmp_path/'captures';write_capture(p)
    monkeypatch.setattr(shared,'capture_query',lambda *a,**k:pytest.fail('source requested'))
    assert main(['inspect','--capture',str(p)])==0
    results=[json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert len(results)==3 and all(not x['pdf_read'] for x in results)
    local=tmp_path/'store.git';GitStore.init_synthetic(local)
    out=tmp_path/'receipts'
    args=['save','--capture',str(p),'--batch-id','participants','--public-rights-confirmed',
          '--local-synthetic-store',str(local),'--receipts',str(out)]
    assert main(args)==0
    receipts=[json.loads(x) for x in out.read_text().splitlines()]
    assert len(receipts)==3 and all(x['complete'] for x in receipts)
    ref=SourceRef.from_dict(receipts[2]['saved'][0]['reference'])
    read=VersionedStore(GitStore(local)).read_source(ref)
    assert read['manifest']['input']['published_at'] is None
    old=out.read_bytes();assert main([*args[:-1],str(tmp_path/'again')])==0
    assert (tmp_path/'again').read_bytes()==old


def test_capture_requires_secret_and_preserves_source_failure(tmp_path,capsys,monkeypatch):
    p=tmp_path/'plan';p.write_text(json.dumps(asdict(query()))+'\n')
    out=tmp_path/'out';monkeypatch.delenv('TUSHARE_PROXY_API_KEY',raising=False)
    assert main(['capture','--plan',str(p),'--live','--output',str(out)])==2 and not out.exists()
    capsys.readouterr()
    from decision_kernel.interfaces import relay_financial_cli as shared
    c=Client([RecordError('RELAY_HTTP_504')]);c.requests=1;c.validate_credential=lambda:None
    monkeypatch.setattr(shared,'RelayHTTP',lambda:c)
    assert main(['capture','--plan',str(p),'--live','--output',str(out)])==2
    item=json.loads(out.read_text());assert item['capture']['failure']['classification']=='temporary-gateway'


def context(tmp_path,**values):
    return ({'inputs':{'operation':'plan','family':'participants','code_sha':SHA,
        'codes':'000001.SZ 600519.SH','dates':'20261015 20260930',**values}},
        {'GITHUB_REPOSITORY':runtime.REPO,'GITHUB_REF':'refs/heads/main','GITHUB_EVENT_NAME':'workflow_dispatch',
         'GITHUB_SHA':SHA,'RUNNER_TEMP':str(tmp_path)})


def test_same_runtime_prepares_real_offline_participant_plan(tmp_path,monkeypatch):
    monkeypatch.delenv('TUSHARE_PROXY_API_KEY',raising=False)
    event,env=context(tmp_path);out=tmp_path/'host'
    def run(args,**kwargs):
        return subprocess.run(args,**kwargs,cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(ROOTDIR/'src')},
                              capture_output=True,text=True,timeout=20)
    assert runtime.execute('prepare',event,env,out,run=run)==0
    assert len((out/'plan.jsonl').read_text().splitlines())==12
    request=json.loads((out/'request.json').read_text())
    assert request['dates']==['20261015','20260930'] and 'periods' not in request
    assert not (out/'capture.jsonl').exists()

@pytest.mark.parametrize('values',[{'family':'all'}, {'dates':'20260230'}, {'periods':'20260630'},
    {'dates':''}, {'operation':'capture'}, {'codes':'--output'}, {'dates':'$(secret)'}])
def test_runtime_rejects_mixed_or_unauthorized_scope_before_creation(values,tmp_path):
    event,env=context(tmp_path,**values)
    with pytest.raises(ValueError):runtime.execute('prepare',event,env,tmp_path/'out',run=lambda *a,**k:pytest.fail('ran'))
    assert not (tmp_path/'out').exists()


def test_runtime_has_exact_ten_inputs_and_no_extra_secret_write_or_schedule():
    text=(ROOTDIR/'.github/workflows/relay-source-runtime.yml').read_text()
    import re
    inputs=re.findall(r'^      ([a-z_]+):$',text,re.M)
    assert len(inputs)==10 and set(inputs)==runtime.INPUTS
    assert text.count('secrets.TUSHARE_PROXY_API_KEY')==1
    assert 'contents: write' not in text and 'schedule:' not in text
    assert 'DECISION_OS_STORE_TOKEN' not in text
