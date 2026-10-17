"""Industry Brief adoption over original Git/reader/artifact contracts, synthetic data."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from decision_kernel.application import brief_delivery as brief, brief_industry as industry, news_delivery
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, SourceRef, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_industry_reading import prepare, packet
from test_industry_operations import retain as operating_packet
from test_news_delivery import END, index_ref, plan as news_plan, retain
from test_news_rolling import store

STAMP = '2026-10-16T10:00:00Z'


def item(ref, series='synthetic-copper-price', view='industry'):
    return {'reference': asdict(ref), 'view': view, 'series_id': series}


@pytest.fixture
def sample(store):
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP):
        ref, payload = prepare(store)
    return store, ref, payload, index_ref(store)


def build(store, window, items, **changes):
    value = news_plan(window, industry={'items': items, 'max_items': 3}, **changes)
    return brief.build_edition(store.read_source, ResearchArtifacts(store), value)[0]


def test_series_has_original_values_roles_unknowns_and_exact_body(sample):
    store, ref, _, window = sample
    doc = build(store, window, [item(ref)])
    row = doc['industry']['rows'][0]
    assert row['status'] == 'readable', row
    c = row['context']
    assert doc['schema'] == brief.INDUSTRY_SCHEMA
    assert [p['value'] for p in c['series']['points']] == ['100', '110']
    assert c['series']['changes'][0]['delta'] == '10'
    assert len(c['relations']) == c['packet_scope']['accepted_relations'] == 2
    assert all(r['net_benefit'] is None and r['applicability_at_cutoff'] == 'historical' for r in c['relations'])
    assert c['stored_at'] == STAMP and not c['root_original_verified']
    assert all('entities' not in p['source'] for p in c['series']['points'])
    assert '100' in doc['brief_markdown'] and 'historical' in doc['brief_markdown']
    assert doc['human_acceptance'] == 'not-established-by-artifact'
    assert doc['industry']['human_acceptance'] == 'not-established'
    assert doc['industry']['included'][0]['classification'] == 'baseline-not-supplied'


def test_single_saved_edition_independent_reopen_is_byte_exact(sample):
    store, ref, _, window = sample
    doc = build(store, window, [item(ref)]); saved = retain(store, doc, 'industry-first')
    head = store.backend.head()
    fresh = VersionedStore(GitStore(store.backend.root))
    actual, _ = brief.open_edition(fresh.read_source, ResearchArtifacts(fresh), saved)
    assert encoded(actual) == encoded(doc) and actual['brief_markdown'] == doc['brief_markdown']
    assert store.backend.head() == head
    next_doc = build(fresh, window, [item(ref)], baseline=asdict(saved))
    assert next_doc['industry']['included'] == []
    assert next_doc['industry']['omissions']['previously-included-same-selection'] == 1
    assert next_doc['comparison']['baseline_body_sha256'] == digest(doc['brief_markdown'].encode())


def test_same_packet_other_series_capacity_and_later_delivery_are_distinct(sample):
    store, ref, payload, window = sample
    # Explicit synthetic second series, not a second independent evidence source.
    payload['series'].append({**deepcopy(payload['series'][0]), 'series_id': 'synthetic-second-selection'})
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP): ref = packet(store, payload)
    p = news_plan(window, industry={'items': [item(ref), item(ref, 'synthetic-second-selection')], 'max_items': 1})
    first, _ = brief.build_edition(store.read_source, ResearchArtifacts(store), p)
    assert first['industry']['omissions']['brief-capacity'] == 1
    saved = retain(store, first, 'capacity-first'); p['baseline'] = asdict(saved)
    second, _ = brief.build_edition(store.read_source, ResearchArtifacts(store), p)
    assert len(second['industry']['included']) == 1
    assert second['industry']['included'][0]['classification'] == 'previously-available-not-included'
    assert second['industry']['included'][0]['version_id'] != first['industry']['included'][0]['version_id']
    assert second['industry']['omissions']['previously-included-same-selection'] == 1


@pytest.mark.parametrize('stamp,classification', [(STAMP,'late-to-delivery'), ('2026-10-17T10:00:00Z','newly-stored-packet-selection')])
def test_lateness_is_packet_storage_not_period_or_event(sample, stamp, classification):
    store, ref, payload, window = sample
    old = brief.build_edition(store.read_source, ResearchArtifacts(store), news_plan(window))[0]
    previous = retain(store, old, 'legacy-news')
    payload['coverage_note'] = 'Synthetic changed packet; old observation dates remain.'
    with patch('decision_kernel.application.versioned_store._now', lambda: stamp): ref = packet(store, payload)
    p = news_plan(window, baseline=asdict(previous), industry={'items': [item(ref)]})
    p['cutoff'] = '2026-10-19T12:00:00Z'
    doc, _ = brief.build_edition(store.read_source, ResearchArtifacts(store), p)
    assert doc['industry']['included'][0]['classification'] == classification
    assert doc['industry']['baseline']['industry_tracked'] is False
    assert doc['industry']['rows'][0]['context']['as_of'] == payload['as_of']


def test_reachable_commit_not_new_but_different_series_is_new_selection(sample):
    store, ref, _, window = sample
    doc = build(store, window, [item(ref)]); saved = retain(store, doc, 'reachability')
    later = deepcopy(item(ref)); later['reference']['manifest']['commit'] = store.backend.head()
    assert industry.version_id(later) == industry.version_id(item(ref))
    doc2 = build(store, window, [later], baseline=asdict(saved))
    assert not doc2['industry']['included']
    with pytest.raises(RecordError, match='DUPLICATE_SELECTION'):
        industry.plan_input({'items': [item(ref), later]})


def test_tracking_cannot_disappear_but_empty_plan_records_scope_exit(sample):
    store, ref, _, window = sample
    doc = build(store, window, [item(ref)]); saved = retain(store, doc, 'scope')
    with pytest.raises(RecordError, match='INDUSTRY_TRACKING_CANNOT_DISAPPEAR'):
        brief.build_edition(store.read_source, ResearchArtifacts(store), news_plan(window, baseline=asdict(saved)))
    empty = build(store, window, [], baseline=asdict(saved))
    assert empty['industry']['no_longer_selected'] == [industry.version_id(item(ref))]
    assert not empty['industry']['included'] and '不代表没有变化' in empty['brief_markdown']


@pytest.mark.parametrize('mutation', ['value','role','limit','body','scope'])
def test_resealed_false_adopted_content_rejected(sample, mutation):
    store, ref, _, window = sample
    doc = build(store, window, [item(ref)])
    c = doc['industry']['rows'][0]['context']
    if mutation == 'value': c['series']['points'][0]['value'] = '9999'
    if mutation == 'role': c['relations'][0]['net_benefit'] = 'guaranteed'
    if mutation == 'limit': c['root_original_verified'] = True
    if mutation == 'scope': c['packet_scope']['series'] = 9999
    doc['brief_markdown'] = brief.brief_text(doc)
    if mutation == 'body': doc['brief_markdown'] += '\nFAKE'
    news_delivery._seal(doc)
    saved = retain(store, doc, 'forged-' + mutation)
    with pytest.raises(RecordError): brief.open_edition(store.read_source, ResearchArtifacts(store), saved)
    with pytest.raises(RecordError): build(store, window, [item(ref)], baseline=asdict(saved))


@pytest.mark.parametrize('key,value', [('series_id','no-such-series'),('view','bad-view'),('extra','execute'),('reference',{})])
def test_invalid_selection_is_not_fabricated(sample, key, value):
    store, ref, _, window = sample
    bad = {**item(ref),key:value}
    if key == 'series_id':
        doc = build(store, window, [bad])
        assert doc['industry']['rows'][0]['status'] == 'unavailable'
        assert not doc['industry']['included']
    else:
        with pytest.raises(RecordError): industry.plan_input({'items': [bad]})


@pytest.mark.parametrize('limit', [True,0,9,'3'])
def test_invalid_limit_refused(sample, limit):
    with pytest.raises(RecordError): industry.plan_input({'items':[item(sample[1])],'max_items':limit})


def test_future_packet_missing_root_and_bounded_context_do_not_hide_others(sample, monkeypatch):
    store, ref, payload, window = sample
    payload['coverage_note'] = 'Future synthetic'
    with patch('decision_kernel.application.versioned_store._now', lambda: '2026-10-17T12:00:00Z'): future = packet(store, payload)
    bad = deepcopy(item(ref)); bad['reference']['manifest']['sha256'] = '0'*64
    doc = build(store, window, [item(ref),item(future),bad])
    assert [r['status'] for r in doc['industry']['rows']] == ['readable','after-cutoff','unavailable']
    assert len(doc['industry']['included']) == 1
    monkeypatch.setattr(industry,'MAX_ROW_BYTES',100)
    bounded = build(store, window, [item(ref)])
    assert bounded['industry']['rows'][0]['status'] == 'not-included'
    assert bounded['industry']['rows'][0]['item'] == industry.item_input(item(ref))
    assert not bounded['industry']['included']


def test_partial_point_keeps_original_gap_comparison_and_good_point(sample, monkeypatch):
    store, ref, payload, window = sample
    source = payload['series'][0]['observations'][0]['manifest']
    original = store.read_source
    def missing(r):
        if r.manifest.path == source['path'] and r.selection.start == 1:
            raise MissingObject('synthetic absent selected row')
        return original(r)
    monkeypatch.setattr(store,'read_source',missing)
    doc = build(store, window, [item(ref)])
    c = doc['industry']['rows'][0]['context']
    assert c['packet_status'] == 'partial'
    assert [p['status'] for p in c['series']['points']] == ['unavailable','read']
    assert c['series']['changes'][0]['delta'] is None
    assert c['series']['changes'][0]['reason_code'] == 'INDUSTRY_POINT_UNAVAILABLE'
    assert len(doc['industry']['included']) == 1


def test_same_packet_read_once_per_build_and_exit_access_failure_rejects(sample, monkeypatch):
    store, ref, payload, window = sample
    payload['series'].append({**deepcopy(payload['series'][0]),'series_id':'synthetic-secondary'})
    with patch('decision_kernel.application.versioned_store._now',lambda:STAMP): ref = packet(store,payload)
    call = industry._packet; count=[]
    def tracked(*args): count.append(1);return call(*args)
    monkeypatch.setattr(industry,'_packet',tracked)
    build(store,window,[item(ref),item(ref,'synthetic-secondary')]);assert len(count)==1
    def denied(): raise RecordError('synthetic final access denied')
    monkeypatch.setattr(store.backend,'head',denied)
    with pytest.raises(RecordError,match='final access denied'): build(store,window,[item(ref)])


def test_operating_cumulative_comparison_stays_rejected_without_inferred_cashflow(store):
    with patch('decision_kernel.application.versioned_store._now',lambda:STAMP): root,_,_,_ = operating_packet(store)
    doc=build(store,index_ref(store),[item(root,'synthetic-industry-revenue','operating-cycle')])
    c=doc['industry']['rows'][0]['context']
    assert all(p['period_kind']=='ytd-flow' for p in c['series']['points'])
    assert c['series']['changes'][0]['reason_code']=='OPERATING_CUMULATIVE_NOT_MONTHLY'
    assert c['relations']==[] and 'snapshots' not in c['series']


def test_vehicle_stage_and_monthly_cumulative_meanings_use_original_reader(store):
    # Use the existing package's own synthetic vehicle fixture, not mock results.
    from test_vehicle_flows import retain as vehicle_packet
    with patch('decision_kernel.application.versioned_store._now',lambda:STAMP): root,_,_=vehicle_packet(store)
    p=news_plan(index_ref(store),industry={'items':[item(root,'vehicle-production-ytd-flow','vehicle-flows')]})
    p['cutoff']='2026-10-18T12:00:00Z'
    doc,_=brief.build_edition(store.read_source,ResearchArtifacts(store),p)
    c=doc['industry']['rows'][0]['context']
    assert all(x['delta'] is None for x in c['series']['changes'])
    assert all(p['period_kind']=='ytd-flow' for p in c['series']['points'])
    assert len(c['relations'])==1 and c['relations'][0]['net_benefit'] is None


def test_room_saved_industry_brief_needs_source_permission_even_without_industry_panel(sample):
    from decision_kernel.application.reading_room import ReadingRoom
    store,ref,_,window=sample;doc=build(store,window,[item(ref)]);saved=retain(store,doc,'room-brief')
    plan={'as_of':END,'brief':asdict(saved)}
    denied=ReadingRoom(store).build(plan,industry_export=False)
    assert denied['brief']['status']=='sources-permission-required'
    assert 'view' not in denied['brief'] and denied['status']=='partial'
    allowed=ReadingRoom(store).build(plan,industry_export=True)
    assert allowed['brief']['view']==doc


def exercise_installed_industry_brief(outside, python, env, store):
    """Called after the existing mixed Brief helper within the ONE wheel consumer."""
    from test_industry_reading import prepare
    local=VersionedStore(GitStore(store))
    with patch('decision_kernel.application.versioned_store._now',lambda:STAMP): root,_=prepare(local)
    (outside/'industry-brief-input.json').write_bytes(encoded(item(root)))
    script=r'''
import json,sys
from dataclasses import asdict
from pathlib import Path
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.brief_delivery import build_edition,open_edition,page_model
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.records import encoded
from decision_kernel.interfaces.news_page import render_page
s=VersionedStore(GitStore(Path(sys.argv[1])));a=ResearchArtifacts(s);head=s.backend.head()
if sys.argv[2]=='first':
 paths=[p for p in s.backend.paths(head,'research/artifacts/mixed-edition/') if p.endswith('/manifest.json')]
 assert len(paths)==1
 prior=asdict(s.backend.reference(head,paths[0]));old,_=open_edition(s.read_source,a,prior)
 plan={**old['plan'],'baseline':prior,'industry':{'items':[json.loads(Path('industry-brief-input.json').read_bytes())]}}
 doc,rows=build_edition(s.read_source,a,plan)
 assert len(doc['industry']['included'])==1 and doc['research']['included']==[]
 ref=a.put('industry-installed-brief',ArtifactInput.from_bytes('industry-installed-brief','edition.json','application/json',encoded(doc)))
 Path('industry-brief-ref.json').write_bytes(encoded(asdict(ref)))
 Path('industry-brief-original.json').write_bytes(encoded(doc))
 Path('industry-brief-installed.html').write_text(render_page(page_model(doc,rows,ref)),encoding='utf-8')
else:
 ref=json.loads(Path('industry-brief-ref.json').read_bytes());doc,_=open_edition(s.read_source,a,ref)
 assert encoded(doc)==Path('industry-brief-original.json').read_bytes()
 nxt,_=build_edition(s.read_source,a,{**doc['plan'],'baseline':ref})
 assert nxt['industry']['included']==[] and nxt['industry']['omissions']['previously-included-same-selection']==1
 assert s.backend.head()==head
print('industry-installed-brief-passed')
'''
    for mode in ('first','second'):
        result=subprocess.run([str(python),'-I','-c',script,str(store),mode],cwd=outside,env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=='industry-installed-brief-passed'
