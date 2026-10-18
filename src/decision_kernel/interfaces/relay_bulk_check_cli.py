"""Private, read-only finite diagnostic in the existing Relay Actions host."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from decision_kernel.application import relay_bulk_check as core
from decision_kernel.application.relay_registry import _json
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI
from decision_kernel.infrastructure.relay_http import RelayHTTP, ROOT
from decision_kernel.interfaces.relay_source_runtime import INPUTS, REPO

FAMILY = 'market-bulk-check'
WORKFLOW = '.github/workflows/relay-source-runtime.yml'
TITLE = 'Relay bulk boundary 20261018'
PREFIX = '/repos/' + REPO


def settings(event, env):
    require(env.get('GITHUB_REPOSITORY') == REPO and env.get('GITHUB_REF') == 'refs/heads/main'
            and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
            and env.get('GITHUB_WORKFLOW_REF') == REPO + '/' + WORKFLOW + '@refs/heads/main'
            and env.get('GITHUB_RUN_ATTEMPT') == '1', 'BULK_MANUAL_MAIN_ATTEMPT_ONE')
    require(isinstance(event, dict) and isinstance(event.get('inputs'), dict), 'BULK_EVENT')
    data = event['inputs']
    require(set(data) <= INPUTS and data.get('family') == FAMILY
            and data.get('operation') == 'check'
            and (data.get('access_confirmed') is True or type(data.get('access_confirmed')) is str and data['access_confirmed'] == 'true')
            and (data.get('public_rights_confirmed') is False or type(data.get('public_rights_confirmed')) is str and data['public_rights_confirmed'] == 'false'), 'BULK_EXPLICIT_PRIVATE_CHECK')
    require(all(not data.get(k) for k in ('codes', 'periods', 'dates', 'apis'))
            and str(data.get('page_size', '100')) == '100'
            and str(data.get('max_pages', '100')) == '100', 'BULK_UNUSED_SELECTORS')
    raw = data.get('source_plan')
    require(isinstance(raw, str) and len(raw.encode()) <= 256
            and _json(raw.encode()) == {'study': core.STUDY}, 'BULK_FIXED_STUDY')
    sha, run_id = data.get('code_sha'), env.get('GITHUB_RUN_ID')
    require(isinstance(sha, str) and re.fullmatch(r'[a-f0-9]{40}', sha)
            and sha == env.get('GITHUB_SHA'), 'BULK_CODE_SHA')
    require(isinstance(run_id, str) and re.fullmatch(r'[1-9][0-9]{0,14}', run_id), 'BULK_RUN_ID')
    return {'study': core.STUDY, 'code_sha': sha, 'run_id': run_id,
            'attempt': 1, 'repository': REPO, 'plan': core.plan()}


def check_private(api, selected):
    meta = api.request('GET', PREFIX)
    require(meta.get('id') == 1420041744 and meta.get('full_name') == REPO
            and meta.get('private') is True and meta.get('archived') is False
            and meta.get('default_branch') == 'main', 'BULK_PRIVATE_REPOSITORY_REQUIRED')
    head = api.request('GET', PREFIX + '/git/ref/heads/main')
    require(head.get('object', {}).get('sha') == selected['code_sha'], 'BULK_MAIN_MOVED')


def native_ready(api, selected):
    check_private(api, selected)
    run = api.request('GET', PREFIX + '/actions/runs/' + selected['run_id'])
    require(run.get('id') == int(selected['run_id']) and run.get('head_sha') == selected['code_sha']
            and run.get('head_branch') == 'main' and run.get('run_attempt') == 1
            and run.get('event') == 'workflow_dispatch' and run.get('path') == WORKFLOW
            and run.get('display_title') == TITLE and run.get('status') == 'in_progress'
            and run.get('repository', {}).get('id') == 1420041744, 'BULK_NATIVE_RUN_IDENTITY')
    # The CI gate owns full/qualified merge reuse. This is the real main run,
    # not an old PR success or source-input claim.
    ci = api.request('GET', PREFIX + '/actions/workflows/ci.yml/runs?' + urlencode({
        'branch': 'main', 'event': 'push', 'head_sha': selected['code_sha'], 'per_page': 5}))
    runs = ci.get('workflow_runs')
    require(isinstance(runs, list) and bool(runs), 'BULK_MAIN_CI_REQUIRED')
    latest = runs[0]
    require(latest.get('head_sha') == selected['code_sha'] and latest.get('head_branch') == 'main'
            and latest.get('event') == 'push' and latest.get('path') == '.github/workflows/ci.yml'
            and latest.get('status') == 'completed' and latest.get('conclusion') == 'success',
            'BULK_MAIN_CI_REQUIRED')
    matches = []
    for page in range(1, 11):
        result = api.request('GET', PREFIX + '/actions/workflows/relay-source-runtime.yml/runs?'
                             + urlencode({'event': 'workflow_dispatch', 'per_page': 100, 'page': page}))
        history = result.get('workflow_runs')
        require(isinstance(history, list) and len(history) <= 100, 'BULK_RUN_INVENTORY')
        matches.extend(r for r in history if r.get('display_title') == TITLE)
        if len(history) < 100:
            break
    else:
        raise RecordError('BULK_RUN_INVENTORY_INCOMPLETE')
    require(len(matches) == 1 and matches[0].get('id') == int(selected['run_id'])
            and matches[0].get('run_attempt') == 1, 'BULK_ALREADY_ATTEMPTED')
    # The existing workflow concurrency group also serializes with ashare-data.
    return {'main_ci_run': latest['id'], 'one_shot_inventory': 'unique-at-check'}


def _safe_error(exc):
    text = str(exc)
    return text if isinstance(exc, (RecordError, ValueError)) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', text) else 'BULK_OPERATION_FAILED'


def _regular(path, bound):
    require(path.is_file() and not path.is_symlink(), 'BULK_REGULAR_INPUT')
    with path.open('rb') as stream:
        raw = stream.read(bound + 1)
    require(0 < len(raw) <= bound, 'BULK_INPUT_BOUND')
    return raw


def execute(stage, event, env, out, *, api=None, client_factory=RelayHTTP,
            monotonic=time.monotonic, sleep=time.sleep, clock=None):
    selected = settings(event, env)
    root = Path(env['RUNNER_TEMP']).resolve()
    out = Path(out)
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
            'BULK_OUTPUT_PATH')
    api = GitHubAPI.from_environment() if api is None else api
    proof = native_ready(api, selected)
    if stage == 'prepare':
        out.mkdir(parents=False, exist_ok=False)
        (out / 'request.json').write_bytes(encoded(selected))
        (out / 'result.json').write_bytes(encoded({'status': 'prepared-no-source-requests',
                                                  'source_requests': 0, 'proof': proof}))
        return 0
    require(stage == 'capture' and _json(_regular(out / 'request.json', 6000)) == selected,
            'BULK_PREPARED_INPUT_CHANGED')
    require(_json(_regular(out / 'result.json', 65536)).get('status') == 'prepared-no-source-requests',
            'BULK_OUTPUT_ALREADY_CONSUMED')
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    report = {**selected, 'schema': 'relay-bulk-check-v1', 'status': 'started', 'proof': proof,
              'source_requests': 0, 'records_written': False, 'market_qualification_granted': False,
              'started_at': clock(), 'results': [], 'raw_release': 'not-released'}
    def save():
        (out / 'result.json').write_bytes(encoded(report))
    save()
    client = client_factory(request_timeout_seconds=90)
    require(client.requests == 0, 'BULK_FRESH_CLIENT_REQUIRED')
    retained = []
    failed = False
    for spec in selected['plan']:
        row = {'id': spec['id'], 'parameters': spec['parameters'], 'status': 'not-requested'}
        report['results'].append(row)
        if failed:
            save(); continue
        start = None
        try:
            check_private(api, selected)
            if report['source_requests']:
                sleep(30)  # Space separate experiments; never retry a failed request.
                check_private(api, selected)
            row.update(status='request-started', requested_at=clock())
            report['source_requests'] += 1
            require(report['source_requests'] <= core.MAX_CALLS, 'BULK_SOURCE_BUDGET')
            save()
            start = monotonic()
            if spec['kind'] == 'capability':
                client.capabilities('daily')
            else:
                # Fixed reviewed experiment only. The normal 5000 plan cap is
                # neither monkeypatched nor made caller-configurable.
                client._get_json(ROOT + '/daily?' + urlencode(spec['parameters']),
                                 max_bytes=core.MAX_BODY, agent='DecisionOS2-BulkCheck/0.1')
            row.update(received_at=clock(), elapsed_seconds=round(monotonic() - start, 6))
            raw = client.last_response_bytes
            require(isinstance(raw, bytes) and 0 < len(raw) <= core.MAX_BODY, 'BULK_RAW_UNAVAILABLE')
            row.update(bytes=len(raw), sha256=sha256(raw).hexdigest(), transport=client.last_response_meta)
            # Full raw bytes have passed the existing reflection guard. They
            # are released only following the final native private recheck.
            filename = spec['id'] + '.json'
            retained.append((filename, raw))
            row['raw_file'] = filename
            finding = core.inspect_capability(raw) if spec['kind'] == 'capability' else core.inspect_quote(raw, spec)
            row.update(status='response-inspected', finding=finding)
            if spec['kind'] == 'capability' and finding.get('max_limit') is not None:
                require(finding['max_limit'] >= 12000, 'BULK_DECLARED_LIMIT_STOPS_LARGER_REQUESTS')
        except (RecordError, OSError, ValueError, TypeError, KeyError) as exc:
            row.update(status='stopped', error=_safe_error(exc), finished_at=clock())
            meta = getattr(client, 'last_error_meta', None) or {}
            row['observed_http_status'] = meta.get('http_status')
            if start is not None:
                row['elapsed_seconds'] = round(monotonic() - start, 6)
            for key in ('classification', 'x_request_id', 'x_cache'):
                value = meta.get(key)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value):
                    row[key] = value
            failed = True
        save()
    require(client.requests <= core.MAX_CALLS, 'BULK_SOURCE_BUDGET')
    # A request-started receipt is intentionally not a claim that a response
    # arrived. Keep actual transport count separately for interrupted admission.
    report['transport_requests'] = client.requests
    try:
        check_private(api, selected)
        for filename, raw in retained:
            with (out / filename).open('xb') as stream:
                stream.write(raw)
        report['raw_release'] = 'native-private-rechecked'
    except (RecordError, OSError) as exc:
        report['raw_release'] = 'withheld-or-incomplete'
        report['release_error'] = _safe_error(exc)
        failed = True
    report.update(status='stopped' if failed else 'completed-diagnostic-not-production', finished_at=clock())
    save()
    return 2 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'capture'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        env = dict(os.environ)
        event = _json(_regular(Path(env['GITHUB_EVENT_PATH']), 65536))
        return execute(args.stage, event, env, args.out)
    except (RecordError, OSError, ValueError, TypeError, KeyError):
        print('{"status":"rejected-or-interrupted","error":"BULK_CHECK_FAILED"}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
