"""Finite explicit-cohort history check using original readers, capture and retry.

Only engineering counts and opted-in generated request arguments leave this
operation. This is not full-market discovery, a scheduler, or source custody.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re
import time

from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _range, qualified_trade_calendar, qualified_daily,
)
from decision_kernel.application.market_security_inputs import (
    MASTER_FIELDS, FACTOR_FIELDS, inspect_security_master, qualified_adj_factors, _within_listing,
)
from decision_kernel.application.market_history_suspensions import period_suspensions
from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_session_check import session_scope
from decision_kernel.application.market_member_recovery import retry_context
from decision_kernel.application.market_source_check import _code, _last_attempt_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, encoded, digest, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError

HISTORY_FIELDS = {'daily': {'ts_code', 'trade_date', 'close'},
                  'adj_factor': FACTOR_FIELDS, 'suspend_d': SUSPENSION_FIELDS}


def history_codes(value):
    require(isinstance(value, list) and 1 <= len(value) <= 8
            and all(isinstance(c, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ)', c) for c in value)
            and len(set(value)) == len(value), 'MARKET_HISTORY_CODES')
    return list(value)


def check_stock_history_sources(*, registry, exchange, start, end, stock_codes,
        access_confirmed=False, request_context_confirmed=False, selection='stock-history',
        client=None, now=None, progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    now = datetime.now(timezone.utc) if now is None else now
    through = session_scope(exchange, start, end, now)
    codes = history_codes(stock_codes)
    require(selection == 'stock-history' and access_confirmed is True
            and request_context_confirmed is True, 'MARKET_HISTORY_PERMISSION_REQUIRED')
    require(isinstance(registry, RelayRegistry), 'MARKET_CHECK_REGISTRY')
    venues = [v for v, suffix in EXCHANGES.items() if any(c.endswith('.' + suffix) for c in codes)]
    owner = {c: next(v for v in venues if c.endswith('.' + EXCHANGES[v])) for c in codes}
    specs = []
    for venue in venues:
        specs += [(venue + '_calendar', 'trade_cal', {'exchange': venue, 'start_date': start, 'end_date': end},
                   {'exchange', 'cal_date', 'is_open'}, 5000, 2),
                  (venue + '_master', 'stock_basic', {'exchange': venue, 'list_status': 'L'}, MASTER_FIELDS, 500, 20)]
        if end != through:
            specs.append((venue + '_freshness', 'trade_cal',
                {'exchange': venue, 'start_date': end, 'end_date': through}, {'exchange', 'cal_date', 'is_open'}, 5000, 2))
    for index, code in enumerate(codes, 1):
        for api, fields in HISTORY_FIELDS.items():
            specs.append((f'stock_{index}_{api}', api, {'ts_code': code, 'start_date': start, 'end_date': end}, fields, 5000, 2))
    plan = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'market-source-check', 'requests': [
        {'api': api, 'stage': 'baseline', 'purpose': 'Finite explicit stock history; no source export',
         'parameters': {**params, 'fields': ','.join(sorted(fields)), 'limit': str(size)}}
        for _, api, params, fields, size, _ in specs]}, registry)
    report = {'schema': 'market-source-check-v1', 'selection': selection, 'status': 'in-progress',
        'scope': {'exchange': 'CN', 'start': start, 'end': end, 'calendar_end': through, 'list_status': 'L'},
        'cohort_count': len(codes), 'request_context_confirmed': True, 'supplier_data_retained': False,
        'source_refs_created': False, 'stopped': False, 'http_attempts': 0, 'queries': {}, 'surfaces': {},
        'limitations': ['EXPLICIT_COHORT_NOT_FULL_MARKET_HISTORY_OR_DISCOVERY',
            'CURRENT_L_REFERENCE_NOT_HISTORICAL_UNIVERSE; NO_TOTAL_RETURN_OR_CORPORATE_ACTION_CERTIFICATION',
            'NO_SOURCE_VALUES_OR_ROWS; GENERATED_REQUESTS_NOT_REPLAYABLE_CAPTURE',
            'NO_HALT_FILL_OR_WINDOW_RECLASSIFICATION; MEMBERS_RESUME_NOT_APPLICABLE']}
    for spec, use in zip(specs, plan['requests']):
        slot, api, _, _, _, pages = spec
        report['queries'][slot] = {'api': api, 'status': 'not-attempted', 'reason_code': 'DEPENDENCY_UNAVAILABLE',
            'row_count': None, 'page_count': None, 'http_attempts': 0, 'last_attempt_failure': None,
            'retry_context': {'eligible': False}, 'request_context': {'schema': 'market-history-request-v1',
                'api': api, 'parameters': deepcopy(use['parameters']), 'query_key': use['query_key'],
                'max_pages': pages, 'registry_sha256': digest(encoded(registry.snapshot))}}
    def emit():
        if progress is not None:
            progress(deepcopy(report))
    emit()  # Complete cohort and every generated query, before credential validation or I/O.
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, sleep=sleep, monotonic=monotonic, clock=clock, deadline=monotonic() + 600)
    initial = client.requests
    calendars, masters, results, lag = {}, {}, {}, {}
    for spec, use in zip(specs, plan['requests']):
        slot, api, params, _, _, pages = spec
        item = report['queries'][slot]
        venue = params.get('exchange') or owner[params['ts_code']]
        if report['stopped']:
            item['reason_code'] = 'GLOBAL_SOURCE_STOP'; emit(); continue
        if api == 'stock_basic' and venue not in calendars:
            continue
        if api in HISTORY_FIELDS:
            reference = masters.get(venue, {}).get('securities', {}).get(params['ts_code'])
            if reference is None or not _within_listing(_range(start, end)[1], reference):
                continue
        before = client.requests
        item.update(status='in-progress', reason_code=None); emit()
        try:
            capture = capture_registered(use, registry, session, max_pages=pages)
            report['stopped'] = ((capture.get('failure') or {}).get('classification') in {'auth-or-entitlement', 'rate-limit'}
                                or capture['error'] in {'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED'})
            item['last_attempt_failure'], item['retry_context'] = _last_attempt_failure(capture), retry_context(capture)
            result = {'case_id': 'market-source-check', 'use': use,
                'status': 'captured' if capture['complete'] else 'capture-incomplete', 'capture': capture}
            results[slot] = result
            view = inspect_result(result, registry)
            item.update(row_count=view['row_count'], page_count=view['page_count'])
            if not view['complete']:
                item.update(status='capture-incomplete', reason_code=_code(capture['error'])); continue
            if api == 'trade_cal':
                days = qualified_trade_calendar(result, registry=registry, exchange=venue,
                                                start=params['start_date'], end=params['end_date'])
                require(_range(start, end)[1] in days, 'MARKET_RELAY_END_NOT_MARKET_SESSION')
                if slot.endswith('_freshness'):
                    lag[venue] = sum(d > _range(start, end)[1] for d in days)
                else:
                    calendars[venue] = days
                    if end == through:
                        lag[venue] = 0
            elif api == 'stock_basic':
                masters[venue] = inspect_security_master({'L': result}, registry=registry, exchange=venue)
                item['reference_quality'] = masters[venue]['quality']
            elif api == 'suspend_d':
                proof = period_suspensions(result, registry=registry, code=params['ts_code'], start=start,
                                           end=end, sessions=set(calendars[venue]))
                item['quarantined_day_count'] = proof['quarantined_day_count']
            else:
                qualify = qualified_daily if api == 'daily' else qualified_adj_factors
                qualify(result, registry=registry, ts_code=params['ts_code'], start=start, end=end,
                        calendar=calendars[venue])
            item.update(status='checked' if view['row_count'] else 'checked-empty', reason_code=None)
        except (RecordError, MarketInputError) as exc:
            item.update(status='rejected', reason_code=_code(str(exc)))
        finally:
            item['http_attempts'] = client.requests - before
            report['http_attempts'] = client.requests - initial; emit()
    payloads = {}
    for venue in venues:
        if venue not in masters or venue not in calendars:
            continue
        payloads[venue] = {'schema': 'relay-security-stock-reading-v1', 'exchange': venue, 'start': start, 'end': end,
            'calendar_result': results[venue + '_calendar'], 'master_results': {'L': results[venue + '_master']},
            'stock_results': {}, 'factor_results': {}, 'suspension_results': {}, 'reference_policy': 'isolate-identified'}
        for index, code in enumerate(codes, 1):
            if owner[code] != venue:
                continue
            for api, field in [('daily', 'stock_results'), ('adj_factor', 'factor_results'), ('suspend_d', 'suspension_results')]:
                slot = f'stock_{index}_{api}'
                if slot in results:
                    payloads[venue][field][code] = results[slot]
    complete = True
    for index, code in enumerate(codes, 1):
        venue = owner[code]
        sample = {'status': 'unavailable', 'calendar_session_count': len(calendars[venue]) if venue in calendars else None,
            'session_lag': lag.get(venue), 'raw_horizons': None, 'qfq_horizons': None, 'quote_counts': None,
            'factor_gap_count': None, 'factor_change_count': None, 'suspension_evidence_status': 'not-assessed',
            'suspension_quarantined_day_count': None}
        if venue in payloads:
            context = read_market_payload(payloads[venue], registry, entity_id=code)
            raw, qfq = context['raw']['entries'], context['qfq']['entries']
            if raw and qfq:
                a, b = raw[0], qfq[0]
                history, adjustment = a['history_quote_explanation'], a['qualification']['adjustment']
                sample.update(status='read', raw_horizons=[{'sessions': h['sessions'], 'status': h['status']} for h in a['horizons']],
                    qfq_horizons=[{'sessions': h['sessions'], 'status': h['status']} for h in b['horizons']],
                    quote_counts=history.get('counts'), suspension_evidence_status=history.get('evidence_status', 'not-assessed'),
                    suspension_quarantined_day_count=history.get('evidence_quarantined_day_count'),
                    factor_gap_count=len(adjustment['missing_factor_sessions']) if 'missing_factor_sessions' in adjustment else None,
                    factor_change_count=len(adjustment['factor_change_dates']) if 'factor_change_dates' in adjustment else None)
        okay = (sample['status'] == 'read' and sample['session_lag'] == 0 and sample['factor_gap_count'] == 0
            and sample['suspension_evidence_status'] == 'checked' and sample['suspension_quarantined_day_count'] == 0
            and sample['quote_counts'] is not None
            and all(v == 0 for k, v in sample['quote_counts'].items() if k != 'quote-observed')
            and all(h['status'] == 'observed' for h in sample['raw_horizons'] + sample['qfq_horizons']))
        sample['cohort_history_qualified'] = okay
        complete = complete and okay
        report['surfaces'][f'stock_{index}'] = sample
    report['reference_scope'] = {v: {'scope_count': masters[v]['scope_count'] if v in masters else None,
        'quarantined_count': masters[v]['quality']['quarantined_count'] if v in masters else None,
        'cohort_count': sum(owner[c] == v for c in codes)} for v in venues}
    complete = complete and all(q['status'] in ('checked', 'checked-empty')
        and q.get('quarantined_day_count', 0) == 0 for q in report['queries'].values())
    quarantined = any(m['quality']['quarantined_count'] for m in masters.values())
    report['status'] = ('available-with-quarantine' if quarantined else 'checked-stock-history') if complete else 'completed-with-gaps'
    emit()
    return report
