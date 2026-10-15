"""Actual consumer of report directory, no external requests and no PDF reads."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from decision_kernel.interfaces.broker_report_catalog_cli import main
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore


def input_path(tmp_path, *, rows=None):
    sample = {"captured_at":"2026-10-15T08:00:00+00:00",
              "pages":[{"code":0,"pageNo":1,"hits":1,"TotalPage":1,"data":rows if rows is not None else
              [{"infoCode":"REPORT42","title":"研报目录样例","publishDate":"2026-10-11 08:30:00",
                "orgSName":"研究机构A","column":"002001002-001"}]}]}
    p=tmp_path/"saved-pages.json"
    p.write_text(json.dumps(sample,ensure_ascii=False),encoding="utf-8")
    return p


def args(path):return ["--kind","strategy","--begin","2026-10-01","--end","2026-10-12","--offline",str(path)]


def test_cli_readonly_directory_and_keyword_no_side_effects(tmp_path,capsys):
    file=input_path(tmp_path)
    assert main(args(file)+["--keyword","研报"])==0
    data=json.loads(capsys.readouterr().out)
    assert data["matched"]==1 and data["provider_rows"]==1
    assert data["report_pdf_read"] is False and data["source_mode"]=="offline-unverified"
    assert data["items"][0]["detail_url"].startswith("https://data.eastmoney.com/report/")
    assert list(tmp_path.iterdir())==[file]


def test_cli_explicit_synthetic_save_and_original_version_read(tmp_path,capsys):
    f=input_path(tmp_path)
    store=tmp_path/"store.git"
    GitStore.init_synthetic(store)
    assert main(args(f)+["--save","--batch-id","b42","--local-synthetic-store",str(store)])==0
    raw=json.loads(capsys.readouterr().out)
    assert raw["storage"]["complete"] and raw["storage"]["saved_count"]==1
    from decision_kernel.domain.records import SourceRef
    ref=SourceRef.from_dict(raw["storage"]["saved"][0]["reference"])
    view=VersionedStore(GitStore(store)).read_source(ref)
    assert view["data"] is None and view["original_verified"] is False
    assert "BROKER_REPORT_DISCOVERY_ONLY" in " ".join(view["limitations"])
    assert main(args(f)+["--save","--batch-id","b42","--local-synthetic-store",str(store)])==0
    repeated=json.loads(capsys.readouterr().out)
    assert repeated["storage"]["saved"][0]["reference"]==raw["storage"]["saved"][0]["reference"]
    assert VersionedStore(GitStore(store)).search("")["total"]==1


def test_cli_no_implicit_save_or_credential_guessing(tmp_path,capsys):
    f=input_path(tmp_path)
    assert main(args(f)+["--save"])==2
    assert "BROKER_SAVE_NEEDS_WRITER_AND_BATCH" in capsys.readouterr().err
    assert main(args(f)+["--batch-id","wrong"])==2
    assert "BROKER_STORE_WITHOUT_SAVE" in capsys.readouterr().err


def test_cli_rejects_duplicate_json_and_invalid_data_before_creating_store(tmp_path,capsys):
    file=tmp_path/"ambiguous.json"
    file.write_text('{"pages":[],"pages":[],"captured_at":"2026-10-15T09:00:00+00:00"}')
    assert main(args(file))==2
    assert "BROKER_INPUT_INVALID" in capsys.readouterr().err
    missing=tmp_path/"missing.git"
    input_path(tmp_path,rows=[{"publishDate":"2026-10-11 08:30:00","title":"Missing report id"}])
    assert main(args(tmp_path/"saved-pages.json")+["--save","--batch-id","one","--local-synthetic-store",str(missing)])==2
    assert not missing.exists()
    assert "BROKER_REPORT_ID_REQUIRED" in capsys.readouterr().err


def test_cli_explicit_live_hook_can_only_be_invoked_by_live_flag(monkeypatch,tmp_path,capsys):
    f=input_path(tmp_path)
    def unexpected(*a,**k):raise AssertionError("offline touched network")
    monkeypatch.setattr("decision_kernel.interfaces.broker_report_catalog_cli.fetch_catalog",unexpected)
    assert main(args(f))==0
    capsys.readouterr()
    with pytest.raises(AssertionError,match="offline touched network"):
        main(["--kind","industry","--begin","2026-10-01","--end","2026-10-12","--live"])


def test_cli_subprocess_outside_repo_using_pythonpath(tmp_path):
    f=input_path(tmp_path)
    import os
    root=__import__("pathlib").Path(__file__).resolve().parents[1]
    cmd=[sys.executable,"-m","decision_kernel.interfaces.broker_report_catalog_cli",*args(f)]
    run=subprocess.run(cmd,cwd=tmp_path,env={**os.environ,"PYTHONPATH":str(root/"src")},
                       capture_output=True,text=True,timeout=15,check=False)
    assert run.returncode==0,run.stderr
    result=json.loads(run.stdout)
    assert result["matched"]==1 and result["report_pdf_read"] is False
