"""Manual news source capture and offline read/locator export.

No implicit RSS refresh, model execution, research save, or GitHub data write.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.news_capture import capture_newsnow, capture_relay_news
from decision_kernel.observations.news_windows import source_locators, verify_reading, merge_readings

MAX_INPUT = 5 * 1024 * 1024


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as inp:
            raw = inp.read(MAX_INPUT + 1)
        require(0 < len(raw) <= MAX_INPUT, "NEWS_READ_BOUND")
        def unique(pairs):
            result = {}
            for k, v in pairs:
                require(k not in result, "NEWS_DUPLICATE_JSON_KEY")
                result[k] = v
            return result
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return verify_reading(value)
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise RecordError("NEWS_READING_UNAVAILABLE_OR_INVALID") from None


def _write(path: Path, value):
    raw = encoded(value)
    require(len(raw) <= MAX_INPUT, "NEWS_OUTPUT_TOO_LARGE")
    try:
        with path.open("xb") as out:
            out.write(raw)
    except OSError:
        raise RecordError("NEWS_OUTPUT_EXISTS_OR_UNWRITABLE") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.news_cli")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("capture-newsnow", "capture-relay"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--output", type=Path, required=True)
    relay = sub.choices["capture-relay"]
    relay.add_argument("--api", choices=("news", "major_news", "cctv_news"), required=True)
    relay.add_argument("--source", default=None)
    relay.add_argument("--start", default=None)
    relay.add_argument("--end", default=None)
    relay.add_argument("--date", default=None)
    read = sub.add_parser("read")
    read.add_argument("--window", type=Path, required=True)
    read.add_argument("--source")
    read.add_argument("--limit", type=int, default=40)
    merge = sub.add_parser("merge", help="merge explicit NewsNow/Relay windows; no network")
    merge.add_argument("--windows", type=Path, nargs="+", required=True)
    merge.add_argument("--output", type=Path, required=True)
    loc = sub.add_parser("export-locators")
    loc.add_argument("--window", type=Path, required=True)
    loc.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action in ("capture-newsnow", "capture-relay"):
            when = datetime.now(timezone.utc).isoformat()
            if args.action == "capture-newsnow":
                result = capture_newsnow(observed_at=when)
            else:
                result = capture_relay_news(args.api, source=args.source,
                                            start=args.start, end=args.end,
                                            date=args.date, observed_at=when)
            _write(args.output, result)
            print(json.dumps({"status": "captured" if all(x["status"] == "captured"
                    for x in result["source_outcomes"]) else "partial",
                    "observations": len(result["observations"]),
                    "source_outcomes": result["source_outcomes"],
                    "output": str(args.output)}, ensure_ascii=False))
            return 0 if all(x["status"] == "captured" for x in result["source_outcomes"]) else 2
        if args.action == "merge":
            result = merge_readings([_read(path) for path in args.windows])
            _write(args.output, result)
            print(json.dumps({"status": "merged", "source_windows": len(args.windows),
                              "observations": len(result["observations"]),
                              "complete_news_coverage": False}))
            return 0
        reading = _read(args.window)
        if args.action == "read":
            require(0 < args.limit <= 200, "NEWS_READ_LIMIT")
            rows = reading["observations"]
            if args.source:
                rows = [x for x in rows if x["source_id"] == args.source]
            print(json.dumps({"status": "observation-only", "rows": rows[:args.limit],
                              "visible": min(len(rows), args.limit),
                              "selected_count": len(rows),
                              "complete_news_coverage": False}, ensure_ascii=False))
            return 0
        inputs = [asdict(x) for x in source_locators(reading)]
        _write(args.output, {"schema": "dos2-news-locators-v1", "items": inputs,
                             "write_performed": False, "rights_verified": False})
        print(json.dumps({"source_locator_candidates": len(inputs), "write_performed": False}))
        return 0
    except RecordError as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
