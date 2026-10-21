"""Read retained industry changes and bidirectional company roles; no collection."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

from decision_kernel.application.industry_reading import _clock, read_industry
from decision_kernel.application.industry_operations import read_industry_operations
from decision_kernel.application.vehicle_flows import read_vehicle_flows
from decision_kernel.application.livestock_weekly import read_livestock_weekly
from decision_kernel.application.industry_uses import read_industry_uses
from decision_kernel.application.industry_goods import read_industry_goods
from decision_kernel.application.industry_goods_coverage import read_industry_goods_coverage
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, require
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.interfaces.store_cli import _load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--path", type=Path)
    where.add_argument("--binding", type=Path)
    parser.add_argument("--ref", type=Path, required=True, help="Exact retained packet SourceRef JSON")
    parser.add_argument("--evaluation-at", help="Explicit clock for goods qualification or fixed coverage")
    query = parser.add_mutually_exclusive_group()
    query.add_argument("--subject-id")
    query.add_argument("--company-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--industry-goods-coverage", action="store_true", help="Audit a fixed collection of retained goods batches")
    mode.add_argument("--industry-goods", action="store_true", help="Read evidenced distinct goods qualification at an explicit clock")
    mode.add_argument("--industry-uses", action="store_true", help="Read six declared uses with source blocks and original scopes")
    mode.add_argument("--livestock-weekly", action="store_true", help="Read collection-day livestock prices and published comparisons")
    mode.add_argument("--vehicle-flows", action="store_true", help="Read vehicle channels separately; no inferred inventory")
    mode.add_argument("--operating-cycle", action="store_true", help="Read retained revenue/profit/stocks; never infer monthly flows")
    args = parser.parse_args(argv)
    try:
        require((args.industry_goods or args.industry_goods_coverage) == (args.evaluation_at is not None), "INDUSTRY_GOODS_EVALUATION_REQUIRED")
        if args.industry_goods or args.industry_goods_coverage:
            _clock(args.evaluation_at)
        require(not args.industry_goods_coverage or args.company_id is None,
                "INDUSTRY_COVERAGE_COMPANY_QUERY_USE_BATCH")
        reference = SourceRef.from_dict(_load(args.ref))
        if args.binding is None:
            backend = GitStore(args.path)
        else:
            binding = replace(GitHubBinding.from_dict(_load(args.binding)), write_enabled=False)
            backend = GitHubStore(binding, GitHubAPI.from_environment())
        extra = {}
        if args.industry_goods_coverage:
            reader = read_industry_goods_coverage
            extra["evaluation_at"] = args.evaluation_at
        elif args.industry_goods:
            reader = read_industry_goods
            extra["evaluation_at"] = args.evaluation_at
        elif args.industry_uses:
            reader = read_industry_uses
        elif args.livestock_weekly:
            reader = read_livestock_weekly
        elif args.vehicle_flows:
            reader = read_vehicle_flows
        elif args.operating_cycle:
            reader = read_industry_operations
        else:
            reader = read_industry
        result = reader(reference, VersionedStore(backend),
                               subject_id=args.subject_id, company_id=args.company_id, **extra)
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
