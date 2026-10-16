"""Research adoption into the original Brief over real temporary Git storage."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import brief_delivery as brief, brief_research as research
from decision_kernel.application import news_delivery as news, versioned_store
from decision_kernel.application.research_agenda import ResearchAgenda
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, RevisionInput, digest, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from test_news_delivery import store, index_ref, plan, retain, END


def revision(store, monkeypatch, case='example', rev='r1', at='2026-10-16T10:00:00Z', predecessor=None, body='原作者的经营判断。\n仍须核验经营现金流。\n不是已完成Full。\n'):
    monkeypatch.setattr(versioned_store, '_now', lambda: at)
    ref = store.submit_revision('research-'+case+'-'+rev, RevisionInput(
        case_id=case, revision_id=rev, kind='full', body=body, mandate='合成原委托',
        method='synthetic:method', unresolved=('原证据缺口仍未解决',), predecessor=predecessor))
    return {'reference': asdict(ref), 'start': 1, 'end': 2}


def mixed_plan(window, items, **extra):
    return plan(window, research={'items': items}, **extra)


def build(store, value):
    return brief.build_edition(store.read_source, ResearchArtifacts(store), value)


def test_legacy_news_result_remains_byte_identical(store):
    p = plan(index_ref(store))
    original, logical = news.build_edition(store.read_source, ResearchArtifacts(store), p)
    actual, same = build(store, p)
    assert encoded(actual) == encoded(original) and same == logical
    ref = retain(store, original)
    assert brief.open_edition(store.read_source, ResearchArtifacts(store), ref)[0] == original


def test_first_adoption_contains_actual_author_lines_and_all_limits_then_reopens(store, monkeypatch, tmp_path):
    item = revision(store, monkeypatch)
    doc, logical = build(store, mixed_plan(index_ref(store), [item]))
    section = doc['research']
    assert doc['schema'] == brief.SCHEMA and len(section['included']) == 1
    row = section['rows'][0]
    assert row['excerpt']['text'] == '原作者的经营判断。\n仍须核验经营现金流。\n'
    assert row['context']['recorded_unresolved'] == ['原证据缺口仍未解决']
    assert section['included'][0]['classification'] == 'baseline-not-supplied'
    assert '原作者的经营判断' in doc['brief_markdown'] and '原证据缺口仍未解决' in doc['brief_markdown']
    assert '研究产物未接入' not in doc['brief_markdown']
    current = store.current('example')
    saved = retain(store, doc)
    fresh = VersionedStore(GitStore(tmp_path/'news.git'))
    restored, rebuilt = brief.open_edition(fresh.read_source, ResearchArtifacts(fresh), saved)
    assert encoded(restored) == encoded(doc) and rebuilt == logical and store.current('example') == current
    assert section['research_executed'] is False and doc['human_acceptance'] == 'not-established-by-artifact'


def test_actual_mixed_body_hash_and_same_version_omission(store, monkeypatch):
    item = revision(store, monkeypatch)
    p = mixed_plan(index_ref(store), [item])
    first, _ = build(store, p)
    prior = retain(store, first)
    second, _ = build(store, {**p, 'baseline': asdict(prior)})
    assert second['research']['included'] == []
    assert second['research']['omissions']['previously-included-same-version'] == 1
    assert second['comparison']['baseline_body_sha256'] == digest(first['brief_markdown'].encode())
    assert first['brief_markdown'] != news.brief_text(brief._news_projection(first))
    assert sum(second['research']['omissions'].values()) == 1


@pytest.mark.parametrize('legacy', [True, False])
def test_late_research_across_weekend_uses_actual_previous_cutoff(store, monkeypatch, legacy):
    window = index_ref(store)
    first, _ = build(store, plan(window) if legacy else mixed_plan(window, []))
    prior = retain(store, first)
    # Stored on Friday before that actual edition but not previously supplied.
    item = revision(store, monkeypatch)
    p = mixed_plan(window, [item], baseline=asdict(prior))
    p['cutoff'] = '2026-10-19T12:00:00Z'
    later, _ = build(store, p)
    assert later['research']['included'][0]['classification'] == 'late-to-delivery'
    assert later['research']['baseline']['research_tracked'] is not legacy
    assert later['research']['time_basis'] == 'stored-time-not-event-time-or-research-completion'


def test_newly_stored_revision_is_not_late_or_an_inherited_defer(store, monkeypatch):
    old = revision(store, monkeypatch)
    window = index_ref(store)
    agenda_doc = ResearchAgenda(store).build({'references':[old['reference']], 'as_of':END,
        'changes':[{'reference':old['reference'], 'question_id':None, 'action':'defer', 'reason':'合成暂缓'}]})['document']
    agenda = ResearchArtifacts(store).put('agenda', ArtifactInput.from_bytes('agenda', 'agenda.json', 'application/json', encoded(agenda_doc)))
    p = mixed_plan(window, [old]); p['research']['agenda'] = asdict(agenda)
    first, _ = build(store, p)
    assert first['research']['omissions']['explicitly-deferred'] == 1
    prior = retain(store, first)
    newer = revision(store, monkeypatch, rev='r2', at='2026-10-16T13:00:00Z', predecessor=store.current('example'))
    p.update(baseline=asdict(prior), cutoff='2026-10-16T14:00:00Z')
    p['research']['items'] = [newer]
    second, _ = build(store, p)
    assert second['research']['included'][0]['classification'] == 'newly-stored-research'
    assert second['research']['omissions']['explicitly-deferred'] == 0
    assert second['research']['rows'][0]['predecessor'] == old['reference']


def test_saved_focus_can_reinclude_but_is_not_human_authentication(store, monkeypatch):
    item = revision(store, monkeypatch)
    p = mixed_plan(index_ref(store), [item]); first, _ = build(store, p)
    prior = retain(store, first)
    agenda_doc = ResearchAgenda(store).build({'references':[item['reference']], 'as_of':END,
        'changes':[{'reference':item['reference'], 'question_id':None, 'action':'focus', 'reason':'合成明确复核'}]})['document']
    agenda = ResearchArtifacts(store).put('agenda', ArtifactInput.from_bytes('agenda', 'agenda.json', 'application/json', encoded(agenda_doc)))
    p['research']['agenda'] = asdict(agenda); p['baseline'] = asdict(prior)
    second, _ = build(store, p)
    assert second['research']['included'][0]['classification'] == 'repeat-explicitly-requested'
    assert second['research']['rows'][0]['context']['agenda'][0]['selection']['reason'] == '合成明确复核'
    assert second['human_acceptance'] == first['human_acceptance']


def test_brief_capacity_and_previously_available_are_not_new_research(store, monkeypatch):
    items = [revision(store, monkeypatch, case=c) for c in ('one','two')]
    p = mixed_plan(index_ref(store), items); p['research']['max_items'] = 1
    first, _ = build(store, p); prior = retain(store, first)
    second, _ = build(store, {**p, 'baseline':asdict(prior)})
    assert first['research']['omissions']['brief-capacity'] == 1
    assert second['research']['included'][0]['classification'] == 'previously-available-not-included'
    assert second['research']['included'][0]['version_id'] != first['research']['included'][0]['version_id']


def test_bad_excerpt_is_isolated_and_later_read_failure_is_not_zero_tasks(store, monkeypatch):
    bad = revision(store, monkeypatch, case='bad'); bad['end'] = 8
    good = revision(store, monkeypatch, case='good')
    p = mixed_plan(index_ref(store), [bad, good])
    doc, _ = build(store, p)
    assert doc['research']['rows'][0]['status'] == 'invalid' and len(doc['research']['included']) == 1
    assert 'BRIEF_RESEARCH_EXCERPT_UNAVAILABLE' in doc['brief_markdown']
    original = store.read_revision
    def failed(ref):
        if ref.path == good['reference']['path']:
            raise MissingObject('synthetic failure')
        return original(ref)
    monkeypatch.setattr(store, 'read_revision', failed)
    missing, _ = build(store, p)
    assert missing['research']['included'] == []
    assert '不等于无研究' in missing['brief_markdown']
    assert missing['research']['omissions']['unreadable-or-outside-cutoff'] == 2


def test_stored_after_cutoff_is_excluded_without_leaking_future_excerpt(store, monkeypatch):
    item = revision(store, monkeypatch, at='2026-10-16T13:00:00Z')
    doc, _ = build(store, mixed_plan(index_ref(store), [item]))
    assert doc['research']['rows'][0]['status'] == 'after-cutoff'
    assert 'context' not in doc['research']['rows'][0] and doc['research']['included'] == []


def test_no_silent_downgrade_from_research_tracking(store, monkeypatch):
    p = mixed_plan(index_ref(store), [revision(store, monkeypatch)])
    first, _ = build(store, p); prior = retain(store, first)
    del p['research']; p['baseline'] = asdict(prior)
    with pytest.raises(RecordError, match='TRACKING_CANNOT_DISAPPEAR'):
        build(store, p)


def test_tampered_excerpt_cannot_be_used_as_an_actual_baseline(store, monkeypatch):
    p = mixed_plan(index_ref(store), [revision(store, monkeypatch)])
    doc, _ = build(store, p)
    doc['research']['rows'][0]['excerpt']['text'] = '伪造片段'
    doc['research']['rows'][0]['excerpt']['sha256'] = digest('伪造片段'.encode())
    doc['brief_markdown'] = brief.brief_text(doc); news._seal(doc)
    prior = retain(store, doc)
    with pytest.raises(RecordError, match='ORIGINAL_MISMATCH'):
        build(store, {**p, 'baseline':asdict(prior)})


@pytest.mark.parametrize('changes', [{'start':0}, {'end':81}, {'start':True}, {'end':'2'}, {'extra':'ignored'}])
def test_bad_original_line_selection_rejected(changes):
    ref = {'repository':'synthetic', 'commit':'a'*40, 'path':'research/cases/x/revisions/r1/record.json', 'sha256':'b'*64}
    with pytest.raises(RecordError):
        research.plan_input({'items':[{'reference':ref, 'start':1, 'end':2, **changes}]})


def test_same_version_at_different_commit_cannot_duplicate_delivery():
    ref = {'repository':'synthetic', 'commit':'a'*40, 'path':'research/cases/x/revisions/r1/record.json', 'sha256':'b'*64}
    item = {'reference':ref, 'start':1, 'end':2}
    alias = deepcopy(item); alias['reference']['commit'] = 'c'*40
    with pytest.raises(RecordError, match='DUPLICATE_VERSION'):
        research.plan_input({'items':[item, alias]})


@pytest.mark.parametrize('value', [None, [], {'items':[],'max_items':True}, {'items':[],'max_items':9}, {'items':[],'ignored':1}])
def test_invalid_research_plan_never_silently_coerces(value):
    with pytest.raises(RecordError): research.plan_input(value)


def test_context_bound_retains_reference_and_explicit_omission(store, monkeypatch):
    item = revision(store, monkeypatch)
    monkeypatch.setattr(research, 'MAX_ROW_BYTES', 1)
    doc, _ = build(store, mixed_plan(index_ref(store), [item]))
    assert doc['research']['rows'][0]['item'] == item
    assert doc['research']['rows'][0]['error'] == 'BRIEF_RESEARCH_CONTEXT_BOUND'
    assert not doc['research']['included']
