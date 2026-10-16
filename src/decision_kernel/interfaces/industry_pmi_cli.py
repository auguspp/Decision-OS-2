"""Offline manufacturing PMI plan/read. Capture/export remain with relay_research_cli."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from decision_kernel.application.industry_pmi import pmi_source_plan, read_pmi_context
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.domain.records import RecordError, encoded, require

MAX_CAPTURE_BYTES = 16 * 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=Path('docs/sources/relay-registry.json'))
    operations = parser.add_subparsers(dest='operation', required=True)
    plan = operations.add_parser('plan', help='Write one ordinary registered-source plan; no request')
    plan.add_argument('--case-id', required=True)
    read = operations.add_parser('read', help='Read exactly one capture result JSON/JSONL record')
    read.add_argument('--capture', type=Path, required=True)
    read.add_argument('--as-of', required=True, help='Explicit timezone-aware reading cutoff')
    for operation in (plan, read):
        operation.add_argument('--start-month', required=True)
        operation.add_argument('--end-month', required=True)
        operation.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        registry = RelayRegistry(args.registry)
        scope = {'start_month': args.start_month, 'end_month': args.end_month}
        if args.operation == 'plan':
            result = pmi_source_plan(registry, case_id=args.case_id, **scope)
        else:
            with args.capture.open('rb') as stream:
                raw = stream.read(MAX_CAPTURE_BYTES + 1)
            require(len(raw) <= MAX_CAPTURE_BYTES, 'PMI_CAPTURE_TOO_LARGE')
            result = read_pmi_context(_json(raw), registry, as_of=args.as_of, **scope)
        # Validate/serialize before opening the destination; never overwrite a prior result.
        raw = encoded(result)
        with args.output.open('xb') as stream:
            stream.write(raw)
        print(encoded({'operation': args.operation, 'network_requests': 0, 'records_written': False,
                       'status': result.get('status', 'planned')}).decode(), end='')
        return 0
    except (RecordError, OSError, ValueError, TypeError, KeyError, RecursionError):
        print('{"error":"PMI_INPUT_OR_OUTPUT_REJECTED","network_requests":0,"records_written":false}',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
