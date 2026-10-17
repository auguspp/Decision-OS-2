"""Saved-brief joins over original Git custody; fixtures are not business data."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from decision_kernel.application import brief_industry
from decision_kernel.application.brief_delivery import build_edition, open_edition
from decision_kernel.application.industry_adoption import brief_adoption
from decision_kernel.application.reading_industry import open_industry
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_industry_reading import BUYER, prepare, packet
from test_news_delivery import END, index_ref, plan, retain

STAMP = '2026-10-16T10:00:00Z'


def item(root, series='synthetic-copper-price', view='industry'):
    return {'reference': asdict(root), 'series_id': series, 'view': view}


def build(store, window, items, *, limit=1, **kw):
    return build_edition(store.read_source, ResearchArtifacts(store),
                         plan(window, industry={'items': items, 'max_items': limit}, **kw))[0]


@pytest.fixture
def sample(tmp_path):
    store = VersionedStore(GitStore.init_synthetic(tmp_path / 'records.git'))
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP):
        _, payload = prepare(store)
        payload['series'].append({**deepcopy(payload['series'][0]), 'series_id': 'synthetic-second'})
        root = packet(store, payload)
    window = index_ref(store)
    doc = build(store, window, [item(root), item(root, 'synthetic-second')])
    ref = retain(store, doc, 'adoption-first')
    return store, root, doc, ref, window


def room_plan(root, ref, **query):
    return {'as_of': END, 'brief': asdict(ref),
            'industry': {'reference': asdict(root), 'view': 'industry', **query}}


def panels(sample, **query):
    store, root, doc, ref, _ = sample
    result = ReadingRoom(store).build(room_plan(root, ref, **query))
    return result['industry'], result['brief']


def test_actual_saved_capacity_and_adoption_are_distinct(sample):
    industry, brief = panels(sample)
    relation = industry['brief_adoption']
    assert [r['status'] for r in relation['rows']] == ['included', 'considered-not-included']
    assert [r['omission_reason'] for r in relation['rows']] == [None, 'brief-capacity']
    assert all(r['displayed_content']['status'] == 'same-displayed-series-and-roles' for r in relation['rows'])
    assert relation['brief_body_sha256'] == digest(brief['view']['brief_markdown'].encode())
    assert relation['human_acceptance'] == 'not-established'


def test_fresh_read_preserves_every_original_byte_and_head(sample):
    store, root, doc, ref, _ = sample
    head = store.backend.head()
    original_view = open_industry(store, room_plan(root, ref)['industry'])['view']
    first = ReadingRoom(store).build(room_plan(root, ref))
    fresh = VersionedStore(GitStore(store.backend.root))
    second = ReadingRoom(fresh).build(room_plan(root, ref))
    assert encoded(first) == encoded(second)
    assert encoded(first['industry']['view']) == encoded(original_view)
    assert encoded(first['brief']['view']) == encoded(doc)
    assert store.backend.head() == head


def test_pure_join_does_not_mutate_inputs_or_call_store(sample, monkeypatch):
    industry, brief = panels(sample)
    before = encoded([industry, brief])
    monkeypatch.setattr(sample[0], 'read_source', lambda *args: pytest.fail('join must not read sources'))
    assert brief_adoption(industry, brief) == industry['brief_adoption']
    assert encoded([industry, brief]) == before


def test_actual_successor_omits_same_selection_and_adopts_capacity_remainder(sample):
    store, root, first, previous, window = sample
    second = build(store, window, [item(root), item(root, 'synthetic-second')], baseline=asdict(previous))
    ref = retain(store, second, 'adoption-next')
    relation = ReadingRoom(store).build(room_plan(root, ref))['industry']['brief_adoption']
    assert relation['rows'][0]['omission_reason'] == 'previously-included-same-selection'
    assert relation['rows'][1]['status'] == 'included'
    assert relation['rows'][1]['classification'] == 'previously-available-not-included'
    assert relation['brief_body_sha256'] != digest(first['brief_markdown'].encode())


def test_reachable_commit_matches_existing_identity(sample):
    store, root, _, ref, _ = sample
    p = room_plan(root, ref)
    p['industry']['reference']['manifest']['commit'] = store.backend.head()
    rows = ReadingRoom(store).build(p)['industry']['brief_adoption']['rows']
    assert rows[0]['status'] == 'included'
    assert rows[0]['displayed_content']['status'] == 'same-displayed-series-and-roles'


@pytest.mark.parametrize('change', ['packet', 'view', 'series'])
def test_names_or_other_selection_do_not_confer_adoption(sample, change):
    industry, brief = panels(sample)
    # Synthetic malformed/display-mismatch models isolate identity rules; not new retained sources.
    if change == 'packet': industry['reference']['manifest']['sha256'] = '0' * 64
    elif change == 'view':
        industry['plan']['view'] = 'vehicle-flows'
        industry['view']['entries'] = industry['view']['series']
    else: industry['view']['series'][0]['series_id'] = 'same-name-different-series'
    result = brief_adoption(industry, brief)
    assert result['rows'][0]['status'] == 'not-considered-in-this-brief'


@pytest.mark.parametrize('status', ['missing', 'invalid', 'permission-required', 'sources-permission-required', 'not-included'])
def test_unavailable_brief_never_leaks_hidden_context_or_negative_adoption(sample, status):
    industry, brief = panels(sample)
    brief['status'] = status
    brief['view'] = {'private-marker': 'must-not-escape'}
    result = brief_adoption(industry, brief)
    assert result['status'] == 'unknown' and result['rows'] == []
    assert 'must-not-escape' not in encoded(result).decode() and 'brief_body_sha256' not in result


def test_missing_brief_is_explicit_but_unsaved_doc_is_unknown(sample):
    industry, brief = panels(sample)
    assert brief_adoption(industry, {'status': 'not-supplied', 'reference': None})['status'] == 'not-supplied'
    brief['reference'] = None
    assert brief_adoption(industry, brief)['reason'] == 'saved-brief-reference-required'


def test_legacy_brief_and_explicit_empty_scope_are_not_equivalent(sample):
    store, root, _, _, window = sample
    old, _ = build_edition(store.read_source, ResearchArtifacts(store), plan(window))
    legacy = retain(store, old, 'legacy')
    rows = ReadingRoom(store).build(room_plan(root, legacy))['industry']['brief_adoption']['rows']
    assert all(r['status'] == 'industry-not-tracked-in-this-brief' for r in rows)
    empty = retain(store, build(store, window, []), 'explicit-empty')
    rows = ReadingRoom(store).build(room_plan(root, empty))['industry']['brief_adoption']['rows']
    assert all(r['status'] == 'not-considered-in-this-brief' for r in rows)


@pytest.mark.parametrize('field', ['value', 'source-limit', 'role'])
def test_display_changes_are_separate_from_original_adoption(sample, field):
    industry, brief = panels(sample)
    if field == 'value': industry['view']['series'][0]['observations'][0]['value'] = '9999'
    if field == 'source-limit': industry['view']['series'][0]['observations'][0]['source_limitations'].append('Synthetic difference')
    if field == 'role': industry['view']['relations'][0]['net_benefit'] = 'synthetic-invalid-guarantee'
    row = brief_adoption(industry, brief)['rows'][0]
    assert row['status'] == 'included'
    assert row['displayed_content']['status'] == 'differs-from-delivery'
    assert row['displayed_content']['different_fields'] == (['displayed_roles'] if field == 'role' else ['series'])


def test_company_query_compares_only_visible_roles_and_keeps_original_counts(sample):
    industry, _ = panels(sample, company_id=BUYER.entity_id)
    row = industry['brief_adoption']['rows'][0]
    assert row['displayed_content']['status'] == 'same-displayed-series-and-roles'
    assert row['displayed_content']['saved_role_count'] == 2
    assert row['displayed_content']['compared_role_count'] == 1
    assert len(industry['view']['relations']) == 1
    assert industry['view']['scope']['accepted_relations'] == 2


def test_unknown_company_does_not_claim_no_industry_or_change_denominator(sample):
    industry, _ = panels(sample, company_id='no-such-company')
    assert industry['brief_adoption']['rows'] == []
    assert industry['view']['scope']['accepted_relations'] == 2


def test_saved_unavailable_series_keeps_actual_reason(sample):
    store, root, _, _, window = sample
    with patch.object(brief_industry, 'MAX_ROW_BYTES', 100):
        doc = build(store, window, [item(root)])
    # Restore under identical synthetic display capacity so exact replay remains required.
    ref = retain(store, doc, 'bounded-edition')
    with patch.object(brief_industry, 'MAX_ROW_BYTES', 100):
        relation = ReadingRoom(store).build(room_plan(root, ref))['industry']['brief_adoption']
    assert relation['rows'][0]['omission_reason'] == 'unreadable-or-outside-cutoff'
    assert relation['rows'][0]['reading_status_at_delivery'] == 'not-included'
    assert relation['rows'][0]['displayed_content']['status'] == 'unknown'


def test_sources_permission_denied_before_industry_access(sample, monkeypatch):
    store, root, _, ref, _ = sample
    monkeypatch.setattr(store, 'read_source', lambda *a: pytest.fail('no I/O before source consent'))
    with pytest.raises(RecordError, match='PUBLIC_INDUSTRY'):
        ReadingRoom(store).build(room_plan(root, ref), industry_export=False)


def test_missing_saved_artifact_does_not_hide_readable_industry(sample):
    store, root, _, ref, _ = sample
    p = room_plan(root, ref); p['brief']['sha256'] = '0' * 64
    result = ReadingRoom(store).build(p)
    assert result['industry']['status'] == 'readable'
    assert result['industry']['brief_adoption']['status'] == 'unknown'
    assert result['status'] == 'partial'


def test_join_budget_omits_whole_summary_without_changing_reader(sample, monkeypatch):
    from decision_kernel.application import industry_adoption
    industry, brief = panels(sample); original = encoded(industry['view'])
    monkeypatch.setattr(industry_adoption, 'MAX_ADOPTION_BYTES', 100)
    result = brief_adoption(industry, brief)
    assert result['status'] == 'not-included' and result['rows'] == []
    assert encoded(industry['view']) == original


def test_invalid_section_is_unknown_not_a_fabricated_capacity_reason(sample):
    industry, brief = panels(sample)
    brief['view']['industry']['omissions']['brief-capacity'] = 77
    result = brief_adoption(industry, brief)
    assert result['status'] == 'unknown' and result['rows'] == []


def test_actual_saved_feedback_is_masked_before_join(sample):
    store, root, first, _, window = sample
    key = first['included'][0]['entry_id']
    doc = build(store, window, [item(root)], attention=[{'entry_id': key, 'action': 'focus',
        'reason': 'Synthetic private review note', 'question': '', 'review_at': None}])
    ref = retain(store, doc, 'feedback-masking')
    result = ReadingRoom(store).build(room_plan(root, ref), feedback_export=False)
    assert result['brief']['status'] == 'permission-required'
    assert result['industry']['brief_adoption']['status'] == 'unknown'
    assert 'Synthetic private review note' not in encoded(result).decode()


def test_final_access_failure_still_refuses_entire_export(sample, monkeypatch):
    store, root, _, ref, _ = sample
    from decision_kernel.application import industry_adoption
    original = industry_adoption.brief_adoption
    def revoke(*args):
        result = original(*args)
        monkeypatch.setattr(store.backend, 'head', lambda: (_ for _ in ()).throw(RecordError('SYNTHETIC_REVOKED')))
        return result
    monkeypatch.setattr(industry_adoption, 'brief_adoption', revoke)
    with pytest.raises(RecordError, match='SYNTHETIC_REVOKED'):
        ReadingRoom(store).build(room_plan(root, ref))


def test_operating_projection_keeps_original_cumulative_rejection(sample):
    from test_industry_operations import retain as operating
    store, _, _, _, window = sample
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP):
        root, _, _, _ = operating(store)
    doc = build(store, window, [item(root, 'synthetic-industry-revenue', 'operating-cycle')])
    ref = retain(store, doc, 'operating-adoption')
    p = room_plan(root, ref); p['industry']['view'] = 'operating-cycle'
    result = ReadingRoom(store).build(p)
    row = next(r for r in result['industry']['brief_adoption']['rows'] if r['status'] == 'included')
    assert row['displayed_content']['status'] == 'same-displayed-series-and-roles'
    assert row['displayed_content']['saved_role_count'] == 0
    assert doc['industry']['rows'][0]['context']['series']['changes'][0]['reason_code'] == 'OPERATING_CUMULATIVE_NOT_MONTHLY'


def test_native_vehicle_projection_and_real_renderer(sample):
    # Current complete-tree fixture/renderer, executed by normal native CI.
    from test_vehicle_flows import retain as vehicles
    from decision_kernel.interfaces.reading_page import render_page
    store, _, _, _, window = sample
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP):
        root, _, _ = vehicles(store)
    value = plan(window, industry={'items': [item(root, 'vehicle-production-ytd-flow', 'vehicle-flows')]})
    value['cutoff'] = '2026-10-18T12:00:00Z'
    doc, _ = build_edition(store.read_source, ResearchArtifacts(store), value)
    ref = retain(store, doc, 'vehicle-adoption')
    p = room_plan(root, ref); p['industry']['view'] = 'vehicle-flows'
    result = ReadingRoom(store).build(p)
    rows = result['industry']['brief_adoption']['rows']
    selected = next(r for r in rows if r['status'] == 'included')
    assert selected['displayed_content']['status'] == 'same-displayed-series-and-roles'
    assert all(p['period_kind'] == 'ytd-flow' for p in doc['industry']['rows'][0]['context']['series']['points'])
    html = render_page(result)
    assert 'industry-adoption' in html and '不代表研究采用' in html


def exercise_installed_industry_adoption(outside, python, env, store):
    """Extend the existing industry Brief's ONE installed consumer, no new write."""
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.interfaces.reading_page import render_page
from decision_kernel.domain.records import encoded
s=VersionedStore(GitStore(Path(sys.argv[1])));head=s.backend.head()
item=json.loads(Path('industry-brief-input.json').read_bytes())
ref=json.loads(Path('industry-brief-ref.json').read_bytes())
original=json.loads(Path('industry-brief-original.json').read_bytes())
plan={'as_of':original['plan']['cutoff'],'brief':ref,'industry':{'reference':item['reference'],'view':item['view']}}
r=ReadingRoom(s).build(plan)
row=next(x for x in r['industry']['brief_adoption']['rows'] if x['series_id']==item['series_id'])
assert row['status']=='included' and row['displayed_content']['status']=='same-displayed-series-and-roles'
assert r['brief']['view']==original and s.backend.head()==head
assert r['readings'], 'original installed research remains'
raw=encoded(r);html=render_page(r).encode('utf-8')
if sys.argv[2]=='first':
 Path('industry-adoption-original.json').write_bytes(raw)
 Path('industry-adoption-original.html').write_bytes(html)
else:
 assert raw==Path('industry-adoption-original.json').read_bytes()
 assert html==Path('industry-adoption-original.html').read_bytes()
print('installed-industry-adoption-passed')
'''
    for phase in ('first', 'fresh'):
        result = subprocess.run([str(python), '-I', '-c', script, str(store), phase],
                                cwd=outside, env=env, check=True, capture_output=True, text=True)
        assert result.stdout.strip() == 'installed-industry-adoption-passed'
