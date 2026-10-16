"""Odds routes on the existing explicit store and host, without new credentials."""
from pathlib import Path

from decision_kernel.application.conditional_odds import ConditionalOdds, publication, verify_fresh
from decision_kernel.domain.conditional_odds import shape, validate_spec

WRITE_COMMANDS = ("freeze-valuation", "save-odds")
RECONCILE_COMMANDS = ("reconcile-valuation", "reconcile-odds")
READ_COMMANDS = ("read-valuation", "calculate-odds", "read-odds")
COMMANDS = WRITE_COMMANDS + READ_COMMANDS


def configure(actions):
    for name in COMMANDS:
        action = actions.add_parser(name, help="exact research conditional analysis, not qualified probability Odds")
        action.add_argument("--ref" if name in ("read-valuation", "read-odds") else "--input", type=Path, required=True)
        if name in WRITE_COMMANDS:
            action.add_argument("--submission-id", required=True)


def preflight(operation, payload):
    if operation in ("freeze-valuation", "reconcile-valuation"):
        validate_spec(publication(payload, "spec"))
    elif operation in ("save-odds", "reconcile-odds"):
        shape(publication(payload, "calculation"), {"spec_ref", "price"})


def perform(store, operation, payload, submission_id=""):
    service = ConditionalOdds(store)
    preflight(operation, payload)
    if operation in ("freeze-valuation", "reconcile-valuation"):
        return service.freeze(payload, submission_id=submission_id)
    if operation in ("save-odds", "reconcile-odds"):
        return service.save(payload, submission_id=submission_id)
    if operation == "calculate-odds":
        return service.compute(payload)
    shape(payload, {"reference"})
    return service.read_spec(payload["reference"]) if operation == "read-valuation" else service.read(payload["reference"])


def execute(args, store, load, reader_factory):
    payload = {"reference": load(args.ref)} if args.store_command in ("read-valuation", "read-odds") else load(args.input)
    result = perform(store, args.store_command, payload, getattr(args, "submission_id", ""))
    if args.store_command in WRITE_COMMANDS:
        return verify_fresh(result, args.store_command, reader_factory)
    return result
