"""Odds routes on the existing explicit store and host, without new credentials."""
from pathlib import Path

from decision_kernel.interfaces import watch_commands

from decision_kernel.application.conditional_odds import (
    ConditionalOdds, publication, validate_calculation, verify_fresh as verify_conditional,
)
from decision_kernel.domain.conditional_odds import shape, validate_spec

BOOK_OPERATION = "read-odds-book"
PROBABILITY_WRITES = ("freeze-odds-assessment", "save-probability-odds")
PROBABILITY_RECONCILES = ("reconcile-odds-assessment", "reconcile-probability-odds")
PROBABILITY_READS = ("read-odds-assessment", "calculate-probability-odds", "read-probability-odds")
PROBABILITY_COMMANDS = PROBABILITY_WRITES + PROBABILITY_RECONCILES + PROBABILITY_READS
REGISTRY_COMMANDS = ("calculate-probability-odds", "save-probability-odds", "reconcile-probability-odds", "read-probability-odds")
CONDITIONAL_REGISTRIES = ("calculate-odds", "save-odds", "reconcile-odds", "read-odds")
REFERENCE_COMMANDS = ("read-valuation", "read-odds", "read-odds-assessment", "read-probability-odds") + watch_commands.REFERENCES
WRITE_COMMANDS = ("freeze-valuation", "save-odds") + PROBABILITY_WRITES + watch_commands.WRITES
RECONCILE_COMMANDS = ("reconcile-valuation", "reconcile-odds") + PROBABILITY_RECONCILES + watch_commands.RECONCILES
READ_COMMANDS = ("read-valuation", "calculate-odds", "read-odds", BOOK_OPERATION) + PROBABILITY_READS + watch_commands.READS
COMMANDS = WRITE_COMMANDS + READ_COMMANDS


def configure(actions):
    for name in COMMANDS:
        action = actions.add_parser(name, help="frozen research Odds with explicit method and source limitations")
        action.add_argument("--ref" if name in REFERENCE_COMMANDS else "--input", type=Path, required=True)
        if name in REGISTRY_COMMANDS + CONDITIONAL_REGISTRIES + watch_commands.REGISTRIES:
            action.add_argument("--registry", type=Path, required=name in REGISTRY_COMMANDS, help="matching retained Relay registry, not a feed")
        if name in WRITE_COMMANDS:
            action.add_argument("--submission-id", required=True)


def preflight(operation, payload):
    if operation == BOOK_OPERATION:
        from decision_kernel.application.odds_book import validate_plan
        return validate_plan(payload)
    if operation in watch_commands.COMMANDS:
        return watch_commands.preflight(operation, payload)
    if operation in ("freeze-odds-assessment", "reconcile-odds-assessment"):
        from decision_kernel.domain.probability_odds import validate_assessment
        validate_assessment(publication(payload, "assessment"))
    elif operation in ("save-probability-odds", "reconcile-probability-odds"):
        shape(publication(payload, "calculation"), {"assessment_ref", "market", "evaluated_at"})
    elif operation in ("freeze-valuation", "reconcile-valuation"):
        validate_spec(publication(payload, "spec"))
    elif operation in ("save-odds", "reconcile-odds"):
        validate_calculation(publication(payload, "calculation"))


def perform(store, operation, payload, submission_id="", registry=None):
    preflight(operation, payload)
    if operation == BOOK_OPERATION:
        from decision_kernel.application.odds_book import read_book
        from decision_kernel.infrastructure.github_store import GitHubStore
        from decision_kernel.infrastructure.runtime_publication import checked_odds_book_visibility
        # The normal CLI and host share this gate on their original native
        # adapter. Recreating it would discard a private-upgrade observation.
        backend = store.backend
        remote = isinstance(backend, GitHubStore)
        if remote:
            checked_odds_book_visibility(backend)
        result = read_book(store, payload, registry=registry)
        if remote:
            checked_odds_book_visibility(backend)
        return result
    if operation in watch_commands.COMMANDS:
        return watch_commands.perform(store, operation, payload, submission_id, _registry(operation, registry))
    if operation in PROBABILITY_COMMANDS:
        from decision_kernel.application.probability_odds import ProbabilityOdds
        service = ProbabilityOdds(store, _registry(operation, registry))
        if operation in ("freeze-odds-assessment", "reconcile-odds-assessment"):
            return service.freeze(payload, submission_id=submission_id)
        if operation in ("save-probability-odds", "reconcile-probability-odds"):
            return service.save(payload, submission_id=submission_id)
        if operation == "calculate-probability-odds":
            return service.compute(payload)
        shape(payload, {"reference"})
        return service.read_assessment(payload["reference"]) if operation == "read-odds-assessment" else service.read(payload["reference"])
    service = ConditionalOdds(store, _registry(operation, registry))
    if operation in ("freeze-valuation", "reconcile-valuation"):
        return service.freeze(payload, submission_id=submission_id)
    if operation in ("save-odds", "reconcile-odds"):
        return service.save(payload, submission_id=submission_id)
    if operation == "calculate-odds":
        return service.compute(payload)
    shape(payload, {"reference"})
    return service.read_spec(payload["reference"]) if operation == "read-valuation" else service.read(payload["reference"])


def execute(args, store, load, reader_factory):
    payload = {"reference": load(args.ref)} if args.store_command in REFERENCE_COMMANDS else load(args.input)
    registry = _registry(args.store_command, getattr(args, "registry", None))
    result = perform(store, args.store_command, payload, getattr(args, "submission_id", ""), registry)
    if args.store_command in WRITE_COMMANDS:
        return verify_fresh(result, args.store_command, reader_factory, registry)
    return result


def _registry(operation, registry=None):
    if operation in CONDITIONAL_REGISTRIES + watch_commands.REGISTRIES and registry is None:
        return None  # Old declared-price paths still need no market Registry.
    if operation not in REGISTRY_COMMANDS + CONDITIONAL_REGISTRIES + watch_commands.REGISTRIES:
        return None
    from decision_kernel.application.relay_registry import RelayRegistry
    return registry if isinstance(registry, RelayRegistry) else RelayRegistry(registry or Path("docs/sources/relay-registry.json"))


def verify_fresh(receipt, operation, reader_factory, registry=None):
    if operation in watch_commands.WRITES + watch_commands.RECONCILES:
        return watch_commands.verify_fresh(receipt, operation, reader_factory, _registry(operation, registry))
    if operation in PROBABILITY_WRITES + PROBABILITY_RECONCILES:
        from decision_kernel.application.probability_odds import verify_fresh as verify_probability
        return verify_probability(receipt, operation, reader_factory, _registry(operation, registry))
    return verify_conditional(receipt, operation, reader_factory, _registry(operation, registry))
