"""Adapters for the existing store/host; preparation cannot confirm a Decision."""
from pathlib import Path

from decision_kernel.application.decision_preparation import DecisionPreparation, publication, verify_fresh
from decision_kernel.domain.decision_preparation import moment, shape, validate_preparation
from decision_kernel.domain.records import require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.infrastructure.runtime_publication import checked_output_visibility, publication_basis, publication_payload

READS = ("prepare-decision", "read-decision-preparation")
WRITES = ("save-decision-preparation",)
RECONCILES = ("reconcile-decision-preparation",)
COMMANDS = READS + WRITES


def configure(actions):
    for name in COMMANDS:
        parser = actions.add_parser(name, help="evidence-bound AI preparation; no formal Decision or execution")
        parser.add_argument("--input", type=Path, required=True)
        parser.add_argument("--registry", type=Path, help="matching existing Relay registry for retained-market Odds")
        if name in WRITES:
            parser.add_argument("--submission-id", required=True)


def preflight(operation, payload):
    require(operation in COMMANDS + RECONCILES, "DECISION_PREPARATION_OPERATION")
    if operation in WRITES + RECONCILES:
        publication(payload)
    elif operation == "prepare-decision":
        validate_preparation(payload)
    else:
        shape(payload, {"reference", "at"})
        artifact_reference(payload["reference"])
        moment(payload["at"])


def perform(store, operation, payload, submission_id="", registry=None):
    binding = getattr(store.backend, "binding", None)
    if publication_basis(binding, "decision-preparation") is not None and store.backend.live_visibility() == "public":
        payload = publication_payload(operation, payload, binding)
    preflight(operation, payload)
    if registry is None:
        from decision_kernel.application.relay_registry import RelayRegistry
        registry = RelayRegistry(Path("docs/sources/relay-registry.json"))
    service = DecisionPreparation(store, registry)
    if operation in WRITES + RECONCILES:
        return service.save(payload, submission_id=submission_id)
    if operation == "prepare-decision":
        return service.prepare(payload)
    return service.read(payload["reference"], at=payload["at"])


def execute(args, store, load, reader_factory):
    from decision_kernel.application.relay_registry import RelayRegistry
    registry_path = getattr(args, "registry", None)
    registry = RelayRegistry(registry_path if registry_path is not None else Path("docs/sources/relay-registry.json"))
    result = perform(store, args.store_command, load(args.input), getattr(args, "submission_id", ""), registry)
    return verify_fresh(result, reader_factory, registry) if args.store_command in WRITES else result


def private_reader(binding, api):
    """Admit each new native reader before opening any preparation or evidence."""
    from dataclasses import replace
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    backend = GitHubStore(replace(binding, write_enabled=False), api)
    checked_output_visibility(backend, "decision-preparation", "DECISION_PREPARATION_PRIVATE_CUSTODY_REQUIRED")
    return VersionedStore(backend)
