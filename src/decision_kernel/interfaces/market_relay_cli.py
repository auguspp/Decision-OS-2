"""Read retained Relay market results locally; no collection or custody write."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces.market_cli import MAX_INPUT_BYTES, _object, _constant
from decision_kernel.observations.market_daily import MarketInputError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
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
        result = read_market_payload(payload, RelayRegistry(args.registry),
            offset=args.offset, limit=args.limit, entity_id=args.entity_id)
    except OSError:
        error = "INPUT_UNREADABLE"
    except (MarketInputError, RecordError) as exc:
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
