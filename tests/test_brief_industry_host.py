"""Existing native news host/writer over synthetic Git, not live source acceptance."""
from dataclasses import asdict
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from decision_kernel.domain.records import encoded
from decision_kernel.interfaces import news_rolling_cli as host, research_runtime
from test_news_read_budget import sources, setup, transport
from test_brief_delivery_host import mixed_setup
from test_industry_reading import prepare
from test_brief_industry import STAMP, item


def arrange(sources, monkeypatch, tmp_path, saving=True):
    model, api, inputs, plan = mixed_setup(sources, monkeypatch, tmp_path, saving)
    with patch('decision_kernel.application.versioned_store._now', lambda: STAMP):
        root, _ = prepare(sources[1])
    plan['industry'] = {'items': [item(root)]}
    inputs['plan'] = encoded(plan).decode()
    inputs['public_sources_custody_confirmed'] = 'true'
    inputs['expected_head'] = model.git.head() if saving else ''
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    return model, api, inputs, plan


def test_native_industry_and_research_share_one_saved_artifact_and_cold_read(sources, monkeypatch, tmp_path):
    model, api, _, _ = arrange(sources, monkeypatch, tmp_path)
    out = tmp_path/'saved'
    assert host.main(['build','--out',str(out)]) == 0
    report = json.loads((out/'desk-report.json').read_bytes())
    assert report['write'] == 'published' and report['industry_included'] == report['research_included'] == 1
    assert model.writes == 1 and 0 < api._requests < 512
    new_api, inputs = setup(monkeypatch, tmp_path, model, {'reference':report['reference']}, saving=False, operation='read-desk')
    inputs.update(public_sources_custody_confirmed='true', public_research_custody_confirmed='true')
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    read = tmp_path/'fresh'
    assert host.main(['build','--out',str(read)]) == 0
    for name in ('edition.json','news-brief.md'):
        assert (read/name).read_bytes() == (out/name).read_bytes()
    doc = json.loads((read/'edition.json').read_bytes())
    assert doc['schema'] == 'dos2-news-edition-v3'
    assert '产业观察的本期采用' in doc['brief_markdown'] and '原作者的经营判断' in (read/'news-desk.html').read_text()
    assert model.writes == 1 and 0 < new_api._requests < 512


@pytest.mark.parametrize('saving',[False,True])
@pytest.mark.parametrize('confirmed',[None,'false',True,'TRUE'])
def test_source_permission_cannot_be_inherited_before_network_or_output(sources, monkeypatch, tmp_path, saving, confirmed):
    model, api, inputs, _ = arrange(sources, monkeypatch, tmp_path, saving)
    inputs.update(public_sources_custody_confirmed=confirmed, public_research_custody_confirmed='true',
                  public_feedback_custody_confirmed='true', public_metadata_custody_confirmed='true')
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    out = tmp_path/'denied'
    assert host.main(['build','--out',str(out)]) == 2
    assert api._requests == model.writes == 0 and not out.exists()


def test_saved_industry_brief_without_source_permission_never_exports_content(sources, monkeypatch, tmp_path):
    model, _, _, _ = arrange(sources, monkeypatch, tmp_path)
    out = tmp_path/'saved'
    assert host.main(['build','--out',str(out)]) == 0
    ref = json.loads((out/'desk-report.json').read_bytes())['reference']
    _, inputs = setup(monkeypatch,tmp_path,model,{'reference':ref},saving=False,operation='read-desk')
    inputs['public_research_custody_confirmed'] = 'true'
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    denied=tmp_path/'reopen-denied'
    assert host.main(['build','--out',str(denied)]) == 2 and model.writes == 1
    assert sorted(p.name for p in denied.iterdir()) == ['desk-report.json','plan.json']
    assert json.loads((denied/'desk-report.json').read_bytes())['status'] == 'failed-see-original-receipt'


def test_unknown_ack_uses_original_artifact_reconciliation_without_republish(sources, monkeypatch, tmp_path):
    model, _, _, _ = arrange(sources,monkeypatch,tmp_path)
    model.after_write = lambda _: (_ for _ in ()).throw(TimeoutError('synthetic lost ACK'))
    out=tmp_path/'unknown'
    assert host.main(['build','--out',str(out)]) == 2 and model.writes == 1
    report=json.loads((out/'result/report.json').read_bytes())
    assert report['status'] == 'unknown-write'
    original=json.loads((out/'result/inputs.json').read_bytes())
    original['operation']='reconcile-artifact';model.after_write=None
    found=research_runtime.run_command(model.binding,api=transport(model),inputs=original,code_sha='a'*40,
        output=tmp_path/'reconciled',run_id='124',attempt='1')
    assert found['write']=='reconciled-without-write' and model.writes==1


def test_existing_workflow_has_one_separate_flag_without_new_job_or_trigger():
    workflow=yaml.safe_load((Path(__file__).parents[1]/'.github/workflows/news-rolling.yml').read_text())
    trigger=workflow.get('on',workflow.get(True))
    inputs=trigger['workflow_dispatch']['inputs']
    assert len(inputs)<=10 and inputs['public_sources_custody_confirmed']['default'] is False
    assert inputs['public_sources_custody_confirmed']['type']=='boolean'
    assert set(trigger)=={'workflow_dispatch'}
    assert set(workflow['jobs'])=={'build','save'}
    assert workflow['jobs']['build']['permissions']=={'contents':'read','actions':'read'}
    assert workflow['jobs']['save']['permissions']=={'contents':'write'}
