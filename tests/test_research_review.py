"""Question review and actual passage adoption, not economic or human approval."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.application import brief_delivery, research_review as review
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_agenda import ResearchAgenda
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.research_work import read_workpaper, render_workpaper
from decision_kernel.methods.research import research_method
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_brief_workpaper import structured_revision
from test_news_delivery import store, index_ref, plan, retain, END


def successor(store, before, *, storage_only=False, **changes):
    request, progress = read_workpaper(store.read_revision(BlobRef(**before)))
    request = replace(request, predecessor=BlobRef(**before), **changes.pop('request_changes', {}))
    progress = replace(progress, body='Revised heading.\nCorrection with limitations.\nStill unverified.\n', **changes)
    if storage_only:
        # Model a format-valid retained legacy/lower-level input. The current
        # ResearchWorkflow rejects this transition; do not weaken its checks.
        with pytest.raises(RecordError):
            ResearchWorkflow(store).checkpoint(request, progress, submission_id='review-rejected', revision_id='r2')
        method=research_method(request.kind)
        ref=asdict(store.submit_revision('legacy-review-r2', RevisionInput(
            request.case_id,'r2',request.kind,render_workpaper(request,progress,method),request.mandate,
            method['id']+'@sha256:'+method['sha256'],progress.sources,request.predecessor,progress.unresolved(request))))
    else:
        ref = ResearchWorkflow(store).checkpoint(request, progress, submission_id='review-r2', revision_id='r2')['reference']
    return {'reference': ref, 'start': 2, 'end': 3}


def pair(store, monkeypatch, **changes):
    before = structured_revision(store, monkeypatch)
    after = successor(store, before['reference'], **changes)
    return {'before': before, 'after': after, 'question_id': 'q1'}


def room(store, selection, **extras):
    return ReadingRoom(store).build({'as_of': END, 'review': selection, **extras})


def with_brief(store, selection, start=2, end=3, side='after'):
    selected = {**selection[side], 'start': start, 'end': end}
    doc, _ = brief_delivery.build_edition(store.read_source, ResearchArtifacts(store),
        plan(index_ref(store), research={'items': [selected]}))
    return asdict(retain(store, doc))


def test_original_question_exact_predecessor_and_independent_readback(store, monkeypatch, tmp_path):
    selection = pair(store, monkeypatch)
    head = store.backend.head()
    first = room(store, selection)
    fresh = VersionedStore(GitStore(tmp_path/'news.git'))
    second = room(fresh, selection)
    assert first == second and fresh.backend.head() == head
    v = first['review']['view']
    assert v['revision_relation'] == 'exact-direct-successor'
    assert v['question_relation'] == 'same-question-text' and v['selected_text_changed']
    assert len(v['before']['challenges']) == len(v['after']['challenges']) == 1
    assert v['request']['status'] == 'not-supplied'
    assert v['human_acceptance'] == 'not-established' and v['execution'] == 'not-started'
    assert v['before']['finding']['evidence'] == [0]
    assert v['before']['evidence']['items'][0]['view']['data'] == '原有证据'


@pytest.mark.parametrize('start,end,status,overlap,missing', [
    (2,3,'full-range-included',[2,3],[]),
    (1,2,'partial-range-included',[2,2],[[3,3]]),
    (3,3,'partial-range-included',[3,3],[[2,2]]),
    (1,1,'outside-adopted-excerpt',None,[[2,3]]),
])
def test_actual_brief_version_badge_is_not_selected_correction_coverage(store,monkeypatch,start,end,status,overlap,missing):
    selection=pair(store,monkeypatch)
    ref=with_brief(store,selection,start,end)
    head=store.backend.head()
    v=room(store,selection,brief=ref)['review']['view']
    ad=v['adoption']['after']
    assert ad['version_included'] is True and ad['status']==status
    assert ad['overlap_range']==overlap and ad['uncovered_ranges']==missing
    assert v['adoption']['before']['status']=='not-considered-in-this-brief'
    assert store.backend.head()==head


def test_before_only_brief_does_not_adopt_successor_of_same_case(store,monkeypatch):
    p=pair(store,monkeypatch);ref=with_brief(store,p,1,2,side='before')
    v=room(store,p,brief=ref)['review']['view']
    assert v['adoption']['before']['status']=='full-range-included'
    assert v['adoption']['after']['version_included'] is False
    assert v['adoption']['after']['status']=='not-considered-in-this-brief'


def test_actual_same_version_omission_is_not_new_adoption(store,monkeypatch):
    p=pair(store,monkeypatch);ref=with_brief(store,p)
    doc,_=brief_delivery.open_edition(store.read_source,ResearchArtifacts(store),ref)
    following,_=brief_delivery.build_edition(store.read_source,ResearchArtifacts(store),{**doc['plan'],'baseline':ref})
    omitted=asdict(retain(store,following,'review-omitted'))
    v=room(store,p,brief=omitted)['review']['view']
    assert v['adoption']['after']['status']=='considered-not-included'
    assert v['adoption']['after']['version_included'] is False


def test_old_news_edition_does_not_claim_research_tracking(store,monkeypatch):
    p=pair(store,monkeypatch)
    doc,_=brief_delivery.build_edition(store.read_source,ResearchArtifacts(store),plan(index_ref(store)))
    v=room(store,p,brief=asdict(retain(store,doc)))['review']['view']
    assert v['adoption']['after']['status']=='research-not-tracked-in-this-brief'
    assert v['adoption']['after']['version_included'] is None


@pytest.mark.parametrize('fault',['reverse','same','unknown-question','wrong-case'])
def test_false_lineage_or_question_does_not_fallback_to_current(store,monkeypatch,fault):
    p=pair(store,monkeypatch)
    if fault=='reverse':p['before'],p['after']=p['after'],p['before']
    elif fault=='same':p['after']=deepcopy(p['before'])
    elif fault=='unknown-question':p['question_id']='invented'
    else:p['before']['reference']['path']=p['before']['reference']['path'].replace('/structured/','/other/')
    if fault=='same':
        with pytest.raises(RecordError,match='DISTINCT'):room(store,p)
    else:
        result=room(store,p)
        assert result['review']['status'] in ('missing','invalid') and result['readings'][0]['status']=='readable'
        assert 'view' not in result['review']


def test_removed_old_challenge_is_not_marked_resolved(store,monkeypatch):
    p=pair(store,monkeypatch,challenges=(),storage_only=True)
    v=room(store,p)['review']['view']
    assert v['after']['challenges']==[] and len(v['old_unresolved_not_carried_forward'])==1
    assert v['old_unresolved_not_carried_forward'][0]['disposition']=='unresolved'
    assert 'does-not-mean-resolved' in v['challenge_semantics']


def test_reworded_question_keeps_original_text_and_scope_change(store,monkeypatch):
    before=structured_revision(store,monkeypatch)
    request,progress=read_workpaper(store.read_revision(BlobRef(**before['reference'])))
    questions=(replace(request.questions[0],text='Changed question'),request.questions[1])
    after=successor(store,before['reference'],storage_only=True,request_changes={'questions':questions,'scope_change_reason':'Explicit scope change'})
    v=room(store,{'before':before,'after':after,'question_id':'q1'})['review']['view']
    assert v['question_relation']=='reworded-question'
    assert v['before']['question']['text']=='已回答的原问题'
    assert v['after']['scope_change_reason']=='Explicit scope change'


def test_removed_question_and_findings_are_not_silently_resolved(store,monkeypatch):
    before=structured_revision(store,monkeypatch)
    request,progress=read_workpaper(store.read_revision(BlobRef(**before['reference'])))
    after=successor(store,before['reference'],storage_only=True,request_changes={'questions':request.questions[1:],'scope_change_reason':'Narrowing'},findings=progress.findings[1:],challenges=())
    v=room(store,{'before':before,'after':after,'question_id':'q1'})['review']['view']
    assert v['question_relation']=='not-carried-forward' and v['after']['finding'] is None
    assert v['before']['question'] is not None and len(v['old_unresolved_not_carried_forward'])==1


def test_missing_sources_are_local_but_not_zero_evidence(store,monkeypatch):
    p=pair(store,monkeypatch)
    def missing(*args):raise MissingObject('not-retained')
    monkeypatch.setattr(store,'read_source',missing)
    v=room(store,p)['review']['view']
    assert v['status']=='partial' and v['before']['excerpt']['text']
    assert v['after']['evidence']['total']==1 and v['after']['evidence']['items'][0]['status']=='missing'


def test_source_display_bound_retains_exact_request_without_truncation(store,monkeypatch):
    p=pair(store,monkeypatch);monkeypatch.setattr(review,'MAX_EVIDENCE_BYTES',50)
    v=room(store,p)['review']['view'];r=v['after']['evidence']['items'][0]
    assert v['status']=='partial' and r['view'] is None and r['display']=='not-included'
    assert r['open_request']['payload']==r['reference']


def test_whole_panel_budget_omits_panel_not_original_research(store,monkeypatch):
    p=pair(store,monkeypatch);monkeypatch.setattr(review,'MAX_REVIEW_BYTES',20)
    r=room(store,p)
    assert r['review']['status']=='not-included' and 'view' not in r['review']
    assert r['readings'][0]['status']=='readable' and r['status']=='partial'


def test_final_access_loss_refuses_an_otherwise_readable_review(store,monkeypatch):
    p=pair(store,monkeypatch);side=review._side;calls=[]
    def wrapped(*args,**kw):
        result=side(*args,**kw);calls.append(1)
        if len(calls)==2:
            def denied():raise StoreUnavailable('revoked')
            monkeypatch.setattr(store.backend,'head',denied)
        return result
    monkeypatch.setattr(review,'_side',wrapped)
    none={'reference':None,'status':'not-supplied'}
    with pytest.raises(StoreUnavailable):review.open_review(store,p,agenda=none,brief=none)


@pytest.mark.parametrize('flag',[False,None,'true'])
def test_source_export_permission_is_separate_before_io(store,monkeypatch,flag):
    p=pair(store,monkeypatch);calls=[]
    monkeypatch.setattr(store.backend,'head',lambda:calls.append(1))
    with pytest.raises(RecordError,match='PUBLIC_MATERIAL'):
        reading_commands.perform(store,{'plan':{'as_of':END,'review':p},'public_research_custody_confirmed':True,'public_material_custody_confirmed':flag})
    assert not calls


@pytest.mark.parametrize('field,value',[('human_acceptance',True),('actor','human'),('after',{}),('evidence_limit',True),('evidence_limit',5)])
def test_request_does_not_accept_conclusions_or_invalid_limits(store,monkeypatch,field,value):
    p=pair(store,monkeypatch);p[field]=value
    with pytest.raises(RecordError):review.review_plan(p)


def test_saved_feedback_is_exact_target_and_posthoc_is_explicit(store,monkeypatch):
    p=pair(store,monkeypatch)
    agenda=ResearchAgenda(store).build({'references':[p['before']['reference']],'as_of':END,'changes':[{
        'reference':p['before']['reference'],'question_id':'q1','action':'focus','reason':'Synthetic request',
        'feedback':{'observation':'Evidence challenges the original.','implication':'Please recheck.','outcome':'revision-requested'}}]})
    ref=asdict(ResearchArtifacts(store).put('review-agenda',ArtifactInput.from_dict(agenda['artifact'])))
    v=room(store,p,agenda=ref)['review']['view']
    assert v['request']['status']=='revision-requested' and v['request']['timing']=='recorded-after-revision'
    assert v['request']['selection']['reference']==p['before']['reference']
    assert v['human_acceptance']=='not-established'
    # The request about q1 is not silently attached to another original question.
    other=room(store,{**p,'question_id':'q2'},agenda=ref)['review']['view']
    assert other['request']['status']=='no-matching-feedback'


def test_legacy_no_review_shape_and_inert_page(store,monkeypatch):
    p=pair(store,monkeypatch)
    old=ReadingRoom(store).build({'as_of':END})
    assert 'review' not in old and 'review' not in old['plan']
    view=room(store,p);view['review']['view']['after']['excerpt']['text']='</script><script>globalThis.pwned=1</script>'
    page=render_page(view)
    assert page.count('<script>')==1 and r'\u003c/script\u003e' in page
    assert 'review-content' in page and '原问题复核' in page
    assert 'textContent' in page and 'innerHTML' not in page and 'fetch(' not in page


def test_native_host_review_uses_original_read_route(host,tmp_path,monkeypatch):
    from decision_kernel.infrastructure.github_store import GitHubStore
    from test_research_runtime import invoke
    writer=VersionedStore(GitHubStore(replace(host.binding,write_enabled=True),host))
    p=pair(writer,monkeypatch)
    n=host.writes
    payload={'plan':{'as_of':END,'review':p},'public_research_custody_confirmed':True,'public_material_custody_confirmed':True}
    r=invoke(host,tmp_path/'review-read','reading-room',payload)
    assert r['status']=='completed' and r['write']=='not-requested' and host.writes==n
    assert r['result']['review']['view']['revision_relation']=='exact-direct-successor'
    assert (tmp_path/'review-read'/'reading-room.html').exists()


# Reuse the original authenticated API model fixture; no new host or installation.
from test_research_runtime import host


def test_evidence_budget_keeps_unread_reference_and_original_selection(store,monkeypatch):
    before=structured_revision(store,monkeypatch)
    request,progress=read_workpaper(store.read_revision(BlobRef(**before['reference'])))
    source=store.put_source('review-source-two',SourceInput('review-source-two','content','Synthetic','synthetic:review','First.\nSelected.\nLast.\n'))
    from decision_kernel.domain.records import SourceRef,Selection
    selected=SourceRef(source.manifest,Selection(kind='lines',start=2,end=2))
    findings=(replace(progress.findings[0],evidence=(0,1)),progress.findings[1])
    after=successor(store,before['reference'],sources=(*progress.sources,selected),findings=findings)
    p={'before':before,'after':after,'question_id':'q1','evidence_limit':1}
    first=room(store,p)['review']['view']
    assert first['status']=='partial' and first['after']['evidence']['remaining']==1
    assert first['after']['evidence']['pending'][0]['open_request']['payload']==json.loads(encoded(asdict(selected)))
    second=room(store,{**p,'evidence_limit':2})['review']['view']
    assert second['after']['evidence']['items'][1]['view']['data']=='Selected.\n'
    assert second['after']['evidence']['items'][1]['reference']==json.loads(encoded(asdict(selected)))


def test_unavailable_brief_never_becomes_not_adopted(store,monkeypatch):
    p=pair(store,monkeypatch);ref=with_brief(store,p)
    def missing(*args):raise MissingObject('missing edition')
    monkeypatch.setattr(ReadingRoom,'_brief',missing)
    v=room(store,p,brief=ref)['review']['view']
    assert v['adoption']['after']['status']=='unknown'
    assert v['adoption']['after']['version_included'] is None
    assert v['adoption']['after']['uncovered_ranges'] is None


def test_actual_brief_overlapping_bytes_cannot_be_forged(store,monkeypatch):
    p=pair(store,monkeypatch);ref=with_brief(store,p)
    r=room(store,p);doc=room(store,p,brief=ref)['brief']
    doc['view']['research']['rows'][0]['excerpt']['text']='forged\nbytes\n'
    with pytest.raises(RecordError,match='ADOPTED_BYTES'):
        review.adopted_range(p['after'],r['review']['view']['after']['excerpt'],doc)


def exercise_installed_review(outside, python, env, store):
    """Extend the existing single wheel consumer, with an isolated synthetic case."""
    script=r'''
import json,sys
from dataclasses import asdict,replace
from pathlib import Path
from decision_kernel.application import versioned_store,brief_delivery
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import BlobRef,encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.domain.research_work import read_workpaper
s=VersionedStore(GitStore(Path(sys.argv[1])))
old=s.read_revision(s.current('mixed-research'))
request,progress=read_workpaper(old)
request=replace(request,case_id='installed-question-review',predecessor=None)
versioned_store._now=lambda:'2026-10-16T10:05:00Z'
a=ResearchWorkflow(s).checkpoint(request,progress,submission_id='review-installed-one',revision_id='r1')['reference']
request=replace(request,predecessor=BlobRef(**a))
progress=replace(progress,body='Synthetic revised heading.\nActual qualified revision.\nStill uncertain.\n')
b=ResearchWorkflow(s).checkpoint(request,progress,submission_id='review-installed-two',revision_id='r2')['reference']
head=s.backend.head()
paths=[p for p in s.backend.paths(head,'research/artifacts/mixed-edition/') if p.endswith('/manifest.json')]
assert len(paths)==1
prior,_=brief_delivery.open_edition(s.read_source,ResearchArtifacts(s),asdict(s.backend.reference(head,paths[0])))
# Adopt only the revised heading; the actual qualified revision is outside it.
p={**prior['plan'],'research':{'items':[{'reference':b,'start':1,'end':1}]}}
doc,_=brief_delivery.build_edition(s.read_source,ResearchArtifacts(s),p)
br=ResearchArtifacts(s).put('review-installed-brief',ArtifactInput.from_bytes('review-installed-brief','brief.json','application/json',encoded(doc)))
plan={'as_of':prior['plan']['cutoff'],'catalogue':{'limit':1},'brief':asdict(br),
      'review':{'before':{'reference':a,'start':1,'end':1},'after':{'reference':b,'start':2,'end':3},'question_id':request.questions[0].question_id}}
Path('review-plan.json').write_text(json.dumps(plan),encoding='utf-8')
print(s.backend.head())
'''
    built=subprocess.run([str(python),'-I','-c',script,str(store)],cwd=outside,env=env,capture_output=True,text=True)
    assert built.returncode==0,built.stderr
    executable=Path(python).parent/'decision-os'
    outputs=[]
    for name in ('installed-question-review.html','installed-question-review-again.html'):
        result=subprocess.run([str(executable),'store','--path',str(store),'research-room','--input',str(outside/'review-plan.json'),'--out',str(outside/name)],cwd=outside,env=env,capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        outputs.append(json.loads(result.stdout))
    assert outputs[0]==outputs[1] and outputs[0]['catalogue']['snapshot']==built.stdout.strip()
    v=outputs[1]['review']['view']
    assert v['adoption']['after']['version_included'] is True
    assert v['adoption']['after']['status']=='outside-adopted-excerpt'
    assert v['human_acceptance']=='not-established' and v['persistence']=='not-written'
    assert (outside/'installed-question-review.html').read_bytes()==(outside/'installed-question-review-again.html').read_bytes()


def test_changed_mandate_preserves_both_scopes_without_silent_completion(store,monkeypatch):
    before=structured_revision(store,monkeypatch)
    after=successor(store,before['reference'],request_changes={'mandate':'Changed mandate','scope_change_reason':'Authorized scope change'})
    v=room(store,{'before':before,'after':after,'question_id':'q1'})['review']['view']
    assert v['mandate_changed'] is True and v['status']=='partial'
    assert v['before']['mandate']=='合成原委托' and v['after']['mandate']=='Changed mandate'
    assert v['human_acceptance']=='not-established'
