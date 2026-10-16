"""Exact member-request recovery; no supplier rows, dispatch, store or new retry loop.

Only the native receipt reader authenticates a prior run. A request digest alone
is NOT authentication. Re-execution starts at page zero because no old source
pages were retained; it never certifies the old batch or historical constituents.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import quote

from decision_kernel.application.research_sources import validate_plan
from decision_kernel.domain.records import encoded, digest, require
from decision_kernel.infrastructure.relay_retry import business_retry_delay

CONTEXT_SCHEMA = 'market-member-request-v1'
MEMBER_FIELDS = 'con_code,con_name,ts_code'
MEMBER_SLOTS = ('sector_members', 'concept_members', 'members')
RETRY_CLASSES = frozenset({'temporary-upstream', 'temporary-gateway', 'timeout',
                          'connection', 'dns-temporary', 'incomplete-read'})
RETRY_ERRORS = frozenset({'RELAY_HTTP_502', 'RELAY_HTTP_503', 'RELAY_HTTP_504',
                         'RELAY_HTTP_UNAVAILABLE', 'RELAY_BUSINESS_REFUSAL'})


def _time(value):
    require(isinstance(value, str) and len(value) <= 64, 'MEMBER_RECOVERY_TIME')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.utcoffset() is not None, 'MEMBER_RECOVERY_TIME')
    return parsed.astimezone(timezone.utc)


def member_code(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9]{6}\.TI', value),
            'MEMBER_RECOVERY_CODE')
    return value


def member_context(code, registry):
    request = {'stage': 'baseline', 'purpose': 'Transient market field check; no source export',
        'api': 'ths_member', 'parameters': {'ts_code': member_code(code),
            'fields': MEMBER_FIELDS, 'limit': '5000'}}
    use = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'market-source-check',
                         'requests': [request]}, registry)['requests'][0]
    return {'schema': CONTEXT_SCHEMA, 'api': use['api'], 'parameters': deepcopy(use['parameters']),
            'query_key': use['query_key'], 'max_pages': 2,
            'registry_sha256': digest(encoded(registry.snapshot))}


def retry_context(capture):
    """Last *terminal* failure only; a recovered earlier timeout grants no retry."""
    no = {'eligible': False}
    attempts = capture.get('attempts', [])
    if capture.get('complete') is not False or capture.get('error') not in RETRY_ERRORS or not attempts:
        return no
    last = attempts[-1]
    failure = last.get('failure') or {}
    if last.get('error') != capture['error'] or failure.get('classification') not in RETRY_CLASSES:
        return no
    delay = business_retry_delay(failure)
    if delay is None:
        return no
    try:
        received = _time(last.get('received_at'))
        not_before = received + timedelta(seconds=delay)
    except (ValueError, OverflowError):
        return no
    return {'eligible': True, 'classification': failure['classification'],
            'received_at': received.isoformat(), 'retry_after_seconds': delay,
            'not_before': not_before.isoformat()}


def selector(value):
    require(isinstance(value, dict) and set(value) == {'run_id', 'run_attempt', 'artifact_id', 'slot'}
            and all(type(value[k]) is int and 0 < value[k] < 10**15
                    for k in ('run_id', 'run_attempt', 'artifact_id'))
            and value['run_attempt'] == 1 and isinstance(value['slot'], str)
            and value['slot'] in MEMBER_SLOTS, 'MEMBER_RECOVERY_SELECTOR')
    return dict(value)


def recovery_request(native, selected, *, registry, exchange, start, end, now, current_run):
    """Validate a native-reader outcome, then return one immutable request context."""
    selected = selector(selected)
    require(isinstance(native, dict) and native.get('family') == 'market'
            and native.get('verification') == 'native-artifact-digest-and-exact-run-attempt',
            'MEMBER_RECOVERY_NATIVE_PROOF_REQUIRED')
    identity = native['identity']
    require(all(identity[k] == selected[k] for k in ('run_id', 'run_attempt', 'artifact_id'))
            and identity['run_id'] != int(current_run) and identity['conclusion'] == 'failure'
            and _time(identity['started_at']) < now
            and native.get('jobs', {}).get('check', {}).get('conclusion') == 'failure',
            'MEMBER_RECOVERY_ORIGIN')
    report = native['documents']['result.json']
    require(isinstance(report, dict) and report.get('schema') == 'market-source-check-v1',
            'MEMBER_RECOVERY_RECEIPT')
    source_id = report.get('identity')
    require(isinstance(source_id, dict) and source_id.get('repo') == identity['repository']
            and source_id.get('code_sha') == identity['code_sha']
            and source_id.get('run_id') == str(identity['run_id']) and source_id.get('attempt') == '1'
            and _time(identity['started_at']) <= _time(source_id.get('started_at')) < now,
            'MEMBER_RECOVERY_IDENTITY')
    require('recovery' not in report and report.get('selection') in ('sample', 'availability', 'members'),
            'MEMBER_RECOVERY_CHAIN_FORBIDDEN')
    require(report.get('status') == 'completed-with-gaps' and report.get('stopped') is False
            and report.get('supplier_data_retained') is False and report.get('source_refs_created') is False,
            'MEMBER_RECOVERY_NOT_RETRYABLE')
    scope = report.get('scope')
    require(isinstance(scope, dict) and scope == {'exchange': exchange, 'start': start, 'end': end, 'list_status': 'L'},
            'MEMBER_RECOVERY_SCOPE_CHANGED')
    item = report.get('queries', {}).get(selected['slot'])
    require(isinstance(item, dict) and item.get('api') == 'ths_member'
            and item.get('status') == 'capture-incomplete' and item.get('reason_code') in RETRY_ERRORS
            and type(item.get('http_attempts')) is int and item['http_attempts'] > 0,
            'MEMBER_RECOVERY_NOT_FAILED_QUERY')
    require(report.get('request_context_confirmed') is True and 'request_context' in item,
            'MEMBER_RECOVERY_REQUEST_NOT_RECORDED')
    context = item['request_context']
    require(isinstance(context, dict) and isinstance(context.get('parameters'), dict),
            'MEMBER_RECOVERY_REQUEST_INVALID')
    expected = member_context(context['parameters'].get('ts_code'), registry)
    require(encoded(context) == encoded(expected), 'MEMBER_RECOVERY_REQUEST_CHANGED')
    retry = item.get('retry_context')
    require(isinstance(retry, dict) and set(retry) == {'eligible', 'classification', 'received_at',
                'retry_after_seconds', 'not_before'} and retry['eligible'] is True
            and isinstance(retry['classification'], str) and retry['classification'] in RETRY_CLASSES
            and retry['classification'] == (item.get('last_attempt_failure') or {}).get('classification')
            and type(retry['retry_after_seconds']) is int and retry['retry_after_seconds'] >= 30,
            'MEMBER_RECOVERY_RETRY_NOT_ESTABLISHED')
    received = _time(retry['received_at'])
    require(_time(source_id['started_at']) <= received < now
            and _time(retry['not_before']) == received + timedelta(seconds=retry['retry_after_seconds'])
            and now >= _time(retry['not_before']), 'MEMBER_RECOVERY_RETRY_AFTER_NOT_ELAPSED')
    require(all(type(item.get(k)) is int and item[k] >= 0 for k in ('page_count', 'row_count')),
            'MEMBER_RECOVERY_COUNTS')
    return {'member_code': context['parameters']['ts_code'], 'request_context': deepcopy(context),
            'parent': {**selected, 'code_sha': identity['code_sha'],
                'artifact_sha256': identity['artifact_sha256'], 'prior_status': item['status'],
                'prior_reason_code': item['reason_code'], 'previous_received_pages': item['page_count'],
                'previous_received_rows': item['row_count'], 'source_pages_reused': False,
                'mode': 'explicit-complete-query-retry-from-zero-not-batch-recovery'}}


def load_recovery(native_reader, selected, *, registry, exchange, start, end, now, current_run):
    """Native history prevents consuming the same parent twice; bounded/fail closed.

    Same workflow uses the existing serial source concurrency group. A queued
    peer has no receipt yet; when it runs it must check earlier completed peers.
    A started peer, incomplete listing, missing/expired receipt or >16 intervening
    runs stops before Relay. There is no invented durable exactly-once ledger.
    """
    selected = selector(selected)
    proof = {k: selected[k] for k in ('run_id', 'run_attempt', 'artifact_id')}
    native = native_reader.read_market(proof)
    plan = recovery_request(native, selected, registry=registry, exchange=exchange,
        start=start, end=end, now=now, current_run=current_run)
    base = '/repos/' + native_reader.binding.repository
    get = lambda path: native_reader.api.request('GET', base + path)
    latest = get(f'/actions/runs/{selected["run_id"]}')
    require(latest.get('id') == selected['run_id'] and latest.get('run_attempt') == 1
            and latest.get('status') == 'completed' and latest.get('conclusion') == 'failure'
            and latest.get('head_sha') == native['identity']['code_sha'],
            'MEMBER_RECOVERY_ORIGIN_CHANGED')
    created = _time(latest.get('created_at'))
    require(created <= _time(native['identity']['started_at']), 'MEMBER_RECOVERY_ORIGIN_CHANGED')
    since = quote('>=' + created.isoformat(), safe='')
    listing = get('/actions/workflows/market-source-check.yml/runs?event=workflow_dispatch&per_page=100&created=' + since)
    rows = listing.get('workflow_runs')
    require(isinstance(rows, list) and type(listing.get('total_count')) is int
            and listing['total_count'] == len(rows) and 0 < len(rows) <= 18
            and all(isinstance(row, dict) and type(row.get('id')) is int for row in rows)
            and len({row['id'] for row in rows}) == len(rows)
            and {selected['run_id'], int(current_run)} <= {row['id'] for row in rows},
            'MEMBER_RECOVERY_HISTORY_INCOMPLETE')
    for row in rows:
        if row['id'] in (selected['run_id'], int(current_run)) or row.get('status') == 'queued':
            continue
        require(row.get('status') == 'completed' and row.get('run_attempt') == 1,
                'MEMBER_RECOVERY_PEER_UNSETTLED')
        artifacts = get(f'/actions/runs/{row["id"]}/artifacts?per_page=100')
        values = artifacts.get('artifacts')
        require(isinstance(values, list) and artifacts.get('total_count') == len(values)
                and len(values) == 1 and type(values[0].get('id')) is int,
                'MEMBER_RECOVERY_PEER_RECEIPT_MISSING')
        peer = native_reader.read_market({'run_id': row['id'], 'run_attempt': 1, 'artifact_id': values[0]['id']})
        recovery = peer['documents']['result.json'].get('recovery')
        if recovery is not None:
            # An interrupted prior consumption also consumes this finite retry.
            require(isinstance(recovery, dict) and isinstance(recovery.get('parent'), dict),
                    'MEMBER_RECOVERY_PEER_RECEIPT_INVALID')
            parent = recovery['parent']
            require(not all(parent.get(k) == selected[k] for k in ('run_id', 'run_attempt', 'slot')),
                    'MEMBER_RECOVERY_ALREADY_CONSUMED')
    return plan
