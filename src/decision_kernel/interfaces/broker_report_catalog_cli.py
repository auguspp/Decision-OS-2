"""Opt-in broker report directory. No implicit source or research execution."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from decision_kernel.application.broker_report_catalog import catalog_view, save_catalog
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES, RecordError
from decision_kernel.infrastructure.broker_report_catalog import fetch_catalog
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding, GitHubStore
from decision_kernel.observations.broker_report_catalog import ReportScope, normalize_catalog


def _json(path: Path) -> dict:
    try:
        with path.open("rb") as stream:
            raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            raise RecordError("BROKER_INPUT_TOO_LARGE")
        def unique(pairs):
            out={}
            for k,v in pairs:
                if k in out:raise ValueError("duplicate")
                out[k]=v
            return out
        result=json.loads(raw,object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result,dict):raise RecordError("BROKER_INPUT_SHAPE_INVALID")
        return result
    except (OSError,ValueError,UnicodeError,RecursionError) as exc:
        if isinstance(exc,RecordError):raise
        raise RecordError("BROKER_INPUT_INVALID") from None


def main(argv=None)->int:
    parser=argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.broker_report_catalog_cli")
    parser.add_argument("--kind",required=True,choices=["industry","strategy","macro","morning"])
    parser.add_argument("--begin",required=True,help="YYYY-MM-DD")
    parser.add_argument("--end",required=True,help="YYYY-MM-DD, at most 92 days after begin")
    parser.add_argument("--pages",type=int,default=4,help="maximum pages 1..4; insufficient budget fails explicitly")
    parser.add_argument("--industry-code",help="pre-reviewed numeric Eastmoney code, industry only")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline",type=Path,help="previously obtained JSON pages; origin unverified")
    mode.add_argument("--live",action="store_true",help="explicit bounded HTTPS request")
    parser.add_argument("--keyword",default="",help="local read-only filter, not a source query")
    parser.add_argument("--save",action="store_true",help="explicitly submit verified directory *locators* only")
    parser.add_argument("--batch-id",help="stable intent across retries; required for save")
    backend=parser.add_mutually_exclusive_group()
    backend.add_argument("--local-synthetic-store",type=Path)
    backend.add_argument("--binding",type=Path,help="trusted remote writer binding, not shipped read-only config")
    args=parser.parse_args(argv)
    try:
        if args.save:
            if not args.batch_id or (args.local_synthetic_store is None and args.binding is None):
                raise RecordError("BROKER_SAVE_NEEDS_WRITER_AND_BATCH")
        elif args.batch_id or args.local_synthetic_store is not None or args.binding is not None:
            raise RecordError("BROKER_STORE_WITHOUT_SAVE")
        scope=ReportScope(args.kind,args.begin,args.end,args.pages,args.industry_code)
        if args.live:catalog=fetch_catalog(scope)
        else:
            raw=_json(args.offline)
            if set(raw)!={"captured_at","pages"}:
                raise RecordError("BROKER_CAPTURE_SHAPE_INVALID")
            catalog=normalize_catalog(scope,pages=raw["pages"],captured_at=raw["captured_at"],
                                      source_mode="offline-unverified")
        display=catalog_view(catalog,keyword=args.keyword)
        if args.save:
            if args.local_synthetic_store is not None:
                storage=GitStore(args.local_synthetic_store)
            else:
                value=_json(args.binding)
                binding=GitHubBinding.from_dict(value)
                if not binding.write_enabled:raise RecordError("REMOTE_READ_ONLY")
                storage=GitHubStore(binding,GitHubAPI.from_environment())
            outcome=save_catalog(VersionedStore(storage),catalog,args.batch_id)
            print(json.dumps({"catalog":{k:v for k,v in display.items() if k!="items"},
                              "storage":outcome},ensure_ascii=False,sort_keys=True))
            return 0 if outcome["complete"] else 2
        print(json.dumps(display,ensure_ascii=False,sort_keys=True))
        return 0
    except RecordError as exc:
        print(json.dumps({"status":"rejected","error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
