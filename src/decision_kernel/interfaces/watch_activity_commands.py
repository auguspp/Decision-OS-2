"""Lifecycle commands on the same explicit host; no scheduler or new credential."""
from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.watch_activity import WatchActivity, verify_fresh
from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.watch_activity import validate_change, validate_check

WRITES = ("change-odds-watch-activity", "save-odds-watch-activity-check")
RECONCILES = ("reconcile-odds-watch-activity", "reconcile-odds-watch-activity-check")
READS = ("current-odds-watch-activity", "list-odds-watch-activities", "read-odds-watch-activity", "check-odds-watch-activity",
         "read-odds-watch-activity-segment")
REFERENCES = ("current-odds-watch-activity", "read-odds-watch-activity", "read-odds-watch-activity-segment")
REGISTRIES = WRITES + RECONCILES + READS
COMMANDS = WRITES + RECONCILES + READS


def preflight(operation, payload):
    if operation in (WRITES[0], RECONCILES[0]): validate_change(publication(payload, "change"))
    elif operation in (WRITES[1], RECONCILES[1]): validate_check(publication(payload, "check"))
    elif operation == "check-odds-watch-activity": validate_check(payload)


def perform(store, operation, payload, submission_id, registry=None):
    preflight(operation, payload)
    service = WatchActivity(store, registry)
    if operation in (WRITES[0], RECONCILES[0]): return service.change(payload, submission_id=submission_id, reconcile=operation in RECONCILES)
    if operation in (WRITES[1], RECONCILES[1]): return service.save_check(payload, submission_id=submission_id, reconcile=operation in RECONCILES)
    if operation == "list-odds-watch-activities": return service.catalogue(payload)
    if operation == "check-odds-watch-activity": return service.preview(payload)
    shape(payload, {"reference"})
    if operation == "read-odds-watch-activity-segment": return service.read_segment(payload["reference"])
    return service.current(payload["reference"]) if operation == "current-odds-watch-activity" else service.read(payload["reference"])
