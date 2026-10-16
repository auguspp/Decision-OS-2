"""Read approved local Relay captures as US index context; no fetching or custody."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from decision_kernel.application.global_index_inputs import US_INDEX_READING_SCHEMA, US_INDEX_REQUIRED
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.domain.records import require

MAX_INPUT = 8 * 1024 * 1024
REQUIRED = US_INDEX_REQUIRED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--entity')
    args = parser.parse_args(argv)
    try:
        with args.input.open('rb') as stream:
            raw = stream.read(MAX_INPUT + 1)
        require(len(raw) <= MAX_INPUT, 'GLOBAL_INDEX_INPUT_SIZE')
        data = _json(raw)
        require(isinstance(data, dict) and REQUIRED <= data.keys()
                and data.keys() <= REQUIRED | {'horizons'}, 'GLOBAL_INDEX_INPUT')
        if 'horizons' in data:
            require(isinstance(data['horizons'], list), 'GLOBAL_INDEX_HORIZONS')
        result = read_market_payload({"schema": US_INDEX_READING_SCHEMA, **data}, RelayRegistry(args.registry),
                                     offset=args.offset, limit=args.limit, entity_id=args.entity)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        # Never print raw capture values, credentials, file contents or a traceback.
        print('{"status":"rejected","reason_code":"GLOBAL_INDEX_INPUT_REJECTED"}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
