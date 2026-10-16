"""Watch routes share the existing explicit Odds host and credentials."""
from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.odds_watch import OddsWatch, verify_fresh
from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.odds_watch import validate_request, validate_watch

WRITES = ("freeze-odds-watch", "save-odds-watch-check")
RECONCILES = ("reconcile-odds-watch", "reconcile-odds-watch-check")
READS = ("read-odds-watch", "check-odds-watch", "read-odds-watch-check")
REFERENCES = ("read-odds-watch", "read-odds-watch-check")
REGISTRIES = ("check-odds-watch", "save-odds-watch-check", "reconcile-odds-watch-check", "read-odds-watch-check")
COMMANDS = WRITES + RECONCILES + READS


def preflight(operation, payload):
    if operation in ("freeze-odds-watch", "reconcile-odds-watch"):
        validate_watch(publication(payload, "watch"))
    elif operation in ("save-odds-watch-check", "reconcile-odds-watch-check"):
        validate_request(publication(payload, "check"))
    elif operation == "check-odds-watch":
        validate_request(payload)


def perform(store, operation, payload, submission_id, registry=None):
    preflight(operation, payload)
    service = OddsWatch(store, registry)
    if operation in ("freeze-odds-watch", "reconcile-odds-watch"):
        return service.freeze(payload, submission_id=submission_id)
    if operation in ("save-odds-watch-check", "reconcile-odds-watch-check"):
        return service.save(payload, submission_id=submission_id)
    if operation == "check-odds-watch": return service.inspect(payload)
    shape(payload, {"reference"})
    return service.read_watch(payload["reference"]) if operation == "read-odds-watch" else service.read_check(payload["reference"])
