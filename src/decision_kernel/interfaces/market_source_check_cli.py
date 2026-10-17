"""Manual main-only market check host; persists engineering counters, never source rows."""
from __future__ import annotations

import argparse
from copy import deepcopy
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
OPTIONS = {'selection', 'request_context_confirmed', 'member_code', 'resume'}


def settings(event: dict, env: dict, now: datetime) -> dict:
    require(env.get('GITHUB_REPOSITORY') == REPO and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'MARKET_CHECK_MANUAL_MAIN_ONLY')
    require(isinstance(event, dict) and isinstance(event.get('inputs'), dict)
            and INPUTS <= event['inputs'].keys() <= INPUTS | OPTIONS, 'MARKET_CHECK_INPUT')
    data = event['inputs']
    require(isinstance(data['code_sha'], str) and re.fullmatch(r'[a-f0-9]{40}', data['code_sha'])
            and data['code_sha'] == env.get('GITHUB_SHA'), 'MARKET_CHECK_CODE_CHANGED')
    require(data['access_confirmed'] is True or type(data['access_confirmed']) is str
            and data['access_confirmed'] == 'true', 'MARKET_CHECK_ACCESS_REQUIRED')
    require(isinstance(env.get('GITHUB_RUN_ID'), str) and re.fullmatch(r'[1-9][0-9]*', env['GITHUB_RUN_ID'])
            and env.get('GITHUB_RUN_ATTEMPT') == '1', 'MARKET_CHECK_NEW_EXPLICIT_RUN_REQUIRED')
    selection = data.get('selection', 'sample')
    require(isinstance(selection, str) and selection in (*SELECTIONS, 'resume-members', 'session-coverage'), 'MARKET_CHECK_SELECTION')
    context = data.get('request_context_confirmed', False)
    require(type(context) is bool or type(context) is str and context in ('true', 'false'), 'MARKET_CHECK_CONTEXT_PERMISSION')
    context = context is True or context == 'true'
    code, resume = data.get('member_code', ''), data.get('resume', '')
    require(isinstance(code, str) and isinstance(resume, str) and len(resume) <= 4096, 'MARKET_CHECK_MEMBER_INPUT')
    selected = {'exchange': data['exchange'], 'start': data['start'], 'end': data['end'],
                'access_confirmed': True, 'selection': selection}
    if selection == 'session-coverage':
        from decision_kernel.application.market_session_check import session_scope
        require(context and not code and not resume, 'MARKET_SESSION_PERMISSION_REQUIRED')
        session_scope(data['exchange'], data['start'], data['end'], now)
    elif selection == 'resume-members':
        from decision_kernel.application.market_member_recovery import selector
        require(context and not code and resume, 'MARKET_CHECK_RECOVERY_INPUT')
        selected.update(selection='members', resume=selector(_json(resume.encode())))
    elif selection == 'members':
        from decision_kernel.application.market_member_recovery import member_code
        require(context and not resume, 'MARKET_CHECK_RECOVERY_INPUT')
        selected['member_code'] = member_code(code)
    else:
        require(not code and not resume and (not context or selection in ('sample', 'availability')),
                'MARKET_CHECK_RECOVERY_INPUT')
    if context:
        selected['request_context_confirmed'] = True
    if selection != 'session-coverage':
        check_scope(data['exchange'], data['start'], data['end'], now, selected['selection'])
    return selected


def execute(event: dict, env: dict, out: Path, *, now: datetime | None = None,
            checker=check_sources, registry_path=Path('docs/sources/relay-registry.json'),
            native_reader=None) -> int:
    now = datetime.now(timezone.utc) if now is None else now
    selected = settings(event, env, now)
    if selected['selection'] == 'session-coverage' and checker is check_sources:
        from decision_kernel.application.market_session_check import check_session_sources
        checker = check_session_sources
    root = Path(env['RUNNER_TEMP']).resolve()
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
            'MARKET_CHECK_OUTPUT')
    registry = RelayRegistry(registry_path)
    out.mkdir(parents=False, exist_ok=False)
    identity = {'repo': REPO, 'code_sha': env['GITHUB_SHA'], 'run_id': env['GITHUB_RUN_ID'],
                'attempt': '1', 'started_at': now.isoformat()}
    latest = {'status': 'in-progress', 'supplier_data_retained': False}
    recovery = None
    def progress(receipt):
        nonlocal latest
        latest = {**receipt, 'identity': identity}
        if recovery is not None:
            latest['recovery'] = deepcopy(recovery)
        # Only this fixed engineering receipt is uploaded, never a directory of captures.
        pending = out / 'result.tmp'
        pending.write_text(json.dumps(latest, ensure_ascii=False, allow_nan=False, sort_keys=True), encoding='utf-8')
        pending.replace(out / 'result.json')
    progress(latest)
    resume = selected.pop('resume', None)
    if resume is not None:
        from decision_kernel.application.market_member_recovery import load_recovery
        try:
            native_reader = native_reader or _native_market_reader(env)
            plan = load_recovery(native_reader, resume, registry=registry,
                exchange=selected['exchange'], start=selected['start'], end=selected['end'],
                now=now, current_run=env['GITHUB_RUN_ID'])
            selected['member_code'] = plan['member_code']
            recovery = {'parent': plan['parent'], 'source_query_recovered': False,
                        'original_batch_revalidated': False}
        except Exception:
            # Native proof failures never validate or call the Relay credential.
            progress({'status': 'resume-unavailable', 'reason_code': 'MARKET_CHECK_RECOVERY_NOT_VERIFIED',
                'supplier_data_retained': False, 'source_refs_created': False, 'http_attempts': 0})
            return 2
        progress(latest)  # Reserve the explicit recovery in the original artifact before source I/O.
    try:
        result = checker(registry=registry, **selected, now=now, progress=progress)
    except Exception:
        # Do not echo arbitrary exception text or traceback (which may contain source values).
        progress({**latest, 'status': 'interrupted', 'reason_code': 'MARKET_CHECK_INTERRUPTED'})
        return 2
    if recovery is not None:
        recovery['source_query_recovered'] = result.get('status') == 'checked-members'
    progress(result)
    return 0 if result['status'] in ('checked-sample', 'checked-catalogues', 'checked-stock-sample',
                                       'checked-us-index-context', 'checked-availability-sample',
                                       'available-with-quarantine', 'checked-members', 'checked-session-coverage') else 2


def _native_market_reader(env):
    from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding
    from decision_kernel.infrastructure.research_receipts import NativeResearchReceipts, download_archive
    # This explicit, read-only job token is never given to Relay or its opener.
    token = env.get('DECISION_OS_CHECK_READ_TOKEN', '')
    api = GitHubAPI(token)
    binding = GitHubBinding.from_dict(_json(Path('config/store-binding.json').read_bytes()))
    require(binding.repository == REPO and binding.repository_id == 1420041744, 'MARKET_CHECK_BINDING')
    return NativeResearchReceipts(binding, api,
        archive_loader=lambda repo, aid: download_archive(repo, aid, token=token))


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
