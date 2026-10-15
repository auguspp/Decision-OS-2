"""Explicit CNINFO catalog entry, separate from #7's research/store CLI.

Only --live contacts the official provider. Only --save may create source revisions.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.application.source_catalog import catalog_view, save_catalog
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES, RecordError
from decision_kernel.infrastructure.cninfo_catalog import fetch_catalog
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.observations.cninfo_catalog import normalize_catalog


def _json(path: Path):
    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise RecordError("CNINFO_INPUT_TOO_LARGE")
        return json.loads(raw, object_pairs_hook=no_duplicates,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("CNINFO_INPUT_UNREADABLE") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.cninfo_catalog_cli")
    parser.add_argument("--stock", required=True, help="one exact 6-digit Chinese stock code")
    parser.add_argument("--start", required=True, help="first official announcement date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last official announcement date YYYY-MM-DD, <=31 days")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--pages", type=Path, help="unverified previously captured JSON pages; offline")
    location.add_argument("--live", action="store_true", help="explicit bounded HTTPS provider query")
    parser.add_argument("--max-pages", type=int, default=4, help="1..4, 30 rows per page maximum")
    parser.add_argument("--save", action="store_true", help="explicitly commit only external PDF locators")
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--local-synthetic-store", type=Path, help="existing synthetic bare store for testing")
    store.add_argument("--binding", type=Path, help="trusted GitHub writer binding, NOT repository's read-only default")
    parser.add_argument("--batch-id", help="same replay intent across retries; required for --save")
    args = parser.parse_args(argv)
    try:
        if args.save:
            if not args.batch_id or (args.local_synthetic_store is None and args.binding is None):
                raise RecordError("CNINFO_SAVE_NEEDS_EXPLICIT_WRITER_AND_BATCH")
        elif args.batch_id or args.local_synthetic_store or args.binding:
            raise RecordError("CNINFO_STORE_WITHOUT_SAVE")
        if args.live:
            catalog = fetch_catalog(stock_code=args.stock, start_date=args.start,
                                    end_date=args.end, max_pages=args.max_pages)
        else:
            data = _json(args.pages)
            if not isinstance(data, dict) or set(data) != {"captured_at", "org_rows", "pages"}:
                raise RecordError("CNINFO_CAPTURE_SHAPE_INVALID")
            catalog = normalize_catalog(stock_code=args.stock, start_date=args.start,
                                        end_date=args.end, captured_at=data["captured_at"],
                                        org_rows=data["org_rows"], pages=data["pages"],
                                        source_mode="offline-unverified", max_pages=args.max_pages)
        result = catalog_view(catalog)
        if args.save:
            if args.local_synthetic_store:
                backend = GitStore(args.local_synthetic_store)
            else:
                binding_data = _json(args.binding)
                binding = GitHubBinding.from_dict(binding_data)
                if not binding.write_enabled:
                    raise RecordError("REMOTE_READ_ONLY")
                backend = GitHubStore(binding, GitHubAPI.from_environment())
            outcome = save_catalog(VersionedStore(backend), catalog, args.batch_id)
            # Do not silently turn partial, unknown or failed writes into successful exit 0.
            result = {"catalog": {k: v for k, v in result.items() if k != "items"},
                      "storage": outcome}
            print(json.dumps(result, sort_keys=True, ensure_ascii=False))
            return 0 if outcome["complete"] else 2
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0
    except RecordError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
