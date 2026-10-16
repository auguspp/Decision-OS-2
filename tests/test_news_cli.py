"""Manual news CLI and native workflow guardrails, not production source proof."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path

import pytest
import yaml

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.news_capture import fetch_newsnow, NEWSNOW_ROOT
from decision_kernel.interfaces.news_cli import main
from decision_kernel.observations.news_windows import combine, newsnow_window
from test_news_windows import NOW, newsnow


class Response(BytesIO):
    status = 200
    def __init__(self, url, raw, headers=None):
        super().__init__(raw)
        self.url = url
        self.headers = headers or {}
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def open(self, request, timeout):
        self.calls.append(request)
        return self.response


def test_newsnow_reads_one_fixed_loopback_url_and_rejects_redirect_response():
    url = NEWSNOW_ROOT + "?id=cls&latest="
    opener = Opener(Response(url, newsnow(), {"Content-Length": str(len(newsnow()))}))
    assert fetch_newsnow("cls", opener=opener) == newsnow()
    assert len(opener.calls) == 1
    assert opener.calls[0].full_url == url
    with pytest.raises(RecordError):
        fetch_newsnow("evil", opener=opener)
    with pytest.raises(RecordError):
        fetch_newsnow("cls", opener=Opener(Response("https://evil.example/", newsnow())))


def test_newsnow_fails_closed_on_size_and_encoding():
    url = NEWSNOW_ROOT + "?id=cls&latest="
    for headers in ({"Content-Length": "999999999"},
                    {"Content-Encoding": "gzip"}):
        with pytest.raises(RecordError):
            fetch_newsnow("cls", opener=Opener(Response(url, newsnow(), headers)))


def test_offline_read_and_locator_export_are_explicit_and_create_only(tmp_path, capsys):
    path = tmp_path / "news-window.json"
    reading = combine([newsnow_window(newsnow(), "cls", observed_at=NOW)])
    path.write_bytes(encoded(reading))
    assert main(["read", "--window", str(path), "--limit", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "observation-only" and data["visible"] == 1
    assert data["complete_news_coverage"] is False
    assert main(["read", "--window", str(path), "--limit", "0"]) == 2
    out = tmp_path / "locators.json"
    assert main(["export-locators", "--window", str(path), "--output", str(out)]) == 0
    saved = json.loads(out.read_bytes())
    assert saved["schema"] == "dos2-news-locators-v1"
    assert saved["write_performed"] is False and saved["rights_verified"] is False
    assert saved["items"][0]["text"] is None
    assert main(["export-locators", "--window", str(path), "--output", str(out)]) == 2


def test_no_publication_undeclared_source_capture_or_automatic_schedule():
    workflow = yaml.load((Path(__file__).parents[1] / ".github/workflows/news-intake.yml").read_text(),
                         Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["capture"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    steps = job["steps"]
    assert "public_metadata_capture_confirmed" in job["if"]
    assert any("docker run" in x.get("run", "") for x in steps)
    assert any("news_cli capture-newsnow" in x.get("run", "") for x in steps)
    assert any("news_cli capture-relay" in x.get("run", "") for x in steps)
    assert all("continue-on-error" not in x for x in steps)
    for s in steps:
        if "TUSHARE_PROXY_API_KEY" in s.get("env", {}):
            assert s["name"] == "Relay fixed-host reviewed news capture"
    assert not any("TUSHARE_PROXY_API_KEY" in x.get("env", {}) for x in steps
                   if x.get("name", "").startswith(("Preflight", "NewsNow")))
