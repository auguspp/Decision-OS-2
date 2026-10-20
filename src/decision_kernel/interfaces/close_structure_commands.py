"""One source-free read command in the existing decision-os store CLI."""
from pathlib import Path

from decision_kernel.application.close_structure import open_structure, plan_input
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.github_reading import reading_scope

COMMAND = "read-close-structure"


def perform(store, plan, *, registry=None):
    checked = plan_input(plan)
    with reading_scope(store.backend) as backend:
        result = open_structure(VersionedStore(backend), checked, registry=registry)
    return result


def configure(actions):
    parser = actions.add_parser(COMMAND, help="read close ranges and confirmed local turns from exact retained sources")
    parser.add_argument("--input", required=True, type=Path, help="close-structure-plan-v1 JSON")
    parser.add_argument("--market-registry", type=Path,
                        help="trusted local Relay registry; required outside a checkout with docs/sources")


def execute(args, store, load):
    registry = None
    if args.market_registry is not None:
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(args.market_registry)
    return perform(store, load(args.input), registry=registry)
