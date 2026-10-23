"""The delivery-entry inventory must describe the repository, not a remembered list."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/architecture/workflow-estate.json"
WORKFLOWS = ROOT / ".github/workflows"


def entries() -> list:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["workflows"]


def text(entry) -> str:
    return (WORKFLOWS / entry["file"]).read_text(encoding="utf-8")


def on_block(source: str) -> str:
    return source.split("\non:", 1)[1].split("\npermissions:", 1)[0]


def test_every_file_is_recorded_once_and_present_entries_have_a_file():
    recorded = [entry["file"] for entry in entries()]
    assert len(recorded) == len(set(recorded)), "a workflow file is recorded twice"
    present = sorted(entry["file"] for entry in entries() if entry["state"] == "present")
    assert present == sorted(page.name for page in WORKFLOWS.glob("*.yml"))
    for entry in entries():
        assert (WORKFLOWS / entry["file"]).exists() is (entry["state"] == "present"), (
            entry["file"] + " disagrees with its state")


def test_registry_only_entries_never_come_back_as_files():
    for entry in entries():
        if entry["state"] == "registry-only":
            assert not (WORKFLOWS / entry["file"]).exists(), (
                entry["file"] + " returned without an inventory update")
            assert entry["disposition"] == "registry-only"
            assert entry["triggers"] == {"dispatch": None, "schedule": None,
                                         "pull_request": None, "push": None}


def test_declared_triggers_match_the_workflow_file():
    for entry in entries():
        if entry["state"] != "present":
            continue
        block = on_block(text(entry))
        declared = entry["triggers"]
        assert declared["dispatch"] is ("workflow_dispatch" in block), entry["file"]
        assert declared["pull_request"] is ("pull_request" in block), entry["file"]
        assert declared["push"] is bool(re.search(r"(?m)^  push:", block)), entry["file"]
        cron = re.search(r"cron:\s*'([^']+)'", block)
        assert declared["schedule"] == (cron.group(1) if cron else None), entry["file"]


def test_recorded_name_matches_the_workflow_name():
    for entry in entries():
        if entry["state"] != "present":
            continue
        name = re.search(r"(?m)^name:\s*(\S+)", text(entry)).group(1)
        assert entry["name"] == name, entry["file"]


def test_dispositions_are_known_and_a_self_starting_entry_is_never_on_the_retire_list():
    allowed = set(json.loads(MANIFEST.read_text(encoding="utf-8"))["dispositions"])
    for entry in entries():
        assert entry["disposition"] in allowed, entry["file"]
        self_starting = (entry["triggers"]["schedule"] is not None
                         or entry["triggers"]["push"] or entry["triggers"]["pull_request"])
        if self_starting:
            assert entry["disposition"] not in ("retire-candidate", "registry-only"), (
                entry["file"] + " would retire while still starting itself")


def test_each_entry_names_an_existing_entry_point():
    for entry in entries():
        for module in entry["entries"]:
            relative = Path(*module.split(".")).with_suffix(".py")
            assert (ROOT / "src" / relative).exists(), module + " is not a source file"


def test_each_entry_states_its_responsibility_and_exit():
    for entry in entries():
        assert entry["responsibility"].strip(), entry["file"]
        assert entry["exit"].strip(), entry["file"]
        assert entry["consumers"].strip(), entry["file"]
