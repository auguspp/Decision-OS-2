"""Manual main-only market check host; persists engineering counters, never source rows."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

from decision_kernel.application.market_source_check import SELECTIONS, check_scope, check_sources
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.domain.records import require

REPO = 'auguspp/Decision-OS-2'
INPUTS = {'code_sha', 'exchange', 'start', 'end', 'access_confirmed'}


def settings(event: dict, env: dict, now: datetime) -> dict:
    require(env.get('GITHUB_REPOSITORY') == REPO and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'MARKET_CHECK_MANUAL_MAIN_ONLY')
    require(isinstance(event, dict) and isinstance(event.get('inputs'), dict)
            and INPUTS <= event['inputs'].keys() <= INPUTS | {'selection'}, 'MARKET_CHECK_INPUT')
    data = event['inputs']
    require(isinstance(data['code_sha'], str) and re.fullmatch(r'[a-f0-9]{40}', data['code_sha'])
            and data['code_sha'] == env.get('GITHUB_SHA'), 'MARKET_CHECK_CODE_CHANGED')
    require(data['access_confirmed'] is True or type(data['access_confirmed']) is str
            and data['access_confirmed'] == 'true', 'MARKET_CHECK_ACCESS_REQUIRED')
    require(isinstance(env.get('GITHUB_RUN_ID'), str) and re.fullmatch(r'[1-9][0-9]*', env['GITHUB_RUN_ID'])
            and env.get('GITHUB_RUN_ATTEMPT') == '1', 'MARKET_CHECK_NEW_EXPLICIT_RUN_REQUIRED')
    selection = data.get('selection', 'sample')
    require(isinstance(selection, str) and selection in SELECTIONS, 'MARKET_CHECK_SELECTION')
    check_scope(data['exchange'], data['start'], data['end'], now)
    return {'exchange': data['exchange'], 'start': data['start'], 'end': data['end'],
            'access_confirmed': True, 'selection': selection}


def execute(event: dict, env: dict, out: Path, *, now: datetime | None = None,
            checker=check_sources, registry_path=Path('docs/sources/relay-registry.json')) -> int:
    now = datetime.now(timezone.utc) if now is None else now
    selected = settings(event, env, now)
    root = Path(env['RUNNER_TEMP']).resolve()
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
            'MARKET_CHECK_OUTPUT')
    registry = RelayRegistry(registry_path)
    out.mkdir(parents=False, exist_ok=False)
    identity = {'repo': REPO, 'code_sha': env['GITHUB_SHA'], 'run_id': env['GITHUB_RUN_ID'],
                'attempt': '1', 'started_at': now.isoformat()}
    latest = {'status': 'in-progress', 'supplier_data_retained': False}
    def progress(receipt):
        nonlocal latest
        latest = {**receipt, 'identity': identity}
        # Only this fixed engineering receipt is uploaded, never a directory of captures.
        pending = out / 'result.tmp'
        pending.write_text(json.dumps(latest, ensure_ascii=False, allow_nan=False, sort_keys=True), encoding='utf-8')
        pending.replace(out / 'result.json')
    progress(latest)
    try:
        result = checker(registry=registry, **selected, now=now, progress=progress)
    except Exception:
        # Do not echo arbitrary exception text or traceback (which may contain source values).
        progress({**latest, 'status': 'interrupted', 'reason_code': 'MARKET_CHECK_INTERRUPTED'})
        return 2
    progress(result)
    return 0 if result['status'] in ('checked-sample', 'checked-catalogues', 'checked-stock-sample') else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        with Path(os.environ['GITHUB_EVENT_PATH']).open('rb') as stream:
            raw = stream.read(65537)
        require(len(raw) <= 65536, 'MARKET_CHECK_EVENT_SIZE')
        return execute(_json(raw), dict(os.environ), args.out)
    except Exception:
        print('{"status":"rejected","reason_code":"MARKET_CHECK_INPUT_OR_HOST_INVALID"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
