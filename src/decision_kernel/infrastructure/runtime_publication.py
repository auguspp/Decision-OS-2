"""Explicit owner publication authority for this deployment, not source data.

Visibility, access, custody, publication and execution remain separate facts.
The checked-in authorization can be revoked without changing immutable markers
or old references. This module never opens a source, writes a Store or executes
an event. Callers still perform their original native run and final checks.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import os
from pathlib import Path
import re

from decision_kernel.domain.records import RecordError, require

AUTHORIZATION_FILE = Path("config/public-runtime.json")
REPOSITORY = "auguspp/Decision-OS-2"
REPOSITORY_ID = 1420041744
ANCHOR = "06588c36e5a9ac6010e0ca858a95ef7519ee8e45"
STORE_ID = "decision-os-runtime"
SCOPES = frozenset({"market", "sources", "research", "reading", "brief", "watch",
                    "backup", "handoff", "decision-preparation"})
_NATIVE_OPERATIONS = {
    "daily-market.yml": frozenset({"status", "update", "recover-late"}),
    "ashare-data.yml": frozenset({"status", "sync", "ftshare-status", "ftshare-sync"}),
}


def _object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "PUBLIC_RUNTIME_POLICY_INVALID")
        value[key] = item
    return value


def authorization():
    """Read only the fixed deployment file. Missing/disabled means no grant."""
    path = AUTHORIZATION_FILE
    if not path.exists() and not path.is_symlink():
        return None
    try:
        require(path.is_file() and not path.is_symlink(), "PUBLIC_RUNTIME_POLICY_INVALID")
        with path.open("rb") as stream:
            raw = stream.read(8193)
        require(len(raw) <= 8192, "PUBLIC_RUNTIME_POLICY_INVALID")
        value = json.loads(raw, object_pairs_hook=_object)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise RecordError("PUBLIC_RUNTIME_POLICY_INVALID") from None
    require(isinstance(value, dict) and set(value) == {
        "schema", "enabled", "repository", "repository_id", "branch", "anchor",
        "store_id", "authorization_ref", "scopes"}, "PUBLIC_RUNTIME_POLICY_INVALID")
    require(value["schema"] == "public-runtime-authorization-v1"
        and type(value["enabled"]) is bool
        and value["repository"] == REPOSITORY
        and type(value["repository_id"]) is int and value["repository_id"] == REPOSITORY_ID
        and value["branch"] == "records" and value["anchor"] == ANCHOR
        and value["store_id"] == STORE_ID, "PUBLIC_RUNTIME_POLICY_INVALID")
    basis = value["authorization_ref"]
    require(isinstance(basis, str) and re.fullmatch(
        r"https://github\.com/auguspp/Decision-OS-2/issues/164#issuecomment-[1-9][0-9]*", basis),
        "PUBLIC_RUNTIME_POLICY_INVALID")
    scopes = value["scopes"]
    require(isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes)
        and len(scopes) == len(set(scopes)) and set(scopes) <= SCOPES,
        "PUBLIC_RUNTIME_POLICY_INVALID")
    return value if value["enabled"] else None


def publication_basis(binding, scope):
    """Publication only for the exact existing, public-origin data binding."""
    from decision_kernel.infrastructure.github_store import GitHubBinding
    require(isinstance(scope, str) and scope in SCOPES, "PUBLIC_RUNTIME_SCOPE_INVALID")
    if not isinstance(binding, GitHubBinding) or not (
        binding.repository == REPOSITORY and type(binding.repository_id) is int
        and binding.repository_id == REPOSITORY_ID and binding.branch == "records"
        and binding.anchor == ANCHOR and binding.store_id == STORE_ID
        and binding.visibility == "public"):
        return None
    grant = authorization()
    return grant["authorization_ref"] if grant is not None and scope in grant["scopes"] else None


def native_publication_basis(selected, *, env=None, scope="market"):
    """Normal market hosts only. Old studies/repairs never inherit a new run."""
    env = os.environ if env is None else env
    if not isinstance(selected, dict) or "study" in selected or not isinstance(env, Mapping):
        return None
    workflow = env.get("GITHUB_WORKFLOW_REF", "")
    prefix = REPOSITORY + "/.github/workflows/"
    if not isinstance(workflow, str) or not workflow.startswith(prefix) or not workflow.endswith("@refs/heads/main"):
        return None
    name = workflow[len(prefix):-len("@refs/heads/main")]
    if selected.get("operation") not in _NATIVE_OPERATIONS.get(name, ()):
        return None
    sha = selected.get("code_sha")
    if not (env.get("GITHUB_REPOSITORY") == REPOSITORY
        and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
        and env.get("GITHUB_REF") == "refs/heads/main"
        and env.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "schedule"}
        and (env.get("GITHUB_EVENT_NAME") != "schedule" or name == "daily-market.yml")
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
        and env.get("GITHUB_SHA") == sha
        and str(selected.get("run_id", "")) == env.get("GITHUB_RUN_ID")):
        return None
    require(isinstance(scope, str) and scope in SCOPES, "PUBLIC_RUNTIME_SCOPE_INVALID")
    grant = authorization()
    required = {scope}
    if name == "daily-market.yml":
        required.update({"market", "reading", "brief", "research"})
        if selected["operation"] != "status":
            required.add("sources")
    return grant["authorization_ref"] if grant is not None and required <= set(grant["scopes"]) else None


def checked_output_visibility(backend, scope, error):
    """Check real visibility; never pretend that an authorized public repo is private."""
    visibility = backend.live_visibility()
    require(visibility == "private" or visibility == "public" and
        publication_basis(getattr(backend, "binding", None), scope) is not None, error)
    return visibility


def publication_scope(operation):
    if operation == "agenda" or isinstance(operation, str) and "watch" in operation:
        return "watch"
    if operation in {"reading-room", "industry-read"}:
        return "reading"
    if operation in {"save-hosted-handoff", "reconcile-hosted-handoff", "read-hosted-handoff", "latest-hosted-handoff"}:
        return "handoff"
    if operation in {"prepare-decision", "read-decision-preparation", "save-decision-preparation", "reconcile-decision-preparation"}:
        return "decision-preparation"
    return "research"


def publication_payload(operation, payload, binding):
    """Resolve only known publication fields; never alter research/source contents.

    The native host retains both the original payload hash and this effective
    one, plus the authorization reference. No fields indicating acceptance,
    trading, actor identity or new research execution are supplied here.
    """
    if not isinstance(payload, dict):
        return payload
    scope = publication_scope(operation)
    if publication_basis(binding, scope) is None:
        return payload
    fields = ()
    rights_envelopes = {
        "deliver", "reconcile-delivery", "put-artifact", "reconcile-artifact",
        "freeze-valuation", "reconcile-valuation", "save-odds", "reconcile-odds",
        "freeze-odds-assessment", "reconcile-odds-assessment", "save-probability-odds", "reconcile-probability-odds",
        "freeze-odds-watch", "reconcile-odds-watch", "save-odds-watch-check", "reconcile-odds-watch-check",
        "change-odds-watch-activity", "reconcile-odds-watch-activity", "save-odds-watch-activity-check", "reconcile-odds-watch-activity-check",
        "freeze-odds-watch-execution", "reconcile-odds-watch-execution-plan", "reserve-odds-watch-execution", "reconcile-odds-watch-execution-slot",
        "change-odds-watch-delivery", "reconcile-odds-watch-delivery",
        "save-decision-preparation", "reconcile-decision-preparation",
    }
    if operation == "agenda":
        fields = ("public_feedback_custody_confirmed",)
    elif operation == "reading-room":
        fields = ("public_research_custody_confirmed", "public_feedback_custody_confirmed",
                  "public_material_custody_confirmed", "public_sources_custody_confirmed")
    elif operation == "industry-read":
        fields = ("public_sources_custody_confirmed",)
    elif operation in rights_envelopes or operation in {"save-hosted-handoff", "reconcile-hosted-handoff"}:
        fields = ("rights_confirmed",)
    if not fields:
        return payload
    result = deepcopy(payload)
    for field in fields:
        result[field] = True
    if operation in rights_envelopes and not result.get("rights_basis"):
        result["rights_basis"] = publication_basis(binding, scope)
    return result


def main(argv=None, *, env=None, api_factory=None):
    """Source-free workflow publication check before capture and artifact upload."""
    import sys
    from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
    env = os.environ if env is None else env
    args = sys.argv[1:] if argv is None else argv
    try:
        require(isinstance(args, list) and len(args) == 2 and args[0] == "check"
            and args[1] in SCOPES, "PUBLIC_RUNTIME_CHECK_ARGUMENTS")
        require(isinstance(env, Mapping) and env.get("GITHUB_REPOSITORY") == REPOSITORY
            and env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID)
            and env.get("GITHUB_REF") == "refs/heads/main"
            and env.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "schedule"}
            and env.get("GITHUB_RUN_ATTEMPT") == "1", "PUBLIC_RUNTIME_NATIVE_CONTEXT")
        sha = env.get("GITHUB_SHA")
        require(isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha), "PUBLIC_RUNTIME_NATIVE_CONTEXT")
        token = env.get("DECISION_OS_STORE_TOKEN")
        require(isinstance(token, str) and bool(token), "PUBLIC_RUNTIME_TOKEN_REQUIRED")
        binding = GitHubBinding.from_dict(json.loads(Path("config/store-binding.json").read_bytes()))
        require(not binding.write_enabled and binding.repository == REPOSITORY
            and binding.repository_id == REPOSITORY_ID, "PUBLIC_RUNTIME_BINDING_REQUIRED")
        api = (GitHubAPI if api_factory is None else api_factory)(token)
        head = api.request("GET", "/repos/" + REPOSITORY + "/git/ref/heads/main")
        require(head.get("object", {}).get("sha") == sha, "PUBLIC_RUNTIME_MAIN_MOVED")
        visibility = checked_output_visibility(GitHubStore(binding, api), args[1], "PUBLIC_RUNTIME_NOT_AUTHORIZED")
        print(json.dumps({"status": "publication-authorized", "visibility": visibility,
            "scope": args[1], "source_requests": 0, "records_written": False}, sort_keys=True))
        return 0
    except Exception:
        # Never serialize an arbitrary source error, token, input path or traceback.
        print('{"status":"blocked","reason_code":"PUBLIC_RUNTIME_CHECK_FAILED","source_requests":0,"records_written":false}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
