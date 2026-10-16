"""Normal prepare/save/read/audit consumers; no synthetic test certifies a live Full."""
from dataclasses import asdict
import json
import subprocess

import pytest

from decision_kernel.domain.records import BlobRef, encoded
from decision_kernel.interfaces import research_runtime as runtime
from decision_kernel.infrastructure.relay_http import RelayHTTP
from test_research_runtime import host, invoke, request, source, work, inputs, CODE


def test_default_full_requires_actual_source_work_without_extra_user_prompts(host, tmp_path):
    result = invoke(host, tmp_path/'prepare', 'prepare', {'request': request()})['result']
    contract = result['full_execution']
    assert contract['required_for_full'] and contract['pool'] == 'not-executed'
    assert contract['workflow_acceptance'] == 'not-demonstrated'
    assert contract['required_actions'][0]['action'] == 'execute-pool-plan'
    assert contract['required_actions'][0]['request'] == result['request']
    assert contract['required_actions'][-1]['operation'] == 'audit-source-use'
    assert contract['partial_research_save_allowed'] and not contract['automatic_research']
    assert host.writes == 0


def test_source_audit_missing_execution_is_nonpassing_read_with_retained_report(host, tmp_path, monkeypatch):
    monkeypatch.setattr(RelayHTTP, '__init__', lambda *a, **k: pytest.fail('must not create source HTTP client'))
    payload = {'request': request(), 'progress': {
        'body':'合成公开分析，未调用系统来源', 'findings':[{'question_id':'economics', 'status':'open',
        'finding':'等待取材', 'remaining_kind':'interrupted', 'next_action':'实际使用来源入口'}],
        'stop':'continue', 'stop_reason':'未完成', 'limitations':['没有已保存来源']}, 'receipts':[]}
    report = invoke(host, tmp_path/'audit', 'audit-source-use', payload)
    assert report['status'] == 'source-use-not-demonstrated' and report['write'] == 'not-requested'
    assert report['result']['pool'] == report['result']['relay'] == 'not-executed'
    assert json.loads((tmp_path/'audit/report.json').read_bytes()) == report
    assert host.writes == 0
    assert not any('/actions/' in path for _, path, _ in host.requests)


def test_partial_save_and_original_intent_replay_survive_missing_source_execution(host, tmp_path):
    ref = invoke(host, tmp_path/'source', 'put-source', source(), 'source')['result']['reference']
    paper = work(ref)
    saved = invoke(host, tmp_path/'save', 'checkpoint', paper, 'research')['result']
    assert saved['source_use_acceptance']['status'] == 'not-demonstrated'
    replay = invoke(host, tmp_path/'replay', 'reconcile-checkpoint', paper, 'research')['result']
    assert replay == saved and host.writes == 2
    restored = invoke(host, tmp_path/'read', 'resume', {'reference':saved['reference']})['result']
    assert restored['source_use_acceptance']['reference'] == saved['reference']
    before = host.git.head()
    audit = invoke(host, tmp_path/'audit', 'audit-source-use', {'reference':saved['reference']})['result']
    assert audit['status'] == 'needs-action' and audit['reference'] == saved['reference']
    assert audit['full_product_acceptance'] == 'not-performed'
    assert host.git.head() == before and host.writes == 2


def test_actual_empty_pool_still_prompts_relay_and_final_assessment(host, tmp_path):
    plan = {'schema':'research-evidence-plan-v1', 'case_id':'example', 'queries':[
        {'query_id':'company','question_id':'economics','purpose':'合成正常公司查询','query':'合成零命中'}]}
    result = invoke(host, tmp_path/'prepare', 'prepare', {'request':request(), 'evidence_plan':plan})['result']
    contract = result['full_execution']
    assert contract['pool'] == 'no-matches-in-page'
    assert not any(a['action'] == 'execute-pool-plan' for a in contract['required_actions'])
    assert any(a['action'] == 'review-relay-and-select-plan-or-reuse' for a in contract['required_actions'])
    assert contract['workflow_acceptance'] == 'not-demonstrated'


def test_checked_interface_plan_returns_next_capture_not_executed_status(host, tmp_path):
    from test_research_sources import plan as relay_plan
    plan = {'schema':'research-evidence-plan-v1', 'case_id':'example', 'queries':[
        {'query_id':'company','question_id':'economics','purpose':'合成查询','query':'合成'}],
        'relay_plan': {**relay_plan(), 'case_id':'example'}}
    result = invoke(host, tmp_path/'prepare', 'prepare', {'request':request(), 'evidence_plan':plan})['result']
    assert result['full_execution']['relay'] == 'checked-not-executed'
    action = result['full_execution']['required_actions'][0]
    assert action['action'] == 'capture-or-justify-reuse' and action['source_plan'] == plan['relay_plan']
    assert not result['full_execution']['automatic_research'] and host.writes == 0


def test_native_main_exit_is_nonzero_but_partial_assessment_remains_readable(host, tmp_path, monkeypatch, capsys):
    from test_research_runtime import native_environment
    payload = {'request':request(), 'progress':{'body':'合成报告','findings':[
        {'question_id':'economics','status':'answered','finding':'仅为合成自述'}],
        'stop':'continue','stop_reason':'继续取材','limitations':['尚未引用来源']}}
    native_environment(monkeypatch, host, tmp_path, inputs('audit-source-use', payload))
    assert runtime.main(['--out', str(tmp_path/'out')]) == 2
    console = capsys.readouterr()
    assert payload['progress']['body'] not in console.out
    report = json.loads((tmp_path/'out/report.json').read_text())
    assert report['result']['status'] == 'needs-action' and report['write'] == 'not-requested'
    assert host.writes == 0


def exercise_installed_source_use(command, outside, python, env, executable, store):
    """Extend the one existing cold-wheel consumer; no second installation or source call."""
    request0 = {'case_id':'source-use-work', 'kind':'full','mandate':'合成执行证据回归',
                'questions':[{'question_id':'q','text':'是否实际使用来源？'}]}
    path = outside/'source-use-request.json';path.write_bytes(encoded(request0))
    prepared = command('prepare-research','--request',str(path))
    assert prepared['full_execution']['pool'] == 'not-executed'
    progress = {'body':'只有合成公开分析，不是系统验收通过','findings':[
        {'question_id':'q','status':'open','finding':'尚未取材','remaining_kind':'interrupted','next_action':'执行取材'}],
        'stop':'continue','stop_reason':'执行证据未完','limitations':['无来源回执']}
    progress_path = outside/'source-use-progress.json';progress_path.write_bytes(encoded(progress))
    saved = command('checkpoint-research','--request',str(path),'--input',str(progress_path),
                    '--submission-id','source-use-partial','--revision-id','v1')
    assert saved['source_use_acceptance']['status'] == 'not-demonstrated'
    audit_path = outside/'source-use-audit.json';audit_path.write_bytes(encoded({'reference':saved['reference']}))
    clean = {k:v for k,v in env.items() if not k.startswith(('GH_', 'GITHUB_', 'DECISION_OS_', 'TUSHARE_'))}
    result = subprocess.run([str(executable),'store','--path',str(store),'audit-source-use',
        '--input',str(audit_path)], cwd=outside, env=clean, capture_output=True, text=True)
    assert result.returncode == 2 and result.stderr == ''
    report = json.loads(result.stdout)
    assert report['pool'] == report['relay'] == 'not-executed' and report['reference'] == saved['reference']
    assert report['full_product_acceptance'] == 'not-performed'
    assert command('current','--case-id','source-use-work')['reference'] == saved['reference']

    # Same installed wheel: complementary projections keep native row/version/null witnesses.
    from test_multi_capture_coverage import exercise_installed_multi_capture
    exercise_installed_multi_capture(outside, python, clean)
