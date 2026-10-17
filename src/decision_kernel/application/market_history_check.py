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
    MASTER_FIELDS, FACTOR_FIELDS, inspect_security_master, qualified_adj_factors,
    reconcile_adj_factors, _within_listing,
)
from decision_kernel.application.market_custody import MAX_QUERIES
from decision_kernel.application.market_history_suspensions import period_suspensions
from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_session_check import session_scope
from decision_kernel.application.market_member_recovery import retry_context
from decision_kernel.application.market_retry_diagnostics import retry_diagnostics
from decision_kernel.application.market_source_check import _code, _last_attempt_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, encoded, digest, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError

HISTORY_FIELDS = {'daily': {'ts_code', 'trade_date', 'close'},
                  'adj_factor': FACTOR_FIELDS, 'suspend_d': SUSPENSION_FIELDS}
HISTORY_SELECTIONS = ('stock-history', 'stock-history-reconcile')


def history_codes(value, *, reconcile=False):
    require(isinstance(value, list) and 1 <= len(value) <= 8
            and all(isinstance(c, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ)', c) for c in value)
            and len(set(value)) == len(value), 'MARKET_HISTORY_CODES')
    require(not reconcile or len(value) <= 4, 'MARKET_HISTORY_RECONCILE_COHORT_BOUND')
    return list(value)


def check_stock_history_sources(*, registry, exchange, start, end, stock_codes,
        access_confirmed=False, request_context_confirmed=False, selection='stock-history',
        client=None, now=None, progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    report, _ = _collect_stock_history_sources(registry=registry, exchange=exchange, start=start,
        end=end, stock_codes=stock_codes, access_confirmed=access_confirmed,
        request_context_confirmed=request_context_confirmed, selection=selection,
        client=client, now=now, progress=progress, sleep=sleep, monotonic=monotonic, clock=clock)
    return report  # The public check host never receives or exports captured payloads.


def _collect_stock_history_sources(*, registry, exchange, start, end, stock_codes,
        access_confirmed=False, request_context_confirmed=False, selection='stock-history',
        client=None, now=None, progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    """Compose existing per-venue reading inputs in memory, without custody or export."""
    now = datetime.now(timezone.utc) if now is None else now
    through = session_scope(exchange, start, end, now)
    reconcile = selection == 'stock-history-reconcile'
    codes = history_codes(stock_codes, reconcile=reconcile)
    require(selection in HISTORY_SELECTIONS and access_confirmed is True
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
    require(len(specs) <= MAX_QUERIES, 'MARKET_HISTORY_QUERY_BOUND')
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
    if reconcile:
        report.update(original_batch_revalidated=False, point_budget=min(16, MAX_QUERIES - len(specs)),
                      point_budget_exceeded=False)
        report['limitations'] += ['SAME_INPUT_BASELINE_AND_COMBINED_READING_NOT_OLD_BATCH_RECOVERY',
            'SAME_PROVIDER_POINTS; NO_BASELINE_REWRITE_OR_FACTOR_FILL',
            'IN_MEMORY_PAYLOAD_NOT_RETAINED_SOURCE_OR_INDEPENDENT_SESSION_RECOVERY']

    def register(slot, use, pages):
        api = use['api']
        report['queries'][slot] = {'api': api, 'status': 'not-attempted', 'reason_code': 'DEPENDENCY_UNAVAILABLE',
            'row_count': None, 'page_count': None, 'http_attempts': 0, 'last_attempt_failure': None, 'retry_diagnostics': None,
            'retry_context': {'eligible': False}, 'request_context': {'schema': 'market-history-request-v1',
                'api': api, 'parameters': deepcopy(use['parameters']), 'query_key': use['query_key'],
                'max_pages': pages, 'registry_sha256': digest(encoded(registry.snapshot))}}
    for spec, use in zip(specs, plan['requests']):
        register(spec[0], use, spec[5])
    def emit():
        if progress is not None:
            progress(deepcopy(report))
    emit()  # Complete cohort and every generated query, before credential validation or I/O.
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, sleep=sleep, monotonic=monotonic, clock=clock, deadline=monotonic() + 600)
    initial = client.requests
    calendars, masters, results, lag = {}, {}, {}, {}

    def take(slot, use, pages, qualify):
        item = report['queries'][slot]
        if report['stopped']:
            item['reason_code'] = 'GLOBAL_SOURCE_STOP'; emit(); return None
        before = client.requests
        item.update(status='in-progress', reason_code=None); emit()
        try:
            capture = capture_registered(use, registry, session, max_pages=pages)
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
            report['http_attempts'] = client.requests - initial; emit()

    for spec, use in zip(specs, plan['requests']):
        slot, api, params, _, _, pages = spec
        venue = params.get('exchange') or owner[params['ts_code']]
        if report['stopped']:
            report['queries'][slot]['reason_code'] = 'GLOBAL_SOURCE_STOP'; emit(); continue
        if api == 'stock_basic' and venue not in calendars:
            continue
        if api in HISTORY_FIELDS:
            reference = masters.get(venue, {}).get('securities', {}).get(params['ts_code'])
            if reference is None or not _within_listing(_range(start, end)[1], reference):
                continue

        def qualify(result):
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
                report['queries'][slot]['reference_quality'] = masters[venue]['quality']
            elif api == 'suspend_d':
                proof = period_suspensions(result, registry=registry, code=params['ts_code'], start=start,
                                           end=end, sessions=set(calendars[venue]))
                report['queries'][slot]['quarantined_day_count'] = proof['quarantined_day_count']
            else:
                reader = qualified_daily if api == 'daily' else qualified_adj_factors
                reader(result, registry=registry, ts_code=params['ts_code'], start=start, end=end,
                       calendar=calendars[venue])
        take(slot, use, pages, qualify)
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

    if reconcile:
        baseline, original_raw, _ = _history_samples(payloads, registry, codes, owner, calendars, lag)
        gaps = []
        for index, code in enumerate(codes, 1):
            slot, venue = f'stock_{index}_adj_factor', owner[code]
            if report['queries'][slot]['status'] not in ('checked', 'checked-empty'):
                continue  # An invalid interval is unknown, never replaceable by point queries.
            audit = reconcile_adj_factors(results[slot], {}, registry=registry, ts_code=code,
                start=start, end=end, calendar=calendars[venue])[2]
            payloads[venue].setdefault('factor_supplements', {})[code] = {}
            gaps += [(index, day.replace('-', '')) for day in audit['baseline_missing_sessions']]
        report['point_budget_exceeded'] = len(gaps) > report['point_budget']
        points = []
        if gaps and not report['point_budget_exceeded']:
            point_plan = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'market-source-check',
                'requests': [{'api': 'adj_factor', 'stage': 'baseline',
                    'purpose': 'Same-input history factor gap consumption; no source export',
                    'parameters': {'ts_code': codes[i - 1], 'trade_date': day,
                        'fields': ','.join(sorted(FACTOR_FIELDS)), 'limit': '5000'}} for i, day in gaps]}, registry)
            for (index, day), use in zip(gaps, point_plan['requests']):
                slot = f'stock_{index}_factor_point_{day}'
                register(slot, use, 2)
                points.append((slot, index, day, use))
        emit()  # Entire point plan precedes the first point request; no partial budget selection.
        for slot, index, day, use in points:
            code = codes[index - 1]
            def qualify_point(result):
                audit = reconcile_adj_factors(results[f'stock_{index}_adj_factor'], {day: result},
                    registry=registry, ts_code=code, start=start, end=end, calendar=calendars[owner[code]])[2]
                point = audit['points'][day]
                require(point['status'] != 'rejected', point.get('reason_code', 'MARKET_FACTOR_POINT_REJECTED'))
                return point
            take(slot, use, 2, qualify_point)
            if slot in results:
                # Incomplete or invalid points also stay present, so the ordinary reader owns degradation.
                payloads[owner[code]]['factor_supplements'][code][day] = results[slot]

    samples, current_raw, audits = _history_samples(payloads, registry, codes, owner, calendars, lag)
    if reconcile:
        for slot, sample in samples.items():
            audit = audits.get(slot)
            sample.update(baseline=baseline[slot],
                raw_horizons_unchanged=(original_raw[slot] == current_raw[slot]) if slot in original_raw and slot in current_raw else None,
                factor_reconciliation={
                    'baseline_gap_count': len(audit['baseline_missing_sessions']) if audit else None,
                    'supplemented_count': len(audit['supplemented_sessions']) if audit else None,
                    'remaining_gap_count': len(audit['remaining_missing_sessions']) if audit else None,
                    'original_capture_rewritten': False})
    report['surfaces'] = samples
    report['reference_scope'] = {v: {'scope_count': masters[v]['scope_count'] if v in masters else None,
        'quarantined_count': masters[v]['quality']['quarantined_count'] if v in masters else None,
        'cohort_count': sum(owner[c] == v for c in codes)} for v in venues}
    complete = (all(s['cohort_history_qualified'] for s in samples.values())
        and (not reconcile or all(s['raw_horizons_unchanged'] is True for s in samples.values()))
        and not report.get('point_budget_exceeded', False)
        and all(q['status'] in ('checked', 'checked-empty') and q.get('quarantined_day_count', 0) == 0
                for q in report['queries'].values()))
    quarantined = any(m['quality']['quarantined_count'] for m in masters.values())
    report['status'] = ('available-with-quarantine' if quarantined else 'checked-' + selection) if complete else 'completed-with-gaps'
    emit()
    return report, payloads


def _history_samples(payloads, registry, codes, owner, calendars, lag):
    """Project only counters; raw horizons used for comparison never enter receipts."""
    samples, raw_horizons, audits = {}, {}, {}
    for index, code in enumerate(codes, 1):
        venue = owner[code]
        slot = f'stock_{index}'
        sample = {'status': 'unavailable', 'calendar_session_count': len(calendars[venue]) if venue in calendars else None,
            'session_lag': lag.get(venue), 'raw_horizons': None, 'qfq_horizons': None, 'quote_counts': None,
            'factor_gap_count': None, 'factor_change_count': None, 'suspension_evidence_status': 'not-assessed',
            'suspension_quarantined_day_count': None}
        if venue in payloads:
            context = read_market_payload(payloads[venue], registry, entity_id=code)
            raw, qfq = context['raw']['entries'], context['qfq']['entries']
            if raw and qfq:
                a, b = raw[0], qfq[0]
                raw_horizons[slot] = a['horizons']
                audits[slot] = a['qualification']['factor'].get('reconciliation')
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
        samples[slot] = sample
    return samples, raw_horizons, audits
