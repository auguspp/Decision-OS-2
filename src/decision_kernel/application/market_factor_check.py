"""Bounded factor-gap diagnosis through the existing manual source host.

Range completion is not market-date completeness. Exact-day queries are a new
explicit diagnostic purpose, not transport retries or original-batch recovery.
Only counts and opted-in generated request arguments leave this operation.
"""
from copy import deepcopy
from datetime import datetime, timezone
import time

from decision_kernel.application.market_history_check import history_codes
from decision_kernel.application.market_member_recovery import retry_context
from decision_kernel.application.market_retry_diagnostics import retry_diagnostics
from decision_kernel.application.market_relay_inputs import EXCHANGES, qualified_trade_calendar
from decision_kernel.application.market_security_inputs import FACTOR_FIELDS, reconcile_adj_factors
from decision_kernel.application.market_session_check import session_scope
from decision_kernel.application.market_source_check import _code, _last_attempt_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, encoded, digest, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError

MAX_POINTS = 16


def factor_codes(value):
    codes = history_codes(value)
    require(len(codes) <= 4, 'MARKET_FACTOR_COHORT_BOUND')
    return codes


def check_factor_sources(*, registry, exchange, start, end, stock_codes,
        access_confirmed=False, request_context_confirmed=False, selection='factor-audit',
        client=None, now=None, progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    now = datetime.now(timezone.utc) if now is None else now
    session_scope(exchange, start, end, now)
    codes = factor_codes(stock_codes)
    require(selection == 'factor-audit' and access_confirmed is True
            and request_context_confirmed is True, 'MARKET_FACTOR_PERMISSION_REQUIRED')
    require(isinstance(registry, RelayRegistry), 'MARKET_CHECK_REGISTRY')
    owner = {c: next(v for v, suffix in EXCHANGES.items() if c.endswith('.' + suffix)) for c in codes}
    report = {'schema': 'market-source-check-v1', 'selection': selection, 'status': 'in-progress',
        'scope': {'exchange': 'CN', 'start': start, 'end': end}, 'cohort_count': len(codes),
        'request_context_confirmed': True, 'supplier_data_retained': False, 'source_refs_created': False,
        'original_batch_revalidated': False, 'stopped': False, 'http_attempts': 0,
        'point_budget': MAX_POINTS, 'point_budget_exceeded': False, 'queries': {}, 'surfaces': {},
        'limitations': ['CALENDAR_FACTOR_COVERAGE_NOT_LISTING_PRICE_OR_QFQ_HISTORY_APPROVAL',
            'SAME_PROVIDER_QUERY_SHAPES_NOT_INDEPENDENT_PROVIDER_VERIFICATION',
            'GENERATED_POINT_DATES_NOT_REPLAYABLE_SOURCE_ROWS; NO_FACTOR_VALUE_EXPORT',
            'BASELINE_CAPTURE_UNCHANGED; NO_INTERPOLATION_OR_PREVIOUS_FACTOR_FILL']}
    plans, results, calendars, baseline = {}, {}, {}, {}

    def emit():
        if progress is not None:
            progress(deepcopy(report))

    def register(slot, api, parameters, fields):
        use = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'market-source-check',
            'requests': [{'api': api, 'stage': 'baseline', 'purpose': 'Finite factor gap diagnosis; no source export',
                'parameters': {**parameters, 'fields': ','.join(sorted(fields)), 'limit': '5000'}}]}, registry)['requests'][0]
        plans[slot] = use
        report['queries'][slot] = {'api': api, 'status': 'not-attempted', 'reason_code': 'DEPENDENCY_UNAVAILABLE',
            'row_count': None, 'page_count': None, 'http_attempts': 0, 'last_attempt_failure': None, 'retry_diagnostics': None,
            'retry_context': {'eligible': False}, 'request_context': {'schema': 'market-factor-request-v1',
                'api': api, 'parameters': deepcopy(use['parameters']), 'query_key': use['query_key'],
                'max_pages': 2, 'registry_sha256': digest(encoded(registry.snapshot))}}

    venues = [v for v in EXCHANGES if v in owner.values()]
    for venue in venues:
        register(venue + '_calendar', 'trade_cal', {'exchange': venue, 'start_date': start, 'end_date': end},
                 {'exchange', 'cal_date', 'is_open'})
    for i, code in enumerate(codes, 1):
        register(f'factor_{i}', 'adj_factor', {'ts_code': code, 'start_date': start, 'end_date': end}, FACTOR_FIELDS)
    emit()  # Entire baseline cohort is recorded before any source I/O.
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, sleep=sleep, monotonic=monotonic, clock=clock, deadline=monotonic() + 600)
    initial = client.requests

    def take(slot, qualify):
        item, use = report['queries'][slot], plans[slot]
        if report['stopped']:
            item['reason_code'] = 'GLOBAL_SOURCE_STOP'; emit(); return None
        before = client.requests
        item.update(status='in-progress', reason_code=None); emit()
        try:
            capture = capture_registered(use, registry, session, max_pages=2)
            report['stopped'] = ((capture.get('failure') or {}).get('classification') in {'auth-or-entitlement', 'rate-limit'}
                or capture['error'] in {'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED'})
            item['last_attempt_failure'], item['retry_context'] = _last_attempt_failure(capture), retry_context(capture)
            item['retry_diagnostics'] = retry_diagnostics(capture)
            result = {'case_id': 'market-source-check', 'use': use,
                'status': 'captured' if capture['complete'] else 'capture-incomplete', 'capture': capture}
            results[slot] = result
            view = inspect_result(result, registry)
            item.update(row_count=view['row_count'], page_count=view['page_count'])
            if not view['complete']:
                item.update(status='capture-incomplete', reason_code=_code(capture['error'])); return None
            qualified = qualify(result)
            item.update(status='checked' if view['row_count'] else 'checked-empty', reason_code=None)
            return qualified
        except (RecordError, MarketInputError) as exc:
            item.update(status='rejected', reason_code=_code(str(exc)))
            return None
        finally:
            item['http_attempts'] = client.requests - before
            report['http_attempts'] = client.requests - initial
            emit()

    for venue in venues:
        days = take(venue + '_calendar', lambda r: qualified_trade_calendar(r, registry=registry,
                    exchange=venue, start=start, end=end))
        if days is not None:
            calendars[venue] = days
    for i, code in enumerate(codes, 1):
        if owner[code] not in calendars:
            continue
        baseline[i] = take(f'factor_{i}', lambda r: reconcile_adj_factors(r, {}, registry=registry,
            ts_code=code, start=start, end=end, calendar=calendars[owner[code]]))
    gaps = [(i, day) for i, value in baseline.items() if value is not None
            for day in value[2]['baseline_missing_sessions']]
    report['point_budget_exceeded'] = len(gaps) > MAX_POINTS
    points = {}
    if not report['point_budget_exceeded']:
        for i, day in gaps:
            compact = day.replace('-', '')
            slot = f'factor_{i}_point_{compact}'
            register(slot, 'adj_factor', {'ts_code': codes[i - 1], 'trade_date': compact}, FACTOR_FIELDS)
            points[slot] = (i, compact)
    emit()  # All generated gap requests, including their dates, precede point I/O.
    for slot, (i, day) in points.items():
        def qualify_point(result):
            audit = reconcile_adj_factors(results[f'factor_{i}'], {day: result}, registry=registry,
                ts_code=codes[i - 1], start=start, end=end, calendar=calendars[owner[codes[i - 1]]])[2]
            point = audit['points'][day]
            require(point['status'] != 'rejected', point.get('reason_code', 'MARKET_FACTOR_POINT_REJECTED'))
            return point
        take(slot, qualify_point)
    all_complete = not report['point_budget_exceeded']
    for i, code in enumerate(codes, 1):
        sample = {'status': 'unavailable', 'calendar_session_count': len(calendars[owner[code]]) if owner[code] in calendars else None,
            'baseline_factor_count': None, 'baseline_gap_count': None, 'point_observed_count': 0,
            'remaining_gap_count': None, 'combined_factor_count': None, 'combined_calendar_covered': False}
        if baseline.get(i) is not None:
            supplied = {d: results[s] for s, (index, d) in points.items() if index == i and s in results}
            merged, _, audit = reconcile_adj_factors(results[f'factor_{i}'], supplied, registry=registry,
                ts_code=code, start=start, end=end, calendar=calendars[owner[code]])
            sample.update(status='read', baseline_factor_count=len(baseline[i][0]),
                baseline_gap_count=len(audit['baseline_missing_sessions']), point_observed_count=len(audit['supplemented_sessions']),
                remaining_gap_count=len(audit['remaining_missing_sessions']), combined_factor_count=len(merged),
                combined_calendar_covered=bool(calendars[owner[code]]) and not audit['remaining_missing_sessions'])
        all_complete = all_complete and sample['combined_calendar_covered']
        report['surfaces'][f'factor_{i}'] = sample
    all_complete = all_complete and all(q['status'] in ('checked', 'checked-empty') for q in report['queries'].values())
    report['status'] = 'checked-factor-audit' if all_complete else 'completed-with-gaps'
    emit()
    return report
