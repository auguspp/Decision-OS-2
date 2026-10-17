"""Offline business-mix reading of an existing Relay JSONL; never acquire or save sources."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from decision_kernel.application.main_business import public_summary, read_main_business
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import RecordError, encoded, require

MAX_CAPTURE_BYTES = 32 * 1024 * 1024


def select_capture(path, api, code, period, dimension=None, *, registry):
    """No first/last selection. Equal cross-stage reuses share one original capture."""
    matches, consumed, count = [], 0, 0
    with Path(path).open('rb') as source:
        while raw := source.readline(16 * 1024 * 1024 + 1):
            consumed += len(raw); count += 1
            require(len(raw) <= 16 * 1024 * 1024 and consumed <= MAX_CAPTURE_BYTES and count <= 512,
                    'MAINBZ_CAPTURE_FILE_BOUND')
            row = _json(raw)
            require(isinstance(row, dict) and isinstance(row.get('use'), dict)
                    and isinstance(row['use'].get('parameters'), dict), 'MAINBZ_CAPTURE_FILE_ROW')
            use, params = row['use'], row['use']['parameters']
            if (use.get('api') == api and params.get('ts_code') == code and params.get('period') == period
                    and (dimension is None or params.get('type') == dimension)):
                view = inspect_result(row, registry)
                require(row.get('status') in (('captured', 'reused-in-this-batch') if view['complete'] else
                        ('capture-incomplete', 'prior-incomplete-in-this-batch')), 'MAINBZ_RESULT_STATUS')
                matches.append(row)
    require(matches, 'MAINBZ_SELECTED_CAPTURE_MISSING')
    require(all(m['use'].get('query_key') == matches[0]['use'].get('query_key')
                and m.get('capture') == matches[0].get('capture') for m in matches),
            'MAINBZ_MULTIPLE_CAPTURE_VERSIONS')
    return matches[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True, type=Path)
    parser.add_argument('--registry', type=Path, default=Path('docs/sources/relay-registry.json'))
    parser.add_argument('--ts-code', required=True)
    parser.add_argument('--period', required=True)
    parser.add_argument('--dimension', required=True, choices=('P', 'D', 'I'))
    parser.add_argument('--as-of', required=True)
    parser.add_argument('--with-income', action='store_true', help='Require one matching original income capture in the same file')
    parser.add_argument('--item', help='Exact returned business label; whole scope and arithmetic stay unchanged')
    parser.add_argument('--include-values', action='store_true', help='Explicit LOCAL detailed output; does not authorize publication')
    parser.add_argument('--output', required=True, type=Path, help='New file; default is a counts-only receipt')
    args = parser.parse_args(argv)
    try:
        registry = RelayRegistry(args.registry)
        original = select_capture(args.capture, 'fina_mainbz', args.ts_code, args.period, args.dimension, registry=registry)
        control, selection_error = None, None
        if args.with_income:
            try:
                control = select_capture(args.capture, 'income', args.ts_code, args.period, registry=registry)
            except RecordError as error:
                selection_error = str(error) if str(error) in {
                    'MAINBZ_SELECTED_CAPTURE_MISSING', 'MAINBZ_MULTIPLE_CAPTURE_VERSIONS'
                } else 'MAINBZ_CAPTURE_SELECTION_INVALID'
        view = read_main_business(original, registry, ts_code=args.ts_code,
            period=args.period, dimension=args.dimension, as_of=args.as_of, income_result=control, item=args.item,
            income_selection_error=selection_error)
        with args.output.open('xb') as output:
            output.write(encoded(view if args.include_values else public_summary(view)))
        return 0 if view['status'] == 'read' and view['income_reconciliation']['status'] in ('matched', 'not-supplied') else 2
    except (RecordError, OSError, ValueError, TypeError, KeyError, RecursionError):
        print('{"error":"MAIN_BUSINESS_INPUT_OR_OUTPUT_REJECTED"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
