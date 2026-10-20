"""Explicit 24-hour news build/read on the existing store; never source collection."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, encoded, identifier, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.research_payload import json_object, pack_payload
from decision_kernel.interfaces.research_runtime import run_command
from decision_kernel.application.news_rolling import MAX_BYTES, _read_verified, read_rolling, reference
from decision_kernel.application.news_rolling_index import SCHEMA as INDEX_SCHEMA, open_index, read_index, rolling_index

REPOSITORY = "auguspp/Decision-OS-2"
WORKFLOW = REPOSITORY + "/.github/workflows/news-rolling.yml@refs/heads/main"
MAX_SOURCE_CACHE_BYTES = 8 * 1024 * 1024


class _SourceReads:
    """One host operation only: reuse exact verified bytes, never mutable heads.

    Store validation owns admission. Consumers still verify manifests and rebuild
    the logical index. Serialized copies prevent a caller from poisoning later
    reads; failures are never memoized. An exhausted cache falls back to the same
    bounded reader, not dropped sources or a fresh/reset API request budget.
    """
    def __init__(self, reader, *, fail_closed=False):
        self._reader, self._cache = reader, {}
        self.serialized_bytes = self.reads = self.hits = 0
        self._fail_closed, self._terminal = fail_closed, None

    def raise_if_terminal(self):
        if self._terminal is not None:
            raise self._terminal

    def __call__(self, ref):
        self.raise_if_terminal()  # Before parsing a new Ref or using cached bytes.
        try:
            exact = reference(ref)
            key = encoded(exact)
            raw = self._cache.get(key)
            if raw is None:
                raw = encoded(_read_verified(self._reader, exact))
                self.reads += 1
                size = len(key) + len(raw)
                if self.serialized_bytes + size <= MAX_SOURCE_CACHE_BYTES:
                    self._cache[key] = raw
                    self.serialized_bytes += size
            else:
                self.hits += 1
            return json_object(raw)
        except RecordError as error:
            if self._fail_closed:
                from decision_kernel.application.brief_discovery import operation_failure
                if operation_failure(error):
                    self._terminal = error
            raise

    def usage(self):
        return {"verified_source_reads": self.reads, "reused_source_reads": self.hits,
                "cached_references": len(self._cache), "serialized_bytes": self.serialized_bytes,
                "byte_limit": MAX_SOURCE_CACHE_BYTES, "scope": "one-explicit-news-operation"}



def _file(path, maximum):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "NEWS_ROLL_INPUT_BOUND")
    return json_object(raw)


def build_on_host(output):
    require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
            and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_WORKFLOW_REF") == WORKFLOW
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "NEWS_ROLL_MANUAL_HOST_REQUIRED")
    event = _file(Path(os.environ["GITHUB_EVENT_PATH"]), 2 * 1024 * 1024)
    inputs = event["inputs"]
    operation = inputs.get("operation", "build")
    if operation == "latest-desk":
        # This read-only entry alone can pin the trusted native checkout itself.
        from decision_kernel.domain.records import SHA
        actual = os.environ.get("GITHUB_SHA")
        require(isinstance(actual, str) and SHA.fullmatch(actual)
                and inputs.get("code_sha", "") in ("", actual), "NEWS_ROLL_CODE_CHANGED")
        inputs = {**inputs, "code_sha": actual}
    else:
        require(inputs["code_sha"] == os.environ.get("GITHUB_SHA"), "NEWS_ROLL_CODE_CHANGED")
    require(inputs.get("save") in ("true", "false"), "NEWS_ROLL_SAVE_REQUIRED")
    if operation == "latest-desk":
        from decision_kernel.interfaces.news_delivery_cli import run_latest_desk
        return run_latest_desk(output, inputs)
    if operation in ("desk", "read-desk"):
        from decision_kernel.interfaces.news_delivery_cli import run_desk
        return run_desk(output, inputs)
    require(operation in ("build", "read"), "NEWS_ROLL_OPERATION_REQUIRED")
    saving = inputs["save"] == "true"
    require(not saving or operation == "build", "NEWS_ROLL_READ_CANNOT_SAVE")
    require(os.environ.get("DOS2_NEWS_MODE") == ("write" if saving else "read"),
            "NEWS_ROLL_JOB_PERMISSION_MISMATCH")
    if saving:
        require(inputs.get("public_metadata_custody_confirmed") == "true", "NEWS_ROLL_RIGHTS_REQUIRED")
        identifier(inputs.get("submission_id"))
        identifier("news-roll-" + inputs["submission_id"])
    else:
        require(not inputs.get("submission_id") and not inputs.get("expected_head"),
                "NEWS_ROLL_UNEXPECTED_WRITE_FIELDS")
    text = inputs["plan"]
    require(isinstance(text, str) and len(text.encode("utf-8")) <= 60 * 1024, "NEWS_ROLL_PLAN_BOUND")
    plan = json_object(text.encode("utf-8"))
    if operation == "build":
        require(set(plan) <= {"as_of", "batch_references", "predecessor"}
                and {"as_of", "batch_references"} <= set(plan), "NEWS_ROLL_PLAN_REQUIRED")
    else:
        require("reference" in plan and set(plan) <= {"reference", "source", "query", "limit", "cursor"},
                "NEWS_ROLL_EXACT_INDEX_REQUIRED")
    binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json"), 4096))
    require(binding.repository == REPOSITORY and binding.repository_id == 1420041744
            and binding.visibility == "public" and not binding.write_enabled, "NEWS_ROLL_BINDING_CHANGED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "plan.json").write_bytes(encoded(plan))
    api = GitHubAPI.from_environment()
    reader = VersionedStore(GitHubStore(binding, api))
    source_reads = _SourceReads(reader.read_source)
    if operation == "build":
        value = rolling_index(source_reads, plan["batch_references"], as_of=plan["as_of"],
                              predecessor=plan.get("predecessor"))
        reading = read_index(source_reads, value)
    else:
        value = open_index(source_reads, plan["reference"])
        reading = read_index(source_reads, value, **{k: v for k, v in plan.items() if k != "reference"})
        reading["snapshot_reference"] = plan["reference"]
    # Revalidate mutable visibility/ownership/binding after all cached replay and
    # before exporting results. The original writer also rechecks permissions/CAS.
    reader.backend.head()
    (output / "view.json").write_bytes(encoded(value))
    (output / "reading.json").write_bytes(encoded(reading))
    if saving:
        source = SourceInput(source_id="news-roll-" + inputs["submission_id"], kind="content",
                             title="24小时新闻观察 / " + value["as_of"],
                             origin="https://github.com/" + REPOSITORY + "/issues/48",
                             text=encoded(value).decode("utf-8"), custody="provided",
                             limitations=("DERIVED_NEWS_METADATA_NOT_ORIGINAL_ARTICLES",
                                          "OBSERVATION_TIME_NOT_PUBLICATION_TIME",
                                          "BOUNDED_SELECTION_NOT_COMPLETE_NEWS_COVERAGE",
                                          "DERIVED_NEWS_INDEX_REQUIRES_RETAINED_SOURCE_VERSIONS"))
        command = {"operation": "put-source", "code_sha": inputs["code_sha"],
                   "submission_id": inputs["submission_id"], "expected_head": inputs.get("expected_head", ""),
                   "payload": pack_payload(encoded(asdict(source)))}
        # The existing writer retains its original input and handles CAS/unknown writes.
        report = run_command(binding, api=api, inputs=command, code_sha=inputs["code_sha"],
                             output=output / "result", run_id=os.environ["GITHUB_RUN_ID"], attempt="1")
        require(report["status"] == "completed" and report["write"] in ("published", "replayed"),
                "NEWS_ROLL_SAVE_NOT_CONFIRMED")
    # Safe measured counters; no source text, credentials, or cached bytes.
    usage = {**source_reads.usage(), "storage_api_requests": getattr(api, "_requests", None),
             "binding_rechecked": True}
    (output / "read-usage.json").write_bytes(encoded(usage))
    print(json.dumps({"status": "read-exact-index" if operation == "read" else "built-observation-index",
                      "saved_requested": saving, "window_hours": value["window_hours"],
                      "index_sha256": value["index_sha256"], "logical_sha256": value["logical_sha256"],
                      "entry_count": value["entry_count"], "coverage": value["coverage"],
                      "losses": value["losses"]}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.news_rolling_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="explicit native build/read host only; no source collection")
    build.add_argument("--out", required=True, type=Path)
    read = sub.add_parser("read", help="offline legacy inline view; compact indexes need exact source replay")
    read.add_argument("--window", required=True, type=Path)
    read.add_argument("--source")
    read.add_argument("--query", default="")
    read.add_argument("--limit", type=int, default=40)
    read.add_argument("--cursor", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            return build_on_host(args.out)
        value = _file(args.window, MAX_BYTES)
        require(value.get("schema") != INDEX_SCHEMA, "NEWS_INDEX_REQUIRES_EXACT_SOURCE_READER")
        cursor = _file(args.cursor, 4096) if args.cursor else None
        print(json.dumps(read_rolling(value, source=args.source, query=args.query,
                                      limit=args.limit, cursor=cursor), ensure_ascii=False))
        return 0
    except (RecordError, OSError, KeyError, TypeError, UnicodeError) as exc:
        code = str(exc) if isinstance(exc, RecordError) else "NEWS_ROLL_LOCAL_OR_INPUT_FAILURE"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
