"""Pure CI evidence checks, narrowed from decision-kernel@cb3bbc87.

No test selection, scheduling, artifact execution or old evidence codecs.
See docs/engineering/ci-implementation.md for provenance and maintenance scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import math
from pathlib import PurePosixPath
import re
import stat
import xml.etree.ElementTree as ET
import zipfile

REPO = "auguspp/Decision-OS-2"
WORKFLOW = ".github/workflows/ci.yml"
NAME = "decision-os-ci"
SCOPES = {"content", "draft_feedback", "full", "merge_reuse"}
GROUPS = ("contracts", "1", "2", "3", "4", "5", "6", "7", "8")
MAX_ZIP = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
ENV_KEYS = {"tree", "python", "machine", "platform", "image_os", "image_version", "runner_os", "runner_arch", "packages"}


def require(ok, code):
    if not ok:
        raise ValueError(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def nodes(text):
    result = [s for s in text.splitlines() if s.startswith("tests/") and "::" in s]
    require(len(result) == len(set(result)), "DUPLICATE_COLLECTION")
    return result


def partition(collection, paths):
    require(len(collection) == len(set(collection)) and bool(collection), "FULL_COLLECTION")
    require(bool(paths) and len(paths) == len(set(paths)), "CONTRACT_PATHS")
    require(all(re.fullmatch(r"tests/(?:[A-Za-z0-9_]+/)*test_[A-Za-z0-9_]+\.py", p) for p in paths), "CONTRACT_PATHS")
    contracts = [n for n in collection if n.split("::", 1)[0] in paths]
    require({n.split("::", 1)[0] for n in contracts} == set(paths), "EMPTY_CONTRACT_FILE")
    return contracts, [n for n in collection if n not in set(contracts)]


def same_environment(actual, expected, *, small=False, fresh=False):
    if not isinstance(actual, dict) or not isinstance(expected, dict) or actual.keys() != ENV_KEYS or expected.keys() != ENV_KEYS:
        return False
    for data in (actual, expected):
        if any(not isinstance(data[k], str) or not data[k] for k in ENV_KEYS - {"packages"}):
            return False
        if re.fullmatch(r"[0-9]{8}\.[0-9]+\.[0-9]+", data["image_version"]) is None:
            return False
        if not isinstance(data["packages"], dict) or not data["packages"]:
            return False
    omitted = {"packages"} | ({"image_version"} if fresh else set())
    if any(actual[k] != expected[k] for k in ENV_KEYS - omitted):
        return False
    a, b = actual["packages"], expected["packages"]
    return all(b.get(k) == v for k, v in a.items()) if small else a == b


def passed_tests(selected, xml):
    # Use pytest's actual JUnit address conversion, including parameters with ::.
    from _pytest.junitxml import mangle_test_address
    require(len(selected) == len(set(selected)), "DUPLICATE_COLLECTION")
    addresses = [mangle_test_address(node) for node in selected]
    expected = {(".".join(x[:-1]), x[-1]) for x in addresses}
    require(len(expected) == len(selected), "JUNIT_ADDRESS_COLLISION")
    require(b"<!DOCTYPE" not in xml and b"<!ENTITY" not in xml, "XML_DECLARATION")
    root = ET.fromstring(xml)
    suites, cases = root.findall(".//testsuite"), root.findall(".//testcase")
    require(bool(suites), "MISSING_JUNIT_SUITE")
    require(all(int(s.get(k, "-1")) == 0 for s in suites for k in ("errors", "failures", "skipped")), "TESTS_NOT_PASSED")
    require(all(root.find(".//" + tag) is None for tag in ("error", "failure", "skipped")), "TESTS_NOT_PASSED")
    actual = {(c.attrib["classname"], c.attrib["name"]) for c in cases}
    require(len(actual) == len(cases) and actual == expected, "COLLECTION_JUNIT_MISMATCH")
    return len(actual)


@contextmanager
def verified_archive(raw, metadata):
    require(0 < len(raw) == metadata["size_in_bytes"] <= MAX_ZIP, "ARCHIVE_SIZE")
    require(metadata["digest"] == "sha256:" + digest(raw), "ARCHIVE_DIGEST")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        names = [i.filename for i in infos]
        require(0 < len(names) <= 128 and len(names) == len(set(names)), "ARCHIVE_NAMES")
        require(sum(i.file_size for i in infos) <= MAX_EXPANDED, "ARCHIVE_EXPANDED_SIZE")
        require(all(not PurePosixPath(n).is_absolute() and ".." not in PurePosixPath(n).parts and "\\" not in n for n in names), "ARCHIVE_PATH")
        require(all(not stat.S_ISLNK(i.external_attr >> 16) and not i.flag_bits & 1 for i in infos), "ARCHIVE_TYPE")
        require(archive.testzip() is None, "ARCHIVE_CRC")
        yield archive


def artifact_identity(metadata, identity, name):
    require(type(metadata.get("id")) is int and metadata["id"] > 0 and metadata.get("expired") is False, "ARTIFACT_ID")
    require(metadata["name"] == name and str(metadata["workflow_run"]["id"]) == identity["run_id"]
            and metadata["workflow_run"]["head_sha"] == identity["code_sha"], "ARTIFACT_IDENTITY")


def check_needs(needs):
    require(set(needs) == {"prepare", "contracts", "remaining"}, "UNEXPECTED_JOBS")
    require(needs["prepare"]["result"] == "success", "PREPARATION_FAILED")
    scope = needs["prepare"]["outputs"]["scope"]
    require(scope in SCOPES, "UNKNOWN_SCOPE")
    expected = "success" if scope == "full" else "skipped"
    require(all(needs[k]["result"] == expected for k in ("contracts", "remaining")), "REQUIRED_JOBS_NOT_PASSED")
    return scope


def latest_run(payload, sha, event):
    rows = payload["workflow_runs"]
    require(payload["total_count"] == len(rows) and bool(rows), "INCOMPLETE_OR_EMPTY_RUNS")
    require(all(type(r.get("id")) is int and r["id"] > 0 for r in rows), "RUN_ID")
    run = max(rows, key=lambda r: r["id"])  # Never skip a newer failed run.
    require(run["head_sha"] == sha and run["event"] == event and run["name"] == NAME and run["path"] == WORKFLOW
            and run["head_repository"]["full_name"] == REPO and type(run["run_attempt"]) is int and run["run_attempt"] == 1
            and run["status"] == "completed" and run["conclusion"] == "success", "NO_TRUSTED_LATEST_RUN")
    require(event != "push" or run["head_branch"] == "main", "NOT_MAIN_BASE")
    return run


def valid_hints(values):
    return isinstance(values, dict) and len(values) <= 100000 and all(
        isinstance(k, str) and k.startswith("tests/") and "::" in k and type(v) in (int, float)
        and math.isfinite(v) and v >= 0 for k, v in values.items())


def verify_full(plan, sources, *, fresh):
    require(plan["scope"] == "full" and plan["identity"]["repo"] == REPO, "NOT_THIS_FULL")
    require(plan["environment_policy"] == "observed-per-job-image-v1", "ENVIRONMENT_POLICY")
    require(set(sources) == set(GROUPS), "MISSING_PARTITION")
    contracts, remaining = partition(plan["collection"], plan["contract_paths"])
    require(valid_hints(plan["durations"]), "TIMING_HINTS")
    seen, timing = [], {}
    from _pytest.junitxml import mangle_test_address
    for group in GROUPS:
        raw, meta = sources[group]
        ident = plan["identity"]
        name = f"dos2-part-{group}-{ident['run_id']}-{ident['attempt']}"
        artifact_identity(meta, ident, name)
        with verified_archive(raw, meta) as z:
            result = json.loads(z.read("result.json"))
            require(result["identity"] == ident and result["group"] == group and result["merge_eligible"] is False, "PARTITION_IDENTITY")
            require(result["timing_digest"] == digest(encoded(plan["durations"])), "TIMING_DRIFT")
            require(same_environment(result["environment"], plan["environment"], small=group == "contracts", fresh=fresh), "ENVIRONMENT_DRIFT")
            selected = nodes(z.read("collection.txt").decode())
            xml = z.read("junit.xml")
            count = passed_tests(selected, xml)
            require(type(result["count"]) is int and result["count"] == count, "PARTITION_COUNT")
            require(result["exit_code"] == (0 if selected else 5), "PARTITION_EXIT")
            require((selected == contracts) if group == "contracts" else set(selected) <= set(remaining), "PARTITION_MEMBERSHIP")
            require(result["empty"] is (not selected), "EMPTY_PARTITION_MARKER")
            seen.extend(selected)
            cases = {(c.get("classname"), c.get("name")): float(c.get("time", "0")) for c in ET.fromstring(xml).findall(".//testcase")}
            for node in selected:
                address = mangle_test_address(node)
                value = cases[(".".join(address[:-1]), address[-1])]
                require(math.isfinite(value) and value >= 0, "INVALID_DURATION")
                timing[node] = value
    require(len(seen) == len(set(seen)) and set(seen) == set(plan["collection"]), "PARTITION_UNION")
    return timing
