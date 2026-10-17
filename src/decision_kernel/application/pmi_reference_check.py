"""Use Relay in memory against a retained PMI reference; publish no supplier rows.

This is an exact-value comparison, not source authentication, a revised source,
publication permission or a company exposure calculation. Existing owners retain
all capture, retry, PMI interpretation and Store responsibilities.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import time
from urllib.parse import urlsplit

from decision_kernel.application.industry_pmi import pmi_source_plan, read_pmi_context, _stamp, _china_month
from decision_kernel.application.industry_reading import _available, _document
from decision_kernel.application.research_sources import capture_registered, validate_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, Selection, SourceRef, encoded, require
from decision_kernel.infrastructure.relay_failure import KNOWN_ERRORS
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.manufacturing_pmi import FIELDS, month_range, read_monthly_pmi

SCHEMA = 'industry-pmi-reference-check-plan-v1'
CASE = 'industry-pmi-reference-check'
REPOSITORY = 'auguspp/Decision-OS-2'
PUBLIC_ERRORS = frozenset({
    'SOURCE_PUBLIC_GET_NOT_ADMITTED', 'SOURCE_REQUESTED_FIELDS_MISSING',
    'SOURCE_COUNT_SUGGESTS_TRUNCATION', 'SOURCE_PAGINATION_STALLED', 'SOURCE_PAGE_BOUND_REACHED',
    'SOURCE_BUSINESS_FAILED', 'SOURCE_FIELDS_INVALID', 'SOURCE_ROWS_INVALID',
    'SOURCE_ROW_WIDTH_INVALID', 'SOURCE_CELL_INVALID', 'SOURCE_COUNT_INVALID',
    'RELAY_HTTP_UNAVAILABLE', 'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED',
    'RELAY_BUSINESS_REFUSAL', 'RELAY_AUTH_OR_ENTITLEMENT', 'RELAY_RATE_LIMIT',
    'PMI_DUPLICATE_MONTH', 'PMI_RETURNED_MONTH_OUTSIDE_REQUEST', 'PMI_MONTH_INVALID',
    'PMI_CAPTURE_AFTER_AS_OF', 'PMI_FUTURE_PERIOD', 'PMI_COMPLETE_CAPTURE_REQUIRED',
    'PMI_REFERENCE_INVALID', 'PMI_REFERENCE_INCOMPLETE', 'PMI_REFERENCE_SCOPE',
    'PMI_REFERENCE_CLOCK', 'PMI_CHECK_CLOCK_REGRESSED',
}) | frozenset('RELAY_HTTP_' + str(c) for c in (400, 401, 403, 404, 405, 429, 502, 503, 504))


def checked_plan(value):
    require(isinstance(value, dict) and set(value) == {'schema', 'start_month', 'end_month', 'reference'}
            and value['schema'] == SCHEMA, 'PMI_CHECK_PLAN')
    month_range(value['start_month'], value['end_month'])
    ref = SourceRef.from_dict(value['reference'])
    require(ref.manifest.repository == REPOSITORY and ref.selection == Selection(), 'PMI_CHECK_REFERENCE')
    return _document(encoded({**value, 'reference': asdict(ref)}).decode())


def reference_reading(view, plan, as_of):
    """Validate the existing curated NBS excerpt format; labels do not authenticate it."""
    require(encoded(view['reference']) == encoded(plan['reference']) and view['availability'] == 'stored-representation',
            'PMI_REFERENCE_INVALID')
    meta = view['manifest']['input']
    require(meta['kind'] == 'content' and meta['custody'] in ('excerpt', 'provided')
            and urlsplit(meta['origin']).hostname == 'www.stats.gov.cn', 'PMI_REFERENCE_INVALID')
    _available(meta, _stamp(as_of))
    doc = _document(view['data'])
    require(isinstance(doc, dict) and doc.get('unit') == 'index-percent'
            and doc.get('comparison_unit') == 'percentage-point'
            and doc.get('representation') == 'official-web-table-manual-transcription-not-original-html-or-relay',
            'PMI_REFERENCE_INVALID')
    require(doc.get('scope') == {'start_month': plan['start_month'], 'end_month': plan['end_month'],
                                'population': 'china-national-manufacturing'}, 'PMI_REFERENCE_SCOPE')
    # A reporting month must have ended by publication AND acquisition, not merely by today's check.
    for name in ('published_at', 'acquired_at'):
        stamp = meta[name]
        month = stamp[:7].replace('-', '')
        if month == plan['end_month']:
            from calendar import monthrange
            require(int(stamp[8:10]) == monthrange(int(month[:4]), int(month[4:]))[1], 'PMI_REFERENCE_CLOCK')
        else:
            require(month > plan['end_month'], 'PMI_REFERENCE_CLOCK')
    out = read_monthly_pmi(doc.get('rows'), start_month=plan['start_month'], end_month=plan['end_month'])
    require(out['status'] == 'read', 'PMI_REFERENCE_INCOMPLETE')
    return out


def compare_readings(reference, observed):
    """Return states and coordinates from the request, never either operand's values."""
    require(reference['status'] == 'read' and reference['scope'] == observed['scope'], 'PMI_REFERENCE_SCOPE')
    cells = {'matched': 0, 'different': 0, 'unavailable': 0}
    changes = dict(cells)
    differences = []
    for expected, actual in zip(reference['series'], observed['series'], strict=True):
        require(expected['field'] == actual['field'], 'PMI_REFERENCE_SCOPE')
        for a, b in zip(expected['points'], actual['points'], strict=True):
            require(a['month'] == b['month'], 'PMI_REFERENCE_SCOPE')
            status = ('unavailable' if b['level'] is None else 'matched'
                      if Decimal(str(a['level'])) == Decimal(str(b['level'])) else 'different')
            cells[status] += 1
            if status != 'matched':
                differences.append({'month': a['month'], 'field': expected['field'], 'status': status})
            if a['previous_month'] is not None:
                state = ('unavailable' if b['change_pp'] is None else 'matched'
                         if Decimal(str(a['change_pp'])) == Decimal(str(b['change_pp'])) else 'different')
                changes[state] += 1
    return {'cells': cells, 'adjacent_changes': changes, 'nonmatching_cells': differences,
            'all_cells_match': cells['different'] == cells['unavailable'] == 0,
            'comparison': 'exact-decimal-values-no-rounding-or-tolerance',
            'supplier_values_exported': False}


def _error(exc):
    value = str(exc)
    return value if value in PUBLIC_ERRORS else 'PMI_CHECK_REJECTED'


def _attempts(capture):
    """The original retry facts, narrowed to codes/counts/waits. No response IDs or hashes."""
    attempts = [a for p in capture['pages'] for a in p['attempts']] + capture.get('attempts', [])
    out = []
    for i, a in enumerate(attempts, 1):
        failure = a.get('failure') or {}
        transport = a.get('transport') or failure
        status, business = transport.get('http_status'), failure.get('business_error')
        wait, retry = a.get('wait_before_next_attempt'), failure.get('retry_after_seconds')
        out.append({'sequence': i, 'error': _error(a['error']) if a.get('error') else None,
            'http_status': status if type(status) is int and 100 <= status <= 599 else None,
            'business_error': business if isinstance(business, str) and business in KNOWN_ERRORS else None,
            'classification': failure.get('classification') if failure.get('classification') in (
                'auth-or-entitlement', 'rate-limit', 'source-unavailable', 'request-rejected',
                'temporary-upstream', 'temporary-gateway', 'http-unclassified', 'tls-certificate',
                'tls', 'dns-temporary', 'dns', 'timeout', 'incomplete-read', 'connection',
                'http-protocol', 'transport-unclassified') else None,
            'wait_before_next_attempt': wait if type(wait) is int and wait >= 0 else None,
            'retry_after_seconds': retry if type(retry) is int and retry >= 0 else None})
    return out


def check_reference(value, registry, store, *, access_confirmed=False, client=None,
                    clock=None, monotonic=time.monotonic, sleep=time.sleep, progress=None):
    require(access_confirmed is True, 'PMI_CHECK_ACCESS_REQUIRED')
    plan = checked_plan(value)
    clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
    started = clock()
    require(plan['end_month'] < _china_month(_stamp(started)), 'PMI_CHECK_CLOSED_MONTHS_REQUIRED')
    source_plan = pmi_source_plan(registry, case_id=CASE, start_month=plan['start_month'], end_month=plan['end_month'])
    use = validate_plan(source_plan, registry)['requests'][0]
    out = {'schema': 'industry-pmi-reference-check-v1', 'plan': plan, 'started_at': started,
        'request': use, 'catalogue_snapshot': deepcopy(registry.snapshot), 'status': 'in-progress',
        'source': {'http_requests': 0, 'pages': 0, 'rows': None, 'complete': False, 'attempts': []},
        'comparison': None, 'reference_checked': False, 'records_written': False,
        'supplier_rows_retained': False, 'research_executed': False,
        'limitations': ['RETAINED_CURATED_REFERENCE_NOT_AUTHENTICATED_NBS_ORIGINAL',
            'EQUALITY_NOT_HISTORICAL_PIT_OR_INDEPENDENT_ECONOMIC_EVIDENCE',
            'PMI_DIFFUSION_NOT_QUANTITY_INVENTORY_BALANCE_OR_COMPANY_BENEFIT',
            'COUNTS_ONLY_RECEIPT_CANNOT_REPLAY_SUPPLIER_INPUT_OR_CREATE_SOURCEREF',
            'ACCESS_NOT_SUPPLIER_REPUBLICATION_RIGHTS_OR_RESEARCH_ACCEPTANCE']}
    if progress:
        progress(deepcopy(out))
    phase = 'reference'
    try:
        ref = SourceRef.from_dict(plan['reference'])
        # Keep the original authenticated read scope open through the comparison.
        # Its exit check rejects withdrawal or changed binding before any comparison is returned.
        from contextlib import nullcontext
        scope = getattr(store.backend, 'source_read_scope', None)
        with scope(ref.manifest.commit) if scope else nullcontext(store.backend) as backend:
            expected = reference_reading(VersionedStore(backend).read_source(ref), plan, started)
            out['reference_checked'] = True
            phase = 'source'
            client = client or RelayHTTP()
            client.validate_credential()
            initial = client.requests
            session = RetryingRelay(client, clock=clock, monotonic=monotonic, sleep=sleep,
                                    deadline=monotonic() + 300)
            capture = capture_registered(use, registry, session, max_pages=2)
            out['source'] = {'http_requests': client.requests - initial, 'pages': len(capture['pages']),
                'rows': sum(len(p['table']['items']) for p in capture['pages']),
                'complete': capture['complete'], 'attempts': _attempts(capture)}
            pending = capture.get('pending_retry') or {}
            if pending.get('reason') in ('run-deadline', 'attempt-or-wait-bound'):
                wait = pending.get('retry_after_seconds')
                out['source']['pending_retry'] = {'reason': pending['reason'],
                    'retry_after_seconds': wait if type(wait) is int and wait >= 0 else None}
            if not capture['complete']:
                out.update(status='source-incomplete', error=_error(capture['error']))
            else:
                require(all(_stamp(p['captured_at']) >= _stamp(started) for p in capture['pages']),
                        'PMI_CHECK_CLOCK_REGRESSED')
                result = {'case_id': CASE, 'use': use, 'status': 'captured', 'capture': capture}
                observed = read_pmi_context(result, registry, start_month=plan['start_month'],
                                           end_month=plan['end_month'], as_of=clock())
                out['comparison'] = compare_readings(expected, observed)
                cells = out['comparison']['cells']
                out['status'] = ('incomplete' if cells['unavailable'] else 'matched'
                                 if out['comparison']['all_cells_match'] else 'different')
            phase = 'reference'  # includes scope exit and final mutable authorization check
        store.backend.head()
        finished = clock()
        require(_stamp(finished) >= _stamp(started), 'PMI_CHECK_CLOCK_REGRESSED')
        out['finished_at'] = finished
    except (RecordError, KeyError, TypeError, ValueError) as exc:
        out.update(status='reference-unavailable' if phase == 'reference' else 'source-rejected',
                   error=_error(exc), comparison=None)
        if phase == 'reference':
            out['reference_checked'] = False
    finally:
        if client is not None:
            client.last_response_bytes = None
    return out
