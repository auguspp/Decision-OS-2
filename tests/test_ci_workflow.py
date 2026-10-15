"""Owner: native CI wiring/locks. Pure evidence tests live in the small environment."""
from pathlib import Path
import re
import tomllib

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    # BaseLoader preserves GitHub's YAML 1.2 `on` rather than YAML 1.1 boolean.
    return yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)


def test_native_graph_and_four_runners():
    flow = workflow()
    assert set(flow["jobs"]) == {"prepare", "contracts", "remaining", "gate"}
    assert flow["jobs"]["remaining"]["strategy"]["matrix"]["group"] == ["1", "2", "3", "4"]
    assert flow["jobs"]["gate"]["needs"] == ["prepare", "contracts", "remaining"]
    assert flow["jobs"]["gate"]["if"] == "always()"
    assert "ready_for_review" in flow["on"]["pull_request"]["types"]


def test_read_only_scope_and_no_test_tokens():
    flow = workflow()
    assert flow["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert "GH_TOKEN" not in flow["env"]
    for job in flow["jobs"].values():
        assert "GH_TOKEN" not in job.get("env", {})
        for step in job["steps"]:
            if any(op in step.get("run", "") for op in (" execute ", " prepare ")):
                assert "GH_TOKEN" not in step.get("env", {})
            assert "continue-on-error" not in step


def test_actions_pinned_and_checkout_credentials_not_persisted():
    files = [ROOT / ".github/workflows/ci.yml", ROOT / ".github/actions/ci-python/action.yml"]
    for file in files:
        for uses in re.findall(r"uses: ([^\n]+)", file.read_text()):
            if not uses.startswith("./"):
                assert re.fullmatch(r"[A-Za-z0-9_./-]+@[0-9a-f]{40}", uses)
    for job in workflow()["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"


def test_exact_installer_and_no_persistent_cache():
    action = yaml.load((ROOT / ".github/actions/ci-python/action.yml").read_text(), Loader=yaml.BaseLoader)
    steps = action["runs"]["steps"]
    assert steps[0]["with"]["python-version"] == "3.12.15"
    assert steps[1]["with"] == {"version": "0.12.17", "enable-cache": "false"}
    assert "uv pip sync" in steps[2]["run"] and "uv pip check" in steps[2]["run"]


def test_locks_have_exact_shared_versions_and_no_runtime_extras():
    def parse(name):
        result = {}
        for line in (ROOT / "requirements" / name).read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("-r "):
                result.update(parse(line[3:]))
            else:
                assert re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.]+", line)
                key, value = line.split("==")
                assert key not in result
                result[key] = value
        return result
    small, full = parse("contracts.txt"), parse("dev.txt")
    assert small.items() <= full.items() and set(full) > set(small)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["dependencies"] == []
    assert project["build-system"]["requires"] == ["setuptools==" + small["setuptools"]]


def test_small_contract_manifest_has_real_current_consumers():
    paths = (ROOT / ".github/ci-contracts.txt").read_text().splitlines()
    assert set(paths) == {p.relative_to(ROOT).as_posix() for p in (ROOT / "tests/contracts").glob("test_*.py")}
    assert len(paths) == len(set(paths))
