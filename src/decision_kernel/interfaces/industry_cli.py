"""Read retained industry changes and bidirectional company roles; no collection."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.industry_operations import read_industry_operations
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.store_cli import _load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--binding", type=Path)
    parser.add_argument("--ref", type=Path, required=True, help="Exact retained packet SourceRef JSON")
    query = parser.add_mutually_exclusive_group()
    query.add_argument("--subject-id")
    query.add_argument("--company-id")
    parser.add_argument("--operating-cycle", action="store_true", help="Read retained revenue/profit/stocks; never infer monthly flows")
    args = parser.parse_args(argv)
    try:
        reference = SourceRef.from_dict(_load(args.ref))
        if args.binding is None:
            backend = GitStore(args.path)
        else:
            binding = replace(GitHubBinding.from_dict(_load(args.binding)), write_enabled=False)
            backend = GitHubStore(binding, GitHubAPI.from_environment())
        reader = read_industry_operations if args.operating_cycle else read_industry
        result = reader(reference, VersionedStore(backend),
                               subject_id=args.subject_id, company_id=args.company_id)
    except RecordError as exc:
        error = str(exc)
    except (TypeError, ValueError, KeyError, RecursionError):
        error = "INDUSTRY_INPUT_INVALID"
    else:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2 if result["status"] == "partial" else 0
    print(json.dumps({"error": error}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
