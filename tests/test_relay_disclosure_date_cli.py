"""Installed-style explicit consumer and replay; no implicit Relay requests."""
from __future__ import annotations

import json,subprocess,sys,os
from pathlib import Path

import pytest

from decision_kernel.interfaces.relay_disclosure_date_cli import main
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import SourceRef

TICK="600598.SH"
PERIOD="20260930"


def previous_capture(tmp_path,*,body=None):
    if body is None:
        body={"api_name":"disclosure_date","code":0,"data":{
            "fields":["ts_code","ann_date","end_date","pre_date","actual_date","modify_date"],
            "items":[[TICK,"20260924",PERIOD,"20261029",None,None]]},"count":1}
    path=tmp_path/"relay_response.json"
    path.write_text(json.dumps({"captured_at":"2026-10-15T10:00:00+00:00","response":body}),encoding="utf-8")
    return path


def args(path):return ["--ts-code",TICK,"--end-date",PERIOD,"--offline",str(path)]


def test_offline_displays_schedule_without_writing_or_contacting_relay(tmp_path,capsys,monkeypatch):
    path=previous_capture(tmp_path)
    monkeypatch.setattr("decision_kernel.interfaces.relay_disclosure_date_cli.fetch_disclosure_dates",
                        lambda **_: (_ for _ in ()).throw(AssertionError("network")))
    assert main(args(path))==0
    out=json.loads(capsys.readouterr().out)
    assert out["rows"][0]["modify_date"]["presence"]=="null"
    assert out["issuer_pdf_read"] is False
    assert list(tmp_path.iterdir())==[path]


def test_save_requires_explicit_rights_and_existing_store(tmp_path,capsys):
    path=previous_capture(tmp_path)
    assert main(args(path)+["--save"])==2
    assert "RELAY_SAVE_SCOPE_AND_RIGHTS_REQUIRED" in capsys.readouterr().err
    assert main(args(path)+["--public-rights-confirmed"])==2
    assert "RELAY_UNAUTHORIZED_STORE_PARAMETERS" in capsys.readouterr().err
    root=tmp_path/"not-initiated.git"
    assert main(args(path)+["--save","--batch-id","legal-approved-one","--public-rights-confirmed",
                             "--local-synthetic-store",str(root)])==2
    assert not root.exists()


def test_installed_cli_synthetic_store_reopen(tmp_path,capsys):
    path=previous_capture(tmp_path)
    store=tmp_path/"prepared.git"
    GitStore.init_synthetic(store)
    opts=["--save","--batch-id","legal-approved-one","--public-rights-confirmed",
          "--local-synthetic-store",str(store)]
    assert main(args(path)+opts)==0
    out=json.loads(capsys.readouterr().out)
    assert out["storage"]["complete"]
    ref=SourceRef.from_dict(out["storage"]["reference"])
    view=VersionedStore(GitStore(store)).read_source(ref)
    assert view["original_verified"] is False and view["manifest"]["input"]["kind"]=="content"
    assert json.loads(view["data"])["rows"][0]["modify_date"]["presence"]=="null"
    assert main(args(path)+opts)==0
    repeat=json.loads(capsys.readouterr().out)
    assert repeat["storage"]["reference"]==out["storage"]["reference"]


def test_cli_missing_field_provenance_not_mistaken_for_null(tmp_path,capsys):
    x={"api_name":"disclosure_date","code":0,"data":{
        "fields":["ts_code","ann_date","end_date","pre_date","actual_date"],
        "items":[[TICK,"20260924",PERIOD,"20261029",None]]},"count":1}
    p=previous_capture(tmp_path,body=x)
    assert main(args(p))==0
    out=json.loads(capsys.readouterr().out)
    assert out["rows"][0]["modify_date"]["presence"]=="missing"


def test_cli_rejects_ambiguous_json_and_no_network(tmp_path,capsys):
    p=tmp_path/"dup.json"
    p.write_text('{"captured_at":"2026-10-15T10:00:00+00:00","response":{},"response":{}}')
    assert main(args(p))==2
    assert "RELAY_INPUT_INVALID" in capsys.readouterr().err


def test_cli_explicit_live_only(monkeypatch,tmp_path,capsys):
    p=previous_capture(tmp_path)
    def fail(*a,**k):raise AssertionError("explicit live called")
    monkeypatch.setattr("decision_kernel.interfaces.relay_disclosure_date_cli.fetch_disclosure_dates",fail)
    assert main(args(p))==0
    capsys.readouterr()
    with pytest.raises(AssertionError,match="explicit live called"):
        main(["--ts-code",TICK,"--end-date",PERIOD,"--live"])


def test_subprocess_outside_repo(tmp_path):
    p=previous_capture(tmp_path)
    root=Path(__file__).resolve().parents[1]
    run=subprocess.run([sys.executable,"-m","decision_kernel.interfaces.relay_disclosure_date_cli",*args(p)],
        cwd=tmp_path,capture_output=True,text=True,env={**os.environ,"PYTHONPATH":str(root/"src")},timeout=15)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)["observed_count"]==1
