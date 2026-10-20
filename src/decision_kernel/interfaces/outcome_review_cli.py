"""Read exact operating forecasts and T+5/T+20 outcomes; never collect or write."""
import argparse
from dataclasses import replace
from pathlib import Path
import sys

from decision_kernel.application.outcome_review import open_outcome_review, outcome_plan
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.store_cli import _load
from decision_kernel.observations.market_daily import MarketInputError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--binding", type=Path)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=Path("docs/sources/relay-registry.json"))
    args = parser.parse_args(argv)
    try:
        plan = outcome_plan(_load(args.input))
        if args.binding is None:
            backend = GitStore(args.path)
        else:
            binding = replace(GitHubBinding.from_dict(_load(args.binding)), write_enabled=False)
            backend = GitHubStore(binding, GitHubAPI.from_environment())
            require(backend.live_visibility() == "private", "OUTCOME_PRIVATE_READING_REQUIRED")
        result = open_outcome_review(VersionedStore(backend), plan, registry=RelayRegistry(args.registry))
        if args.binding is not None:
            require(backend.live_visibility() == "private", "OUTCOME_PRIVATE_READING_REQUIRED")
        print(encoded(result).decode(), end="")
        return 0
    except (RecordError, MarketInputError) as exc:
        print(encoded({"error": str(exc)}).decode(), file=sys.stderr, end="")
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        print('{"error":"OUTCOME_INPUT_OR_SOURCE_INVALID"}', file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
