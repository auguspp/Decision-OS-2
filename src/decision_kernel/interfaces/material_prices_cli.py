"""Finite official price check and offline replay; no automatic research or alerts."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.application.material_prices import dataset_inputs, replay, seal, source_input, verify
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.material_prices_http import capture
from decision_kernel.interfaces.research_payload import json_object


def load(path):
    with path.open("rb") as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, "MATERIAL_INPUT_BOUND")
    return json_object(raw)


def restore(directory):
    value = verify(load(directory / "capture.json"))
    originals = {}
    for index, receipt in enumerate(value["report"]["receipts"]):
        if "response_sha256" in receipt:
            with (directory / ("response-" + str(index) + ".body")).open("rb") as stream:
                originals[receipt["url"]] = stream.read(4 * 1024 * 1024 + 1)
    replay(value, originals)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--out", type=Path, required=True)
    collect.add_argument("--predecessor", type=Path, help="Exact previously retained capture directory")
    read = commands.add_parser("read")
    read.add_argument("--capture", type=Path, required=True)
    read.add_argument("--subject-id")
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            require(not args.out.exists(), "MATERIAL_OUTPUT_EXISTS")
            prior = restore(args.predecessor) if args.predecessor else None
            report, originals = capture(predecessor=prior["report"] if prior else None)
            value = seal(report, predecessor=prior)
            replay(value, originals)
            args.out.mkdir(parents=True, exist_ok=False)
            for index, receipt in enumerate(report["receipts"]):
                if receipt["url"] in originals:
                    (args.out / ("response-" + str(index) + ".body")).write_bytes(originals[receipt["url"]])
            (args.out / "capture.json").write_bytes(encoded(value))
            (args.out / "source-input.json").write_bytes(encoded(asdict(source_input(
                value, "material-monitor-" + value["capture_sha256"][:24]))))
            for index, source in enumerate(dataset_inputs(value)):
                (args.out / ("dataset-input-" + str(index) + ".json")).write_bytes(encoded(asdict(source)))
            restore(args.out)
        else:
            value = restore(args.capture)
            report = value["report"]
        selected = [r for r in report["rows"] if not getattr(args, "subject_id", None)
                    or r["subject_id"] == args.subject_id]
        print(json.dumps({**{k: v for k, v in report.items() if k not in ("rows", "releases")},
                          "capture_sha256": value["capture_sha256"], "rows": selected},
                         ensure_ascii=False))
        return 2 if report["status"] == "partial-or-unavailable" else 0
    except (RecordError, OSError, KeyError, TypeError, ValueError, OverflowError) as exc:
        code = str(exc) if isinstance(exc, RecordError) else "MATERIAL_INPUT_OR_LOCAL_FAILURE"
        print(json.dumps({"status": "failed", "error_code": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
