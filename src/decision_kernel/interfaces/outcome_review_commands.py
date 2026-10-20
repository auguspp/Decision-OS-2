"""Optional existing-host/CLI adapter for a bounded, read-only outcome review."""
from pathlib import Path

from decision_kernel.application.outcome_review import open_outcome_review, outcome_plan
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import require

OPERATION = "outcome-review"
COMMAND = "research-outcome-review"


def preflight(operation, payload, *, public=True):
    if operation != OPERATION:
        return
    require(isinstance(payload, dict) and "plan" in payload and set(payload) <= {
        "plan", "public_research_custody_confirmed", "public_sources_custody_confirmed"},
        "OUTCOME_HOST_PAYLOAD")
    plan = outcome_plan(payload["plan"])
    require(type(public) is bool, "OUTCOME_EXPORT_MODE")
    if public:
        require(payload.get("public_research_custody_confirmed") is True,
                "OUTCOME_PUBLIC_RESEARCH_NOT_CONFIRMED")
        require(not (plan["market"] or any(p["actual"] is not None for p in plan["economic"]))
                or payload.get("public_sources_custody_confirmed") is True,
                "OUTCOME_PUBLIC_SOURCES_NOT_CONFIRMED")


def perform(store, payload, *, public=True, registry=None):
    preflight(OPERATION, payload, public=public)
    return open_outcome_review(store, payload["plan"], registry=registry)


def configure(actions):
    parser = actions.add_parser(COMMAND, help="read original forecasts and fixed-session price outcomes")
    parser.add_argument("--input", type=Path, required=True, help="exact retained outcome review plan JSON")
    parser.add_argument("--market-registry", type=Path, help="trusted local Relay registry")


def execute(args, store, load):
    registry = RelayRegistry(args.market_registry) if args.market_registry is not None else None
    return perform(store, {"plan": load(args.input)}, public=False, registry=registry)
