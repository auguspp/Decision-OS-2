"""Synthetic original registered captures, not issuer facts or publication approval."""
from copy import deepcopy
from decimal import Inexact, ROUND_DOWN, localcontext
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application.main_business import read_main_business, public_summary, FIELDS
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.interfaces.main_business_cli import main, select_capture
from test_research_sources import http, plan, request

ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-10-16T02:00:00+00:00'
CUT = '2026-10-17T02:00:00+00:00'
CODE = '600519.SH'
PERIOD = '20260630'
MAIN_FIELDS = ['ts_code','end_date','bz_item','bz_sales','bz_profit','bz_cost','curr_type','update_flag']
INCOME_FIELDS = ['ts_code','end_date','report_type','revenue','oper_cost','ann_date','f_ann_date','update_flag']


@pytest.fixture(scope='module')
def registry():
    return RelayRegistry(ROOT / 'docs/sources/relay-registry.json')


def capture(registry, *, rows=None, api='fina_mainbz', dimension='P', fields=None, limit=100, complete=True):
    if fields is None:
        fields = MAIN_FIELDS if api == 'fina_mainbz' else INCOME_FIELDS
    if rows is None:
        rows = ([[CODE,PERIOD,'Alpha',100.01,20,80.01,'CNY','0'],
                 [CODE,PERIOD,'Beta',90,10,80,'CNY','1'],
                 [CODE,PERIOD,'Elimination',-50,None,-60,'CNY','0']] if api == 'fina_mainbz' else
                [[CODE,PERIOD,'1',140.01,100.01,'20260821','20260821','1']])
    params = {'ts_code': CODE, 'period': PERIOD, 'fields': ','.join(fields), 'limit': limit}
    if api == 'fina_mainbz':
        params['type'] = dimension
    use = {'api': api, 'stage': 'business', 'purpose': 'Synthetic arithmetic only', 'parameters': params}
    bodies = [{'code':0,'api_name':api,'count':len(rows),
               'data':{'fields':fields,'items':rows[n:n+limit]}} for n in range(0,len(rows),limit)]
    if not bodies or len(rows) % limit == 0:
        bodies.append({'code':0,'api_name':api,'count':len(rows),'data':{'fields':fields,'items':[]}})
    client, opener = http(*bodies)
    out = list(capture_plan(plan(use), registry, client=client, public_rights_confirmed=True,
                           clock=lambda: STAMP, max_pages=len(bodies) if complete else 1))[0]
    return out


def read(registry, value=None, **kwargs):
    return read_main_business(capture(registry) if value is None else value, registry,
        ts_code=kwargs.pop('ts_code',CODE), period=kwargs.pop('period',PERIOD),
        dimension=kwargs.pop('dimension','P'), as_of=kwargs.pop('as_of',CUT), **kwargs)


def rows(value):
    return value['capture']['pages'][0]['table']['items']


def test_signed_elimination_null_profit_and_control_arithmetic(registry):
    original, income = capture(registry), capture(registry,api='income')
    saved = deepcopy(original)
    view = read(registry,original,income_result=income)
    assert original == saved and view['status'] == 'read'
    sums = view['signed_sums'][0]['sums']
    assert sums['bz_sales']['value'] == '140.01' and sums['bz_cost']['value'] == '100.01'
    assert sums['bz_profit']['value'] is None
    eliminated = view['entries'][2]
    assert eliminated['cells']['bz_sales']['numeric_text'] == '-50'
    assert eliminated['sales_minus_cost']['value'] == '10'
    assert eliminated['cells']['bz_profit']['presence'] == 'null'
    assert eliminated['difference_ratio_percent']['value'] is None
    assert view['profit_checks'] == {'matched':2,'different':0,'not-reported':1,'unavailable':0}
    assert view['income_reconciliation']['status'] == 'matched'
    assert not view['income_reconciliation']['partition_completeness_verified']
    assert not view['income_reconciliation']['same_vintage_verified']
    assert not view['income_reconciliation']['same_currency_and_scale_verified']
    assert view['income_reconciliation']['checks'][0]['control_cell']['unit']=='currency-unit'
    assert view['issuer_roles'] == [] and view['net_benefit'] is None
    assert view['network_requests'] == 0 and not view['records_written'] and not view['research_executed']


def test_zero_and_negative_cost_no_ratio_unknown_is_not_zero(registry):
    v = capture(registry); rows(v)[0][3:6] = [0,0,0]
    view = read(registry,v)
    assert view['entries'][0]['sales_minus_cost']['value'] == '0'
    assert view['entries'][0]['difference_ratio_percent']['status'] == 'unavailable'
    rows(v)[0][5] = None
    partial = read(registry,v)
    assert partial['status'] == 'partial' and partial['scope']['missing_sales_or_cost_rows'] == 1
    assert partial['signed_sums'][0]['sums']['bz_cost']['value'] is None
    assert partial['signed_sums'][0]['sums']['bz_sales']['value'] == '40'


@pytest.mark.parametrize('flag',['0','1',None,''])
def test_singleton_update_flags_are_not_dropped(registry,flag):
    v=capture(registry); rows(v)[0][-1]=flag
    r=read(registry,v)
    assert r['scope']['qualified_rows']==3 and r['signed_sums'][0]['sums']['bz_sales']['value']=='140.01'


@pytest.mark.parametrize('changed',[False,True])
def test_duplicate_business_rows_isolated_without_latest_selection(registry,changed):
    v=capture(registry); extra=deepcopy(rows(v)[0]); extra[-1]='1'
    if changed: extra[3]+=1
    rows(v).append(extra);v['capture']['pages'][0]['table']['reported_count']=4
    r=read(registry,v)
    assert r['scope']['quarantined_rows']==2 and r['scope']['qualified_rows']==2
    assert len(r['entries'])==4
    assert all('cells' not in e for e in r['entries'] if e['status']=='quarantined')
    assert all(s['value'] is None for s in r['signed_sums'][0]['sums'].values())


@pytest.mark.parametrize('column,value',[(3,True),(3,'not-number'),(3,'1e65'),(3,'1e-65'),
    (6,None),(6,'unknown'),(6,'cny'),(7,1),(7,False),(7,'2'),(2,''),(2,None)])
def test_bad_rows_do_not_clear_other_rows_or_support_partial_sum(registry,column,value):
    v=capture(registry); rows(v)[0][column]=value
    r=read(registry,v)
    assert r['status']=='partial' and r['scope']['quarantined_rows']==1
    assert r['scope']['qualified_rows']==2
    assert r['entries'][1]['sales_minus_cost']['value']=='10'
    assert all(s['value'] is None for g in r['signed_sums'] for s in g['sums'].values())


@pytest.mark.parametrize('missing',[None,''])
def test_missing_profit_preserves_presence_and_never_reconstructs(registry,missing):
    v=capture(registry); rows(v)[0][4]=missing
    e=read(registry,v)['entries'][0]
    assert e['cells']['bz_profit']['raw']==missing and e['reported_profit_check']['status']=='not-reported'
    assert e['sales_minus_cost']['value']=='20.00'


def test_precision_context_and_one_cent_discrepancy(registry):
    v=capture(registry); rows(v)[0][4]=20.01
    with localcontext() as context:
        context.prec=3;context.rounding=ROUND_DOWN;context.traps[Inexact]=True
        r=read(registry,v)
    assert r['profit_checks']['different']==1
    assert r['entries'][0]['reported_profit_check']['difference']=='-0.01'


def test_mixed_currencies_separate_and_prevent_control_comparison(registry):
    v=capture(registry);rows(v)[1][6]='USD'
    r=read(registry,v,income_result=capture(registry,api='income'))
    assert [g['currency'] for g in r['signed_sums']]==['CNY','USD']
    assert r['income_reconciliation']['status']=='unavailable'
    assert r['income_reconciliation']['reason_code']=='MAINBZ_INCOME_CURRENCY_NOT_COMPARABLE'


@pytest.mark.parametrize('dim',['P','D','I'])
def test_dimensions_and_returned_dimension_validation(registry,dim):
    r=read(registry,capture(registry,dimension=dim),dimension=dim)
    assert r['scope']['dimension']==dim and all(e['dimension_basis']=='request-only' for e in r['entries'])
    data=[row+[dim] for row in rows(capture(registry))]
    v=capture(registry,rows=data,dimension=dim,fields=MAIN_FIELDS+['bz_code'])
    assert read(registry,v,dimension=dim)['entries'][0]['dimension_basis']=='returned-and-requested'
    rows(v)[0][-1]='wrong'
    assert read(registry,v,dimension=dim)['scope']['quarantined_rows']==1


def test_pages_keep_original_row_positions_and_filter_denominators(registry):
    v=capture(registry,limit=2)
    whole=read(registry,v)
    beta=read(registry,v,item='Beta');none=read(registry,v,item='not-covered')
    assert whole['entries'][2]['source']['page_offset']==2
    assert whole['entries'][2]['source']['row_1based']==1
    assert whole['view_version']==beta['view_version']==none['view_version']
    assert whole['scope']==beta['scope']==none['scope']
    assert whole['signed_sums']==beta['signed_sums']==none['signed_sums']
    assert len(beta['entries'])==1 and none['entries']==[]


def test_incomplete_capture_keeps_rows_but_rejects_totals(registry):
    r=read(registry,capture(registry,limit=2,complete=False))
    assert r['status']=='partial' and r['scope']['returned_rows']==2 and not r['scope']['pagination_complete']
    assert r['entries'][0]['sales_minus_cost']['value']=='20.00'
    assert r['signed_sums'][0]['sums']['bz_sales']['value'] is None


def test_empty_is_not_zero_exposure(registry):
    r=read(registry,capture(registry,rows=[]))
    assert r['status']=='empty' and r['signed_sums']==[] and r['net_benefit'] is None


@pytest.mark.parametrize('change,code',[
    ({'as_of':'2026-10-15T01:00:00Z'},'MAINBZ_CAPTURE_CLOCK'),
    ({'as_of':'2026-10-17'},'FINANCIAL_CAPTURE_TIME_INVALID'),
    ({'as_of':'2026-10-17T01:00:00'},'INVALID_SOURCE_TIME'),
    ({'period':'20261231'},'MAINBZ_FUTURE_PERIOD'),
    ({'dimension':'D'},'MAINBZ_REQUEST_FIELDS_OR_DIMENSION'),
    ({'ts_code':'000001.SZ'},'MAINBZ_REQUEST_SCOPE')])
def test_wrong_scope_or_clock_rejected(registry,change,code):
    with pytest.raises(RecordError,match=code):read(registry,**change)


@pytest.mark.parametrize('mutation',['snapshot','status','identity','period','fields','projection','nan'])
def test_cannot_relabel_or_forge_a_capture(registry,mutation):
    v=capture(registry)
    if mutation=='snapshot':v['capture']['snapshot']['sha256']='0'*64
    if mutation=='status':v['status']='capture-incomplete'
    if mutation=='identity':rows(v)[0][0]='000001.SZ'
    if mutation=='period':rows(v)[0][1]='20251231'
    if mutation=='fields':v['use']['parameters']['fields']='ts_code,end_date'
    if mutation=='projection':v['capture']['pages'][0]['table']['units']='yuan'
    if mutation=='nan':rows(v)[0][3]=float('nan')
    with pytest.raises(RecordError):read(registry,v)


@pytest.mark.parametrize('case',['wrong-period','parent','quarter','duplicate','missing','future-ann','incomplete','empty'])
def test_income_failure_is_local_and_never_latest_selected(registry,case):
    i=capture(registry,api='income')
    if case=='wrong-period':rows(i)[0][1]='20251231'
    if case=='parent':rows(i)[0][2]='6'
    if case=='quarter':rows(i)[0][2]='2'
    if case=='duplicate':rows(i).append(deepcopy(rows(i)[0]));i['capture']['pages'][0]['table']['reported_count']=2
    if case=='missing':rows(i)[0][3]=None
    if case=='future-ann':rows(i)[0][5]='20261018'
    if case=='incomplete':i=capture(registry,api='income',limit=1,complete=False)
    if case=='empty':i=capture(registry,api='income',rows=[])
    r=read(registry,income_result=i)
    assert r['status']=='read' and r['scope']['qualified_rows']==3
    assert r['income_reconciliation']['status']=='unavailable'


def test_income_uses_oper_cost_not_total_cogs_and_does_not_rename_difference(registry):
    i=capture(registry,api='income');rows(i)[0][4]=100
    r=read(registry,income_result=i)
    assert r['income_reconciliation']['status']=='different'
    assert r['income_reconciliation']['checks'][1]['difference']=='0.01'
    assert r['income_reconciliation']['checks'][1]['control_field']=='oper_cost'


def args(capture_path, output, *extra):
    return ['--capture',str(capture_path),'--registry',str(ROOT/'docs/sources/relay-registry.json'),
        '--ts-code',CODE,'--period',PERIOD,'--dimension','P','--as-of',CUT,'--output',str(output),*extra]


def test_cli_default_redaction_detail_and_no_overwrite(registry,tmp_path,capsys):
    path=tmp_path/'capture.jsonl';path.write_bytes(encoded(capture(registry))+encoded(capture(registry,api='income')))
    receipt=tmp_path/'receipt.json';detail=tmp_path/'detail.json'
    assert main(args(path,receipt,'--with-income'))==0
    body=receipt.read_text()
    assert all(x not in body for x in ('Alpha','Elimination','140.01','projection_sha256','wire_sha256','view_version'))
    assert json.loads(body)['source_values_exported'] is False
    assert main(args(path,detail,'--with-income','--include-values'))==0
    assert json.loads(detail.read_text())['entries'][0]['raw']['bz_item']=='Alpha'
    assert main(args(path,receipt))==2 and receipt.read_text()==body
    assert main(args(path,path))==2
    assert 'MAIN_BUSINESS_INPUT_OR_OUTPUT_REJECTED' in capsys.readouterr().err


def test_cli_ambiguous_capture_and_invalid_reuse_rejected(registry,tmp_path):
    a=capture(registry);b=deepcopy(a);b['use']['stage']='challenge';b['status']='reused-in-this-batch'
    p=tmp_path/'c.jsonl';p.write_bytes(encoded(a)+encoded(b))
    assert select_capture(p,'fina_mainbz',CODE,PERIOD,'P',registry=registry)==a
    b['capture']['pages'][0]['captured_at']='2026-10-16T02:00:01Z'
    p.write_bytes(encoded(a)+encoded(b))
    with pytest.raises(RecordError,match='MULTIPLE_CAPTURE'):select_capture(p,'fina_mainbz',CODE,PERIOD,'P',registry=registry)
    b=deepcopy(a);b['use']['query_key']='0'*64;p.write_bytes(encoded(a)+encoded(b))
    with pytest.raises(RecordError):select_capture(p,'fina_mainbz',CODE,PERIOD,'P',registry=registry)


def test_cli_corrupt_file_rejects_but_missing_control_keeps_main_reading(registry,tmp_path):
    p=tmp_path/'input';out=tmp_path/'out'
    p.write_text('{"use":{},"use":{}}\n')
    assert main(args(p,out))==2 and not out.exists()
    p.write_bytes(encoded(capture(registry)))
    assert main(args(p,out,'--with-income'))==2
    result=json.loads(out.read_text())
    assert result['scope']['qualified_rows']==3 and result['status']=='read'
    assert result['income_reconciliation']['reason_code']=='MAINBZ_SELECTED_CAPTURE_MISSING'


def test_fresh_process_uses_only_original_input_and_no_network(registry,tmp_path):
    p=tmp_path/'c.jsonl';p.write_bytes(encoded(capture(registry))+encoded(capture(registry,api='income')))
    blocker=tmp_path/'block';blocker.mkdir()
    (blocker/'sitecustomize.py').write_text('import sys\ndef deny(event,args):\n if event.startswith("socket."): raise AssertionError("network forbidden")\nsys.addaudithook(deny)\n')
    env={'PATH':os.environ['PATH'],'PYTHONPATH':str(blocker)+os.pathsep+str(ROOT/'src')}
    first,second=tmp_path/'a.json',tmp_path/'b.json'
    for out in (first,second):
        run=subprocess.run([sys.executable,'-m','decision_kernel.interfaces.main_business_cli',
            *args(p,out,'--with-income','--include-values')],cwd=tmp_path,env=env,capture_output=True,timeout=30)
        assert run.returncode==0,run.stderr
    assert first.read_bytes()==second.read_bytes()


@pytest.mark.parametrize('value',[{}, [], 1, True, 'untrusted prose'])
def test_control_selection_errors_are_bounded(registry,value):
    with pytest.raises(RecordError,match='MAINBZ_INCOME_SELECTION_ERROR'):
        read(registry,income_selection_error=value)


def exercise_installed_main_business(outside, python, env):
    """Reuse the original single wheel installation and two isolated consumers."""
    registry = RelayRegistry(ROOT / 'docs/sources/relay-registry.json')
    path = outside / 'main-business-original-synthetic.jsonl'
    path.write_bytes(encoded(capture(registry))+encoded(capture(registry,api='income')))
    outputs = [outside / ('main-business-installed-' + name + '.json') for name in ('first','second')]
    no_keys = {'PATH': env.get('PATH', os.defpath), 'LANG':'C.UTF-8'}
    script = ('import sys\ndef deny(event,args):\n'
        ' if event.startswith("socket."): raise AssertionError("network forbidden")\n'
        'sys.addaudithook(deny)\n'
        'from decision_kernel.interfaces.main_business_cli import main; sys.exit(main(sys.argv[1:]))')
    for output in outputs:
        process = subprocess.run([str(python), '-I', '-c', script,
            *args(path,output,'--with-income')],cwd=outside,env=no_keys,capture_output=True,timeout=30)
        assert process.returncode==0,process.stderr
    assert outputs[0].read_bytes()==outputs[1].read_bytes()
    receipt=json.loads(outputs[1].read_bytes())
    assert receipt['income_reconciliation']['status']=='matched'
    assert receipt['scope']['qualified_rows']==3 and receipt['network_requests']==0
    assert not receipt['source_values_exported'] and not receipt['records_written']
