"""Watch routes share the existing explicit Odds host and credentials."""
from decision_kernel.interfaces import watch_activity_commands as activity
from decision_kernel.interfaces import watch_execution_commands as execution
from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.odds_watch import OddsWatch, verify_fresh as verify_watch
from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.odds_watch import validate_request, validate_watch

WRITES = ("freeze-odds-watch", "save-odds-watch-check")
RECONCILES = ("reconcile-odds-watch", "reconcile-odds-watch-check")
READS = ("read-odds-watch", "check-odds-watch", "read-odds-watch-check")
REFERENCES = ("read-odds-watch", "read-odds-watch-check")
REGISTRIES = ("check-odds-watch", "save-odds-watch-check", "reconcile-odds-watch-check", "read-odds-watch-check")
WRITES += activity.WRITES
RECONCILES += activity.RECONCILES
READS += activity.READS
REFERENCES += activity.REFERENCES
REGISTRIES += activity.REGISTRIES
WRITES += execution.WRITES
RECONCILES += execution.RECONCILES
READS += execution.READS
REFERENCES += execution.REFERENCES
REGISTRIES += execution.REGISTRIES
COMMANDS = WRITES + RECONCILES + READS


def preflight(operation, payload):
    if operation in activity.COMMANDS: return activity.preflight(operation, payload)
    if operation in execution.COMMANDS: return execution.preflight(operation, payload)
    if operation in ("freeze-odds-watch", "reconcile-odds-watch"):
        validate_watch(publication(payload, "watch"))
    elif operation in ("save-odds-watch-check", "reconcile-odds-watch-check"):
        validate_request(publication(payload, "check"))
    elif operation == "check-odds-watch":
        validate_request(payload)


def perform(store, operation, payload, submission_id, registry=None):
    preflight(operation, payload)
    if operation in activity.COMMANDS: return activity.perform(store, operation, payload, submission_id, registry)
    if operation in execution.COMMANDS: return execution.perform(store, operation, payload, submission_id, registry)
    service = OddsWatch(store, registry)
    if operation in ("freeze-odds-watch", "reconcile-odds-watch"):
        return service.freeze(payload, submission_id=submission_id)
    if operation in ("save-odds-watch-check", "reconcile-odds-watch-check"):
        return service.save(payload, submission_id=submission_id)
    if operation == "check-odds-watch": return service.inspect(payload)
    shape(payload, {"reference"})
    return service.read_watch(payload["reference"]) if operation == "read-odds-watch" else service.read_check(payload["reference"])


def verify_fresh(receipt, operation, reader_factory, registry=None):
    if operation in execution.WRITES + execution.RECONCILES:
        return execution.verify_fresh(receipt, operation, reader_factory, registry)
    if operation in activity.WRITES + activity.RECONCILES:
        return activity.verify_fresh(receipt, operation, reader_factory, registry)
    return verify_watch(receipt, operation, reader_factory, registry)
