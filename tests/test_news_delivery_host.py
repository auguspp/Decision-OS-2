"""Existing native news host, real Git/GitHubAPI/CAS; only HTTP wire is synthetic."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

import pytest

from decision_kernel.application import news_delivery as desk
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.domain.records import encoded
from decision_kernel.interfaces import news_rolling_cli as host, news_delivery_cli as delivery, research_runtime
from test_news_read_budget import sources, setup, transport
from test_news_rolling import save
from test_news_delivery import END, choice


def setup_desk(sources, monkeypatch, tmp_path, **kw):
    model, store, _, _, index = sources
    ref = save(store,'desk-index',index)
    plan = {'window':asdict(ref),'cutoff':END,'kind':'evening'}
    api, inputs=setup(monkeypatch,tmp_path,model,plan,operation='desk',**kw)
    return model, store, api, inputs, plan


def test_native_save_then_independent_full_desk_read_without_source_calls(sources,monkeypatch,tmp_path):
    model,_,api,inputs,_=setup_desk(sources,monkeypatch,tmp_path)
    out=tmp_path/'edition'
    assert host.main(['build','--out',str(out)])==0
    report=json.loads((out/'desk-report.json').read_bytes())
    assert report['write']=='published' and model.writes==1 and 0<api._requests<512
    doc=(out/'edition.json').read_bytes();body=(out/'news-brief.md').read_bytes()
    fresh,_=setup(monkeypatch,tmp_path,model,{'reference':report['reference']},saving=False,operation='read-desk')
    read=tmp_path/'read'
    assert host.main(['build','--out',str(read)])==0
    assert (read/'edition.json').read_bytes()==doc and (read/'news-brief.md').read_bytes()==body
    assert model.writes==1 and 0<fresh._requests<512
    assert json.loads((read/'desk-report.json').read_bytes())['write']=='not-requested'
    assert '此版本已保管' in (read/'news-desk.html').read_text()


@pytest.mark.parametrize('saving',[True,False])
def test_public_feedback_needs_its_own_permission_before_any_output(sources,monkeypatch,tmp_path,saving):
    model,store,api,inputs,plan=setup_desk(sources,monkeypatch,tmp_path,saving=saving)
    doc,logical=desk.build_edition(store.read_source,ResearchArtifacts(store),plan)
    plan['attention']=[choice(desk.entry_id(logical['entries'][0]))]
    inputs['plan']=encoded(plan).decode()
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    out=tmp_path/'no-public-feedback'
    assert host.main(['build','--out',str(out)])==2
    assert not out.exists() and api._requests==0 and model.writes==0


def test_original_writer_unknown_write_reconciles_same_artifact_without_second_write(sources,monkeypatch,tmp_path):
    model,_,_,_,_=setup_desk(sources,monkeypatch,tmp_path)
    model.after_write=lambda _:(_ for _ in ()).throw(TimeoutError('synthetic lost ACK'))
    out=tmp_path/'unknown';assert host.main(['build','--out',str(out)])==2
    report=json.loads((out/'result/report.json').read_bytes())
    assert report['status']=='unknown-write' and report['write']=='unconfirmed' and model.writes==1
    original=json.loads((out/'result/inputs.json').read_bytes());original['operation']='reconcile-artifact'
    model.after_write=None
    result=research_runtime.run_command(model.binding,api=transport(model),inputs=original,code_sha='a'*40,
                                        output=tmp_path/'reconciled',run_id='124',attempt='1')
    assert result['write']=='reconciled-without-write' and model.writes==1


def test_end_binding_failure_prevents_export(sources,monkeypatch,tmp_path):
    model,_,_,_,_=setup_desk(sources,monkeypatch,tmp_path)
    original=delivery.build_edition
    def changed(*a,**k):
        result=original(*a,**k);model.meta['private']=True;return result
    monkeypatch.setattr(delivery,'build_edition',changed)
    out=tmp_path/'bad-binding';assert host.main(['build','--out',str(out)])==2
    assert not (out/'edition.json').exists() and not (out/'news-desk.html').exists() and model.writes==0


def test_new_host_options_keep_same_workflow_read_write_separation():
    path=Path(__file__).parents[1]/'.github/workflows/news-rolling.yml'
    text=path.read_text()
    assert 'options: [build, read, desk, read-desk]' in text
    assert "inputs.operation != 'read' && inputs.operation != 'read-desk'" in text
    assert 'public_feedback_custody_confirmed' in text
    assert 'schedule:' not in text and 'contents: write' in text


def exercise_installed_news(outside, python, env, store):
    """Extend the existing single cold-wheel consumer; no second installation."""
    import subprocess
    script = r'''
import json,sys
from dataclasses import asdict
from pathlib import Path
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import SourceInput,encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.observations.news_windows import newsnow_window,combine
from decision_kernel.application.news_rolling_index import rolling_index
from decision_kernel.application.news_delivery import build_edition,open_edition,page_model,entry_id
from decision_kernel.interfaces.news_page import render_page
store=VersionedStore(GitStore(Path(sys.argv[1])))
def save(name,value):
    return store.put_source(name,SourceInput(name,'content','Synthetic news','synthetic:news',text=encoded(value).decode()))
raw=encoded({'status':'success','id':'cls','items':[{'id':'one','title':'Synthetic observation','url':'https://www.cls.cn/detail/123'}]})
batch=save('installed-news-batch',combine([newsnow_window(raw,'cls',observed_at='2026-10-16T10:00:00Z')]))
window=save('installed-news-window',rolling_index(store.read_source,[batch],as_of='2026-10-16T12:00:00Z'))
artifacts=ResearchArtifacts(store)
plan={'window':asdict(window),'cutoff':'2026-10-16T12:00:00Z','kind':'evening'}
doc,logical=build_edition(store.read_source,artifacts,plan)
key=entry_id(logical['entries'][0])
plan['attention']=[{'entry_id':key,'action':'focus','reason':'Synthetic test','question':'What should be verified?','review_at':None}]
doc,logical=build_edition(store.read_source,artifacts,plan)
ref=artifacts.put('installed-news-edition',ArtifactInput.from_bytes('installed-news-edition','edition.json','application/json',encoded(doc)))
fresh=VersionedStore(GitStore(Path(sys.argv[1])))
actual,rows=open_edition(fresh.read_source,ResearchArtifacts(fresh),asdict(ref))
assert encoded(actual)==encoded(doc) and rows==logical
assert actual['quick_candidates'][0]['research_started'] is False
page=render_page(page_model(actual,rows,ref))
assert '新闻工作台' in page and '此版本已保管' in page
Path('installed-news-desk.html').write_text(page,encoding='utf-8')
print(json.dumps({'status':'installed-news-consumer-passed','entries':len(rows['entries'])}))
'''
    result = subprocess.run([str(python), '-I', '-c', script, str(store)], cwd=outside,
                            env=env, check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {'status': 'installed-news-consumer-passed', 'entries': 1}

    from test_brief_delivery_host import exercise_installed_research_brief
    exercise_installed_research_brief(outside, python, env, store)

    from test_reading_room_integration import exercise_installed_room
    exercise_installed_room(outside, python, env, store)

    from test_reading_materials import exercise_installed_materials
    exercise_installed_materials(outside, python, env, store)

    from test_research_review import exercise_installed_review
    exercise_installed_review(outside, python, env, store)

    from test_industry_room import exercise_installed_industry_room
    exercise_installed_industry_room(outside, python, env, store)

    from test_market_room import exercise_installed_market_room
    exercise_installed_market_room(outside, python, env, store)

    from test_recovery_commands import exercise_installed_recovery
    exercise_installed_recovery(outside, python, env, store)
