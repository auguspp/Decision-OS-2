"""Whole-research registered source plan/capture/export; never writes the records branch."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_sources import capture_plan, source_inputs, validate_plan, inspect_result
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.relay_retry import RetryPolicy


def load_plan(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    require(len(raw) <= 2 * 1024 * 1024, 'SOURCE_PLAN_TOO_LARGE')
    return _json(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('plan', 'capture', 'inspect', 'export'))
    parser.add_argument('--registry', type=Path, default=Path('docs/sources/relay-registry.json'))
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--public-rights-confirmed', action='store_true')
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--max-attempts', type=int, default=4)
    parser.add_argument('--max-wait-seconds', type=int, default=180)
    parser.add_argument('--run-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    try:
        if args.operation in ('inspect', 'export'):
            require(args.capture is not None and not args.live and args.plan is None, 'SOURCE_EXPORT_INPUT_INVALID')
            registry = RelayRegistry(args.registry)
            seen = set()
            # Local export only. Actual writer still validates SourceInput and public rights.
            with args.capture.open('rb') as source, args.output.open('x', encoding='utf-8') as output:
                found = False
                while raw := source.readline(16 * 1024 * 1024 + 1):
                    require(len(raw) <= 16 * 1024 * 1024, 'SOURCE_CAPTURE_LINE_TOO_LARGE')
                    item = _json(raw)
                    if args.operation == 'inspect':
                        output.write(encoded(inspect_result(item, registry)).decode())
                        found = True
                    else:
                        for value in source_inputs(item, registry):
                            identity = encoded(asdict(value))
                            if identity not in seen:
                                output.write(identity.decode())
                                seen.add(identity)
                                found = True
                require(found, 'SOURCE_NO_EXPORTABLE_CAPTURE')
            return 0
        require(args.plan is not None and args.capture is None, 'SOURCE_PLAN_REQUIRED')
        plan, registry = load_plan(args.plan), RelayRegistry(args.registry)
        checked = validate_plan(plan, registry)
        if args.operation == 'plan':
            require(not args.live, 'SOURCE_PLAN_IS_OFFLINE')
            with args.output.open('x', encoding='utf-8') as output:
                output.write(encoded(checked).decode())
            print(json.dumps({'requests': len(checked['requests']), 'network_requests': 0}))
            return 0
        require(args.live and args.public_rights_confirmed, 'SOURCE_CAPTURE_AUTHORIZATION_REQUIRED')
        policy = RetryPolicy(args.max_attempts, args.max_wait_seconds)
        failed = 0
        with args.output.open('x', encoding='utf-8') as output:
            for result in capture_plan(plan, registry, public_rights_confirmed=True,
                                       policy=policy, max_pages=args.max_pages, run_seconds=args.run_seconds):
                output.write(encoded(result).decode()); output.flush()
                failed += not (result.get('capture') or {}).get('complete', False)
        print(json.dumps({'failed_or_not_attempted': failed, 'records_written': False}))
        return 2 if failed else 0
    except (RecordError, OSError, ValueError, TypeError, KeyError):
        print('{"error":"SOURCE_PLAN_OR_CAPTURE_REJECTED","records_written":false}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
