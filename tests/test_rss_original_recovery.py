"""Original large RSS intent recovery: fixed native archive, no publisher refetch."""
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

from decision_kernel.domain.records import RecordError, encoded, digest
from decision_kernel.application import rss_intake as rss
from decision_kernel.interfaces import rss_recovery as recover, rss_cli as host
from decision_kernel.interfaces.news_custody import verify_native
from test_rss_failure_custody import baseline, attempt, native, model

SUB = "original-rss-intent"
SHA = "a" * 40
RUN = "1234"


def archive(parts):
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in parts.items():
            z.writestr(name, raw)
    return buf.getvalue()


def fixture(*, state="conflict", failed=False):
    value = attempt(batch_id="rss-run-1234") if failed else baseline(batch_id="rss-run-1234")
    source = rss.source_input(value, SUB)
    inputs = {"operation": "capture", "code_sha": SHA, "submission_id": SUB, "expected_head": "b"*40,
              "public_metadata_custody_confirmed": "true", "public_original_custody_confirmed": "false",
              "plan": json.dumps({"feed_id": "nbs-releases", "bootstrap": True, "retain_original": False})}
    report = {"operation": "capture", "run_id": RUN, "run_attempt": 1, "code_sha": SHA,
              "status": "failed", "write": "not-attempted", "error_code": "RSS_PLANNED_HEAD_CHANGED"}
    if state == "unknown": report.update(status="unknown-write-reconcile-original", write="unknown")
    if state == "published": report.update(status="completed", write="published")
    parts = {"inputs.json": encoded(inputs), "report.json": encoded(report), "capture.json": encoded(value),
             "source.json": encoded(asdict(source)), "reading.json": encoded(rss.read_capture(value))}
    run = {"id": 1234, "repository": {"id": 1420041744}, "head_repository": {"id": 1420041744},
           "path": ".github/workflows/rss-intake.yml", "head_branch": "main", "event": "workflow_dispatch",
           "run_attempt": 1, "status": "completed", "conclusion": "failure", "head_sha": SHA}
    artifact = {"id": 222, "name": "dos2-rss-1234-1", "expired": False,
        "workflow_run": {"id":1234,"head_sha":SHA,"repository_id":1420041744,"head_repository_id":1420041744}}
    chosen = {"source_run": RUN, "artifact_id":"222"}
    return repack(parts, run, artifact, chosen)


def repack(parts, run, artifact, chosen):
    raw = archive(parts)
    artifact = {**artifact,"size_in_bytes":len(raw),"digest":"sha256:"+digest(raw)}
    chosen = {**chosen,"artifact_sha256":digest(raw)}
    return parts,run,artifact,chosen,raw


def load(data, **kwargs):
    parts, run, artifact, chosen, raw = data
    return recover.source_from_archive(raw,run,artifact,chosen,submission_id=kwargs.pop("submission_id",SUB),**kwargs)


def test_preserves_original_exact_source_capture_and_reading_without_rebuilding_title():
    data = fixture()
    source,value,report=load(data)
    assert encoded(asdict(source)) == data[0]["source.json"]
    assert encoded(value) == data[0]["capture.json"]
    assert source.source_id == "rss-"+SUB and report["write"] == "not-attempted"


def test_shared_native_validator_keeps_news_profile_as_default():
    data=fixture();_,run,artifact,chosen,_=data
    with pytest.raises(RecordError,match="NEWS_NATIVE_RUN_MISMATCH"):
        verify_native(run,artifact,chosen)
    recover.native_metadata(run,artifact,chosen)
    with pytest.raises(RecordError): recover.selection({**chosen,"workflow_path":"other.yml"})


@pytest.mark.parametrize("field,value",[("source_run","../123"),("artifact_id","222;echo secret"),
    ("artifact_sha256","b"*63),("source_run",1234)])
def test_untrusted_selector_cannot_become_a_path_or_shell_fragment(field,value):
    chosen=fixture()[3];chosen[field]=value
    with pytest.raises(RecordError): recover.selection(chosen)


@pytest.mark.parametrize("field,value",[("head_branch","other"),("run_attempt",2),("status","in_progress"),
    ("path",".github/workflows/research-runtime.yml"),("event","push")])
def test_wrong_native_run_never_qualifies(field,value):
    data=fixture();data[1][field]=value
    with pytest.raises(RecordError): load(data)


def test_native_archive_expired_corrupt_or_missing_original_file_rejected():
    data=fixture();data[2]["expired"]=True
    with pytest.raises(RecordError): load(data)
    data=fixture();data=(*data[:-1],data[-1]+b"corrupt")
    with pytest.raises(RecordError):load(data)
    parts,run,artifact,chosen,_=fixture();parts["../escape.json"]=parts.pop("source.json")
    with pytest.raises(RecordError):load(repack(parts,run,artifact,chosen))


@pytest.mark.parametrize("member,field,value",[
 ("inputs.json","submission_id","different"),("inputs.json","code_sha","b"*40),
 ("inputs.json","public_metadata_custody_confirmed","false"),("report.json","operation","recover"),
 ("report.json","run_id","9999"),("source.json","title","rewritten headline"),
 ("reading.json","selected_count",999)])
def test_original_fields_cannot_be_rewritten_even_under_valid_new_zip_checksum(member,field,value):
    parts,run,artifact,chosen,_=fixture();d=json.loads(parts[member]);d[field]=value;parts[member]=encoded(d)
    with pytest.raises(RecordError):load(repack(parts,run,artifact,chosen))


def test_no_raw_xml_rights_expansion_and_no_plan_feed_replacement():
    for changes in [{"retain_original":True,"rights_basis":"assertion"},{"feed_id":"nbs-interpretations"}]:
        parts,run,artifact,chosen,_=fixture();d=json.loads(parts['inputs.json']);p=json.loads(d['plan']);p.update(changes)
        d['plan']=json.dumps(p);parts['inputs.json']=encoded(d)
        with pytest.raises(RecordError):load(repack(parts,run,artifact,chosen))


@pytest.mark.parametrize("state",["unknown","published"])
def test_unknown_or_already_published_original_can_only_reconcile_not_new_save(state):
    data=fixture(state=state)
    with pytest.raises(RecordError,match="RSS_RECOVERY_REQUIRES_UNWRITTEN_CONFLICT"):load(data)
    source,value,report=load(data,reconcile=True)
    assert encoded(asdict(source))==data[0]['source.json']


def native_recovery(monkeypatch,tmp_path,model,data,*,operation="recover"):
    inputs,api=native(monkeypatch,tmp_path,model,submission=SUB)
    inputs.update(operation=operation,plan=encoded(data[3]).decode(),expected_head=model.git.head() if operation=='recover' else '')
    (tmp_path/'event.json').write_bytes(encoded({'inputs':inputs}))
    monkeypatch.setenv('DOS2_RSS_MODE',operation);monkeypatch.setenv('RUNNER_TEMP',str(tmp_path))
    p=tmp_path/'rss-recovery';p.mkdir(exist_ok=True)
    (p/'run.json').write_bytes(encoded(data[1]));(p/'artifact.json').write_bytes(encoded(data[2]));(p/'source.zip').write_bytes(data[4])
    monkeypatch.setattr(host,'fetch_feed',lambda _:pytest.fail('never fetch publisher during recovery'))
    return api


def test_native_conflict_recovery_and_separate_readonly_reconciliation(model,tmp_path,monkeypatch):
    data=fixture();api=native_recovery(monkeypatch,tmp_path,model,data)
    out=tmp_path/'recovered';assert host.main(['run','--out',str(out)])==0
    report=json.loads((out/'report.json').read_bytes())
    assert report['write']=='published' and model.writes==1 and api._requests<512
    assert (out/'source.json').read_bytes()==data[0]['source.json']
    assert (out/'capture.json').read_bytes()==data[0]['capture.json']
    native_recovery(monkeypatch,tmp_path,model,data,operation='reconcile')
    cold=tmp_path/'reconciled';assert host.main(['run','--out',str(cold)])==0
    again=json.loads((cold/'report.json').read_bytes())
    assert again['write']=='reconciled-without-write' and again['reference']==report['reference']
    assert model.writes==1 and (cold/'capture.json').read_bytes()==data[0]['capture.json']


def test_native_missing_reconciliation_cannot_create_a_record(model,tmp_path,monkeypatch):
    native_recovery(monkeypatch,tmp_path,model,fixture(),operation='reconcile')
    out=tmp_path/'missing';assert host.main(['run','--out',str(out)])==2
    report=json.loads((out/'report.json').read_bytes())
    assert report['write']=='not-requested' and 'reference' not in report and model.writes==0


def test_native_unknown_recovery_write_reconciles_same_original_once(model,tmp_path,monkeypatch):
    data=fixture();native_recovery(monkeypatch,tmp_path,model,data)
    model.after_write=lambda _: (_ for _ in ()).throw(OSError('lost ACK'))
    out=tmp_path/'lost';assert host.main(['run','--out',str(out)])==2
    report=json.loads((out/'report.json').read_bytes());assert report['write']=='unknown' and model.writes==1
    model.after_write=None
    native_recovery(monkeypatch,tmp_path,model,data,operation='reconcile')
    cold=tmp_path/'found';assert host.main(['run','--out',str(cold)])==0
    assert json.loads((cold/'report.json').read_bytes())['write']=='reconciled-without-write'
    assert model.writes==1


def test_recovered_failed_capture_stays_nonzero_even_when_receipt_saved(model,tmp_path,monkeypatch):
    data=fixture(failed=True);native_recovery(monkeypatch,tmp_path,model,data)
    out=tmp_path/'failure';assert host.main(['run','--out',str(out)])==2
    report=json.loads((out/'report.json').read_bytes())
    assert report['status']=='source-failed-receipt-saved' and report['write']=='published'
    assert report['source_outcome']=='unavailable' and report['observations'] is None and model.writes==1


def test_existing_workflow_maintains_separate_read_write_jobs_and_fixed_archive_preflight():
    import yaml
    root=Path(__file__).resolve().parents[1]
    flow=yaml.load((root/'.github/workflows/rss-intake.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(flow['on'])=={'workflow_dispatch'} and flow['permissions']=={}
    assert flow['on']['workflow_dispatch']['inputs']['operation']['options']==['read','capture','recover','reconcile']
    r,w=flow['jobs']['read'],flow['jobs']['capture']
    assert r['permissions']=={'contents':'read','actions':'read'}
    assert w['permissions']=={'contents':'write','actions':'read'}
    assert "inputs.operation == 'reconcile'" in r['if'] and "inputs.operation == 'recover'" in w['if']
    for job in (r,w):
        step=next(s for s in job['steps'] if s.get('name','').startswith('Fetch only'))
        assert step['run'].index('preflight --root')<step['run'].index('/zip"')
        assert 'rss_recovery select' in step['run'] and 'curl' not in step['run']
