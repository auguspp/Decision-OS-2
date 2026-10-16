"""Read a bounded local market snapshot. No network, credentials or store writes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from decision_kernel.application.market_radar import read_market_surface
from decision_kernel.observations.market_daily import MarketInputError

MAX_INPUT_BYTES = 2 * 1024 * 1024


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MarketInputError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_value):
    raise MarketInputError("NONFINITE_JSON_VALUE")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--entity-id")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise MarketInputError("INPUT_TOO_LARGE")
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
        result = read_market_surface(payload, offset=args.offset, limit=args.limit, entity_id=args.entity_id)
    except OSError:
        error = "INPUT_UNREADABLE"
    except MarketInputError as exc:
        error = str(exc)
    except (UnicodeError, ValueError, RecursionError):
        error = "INPUT_JSON_INVALID"
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
