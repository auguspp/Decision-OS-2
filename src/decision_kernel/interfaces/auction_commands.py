"""Source-free auction reading through the existing store CLI and scope."""
from pathlib import Path

from decision_kernel.application.reading_auction import auction_plan, open_auction
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.github_reading import reading_scope

COMMAND = 'read-opening-auction'


def perform(store, plan, *, registry=None):
    checked = auction_plan(plan)
    with reading_scope(store.backend) as backend:
        result = open_auction(VersionedStore(backend), checked, registry=registry)
    return result


def configure(actions):
    parser = actions.add_parser(COMMAND, help='read the fixed prior-pool auction and same-session raw-close observation')
    parser.add_argument('--input', required=True, type=Path, help='exact auction root and navigation JSON')
    parser.add_argument('--market-registry', type=Path, help='trusted local Relay registry; never source-provided')


def execute(args, store, load):
    registry = None
    if args.market_registry is not None:
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(args.market_registry)
    return perform(store, load(args.input), registry=registry)
