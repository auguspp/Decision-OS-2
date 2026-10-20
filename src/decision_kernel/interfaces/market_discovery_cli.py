"""Explicit source-free discovery build/save/read on the original versioned store."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from decision_kernel.application.market_discovery import MarketDiscovery, plan_input
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.domain.research_artifacts import artifact_reference
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.infrastructure.runtime_publication import checked_output_visibility
from decision_kernel.interfaces.store_cli import _load
from decision_kernel.observations.market_daily import MarketInputError


def execute(operation, payload, store, *, registry, submission_id=None, custody_authorized=False,
            rights_confirmed=False):
    require(operation in ("build", "save", "read"), "DISCOVERY_OPERATION_INVALID")
    if operation == "read":
        artifact_reference(payload)
    else:
        payload = plan_input(payload)
    if operation == "save":
        require(type(custody_authorized) is bool and type(rights_confirmed) is bool
                and (custody_authorized or rights_confirmed) and isinstance(submission_id, str),
                "DISCOVERY_SAVE_NOT_AUTHORIZED")
        return MarketDiscovery(store, registry=registry).save(submission_id, payload)
    require(submission_id is None, "DISCOVERY_UNEXPECTED_WRITE_FIELDS")
    with reading_scope(store.backend) as backend:
        reader = MarketDiscovery(VersionedStore(backend), registry=registry)
        result = reader.read(payload) if operation == "read" else reader.build(payload)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("build", "save", "read"))
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--binding", type=Path)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("docs/sources/relay-registry.json"))
    parser.add_argument("--submission-id")
    parser.add_argument("--public-rights-confirmed", action="store_true")
    parser.add_argument("--custody-authorized", action="store_true",
                        help="Explicit private custody authorization; does not grant public redistribution")
    args = parser.parse_args(argv)
    try:
        payload = _load(args.input)
        if args.binding is None:
            store = VersionedStore(GitStore(args.path))
        else:
            binding = GitHubBinding.from_dict(_load(args.binding))
            if args.operation == "save":
                require(binding.write_enabled, "REMOTE_READ_ONLY")
            else:
                binding = replace(binding, write_enabled=False)
            backend = GitHubStore(binding, GitHubAPI.from_environment())
            checked_output_visibility(backend, "market", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
            store = VersionedStore(backend)
        result = execute(args.operation, payload, store, registry=RelayRegistry(args.registry),
                         submission_id=args.submission_id, custody_authorized=args.custody_authorized,
                         rights_confirmed=args.public_rights_confirmed)
        if args.binding is not None:
            checked_output_visibility(store.backend, "market", "DISCOVERY_PRIVATE_CUSTODY_REQUIRED")
        print(encoded(result).decode(), end="")
        return 0
    except (RecordError, MarketInputError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print('{"error":"DISCOVERY_INPUT_OR_SOURCE_INVALID"}', file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
