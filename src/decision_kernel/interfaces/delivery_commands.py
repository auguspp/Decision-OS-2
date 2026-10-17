"""Delivery commands composed into the existing store CLI; no hidden file discovery."""
from dataclasses import asdict
from pathlib import Path

from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery, verify_fresh
from decision_kernel.domain.records import BlobRef, RecordError, checked_text, require
from decision_kernel.domain.research_artifacts import ArtifactInput, artifact_reference

COMMANDS = ("deliver-research", "verify-delivery", "open-delivery", "put-artifact", "read-artifact")
WRITE_COMMANDS = ("deliver-research", "put-artifact")


def configure(actions):
    deliver = actions.add_parser("deliver-research", help="save declared sources/files/revision, then verify using a fresh reader")
    deliver.add_argument("--input", type=Path, required=True)
    deliver.add_argument("--submission-id", required=True)
    for name in ("verify-delivery", "open-delivery", "read-artifact"):
        action = actions.add_parser(name, help="read exact retained bytes; never execute a file")
        action.add_argument("--ref", type=Path, required=True)
        if name == "open-delivery":
            action.add_argument("--history-limit", type=int, default=2)
    artifact = actions.add_parser("put-artifact", help="retain an authorized file with exact byte identity")
    artifact.add_argument("--input", type=Path, required=True)
    artifact.add_argument("--submission-id", required=True)


def execute(args, store, load, fresh_reader):
    if args.store_command == "put-artifact":
        payload = load(args.input)
        require(set(payload) == {"artifact", "rights_confirmed", "rights_basis"}
                and payload["rights_confirmed"] is True, "ARTIFACT_RIGHTS_NOT_CONFIRMED")
        checked_text(payload["rights_basis"], limit=4096)
        ref = ResearchArtifacts(store).put(args.submission_id, ArtifactInput.from_dict(payload["artifact"]))
        try:
            verified = ResearchArtifacts(fresh_reader()).read(ref, include_data=False)
        except RecordError:
            verified = {"status": "failed", "error": "FRESH_ARTIFACT_READBACK_FAILED",
                        "bytes_verified": False, "content_assessment": "not-performed", "executed": False}
        return {"reference": asdict(ref), "readback": verified}
    if args.store_command == "read-artifact":
        return ResearchArtifacts(store).read(artifact_reference(load(args.ref)))
    if args.store_command == "deliver-research":
        receipt = ResearchDelivery(store).deliver(load(args.input), submission_id=args.submission_id)
        return verify_fresh(receipt, fresh_reader)
    ref = BlobRef(**load(args.ref))
    delivery = ResearchDelivery(store)
    return delivery.open(ref, history_limit=args.history_limit) if args.store_command == "open-delivery" else delivery.verify(ref)
