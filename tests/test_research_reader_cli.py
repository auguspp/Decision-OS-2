"""CLI wiring to shared authenticated storage; HTTP responses are synthetic."""
import argparse
from dataclasses import asdict, replace
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RevisionInput, SourceInput
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import store_cli
from test_github_store import GitAPI


def arguments(*args):
    parser = argparse.ArgumentParser()
    store_cli.configure(parser.add_subparsers(dest="command"))
    return parser.parse_args(["store", *args])


@pytest.mark.parametrize("selector", ["case", "reference", "cursor"])
def test_resume_uses_existing_read_only_binding(tmp_path, monkeypatch, capsys, selector):
    api = GitAPI(tmp_path / "remote.git")
    store = VersionedStore(GitHubStore(api.binding, api))
    ref = store.put_source("source", SourceInput("one", "content", "合成来源", "synthetic:one", "data"))
    revision = store.submit_revision("research", RevisionInput(
        "case", "r1", "full", "合成研究", "合成委托", "合成方法", (ref, ref)))
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(asdict(replace(api.binding, write_enabled=False))), encoding="utf-8")
    monkeypatch.setattr(store_cli.GitHubAPI, "from_environment", classmethod(lambda cls: api))
    writes = api.writes
    prefix = ["--binding", str(binding), "resume-research"]
    if selector == "case":
        options = ["--case-id", "case"]
    elif selector == "reference":
        path = tmp_path / "ref.json"
        path.write_text(json.dumps(asdict(revision)), encoding="utf-8")
        options = ["--ref", str(path)]
    else:
        assert store_cli.execute(arguments(*prefix, "--case-id", "case", "--source-limit", "1")) == 0
        first = json.loads(capsys.readouterr().out)
        path = tmp_path / "cursor.json"
        path.write_text(json.dumps(first["next_cursor"]), encoding="utf-8")
        options = ["--cursor", str(path)]
    assert store_cli.execute(arguments(*prefix, *options)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["research"]["reference"] == asdict(revision)
    assert result["source_page"]["offset"] == (1 if selector == "cursor" else 0)
    assert api.writes == writes


def test_resume_cursor_loader_reports_malformed_json(tmp_path, monkeypatch, capsys):
    api = GitAPI(tmp_path / "remote.git")
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps(asdict(api.binding)), encoding="utf-8")
    cursor = tmp_path / "cursor.json"
    cursor.write_text('{"schema":1,"schema":2}', encoding="utf-8")
    monkeypatch.setattr(store_cli.GitHubAPI, "from_environment", classmethod(lambda cls: api))
    assert store_cli.execute(arguments("--binding", str(binding), "resume-research", "--cursor", str(cursor))) == 2
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error"] == "DUPLICATE_JSON_KEY"


def test_resume_selectors_are_required_and_exclusive():
    for options in ([], ["--case-id", "case", "--ref", "ref.json"]):
        with pytest.raises(SystemExit) as exc:
            arguments("--path", "unused.git", "resume-research", *options)
        assert exc.value.code == 2
