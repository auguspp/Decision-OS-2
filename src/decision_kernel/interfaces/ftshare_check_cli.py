"""Finite FTShare qualification in the existing private A-share Actions host.

The first real account check does not alter the active collection or its budget.
Only the secret-bearing capture stage contacts the fixed FTShare transport.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

from decision_kernel.application.market_collection import effective_unit
from decision_kernel.application.market_collection_custody import restore_market_collection
from decision_kernel.application.market_custody import market_read_scope, restore_market_query
from decision_kernel.application.market_security_inputs import inspect_security_master
from decision_kernel.application.relay_registry import RelayRegistry, _json
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.github_store import GitHubAPI
from decision_kernel.infrastructure.market_snapshot import GitMarketReader
from decision_kernel.interfaces.market_collection_cli import REGISTRY, _records_runtime_binding

REPO = 'auguspp/Decision-OS-2'
WORKFLOW = '.github/workflows/ashare-data.yml'
TITLE = 'FTShare daily check 20261018'
STUDY = 'ftshare-daily-qualification-20261018'
MAX_REQUESTS = 6
PREFIX = '/repos/' + REPO
BASE_REFERENCE = {
    'manifest': {'commit': '3fcaa33e745afa0084ff20136538a06ce8c38877',
        'path': 'source-views/market-collection-7c761159fc6a90a08b8f8634-6ed2725eea4de422add3acce/5176cd63eacc36b864e0e3d1dfd85f53a8d9b54984e224afcbf57ebeaac4bff5/manifest.json',
        'repository': REPO,
        'sha256': 'a1f9884e92df80d834adc41eca7c65de97ecdde443eaa5b419f00792ccff3acc'},
    'selection': {'kind': 'all', 'start': None, 'end': None, 'columns': []},
}
SHANGHAI = timezone(timedelta(hours=8))


def date_bounds(start, end):
    first = datetime.strptime(start, '%Y%m%d').replace(tzinfo=SHANGHAI)
    last = datetime.strptime(end, '%Y%m%d').replace(tzinfo=SHANGHAI) + timedelta(days=1)
    return int(first.timestamp() * 1000), int(last.timestamp() * 1000) - 1


def settings(event, env):
    require(env.get('GITHUB_REPOSITORY') == REPO and env.get('GITHUB_REF') == 'refs/heads/main'
        and env.get('GITHUB_EVENT_NAME') == 'workflow_dispatch'
        and env.get('GITHUB_WORKFLOW_REF') == REPO + '/' + WORKFLOW + '@refs/heads/main'
        and env.get('GITHUB_RUN_ATTEMPT') == '1', 'FTSHARE_MANUAL_MAIN_ATTEMPT_ONE')
    data = event.get('inputs') if isinstance(event, dict) else None
    require(isinstance(data, dict) and set(data) <= {'operation', 'code_sha', 'selection',
        'work_units', 'repair_request', 'access_and_custody_confirmed', 'public_rights_confirmed'},
        'FTSHARE_CHECK_EVENT')
    require(data.get('operation') == 'ftshare-check'
        and (data.get('access_and_custody_confirmed') is True or data.get('access_and_custody_confirmed') == 'true')
        and (data.get('public_rights_confirmed') is False or data.get('public_rights_confirmed') == 'false')
        and str(data.get('work_units', '1')) == '1' and not data.get('repair_request'),
        'FTSHARE_EXPLICIT_PRIVATE_CHECK')
    selection = data.get('selection')
    require(isinstance(selection, str) and len(selection.encode()) <= 256
        and _json(selection.encode()) == {'study': STUDY}, 'FTSHARE_FIXED_STUDY')
    sha, run_id = data.get('code_sha'), env.get('GITHUB_RUN_ID')
    require(isinstance(sha, str) and re.fullmatch(r'[a-f0-9]{40}', sha)
        and sha == env.get('GITHUB_SHA'), 'FTSHARE_CODE_SHA')
    require(isinstance(run_id, str) and re.fullmatch(r'[1-9][0-9]{0,14}', run_id), 'FTSHARE_RUN_ID')
    return {'study': STUDY, 'code_sha': sha, 'run_id': run_id, 'attempt': 1,
        'repository': REPO, 'source_budget': MAX_REQUESTS}


def check_private(api, selected):
    # Historical studies remain private-only; normal hosts may use #164 authority.
    from decision_kernel.infrastructure.runtime_publication import native_publication_basis, require_code_on_main
    if "runtime_version" in selected:
        # Only the daily entry establishes this frozen execution binding. Its
        # live guard owns both native visibility and current deployment grants;
        # the old checkout's public-runtime file cannot override a revocation.
        from decision_kernel.infrastructure.daily_execution import require_live_execution
        require("study" not in selected, 'FTSHARE_PRIVATE_REPOSITORY_REQUIRED')
        require_live_execution(api, selected)
        return
    meta = api.request('GET', PREFIX)
    authorization_selection = selected
    if meta.get('private') is False and set(selected) == {'code_sha'} \
            and os.environ.get('GITHUB_WORKFLOW_REF') == REPO + '/.github/workflows/daily-market.yml@refs/heads/main':
        # The existing release command supplies only the code SHA. Recover its
        # normal operation from the SAME native event, never from source text.
        from decision_kernel.interfaces.daily_market_cli import POLICY_FILE, load_clock_policy, settings as daily_settings
        event = _json(_regular(Path(os.environ['GITHUB_EVENT_PATH']), 1024 * 1024))
        approved = _json(_regular(POLICY_FILE, 8192))
        authorization_selection = daily_settings(event, os.environ, approved, clock=load_clock_policy())
    require(meta.get('id') == 1420041744 and meta.get('full_name') == REPO
        and (meta.get('private') is True or meta.get('private') is False
             and native_publication_basis(authorization_selection) is not None)
        and meta.get('archived') is False
        and meta.get('default_branch') == 'main', 'FTSHARE_PRIVATE_REPOSITORY_REQUIRED')
    # A merge behind this run does not change the code it is executing; only a
    # rewrite that removed its commit from main invalidates the run.
    require_code_on_main(api, selected['code_sha'], 'FTSHARE_MAIN_MOVED')


def native_ready(api, selected, *, title=TITLE):
    check_private(api, selected)
    run = api.request('GET', PREFIX + '/actions/runs/' + selected['run_id'])
    require(run.get('id') == int(selected['run_id']) and run.get('head_sha') == selected['code_sha']
        and run.get('head_branch') == 'main' and run.get('run_attempt') == 1
        and run.get('event') == 'workflow_dispatch' and run.get('path') == WORKFLOW
        and run.get('display_title') == title and run.get('status') == 'in_progress'
        and run.get('repository', {}).get('id') == 1420041744, 'FTSHARE_NATIVE_RUN_IDENTITY')
    ci = api.request('GET', PREFIX + '/actions/workflows/ci.yml/runs?' + urlencode({
        'branch': 'main', 'event': 'push', 'head_sha': selected['code_sha'], 'per_page': 5}))
    runs = ci.get('workflow_runs')
    require(isinstance(runs, list) and bool(runs), 'FTSHARE_MAIN_CI_REQUIRED')
    latest = runs[0]
    require(latest.get('head_sha') == selected['code_sha'] and latest.get('head_branch') == 'main'
        and latest.get('event') == 'push' and latest.get('path') == '.github/workflows/ci.yml'
        and latest.get('status') == 'completed' and latest.get('conclusion') == 'success',
        'FTSHARE_MAIN_CI_REQUIRED')
    matches = []
    for page in range(1, 11):
        history = api.request('GET', PREFIX + '/actions/workflows/ashare-data.yml/runs?'
            + urlencode({'event': 'workflow_dispatch', 'per_page': 100, 'page': page})).get('workflow_runs')
        require(isinstance(history, list) and len(history) <= 100, 'FTSHARE_RUN_INVENTORY')
        matches.extend(r for r in history if r.get('display_title') == title)
        if len(history) < 100:
            break
    else:
        raise RecordError('FTSHARE_RUN_INVENTORY_INCOMPLETE')
    require(len(matches) == 1 and matches[0].get('id') == int(selected['run_id'])
        and matches[0].get('run_attempt') == 1, 'FTSHARE_CHECK_ALREADY_ATTEMPTED')
    return {'main_ci_run': latest['id'], 'one_shot_inventory': 'unique-at-check'}


def load_baseline(api):
    """Reuse exact current-L masters and raw close; no Relay source requests."""
    registry = RelayRegistry(REGISTRY)
    store = VersionedStore(GitMarketReader(_records_runtime_binding('status'), api))
    with market_read_scope(store) as reader:
        restored = restore_market_collection(BASE_REFERENCE, registry, reader)
        collection = restored['collection']
        require(collection['plan']['collection_id'] == 'cn-eod'
            and collection['plan']['generation'] == 'g20261016', 'FTSHARE_BASELINE_COLLECTION')
        masters, master_refs = {}, {}
        for venue in ('SSE', 'SZSE'):
            slot = 'master:' + venue
            unit = effective_unit(collection, slot)
            require(unit['status'] == 'complete', 'FTSHARE_BASELINE_MASTER_REQUIRED')
            result = restore_market_query(unit['query'], registry, reader)['result']
            masters[venue] = inspect_security_master({'L': result}, registry=registry, exchange=venue)
            master_refs[venue] = unit['query']
        selected = sorted(c for venue in masters.values() for c, item in venue['securities'].items()
            if item is not None)
        codes = sorted([c for c in selected if c.endswith('.SH')][:50]
            + [c for c in selected if c.endswith('.SZ')][:50])
        require(len(codes) == 100 and {'000001.SZ', '600000.SH'} <= set(selected),
            'FTSHARE_BASELINE_SAMPLE')
        unit = effective_unit(collection, '20261016:daily')
        require(unit['status'] == 'complete', 'FTSHARE_BASELINE_DAILY_REQUIRED')
        raw = restore_market_query(unit['query'], registry, reader)
        return {'reference': BASE_REFERENCE, 'master_references': master_refs,
            'scope_count': sum(v['scope_count'] for v in masters.values()), 'symbols': codes,
            'daily_reference': unit['query'], 'daily': raw['result']}


def check_plan(codes):
    require(isinstance(codes, list) and len(codes) == 100 and len(set(codes)) == 100
        and codes == sorted(codes) and all(re.fullmatch(r'[0-9]{6}\.(SH|SZ)', c) for c in codes),
        'FTSHARE_CHECK_SYMBOLS')
    smaller = sorted([c for c in codes if c.endswith('.SH')][:25]
        + [c for c in codes if c.endswith('.SZ')][:25])
    day_start, day_end = date_bounds('20261016', '20261016')
    history_start, history_end = date_bounds('20260701', '20261016')
    pair = ['000001.SZ', '600000.SH']
    def bars(name, symbols, since, until, adjustment='none'):
        return {'id': name, 'api': 'candlesticks', 'symbols': symbols, 'since': since,
            'until': until, 'adjust_kind': adjustment}
    return [bars('raw-pair', pair, day_start, day_end),
        bars('raw-pair-repeat', pair, day_start, day_end),
        bars('raw-100-day', codes, day_start, day_end),
        bars('raw-50-history', smaller, history_start, history_end),
        bars('forward-50-history', smaller, history_start, history_end, 'forward'),
        {'id': 'factor-semantics', 'api': 'factors', 'symbol': '000001.SZ',
            'since': history_start, 'until': history_end, 'page': 1, 'page_size': 1000}]


def _safe_error(exc):
    code = str(exc)
    return code if isinstance(exc, (RecordError, ValueError)) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', code) else 'FTSHARE_CHECK_FAILED'


def _regular(path, bound):
    require(path.is_file() and not path.is_symlink(), 'FTSHARE_CHECK_REGULAR_INPUT')
    with path.open('rb') as stream:
        raw = stream.read(bound + 1)
    require(0 < len(raw) <= bound, 'FTSHARE_CHECK_INPUT_BOUND')
    return raw


def next_request_delay(client):
    """Honor observed source instructions; no retry or quota-reset waiting."""
    meta = getattr(client, 'last_response_meta', None) or {}
    for quota in meta.get('rate_limits', {}).values():
        require(quota.get('state') != 'valid' or quota.get('remaining') != 0,
            'FTSHARE_OBSERVED_QUOTA_EXHAUSTED')
    state, seconds = getattr(client, 'last_retry_after', ('missing', None))
    require(state in ('missing', 'valid'), 'FTSHARE_WAIT_INSTRUCTION_UNUSABLE')
    if state == 'valid':
        require(type(seconds) is int and 0 <= seconds <= 60, 'FTSHARE_WAIT_EXCEEDS_CHECK_BOUND')
    return max(2, seconds or 0)


def execute(stage, event, env, out, *, api=None, client_factory=None,
            baseline_loader=load_baseline, monotonic=time.monotonic, sleep=time.sleep, clock=None):
    selected = settings(event, env)
    root = Path(env['RUNNER_TEMP']).resolve()
    out = Path(out)
    require(out.resolve().is_relative_to(root) and out.resolve() != root and not out.is_symlink(),
        'FTSHARE_CHECK_OUTPUT_PATH')
    api = GitHubAPI.from_environment() if api is None else api
    proof = native_ready(api, selected)
    if stage == 'prepare':
        baseline = baseline_loader(api)
        check_private(api, selected)
        out.mkdir(parents=False, exist_ok=False)
        (out / 'baseline.json').write_bytes(encoded(baseline))
        plan = check_plan(baseline['symbols'])
        (out / 'request.json').write_bytes(encoded({**selected, 'plan': plan,
            'baseline_sha256': digest(encoded(baseline))}))
        (out / 'result.json').write_bytes(encoded({'status': 'prepared-no-source-requests',
            'source_requests': 0, 'proof': proof}))
        return 0
    request = _json(_regular(out / 'request.json', 65536))
    baseline = _json(_regular(out / 'baseline.json', 2 * 1024 * 1024))
    require(request == {**selected, 'plan': check_plan(baseline['symbols']),
        'baseline_sha256': digest(encoded(baseline))}, 'FTSHARE_PREPARED_INPUT_CHANGED')
    previous = _json(_regular(out / 'result.json', 262144))
    if stage == 'release':
        require(previous.get('status') in ('validated', 'partial', 'failed')
            and previous.get('raw_release') == 'not-released', 'FTSHARE_CHECK_RELEASE_STATE')
        check_private(api, selected)
        previous['raw_release'] = 'private-artifact-only'
        (out / 'result.json').write_bytes(encoded(previous))
        print(encoded({k: v for k, v in previous.items() if k not in ('results',)}).decode())
        return 0
    require(stage == 'capture' and previous.get('status') == 'prepared-no-source-requests',
        'FTSHARE_CHECK_ALREADY_CONSUMED')
    from decision_kernel.application.ftshare_daily import validate_batch
    from decision_kernel.infrastructure.ftshare_http import FTShareHTTP
    client = (client_factory or FTShareHTTP)()
    client.validate_credential()
    require(client.requests == 0, 'FTSHARE_FRESH_CLIENT_REQUIRED')
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    report = {**selected, 'schema': 'ftshare-daily-check-v1', 'status': 'started',
        'proof': proof, 'source_requests': 0, 'records_written': False,
        'active_source_changed': False, 'started_at': clock(), 'results': [],
        'raw_release': 'not-released', 'account_daily_limit_claim': 1000,
        'account_daily_limit_verified': False, 'long_term_uptime_certified': False}
    def save():
        (out / 'result.json').write_bytes(encoded(report))
    save()
    failed = False
    normalized = {}
    for spec in request['plan']:
        row = {'id': spec['id'], 'status': 'not-requested'}
        report['results'].append(row)
        if failed:
            save(); continue
        try:
            check_private(api, selected)
            if report['source_requests']:
                sleep(next_request_delay(client))
                check_private(api, selected)
            start = monotonic()
            row.update(status='request-started', requested_at=clock())
            report['source_requests'] += 1
            require(report['source_requests'] <= MAX_REQUESTS, 'FTSHARE_SOURCE_BUDGET')
            save()
            if spec['api'] == 'candlesticks':
                body = client.candlesticks_batch(spec['symbols'], since=spec['since'],
                    until=spec['until'], adjust_kind=spec['adjust_kind'])
            else:
                body = client.stock_adjust_factor(spec['symbol'], since=spec['since'],
                    until=spec['until'], page=spec['page'], page_size=spec['page_size'])
            row.update(elapsed_seconds=round(monotonic() - start, 6),
                transport=client.last_response_meta)
            raw = client.last_response_bytes
            require(isinstance(raw, bytes) and bool(raw), 'FTSHARE_ORIGINAL_BODY_REQUIRED')
            # Even protocol mismatches retain the original safe bytes for diagnosis.
            check_private(api, selected)
            name = spec['id'] + '.raw.json'
            (out / name).write_bytes(raw)
            row.update(raw_file=name, raw_bytes=len(raw), raw_sha256=digest(raw))
            if spec['api'] == 'candlesticks':
                checked = validate_batch(body, symbols=spec['symbols'], since=spec['since'],
                    until=spec['until'], adjust_kind=spec['adjust_kind'])
                normalized[spec['id']] = checked
                row.update(status='qualified-response', counts=checked['counts'],
                    empty_symbol_count=len(checked['missing']['empty_symbols']))
                require(checked['counts']['quarantined_rows'] == 0, 'FTSHARE_CHECK_QUARANTINED_ROWS')
                require(checked['counts']['observed_rows'] > 0, 'FTSHARE_CHECK_EMPTY_RESPONSE')
                if spec['id'] in ('raw-pair', 'raw-pair-repeat'):
                    require(all(len(checked['series'][code]) == 1 for code in spec['symbols']),
                        'FTSHARE_CHECK_PAIR_MISSING')
                if spec['id'] == 'raw-pair-repeat':
                    equal = checked['series'] == normalized['raw-pair']['series']
                    row['repeat_equal'] = equal
                    require(equal, 'FTSHARE_REPEAT_CHANGED')
            else:
                require(isinstance(body, dict) and type(body.get('code')) is int
                    and body['code'] == 200, 'FTSHARE_FACTOR_RESPONSE_NOT_QUALIFIED')
                row.update(status='retrieved-semantics-unverified',
                    factor_qualification_granted=False)
        except Exception as exc:
            row.update(status='failed', error=_safe_error(exc),
                transport=getattr(client, 'last_error_meta', None) or getattr(client, 'last_response_meta', None))
            failed = True
        save()
    successful = sum(x['status'] == 'qualified-response' for x in report['results'])
    report.update(status='validated' if successful == 5 and not failed else 'partial' if successful else 'failed',
        completed_at=clock(), required_candle_checks=5, qualified_candle_checks=successful,
        source_requests=client.requests)
    save()
    return 0 if report['status'] == 'validated' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('prepare', 'capture', 'release'))
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        event = _json(_regular(Path(os.environ['GITHUB_EVENT_PATH']), 1024 * 1024))
        return execute(args.stage, event, os.environ, args.out)
    except Exception as exc:
        print(_safe_error(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
