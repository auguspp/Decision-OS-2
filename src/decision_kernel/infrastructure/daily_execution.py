"""One approved daily execution version, separate from its native workflow head.

Only this deployment's daily host uses this protocol. Exact Git contents and
successful static checks can be reused within a run; current repository access,
main head and the authorization selected by that head are always checked live.
The interface marker identifies a reviewed contract, not an independent code
signature. Its exact source tree and native CI remain part of admission.
"""
from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlencode

from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.runtime_publication import REPOSITORY, REPOSITORY_ID

PREFIX = "/repos/" + REPOSITORY
WORKFLOW = ".github/workflows/daily-market.yml"
POLICY_PATH = "config/daily-runtime-version.json"
PUBLIC_PATH = "config/public-runtime.json"
INTERFACE_PATH = "config/daily-runtime-interface.json"
POLICY_SCHEMA = "daily-runtime-policy-v1"
SELECTION_SCHEMA = "daily-runtime-selection-v1"
PROTOCOL = "daily-runtime-v1"
INTERFACE = {"schema": "daily-runtime-interface-v1", "protocol": PROTOCOL}
ENVIRONMENT_KEY = "DECISION_OS_EXECUTION"
AUTH_GUARD_REQUESTS = 6
# The existing daily page always asks for the saved Hosted AI Brief, which can
# contain retained Watch evidence. These are publication dependencies only;
# requiring them neither enables nor executes a Watch.
DAILY_SCOPES = frozenset({"market", "sources", "reading", "brief", "research", "handoff", "watch"})
_SHA = re.compile(r"[a-f0-9]{40}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_APPROVAL = re.compile(r"https://github\.com/auguspp/Decision-OS-2/(?:issues|pull)/[1-9][0-9]*"
    r"(?:#(?:issuecomment|pullrequestreview)-[1-9][0-9]*)?\Z")
_VERSION_KEYS = {"schema", "mode", "code_tree", "policy_commit", "policy_sha256", "protocol",
    "workflow_ci_run", "execution_ci_run", "approval_ref"}
_SELECTED_KEYS = {"operation", "invocation", "event", "run_id", "run_attempt", "code_sha",
    "workflow_sha", "runtime_version"}
ERROR_CODES = frozenset({"DAILY_EXECUTION_IDENTITY", "DAILY_EXECUTION_POLICY",
    "DAILY_EXECUTION_ENVIRONMENT", "DAILY_EXECUTION_NATIVE_CONTEXT", "DAILY_EXECUTION_CACHE",
    "DAILY_EXECUTION_CONTENT", "DAILY_EXECUTION_REPOSITORY", "DAILY_EXECUTION_MAIN",
    "DAILY_EXECUTION_DISABLED", "DAILY_EXECUTION_PUBLICATION_REVOKED", "DAILY_EXECUTION_OFF_MAIN",
    "DAILY_EXECUTION_POLICY_CHANGED", "DAILY_EXECUTION_NATIVE_RUN", "DAILY_EXECUTION_MAIN_CI",
    "DAILY_EXECUTION_TREE", "DAILY_EXECUTION_PROTOCOL_UNSUPPORTED", "DAILY_EXECUTION_POLICY_PROVENANCE",
    "DAILY_EXECUTION_STATIC_CHANGED", "DAILY_EXECUTION_CHECKOUT"})


def _sha(value, error="DAILY_EXECUTION_IDENTITY"):
    require(isinstance(value, str) and _SHA.fullmatch(value) is not None, error)
    return value


def _integer(value, error="DAILY_EXECUTION_IDENTITY"):
    require(type(value) is int and value > 0, error)
    return value


def _json(raw, error):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, error)
            value[key] = item
        return value
    def invalid(_value):
        raise RecordError(error)
    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise RecordError(error) from None


def policy(value):
    """Validate one fixed deployment policy; no caller-supplied paths or hosts."""
    error = "DAILY_EXECUTION_POLICY"
    require(isinstance(value, dict) and set(value) == {"schema", "enabled", "mode", "pinned"}
        and value["schema"] == POLICY_SCHEMA and type(value["enabled"]) is bool
        and isinstance(value["mode"], str) and value["mode"] in {"current-main", "pinned"}, error)
    pinned = value["pinned"]
    if value["mode"] == "current-main":
        require(pinned is None, error)
    else:
        require(isinstance(pinned, dict)
            and set(pinned) == {"code_sha", "tree_sha", "main_ci_run", "approval_ref"}, error)
        _sha(pinned["code_sha"], error); _sha(pinned["tree_sha"], error)
        _integer(pinned["main_ci_run"], error)
        require(isinstance(pinned["approval_ref"], str)
            and _APPROVAL.fullmatch(pinned["approval_ref"]) is not None, error)
    return deepcopy(value)


def _identity(value):
    error = "DAILY_EXECUTION_IDENTITY"
    require(isinstance(value, dict) and set(value) == {"workflow_sha", "code_sha", "runtime_version"}, error)
    _sha(value["workflow_sha"]); _sha(value["code_sha"])
    version = value["runtime_version"]
    require(isinstance(version, dict) and set(version) == _VERSION_KEYS
        and version["schema"] == SELECTION_SCHEMA and version["protocol"] == PROTOCOL
        and isinstance(version["mode"], str) and version["mode"] in {"current-main", "pinned"}, error)
    _sha(version["code_tree"]); _sha(version["policy_commit"])
    require(isinstance(version["policy_sha256"], str)
        and _DIGEST.fullmatch(version["policy_sha256"]) is not None, error)
    _integer(version["workflow_ci_run"]); _integer(version["execution_ci_run"])
    if version["mode"] == "current-main":
        require(version["approval_ref"] is None and value["code_sha"] == value["workflow_sha"]
            and version["workflow_ci_run"] == version["execution_ci_run"], error)
    else:
        require(isinstance(version["approval_ref"], str)
            and _APPROVAL.fullmatch(version["approval_ref"]) is not None, error)
    return deepcopy(value)


def execution_identity(env):
    """Parse workflow output; this alone never grants execution authority."""
    require(isinstance(env, Mapping), "DAILY_EXECUTION_ENVIRONMENT")
    if ENVIRONMENT_KEY not in env:
        return None
    raw = env[ENVIRONMENT_KEY]
    require(isinstance(raw, str) and 0 < len(raw) <= 16384
        and "\n" not in raw and "\r" not in raw, "DAILY_EXECUTION_ENVIRONMENT")
    return _identity(_json(raw, "DAILY_EXECUTION_ENVIRONMENT"))


def native_head_sha(selected):
    """Legacy non-daily selections retain their original single-SHA contract."""
    require(isinstance(selected, dict), "DAILY_EXECUTION_IDENTITY")
    if "runtime_version" not in selected:
        require("workflow_sha" not in selected, "DAILY_EXECUTION_IDENTITY")
        return _sha(selected.get("code_sha"))
    identity = _identity({key: selected.get(key) for key in ("workflow_sha", "code_sha", "runtime_version")})
    return identity["workflow_sha"]


def _selected(selected):
    from decision_kernel.application.daily_clock import DISPATCH_OPERATIONS
    require(isinstance(selected, dict) and set(selected) <= _SELECTED_KEYS
        and isinstance(selected.get("operation"), str) and selected["operation"] in {"status", "update"}
        and isinstance(selected.get("event"), str) and selected["event"] in {"workflow_dispatch", "schedule"}
        and isinstance(selected.get("invocation"), str)
        and selected["invocation"] in DISPATCH_OPERATIONS | {"schedule"}
        and type(selected.get("run_attempt")) is int and selected["run_attempt"] == 1,
        "DAILY_EXECUTION_IDENTITY")
    require((selected["event"] == "schedule") == (selected["invocation"] == "schedule")
        and selected["operation"] == ("status" if selected["invocation"] in {"status", "clock-probe"} else "update"),
        "DAILY_EXECUTION_IDENTITY")
    _integer(selected.get("run_id"))
    native_head_sha(selected)


def _context(selected, env):
    _selected(selected)
    require(isinstance(env, Mapping)
        and env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_WORKFLOW_REF") == REPOSITORY + "/" + WORKFLOW + "@refs/heads/main"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_RUN_ID") == str(selected["run_id"])
        and env.get("GITHUB_EVENT_NAME") == selected["event"]
        and env.get("GITHUB_SHA") == native_head_sha(selected)
        and env.get("GITHUB_WORKFLOW_SHA") == native_head_sha(selected), "DAILY_EXECUTION_NATIVE_CONTEXT")


def _cache(api, supplied):
    if supplied is None:
        supplied = getattr(api, "_daily_execution_cache", None)
        if supplied is None:
            supplied = {}
            try:
                api._daily_execution_cache = supplied
            except AttributeError:
                pass
    require(isinstance(supplied, dict), "DAILY_EXECUTION_CACHE")
    return supplied


def _remember(cache, key, value, *, limit=96):
    if len(cache) < limit:
        cache[key] = deepcopy(value)


def _contents(api, commit, path, cache, *, bound=8192):
    key = ("daily-content", REPOSITORY_ID, commit, path)
    if key in cache:
        return cache[key]
    value = api.request("GET", PREFIX + "/contents/" + path + "?ref=" + _sha(commit))
    error = "DAILY_EXECUTION_CONTENT"
    require(isinstance(value, dict) and value.get("type") == "file" and value.get("path") == path
        and value.get("encoding") == "base64" and isinstance(value.get("content"), str)
        and type(value.get("size")) is int and 0 < value["size"] <= bound
        and len(value["content"]) <= (bound * 2 + 128), error)
    try:
        raw = base64.b64decode(value["content"].replace("\n", ""), validate=True)
    except (ValueError, binascii.Error):
        raise RecordError(error) from None
    require(len(raw) == value["size"] and len(raw) <= bound, error)
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    require(value.get("sha") == blob, error)
    result = _json(raw, error)
    _remember(cache, key, result)
    return result


def _snapshot(api, cache):
    meta = api.request("GET", PREFIX)
    error = "DAILY_EXECUTION_REPOSITORY"
    require(isinstance(meta, dict) and type(meta.get("id")) is int and meta["id"] == REPOSITORY_ID
        and meta.get("full_name") == REPOSITORY and type(meta.get("private")) is bool
        and meta.get("archived") is False and meta.get("default_branch") == "main", error)
    visibility = "private" if meta["private"] else "public"
    require("visibility" not in meta or meta["visibility"] == visibility, error)
    ref = api.request("GET", PREFIX + "/git/ref/heads/main")
    require(isinstance(ref, dict) and ref.get("ref") == "refs/heads/main"
        and isinstance(ref.get("object"), dict) and ref["object"].get("type") == "commit",
        "DAILY_EXECUTION_MAIN")
    head = _sha(ref["object"].get("sha"), "DAILY_EXECUTION_MAIN")
    approved = policy(_contents(api, head, POLICY_PATH, cache))
    from decision_kernel.infrastructure.runtime_publication import validate_authorization
    publication = validate_authorization(_contents(api, head, PUBLIC_PATH, cache))
    require(approved["enabled"], "DAILY_EXECUTION_DISABLED")
    basis = None
    if visibility == "public":
        require(publication["enabled"] and DAILY_SCOPES <= set(publication["scopes"]),
            "DAILY_EXECUTION_PUBLICATION_REVOKED")
        basis = publication["authorization_ref"]
    return head, approved, {"visibility": visibility, "publication_basis": basis, "main_sha": head}


def _ancestor(api, base, head, cache):
    key = ("daily-ancestor", REPOSITORY_ID, base, head)
    if base == head or key in cache:
        return
    value = api.request("GET", PREFIX + "/compare/" + base + "..." + head + "?per_page=1")
    require(isinstance(value, dict) and value.get("status") in {"ahead", "identical"}
        and isinstance(value.get("merge_base_commit"), dict)
        and value["merge_base_commit"].get("sha") == base, "DAILY_EXECUTION_OFF_MAIN")
    _remember(cache, key, True)


def _allows(approved, identity):
    version = identity["runtime_version"]
    require(digest(encoded(approved)) == version["policy_sha256"]
        and approved["mode"] == version["mode"], "DAILY_EXECUTION_POLICY_CHANGED")
    if approved["mode"] == "pinned":
        pinned = approved["pinned"]
        require(pinned["code_sha"] == identity["code_sha"]
            and pinned["tree_sha"] == version["code_tree"]
            and pinned["main_ci_run"] == version["execution_ci_run"]
            and pinned["approval_ref"] == version["approval_ref"], "DAILY_EXECUTION_POLICY_CHANGED")


def require_live_execution(api, selected, *, cache=None):
    """At most six GETs, including a cold changed-head authorization check.

Repository and head are never cached. Two exact-commit contents and at most
two ancestry comparisons complete the check; source peer scans are separate.
Static run/CI/tree/protocol admission is the caller's entry-point responsibility.
"""
    _selected(selected)
    identity = _identity({key: selected.get(key) for key in ("workflow_sha", "code_sha", "runtime_version")})
    cache = _cache(api, cache)
    head, approved, result = _snapshot(api, cache)
    _allows(approved, identity)
    for code in dict.fromkeys((identity["workflow_sha"], identity["code_sha"])):
        _ancestor(api, code, head, cache)
    return result


def _native_run(api, selected):
    from decision_kernel.application.daily_clock import run_title
    value = api.request("GET", PREFIX + "/actions/runs/" + str(selected["run_id"]))
    title = run_title(selected.get("invocation"))
    require(isinstance(value, dict) and type(value.get("id")) is int and value["id"] == selected["run_id"]
        and value.get("head_sha") == native_head_sha(selected) and value.get("head_branch") == "main"
        and value.get("event") == selected["event"] and type(value.get("run_attempt")) is int
        and value["run_attempt"] == 1 and value.get("path") == WORKFLOW and value.get("name") == title
        and value.get("status") == "in_progress"
        and (selected.get("invocation") not in {"external-clock", "clock-probe"} or value.get("display_title") == title)
        and all(isinstance(value.get(key), dict) and type(value[key].get("id")) is int
            and value[key]["id"] == REPOSITORY_ID for key in ("repository", "head_repository")),
        "DAILY_EXECUTION_NATIVE_RUN")


def _ci(api, code):
    value = api.request("GET", PREFIX + "/actions/workflows/ci.yml/runs?" + urlencode({
        "branch": "main", "event": "push", "head_sha": code, "per_page": 5}))
    error = "DAILY_EXECUTION_MAIN_CI"
    rows = value.get("workflow_runs") if isinstance(value, dict) else None
    require(isinstance(rows, list) and 0 < len(rows) <= 5
        and type(value.get("total_count")) is int and value["total_count"] == len(rows)
        and all(isinstance(row, dict) and type(row.get("id")) is int for row in rows), error)
    row = max(rows, key=lambda item: item["id"])
    require(row["id"] > 0 and row.get("head_sha") == code and row.get("head_branch") == "main"
        and row.get("event") == "push" and row.get("path") == ".github/workflows/ci.yml"
        and row.get("status") == "completed" and row.get("conclusion") == "success"
        and type(row.get("run_attempt")) is int and row["run_attempt"] == 1
        and row.get("name") == "decision-os-ci"
        and all(isinstance(row.get(key), dict) and type(row[key].get("id")) is int
            and row[key]["id"] == REPOSITORY_ID for key in ("repository", "head_repository")), error)
    return row["id"]


def _tree(api, code):
    value = api.request("GET", PREFIX + "/git/commits/" + code)
    require(isinstance(value, dict) and value.get("sha") == code
        and isinstance(value.get("tree"), dict), "DAILY_EXECUTION_TREE")
    return _sha(value["tree"].get("sha"), "DAILY_EXECUTION_TREE")


def select_execution(api, selected, *, env=None, cache=None, require_inherited=False):
    """Admit or revalidate the exact preflight binding without silently reselection."""
    env = os.environ if env is None else env
    selected = deepcopy(selected)
    inherited = execution_identity(env)
    require(type(require_inherited) is bool and (not require_inherited or inherited is not None),
        "DAILY_EXECUTION_ENVIRONMENT")
    if inherited is not None:
        if "runtime_version" in selected:
            require(encoded({key: selected.get(key) for key in inherited}) == encoded(inherited),
                "DAILY_EXECUTION_IDENTITY")
        selected.update(inherited)
    _context(selected, env)
    cache = _cache(api, cache)
    head, approved, _ = _snapshot(api, cache)
    _native_run(api, selected)
    frozen = "runtime_version" in selected
    workflow_sha = native_head_sha(selected)
    if frozen:
        identity = _identity({key: selected.get(key) for key in ("workflow_sha", "code_sha", "runtime_version")})
        _allows(approved, identity)
        _ancestor(api, identity["runtime_version"]["policy_commit"], head, cache)
        old = policy(_contents(api, identity["runtime_version"]["policy_commit"], POLICY_PATH, cache))
        require(digest(encoded(old)) == identity["runtime_version"]["policy_sha256"],
            "DAILY_EXECUTION_POLICY_PROVENANCE")
    else:
        code_sha = workflow_sha if approved["mode"] == "current-main" else approved["pinned"]["code_sha"]
        identity = {"workflow_sha": workflow_sha, "code_sha": code_sha}
    static_key = ("daily-static", REPOSITORY_ID, selected["run_id"], selected["run_attempt"],
        digest(encoded(identity)))
    if not frozen or static_key not in cache:
        workflow_ci = _ci(api, workflow_sha)
        execution_ci = workflow_ci if identity["code_sha"] == workflow_sha else _ci(api, identity["code_sha"])
        tree = _tree(api, identity["code_sha"])
        require(_contents(api, identity["code_sha"], INTERFACE_PATH, cache, bound=1024) == INTERFACE,
            "DAILY_EXECUTION_PROTOCOL_UNSUPPORTED")
        version = {"schema": SELECTION_SCHEMA, "mode": approved["mode"], "code_tree": tree,
            "policy_commit": identity["runtime_version"]["policy_commit"] if frozen else head,
            "policy_sha256": digest(encoded(approved)), "protocol": PROTOCOL,
            "workflow_ci_run": workflow_ci, "execution_ci_run": execution_ci,
            "approval_ref": approved["pinned"]["approval_ref"] if approved["mode"] == "pinned" else None}
        if frozen:
            require(encoded(identity["runtime_version"]) == encoded(version), "DAILY_EXECUTION_STATIC_CHANGED")
        else:
            identity["runtime_version"] = version
        _identity(identity)
        _allows(approved, identity)
        static_key = ("daily-static", REPOSITORY_ID, selected["run_id"], selected["run_attempt"], digest(encoded(identity)))
        _remember(cache, static_key, True)
    selected.update(identity)
    # The static verification may take time. Final live checks prevent a stale
    # preflight receipt from enabling a job after an observed policy change.
    require_live_execution(api, selected, cache=cache)
    return selected


def verify_checkout(identity, *, root=Path(".")):
    """Check actual checked-out tracked code, not an environment's asserted SHA."""
    identity = _identity({key: identity.get(key) for key in ("workflow_sha", "code_sha", "runtime_version")}
        if isinstance(identity, dict) else identity)
    error = "DAILY_EXECUTION_CHECKOUT"
    try:
        root = Path(root).resolve(strict=True)
        clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        coordinates = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD", "HEAD^{tree}"],
            check=True, capture_output=True, timeout=10, env=clean_env).stdout.decode("ascii").splitlines()
        require(coordinates == [identity["code_sha"], identity["runtime_version"]["code_tree"]], error)
        subprocess.run(["git", "-C", str(root), "diff", "--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
            check=True, capture_output=True, timeout=10, env=clean_env)
        # Do not reject normal egg-info or generated cache metadata. Newly
        # importable modules and configuration, including ignored ones, cannot
        # acquire the approved tree's identity just by leaving tracked files clean.
        extra = subprocess.run(["git", "-C", str(root), "ls-files", "--others", "-z", "--", "src", "config"],
            check=True, capture_output=True, timeout=10, env=clean_env).stdout.decode("utf-8").split("\0")
        for name in filter(None, extra):
            candidate = Path(name)
            importable = candidate.suffix in {".py", ".pyi", ".so", ".pyd", ".pth", ".zip"}
            importable = importable or candidate.suffix in {".pyc", ".pyo"} and "__pycache__" not in candidate.parts
            require(not (candidate.parts[0] == "config" or importable), error)
        path = root / INTERFACE_PATH
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1024, error)
        require(_json(path.read_bytes(), error) == INTERFACE, error)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise RecordError(error) from None
