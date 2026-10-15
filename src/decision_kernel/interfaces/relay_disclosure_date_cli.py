"""Explicit off-line or one-shot Relay schedule consumer. No schedules or auto-writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from decision_kernel.application.relay_disclosure_date import save_schedule
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES,RecordError,identifier
from decision_kernel.infrastructure.relay_disclosure_date import fetch_disclosure_dates
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubAPI,GitHubBinding,GitHubStore
from decision_kernel.observations.relay_disclosure_date import normalize_disclosure_dates,view


def _json(path:Path)->dict:
    try:
        with path.open("rb") as f:raw=f.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise RecordError("RELAY_INPUT_TOO_LARGE")
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise ValueError("duplicate")
                result[k]=v
            return result
        x=json.loads(raw,object_pairs_hook=unique,
                     parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if not isinstance(x,dict):raise RecordError("RELAY_INPUT_SHAPE_INVALID")
        return x
    except (OSError,ValueError,UnicodeError,RecursionError) as exc:
        if isinstance(exc,RecordError):raise
        raise RecordError("RELAY_INPUT_INVALID") from None


def main(argv=None)->int:
    p=argparse.ArgumentParser(prog="python -m decision_kernel.interfaces.relay_disclosure_date_cli")
    p.add_argument("--ts-code",required=True,help="one exact 6-digit.SH/.SZ/.BJ ticker")
    p.add_argument("--end-date",required=True,help="report end YYYYMMDD")
    p.add_argument("--limit",type=int,default=100,help="explicit row budget 1..100")
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline",type=Path,help="previously obtained response, provenance unverified")
    mode.add_argument("--live",action="store_true",help="one credentialed Relay request, never automatic")
    p.add_argument("--save",action="store_true",help="persist derived schedule fact, not original PDF")
    p.add_argument("--batch-id",help="unique intent for this exact submission")
    p.add_argument("--public-rights-confirmed",action="store_true",
                   help="run-owner assertion that this excerpt can legally be made public")
    dest=p.add_mutually_exclusive_group()
    dest.add_argument("--local-synthetic-store",type=Path)
    dest.add_argument("--binding",type=Path,help="trusted GitHub writer config, not read-only default")
    args=p.parse_args(argv)
    try:
        if args.save:
            if not args.batch_id or not args.public_rights_confirmed or (
                args.local_synthetic_store is None and args.binding is None):
                raise RecordError("RELAY_SAVE_SCOPE_AND_RIGHTS_REQUIRED")
        elif args.batch_id or args.public_rights_confirmed or args.local_synthetic_store or args.binding:
            raise RecordError("RELAY_UNAUTHORIZED_STORE_PARAMETERS")
        backend = None
        if args.save:
            identifier(args.batch_id)
            if args.local_synthetic_store is not None:
                backend = GitStore(args.local_synthetic_store)
            else:
                binding = GitHubBinding.from_dict(_json(args.binding))
                if not binding.write_enabled:
                    raise RecordError("REMOTE_READ_ONLY")
                backend = GitHubStore(binding, GitHubAPI.from_environment())
        if args.live:
            snapshot=fetch_disclosure_dates(ts_code=args.ts_code,end_date=args.end_date,limit=args.limit)
        else:
            obj=_json(args.offline)
            if set(obj)!={"captured_at","response"}:
                raise RecordError("RELAY_CAPTURE_SHAPE_INVALID")
            snapshot=normalize_disclosure_dates(ts_code=args.ts_code,end_date=args.end_date,
                 raw=obj["response"],captured_at=obj["captured_at"],
                 source_mode="offline-unverified",limit=args.limit)
        result=view(snapshot)
        if args.save:
            receipt=save_schedule(VersionedStore(backend),snapshot,args.batch_id,
                                  public_rights_confirmed=True)
            result={"schedule":result,"storage":receipt}
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0 if not args.save or receipt["complete"] else 2
    except RecordError as exc:
        print(json.dumps({"status":"rejected","error":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=="__main__":raise SystemExit(main())
