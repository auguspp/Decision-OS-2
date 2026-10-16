"""Recovery interprets authenticated attempts, never removes failure history or grants acceptance."""
from copy import deepcopy
from dataclasses import replace

import pytest

from decision_kernel.application.research_source_recovery import relay_recovery
from decision_kernel.domain.records import RecordError
from test_research_source_use import (setup, request, progress, plan, add_source, pool_proof, relay_proof, audit)


def row(ident, state='complete', *, query_key='a', period='20260630', fields='code,cash'):
    return {'run_id':ident, 'run_attempt':1, 'query_index':0, 'started_at':'2026-10-16T01:00:00Z',
            'case_id':'case', 'query_key':query_key, 'api':'cashflow', 'state':state,
            'parameters':{'ts_code':'123456.SZ','period':period,'fields':fields,'limit':'100'}}


def link(a=1,b=2,**changes):
    return {'from':{'run_id':a,'run_attempt':1,'query_index':0},
            'to':{'run_id':b,'run_attempt':1,'query_index':0}, 'reason':'Synthetic explicit field correction',**changes}


def test_exact_later_success_recovers_without_removing_failure():
    attempts=[row(1,'incomplete'),row(2)]
    original=deepcopy(attempts)
    result=relay_recovery(attempts,[])
    assert result['state']=='captured' and result['historical_attempt_count']==2
    assert result['current_queries'][0]['historical_incomplete_count']==1
    assert result['current_queries'][0]['recovered_by']==link()['to']
    assert attempts==original


def test_newer_failure_cannot_be_masked_by_old_success_or_caller_order():
    attempts=[row(3,'incomplete'),row(1,'incomplete'),row(2)]
    result=relay_recovery(attempts,[link()])
    assert result['state']=='capture-incomplete' and result['rejected_corrections']
    assert result['current_queries'][0]['selected_attempt']['run_id']==3


def test_superset_same_fields_and_scope_can_recover_explicitly():
    result=relay_recovery([row(1,'incomplete'),row(2,query_key='b',fields='code,cash,other')],[link()])
    assert result['state']=='captured' and not result['rejected_corrections']
    assert result['current_queries'][0]['state']=='complete'
    assert result['current_queries'][0]['query_key']=='a'


def test_field_correction_is_visible_and_semantics_require_review():
    result=relay_recovery([row(1,'incomplete',fields='code,bad_cash'),row(2,query_key='b')],
                          [link(field_map={'bad_cash':'cash'})])
    assert result['state']=='captured-with-corrections' and result['field_mapping_review_required']
    assert result['corrections'][0]['state']=='complete-corrected-fields'
    assert result['current_queries'][0]['historical_incomplete_count']==1


def test_corrected_subset_does_not_silently_drop_other_requested_fields():
    result=relay_recovery([row(1,'incomplete',fields='code,bad_cash,other'),row(2,query_key='b')],
                         [link(field_map={'bad_cash':'cash'})])
    assert result['state']=='capture-incomplete'
    assert result['current_queries'][0]['state']=='partial-recovery'
    assert result['current_queries'][0]['missing_fields']==['other']


@pytest.mark.parametrize('change',[{'period':'20260331'},{'ts_code':'654321.SZ'}, {'offset':'100'}, {'report_type':'4'}])
def test_scope_changes_cannot_be_used_as_recovery(change):
    a,b=row(1,'incomplete'),row(2,query_key='b');b['parameters'].update(change)
    result=relay_recovery([a,b],[link()])
    assert result['state']=='capture-incomplete' and result['rejected_corrections']
    assert result['current_queries'][0]['state']=='incomplete'


@pytest.mark.parametrize('bad',[{'cash':'cash'},{'bad_cash':'not_returned'}, {'a':'cash'}, {'bad_cash':True}])
def test_invalid_field_mapping_never_erases_failed_attempt(bad):
    result=relay_recovery([row(1,'incomplete',fields='code,bad_cash'),row(2,query_key='b')],[link(field_map=bad)])
    assert result['rejected_corrections'] and result['state']=='capture-incomplete'


def test_plan_only_queries_remain_unexecuted_beside_other_successes():
    result=relay_recovery([row(1),row(2,'planned-not-captured',query_key='b')],[])
    assert result['state']=='capture-incomplete'
    assert result['current_queries'][1]['state']=='planned-not-captured'


def test_audit_exact_native_recovery_can_pass_only_with_pool_and_source_adoption(setup):
    store,api=setup
    failed,_=relay_proof(store,api,ident=200,fail=True)
    good,refs=relay_proof(store,api,ident=201)
    pool,_=pool_proof(store,api,p=plan('600519.SH'),ident=202)
    result=audit(store,api,progress(*refs),[failed,good,pool])
    assert result['status']=='usage-demonstrated' and result['relay']=='captured'
    assert result['relay_results'][0]['state']=='incomplete'
    assert result['relay_recovery']['current_queries'][0]['recovered_by']['run_id']==201
    assert result['full_product_acceptance']=='not-performed'
    no_adoption=audit(store,api,receipts=[failed,good,pool])
    assert no_adoption['relay']=='captured' and no_adoption['status']=='needs-action'
    assert 'NO_READABLE_ADOPTED_SOURCE' in no_adoption['gaps']


def test_pool_retry_keeps_failure_history_but_complete_new_snapshot_can_recover(setup,monkeypatch):
    store,api=setup;add_source(store)
    original=store.search
    monkeypatch.setattr(store,'search',lambda *a,**k: (_ for _ in ()).throw(RecordError('REMOTE_REQUEST_LIMIT')))
    failed,_=pool_proof(store,api,ident=100)
    monkeypatch.setattr(store,'search',original)
    # New source version changes the observed store, not the original plan/mandate.
    add_source(store,ident='new',text='additional company material')
    recovered,_=pool_proof(store,api,ident=101)
    result=audit(store,api,receipts=[recovered,failed])
    assert result['pool']=='queried' and len(result['pool_history'])==2
    assert any(p['status']=='search-unavailable' for p in result['pool_history'])
    assert result['relay']=='not-executed' and result['status']=='needs-action'


def test_later_failed_pool_scan_does_not_inherit_old_completeness(setup,monkeypatch):
    store,api=setup;add_source(store);good,_=pool_proof(store,api,ident=100)
    monkeypatch.setattr(store,'search',lambda *a,**k: (_ for _ in ()).throw(RecordError('REMOTE_REQUEST_LIMIT')))
    failed,_=pool_proof(store,api,ident=101)
    result=audit(store,api,receipts=[failed,good])
    assert result['pool']=='query-failed' and len(result['pool_history'])==2


def test_pool_tail_in_new_snapshot_cannot_borrow_old_first_page(setup):
    store,api=setup;add_source(store);first,_=pool_proof(store,api,ident=100)
    add_source(store,ident='new')
    _,page=pool_proof(store,api,ident=101,limit=1)
    tail,_=pool_proof(store,api,ident=102,limit=1,cursor=page['next_cursor'])
    result=audit(store,api,receipts=[first,tail])
    assert result['pool']=='query-coverage-unproven'
