"""One native news artifact -> existing SourceInput/run_command. No new store or fetcher."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import html
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import sys
import zipfile
from urllib.parse import quote

from decision_kernel.domain.records import MAX_BYTES, RecordError, SourceInput, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding
from decision_kernel.interfaces.research_payload import json_object, pack_payload
from decision_kernel.interfaces.research_runtime import run_command
from decision_kernel.observations.news_windows import verify_reading

REPOSITORY = "auguspp/Decision-OS-2"
REPOSITORY_ID = 1420041744
MAX_ARCHIVE = 8 * 1024 * 1024


def verify_native(run, artifact, selection, *, workflow_path=".github/workflows/news-intake.yml",
                  artifact_prefix="dos2-news-"):
    """Native metadata is host-fetched; profiles are trusted code constants, not user input."""
    for name in ("source_run", "artifact_id"):
        require(isinstance(selection.get(name), str) and re.fullmatch(r"[1-9][0-9]{0,19}", selection[name]),
                "NEWS_NATIVE_ID_REQUIRED")
    checksum = selection.get("artifact_sha256", "")
    require(isinstance(checksum, str) and bool(re.fullmatch(r"[0-9a-f]{64}", checksum)),
            "NEWS_ARCHIVE_DIGEST_REQUIRED")
    require(run.get("id") == int(selection["source_run"])
            and run.get("repository", {}).get("id") == REPOSITORY_ID
            and run.get("head_repository", {}).get("id") == REPOSITORY_ID
            and run.get("path") == workflow_path
            and run.get("head_branch") == "main" and run.get("event") == "workflow_dispatch"
            and run.get("run_attempt") == 1 and run.get("status") == "completed"
            and run.get("conclusion") in ("success", "failure"), "NEWS_NATIVE_RUN_MISMATCH")
    require(isinstance(run.get("head_sha"), str) and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"]),
            "NEWS_NATIVE_CODE_INVALID")
    parent = artifact.get("workflow_run", {})
    require(artifact.get("id") == int(selection["artifact_id"])
            and artifact.get("name") == artifact_prefix + selection["source_run"] + "-1"
            and artifact.get("digest") == "sha256:" + checksum
            and artifact.get("expired") is False
            and type(artifact.get("size_in_bytes")) is int
            and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE
            and parent.get("id") == run["id"] and parent.get("head_sha") == run["head_sha"]
            and parent.get("repository_id") == REPOSITORY_ID
            and parent.get("head_repository_id") == REPOSITORY_ID, "NEWS_NATIVE_ARTIFACT_MISMATCH")


def source_from_archive(raw: bytes, run: dict, artifact: dict, selection: dict) -> SourceInput:
    verify_native(run, artifact, selection)
    require(isinstance(raw, bytes) and len(raw) == artifact["size_in_bytes"]
            and digest(raw) == selection["artifact_sha256"], "NEWS_ARCHIVE_BYTES_MISMATCH")
    try:
        with zipfile.ZipFile(BytesIO(raw)) as bundle:
            entries = bundle.infolist()
            require(len(entries) == 1 and entries[0].filename == "news-window.json"
                    and not entries[0].is_dir() and not entries[0].flag_bits & 1
                    and not stat.S_ISLNK(entries[0].external_attr >> 16)
                    and 0 < entries[0].file_size <= MAX_BYTES, "NEWS_ARCHIVE_MEMBERS_INVALID")
            with bundle.open(entries[0]) as stream:
                text = stream.read(MAX_BYTES + 1)
            require(len(text) == entries[0].file_size and len(text) <= MAX_BYTES,
                    "NEWS_ARCHIVE_EXPANSION_INVALID")
    except (zipfile.BadZipFile, RuntimeError, EOFError, OSError):
        raise RecordError("NEWS_ARCHIVE_INVALID") from None
    reading = verify_reading(json_object(text))
    # The exact generated metadata is retained, not a reconstruction of publisher HTTP bytes.
    source = SourceInput(
        source_id="news-window-" + selection["source_run"], kind="content",
        title="新闻观察元数据 / native run " + selection["source_run"],
        origin=f"https://github.com/{REPOSITORY}/actions/runs/{selection['source_run']}",
        text=text.decode("utf-8"), custody="provided", acquired_at=None,
        limitations=("PROVIDED_METADATA_PROJECTION_NOT_ORIGINAL_NEWS_BODY",
                     "PUBLISHER_TIME_AND_ECONOMIC_EVENTS_NOT_VERIFIED",
                     "SOURCE_FAILURES_AND_COVERAGE_LIMITS_RETAINED",
                     "OLD_OBSERVED_AT_IS_CAPTURE_START_NOT_RECEIPT_TIME",
                     "NATIVE_ARTIFACT_ID_" + selection["artifact_id"],
                     "NATIVE_ARCHIVE_SHA256_" + selection["artifact_sha256"],
                     "NATIVE_SOURCE_CODE_" + run["head_sha"],
                     "READING_SHA256_" + reading["reading_sha256"]))
    return source


def render_news(reading, *, origin):
    verify_reading(reading)
    def cell(value):
        text = html.escape(str(value), quote=False).replace("\n", " ")
        for char in "|[]!`\\":
            text = text.replace(char, "&#" + str(ord(char)) + ";")
        return text
    lines = ["# 已保存新闻观察", "", "来源批次：" + origin, "",
             "仅为来源观察，不是经过查证的经济事实、独立消息源计数或投资建议。",
             "标题/时间为提供方声明；下表中的缺口不会因其他来源成功而消失。", "",
             "## 本批来源与缺口", "", "| 来源 | 状态 | 本次观察数 |", "|---|---|---:|"]
    for item in reading["source_outcomes"]:
        lines.append("| " + " | ".join(cell(item.get(k)) for k in ("source", "status", "observations")) + " |")
    lines += ["", "## 新闻列表", "", "| 来源 | 标题 | 发布时间声明 | 原文定位 |", "|---|---|---|---|"]
    for row in reading["observations"]:
        url = row.get("url")
        link = "未取得逐篇原文链接" if not url else "[原文](" + quote(url, safe=":/?&=,%#@+-._~") + ")"
        lines.append("| " + " | ".join((cell(row["source_id"]), cell(row["title"]),
                     cell(row["published_claim"]["raw"]), link)) + " |")
    return "\n".join(lines) + "\n"


def _read(path, maximum=MAX_BYTES):
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    require(len(raw) <= maximum, "NEWS_CUSTODY_INPUT_BOUND")
    return raw


def _selection():
    return {"source_run": os.environ.get("NEWS_SOURCE_RUN", ""),
            "artifact_id": os.environ.get("NEWS_ARTIFACT_ID", ""),
            "artifact_sha256": os.environ.get("NEWS_ARTIFACT_SHA256", "")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verified native news archive to existing source custody")
    parser.add_argument("operation", choices=("preflight", "publish"))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(os.environ.get("GITHUB_REPOSITORY") == REPOSITORY
                and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == "refs/heads/main"
                and os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "NEWS_CUSTODY_HOST_MISMATCH")
        require(os.environ.get("GITHUB_SHA") == os.environ.get("EXPECTED_CODE")
                and bool(re.fullmatch(r"[0-9a-f]{40}", os.environ.get("EXPECTED_CODE", ""))),
                "NEWS_CUSTODY_CODE_CHANGED")
        require(os.environ.get("NEWS_METADATA_RIGHTS_CONFIRMED") == "true", "NEWS_METADATA_RIGHTS_REQUIRED")
        selection = _selection()
        run = json_object(_read(args.root / "run.json"))
        artifact = json_object(_read(args.root / "artifact.json"))
        verify_native(run, artifact, selection)
        if args.operation == "preflight":
            print(json.dumps({"status": "metadata-verified", **selection}))
            return 0
        source = source_from_archive(_read(args.root / "source.zip", MAX_ARCHIVE), run, artifact, selection)
        reading = json_object(source.text.encode("utf-8"))
        (args.root / "reading.md").write_text(render_news(reading, origin=source.origin), encoding="utf-8")
        inputs = {"operation": "put-source", "code_sha": os.environ["GITHUB_SHA"],
                  "submission_id": "news-window-" + selection["source_run"],
                  "expected_head": os.environ.get("EXPECTED_RECORDS_HEAD", ""),
                  "payload": pack_payload(encoded(asdict(source)))}
        binding = GitHubBinding.from_dict(json_object(_read(Path("config/store-binding.json"))))
        report = run_command(binding, api=GitHubAPI.from_environment(), inputs=inputs,
                             code_sha=os.environ["GITHUB_SHA"], output=args.root / "result",
                             run_id=os.environ.get("GITHUB_RUN_ID", ""), attempt="1")
        print(json.dumps({"status": report["status"], "write": report["write"],
                          "source_run": selection["source_run"],
                          "source_reference": report.get("result", {}).get("reference")}))
        return 0 if report["status"] == "completed" else 2
    except (RecordError, OSError, UnicodeError) as exc:
        code = str(exc) if isinstance(exc, RecordError) else "NEWS_CUSTODY_LOCAL_IO"
        print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
