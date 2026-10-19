"""Synthetic engineering evidence only; never old research or production receipts."""
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

import pytest
from _pytest.junitxml import mangle_test_address

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / ".github/scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def evidence():
    return load("ci-evidence")


@pytest.fixture
def runtime():
    return load("ci-runtime")


@pytest.fixture
def bundle(evidence):
    env = dict(tree="b" * 40, python="3.12.15 synthetic", machine="x86_64", platform="linux",
               image_os="ubuntu24", image_version="20261001.1.0", runner_os="Linux", runner_arch="X64",
               packages={"pytest": "9.1.1", "pytest-xdist": "3.8.0"})
    ident = dict(repo=evidence.REPO, code_sha="a" * 40, tree=env["tree"], event="pull_request", run_id="100", attempt="1")
    contract = "tests/contracts/test_example.py::test_contract"
    selected = {"contracts": [contract], **{g: [f"tests/test_example.py::test_case_{g}"] for g in evidence.GROUPS if g != "contracts"}}
    plan = dict(identity=ident, scope="full", merge_eligible=True, environment=env,
                environment_policy="observed-per-job-image-v1", durations={}, contract_paths=[contract.split("::")[0]],
                collection=[node for group in selected.values() for node in group])

    def junit(nodes):
        root = ET.Element("testsuites")
        suite = ET.SubElement(root, "testsuite", tests=str(len(nodes)), errors="0", failures="0", skipped="0")
        for node in nodes:
            parts = mangle_test_address(node)
            ET.SubElement(suite, "testcase", classname=".".join(parts[:-1]), name=parts[-1], time="0.01")
        return ET.tostring(root)

    def archive(files, name):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            for path, content in files.items():
                z.writestr(path, content)
        raw = output.getvalue()
        meta = dict(id=1, expired=False, name=name, size_in_bytes=len(raw), digest="sha256:" + evidence.digest(raw),
                    workflow_run=dict(id=100, head_sha=ident["code_sha"]))
        return raw, meta

    def source(group, *, chosen=None, mutate=None):
        chosen = selected[group] if chosen is None else chosen
        actual = deepcopy(env)
        if group == "contracts":
            actual["packages"] = {"pytest": "9.1.1"}
        result = dict(identity=deepcopy(ident), group=group, merge_eligible=False, environment=actual,
                      timing_digest=evidence.digest(evidence.encoded(plan["durations"])), count=len(chosen),
                      exit_code=0 if chosen else 5, empty=not chosen)
        if mutate:
            mutate(result)
        return archive({"result.json": evidence.encoded(result), "collection.txt": "\n".join(chosen), "junit.xml": junit(chosen)},
                       f"dos2-part-{group}-100-1")

    sources = {group: source(group) for group in evidence.GROUPS}
    return dict(plan=plan, sources=sources, source=source, archive=archive, junit=junit, selected=selected)
