"""Adapters for the existing store/host; preparation cannot confirm a Decision."""
from pathlib import Path

from decision_kernel.application.decision_preparation import DecisionPreparation, publication, verify_fresh
from decision_kernel.domain.decision_preparation import moment, shape, validate_preparation
from decision_kernel.domain.records import require
from decision_kernel.domain.research_artifacts import artifact_reference

READS = ("prepare-decision", "read-decision-preparation")
WRITES = ("save-decision-preparation",)
RECONCILES = ("reconcile-decision-preparation",)
COMMANDS = READS + WRITES


class _DefaultRegistryLoader:
    """Pin the entry-point path; retain the real catalogue once Odds needs it."""

    def __init__(self):
        self.path = Path("docs/sources/relay-registry.json").resolve()
        self.registry = None

    def __call__(self):
        if self.registry is None:
            from decision_kernel.application.relay_registry import RelayRegistry
            self.registry = RelayRegistry(self.path)
        return self.registry


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


def perform(store, operation, payload, submission_id="", registry=None, *, registry_loader=None):
    preflight(operation, payload)
    if registry is None and registry_loader is None:
        registry_loader = _DefaultRegistryLoader()
    service = DecisionPreparation(store, registry, registry_loader=registry_loader)
    if operation in WRITES + RECONCILES:
        return service.save(payload, submission_id=submission_id)
    if operation == "prepare-decision":
        return service.prepare(payload)
    return service.read(payload["reference"], at=payload["at"])


def execute(args, store, load, reader_factory):
    from decision_kernel.application.relay_registry import RelayRegistry
    registry_path = getattr(args, "registry", None)
    registry = RelayRegistry(registry_path) if registry_path is not None else None
    loader = _DefaultRegistryLoader() if registry is None else None
    result = perform(store, args.store_command, load(args.input), getattr(args, "submission_id", ""),
                     registry, registry_loader=loader)
    if args.store_command in WRITES:
        # Use the actual catalogue already required by this document/history.
        # Null-only histories need none, including in the independent reader.
        return verify_fresh(result, reader_factory, registry if registry is not None else loader.registry)
    return result


def private_reader(binding, api):
    """Admit each new native reader before opening any preparation or evidence."""
    from dataclasses import replace
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.github_store import GitHubStore
    backend = GitHubStore(replace(binding, write_enabled=False), api)
    require(backend.live_visibility() == "private", "DECISION_PREPARATION_PRIVATE_CUSTODY_REQUIRED")
    return VersionedStore(backend)
