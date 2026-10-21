"""Actual store/host readers and the existing single cold-wheel consumer."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_agenda import ResearchAgenda
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application import brief_delivery, versioned_store
from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, RevisionInput, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands, research_runtime
from test_brief_workpaper import structured_revision
from test_news_delivery import store, index_ref, plan, retain, END
from test_news_read_budget import sources, transport
from test_research_runtime import inputs


def retained_inputs(store, monkeypatch):
    item=structured_revision(store,monkeypatch)
    doc,_=brief_delivery.build_edition(store.read_source,ResearchArtifacts(store),
                                     plan(index_ref(store),research={'items':[item]}))
    saved=retain(store,doc,'room-brief')
    view=ResearchAgenda(store).build({'references':[item['reference']],'as_of':END,'changes':[
        {'reference':item['reference'],'question_id':'q2','action':'defer','reason':'合成暂缓，不是真实用户反馈'}]})
    agenda=ResearchArtifacts(store).put('room-agenda',ArtifactInput.from_dict(view['artifact']))
    return item,doc,{'as_of':END,'brief':asdict(saved),'agenda':asdict(agenda)}


def test_existing_catalogue_agenda_and_original_brief_open_together_without_writes(store,monkeypatch,tmp_path):
    item,doc,p=retained_inputs(store,monkeypatch);head=store.backend.head()
    result=ReadingRoom(store).build(p)
    assert result['status']=='readable' and store.backend.head()==head
    row,=result['readings']
    original=store.read_revision(BlobRef(**item['reference']))
    assert row['reference']==item['reference'] and row['body']==original['body']
    assert row['brief_relation']['status']=='included' and result['brief']['view']==doc
    card,=result['agenda']['view']['cards']
    assert card['agenda'][1]['attention_status']=='defer'
    assert len(card['continuation']['unresolved_challenges'])==1
    fresh=VersionedStore(GitStore(tmp_path/'news.git'))
    assert encoded(ReadingRoom(fresh).build(p))==encoded(result) and fresh.backend.head()==head
    assert result['human_selection']=='not-recorded-by-reading'


def test_current_and_explicit_old_version_have_distinct_agenda_and_adoption(store,monkeypatch):
    item,_,p=retained_inputs(store,monkeypatch)
    newer=store.submit_revision('room-newer',RevisionInput('structured','r2','full',
        'New original text.','合成原委托','synthetic',predecessor=BlobRef(**item['reference']),unresolved=('待复核',)))
    p['reference']=item['reference'];result=ReadingRoom(store).build(p)
    current,old=result['readings']
    assert current['reference']==asdict(newer) and current['brief_relation']['status']=='not-considered-in-this-brief'
    assert old['reference']==item['reference'] and old['brief_relation']['status']=='included'
    cards=result['agenda']['view']['cards']
    assert cards[0]['agenda'][0]['selection'] is None
    assert cards[1]['agenda'][1]['attention_status']=='defer'
    assert result['catalogue']['total_cases']==1 and old['origin']=='explicit-reference'


def test_pagination_pins_original_snapshot_after_new_case_and_revision(store,monkeypatch):
    item=structured_revision(store,monkeypatch)
    def add(case):
        return store.submit_revision('room-'+case,RevisionInput(case,'r1','quick','Synthetic report',
            'Synthetic mandate','synthetic',unresolved=('Not assessed',)))
    add('z-last')
    first=ReadingRoom(store).build({'as_of':END,'catalogue':{'limit':1}})
    add('a-new')
    rest=ReadingRoom(store).build(first['next_plan'])
    assert first['catalogue']['snapshot']==rest['catalogue']['snapshot']
    assert rest['catalogue']['total_cases']==2 and rest['catalogue']['items'][0]['case_id']=='z-last'
    assert rest['next_plan'] is None and ReadingRoom(store).build({'as_of':END})['catalogue']['total_cases']==3
    assert first['readings'][0]['reference']==item['reference']


def test_unreadable_agenda_does_not_become_no_choices_and_does_not_hide_research(store,monkeypatch):
    item,_,p=retained_inputs(store,monkeypatch)
    p['agenda']=p['brief']  # Real retained wrong-schema artifact, not a mocked failure.
    result=ReadingRoom(store).build(p)
    assert result['agenda']['status']=='invalid' and result['status']=='partial'
    assert result['readings'][0]['reference']==item['reference'] and result['brief']['status']=='readable'


def test_missing_source_preserves_body_but_invalidates_dependent_brief_reconstruction(store,monkeypatch):
    item,_,p=retained_inputs(store,monkeypatch)
    read=store.read_source
    target=store.read_revision(BlobRef(**item['reference']))['record']['input']['sources'][0]
    def missing(ref):
        value=asdict(ref) if not isinstance(ref,dict) else ref
        if encoded(value)==encoded(target): raise MissingObject('synthetic missing source')
        return read(ref)
    monkeypatch.setattr(store,'read_source',missing)
    result=ReadingRoom(store).build(p)
    assert result['readings'][0]['status']=='readable'
    assert result['catalogue']['items'][0]['source_page']['items'][0]['status']=='missing'
    assert result['brief']['status']=='invalid' and result['readings'][0]['brief_relation']['status']=='unknown'


def test_native_read_host_produces_html_and_retains_exact_result_without_write(sources,monkeypatch,tmp_path):
    model,store,_,_,_=sources
    item,doc,p=retained_inputs(store,monkeypatch);before=model.git.head();api=transport(model)
    payload={'plan':p,'public_research_custody_confirmed':True,'public_feedback_custody_confirmed':True}
    report=research_runtime.run_command(model.binding,api=api,inputs=inputs('reading-room',payload),
        code_sha='a'*40,output=tmp_path/'room-host',run_id='123',attempt='1')
    assert report['status']=='completed' and report['write']=='not-requested'
    assert model.writes==0 and model.git.head()==before and 0<api._requests<512
    result=report['result'];assert result['brief']['view']==doc
    assert result['readings'][0]['reference']==item['reference']
    html=(tmp_path/'room-host/reading-room.html').read_text()
    assert '统一阅读室' in html and '原作者的经营判断' in html
    assert json.loads((tmp_path/'room-host/report.json').read_bytes())['result']==result


@pytest.mark.parametrize('flag',[False,'true',None])
def test_native_preflight_rejects_before_output_or_storage(sources,tmp_path,flag):
    model,_,_,_,_=sources;api=transport(model);out=tmp_path/'denied'
    with pytest.raises(RecordError,match='PUBLIC_RESEARCH'):
        research_runtime.run_command(model.binding,api=api,
            inputs=inputs('reading-room',{'plan':{'as_of':END},'public_research_custody_confirmed':flag}),
            code_sha='a'*40,output=out)
    assert not out.exists() and api._requests==0 and model.writes==0


def test_output_file_is_not_overwritten_and_model_not_mutated(store,monkeypatch,tmp_path):
    structured_revision(store,monkeypatch);result=ReadingRoom(store).build({'as_of':END});before=encoded(result)
    out=tmp_path/'room.html';reading_commands.write_page(out,result);raw=out.read_bytes()
    with pytest.raises(RecordError,match='OUTPUT_EXISTS'): reading_commands.write_page(out,result)
    assert out.read_bytes()==raw and encoded(result)==before


def test_existing_workflow_exposes_only_a_new_read_operation():
    root=Path(__file__).parents[1]
    workflow=yaml.safe_load((root/'.github/workflows/research-runtime.yml').read_text())
    trigger=workflow.get('on',workflow.get(True))
    assert 'reading-room' in trigger['workflow_dispatch']['inputs']['operation']['options']
    assert research_runtime.READ_OPERATIONS >= {'reading-room'} and 'reading-room' not in research_runtime.WRITE_OPERATIONS
    assert workflow['jobs']['read']['permissions']=={'contents':'read','actions':'read'}
    assert 'reading-room' not in workflow['jobs']['write']['if'] and 'schedule' not in trigger


def exercise_installed_room(outside,python,env,store):
    """Consume original research and both Brief owners in the one existing wheel."""
    script=r'''
import base64,json,sys
from dataclasses import asdict
from pathlib import Path
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application import hosted_handoff
from decision_kernel.domain.records import digest
from decision_kernel.domain.research_artifacts import ArtifactInput
s=VersionedStore(GitStore(Path(sys.argv[1])))
head=s.backend.head()
paths=[p for p in s.backend.paths(head,'research/artifacts/mixed-edition/') if p.endswith('/manifest.json')]
assert len(paths)==1
artifacts=ResearchArtifacts(s)
original=artifacts.read(s.backend.reference(head,paths[0]))
artifacts.put('installed-discovery-edition',ArtifactInput.from_bytes('news-edition-installed-discovery',
 'news-edition.json','application/json',base64.b64decode(original['data_base64'])))
research=s.current('mixed-research')
ai_doc={'schema':'hosted-ai-handoff-v1','kind':'brief','execution_date':'2026-10-16',
 'cutoff':'2026-10-16T12:00:00Z','scope':'Synthetic installed author Brief, no investment task',
 'gaps':['No actual business acceptance'],'baseline':None,'body':'# Synthetic AI Brief\r\nOriginal saved author text.\n',
 'adoptions':[{'reference':asdict(research),'body_sha256':digest(s.read_revision(research)['body'].encode()),
              'used_change':'Preserve the original synthetic research challenge'}]}
ai_saved=hosted_handoff.save(s,ai_doc,'installed-room-ai-brief')
head=s.backend.head()
plan={'as_of':'2026-10-16T12:00:00Z','catalogue':{'limit':1},
 'reference':asdict(s.current('mixed-research')),'brief':{'select':'latest-saved'},
 'ai_brief':ai_saved['reference'],
 'odds_book':{'research_ref':asdict(s.current('odds-case')),'limit':8,'scan_limit':32}}
Path('room-plan.json').write_text(json.dumps(plan),encoding='utf-8')
print(head)
'''
    setup=subprocess.run([str(python),'-I','-c',script,str(store)],cwd=outside,env=env,capture_output=True,text=True)
    assert setup.returncode==0,setup.stderr
    executable=Path(python).parent/'decision-os'
    def run(name):
        p=subprocess.run([str(executable),'store','--path',str(store),'research-room',
            '--input',str(outside/'room-plan.json'),'--out',str(outside/name)],
            cwd=outside,env=env,capture_output=True,text=True)
        assert p.returncode==0,p.stderr
        return json.loads(p.stdout)
    first=run('installed-reading-room.html');second=run('installed-reading-room-again.html')
    assert first==second and first['catalogue']['snapshot']==setup.stdout.strip()
    assert first['brief_discovery']['status']=='selected' and first['brief_discovery']['complete'] is True
    assert first['plan']['brief']==first['brief']['reference']
    from decision_kernel.domain.records import digest
    ai=first['ai_brief']
    assert ai['status']=='readable' and ai['reference']==first['plan']['ai_brief']
    assert ai['view']['document']['body']=='# Synthetic AI Brief\r\nOriginal saved author text.\n'
    assert ai['view']['body_sha256']==digest(ai['view']['document']['body'].encode())
    assert ai['view']['document']['adoptions'][0]['reference']==first['plan']['reference']
    assert ai['view']['research'][0]['reference']==first['plan']['reference']
    assert ai['watch_publication_required'] is False
    pinned=next(r for r in first['readings'] if r.get('case_id')=='mixed-research')
    assert pinned['case_id']=='mixed-research' and pinned['brief_relation']['status']=='included'
    assert 'Original finding.' in pinned['body'] and first['persistence']=='not-written'
    book=first['research_book']
    projected=next(r for r in book['entries'] if r['reference']==pinned['reference'])
    assert projected['workpaper_status']=='validated' and projected['brief_relation']['status']=='included'
    assert projected['workpaper']['questions']==[{'question_id':'q1','text':'Qualified answer?'}]
    assert projected['workpaper']['challenges'][0]['argument']=='Alternative'
    assert projected['workpaper']['stop']=='continue' and 'body' not in projected
    assert projected['research_fact_cutoff'] is projected['standard_company_id'] is None
    assert book['catalogue']['total_cases']==first['catalogue']['total_cases']
    assert book['catalogue']['snapshot']==first['catalogue']['snapshot']
    assert book['source_requests']==book['extra_revision_reads']==book['writes']==0
    assert 'Research Book' in (outside/'installed-reading-room.html').read_text()
    from decision_kernel.application.conditional_odds import RESULT_RECORD
    odds=first['odds_book']['view']
    assert odds['snapshot']==first['catalogue']['snapshot'] and odds['coverage']=='this-page-only'
    assert odds['semantic_replay']=='not-requested' and odds['current_validity']=='not-certified'
    assert any(row['kind']==RESULT_RECORD and row['status']=='readable' for row in odds['items'])
    assert 'Odds Book' in (outside/'installed-reading-room.html').read_text()
    assert (outside/'installed-reading-room.html').read_bytes()==(outside/'installed-reading-room-again.html').read_bytes()


def test_explicit_offpage_current_revision_has_no_inferred_age_label(store, monkeypatch):
    from decision_kernel.interfaces.reading_page import render_page

    item = structured_revision(store, monkeypatch)
    store.submit_revision('room-earlier-case', RevisionInput('a-first', 'r1', 'quick',
        'Another synthetic report.', 'Synthetic mandate', 'synthetic', unresolved=('Not assessed',)))
    result = ReadingRoom(store).build({'as_of': END, 'catalogue': {'limit': 1},
                                       'reference': item['reference']})
    assert result['catalogue']['items'][0]['case_id'] == 'a-first'
    assert store.current('structured') == BlobRef(**item['reference'])
    pinned = next(row for row in result['readings'] if row['origin'] == 'explicit-reference')
    assert pinned['reference'] == item['reference']
    page = render_page(result)
    assert '显式指定版本，不判断是否为当前版' in page
    assert '显式指定版本，不是目录当前版' not in page
