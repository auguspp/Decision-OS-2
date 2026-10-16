"""Synthetic channels through existing exact Git custody, not real automotive data."""
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Inexact, ROUND_DOWN, localcontext
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.vehicle_flows import CHANNELS, read_vehicle_flows, vehicle_flows
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import Entity, MissingObject, RecordError, Selection, SourceInput, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from test_github_store import GitAPI

SUBJECT = Entity('synthetic-vehicle', 'industry', 'Synthetic passenger cars',
                 attributes=(('observation_family', 'vehicle-flows'),))
COMPANY = Entity('synthetic-carmaker', 'company', 'Synthetic issuer')
MONTHS = ('2026-07-31', '2026-08-31', '2026-09-30')
ROOT = Path(__file__).resolve().parents[1]


def retain(store):
    refs = []
    for slot, end in enumerate(MONTHS):
        rows = []
        for kind in ('period-flow', 'ytd-flow'):
            for index, metric in enumerate(CHANNELS):
                values = ['100', '110', '121'] if kind == 'period-flow' else ['500', '610', '731']
                rows.append(dict(zip(COLUMNS, [SUBJECT.entity_id, metric, 'synthetic-narrow-cars-including-ckd',
                    'CN-synthetic', 'not-applicable', '10k-vehicles', 'not-applicable', 'not-applicable',
                    'not-applicable', 'not-applicable', 'monthly', kind, 'monthly-release',
                    end[:7] + '-01' if kind == 'period-flow' else '2026-01-01', end,
                    values[slot] if index != 2 else ['90', '80', '85'][slot]])))
        stream = io.StringIO(newline=''); writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
        source = SourceInput('synthetic-vehicle-counts', 'dataset', 'Synthetic published counts',
            'synthetic:monthly-release', stream.getvalue(), entities=(SUBJECT,),
            published_at=['2026-08-10', '2026-09-10', '2026-10-10'][slot], acquired_at='2026-10-16')
        refs.append(store.put_source('vehicle-' + str(slot), source))
    quote = 'Synthetic issuer manufactures passenger cars; deliveries do not establish profitability.'
    issuer = store.put_source('issuer', SourceInput('synthetic-vehicle-issuer', 'content', 'Synthetic statutory disclosure',
        'synthetic:issuer', quote + '\n', entities=(COMPANY,), published_at='2026-10-10',
        acquired_at='2026-10-16', report_period='2026-09'))
    relation = dict(relation_id='synthetic-auto-link', subject_id=SUBJECT.entity_id, company=asdict(COMPANY),
        role='producer', business_scope='Synthetic passenger cars', report_period='2026-09',
        applicable_start='2026-09-01', applicable_end='2026-09-30',
        evidence=dict(reference=asdict(SourceRef(issuer.manifest, Selection('lines', 1, 1))),
            source_role='issuer-filing', quote=quote, issuer_binding_note='Synthetic named issuer in source metadata.'),
        exposure_size_note='No matched industry denominator or net exposure.',
        transmission={k: dict(hypothesis='Volumes might change business outcomes.',
            counterevidence='Prices, mix, scope, margins and timing not established.')
            for k in ('revenue', 'cost', 'profit', 'cash_flow')}, unknowns=['No elasticity or current business revalidation.'])
    series = []
    for row, (kind, metric) in enumerate(((k, m) for k in ('period-flow', 'ytd-flow') for m in CHANNELS), 1):
        series.append(dict(series_id=metric + '-' + kind, subject=asdict(SUBJECT), observations=[
            asdict(SourceRef(ref.manifest, Selection('rows', row, row, COLUMNS))) for ref in refs]))
    packet = dict(schema='industry-reading-v1', prepared_at='2026-10-17T01:00:00+08:00',
        as_of='2026-10-17T01:00:00+08:00', coverage_note='Synthetic eight series only; no live market.',
        use='Channel semantics, not a company Full', series=series, relations=[relation])
    ref = store.put_source('packet', SourceInput('synthetic-vehicle-packet', 'content', 'Synthetic packet',
        'synthetic:packet', encoded(packet).decode()))
    return ref, refs, issuer


@pytest.fixture(scope='module')
def saved(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp('vehicle') / 'records.git'))
    root, sources, issuer = retain(store)
    return store, root, sources, issuer


@pytest.fixture(scope='module')
def original(saved):
    store, root, _, _ = saved
    return read_industry(root, store)


def point(reading, slot=1, channel=0):
    return reading['series'][channel]['observations'][slot]


def entry(reading):
    return vehicle_flows(reading)['entries'][0]


def test_channels_and_ytd_do_not_form_inventory_or_profit(original):
    out = vehicle_flows(original)
    assert out['schema'] == 'industry-vehicle-flows-v1' and out['status'] == 'read'
    assert out['scope']['qualified_series'] == 8 and out['scope']['unavailable_vehicle_points'] == 0
    assert out['coverage'][0]['monthly_channels_not_supplied'] == []
    assert out['coverage'][0]['cumulative_channels_not_supplied'] == []
    for e in out['entries'][:4]:
        assert e['channel_meaning'] == CHANNELS[e['metric']]
        assert e['latest_change']['status'] == 'changed'
        assert e['latest_change']['from_reference'] == e['points'][1]['reference']
        assert e['latest_change']['publisher_reported_growth_verified'] is False
    assert out['entries'][0]['latest_change']['delta'] == '11'
    assert out['entries'][0]['latest_change']['percent_change'] == '10.0'
    for e in out['entries'][4:]:
        assert e['latest_change']['reason_code'] == 'INDUSTRY_CUMULATIVE_NOT_PERIOD_FLOW'
        assert e['latest_change']['delta'] is None
    assert 'CHANNEL_DIFFERENCES_NOT_INVENTORY_OR_CASH_FLOW' in out['limitations']
    assert out['company_benefit'] == 'not-assessed' and not out['research_executed']
    assert out['writes'] == out['source_requests'] == 0
    assert out['retained'] == original


@pytest.mark.parametrize('field,value,code', [
    ('unit','CNY','VEHICLE_METRIC_OR_UNIT'), ('unit','index-point','VEHICLE_METRIC_OR_UNIT'),
    ('currency','CNY','VEHICLE_NONMARKET_BASIS'), ('frequency','daily','VEHICLE_NONMARKET_BASIS'),
    ('market','spot','VEHICLE_NONMARKET_BASIS'), ('price_kind','quote','VEHICLE_NONMARKET_BASIS'),
    ('tax_basis','unknown','VEHICLE_NONMARKET_BASIS'), ('contract','future','VEHICLE_NONMARKET_BASIS'),
    ('specification','unknown','VEHICLE_SCOPE_UNKNOWN'), ('region','unspecified','VEHICLE_SCOPE_UNKNOWN'),
    ('assessment','final','VEHICLE_RELEASE_STAGE'), ('period_start','2026-08-10','VEHICLE_PERIOD_BASIS'),
    ('period_end','2026-08-30','VEHICLE_MONTH_END'), ('value',None,'VEHICLE_VALUE_MISSING'),
    ('value','-1','VEHICLE_NEGATIVE_COUNT'), ('value','NaN','INDUSTRY_DECIMAL'),
])
def test_invalid_point_is_local_never_zero(original, field, value, code):
    r = deepcopy(original); point(r)[field] = value
    out = vehicle_flows(r)
    assert out['status'] == 'partial' and out['scope']['unavailable_vehicle_points'] == 1
    bad = out['entries'][0]['points'][1]
    assert bad['value'] is None and bad['reason_code'] == code
    assert all(c['delta'] is None for c in out['entries'][0]['changes'])
    assert out['entries'][1]['latest_change']['status'] == 'changed'


@pytest.mark.parametrize('field,value', [('specification','broad-with-minivans'), ('region','other-region'),
                                      ('unit','vehicle'), ('assessment','preliminary-scan')])
def test_changed_scope_stage_or_units_not_auto_converted(original, field, value):
    r = deepcopy(original); point(r)[field] = value
    changes = entry(r)['changes']
    assert all(c['reason_code'] == 'INDUSTRY_BASIS_CHANGED' for c in changes)
    assert all(c['delta'] is None for c in changes)


def test_quantity_zero_decline_and_precision_are_preserved(original):
    r = deepcopy(original); point(r, 0)['value'] = '0'; point(r)['value'] = '1.2345'
    c = entry(r)['changes'][0]
    assert c['delta'] == '1.2345' and c['percent_change'] is None
    assert c['reason_code'] == 'INDUSTRY_ZERO_BASE'
    assert vehicle_flows(original)['entries'][2]['changes'][0]['delta'] == '-10'
    point(r)['unit'] = 'vehicle'
    assert entry(r)['points'][1]['reason_code'] == 'VEHICLE_FRACTIONAL_INDIVIDUAL_COUNT'


def test_missing_month_and_missing_point_do_not_get_bridged(original):
    r = deepcopy(original); r['series'][0]['observations'].pop(1)
    assert entry(r)['changes'][0]['reason_code'] == 'VEHICLE_NONADJACENT_MONTHS'
    r = deepcopy(original); r['series'][0]['observations'][1] = {'status':'unavailable','reason_code':'OBJECT_MISSING'}
    assert all(c['reason_code'] == 'VEHICLE_POINT_UNAVAILABLE' for c in entry(r)['changes'])
    assert entry(r)['points'][2]['value'] == '121'


def test_publication_clock_not_backwards_or_invented_intraday(original):
    r = deepcopy(original); point(r)['source']['published_at'] = '2026-08-01'
    assert entry(r)['changes'][0]['reason_code'] == 'VEHICLE_RELEASE_REGRESSED'
    point(r, 0)['source']['published_at'] = '2026-08-01T10:00:00+08:00'
    point(r)['source']['published_at'] = '2026-08-01T09:00:00+08:00'
    assert entry(r)['changes'][0]['reason_code'] == 'VEHICLE_RELEASE_REGRESSED'


def test_revision_is_not_a_new_month_and_stage_change_remains_unavailable(original):
    r = deepcopy(original); point(r).update(period_start='2026-07-01',period_end='2026-07-31')
    c = entry(r)['changes'][0]
    assert c['status'] == 'same-period-revision' and c['delta'] == '10'
    assert c['meaning'].startswith('same-period-vintage')
    point(r)['value'] = '100'
    assert entry(r)['changes'][0]['status'] == 'unchanged'
    point(r)['assessment'] = 'monthly-release'; point(r, 0)['assessment'] = 'preliminary-scan'
    assert entry(r)['changes'][0]['reason_code'] == 'INDUSTRY_BASIS_CHANGED'


def test_provider_changed_or_year_reset_never_fabricates_ytd_month(original):
    r = deepcopy(original); point(r)['source']['origin'] = 'synthetic:other-provider'
    assert entry(r)['changes'][0]['reason_code'] == 'INDUSTRY_BASIS_CHANGED'
    r = deepcopy(original); s = r['series'][4]
    for p,end in zip(s['observations'][:2],('2025-12-31','2026-01-31')):
        p.update(period_start=end[:4]+'-01-01',period_end=end)
    assert vehicle_flows(r)['entries'][4]['changes'][0]['delta'] is None


def test_duplicate_channel_and_mixed_family_not_silently_selected(original):
    r = deepcopy(original); r['series'].append(deepcopy(r['series'][0]))
    with pytest.raises(RecordError, match='DUPLICATE'): vehicle_flows(r)
    r = deepcopy(original); r['series'][0]['subject']['attributes'] = []
    assert vehicle_flows(r)['unclassified_series'][0]['series_id'] == r['series'][0]['series_id']
    assert vehicle_flows(r)['status'] == 'partial'
    r = deepcopy(original); point(r)['metric'] = 'vehicle-wholesale'
    assert vehicle_flows(r)['unclassified_series']


def test_missing_channel_and_period_kind_are_reported_separately(original):
    r = deepcopy(original); r['series'].pop(0)
    assert vehicle_flows(r)['coverage'][0]['monthly_channels_not_supplied'] == ['vehicle-production']
    assert vehicle_flows(r)['coverage'][0]['cumulative_channels_not_supplied'] == []


def test_company_forward_reverse_same_version_and_no_market_share(original):
    all0 = vehicle_flows(original)
    forward = vehicle_flows(original, subject_id=SUBJECT.entity_id)
    reverse = vehicle_flows(original, company_id=COMPANY.entity_id)
    missing = vehicle_flows(original, company_id='not-covered')
    assert all0['entries'] == reverse['entries'] == forward['entries']
    assert all0['relations'] == reverse['relations'] == forward['relations']
    assert missing['entries'] == missing['relations'] == []
    for out in (forward, reverse, missing):
        assert out['scope'] == all0['scope'] and out['view_version'] == all0['view_version']
    relation = reverse['relations'][0]
    assert relation['applicability_at_cutoff'] == 'historical' and relation['current_business_freshness'] == 'not-revalidated'
    assert all(relation[k] is None for k in ('business_share','elasticity','pass_through','lag','net_benefit'))
    with pytest.raises(RecordError): vehicle_flows(original,subject_id='x',company_id='y')
    with pytest.raises(RecordError): vehicle_flows(original,subject_id='../bad')


def test_all_unreadable_never_means_no_coverage_or_zero(original):
    r = deepcopy(original)
    for s in r['series']:
        s['observations'] = [{'status':'unavailable','reason_code':'OBJECT_MISSING'}] * 3
    out = vehicle_flows(r)
    assert out['status'] == 'partial' and out['scope']['series'] == 8
    assert out['scope']['qualified_series'] == 0 and out['relations'] == []
    assert out['unclassified_relations'] == ['synthetic-auto-link']


def test_determinism_and_input_mutation_protection(original):
    before = encoded(original); expected = vehicle_flows(original)
    with localcontext() as ctx:
        ctx.prec = 1; ctx.rounding = ROUND_DOWN; ctx.traps[Inexact] = True
        assert vehicle_flows(original) == expected
    assert encoded(original) == before
    expected['retained']['series'].clear()
    assert original['series']
    r = deepcopy(original); r['query']['subject_id'] = SUBJECT.entity_id
    with pytest.raises(RecordError, match='WHOLE_INPUT'): vehicle_flows(r)


def test_exact_original_store_reopening_lost_source_and_no_latest(saved, monkeypatch):
    store, root, refs, issuer = saved
    reader = VersionedStore(GitStore(store.backend.root)); head = reader.backend.head()
    expected = read_vehicle_flows(root, reader)
    resolve = reader.backend.resolve
    def lose(blob):
        if blob.path == refs[1].manifest.path: raise MissingObject('OBJECT_MISSING')
        return resolve(blob)
    monkeypatch.setattr(reader.backend,'resolve',lose)
    out = read_vehicle_flows(root, reader)
    assert out['scope']['unavailable_vehicle_points'] == 8 and out['status'] == 'partial'
    assert out['entries'][0]['points'][2] == expected['entries'][0]['points'][2]
    assert out['relations'] == expected['relations']
    assert reader.backend.head() == head


def test_real_git_model_scopes_are_readonly_and_revocation_at_exit_fails(tmp_path,monkeypatch):
    from decision_kernel.application import vehicle_flows as module
    api = GitAPI(tmp_path/'remote.git'); writer = VersionedStore(GitHubStore(api.binding,api))
    root, _, _ = retain(writer); posts = api.writes; api.requests.clear()
    reader = VersionedStore(GitHubStore(replace(api.binding,write_enabled=False),api))
    out = read_vehicle_flows(root,reader,company_id=COMPANY.entity_id)
    assert out['status'] == 'read' and len(api.requests) < 160 and api.writes == posts
    assert all(req[0] == 'GET' for req in api.requests)
    interpret = module.vehicle_flows
    def revoke(value,**kw):
        result = interpret(value,**kw); api.meta['private'] = False; return result
    monkeypatch.setattr(module,'vehicle_flows',revoke)
    with pytest.raises(RecordError,match='BINDING_MISMATCH'): read_vehicle_flows(root,reader)
    assert api.writes == posts


def test_original_cli_vehicle_switch_new_process_and_mutual_exclusion(saved,tmp_path):
    store, root, _, _ = saved
    ref_file = tmp_path/'ref.json'; ref_file.write_bytes(encoded(asdict(root)))
    env = {k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('TOKEN','SECRET','KEY'))}
    env['PYTHONPATH'] = str(ROOT/'src')
    cmd = [sys.executable,'-m','decision_kernel.interfaces.industry_cli','--path',str(store.backend.root),
           '--ref',str(ref_file),'--vehicle-flows']
    head = store.backend.head()
    out = subprocess.run([*cmd,'--company-id',COMPANY.entity_id],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=45)
    assert out.returncode == 0,out.stderr
    assert json.loads(out.stdout)['schema'] == 'industry-vehicle-flows-v1'
    bad = subprocess.run([*cmd,'--operating-cycle'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=45)
    assert bad.returncode == 2
    assert store.backend.head() == head
