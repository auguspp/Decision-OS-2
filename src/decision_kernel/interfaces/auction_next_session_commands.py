"""Exact T+1 root through the existing store CLI and reading scope."""
from pathlib import Path

from decision_kernel.application.reading_auction_next_session import next_session_plan, open_next_session
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.github_reading import reading_scope

COMMAND = 'read-auction-next-session'


def perform(store, plan, *, registry=None):
    checked = next_session_plan(plan)
    with reading_scope(store.backend) as backend:
        return open_next_session(VersionedStore(backend), checked, registry=registry)


def configure(actions):
    parser = actions.add_parser(COMMAND, help='read exact next-open-session raw fields over the frozen auction cohort')
    parser.add_argument('--input', required=True, type=Path, help='exact saved root and navigation JSON')
    parser.add_argument('--market-registry', type=Path, help='trusted local Relay registry; never source-provided')


def execute(args, store, load):
    registry = None
    if args.market_registry is not None:
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(args.market_registry)
    return perform(store, load(args.input), registry=registry)
