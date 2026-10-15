"""Read local Relay interface registrations. No network, credentials, or dispatch."""
import argparse
import json
from pathlib import Path
import sys

from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, required=True)
    parser.add_argument('--api')
    parser.add_argument('--group')
    parser.add_argument('--module', help='consumer issue, for example #12')
    parser.add_argument('--summary', action='store_true', help='show snapshot counts and manual differences')
    status = parser.add_mutually_exclusive_group()
    status.add_argument('--enabled', dest='enabled', action='store_true', default=None)
    status.add_argument('--disabled', dest='enabled', action='store_false')
    args = parser.parse_args(argv)
    try:
        if args.api and (args.group or args.module or args.summary or args.enabled is not None) or args.summary and (args.group or args.module or args.enabled is not None):
            raise RecordError('RELAY_REGISTRY_ONE_SELECTOR')
        reader = RelayRegistry(args.registry)
        result = reader.summary() if args.summary else (reader.lookup(args.api) if args.api else
            reader.select(group=args.group, module=args.module, enabled=args.enabled))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RecordError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
