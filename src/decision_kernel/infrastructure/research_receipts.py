"""Verify supplied original Actions archives against live, bound GitHub metadata.

No caller URL is followed, no source credential is obtained, and no write or
workflow dispatch is exposed. Existing GitHubAPI and native gh own GitHub I/O.
"""
from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from io import BytesIO
import zipfile

from decision_kernel.domain.records import RecordError, SHA, digest, require, source_time
from decision_kernel.infrastructure.github_store import GitHubBinding, _json

MAX_ARCHIVE = 1024 * 1024
MAX_EXPANDED = 8 * 1024 * 1024
WORKFLOWS = {
    ".github/workflows/research-runtime.yml": ("pool", "dos2-research", {"inputs.json", "report.json"}),
    ".github/workflows/relay-source-runtime.yml": ("relay", "dos2-relay-source",
        {"request.json", "result.json", "source-plan.json", "plan.json", "capture.jsonl"}),
}


def proof_bytes(proof: dict) -> bytes | None:
    require(isinstance(proof, dict) and {"run_id", "run_attempt", "artifact_id"} <= set(proof)
            <= {"run_id", "run_attempt", "artifact_id", "archive_base64"}, "SOURCE_USE_PROOF_SHAPE")
    require(all(type(proof[k]) is int and 0 < proof[k] < 10**15
                for k in ("run_id", "run_attempt", "artifact_id")), "SOURCE_USE_PROOF_ID")
    if "archive_base64" not in proof:
        return None
    value = proof["archive_base64"]
    require(isinstance(value, str) and 0 < len(value) <= 4 * ((MAX_ARCHIVE + 2) // 3),
            "SOURCE_USE_ARCHIVE_LIMIT")
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError:
        raise RecordError("SOURCE_USE_ARCHIVE_ENCODING") from None
    require(0 < len(raw) <= MAX_ARCHIVE, "SOURCE_USE_ARCHIVE_LIMIT")
    return raw


class NativeResearchReceipts:
    def __init__(self, binding: GitHubBinding, api, *, archive_loader=None):
        require(isinstance(binding, GitHubBinding) and binding.visibility == "public",
                "SOURCE_USE_PUBLIC_BINDING_REQUIRED")
        self.binding, self.api, self.archive_loader = binding, api, archive_loader

    def read(self, proof: dict) -> dict:
        return self._read(proof, market=False)

    def read_market(self, proof: dict) -> dict:
        """Operational market receipts only; never admitted as research captures."""
        return self._read(proof, market=True)

    def _read(self, proof: dict, *, market: bool) -> dict:
        workflows = ({".github/workflows/market-source-check.yml":
            ("market", "dos2-market-check", {"result.json"})} if market else WORKFLOWS)
        raw = proof_bytes(proof)
        base = "/repos/" + self.binding.repository
        def get(path):
            return self.api.request("GET", base + path)
        # Never accept a repository or native metadata object from the proof.
        repo = get("")
        require(isinstance(repo, dict) and repo.get("id") == self.binding.repository_id and repo.get("private") is False
                and repo.get("full_name") == self.binding.repository, "SOURCE_USE_REPOSITORY_MISMATCH")
        run_id, attempt, aid = (proof[k] for k in ("run_id", "run_attempt", "artifact_id"))
        run = get(f"/actions/runs/{run_id}/attempts/{attempt}")
        meta = get(f"/actions/artifacts/{aid}")
        require(isinstance(run, dict) and run.get("id") == run_id and run.get("run_attempt") == attempt
                and run.get("status") == "completed" and run.get("event") == "workflow_dispatch"
                and run.get("head_branch") == "main" and run.get("path") in workflows
                and isinstance(run.get("head_sha"), str) and SHA.fullmatch(run["head_sha"])
                and isinstance(run.get("repository"), dict)
                and run["repository"].get("id") == self.binding.repository_id,
                "SOURCE_USE_RUN_MISMATCH")
        source_time(run.get("run_started_at"))
        require(isinstance(run.get("run_started_at"), str) and "T" in run["run_started_at"],
                "SOURCE_USE_RUN_TIME_REQUIRED")
        family, prefix, allowed = workflows[run["path"]]
        require(isinstance(meta, dict) and meta.get("id") == aid and meta.get("name") == f"{prefix}-{run_id}-{attempt}"
                and meta.get("expired") is False and type(meta.get("size_in_bytes")) is int
                and 0 < meta["size_in_bytes"] <= MAX_ARCHIVE
                and isinstance(meta.get("workflow_run"), dict)
                and meta["workflow_run"].get("id") == run_id
                and meta["workflow_run"].get("repository_id") == self.binding.repository_id
                and meta["workflow_run"].get("head_repository_id") == self.binding.repository_id
                and meta["workflow_run"].get("head_sha") == run["head_sha"], "SOURCE_USE_ARTIFACT_MISMATCH")
        if raw is None:
            require(self.archive_loader is not None, "SOURCE_USE_ARCHIVE_READER_REQUIRED")
            raw = self.archive_loader(self.binding.repository, aid)
        require(isinstance(raw, bytes) and len(raw) == meta["size_in_bytes"]
                and meta.get("digest") == "sha256:" + digest(raw), "SOURCE_USE_ARTIFACT_DIGEST_MISMATCH")
        jobs = get(f"/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
        require(isinstance(jobs, dict), "SOURCE_USE_JOBS_INCOMPLETE")
        rows = jobs.get("jobs")
        require(isinstance(rows, list) and jobs.get("total_count") == len(rows) and 0 < len(rows) <= 100
                and all(isinstance(j, dict) and j.get("run_id") == run_id and j.get("status") == "completed" for j in rows),
                "SOURCE_USE_JOBS_INCOMPLETE")
        by_name = {j["name"]: j for j in rows}
        require(len(by_name) == len(rows), "SOURCE_USE_JOBS_AMBIGUOUS")
        if family == "pool":
            require(run.get("conclusion") == "success", "SOURCE_USE_READ_RUN_NOT_SUCCESSFUL")
            require(set(by_name) == {"read", "write"} and by_name["write"].get("conclusion") == "skipped",
                    "SOURCE_USE_NOT_A_READ_RECEIPT")
        elif family == "market":
            require(set(by_name) == {"check"}, "SOURCE_USE_NOT_A_MARKET_RECEIPT")
        else:
            require(set(by_name) == {"source"}, "SOURCE_USE_NOT_A_SOURCE_RECEIPT")
        try:
            with zipfile.ZipFile(BytesIO(raw)) as archive:
                infos = archive.infolist()
                names = [i.filename for i in infos]
                require(names and len(names) == len(set(names)) and set(names) <= allowed
                        and all(not i.is_dir() and not i.flag_bits & 1
                                and i.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                                and (i.external_attr >> 16) & 0o170000 != 0o120000 for i in infos)
                        and sum(i.file_size for i in infos) <= MAX_EXPANDED, "SOURCE_USE_UNSAFE_ARCHIVE")
                require(archive.testzip() is None, "SOURCE_USE_ARCHIVE_CRC")
                docs = {}
                for name in names:
                    content = archive.read(name)
                    docs[name] = ([_json(line) for line in content.splitlines() if line.strip()]
                                  if name.endswith(".jsonl") else _json(content))
        except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError):
            raise RecordError("SOURCE_USE_ARCHIVE_INVALID") from None
        require(({"result.json"} if family == "market" else {"inputs.json", "report.json"} if family == "pool" else
                 {"request.json", "result.json", "source-plan.json", "plan.json"}) <= set(docs),
                "SOURCE_USE_RECEIPT_FILES_MISSING")
        return {"family": family, "documents": docs, "jobs": by_name,
                "identity": {"repository": self.binding.repository, "run_id": run_id,
                    "run_attempt": attempt, "artifact_id": aid, "artifact_sha256": digest(raw),
                    "code_sha": run["head_sha"], "workflow": run["path"], "conclusion": run.get("conclusion"),
                    "started_at":run["run_started_at"]},
                "verification": "native-artifact-digest-and-exact-run-attempt"}


def download_archive(repository: str, artifact_id: int, *, token: str | None = None) -> bytes:
    """Same native gh artifact read used by CI; isolated dedicated read credential.

    Endpoint/ID come from the validated binding and metadata, not a payload URL.
    No shell, user gh config, engineering token fallback, retries or dispatch.
    """
    from decision_kernel.infrastructure.github_store import GitHubAPI
    # Explicit callers may supply a short-lived actions:read job credential.
    # The original default remains dedicated-store-only, never an implicit fallback.
    token = os.environ.get("DECISION_OS_STORE_TOKEN", "") if token is None else token
    GitHubAPI(token)  # Apply the existing dedicated-credential validation.
    with TemporaryDirectory(prefix="dos2-gh-read-") as home, TemporaryFile() as output:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": home,
               "GH_CONFIG_DIR": str(Path(home) / "config"), "GH_TOKEN": token,
               "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "LANG": "C.UTF-8"}
        try:
            result = subprocess.run(["gh", "api", "--method", "GET",
                f"repos/{repository}/actions/artifacts/{artifact_id}/zip"], env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RecordError("SOURCE_USE_ARTIFACT_DOWNLOAD_UNAVAILABLE") from None
        require(result.returncode == 0, "SOURCE_USE_ARTIFACT_DOWNLOAD_FAILED")
        output.seek(0)
        raw = output.read(MAX_ARCHIVE + 1)
        require(0 < len(raw) <= MAX_ARCHIVE, "SOURCE_USE_ARCHIVE_LIMIT")
        return raw
