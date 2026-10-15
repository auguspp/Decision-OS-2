"""Installation information only; no network, persistent writes or task execution."""
from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
import json
import platform
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision-os", description="Decision OS 2.0 installation entry")
    parser.add_argument("--version", action="store_true", help="show installed distribution version")
    commands = parser.add_subparsers(dest="command")
    info = commands.add_parser("info", help="show installation metadata, not runtime health")
    info.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not args.version and args.command is None:
        parser.print_help()
        return 0
    try:
        installed = version("decision-os-2")
    except PackageNotFoundError:
        parser.error("decision-os-2 is not installed; install the project before using this entry")
    if args.version:
        print(f"decision-os {installed}")
    else:
        data = {"distribution": "decision-os-2", "version": installed,
                "python": platform.python_version(), "import_package": "decision_kernel"}
        if args.as_json:
            print(json.dumps(data, sort_keys=True))
        else:
            print("\n".join(f"{key}: {value}" for key, value in data.items()))
    return 0
