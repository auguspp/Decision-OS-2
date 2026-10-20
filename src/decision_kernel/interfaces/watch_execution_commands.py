"""Finite Watch stages on the original explicit store/host permission boundary."""
from decision_kernel.application.conditional_odds import publication
from decision_kernel.application.watch_delivery import WatchDelivery
from decision_kernel.application.watch_execution import WatchExecution
from decision_kernel.domain.conditional_odds import shape
from decision_kernel.domain.records import RecordError, SHA, require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.domain.watch_execution import validate_delivery_change, validate_plan, validate_query

WRITES = ("freeze-odds-watch-execution", "reserve-odds-watch-execution", "save-odds-watch-execution-check",
          "finish-odds-watch-execution", "change-odds-watch-delivery")
RECONCILES = ("reconcile-odds-watch-execution-plan", "reconcile-odds-watch-execution-slot",
              "reconcile-odds-watch-execution-check", "reconcile-odds-watch-execution-result",
              "reconcile-odds-watch-delivery")
READS = ("read-odds-watch-execution-plan", "preview-odds-watch-execution", "read-odds-watch-execution-slot",
         "read-odds-watch-execution-result", "status-odds-watch-execution",
         "current-odds-watch-delivery", "read-odds-watch-delivery")
REFERENCES = (READS[0], READS[2], READS[3], READS[4], READS[6])
COMMANDS = WRITES + RECONCILES + READS
REGISTRIES = COMMANDS


def preflight(operation, payload):
    if operation in (WRITES[0], RECONCILES[0]):
        validate_plan(publication(payload, "plan"))
    elif operation in (WRITES[1], RECONCILES[1]):
        validate_query(publication(payload, "execution"))
    elif operation in (WRITES[2], WRITES[3], RECONCILES[2], RECONCILES[3]):
        shape(payload, {"slot_ref"})
        artifact_reference(payload["slot_ref"])
    elif operation in (WRITES[4], RECONCILES[4]):
        validate_delivery_change(publication(payload, "change"))
    elif operation == READS[1]:
        validate_query(payload)
    elif operation == READS[5]:
        shape(payload, {"execution_ref", "notice_id", "snapshot"})
        artifact_reference(payload["execution_ref"])
        require(isinstance(payload["notice_id"], str) and len(payload["notice_id"]) == 64
                and all(c in "0123456789abcdef" for c in payload["notice_id"]), "WATCH_DELIVERY_NOTICE_INVALID")
        require(payload["snapshot"] is None or isinstance(payload["snapshot"], str)
                and SHA.fullmatch(payload["snapshot"]), "INVALID_SNAPSHOT")
    else:
        shape(payload, {"reference"})
        artifact_reference(payload["reference"])


def perform(store, operation, payload, submission_id, registry=None):
    preflight(operation, payload)
    service = WatchExecution(store, registry)
    reconcile = operation in RECONCILES
    if operation in WRITES + RECONCILES:
        stage = (RECONCILES if reconcile else WRITES).index(operation)
        method = (service.freeze, service.reserve, service.check, service.finish,
                  WatchDelivery(store, registry).change)[stage]
        return method(payload, submission_id=submission_id, reconcile=reconcile)
    if operation == READS[1]:
        return service.preview(payload)
    if operation == READS[5]:
        return WatchDelivery(store, registry).current(payload["execution_ref"], payload["notice_id"],
                                                     snapshot=payload["snapshot"])
    ref = payload["reference"]
    methods = {READS[0]: service.read_plan, READS[2]: service.read_slot, READS[3]: service.read_result,
               READS[4]: service.status, READS[6]: WatchDelivery(store, registry).read}
    return methods[operation](ref)


def verify_fresh(receipt, operation, reader_factory, registry=None):
    stage = (RECONCILES if operation in RECONCILES else WRITES).index(operation)
    try:
        store = reader_factory()
        service = WatchExecution(store, registry)
        if stage == 0:
            view = service.read_plan(receipt["reference"])
            require(view["record"]["plan_sha256"] == receipt["plan_sha256"], "WATCH_EXECUTION_FRESH_MISMATCH")
        elif stage == 1:
            view = service.read_slot(receipt["reference"])
            require(view["record"]["slot_key"] == receipt["slot_key"]
                    and view["record"]["reserved_at"] == receipt["reserved_at"]
                    and view["reference"]["commit"] == receipt["records_snapshot"], "WATCH_EXECUTION_FRESH_MISMATCH")
        elif stage == 2:
            # A legacy exact Activity read alone does not qualify a new
            # execution's slot, stable journal and original first-parent save.
            checked = service.check({"slot_ref": receipt.get("slot_ref")}, reconcile=True)
            require(all(checked[key] == receipt.get(key) for key in
                        ("reference", "event_sha256", "state", "check_status", "watch_key", "slot_ref")),
                    "WATCH_EXECUTION_FRESH_MISMATCH")
        elif stage == 3:
            view = service.read_result(receipt["reference"])
            require(view["record"] == receipt["record"], "WATCH_EXECUTION_FRESH_MISMATCH")
        else:
            view = WatchDelivery(store, registry).read(receipt["reference"])
            require(view["event"]["state"] == receipt["state"], "WATCH_DELIVERY_FRESH_MISMATCH")
        status = {"status": "verified", "new_reader": True, "economic_assessment": "not-performed",
                  "transport_delivery_verified": False, "user_read_verified": False}
    except RecordError:
        status = {"status": "failed", "new_reader": True, "error": "WATCH_EXECUTION_FRESH_READBACK_FAILED"}
    return {**receipt, "readback": status}
