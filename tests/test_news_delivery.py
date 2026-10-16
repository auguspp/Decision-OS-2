"""News desk/Brief semantics. Synthetic observations, original Git/SourceRef/Artifact owners."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from decision_kernel.application import news_delivery as desk
from decision_kernel.application.news_rolling_index import rolling_index
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.observations.news_windows import combine, newsnow_window
from test_news_rolling import store, save, window
from test_news_windows import newsnow

END = '2026-10-16T12:00:00Z'


def index_ref(store, name='idx', refs=None, as_of=END):
    if refs is None:
        refs = [save(store, 'batch-'+name, combine([
            newsnow_window(newsnow(title='合成公开观察 A', item_id='a'), 'cls', observed_at='2026-10-16T10:00:00Z'),
            newsnow_window(newsnow(source='jin10', title='合成公开观察 B', item_id='b'), 'jin10', observed_at='2026-10-16T09:00:00Z'),
            {'status': 'source-unavailable', 'source_id': 'thepaper'}]))]
    return save(store, name, rolling_index(store.read_source, refs, as_of=as_of))


def plan(ref, **kw):
    return dict(window=asdict(ref), cutoff=END, kind='evening', **kw)


def build(store, value):
    return desk.build_edition(store.read_source, ResearchArtifacts(store), value)


def retain(store, doc, name='edition'):
    return ResearchArtifacts(store).put(name, ArtifactInput.from_bytes(name, 'edition.json', 'application/json', encoded(doc)))


def choice(key, action='focus', **kw):
    return dict(entry_id=key, action=action, reason='合成选择理由', question='', review_at=None, **kw)


def test_first_brief_is_source_balanced_and_does_not_invent_baseline_or_acceptance(store):
    doc, logical = build(store, plan(index_ref(store)))
    assert len(doc['included']) == 2
    assert {x['source'] for x in doc['included']} == {'cls', 'jin10'}
    assert all(x['classification'] == 'baseline-not-supplied' for x in doc['included'])
    assert doc['attention_state'] == doc['quick_candidates'] == []
    assert doc['comparison']['new_to_window'] is None
    assert doc['human_acceptance'] == 'not-established-by-artifact'
    assert doc['window']['source_outcomes'] == logical['source_outcomes']
    assert '未提供' in doc['brief_markdown'] and '待解释' in doc['brief_markdown']


def test_prior_actual_body_suppresses_only_included_versions_not_unseen_rejection(store):
    ref = index_ref(store)
    first, _ = build(store, plan(ref, max_items=1))
    prior = retain(store, first)
    second, _ = build(store, plan(ref, baseline=asdict(prior)))
    assert len(second['included']) == 1
    assert second['included'][0]['entry_id'] != first['included'][0]['entry_id']
    assert second['included'][0]['classification'] == 'previously-available-not-included'
    assert second['comparison']['new_to_window'] == 0
    assert second['omissions']['previously-included-same-version'] == 1
    assert second['comparison']['baseline_body_sha256'] == desk.digest(first['brief_markdown'].encode())


def test_one_step_independent_artifact_read_reconstructs_actual_source_and_body(store, tmp_path):
    first, _ = build(store, plan(index_ref(store)))
    prior = retain(store, first)
    second, logical = build(store, plan(index_ref(store, 'second'), baseline=asdict(prior)))
    result = retain(store, second, 'second-edition')
    fresh = VersionedStore(GitStore(tmp_path/'news.git'))
    actual, restored = desk.open_edition(fresh.read_source, ResearchArtifacts(fresh), result)
    assert encoded(actual) == encoded(second) and restored == logical


def test_explicit_focus_question_becomes_candidate_not_executed_research(store):
    ref = index_ref(store); _, logical = build(store, plan(ref))
    key = desk.entry_id(logical['entries'][0]); c = choice(key); c['question'] = '这项变化是否影响企业现金流？'
    doc, _ = build(store, plan(ref, attention=[c]))
    candidate = doc['quick_candidates'][0]
    assert candidate['question'] == c['question'] and candidate['research_started'] is False
    assert candidate['status'] == 'candidate-not-executed' and candidate['requires_research_scope_check']
    assert candidate['source_reference'] == desk.reference(logical['entries'][0]['last_reference'])


def test_exact_defer_carries_until_due_and_new_title_version_is_not_suppressed(store):
    old_batch = save(store, 'old', window('2026-10-16T10:00:00Z'))
    old_ref = index_ref(store, refs=[old_batch]); _, logical = build(store, plan(old_ref))
    key = desk.entry_id(logical['entries'][0]); c = choice(key, 'defer'); c['review_at'] = '2026-10-16T14:00:00Z'
    first, _ = build(store, plan(old_ref, attention=[c])); prior = retain(store, first)
    changed = save(store, 'changed', window('2026-10-16T11:00:00Z', title='合成修订标题'))
    new_ref = index_ref(store, 'new', [old_batch, changed])
    second, _ = build(store, plan(new_ref, baseline=asdict(prior)))
    assert len(second['included']) == 1 and second['included'][0]['title'] == '合成修订标题'
    assert second['omissions']['explicitly-deferred'] == 1
    later = plan(new_ref, baseline=asdict(prior)); later['cutoff'] = '2026-10-16T15:00:00Z'
    due, _ = build(store, later)
    assert any(i['selection_reason'] == '暂缓到期待复核' for i in due['included'])
    assert due['attention_state'][0]['action'] == 'defer'  # due != user changed their choice


def test_reset_is_explicit_and_does_not_mutate_prior_artifact(store):
    ref = index_ref(store); _, logical = build(store, plan(ref)); key = desk.entry_id(logical['entries'][0])
    first, _ = build(store, plan(ref, attention=[choice(key, 'dismiss')]))
    before = encoded(first); prior = retain(store, first)
    second, _ = build(store, plan(ref, baseline=asdict(prior), attention=[choice(key, 'reset')]))
    assert not second['attention_state'] and encoded(first) == before
    assert any(i['entry_id'] == key for i in second['included'])


def test_late_observation_compares_actual_baseline_not_scheduled_time(store):
    a = save(store, 'early', window('2026-10-16T09:00:00Z', item='a'))
    ref = index_ref(store, refs=[a], as_of='2026-10-16T10:00:00Z')
    p = plan(ref); p['cutoff'] = '2026-10-16T10:30:00Z'
    first, _ = build(store, p); baseline = retain(store, first)
    b = save(store, 'late', window('2026-10-16T10:00:00Z', item='b'))
    ref2 = index_ref(store, 'later', [a,b])
    later, _ = build(store, plan(ref2, baseline=asdict(baseline)))
    assert later['comparison']['late_to_delivery'] == 1
    assert later['included'][0]['classification'] == 'late-to-delivery'


def test_zero_eligible_brief_retains_failures_and_is_not_quiet_market(store):
    ref = index_ref(store); first, _ = build(store, plan(ref)); baseline = retain(store, first)
    doc, logical = build(store, plan(ref, baseline=asdict(baseline)))
    assert doc['included'] == [] and doc['window']['coverage']['failed_source_windows'] == 1
    assert '不代表市场没有新闻' in doc['brief_markdown']
    assert len(desk.page_model(doc, logical)['articles']) == 2


@pytest.mark.parametrize('changes', [dict(kind='other'),dict(max_items=0),dict(max_items=True),dict(max_items=25),
    dict(cutoff='2026-10-16T12:00:00'),dict(extra='ignored'),dict(attention='not-array')])
def test_bad_plan_rejected_before_io(changes):
    def no_io(_): pytest.fail('no IO')
    valid = {'window': {'manifest': {'repository':'example/store', 'commit':'a'*40, 'path':'source-views/x/'+'b'*64+'/manifest.json', 'sha256':'c'*64}, 'selection': {'kind':'all','start':None,'end':None,'columns':[]}}, 'cutoff':END, 'kind':'evening'}
    with pytest.raises(RecordError): desk.build_edition(no_io, None, {**valid, **changes})


def test_selection_id_cannot_target_unavailable_or_other_version(store):
    with pytest.raises(RecordError, match='CHOICE_NOT_IN_WINDOW'):
        build(store, plan(index_ref(store), attention=[choice('0'*64)]))


def test_selection_cannot_claim_human_authority_via_extra_actor_field(store):
    ref = index_ref(store); _, logical = build(store, plan(ref))
    c = choice(desk.entry_id(logical['entries'][0])); c['actor'] = 'human'
    with pytest.raises(RecordError, match='CHOICE_SHAPE'): build(store, plan(ref, attention=[c]))


@pytest.mark.parametrize('mutation', ['body','title','reference','hash','schema'])
def test_artifact_corruption_or_forged_source_text_refused(store, mutation):
    doc, _ = build(store, plan(index_ref(store)))
    if mutation == 'body': doc['brief_markdown'] += '\n捏造结论'
    if mutation == 'title': doc['included'][0]['title'] = '伪造来源文字'; doc['brief_markdown'] = desk.brief_text(doc)
    if mutation == 'reference': doc['plan']['window']['manifest']['sha256'] = '0'*64
    if mutation == 'schema': doc['schema'] = 'other'
    desk._seal(doc)
    if mutation == 'hash': doc['edition_sha256'] = '0'*64
    ref = retain(store, doc)
    with pytest.raises(RecordError): desk.open_edition(store.read_source, ResearchArtifacts(store), ref)


def test_missing_baseline_fails_instead_of_silent_new_edition(store):
    doc, _ = build(store, plan(index_ref(store))); ref = retain(store, doc)
    broken = asdict(ref); broken['sha256'] = '0'*64
    with pytest.raises(RecordError): build(store, plan(index_ref(store,'next'), baseline=broken))


def test_window_after_delivery_cutoff_and_future_baseline_refused(store):
    ref=index_ref(store); p=plan(ref);p['cutoff']='2026-10-16T11:00:00Z'
    with pytest.raises(RecordError,match='WINDOW_AFTER_CUTOFF'):build(store,p)
    p['cutoff']='2026-10-16T14:00:00Z';doc,_=build(store,p);prior=retain(store,doc)
    with pytest.raises(RecordError,match='BASELINE_FROM_FUTURE'):build(store,plan(ref,baseline=asdict(prior)))


@pytest.mark.parametrize('url',[None,'javascript:alert(1)','data:text/html,x','https://user:pw@example.com','https://x\n.example','//example.com'])
def test_page_rejects_non_navigation_links(url):
    assert desk.safe_link(url) is None


def test_page_embeds_text_not_markup_or_script(store):
    from decision_kernel.interfaces.news_page import render_page, JS
    ref=index_ref(store);doc,logical=build(store,plan(ref));model=desk.page_model(doc,logical)
    attack='</script><script>globalThis.pwned=1</script><img src=x onerror=alert(1)>'
    model['articles'][0]['title']=attack
    html=render_page(model)
    assert attack not in html and '\\u003c/script\\u003e' in html
    assert "connect-src &#x27;none&#x27;" in html and 'innerHTML' not in JS and 'localStorage' not in JS
    assert 'default-src' in html and 'sha256-' in html and "'unsafe-inline'" not in html


@pytest.mark.parametrize('field', ['omissions', 'comparison', 'quick_candidates'])
def test_previous_edition_cannot_smuggle_changed_counts_or_candidates(store, field):
    ref = index_ref(store); first, _ = build(store, plan(ref))
    if field == 'omissions':
        first[field]['brief-capacity'] += 1
    elif field == 'comparison':
        first[field]['new_to_window'] = 77
    else:
        first[field] = [{'research_started': True}]
    first['brief_markdown'] = desk.brief_text(first)
    desk._seal(first)
    prior = retain(store, first)
    with pytest.raises(RecordError):
        build(store, plan(ref, baseline=asdict(prior)))
