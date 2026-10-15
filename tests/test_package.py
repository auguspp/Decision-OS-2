"""Small installed entry-point smoke; no source, research, storage or deployment."""
from importlib.metadata import PackageNotFoundError, version
import json
import subprocess
import sys

import pytest
from decision_kernel.interfaces import cli


def test_help(capsys):
    assert cli.main([]) == 0
    assert "installation" in capsys.readouterr().out


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "decision-os " + version("decision-os-2")


def test_info(capsys):
    assert cli.main(["info", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["distribution"] == "decision-os-2"
    assert data["version"] == version("decision-os-2")
    assert data["import_package"] == "decision_kernel"


@pytest.mark.parametrize("command", ["quick", "full", "collect", "decision", "deploy", "watch"])
def test_unimplemented_effectful_commands_are_not_exposed(command):
    with pytest.raises(SystemExit) as exc:
        cli.main([command])
    assert exc.value.code == 2


def test_missing_installation_is_explicit(monkeypatch, capsys):
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(cli, "version", missing)
    with pytest.raises(SystemExit) as exc:
        cli.main(["info"])
    assert exc.value.code == 2
    assert "not installed" in capsys.readouterr().err


def test_python_module_entry():
    result = subprocess.run([sys.executable, "-m", "decision_kernel", "--version"], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "decision-os " + version("decision-os-2")
