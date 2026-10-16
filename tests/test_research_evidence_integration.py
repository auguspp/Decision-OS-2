"""Normal prepare/host/installed consumption; existing writers retain ownership."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from decision_kernel.application.research_evidence import PLAN
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces import research_runtime as runtime
from decision_kernel.infrastructure.relay_http import RelayHTTP
from test_research_runtime import host, invoke, request, source, work
from test_research_sources import plan as relay_plan, request as relay_request


def evidence_plan():
    return {'schema': PLAN, 'case_id': 'example', 'queries': [
        {'query_id': 'company', 'question_id': 'economics', 'purpose': '合成跨池取材', 'query': '铜'}]}


def test_normal_host_prepare_discovers_then_existing_writer_and_reader_consume(host, tmp_path):
    saved = invoke(host, tmp_path/'source', 'put-source', source(text='铜与现金的合成材料'), 'pool')
    before = host.writes
    prepared = invoke(host, tmp_path/'prepare', 'prepare', {'request':request(), 'evidence_plan':evidence_plan()})
    page = prepared['result']['evidence']
    assert prepared['write'] == 'not-requested' and host.writes == before
    assert prepared['result']['previous_context'] is None and page['sources'][0]['data'] == '铜与现金的合成材料'
    ref = page['sources'][0]['reference']
    assert ref['manifest']['path'] == saved['result']['reference']['manifest']['path']
    paper = work(ref)
    revision = invoke(host, tmp_path/'checkpoint', 'checkpoint', paper, 'adoption')['result']['reference']
    restored = invoke(host, tmp_path/'independent', 'resume', {'reference':revision})
    assert restored['result']['source_page']['items'][0]['view']['data'] == '铜与现金的合成材料'
    assert restored['research_assessment'] == 'not-performed'


def test_evidence_pages_use_explicit_request_not_floating_case_selection(host, tmp_path):
    for ident in ('b', 'c'):
        invoke(host, tmp_path/ident, 'put-source', source(source_id=ident, text='铜 '+ident), ident)
    result = invoke(host, tmp_path/'prepare', 'prepare',
                    {'request':request(), 'evidence_plan':evidence_plan(), 'evidence_limit':1})['result']
    invoke(host, tmp_path/'a', 'put-source', source(source_id='a',text='铜 later'), 'a')
    payload = result['evidence']['next_prepare_payload']
    assert 'request' in payload and 'case_id' not in payload
    tail = invoke(host, tmp_path/'tail', 'prepare', payload)['result']['evidence']
    assert tail['snapshot'] == result['evidence']['snapshot']
    assert [s['source_id'] for s in tail['sources']] == ['c']
    assert tail['next_cursor'] is None


def test_linked_version_accounting_uses_all_predecessor_refs_not_only_reader_page(host, tmp_path):
    refs = [invoke(host, tmp_path/ident, 'put-source', source(source_id=ident,text='铜 '+ident), ident)['result']['reference']
            for ident in ('one', 'two')]
    paper = work(refs[0]);paper['progress']['sources'] = refs
    invoke(host, tmp_path/'save', 'checkpoint', paper, 'save')
    result = invoke(host, tmp_path/'prepare', 'prepare',
                    {'case_id':'example', 'source_limit':1, 'evidence_plan':evidence_plan()})['result']
    assert result['previous_context']['source_page']['remaining'] == 1
    assert all(s['linked_version_in_predecessor'] for s in result['evidence']['sources'])
    assert len(result['evidence']['sources']) == 2


def test_actual_registry_checks_interface_plan_without_requesting_credentials(host, tmp_path, monkeypatch):
    invoke(host, tmp_path/'source', 'put-source', source(text='铜'), 'pool')
    p = evidence_plan();p['relay_plan'] = {**relay_plan(relay_request()), 'case_id':'example'}
    monkeypatch.setattr(RelayHTTP, '__init__', lambda *a, **k: pytest.fail('must not create source client'))
    result = invoke(host, tmp_path/'prepare', 'prepare', {'request':request(), 'evidence_plan':p})['result']
    interface = result['evidence']['interface_sources']
    assert interface['status'] == 'checked-not-executed' and interface['network_requests'] == 0
    assert interface['plan'] == p['relay_plan'] and not interface['pool_hits_cancel_requests']
    assert interface['checked']['requests'][0]['api'] == 'daily'
    assert result['source_use']['available_from_start'] and not result['source_use']['gap_required']


def test_source_plan_denied_route_does_not_fall_through_to_capture(host, tmp_path, monkeypatch):
    p = evidence_plan();p['relay_plan'] = {**relay_plan(relay_request(api='p_get')), 'case_id':'example'}
    monkeypatch.setattr(RelayHTTP, '__init__', lambda *a, **k: pytest.fail('must not create source client'))
    with pytest.raises(RecordError):
        invoke(host, tmp_path/'prepare', 'prepare', {'request':request(), 'evidence_plan':p})
    assert host.writes == 0


def test_pool_failure_does_not_discard_previous_context_or_checked_interface_plan(host, tmp_path, monkeypatch):
    ref = invoke(host, tmp_path/'source', 'put-source', source(text='铜'), 'pool')['result']['reference']
    invoke(host, tmp_path/'checkpoint', 'checkpoint', work(ref), 'save')
    p = evidence_plan();p['relay_plan'] = {**relay_plan(), 'case_id':'example'}
    def fail(*a, **kw):
        raise RecordError('QUERY_VERSION_LIMIT_EXCEEDED')
    monkeypatch.setattr(VersionedStore, 'search', fail)
    result = invoke(host, tmp_path/'prepare', 'prepare', {'case_id':'example','evidence_plan':p})['result']
    assert result['previous_context']['source_page']['items'][0]['view']['data'] == '铜'
    assert result['evidence']['status'] == 'search-unavailable'
    assert result['evidence']['interface_sources']['status'] == 'checked-not-executed'


def test_old_prepare_still_returns_method_without_running_unscoped_pool_search(host, tmp_path, monkeypatch):
    monkeypatch.setattr(VersionedStore, 'search', lambda *a, **k: pytest.fail('no inferred unscoped scan'))
    result = invoke(host, tmp_path/'prepare', 'prepare', {'request':request()})['result']
    assert result['evidence']['status'] == 'plan-required' and result['method']['id'] == 'decision-os-research-full-v2'
    assert result['source_use']['available_from_start']


def test_cursor_without_plan_and_evidence_in_resume_are_rejected(host, tmp_path):
    with pytest.raises(RecordError, match='EVIDENCE_PLAN_REQUIRED'):
        invoke(host, tmp_path/'cursor', 'prepare', {'request':request(), 'evidence_cursor':{}})
    with pytest.raises(RecordError, match='SELECTOR'):
        invoke(host, tmp_path/'resume', 'resume', {'case_id':'example', 'evidence_plan':evidence_plan()})
    assert host.writes == 0


def exercise_installed_evidence(command, outside):
    """Called by the single existing cold-wheel test, not another installation."""
    request_path, plan_path = outside/'evidence-request.json', outside/'evidence-plan.json'
    request0 = {'case_id':'evidence-work', 'kind':'full', 'mandate':'合成材料消费，不是公司投资研究',
                'questions':[{'question_id':'q', 'text':'这些合成材料说明什么？'}]}
    request_path.write_text(json.dumps(request0,ensure_ascii=False), encoding='utf-8')
    p = {'schema':PLAN,'case_id':'evidence-work','queries':[
        {'query_id':'pool','question_id':'q','purpose':'不需要预知SourceRef的取材','query':'installed-evidence-unique'}]}
    plan_path.write_text(json.dumps(p,ensure_ascii=False), encoding='utf-8')
    source_path = outside/'evidence-source.json'
    for ident in ('evidence-b','evidence-c'):
        source_path.write_text(json.dumps({'source_id':ident,'kind':'content','title':'合成跨池资料',
            'origin':'synthetic:evidence-installed','text':'installed-evidence-unique '+ident}),encoding='utf-8')
        command('put-source','--submission-id',ident,'--input',str(source_path))
    first = command('prepare-research','--request',str(request_path),'--evidence-plan',str(plan_path),'--evidence-limit','1')
    assert first['previous_context'] is None and first['evidence']['sources'][0]['source_id'] == 'evidence-b'
    cursor_path = outside/'evidence-cursor.json'
    cursor_path.write_text(json.dumps(first['evidence']['next_cursor']),encoding='utf-8')
    source_path.write_text(json.dumps({'source_id':'evidence-a','kind':'content','title':'后来到达的合成资料',
        'origin':'synthetic:evidence-installed','text':'installed-evidence-unique later'}),encoding='utf-8')
    command('put-source','--submission-id','evidence-a','--input',str(source_path))
    tail = command('prepare-research','--request',str(request_path),'--evidence-plan',str(plan_path),
                   '--evidence-cursor',str(cursor_path),'--evidence-limit','1')['evidence']
    assert tail['snapshot'] == first['evidence']['snapshot'] and tail['sources'][0]['source_id'] == 'evidence-c'
    refs = [first['evidence']['sources'][0]['reference'], tail['sources'][0]['reference']]
    progress = {'body':'合成材料已取到；不是经济结论。','findings':[{'question_id':'q','status':'open','finding':'有待分析',
        'remaining_kind':'acquired-unanalysed','next_action':'检查合成解释','evidence':[0,1]}],
        'sources':refs,'stop':'continue','stop_reason':'尚未完成分析与挑战'}
    progress_path = outside/'evidence-progress.json';progress_path.write_text(json.dumps(progress,ensure_ascii=False),encoding='utf-8')
    revision = command('checkpoint-research','--request',str(request_path),'--input',str(progress_path),
                       '--submission-id','evidence-adopt','--revision-id','v1')['reference']
    ref_path = outside/'evidence-revision.json';ref_path.write_text(json.dumps(revision),encoding='utf-8')
    restored = command('prepare-research','--ref',str(ref_path),'--evidence-plan',str(plan_path))
    assert len(restored['previous_context']['source_page']['items']) == 2
    assert sum(s['linked_version_in_predecessor'] for s in restored['evidence']['sources']) == 2
    assert restored['previous_context']['source_page']['items'][1]['view']['data'].endswith('evidence-c')
