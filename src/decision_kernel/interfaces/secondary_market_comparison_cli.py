"""Explicit local/offline dual-source price check, no provider or GitHub I/O."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from decision_kernel.observations.secondary_market_comparison import compare_retained_raw_market

MAX_FILE_BYTES = 128 * 1024


def _read(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("SECONDARY_SOURCE_LOCAL_INPUT_INVALID")
    return json.loads(path.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline FTShare / Relay raw OHLC diagnostic only")
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--ftshare", type=Path, required=True)
    parser.add_argument("--session", required=True)
    args = parser.parse_args(argv)
    result = compare_retained_raw_market(relay=_read(args.relay), ftshare=_read(args.ftshare),
                                         session=args.session)
    # The source prices, locator paths and private origin bytes are never printed.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result["comparison_status"] == "DIAGNOSTIC_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
