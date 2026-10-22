"""Thin Actions/Git/pytest I/O. Native matrix schedules; pytest-split assigns tests."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from importlib.metadata import distributions
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
E = runpy.run_path(str(ROOT / ".github/scripts/ci-evidence.py"))
require, encoded = E["require"], E["encoded"]
REPO = E["REPO"]
POLICY = (".github", "requirements", "pyproject.toml", "tests/contracts", "tests/conftest.py", "tests/test_ci_workflow.py")
SHA = re.compile(r"[0-9a-f]{40}")
SMOKE = ["tests/test_package.py"]


def command(*args, token=False, timeout=30):
    env = dict(os.environ)
    if not token:
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
    return subprocess.run(args, cwd=ROOT, env=env, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout).stdout


def git(*args):
    return command("git", *args).decode().strip()


def api(path):
    return json.loads(command("gh", "api", f"repos/{REPO}/{path}", token=True))


def write(out, name, value):
    (out / name).write_bytes(encoded(value))


def read(out, name):
    return json.loads((out / name).read_bytes())


def identity():
    head = os.environ["CI_CODE_SHA"]
    require(SHA.fullmatch(head) and git("rev-parse", "HEAD") == head, "CHECKOUT_IDENTITY")
    require(not command("git", "diff", "--name-only", "HEAD", "--"), "TRACKED_CHECKOUT_CHANGED")
    require(os.environ["GITHUB_REPOSITORY"] == REPO, "REPOSITORY_IDENTITY")
    require(all(os.environ[k].isdigit() and int(os.environ[k]) > 0 for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT")), "RUN_IDENTITY")
    return dict(repo=REPO, code_sha=head, tree=git("rev-parse", "HEAD^{tree}"),
                event=os.environ["GITHUB_EVENT_NAME"], run_id=os.environ["GITHUB_RUN_ID"], attempt=os.environ["GITHUB_RUN_ATTEMPT"])


def environment():
    packages = [(re.sub(r"[-_.]+", "-", p.metadata["Name"]).lower(), p.version) for p in distributions()]
    require(len(packages) == len(dict(packages)), "DUPLICATE_PACKAGES")
    return dict(tree=git("rev-parse", "HEAD^{tree}"), python=sys.version, machine=platform.machine(), platform=sys.platform,
                image_os=os.environ.get("ImageOS"), image_version=os.environ.get("ImageVersion"),
                runner_os=os.environ.get("RUNNER_OS"), runner_arch=os.environ.get("RUNNER_ARCH"), packages=dict(packages))


def changes(base, head):
    raw = command("git", "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", base, head, "--")
    tokens = raw.split(b"\0")
    require(tokens.pop() == b"" and len(tokens) % 2 == 0, "INCOMPLETE_DIFF")
    rows = []
    for metadata, path in zip(tokens[::2], tokens[1::2]):
        old, new, _, _, status = metadata.decode("ascii").split()
        rows.append(dict(path=path.decode("utf-8"), old_mode=old.removeprefix(":"), new_mode=new, status=status))
    return rows


def ordinary(row):
    p = PurePosixPath(row["path"])
    return (str(p) == row["path"] and not p.is_absolute() and ".." not in p.parts and
            not any(ord(c) < 32 for c in row["path"]) and row["status"] in ("A", "M") and
            row["new_mode"] == "100644" and row["old_mode"] in ("000000", "100644"))


def prose(row):
    # An explicit non-normative consumer, not all Markdown or rule files.
    return ordinary(row) and row["path"] == "CHANGELOG.md"


def draft_allowed(rows, payload, head, base):
    try:
        pr = payload["pull_request"]
        owned = (pr["draft"] is True and pr["head"]["sha"] == head and pr["base"]["sha"] == base and
                 pr["head"]["repo"]["full_name"] == REPO and pr["base"]["repo"]["full_name"] == REPO and pr["base"]["ref"] == "main")
        if not owned or payload["action"] not in ("opened", "synchronize", "reopened", "converted_to_draft") or not rows:
            return False
        return all(ordinary(r) and r["path"].startswith(("src/", "tests/")) and
                   not r["path"].startswith("tests/contracts/") and not PurePosixPath(r["path"]).name.startswith("test_ci_")
                   and PurePosixPath(r["path"]).name != "conftest.py" for r in rows)
    except (KeyError, TypeError):
        return False


def run_query(sha, event):
    suffix = "&branch=main" if event == "push" else ""
    return f"actions/workflows/ci.yml/runs?head_sha={sha}&event={event}&per_page=100{suffix}"


def artifact_inventory(ident):
    payload = api(f"actions/runs/{ident['run_id']}/artifacts?per_page=100")
    require(payload["total_count"] == len(payload["artifacts"]), "INCOMPLETE_ARTIFACTS")
    return payload["artifacts"]


def artifact(ident, name, *, inventory=None):
    if inventory is None:
        inventory = artifact_inventory(ident)
    matches = [a for a in inventory if a["name"] == name]
    require(len(matches) == 1, "NO_UNIQUE_ARTIFACT")
    meta = matches[0]
    E["artifact_identity"](meta, ident, name)
    raw = command("gh", "api", f"repos/{REPO}/actions/artifacts/{meta['id']}/zip", token=True)
    return raw, meta


def fallback(exc):
    # Do not publish external error bodies or credentials in diagnostics.
    return str(exc) if type(exc) is ValueError and re.fullmatch(r"[A-Z_]+", str(exc)) else type(exc).__name__


READ_FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError, E["zipfile"].BadZipFile)


@contextmanager
def verified_main_artifact(sha):
    """Read the exact main head's latest qualified artifact, never an older run."""
    run = E["latest_run"](api(run_query(sha, "push")), sha, "push")
    raw, meta = artifact(dict(run_id=str(run["id"]), code_sha=sha), f"dos2-ci-{run['id']}-1")
    with E["verified_archive"](raw, meta) as archive:
        summary = json.loads(archive.read("summary.json"))
        require(summary["identity"]["code_sha"] == sha and summary["identity"]["run_id"] == str(run["id"])
                and summary["identity"]["repo"] == REPO and summary["identity"]["event"] == "push"
                and summary["identity"]["attempt"] == "1" and summary["merge_eligible"] is True
                and summary["scope"] in ("full", "content", "merge_reuse"), "BASE_PROOF")
        yield archive, dict(head_sha=sha, run_id=run["id"], attempt=1,
                            artifact_id=meta["id"], digest=meta["digest"], scope=summary["scope"])


def timing_hints(archive):
    hint = json.loads(archive.read("durations.json"))
    return hint if E["valid_hints"](hint) else {}


def route(out):
    ident = identity()
    base = os.environ.get("CI_BASE_SHA", "")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    pr = payload.get("pull_request", {})
    eligible = (ident["event"] == "push" or (pr.get("draft") is False and pr.get("head", {}).get("repo", {}).get("full_name") == REPO))
    plan = dict(identity=ident, base_sha=base, scope="full", reason="NO_TRUSTED_BASE", changes=[], baseline=None,
                durations={}, merge_eligible=eligible, environment_policy="observed-per-job-image-v1")
    if SHA.fullmatch(base) and base != "0" * 40:
        try:
            plan["changes"] = changes(base, ident["code_sha"])
        except READ_FAILURES as exc:
            plan["reason"] = "DIFF_UNAVAILABLE_" + fallback(exc)
        try:
            with verified_main_artifact(base) as (archive, source):
                plan["baseline"] = dict(id=source["run_id"], head_sha=base)
                plan["durations"] = timing_hints(archive)
        except READ_FAILURES as exc:
            plan["reason"] = "BASE_UNAVAILABLE_" + fallback(exc)
        if plan["baseline"] is None:
            try:
                parent = git("rev-parse", "--verify", base + "^1")
                require(SHA.fullmatch(parent) and parent not in (base, "0" * 40), "NO_FIRST_PARENT")
                with verified_main_artifact(parent) as (archive, source):
                    hint = timing_hints(archive)
                    if hint:
                        plan["durations"] = hint
                        plan["timing_fallback"] = dict(source, base_sha=base, advisory_only=True)
            except READ_FAILURES:
                pass  # One parent only; missing hints never grant baseline or reuse qualification.
    if plan["baseline"] and plan["changes"] and all(prose(r) for r in plan["changes"]):
        plan.update(scope="content", reason="RELEASE_NOTES_ON_TRUSTED_MAIN")
    elif ident["event"] == "pull_request" and draft_allowed(plan["changes"], payload, ident["code_sha"], base):
        plan.update(scope="draft_feedback", reason="OWNED_DRAFT_ONLY", merge_eligible=False)
    write(out, "route.json", plan)
    output("initial_scope", plan["scope"])


def select_reuse(plan, current):
    result = dict(reuse=False, reason="FULL_REQUIRED", source=None)
    ident, base = plan["identity"], plan["base_sha"]
    if not (plan["scope"] == "full" and ident["event"] == "push" and os.environ.get("GITHUB_REF") == "refs/heads/main" and ident["attempt"] == "1"):
        return result
    try:
        parents = git("rev-list", "--parents", "-n", "1", ident["code_sha"]).split()[1:]
        require(len(parents) == 2 and parents[0] == base, "NOT_CLEAN_MERGE_PUSH")
        head = parents[1]
        require(git("rev-parse", head + "^{tree}") == current["tree"], "MERGE_TREE_DIFFERS")
        require(not command("git", "diff", "--name-only", base, ident["code_sha"], "--", *POLICY), "CI_POLICY_CHANGED")
        linked = api(f"commits/{ident['code_sha']}/pulls?per_page=100")
        require(isinstance(linked, list) and len(linked) < 100, "INCOMPLETE_PRS")
        matches = [p for p in linked if p["merge_commit_sha"] == ident["code_sha"] and p["head"]["sha"] == head
                   and p["base"]["sha"] == base and p["base"]["ref"] == "main"]
        require(len(matches) == 1, "MERGE_ASSOCIATION")
        pr = api(f"pulls/{matches[0]['number']}")
        require(pr["merged"] is True and pr["draft"] is False and pr["merge_commit_sha"] == ident["code_sha"]
                and pr["head"]["sha"] == head and pr["head"]["repo"]["full_name"] == REPO
                and pr["base"]["repo"]["full_name"] == REPO and pr["base"]["ref"] == "main", "NOT_OWNED_MERGE")
        query = run_query(head, "pull_request")
        run = E["latest_run"](api(query), head, "pull_request")
        raw, meta = artifact(dict(run_id=str(run["id"]), code_sha=head), f"dos2-ci-{run['id']}-1")
        with E["verified_archive"](raw, meta) as z:
            previous = json.loads(z.read("plan.json"))
            expected = dict(repo=REPO, code_sha=head, tree=current["tree"], event="pull_request", run_id=str(run["id"]), attempt="1")
            require(previous["identity"] == expected and previous["merge_eligible"] is True, "NOT_READY_PR_FULL")
            require(previous["environment"] == current, "ENVIRONMENT_CHANGED")
            sources = json.loads(z.read("sources.json"))
            timing = E["verify_full"](previous, {g: (z.read(f"part-{g}.zip"), sources[g]) for g in E["GROUPS"]}, fresh=False)
        require(E["latest_run"](api(query), head, "pull_request")["id"] == run["id"], "RUN_CHANGED")
        result.update(reuse=True, reason="EXACT_PR_FULL_REUSED", durations=timing,
                      source=dict(run_id=run["id"], head_sha=head, pr=pr["number"], artifact_id=meta["id"], digest=meta["digest"], count=len(timing)))
    except READ_FAILURES + (ImportError, E["ET"].ParseError) as exc:
        result["reason"] = "FULL_REQUIRED_" + fallback(exc)
    return result


def reuse(out):
    plan = read(out, "route.json")
    require(plan["identity"] == identity(), "PLAN_IDENTITY")
    current = environment()
    write(out, "environment.json", current)
    write(out, "reuse.json", select_reuse(plan, current))


def pytest_run(out, label, args, *, collect=False):
    argv = [sys.executable, "-m", "pytest", "-c", "pyproject.toml", "-q" if collect else "-vv", *args]
    # Keep fatal fault handlers without the asynchronous timed traceback dump.
    argv += ["--collect-only"] if collect else [f"--junitxml={out / (label + '.xml')}", "-o", "faulthandler_timeout=0"]
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    log_path = out / (label + ".log")
    try:
        with log_path.open("wb") as log:
            result = subprocess.run(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    except subprocess.TimeoutExpired:
        # Partial diagnostics must not replace the original timeout failure.
        try:
            print(log_path.read_text(errors="replace"), end="", flush=True)
        except Exception:
            pass
        raise
    text = log_path.read_text()
    print(text, end="")
    require(result.returncode in (0, 5), "PYTEST_FAILED")
    return result.returncode, text


def smoke(out, plan, targets):
    code, text = pytest_run(out, "smoke-collection", targets, collect=True)
    selected = E["nodes"](text)
    require(code == 0 and bool(selected), "SMOKE_COLLECTION")
    code, _ = pytest_run(out, "smoke", targets)
    E["passed_tests"](selected, (out / "smoke.xml").read_bytes())
    plan["smoke_collection"] = selected
    plan["smoke_exit_code"] = code


def prepare(out):
    plan = read(out, "route.json")
    require(plan["identity"] == identity(), "PLAN_IDENTITY")
    if plan["scope"] == "content":
        require(plan["changes"] == changes(plan["base_sha"], plan["identity"]["code_sha"]), "CONTENT_DIFF_CHANGED")
        checked = []
        for row in plan["changes"]:
            require(prose(row), "NOT_CONTENT")
            raw = command("git", "show", plan["identity"]["code_sha"] + ":" + row["path"])
            text = raw.decode("utf-8")
            require(bool(text.strip()) and "\0" not in text and not re.search(r"(?m)^(?:<{7}|>{7})(?: |$)", text), "INVALID_CONTENT")
            checked.append(dict(path=row["path"], digest=E["digest"](raw)))
        plan["content_checks"] = checked
    else:
        plan["environment"] = read(out, "environment.json")
        require(E["same_environment"](environment(), plan["environment"]), "PREPARATION_ENVIRONMENT")
        proof = read(out, "reuse.json")
        if proof["reuse"]:
            plan.update(scope="merge_reuse", source=proof["source"], durations=proof["durations"])
            plan.pop("timing_fallback", None)
        if plan["scope"] == "full":
            code, text = pytest_run(out, "collection", [], collect=True)
            require(code == 0, "EMPTY_FULL_COLLECTION")
            plan["collection"] = E["nodes"](text)
            paths = (ROOT / ".github/ci-contracts.txt").read_text().splitlines()
            require(all((ROOT / p).is_file() and not (ROOT / p).is_symlink() for p in paths), "CONTRACT_FILES")
            plan["contract_paths"] = paths
            E["partition"](plan["collection"], paths)
        else:
            targets = list(SMOKE)
            if plan["scope"] == "draft_feedback":
                for row in plan["changes"]:
                    p = row["path"]
                    if p.startswith("src/") and p.endswith(".py"):
                        compile(command("git", "show", plan["identity"]["code_sha"] + ":" + p), p, "exec")
                    if p.startswith("tests/") and PurePosixPath(p).name.startswith("test_") and p not in targets:
                        targets.append(p)
            smoke(out, plan, targets)
    write(out, "plan.json", plan)
    write(out, "durations-hint.json", plan["durations"])
    output("scope", plan["scope"])
    print("CI_SCOPE=" + plan["scope"])


def restore(out, *, inventory=None):
    ident = identity()
    raw, meta = artifact(ident, f"dos2-plan-{ident['run_id']}-{ident['attempt']}", inventory=inventory)
    with E["verified_archive"](raw, meta) as z:
        plan = json.loads(z.read("plan.json"))
        require(plan["identity"] == ident, "PLAN_IDENTITY")
        for name in ("plan.json", "smoke.xml", "reuse.json"):
            if name in z.namelist():
                (out / name).write_bytes(z.read(name))
    write(out, "durations-hint.json", plan["durations"])
    return plan


def execute(out, group):
    plan = read(out, "plan.json")
    require(plan["identity"] == identity() and plan["scope"] == "full" and group in E["GROUPS"], "PARTITION_PLAN")
    actual = environment()
    write(out, "environment-actual.json", actual)
    require(E["same_environment"](actual, plan["environment"], small=group == "contracts", fresh=True), "PARTITION_ENVIRONMENT")
    _, remaining = E["partition"](plan["collection"], plan["contract_paths"])
    args = list(plan["contract_paths"]) if group == "contracts" else ["--ignore=" + p for p in plan["contract_paths"]]
    if group != "contracts" and remaining:
        args += ["--splits", str(len(E["GROUPS"]) - 1), "--group", group, "--splitting-algorithm", "least_duration", "--durations-path", str(out / "durations-hint.json")]
    code, text = pytest_run(out, "selected", args, collect=True)
    selected = E["nodes"](text)
    require(code == (0 if selected else 5), "COLLECTION_EXIT")
    (out / "collection.txt").write_text("\n".join(selected) + ("\n" if selected else ""))
    test_args = args + (["-n", "auto", "--maxprocesses=4", "--dist=loadfile", "--no-loadscope-reorder", "--max-worker-restart=0"] if group != "contracts" and selected else [])
    code, _ = pytest_run(out, "junit", test_args)
    count = E["passed_tests"](selected, (out / "junit.xml").read_bytes())
    require(code == (0 if selected else 5), "TEST_EXIT")
    write(out, "result.json", dict(identity=plan["identity"], group=group, environment=actual, count=count,
          empty=not selected, exit_code=code, merge_eligible=False, timing_digest=E["digest"](encoded(plan["durations"]))))
    identity()  # Tracked changes made by a test invalidate this job.


def gate(out):
    scope = E["check_needs"](json.loads(os.environ["CI_NEEDS"]))
    # Completed prerequisites share one complete native inventory in this gate.
    # Downloads stay pinned to immutable artifact IDs; failures never reselect.
    inventory = artifact_inventory(identity())
    plan = restore(out, inventory=inventory)
    require(plan["scope"] == scope, "SCOPE_MISMATCH")
    durations = plan["durations"]
    if scope == "full":
        sources = {}
        ident = plan["identity"]
        for group in E["GROUPS"]:
            raw, meta = artifact(ident, f"dos2-part-{group}-{ident['run_id']}-{ident['attempt']}", inventory=inventory)
            sources[group] = (raw, meta)
            (out / f"part-{group}.zip").write_bytes(raw)
        durations = E["verify_full"](plan, sources, fresh=True)
        write(out, "sources.json", {g: meta for g, (_, meta) in sources.items()})
    elif scope in ("draft_feedback", "merge_reuse"):
        require(plan["smoke_exit_code"] == 0, "SMOKE_FAILED")
        E["passed_tests"](plan["smoke_collection"], (out / "smoke.xml").read_bytes())
        if scope == "merge_reuse":
            source = plan["source"]
            require(E["latest_run"](api(run_query(source["head_sha"], "pull_request")), source["head_sha"], "pull_request")["id"] == source["run_id"], "REUSE_RUN_CHANGED")
    else:
        require(bool(plan["content_checks"]) and plan["baseline"], "CONTENT_NOT_CHECKED")
    write(out, "durations.json", durations)
    write(out, "summary.json", dict(identity=plan["identity"], scope=scope, merge_eligible=plan["merge_eligible"],
          executed_full_count=len(durations) if scope == "full" else None, source=plan.get("source")))
    print(f"VALIDATED_SCOPE={scope} MERGE_ELIGIBLE={plan['merge_eligible']}")


def output(key, value):
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        handle.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("route", "reuse", "prepare", "restore", "execute", "gate"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--group", choices=E["GROUPS"])
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.operation == "execute":
        execute(args.out, args.group)
    else:
        globals()[args.operation](args.out)


if __name__ == "__main__":
    main()
