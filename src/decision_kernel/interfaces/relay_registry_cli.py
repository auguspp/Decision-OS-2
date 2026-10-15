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
    args = parser.parse_args(argv)
    try:
        if args.api and (args.group or args.module):
            raise RecordError('RELAY_REGISTRY_ONE_SELECTOR')
        reader = RelayRegistry(args.registry)
        result = reader.lookup(args.api) if args.api else reader.select(group=args.group, module=args.module)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RecordError as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
