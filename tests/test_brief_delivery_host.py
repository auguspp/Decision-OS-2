"""Mixed Brief through the unchanged native host/CAS, with synthetic wire only."""
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.domain.records import encoded
from decision_kernel.interfaces import news_rolling_cli as host, research_runtime
from test_news_read_budget import sources, setup, transport
from test_news_rolling import save
from test_brief_workpaper import structured_revision
from test_news_delivery import END


def mixed_setup(sources, monkeypatch, tmp_path, saving=True):
    model, store, _, _, index = sources
    item = structured_revision(store, monkeypatch)
    window = save(store, 'mixed-index', index)
    plan = {'window':asdict(window), 'cutoff':END, 'kind':'evening', 'research':{'items':[item]}}
    api, inputs = setup(monkeypatch, tmp_path, model, plan, saving=saving, operation='desk')
    inputs['public_research_custody_confirmed'] = 'true'
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    return model, api, inputs, plan


def test_native_mixed_save_and_independent_read_preserve_actual_body(sources, monkeypatch, tmp_path):
    model, api, _, _ = mixed_setup(sources, monkeypatch, tmp_path)
    out = tmp_path/'mixed'
    assert host.main(['build','--out',str(out)]) == 0
    report = json.loads((out/'desk-report.json').read_bytes())
    assert report['research_included'] == 1 and report['write'] == 'published'
    assert model.writes == 1 and 0 < api._requests < 512
    fresh, inputs = setup(monkeypatch, tmp_path, model, {'reference':report['reference']}, saving=False, operation='read-desk')
    inputs['public_research_custody_confirmed'] = 'true'
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    read = tmp_path/'reopened'
    assert host.main(['build','--out',str(read)]) == 0
    assert (read/'edition.json').read_bytes() == (out/'edition.json').read_bytes()
    assert (read/'news-brief.md').read_bytes() == (out/'news-brief.md').read_bytes()
    assert '原作者的经营判断' in (read/'news-desk.html').read_text()
    assert model.writes == 1 and 0 < fresh._requests < 512


@pytest.mark.parametrize('saving', [True, False])
@pytest.mark.parametrize('confirmed', [None, 'false', True])
def test_research_rights_are_separate_before_network_and_outputs(sources, monkeypatch, tmp_path, saving, confirmed):
    model, api, inputs, _ = mixed_setup(sources, monkeypatch, tmp_path, saving)
    inputs['public_research_custody_confirmed'] = confirmed
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    out = tmp_path/'denied'
    assert host.main(['build','--out',str(out)]) == 2
    assert not out.exists() and model.writes == 0 and api._requests == 0


def test_unconfirmed_saved_agenda_cannot_inherit_research_or_news_rights(sources, monkeypatch, tmp_path):
    model, api, inputs, plan = mixed_setup(sources, monkeypatch, tmp_path, False)
    # Validly shaped but unread: publication must be rejected before asking the store.
    plan['research']['agenda'] = {'repository':host.REPOSITORY, 'commit':'a'*40,
        'path':'research/artifacts/agenda/'+'b'*64+'/manifest.json', 'sha256':'c'*64}
    inputs['plan'] = encoded(plan).decode()
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    out = tmp_path/'feedback-denied'
    assert host.main(['build','--out',str(out)]) == 2
    assert not out.exists() and api._requests == 0 and model.writes == 0


def test_mixed_unknown_ack_reconciles_original_payload_without_republishing(sources, monkeypatch, tmp_path):
    model, _, _, _ = mixed_setup(sources, monkeypatch, tmp_path)
    model.after_write = lambda _: (_ for _ in ()).throw(TimeoutError('synthetic lost ACK'))
    out = tmp_path/'unknown'
    assert host.main(['build','--out',str(out)]) == 2
    report = json.loads((out/'result/report.json').read_bytes())
    assert report['status'] == 'unknown-write' and model.writes == 1
    original = json.loads((out/'result/inputs.json').read_bytes())
    original['operation'] = 'reconcile-artifact'
    model.after_write = None
    found = research_runtime.run_command(model.binding, api=transport(model), inputs=original,
        code_sha='a'*40, output=tmp_path/'reconciled', run_id='124', attempt='1')
    assert found['write'] == 'reconciled-without-write' and model.writes == 1


def test_workflow_and_existing_page_keep_one_host_and_original_export():
    root = Path(__file__).parents[1]
    workflow = (root/'.github/workflows/news-rolling.yml').read_text()
    assert 'public_research_custody_confirmed:' in workflow
    assert 'schedule:' not in workflow and "inputs.operation != 'read-desk'" in workflow
    page = (root/'src/decision_kernel/interfaces/news_page.py').read_text()
    assert 'const plan={...E.plan,' in page  # Already preserves the new research plan.
    assert "$('brief-body').textContent=E.brief_markdown" in page
    assert "save('news-brief.md',E.brief_markdown" in page


def exercise_installed_research_brief(outside, python, env, store):
    """Called by the pre-existing single wheel consumer, never a second install."""
    script = r'''
import json,sys
from dataclasses import asdict
from pathlib import Path
from decision_kernel.application import versioned_store
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import SourceInput,BlobRef,encoded
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.domain.research_work import ResearchRequest,ResearchProgress
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.news_rolling_index import rolling_index
from decision_kernel.observations.news_windows import newsnow_window,combine
from decision_kernel.application.brief_delivery import build_edition,open_edition,page_model
from decision_kernel.interfaces.news_page import render_page
versioned_store._now=lambda:'2026-10-16T10:00:00Z'
store=VersionedStore(GitStore(Path(sys.argv[1])))
def save(name,value):
 return store.put_source(name,SourceInput(name,'content','Synthetic','synthetic:brief',text=encoded(value).decode()))
raw=encoded({'status':'success','id':'cls','items':[{'id':'b','title':'Synthetic observation','url':'https://www.cls.cn/detail/456'}]})
batch=save('mixed-batch',combine([newsnow_window(raw,'cls',observed_at='2026-10-16T10:00:00Z')]))
window=save('mixed-window',rolling_index(store.read_source,[batch],as_of='2026-10-16T12:00:00Z'))
request=ResearchRequest.from_dict({'case_id':'mixed-research','kind':'full','mandate':'Synthetic mandate',
 'questions':[{'question_id':'q1','text':'Qualified answer?'}]})
progress=ResearchProgress.from_dict({'body':'Original finding.\nMaterial unresolved question.\n',
 'sources':[asdict(batch)],'findings':[{'question_id':'q1','status':'answered','finding':'Qualified','evidence':[0]}],
 'challenges':[{'question_id':'q1','argument':'Alternative','disposition':'unresolved','response':'Investigate','evidence':[0]}],
 'stop':'continue','stop_reason':'Unresolved challenge'})
r=BlobRef(**ResearchWorkflow(store).checkpoint(request,progress,submission_id='mixed-research',revision_id='r1')['reference'])
plan={'window':asdict(window),'cutoff':'2026-10-16T12:00:00Z','kind':'evening','research':{'items':[{'reference':asdict(r),'start':1,'end':2}]}}
artifacts=ResearchArtifacts(store)
doc,logical=build_edition(store.read_source,artifacts,plan)
assert len(doc['research']['included'])==1 and 'Original finding.' in doc['brief_markdown']
saved=artifacts.put('mixed-edition',ArtifactInput.from_bytes('mixed-edition','edition.json','application/json',encoded(doc)))
fresh=VersionedStore(GitStore(Path(sys.argv[1])))
opened,rows=open_edition(fresh.read_source,ResearchArtifacts(fresh),saved)
assert encoded(opened)==encoded(doc) and rows==logical and fresh.current('mixed-research')==r
page=render_page(page_model(opened,rows,saved))
assert 'Original finding.' in page and 'textContent=E.brief_markdown' in page
plan['baseline']=asdict(saved)
next_doc,_=build_edition(fresh.read_source,ResearchArtifacts(fresh),plan)
assert next_doc['research']['included']==[] and next_doc['research']['omissions']['previously-included-same-version']==1
assert next_doc['research']['research_executed'] is False
print(json.dumps({'status':'installed-mixed-brief-passed'}))
'''
    result = subprocess.run([str(python), '-I', '-c', script, str(store)], cwd=outside,
                            env=env, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr  # Preserve synthetic child diagnostics.
    assert json.loads(result.stdout) == {'status':'installed-mixed-brief-passed'}

    from test_brief_industry import exercise_installed_industry_brief
    exercise_installed_industry_brief(outside, python, env, store)

    from test_industry_adoption import exercise_installed_industry_adoption
    exercise_installed_industry_adoption(outside, python, env, store)
