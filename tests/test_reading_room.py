"""Composition and UI payload boundaries; real owners are exercised in integration."""
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from decision_kernel.application import reading_room as room
from decision_kernel.domain.records import IntegrityError, MissingObject, RecordError, StoreUnavailable, digest, encoded
from decision_kernel.interfaces.reading_commands import preflight
from decision_kernel.interfaces.reading_page import render_page

AT = '2026-10-17T00:00:00Z'


def ref(revision='r1'):
    return dict(repository='example/store', commit='a'*40,
                path=f'research/cases/example/revisions/{revision}/record.json', sha256='b'*64)


def artifact():
    return dict(repository='example/store', commit='a'*40,
                path='research/artifacts/example/'+'c'*64+'/manifest.json', sha256='d'*64)


class Model(room.ReadingRoom):
    def __init__(self):
        self.accesses = []
        backend = SimpleNamespace(head=lambda: self.accesses.append('head'))
        super().__init__(SimpleNamespace(backend=backend, read_source=lambda _: None))
        self.catalogue_error = self.agenda_error = self.brief_error = self.research_error = None
        self.body = 'Original research.\nUnresolved question.\n'
        self.catalogue = dict(items=[dict(case_id='example', revision_id='r1', kind='full',
            status='readable', reference=ref(), mandate='Original mandate')], status='readable-page',
            snapshot='a'*40, next_cursor=None, returned=1, total_cases=1,
            version_scope='current-per-case-at-snapshot', order='case-id-not-priority-or-recency')
        self.agenda = dict(status='readable', cards=[], unselected_attention=[], history_verification='current-step-only')
        self.brief = dict(attention_state=[], plan={'cutoff':AT}, edition_sha256='e'*64,
            comparison={}, brief_markdown='Original Brief.\n', research=dict(
                agenda_reference=None, rows=[dict(item=dict(reference=ref(), start=1, end=2),
                    version_id='f'*64, status='readable')],
                included=[dict(version_id='f'*64, classification='late-to-delivery')]))

    def _catalogue(self, args):
        self.accesses.append(('catalogue',deepcopy(args)))
        if self.catalogue_error: raise self.catalogue_error
        return deepcopy(self.catalogue)

    def _agenda(self, refs, plan):
        self.accesses.append(('agenda',deepcopy(refs)))
        if self.agenda_error: raise self.agenda_error
        return deepcopy(self.agenda)

    def _brief(self, reference):
        self.accesses.append(('brief',deepcopy(reference)))
        if self.brief_error: raise self.brief_error
        return deepcopy(self.brief)

    def _research(self, reference):
        self.accesses.append(('research',deepcopy(reference)))
        if self.research_error: raise self.research_error
        return dict(case_id='example', revision_id=reference['path'].split('/')[-2], kind='full',
                    body=self.body, body_sha256=digest(self.body.encode()), stored_at=AT, predecessor=None)


@pytest.mark.parametrize('change', [dict(as_of='no-clock'),dict(as_of='2026-10-17T00:00:00'),
    dict(catalogue=[]),dict(catalogue={'limit':True}),dict(catalogue={'limit':0}),
    dict(catalogue={'limit':5}),dict(catalogue={'limit':'2'}),dict(catalogue={'unknown':1}),
    dict(reference={**ref(), 'commit':'main'}),dict(brief=ref()),dict(agenda=ref()),dict(changes=[])])
def test_invalid_plans_fail_before_any_read(change):
    model=Model()
    with pytest.raises(RecordError): model.build({'as_of':AT, **change})
    assert not model.accesses


def test_empty_catalogue_is_not_no_research_execution_or_quiet_market():
    model=Model();model.catalogue.update(items=[],status='no-saved-research',returned=0,total_cases=0)
    result=model.build({'as_of':AT})
    assert result['catalogue']['status']=='no-saved-research' and result['readings']==[]
    assert result['execution']=='not-started' and result['human_selection']=='not-recorded-by-reading'
    assert result['persistence']=='not-written' and result['brief']['status']=='not-supplied'
    assert model.accesses[-1]=='head'


def test_next_plan_reuses_exact_cursor_and_does_not_mutate_inputs():
    model=Model();cursor=dict(schema='research-catalogue-cursor-v1',repository='example/store',snapshot='c'*40,offset=1)
    model.catalogue['next_cursor']=cursor
    plan=dict(as_of=AT,catalogue={'limit':1},brief=artifact(),reference=ref())
    before=deepcopy(plan);result=model.build(plan)
    assert plan==before and result['next_plan']['catalogue']=={'limit':1,'cursor':cursor}
    assert len(result['readings'])==1 and result['readings'][0]['brief_relation']['status']=='included'
    assert result['readings'][0]['brief_relation']['classification']=='late-to-delivery'
    result['catalogue']['items'].clear()
    assert len(model.build(plan)['readings'])==1


def test_same_case_other_version_is_not_adopted_and_explicit_original_is_retained():
    model=Model();model.catalogue['items'][0].update(reference=ref('r2'),revision_id='r2')
    result=model.build(dict(as_of=AT,reference=ref(),brief=artifact()))
    current,old=result['readings']
    assert current['brief_relation']['status']=='not-considered-in-this-brief'
    assert old['brief_relation']['status']=='included' and old['origin']=='explicit-reference'
    assert result['catalogue']['total_cases']==1


@pytest.mark.parametrize('error,status', [(MissingObject('raw private error'),'missing'),
    (StoreUnavailable('raw private error'),'unavailable'),(IntegrityError('raw private error'),'invalid'),
    (RecordError('raw private error'),'invalid')])
def test_optional_component_failure_is_visible_and_does_not_destroy_original_reading(error,status):
    model=Model();model.agenda_error=error;model.brief_error=error
    result=model.build(dict(as_of=AT,agenda=artifact(),brief=artifact()))
    assert result['status']=='partial' and result['agenda']['status']==status and result['brief']['status']==status
    assert result['readings'][0]['body']==model.body and result['readings'][0]['brief_relation']['status']=='unknown'
    assert b'raw private error' not in encoded(result)


def test_global_catalogue_failure_is_not_a_successful_empty_room():
    model=Model();model.catalogue_error=StoreUnavailable('denied')
    with pytest.raises(StoreUnavailable): model.build({'as_of':AT})
    assert len(model.accesses)==1


def test_end_access_revocation_blocks_export():
    model=Model()
    model.store.backend.head=lambda: (_ for _ in ()).throw(StoreUnavailable('denied'))
    with pytest.raises(StoreUnavailable): model.build({'as_of':AT})


def test_oversized_body_is_omitted_whole_and_keeps_original_open_request(monkeypatch):
    model=Model();monkeypatch.setattr(room,'MAX_BODY_BYTES',1)
    result=model.build({'as_of':AT});row=result['readings'][0]
    assert row['status']=='not-included' and 'body' not in row
    assert row['open_request']=={'operation':'resume','payload':{'reference':ref()}}
    assert result['status']=='partial'


def test_total_body_budget_keeps_unshown_reference_without_truncation(monkeypatch):
    model=Model();detail=model._research(ref());model.accesses.clear()
    monkeypatch.setattr(room,'MAX_BODIES_BYTES',len(encoded(detail)))
    result=model.build({'as_of':AT,'reference':ref('r2')})
    assert result['readings'][0]['body']==model.body
    assert result['readings'][1]['status']=='not-included' and result['readings'][1]['reference']==ref('r2')


def test_panel_budget_does_not_fabricate_empty_agenda(monkeypatch):
    model=Model();monkeypatch.setattr(room,'MAX_PANEL_BYTES',1)
    result=model.build({'as_of':AT})
    assert result['agenda']['status']=='not-included' and 'view' not in result['agenda']
    assert result['status']=='partial'


def test_research_failure_does_not_hide_catalogue_or_saved_brief():
    model=Model();model.research_error=MissingObject('missing')
    result=model.build(dict(as_of=AT,brief=artifact()))
    assert result['readings'][0]['status']=='missing' and result['catalogue']['returned']==1
    assert result['brief']['view']['brief_markdown']==model.brief['brief_markdown']


def test_considered_is_not_included_and_legacy_brief_does_not_claim_research_tracking():
    model=Model();model.brief['research']['included']=[]
    result=model.build(dict(as_of=AT,brief=artifact()))
    assert result['readings'][0]['brief_relation']['status']=='considered-not-included'
    del model.brief['research']
    assert model.build(dict(as_of=AT,brief=artifact()))['readings'][0]['brief_relation']['status']=='research-not-tracked-in-this-brief'


@pytest.mark.parametrize('flag',[None,False,0,'true'])
def test_public_research_preflight_requires_explicit_boolean(flag):
    with pytest.raises(RecordError,match='PUBLIC_RESEARCH'):
        preflight('reading-room',{'plan':{'as_of':AT},'public_research_custody_confirmed':flag})


def test_feedback_reference_needs_separate_confirmation_before_reading():
    payload=dict(plan=dict(as_of=AT,agenda=artifact()),public_research_custody_confirmed=True)
    with pytest.raises(RecordError,match='FEEDBACK'): preflight('reading-room',payload)
    model=Model()
    with pytest.raises(RecordError,match='FEEDBACK'): model.build(payload['plan'],feedback_export=False)
    assert not model.accesses


@pytest.mark.parametrize('field',['attention','research-agenda'])
def test_saved_brief_notes_are_withheld_without_feedback_export_rights(field):
    model=Model()
    if field=='attention': model.brief['attention_state']=[{'reason':'SYNTHETIC_RESTRICTED_NOTE'}]
    else: model.brief['research']['agenda_reference']=artifact();model.brief['brief_markdown']='SYNTHETIC_RESTRICTED_NOTE'
    result=model.build(dict(as_of=AT,brief=artifact()),feedback_export=False)
    assert result['brief']['status']=='permission-required' and result['status']=='partial'
    assert b'SYNTHETIC_RESTRICTED_NOTE' not in encoded(result)
    assert result['readings'][0]['brief_relation']['status']=='unknown'


def test_offline_page_preserves_literal_body_and_has_no_remote_execution():
    model=Model();model.body='</script><img src="https://invalid.example/x" onerror="alert(1)">\u2028&\n'
    result=model.build(dict(as_of=AT,brief=artifact()));html=render_page(result)
    assert model.body not in html and '\\u003c/script\\u003e' in html
    policies = []
    class Metadata(HTMLParser):
        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == 'meta' and attributes.get('http-equiv') == 'Content-Security-Policy':
                policies.append(attributes.get('content', ''))
    Metadata().feed(html)  # HTML attributes decode character references in the browser.
    assert len(policies) == 1 and "connect-src 'none'" in policies[0]
    assert "default-src 'none'" in policies[0] and "script-src 'sha256-" in policies[0]
    assert 'textContent=text' in html
    assert 'innerHTML' not in html and 'fetch(' not in html and 'localStorage' not in html
    assert 'reading-room-next.json' in html and 'research.md' in html and 'brief.md' in html
    assert result['readings'][0]['body']==model.body
