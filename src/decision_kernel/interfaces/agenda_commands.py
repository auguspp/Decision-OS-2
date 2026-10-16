"""Read-only agenda adapters. Public job artifacts need separate feedback consent."""
from datetime import datetime, timezone
from pathlib import Path

from decision_kernel.application.research_agenda import ResearchAgenda, clock, plan_input
from decision_kernel.domain.records import require
from decision_kernel.domain.research_artifacts import artifact_reference

COMMANDS = ("agenda", "read-agenda")
LOCAL_COMMANDS = ("research-agenda", "read-research-agenda")


def preflight(operation, payload):
    if operation not in COMMANDS:
        return
    if operation == "agenda":
        require(isinstance(payload, dict) and "plan" in payload and set(payload) <= {
            "plan", "public_feedback_custody_confirmed"}, "AGENDA_PUBLIC_PAYLOAD_INVALID")
        plan = plan_input(payload["plan"])
        consent = payload.get("public_feedback_custody_confirmed", False)
        require(type(consent) is bool, "AGENDA_PUBLIC_CONSENT_INVALID")
        require(not plan["changes"] or consent, "AGENDA_PUBLIC_FEEDBACK_NOT_CONFIRMED")
        require(clock(plan["as_of"]) <= datetime.now(timezone.utc), "AGENDA_PUBLIC_FUTURE_CLOCK")
    else:
        require(isinstance(payload, dict) and "reference" in payload
                and set(payload) <= {"reference", "as_of"}, "AGENDA_READ_PAYLOAD_INVALID")
        artifact_reference(payload["reference"])
        if "as_of" in payload:
            require(clock(payload["as_of"]) <= datetime.now(timezone.utc), "AGENDA_PUBLIC_FUTURE_CLOCK")


def perform(store, operation, payload):
    agenda = ResearchAgenda(store)
    if operation == "agenda":
        return agenda.build(payload["plan"])
    require(operation == "read-agenda", "AGENDA_OPERATION_INVALID")
    return agenda.open(payload["reference"], as_of=payload.get("as_of"))


def configure(actions):
    build = actions.add_parser("research-agenda", help="project exact research and submitted choices; no writes or execution")
    build.add_argument("--input", type=Path, required=True, help="agenda plan JSON; select refs from the existing catalogue")
    read = actions.add_parser("read-research-agenda", help="reopen retained choices with fresh exact research reads")
    read.add_argument("--ref", type=Path, required=True, help="exact saved agenda ArtifactRef JSON")
    read.add_argument("--as-of", help="explicit review evaluation clock, not proof an event occurred")


def execute(args, store, load):
    agenda = ResearchAgenda(store)
    if args.store_command == "research-agenda":
        return agenda.build(load(args.input))
    return agenda.open(load(args.ref), as_of=args.as_of)
