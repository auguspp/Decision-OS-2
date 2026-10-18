"""Explicit local/offline dual-source price check, no provider or GitHub I/O."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from decision_kernel.observations.secondary_market_comparison import PURPOSES, compare_retained_raw_market

MAX_FILE_BYTES = 128 * 1024


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("SECONDARY_SOURCE_DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("SECONDARY_SOURCE_NONFINITE_JSON")


def _read(path: Path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents) or not path.is_file():
        raise ValueError("SECONDARY_SOURCE_LOCAL_INPUT_INVALID")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("SECONDARY_SOURCE_LOCAL_INPUT_INVALID")
    return json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline FTShare / Relay raw price diagnostic only")
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--ftshare", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--purpose", choices=PURPOSES, default="trading-rows")
    args = parser.parse_args(argv)
    try:
        result = compare_retained_raw_market(relay=_read(args.relay), ftshare=_read(args.ftshare),
                                            session=args.session, purpose=args.purpose)
    except (ValueError, OSError, ArithmeticError, RecursionError):
        # No arbitrary source value, filesystem path, or traceback in diagnostics.
        print('{"status":"rejected","reason_code":"SECONDARY_SOURCE_INPUT_REJECTED"}', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")))
    return 0 if result["comparison_status"] == "DIAGNOSTIC_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
