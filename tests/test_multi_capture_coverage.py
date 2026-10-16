"""Real capture/inspection and native-receipt models; synthetic financial rows only."""
from copy import deepcopy
import json
import subprocess

import pytest

from decision_kernel.application.research_source_recovery import relay_recovery, attempt_key
from decision_kernel.application.research_sources import capture_plan, validate_plan, source_inputs, inspect_result
from decision_kernel.domain.records import encoded
from test_research_sources import registry, http, busy, STAMP
from test_research_source_use import setup, request, progress, plan, pool_proof, audit, NativeAPI, CODE

KEYS = ['ts_code','ann_date','f_ann_date','end_date','report_type','update_flag']
BASE = dict(ts_code='600519.SH', ann_date='20260821', f_ann_date='20260821', end_date='20260630', report_type='1')


def selector(ident):
    return {'run_id': ident, 'run_attempt': 1, 'query_index': 0}


def make_capture(ident, fields, values, *, failed=False, api=None, save=None):
    p = {'schema':'research-source-plan-v1', 'case_id':request().case_id, 'requests':[
        {'stage':'financial', 'purpose':'Synthetic retained projection', 'api':'cashflow',
         'parameters': {'ts_code':'600519.SH', 'period':'20260630', 'fields':','.join(fields),'limit':100}}]}
    body = {'code':0, 'api_name':'cashflow', 'count':len(values),
            'data':{'fields':fields, 'items':[[row.get(f) for f in fields] for row in values]}}
    from decision_kernel.application.research_sources import SCHEMA
    p['schema'] = SCHEMA
    checked = validate_plan(p, registry())
    raw = list(capture_plan(p, registry(), client=http(busy(403,'forbidden') if failed else body)[0],
               public_rights_confirmed=True, clock=lambda:STAMP))[0]
    inspected = inspect_result(raw, registry())
    attempt = {'run_id':ident,'run_attempt':1,'query_index':0,'started_at':'2026-10-16T00:00:00Z',
               'case_id':p['case_id'], **checked['requests'][0], 'state':'incomplete' if failed else 'complete',
               'row_count':inspected['row_count']}
    proof, refs = None, []
    if api is not None:
        docs = {'request.json':{'family':'research','operation':'capture','code_sha':CODE,
                    'public_rights_confirmed':True,'source_plan':p},
                'source-plan.json':p,'plan.json':checked,
                'result.json':{'code_sha':CODE,'status':'capture-incomplete' if failed else 'capture-returned'},
                'capture.jsonl':[raw]}
        proof = api.add(docs, family='relay', ident=ident, conclusion='failure' if failed else 'success',
                        capture_step='failure' if failed else 'success')
        if save is not None and not failed:
            refs=[save.put_source('multi-'+str(ident)+'-'+str(i), s) for i,s in enumerate(source_inputs(raw,registry()))]
    return attempt, raw, proof, refs


def scenario(api=None, save=None, *, renamed=False):
    values=[{**BASE,'update_flag':'0','cash':0,'fcf':None}, {**BASE,'update_flag':'1','cash':0,'fcf':9}]
    old_fields=KEYS+(['bad_cash'] if renamed else ['cash'])+['fcf']
    entries=[make_capture(10,old_fields,[],failed=True,api=api),
             make_capture(20,KEYS+['cash'],values,api=api,save=save),
             make_capture(30,KEYS+['fcf'],values,api=api,save=save)]
    correction={'from':selector(10), 'parts':[{'to':selector(20), 'field_map':{'bad_cash':'cash'} if renamed else {}},
                                           {'to':selector(30)}], 'reason':'Synthetic complementary captures'}
    return [e[0] for e in entries], {attempt_key(e[0]):e[1] for e in entries}, correction, entries


def run(attempts, captures, correction):
    return relay_recovery(attempts, [correction], captures=captures)


def test_multiple_captures_cover_fields_without_erasing_rows_nulls_or_history():
    a,c,link,_=scenario();before=deepcopy((a,c,link))
    result=run(a,c,link);detail,=result['corrections']
    assert result['state']=='captured' and result['historical_attempt_count']==3
    assert detail['field_coverage_complete'] and detail['row_consistency']=='aligned-retained-rows'
    assert len(detail['cohorts'])==2 and detail['missing_fields']==[]
    assert [r['value_state'] for r in detail['field_coverage']['fcf']]==['null','value']
    assert [r['value_state'] for r in detail['field_coverage']['cash']]==['value','value']
    assert detail['field_coverage']['fcf'][0]['part_index']==1
    assert detail['field_coverage']['fcf'][0]['page_index']==0
    assert detail['field_coverage']['fcf'][1]['row_index']==1
    assert detail['parts'][1]['to']==selector(30) and detail['parts'][1]['capture_sha256']
    assert detail['merged_table']=='not-created' and detail['simultaneous_snapshot']=='not-established'
    assert (a,c,link)==before


def test_field_alias_coverage_still_requires_semantic_review():
    a,c,link,_=scenario(renamed=True);result=run(a,c,link)
    assert result['state']=='captured-with-corrections' and result['field_mapping_review_required']
    assert result['corrections'][0]['field_coverage']['bad_cash'][0]['returned_field']=='cash'


@pytest.mark.parametrize('field', ['update_flag','ann_date','f_ann_date','report_type'])
def test_distinct_row_versions_are_never_blended(field):
    a,c,link,_=scenario();t=c[(30,1,0)]['capture']['pages'][0]['table']
    t['items'][0][t['fields'].index(field)]='different'
    result=run(a,c,link);detail=result['corrections'][0]
    assert detail['field_coverage_complete'] and result['state']=='capture-incomplete'
    assert 'ROW_POPULATIONS_DIFFER' in detail['row_issues']
    assert result['row_consistency_review_required']
    assert len(detail['cohorts'])==3


@pytest.mark.parametrize('field', ['ts_code','end_date','f_ann_date','report_type','update_flag'])
def test_missing_identity_prevents_composition_without_losing_field_observations(field):
    a,c,link,_=scenario();t=c[(30,1,0)]['capture']['pages'][0]['table']
    t['items'][0][t['fields'].index(field)]=None
    result=run(a,c,link)
    assert result['state']=='capture-incomplete' and result['corrections'][0]['field_coverage_complete']
    assert 'ROW_IDENTITY_INCOMPLETE' in result['corrections'][0]['row_issues']


def test_duplicate_identity_not_silently_deduplicated():
    a,c,link,_=scenario();t=c[(30,1,0)]['capture']['pages'][0]['table']
    t['items'].append(deepcopy(t['items'][0]))
    result=run(a,c,link)
    assert 'ROW_IDENTITY_AMBIGUOUS' in result['corrections'][0]['row_issues']
    assert len(result['corrections'][0]['field_coverage']['fcf'])==3


@pytest.mark.parametrize('conflict', [7, None, '', False])
def test_overlapping_values_including_null_zero_disagreement_prevent_composition(conflict):
    a,c,link,_=scenario();values=[{**BASE,'update_flag':'0','cash':conflict,'fcf':None},
                               {**BASE,'update_flag':'1','cash':0,'fcf':9}]
    a[2],c[(30,1,0)],_,_=make_capture(30,KEYS+['cash','fcf','extra_marker'],values)
    result=run(a,c,link)
    assert result['state']=='capture-incomplete'
    assert 'OVERLAPPING_VALUES_CONFLICT' in result['corrections'][0]['row_issues']
    assert any('cash' in r['conflicting_fields'] for r in result['corrections'][0]['cohorts'])


@pytest.mark.parametrize('change', [{'period':'20260331'}, {'ts_code':'000001.SZ'}, {'report_type':'4'}, {'offset':'100'}])
def test_wrong_query_scope_rejected(change):
    a,c,link,_=scenario();a[2]['parameters'].update(change)
    result=run(a,c,link)
    assert result['rejected_corrections'] and result['current_queries'][0]['state']=='incomplete'


def test_newer_failure_of_target_cannot_be_hidden_by_composition():
    a,c,link,_=scenario();later=deepcopy(a[1]);later.update(run_id=40,state='incomplete');a.append(later)
    result=run(a,c,link)
    assert result['rejected_corrections'] and result['state']=='capture-incomplete'


@pytest.mark.parametrize('mutation',['duplicate','missing','not-complete','wrong-capture','ambiguous-alias'])
def test_invalid_parts_are_not_used(mutation):
    a,c,link,_=scenario(renamed=True)
    if mutation=='duplicate':link['parts'][1]=deepcopy(link['parts'][0])
    elif mutation=='missing':link['parts'][1]['to']['run_id']=999
    elif mutation=='not-complete':a[2]['state']='planned-not-captured'
    elif mutation=='wrong-capture':c[(30,1,0)]['use']['query_key']='wrong'
    else:link['parts'][1]['field_map']={'bad_cash':'fcf'}
    assert run(a,c,link)['rejected_corrections']


def test_empty_captures_do_not_become_field_value_evidence():
    a,c,link,_=scenario();a[2],c[(30,1,0)],_,_=make_capture(30,KEYS+['fcf'],[])
    result=run(a,c,link);d=result['corrections'][0]
    assert d['missing_fields']==['fcf'] and d['field_coverage']['fcf']==[]
    assert 'EMPTY_CAPTURE_NO_ROW_EVIDENCE' in d['row_issues']


@pytest.mark.parametrize('which', ['rows','parts','cells','bytes'])
def test_composition_limits_fail_closed_without_changing_original_query(which,monkeypatch):
    from decision_kernel.application import research_field_coverage as module
    a,c,link,_=scenario()
    if which=='rows':monkeypatch.setattr(module,'MAX_ROWS',1)
    elif which=='parts':link['parts']*=5
    elif which=='cells':monkeypatch.setattr(module,'MAX_CELLS',1)
    else:monkeypatch.setattr(module,'MAX_BYTES',1)
    result=run(a,c,link)
    assert result['rejected_corrections'] and result['current_queries'][0]['state']=='incomplete'


def test_unknown_row_policy_does_not_certify_generic_fields():
    a,c,link,_=scenario()
    for r in a:r['api']='other_api'
    for r in c.values():
        r['use']['api']='other_api';r['capture']['api']='other_api'
    result=run(a,c,link)
    assert result['corrections'][0]['field_coverage_complete']
    assert 'ROW_IDENTITY_POLICY_UNAVAILABLE' in result['corrections'][0]['row_issues']
    assert result['state']=='capture-incomplete'


def test_actual_native_audit_composes_but_still_checks_adoption_and_economic_boundary(setup):
    store,api=setup;a,c,link,entries=scenario(api,store)
    refs=[r for e in entries for r in e[3]];pool,_=pool_proof(store,api,p=plan('600519.SH'),ident=40)
    proofs=[e[2] for e in entries]+[pool]
    before=store.backend.head()
    result=audit(store,api,progress(*refs),proofs,relay_recoveries=[link])
    assert not result['rejected_receipts'] and not result['relay_recovery']['rejected_corrections']
    assert result['status']=='usage-demonstrated' and result['relay']=='captured'
    assert result['research_assessment']==result['full_product_acceptance']=='not-performed'
    assert store.backend.head()==before
    absent=audit(store,api,receipts=proofs,relay_recoveries=[link])
    assert absent['status']=='needs-action' and 'RELAY_CAPTURE_NOT_ADOPTED' in absent['gaps']
    assert absent['relay_recovery']['corrections'][0]['field_coverage_complete']


def test_native_rejected_capture_cannot_contribute_composition(setup):
    store,api=setup;a,c,link,entries=scenario(api)
    api.replies['/actions/artifacts/30']['digest']='sha256:'+'0'*64
    result=audit(store,api,receipts=[e[2] for e in entries],relay_recoveries=[link])
    assert result['rejected_receipts'] and result['relay_recovery']['rejected_corrections']
    assert result['status']!='usage-demonstrated'


def test_native_aliases_never_auto_pass_even_with_adopted_refs(setup):
    store,api=setup;a,c,link,entries=scenario(api,store,renamed=True)
    refs=[r for e in entries for r in e[3]];pool,_=pool_proof(store,api,p=plan('600519.SH'),ident=40)
    result=audit(store,api,progress(*refs),[e[2] for e in entries]+[pool],relay_recoveries=[link])
    assert result['relay']=='captured-with-corrections'
    assert result['gaps']==['RELAY_FIELD_CORRECTION_REQUIRES_REVIEW']
    assert result['status']=='needs-action'


def exercise_installed_multi_capture(outside, python, env):
    a,c,link,_=scenario()
    path=outside/'multi-capture.json'
    path.write_bytes(encoded({'attempts':a,'captures':[{'key':list(k),'record':v} for k,v in c.items()], 'corrections':[link]}))
    script="""import json,sys
from pathlib import Path
from decision_kernel.application.research_source_recovery import relay_recovery
p=json.loads(Path(sys.argv[1]).read_bytes())
r=relay_recovery(p['attempts'],p['corrections'],captures={tuple(x['key']):x['record'] for x in p['captures']})
print(json.dumps(r))
"""
    result=subprocess.run([str(python),'-I','-c',script,str(path)],cwd=outside,env=env,check=True,capture_output=True,text=True)
    r=json.loads(result.stdout)
    assert r['state']=='captured' and r['corrections'][0]['missing_fields']==[]
    assert [x['value_state'] for x in r['corrections'][0]['field_coverage']['fcf']]==['null','value']


def test_optional_company_type_cannot_be_silently_discarded():
    a,c,link,_=scenario();values=[{**BASE,'update_flag':'0','comp_type':'1','fcf':None},
                               {**BASE,'update_flag':'1','comp_type':'1','fcf':9}]
    a[2],c[(30,1,0)],_,_=make_capture(30,KEYS+['comp_type','fcf'],values)
    result=run(a,c,link)
    assert 'comp_type' in result['corrections'][0]['identity_fields']
    assert 'ROW_IDENTITY_INCOMPLETE' in result['corrections'][0]['row_issues']


def test_reordered_returned_rows_match_by_identity_not_position():
    a,c,link,_=scenario();c[(30,1,0)]['capture']['pages'][0]['table']['items'].reverse()
    result=run(a,c,link)
    assert result['state']=='captured' and len(result['corrections'][0]['cohorts'])==2
    assert result['corrections'][0]['field_coverage']['fcf'][0]['identity']['update_flag']=='1'


def test_caller_cannot_supply_raw_captures_as_authenticated_evidence(setup):
    from decision_kernel.domain.records import RecordError
    store,api=setup
    with pytest.raises(RecordError,match='SOURCE_USE_INPUT_INVALID'):
        audit(store,api,captures=[])


def test_eight_proof_limit_not_increased_for_composition(setup):
    from decision_kernel.domain.records import RecordError
    store,api=setup;a,c,link,entries=scenario(api)
    with pytest.raises(RecordError,match='SOURCE_USE_RECEIPT_LIMIT'):
        audit(store,api,receipts=[entries[0][2]]*9,relay_recoveries=[link])


def test_original_read_host_handles_composition_without_source_or_record_writes(tmp_path,monkeypatch):
    from test_research_runtime import invoke
    from test_github_store import GitAPI
    from dataclasses import replace
    from decision_kernel.infrastructure.relay_http import RelayHTTP
    model=GitAPI(tmp_path/'host.git');model.binding=replace(model.binding,visibility='public',write_enabled=False)
    anchor=model.git.commit({'store.json':model.binding.marker()},expected=model.git.head())
    model.binding=replace(model.binding,anchor=anchor);model.meta['private']=False
    native=NativeAPI();a,c,link,entries=scenario(native)
    original=model.request
    def route(method,path,payload=None):
        if '/actions/' in path:
            assert method=='GET' and payload is None
            return native.request(method,path.replace('/repos/'+model.binding.repository,'/repos/'+native.binding.repository,1))
        return original(method,path,payload)
    model.request=route
    monkeypatch.setattr(RelayHTTP,'__init__',lambda *a,**kw:pytest.fail('no new acquisition'))
    from dataclasses import asdict
    payload={'request':asdict(request()), 'progress':progress(), 'receipts':[e[2] for e in entries],
             'relay_recoveries':[link]}
    before=model.git.head()
    report=invoke(model,tmp_path/'result','audit-source-use',payload)
    assert report['result']['relay']=='captured'
    assert report['status']=='source-use-not-demonstrated' and report['write']=='not-requested'
    assert json.loads((tmp_path/'result/report.json').read_bytes())==report
    assert model.writes==0 and model.git.head()==before
