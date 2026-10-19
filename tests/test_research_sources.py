"""Whole-cycle source use with actual catalogue and synthetic HTTP/Git, not a live Full."""
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import (
    STAGES, SCHEMA, admitted, validate_plan, capture_plan, inspect_result, source_inputs, _table,
)
from decision_kernel.domain.records import RecordError, SourceRef
from decision_kernel.infrastructure.relay_http import ROOT, RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy, RetryingRelay, business_retry_delay

ROOT_DIR = Path(__file__).resolve().parents[1]
STAMP = '2026-10-16T02:00:00+00:00'


def registry():
    return RelayRegistry(ROOT_DIR/'docs/sources/relay-registry.json')


def request(stage='baseline', api='daily', **params):
    return {'stage': stage, 'purpose': '观察并核对来源，不是投资判断', 'api': api,
            'parameters': {'ts_code': '600519.SH', 'trade_date': '20260930',
                           'fields': 'ts_code,trade_date,close', 'limit': 100, **params}}


def plan(*items):
    return {'schema': SCHEMA, 'case_id': 'synthetic-lifecycle', 'requests': list(items or [request()])}


class Reply(BytesIO):
    status = 200
    def __init__(self, body, url, headers=None):
        super().__init__(json.dumps(body).encode()); self.url = url; self.headers = headers or {}
    def geturl(self): return self.url


class Opener:
    def __init__(self, values): self.values = iter(values); self.calls = []
    def open(self, req, timeout):
        self.calls.append(req)
        value = next(self.values)
        if isinstance(value, Exception): raise value
        return Reply(value, req.full_url)


def body(close=123.45):
    return {'code': 0, 'api_name': 'daily', 'data': {'fields': ['ts_code', 'trade_date', 'close'],
            'items': [['600519.SH', '20260930', close]]}, 'count': 1}


def http(*values):
    opener = Opener(values)
    return RelayHTTP(opener=opener, credential='synthetic-key-123'), opener


def busy(status=504, error='upstream_pool_exhausted', headers=None):
    return HTTPError(ROOT, status, 'unused private prose', headers or {},
                     BytesIO(json.dumps({'error': error, 'message': 'never persist this'}).encode()))


def test_new_full_preparation_has_sources_without_prior_findings(tmp_path):
    from decision_kernel.application.research_workflow import ResearchWorkflow
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    from decision_kernel.domain.research_work import ResearchRequest, ResearchQuestion
    store = VersionedStore(GitStore.init_synthetic(tmp_path/'new.git'))
    before = store.backend.head()
    req = ResearchRequest('new-full', 'full', '既有研究委托范围的合成验证', (ResearchQuestion('business', '经济结构是什么'),))
    prepared = ResearchWorkflow(store).prepare(req)
    assert prepared['previous_context'] is None and prepared['source_use']['available_from_start']
    assert not prepared['source_use']['gap_required'] and prepared['source_use']['stages'] == list(STAGES)
    assert store.backend.head() == before and prepared['research_assessment'] == 'not-performed'


def test_all_stages_can_use_one_capture_not_seven_repeated_requests():
    client, opener = http(body())
    out = list(capture_plan(plan(*(request(s) for s in STAGES)), registry(), client=client,
                            public_rights_confirmed=True, clock=lambda: STAMP))
    assert len(out) == 7 and len(opener.calls) == 1
    assert out[0]['status'] == 'captured'
    assert all(i['capture']['complete'] for i in out)
    assert [i['use']['stage'] for i in out] == list(STAGES)
    assert all(i['status'] == 'reused-in-this-batch' for i in out[1:])


def test_batch_not_limited_to_legacy_twelve_or_cost_quota():
    r = registry()
    assert len([x for x in r.select(enabled=True) if x['group'] not in ('needs-review', 'portfolio-private')
                and not x['private_or_write_scope']]) > 200
    assert admitted(r, 'daily')['api'] == 'daily'
    assert r.lookup('daily')['registered_reader_candidate'] and not r.lookup('daily')['shared_relay_client_admits_api']
    assert not r.lookup('p_get')['registered_reader_candidate']
    many = [request(ts_code=f'{n:06}.SZ') for n in range(200)]
    assert len(validate_plan(plan(*many), r)['requests']) == 200


@pytest.mark.parametrize('api', ['p_list', 'p_get', 'p_save', 'p_delete', 'us_depth', 'the_member', '../daily'])
def test_unadmitted_routes_do_not_request(api):
    client, opener = http(body())
    with pytest.raises(RecordError):
        list(capture_plan(plan(request(api=api)), registry(), client=client, public_rights_confirmed=True))
    assert not opener.calls


@pytest.mark.parametrize('change', [{'token':'x'}, {'url':'https://elsewhere.test'}, {'__probe':1},
    {'arbitrary_override':'x'}, {'fields':'ts_code,content'}, {'fields':'ts_code,email'},
    {'fields':'*'}, {'fields':'ts_code,ts_code'}, {'limit':0}, {'limit':True}, {'limit':5001}, {'offset':-1}, {'ts_code':'x\n'}])
def test_invalid_batch_rejected_before_first_request(change):
    client, opener = http(body())
    with pytest.raises(RecordError):
        list(capture_plan(plan(request(), request(**change)), registry(), client=client, public_rights_confirmed=True))
    assert not opener.calls


def test_rights_not_inferred_from_plan_or_catalogue():
    client, opener = http(body())
    with pytest.raises(RecordError, match='RIGHTS'):
        list(capture_plan(plan(), registry(), client=client))
    assert not opener.calls


def test_queue_waits_thirty_and_preserves_original_request_and_history():
    client, opener = http(busy(), body())
    waits = []
    out = list(capture_plan(plan(), registry(), client=client, public_rights_confirmed=True,
                            sleep=waits.append, clock=lambda: STAMP))
    assert waits == [30] and len(opener.calls) == 2
    assert opener.calls[0].full_url == opener.calls[1].full_url
    attempts = out[0]['capture']['pages'][0]['attempts']
    assert len(attempts) == 2 and attempts[0]['failure']['business_error'] == 'upstream_pool_exhausted'
    assert 'never persist' not in json.dumps(out)


def test_long_retry_after_not_shortened_or_hidden():
    client, opener = http(busy(headers={'Retry-After':'600'}))
    waits = []
    out = list(capture_plan(plan(), registry(), client=client, public_rights_confirmed=True, sleep=waits.append))
    assert not waits and len(opener.calls) == 1
    assert out[0]['capture']['pending_retry']['retry_after_seconds'] == 600
    assert not out[0]['capture']['complete']


@pytest.mark.parametrize('status,error', [(401,'unauthorized'),(403,'forbidden'),(400,'invalid_params'),
    (503,'data_source_unavailable'),(503,'unknown_vendor_code')])
def test_nonqueue_errors_are_not_retried(status,error):
    client, opener = http(busy(status,error))
    waits=[]
    out=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,sleep=waits.append))
    assert not waits and len(opener.calls)==1 and not out[0]['capture']['complete']


def test_rate_denial_stops_batch_without_valid_wait_header():
    client, opener = http(busy(429,'rate_limited'))
    out=list(capture_plan(plan(request(),request(api='daily_basic')),registry(),client=client,public_rights_confirmed=True))
    assert len(opener.calls)==1 and out[1]['status']=='not-attempted'


def test_http200_queue_body_also_waits():
    client, opener = http({'code':1,'error':'upstream_pool_exhausted'},body())
    waits=[]
    out=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,sleep=waits.append))
    assert waits==[30] and out[0]['capture']['complete']


def test_same_failed_query_is_not_reissued_under_another_stage():
    client, opener = http(busy(headers={'Retry-After':'600'}))
    out=list(capture_plan(plan(request(),request('challenge')),registry(),client=client,public_rights_confirmed=True))
    assert len(opener.calls)==1 and out[1]['status']=='prior-incomplete-in-this-batch'


def test_page_failure_keeps_good_page_and_only_retries_current_offset():
    empty={'code':0,'data':{'fields':body()['data']['fields'],'items':[]},'count':0}
    client, opener = http(body(),busy(),empty)
    waits=[]
    out=list(capture_plan(plan(request(limit=1)),registry(),client=client,public_rights_confirmed=True,sleep=waits.append))
    assert out[0]['capture']['complete'] and len(out[0]['capture']['pages'])==2
    assert 'offset=1' in opener.calls[1].full_url and opener.calls[1].full_url==opener.calls[2].full_url
    assert waits==[30]


@pytest.mark.parametrize('mutation', ['code','api','width','scope','nan','count','nested'])
def test_bad_table_is_not_qualified(mutation):
    b=body()
    if mutation=='code': b['code']=False
    if mutation=='api': b['api_name']='income'
    if mutation=='width': b['data']['items'][0].pop()
    if mutation=='scope': b['data']['items'][0][0]='000001.SZ'
    if mutation=='nan': b['data']['items'][0][-1]=float('nan')
    if mutation=='count': b['count']=-1
    if mutation=='nested': b['data']['items'][0][-1]={'private':'value'}
    with pytest.raises(RecordError): _table('daily',{k:str(v) for k,v in request()['parameters'].items()},b)


def test_unrequested_personal_or_text_columns_not_retained():
    b=body(); b['data']['fields']+=['content','email'];b['data']['items'][0]+=['private long prose','contact@example.test']
    client,_=http(b)
    result=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True))[0]
    assert result['capture']['complete'] and 'private long prose' not in json.dumps(result)
    assert result['capture']['pages'][0]['table']['omitted_unrequested_columns']==2


def test_inspected_pages_and_parameters_are_isolated_from_the_supplied_capture():
    client, _ = http(body())
    captured = list(capture_plan(plan(), registry(), client=client,
        public_rights_confirmed=True, clock=lambda: STAMP))[0]
    original = deepcopy(captured)
    view = inspect_result(captured, registry())
    assert captured == original and view['complete'] and view['row_count'] == 1
    assert set(view['pages'][0]) == {'offset', 'captured_at', 'table', 'transport'}
    view['pages'][0]['table']['items'][0][-1] = 'changed return value'
    view['pages'][0]['transport']['http_status'] = 403
    view['parameters']['trade_date'] = '20260101'
    assert captured == original

    later = inspect_result(captured, registry())
    expected = deepcopy(later)
    captured['capture']['pages'][0]['table']['items'][0][-1] = 'changed input'
    captured['capture']['pages'][0]['table']['fields'].append('private_column')
    captured['use']['parameters']['trade_date'] = '20260101'
    assert later == expected


@pytest.mark.parametrize('mutation', ['scope', 'nonfinite', 'transport', 'completeness'])
def test_inspection_rechecks_nested_input_mutated_after_a_successful_read(mutation):
    client, _ = http(body())
    captured = list(capture_plan(plan(), registry(), client=client,
        public_rights_confirmed=True, clock=lambda: STAMP))[0]
    assert inspect_result(captured, registry())['complete']
    page = captured['capture']['pages'][0]
    if mutation == 'scope':
        page['table']['items'][0][0] = '000001.SZ'
    elif mutation == 'nonfinite':
        page['table']['items'][0][-1] = float('nan')
    elif mutation == 'transport':
        page['transport']['wire_sha256'] = 'changed'
    else:
        page['table']['reported_count'] = 101
    with pytest.raises(RecordError):
        inspect_result(captured, registry())
    assert client.requests == 1


def test_existing_store_and_next_research_reader_consume_same_source(tmp_path):
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    from decision_kernel.application.research_workflow import ResearchWorkflow
    from decision_kernel.domain.research_work import ResearchRequest, ResearchQuestion, ResearchProgress, QuestionFinding
    client,_=http(body())
    results=list(capture_plan(plan(),registry(),client=client,public_rights_confirmed=True,clock=lambda:STAMP))
    source=source_inputs(results[0], registry())[0]
    path=tmp_path/'sources.git'; store=VersionedStore(GitStore.init_synthetic(path))
    ref=store.put_source('one-source',source)
    read=VersionedStore(GitStore(path)).read_source(ref)
    assert read['data']==source.text and not read['original_verified']
    request0=ResearchRequest('synthetic-lifecycle','full','Synthetic current mandate',(ResearchQuestion('business','What changed?'),))
    progress=ResearchProgress('仅证明来源链路，未评价经济事实。',(QuestionFinding('business','answered','来源已读取',(0,)),),
                             'continue','继续分析与挑战',sources=(ref,))
    workflow=ResearchWorkflow(store)
    saved=workflow.checkpoint(request0,progress,submission_id='checkpoint',revision_id='v1')
    restored=ResearchWorkflow(VersionedStore(GitStore(path))).prepare(case_id='synthetic-lifecycle')
    assert restored['source_use']['available_from_start'] and not restored['source_use']['gap_required']
    assert restored['previous_context']['source_page']['items'][0]['view']['data']==source.text
    assert saved['research_assessment']=='not-performed'
