"""Explicit 24-hour news build on the existing store, and offline filtered reading."""
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
from decision_kernel.application.news_rolling import MAX_BYTES, read_rolling, rolling_news

REPOSITORY = "auguspp/Decision-OS-2"
WORKFLOW = REPOSITORY + "/.github/workflows/news-rolling.yml@refs/heads/main"


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
    require(inputs["code_sha"] == os.environ.get("GITHUB_SHA"), "NEWS_ROLL_CODE_CHANGED")
    require(inputs.get("save") in ("true", "false"), "NEWS_ROLL_SAVE_REQUIRED")
    saving = inputs["save"] == "true"
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
    require(set(plan) <= {"as_of", "batch_references", "predecessor"}
            and {"as_of", "batch_references"} <= set(plan), "NEWS_ROLL_PLAN_REQUIRED")
    binding = GitHubBinding.from_dict(_file(Path("config/store-binding.json"), 4096))
    require(binding.repository == REPOSITORY and binding.repository_id == 1420041744
            and binding.visibility == "public" and not binding.write_enabled, "NEWS_ROLL_BINDING_CHANGED")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    (output / "plan.json").write_bytes(encoded(plan))
    api = GitHubAPI.from_environment()
    reader = VersionedStore(GitHubStore(binding, api))
    value = rolling_news(reader.read_source, plan["batch_references"], as_of=plan["as_of"],
                         predecessor=plan.get("predecessor"))
    (output / "view.json").write_bytes(encoded(value))
    (output / "reading.json").write_bytes(encoded(read_rolling(value)))
    if saving:
        source = SourceInput(source_id="news-roll-" + inputs["submission_id"], kind="content",
                             title="24小时新闻观察 / " + value["as_of"],
                             origin="https://github.com/" + REPOSITORY + "/issues/48",
                             text=encoded(value).decode("utf-8"), custody="provided",
                             limitations=("DERIVED_NEWS_METADATA_NOT_ORIGINAL_ARTICLES",
                                          "OBSERVATION_TIME_NOT_PUBLICATION_TIME",
                                          "BOUNDED_SELECTION_NOT_COMPLETE_NEWS_COVERAGE"))
        command = {"operation": "put-source", "code_sha": inputs["code_sha"],
                   "submission_id": inputs["submission_id"], "expected_head": inputs.get("expected_head", ""),
                   "payload": pack_payload(encoded(asdict(source)))}
        # The existing writer retains its original input and handles CAS/unknown writes.
        run_command(binding, api=api, inputs=command, code_sha=inputs["code_sha"],
                    output=output / "result", run_id=os.environ["GITHUB_RUN_ID"], attempt="1")
    print(json.dumps({"status": "built-observation-window", "saved_requested": saving,
                      "window_hours": value["window_hours"], "rolling_sha256": value["rolling_sha256"],
                      "coverage": value["coverage"], "losses": value["losses"]}, ensure_ascii=False))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.news_rolling_cli")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="explicit native host only; no source collection")
    build.add_argument("--out", required=True, type=Path)
    read = sub.add_parser("read", help="offline, immutable rolling view and all source health")
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
