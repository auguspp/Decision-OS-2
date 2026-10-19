"""Owner: CI evidence contract. Retire with its last writer and reader, not Issue #5."""
from copy import deepcopy
import pytest


def test_complete_disjoint_bundle(evidence, bundle):
    result = evidence.verify_full(bundle["plan"], bundle["sources"], fresh=True)
    assert set(result) == set(bundle["plan"]["collection"])
    assert all(value == 0.01 for value in result.values())


@pytest.mark.parametrize("fault", ["missing", "duplicate", "omitted", "foreign", "wrong_head", "wrong_run", "count", "timing", "exit", "empty", "digest"])
def test_bad_bundle_fails(evidence, bundle, fault):
    sources = dict(bundle["sources"])
    if fault == "missing":
        del sources["8"]
    elif fault == "duplicate":
        sources["8"] = bundle["source"]("8", chosen=bundle["selected"]["7"])
    elif fault == "omitted":
        sources["8"] = bundle["source"]("8", chosen=[])
    elif fault == "foreign":
        sources["8"] = bundle["source"]("8", chosen=["tests/test_other.py::test_unknown"])
    elif fault in ("wrong_head", "wrong_run"):
        key = "code_sha" if fault == "wrong_head" else "run_id"
        sources["8"] = bundle["source"]("8", mutate=lambda r: r["identity"].update({key: "9" * 40}))
    elif fault == "digest":
        raw, meta = sources["8"]
        sources["8"] = (raw + b"x", {**meta, "size_in_bytes": len(raw) + 1})
    else:
        key, value = {"count": ("count", 99), "timing": ("timing_digest", "f" * 64), "exit": ("exit_code", 5), "empty": ("empty", True)}[fault]
        sources["8"] = bundle["source"]("8", mutate=lambda r: r.update({key: value}))
    with pytest.raises(ValueError):
        evidence.verify_full(bundle["plan"], sources, fresh=True)


def test_proven_empty_partition_is_not_a_missing_job(evidence, bundle):
    bundle["plan"]["collection"].remove(bundle["selected"]["8"][0])
    bundle["sources"]["8"] = bundle["source"]("8", chosen=[])
    assert len(evidence.verify_full(bundle["plan"], bundle["sources"], fresh=True)) == len(bundle["plan"]["collection"])


@pytest.mark.parametrize("field,value", [("python", "3.12.14"), ("runner_arch", "ARM64"), ("image_version", None), ("tree", "d" * 40)])
def test_environment_identity_cannot_drift(evidence, bundle, field, value):
    expected = bundle["plan"]["environment"]
    actual = {**expected, field: value}
    assert not evidence.same_environment(actual, expected, fresh=True)


def test_image_rollout_is_fresh_execution_only(evidence, bundle):
    sources = dict(bundle["sources"])
    sources["3"] = bundle["source"]("3", mutate=lambda r: r["environment"].update(image_version="20261002.1.0"))
    assert evidence.verify_full(bundle["plan"], sources, fresh=True)
    with pytest.raises(ValueError, match="ENVIRONMENT_DRIFT"):
        evidence.verify_full(bundle["plan"], sources, fresh=False)


def test_small_environment_omits_but_never_changes_shared_packages(evidence, bundle):
    expected = bundle["plan"]["environment"]
    small = {**expected, "packages": {"pytest": "9.1.1"}}
    assert evidence.same_environment(small, expected, small=True, fresh=True)
    small["packages"] = {"pytest": "9.0.2"}
    assert not evidence.same_environment(small, expected, small=True, fresh=True)


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
def test_always_gate_never_promotes_failed_needs(evidence, result):
    needs = {"prepare": {"result": "success", "outputs": {"scope": "full"}}, "contracts": {"result": "success"}, "remaining": {"result": result}}
    with pytest.raises(ValueError, match="REQUIRED_JOBS_NOT_PASSED"):
        evidence.check_needs(needs)


@pytest.mark.parametrize("scope", ["content", "draft_feedback", "merge_reuse"])
def test_limited_scope_requires_explicitly_skipped_full_jobs(evidence, scope):
    needs = {"prepare": {"result": "success", "outputs": {"scope": scope}}, "contracts": {"result": "skipped"}, "remaining": {"result": "skipped"}}
    assert evidence.check_needs(needs) == scope
    needs["contracts"]["result"] = "cancelled"
    with pytest.raises(ValueError):
        evidence.check_needs(needs)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "folder\\escape"])
def test_unsafe_archive_names_rejected(evidence, bundle, path):
    raw, meta = bundle["archive"]({path: b"not executed"}, "test")
    with pytest.raises(ValueError, match="ARCHIVE_PATH"):
        with evidence.verified_archive(raw, meta):
            pass


def test_junit_failure_and_skips_are_not_passes(evidence, bundle):
    chosen = bundle["selected"]["1"]
    xml = bundle["junit"](chosen)
    for tag in (b"failure", b"error", b"skipped"):
        bad = xml.replace(b" />", b"><" + tag + b"/></testcase>", 1)
        with pytest.raises(ValueError):
            evidence.passed_tests(chosen, bad)


def test_parameter_with_colons_keeps_pytest_identity(evidence, bundle):
    nodes = ["tests/test_example.py::test_value[a::b]"]
    assert evidence.passed_tests(nodes, bundle["junit"](nodes)) == 1


@pytest.mark.parametrize("hint", [{"tests/test_x.py::test_x": float("nan")}, {"tests/test_x.py::test_x": -1}, {"bad": 1}, {"tests/test_x.py::test_x": True}])
def test_bad_duration_hints_do_not_qualify(evidence, hint):
    assert not evidence.valid_hints(hint)


def test_empty_hints_do_not_remove_tests(evidence, bundle):
    assert evidence.valid_hints({})
    assert len(evidence.verify_full(bundle["plan"], bundle["sources"], fresh=True)) == len(bundle["plan"]["collection"])


def test_duplicate_and_unrepresented_contract_paths_fail(evidence):
    with pytest.raises(ValueError):
        evidence.partition(["tests/test_a.py::test_a"], ["tests/test_missing.py"])
    with pytest.raises(ValueError):
        evidence.nodes("tests/test_a.py::test_a\ntests/test_a.py::test_a")
