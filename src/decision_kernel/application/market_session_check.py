"""Finite whole-session checks using the existing capture and retry engines.

This is orchestration for one real consumer, not a source client or retry policy.
Only counters and explicitly confirmed generated requests leave this operation.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import time

from decision_kernel.application.market_relay_inputs import _range, qualified_trade_calendar
from decision_kernel.application.market_security_inputs import MASTER_FIELDS, inspect_security_master
from decision_kernel.application.market_session_inputs import SESSION_SCHEMA, QUOTE_FIELDS, session_quotes
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_member_recovery import retry_context
from decision_kernel.application.market_source_check import _code, _last_attempt_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, digest, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError


def session_scope(exchange, start, end, now):
    require(exchange == 'CN', 'MARKET_SESSION_CN_REQUIRED')
    lo, hi = _range(start, end)
    require(isinstance(now, datetime) and now.utcoffset() is not None, 'MARKET_CHECK_CLOCK')
    through = now.astimezone(timezone(timedelta(hours=8))).date() - timedelta(days=1)
    require(date.fromisoformat(hi) <= through, 'MARKET_CHECK_PAST_DAYS_ONLY')
    require((through - date.fromisoformat(lo)).days < 120, 'MARKET_CHECK_RANGE')
    return through.strftime('%Y%m%d')


def check_session_sources(*, registry, exchange, start, end, access_confirmed=False,
                          request_context_confirmed=False, selection='session-coverage',
                          client=None, now=None, progress=None, sleep=time.sleep,
                          monotonic=time.monotonic, clock=None):
    now = datetime.now(timezone.utc) if now is None else now
    through = session_scope(exchange, start, end, now)
    require(selection == 'session-coverage' and access_confirmed is True
            and request_context_confirmed is True, 'MARKET_SESSION_PERMISSION_REQUIRED')
    require(isinstance(registry, RelayRegistry), 'MARKET_CHECK_REGISTRY')
    specifications = []
    for venue in ('SSE', 'SZSE'):
        specifications.append((venue + '_calendar', 'trade_cal',
            {'exchange': venue, 'start_date': start, 'end_date': through}, {'exchange','cal_date','is_open'}, 5000, 2))
    for venue in ('SSE', 'SZSE'):
        specifications.append((venue + '_master', 'stock_basic',
            {'exchange': venue, 'list_status': 'L'}, MASTER_FIELDS, 500, 20))
    for api, fields in QUOTE_FIELDS.items():
        specifications.append((api, api, {'trade_date': end}, fields, 5000, 2))
    plan = validate_plan({'schema':'research-source-plan-v1', 'case_id':'market-source-check',
        'requests':[{'api': api, 'stage':'baseline', 'purpose':'Finite SH/SZ session coverage; no source export',
                     'parameters':{**params,'fields':','.join(sorted(fields)),'limit':str(size)}}
                    for _,api,params,fields,size,_ in specifications]}, registry)
    report = {'schema':'market-source-check-v1', 'selection':selection, 'status':'in-progress',
        'scope':{'exchange':'CN','start':start,'end':end,'calendar_end':through,'list_status':'L'},
        'request_context_confirmed':True, 'supplier_data_retained':False,'source_refs_created':False,
        'stopped':False,'http_attempts':0,'surfaces':{},'queries':{},
        'limitations':['SINGLE_SESSION_CURRENT_L_SNAPSHOTS_NOT_HISTORICAL_OR_ALL_A_SHARES',
            'MISSING_QUOTES_NOT_ZERO_OR_CONFIRMED_SUSPENSION; NO_MULTI_HORIZON_ADJUSTMENT',
            'GENERATED_REQUEST_CONTEXT_NOT_CAPTURE; NO_SOURCE_ROWS_OR_PUBLICATION_LICENSE',
            'MEMBERS_RESUME_DOES_NOT_ACCEPT_SESSION_SLOTS; NO_AUTOMATIC_RETRY_OR_SCHEDULER']}
    for spec, use in zip(specifications, plan['requests']):
        slot, api, _, _, size, pages = spec
        report['queries'][slot] = {'api':api,'status':'not-attempted','reason_code':'DEPENDENCY_UNAVAILABLE',
            'row_count':None,'page_count':None,'http_attempts':0,'last_attempt_failure':None,'retry_context':{'eligible':False},
            'request_context':{'schema':'market-session-request-v1','api':api,'parameters':deepcopy(use['parameters']),
                'query_key':use['query_key'],'max_pages':pages,'registry_sha256':digest(encoded(registry.snapshot))}}
    def emit():
        if progress is not None: progress(deepcopy(report))
    emit()  # All fixed generated arguments are recorded BEFORE credential validation / HTTP.
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, sleep=sleep, monotonic=monotonic, clock=clock, deadline=monotonic()+600)
    before = client.requests
    payload = {'schema':SESSION_SCHEMA,'calendar_results':{},'master_results':{},'session_results':{},
               'start':start,'end':end,'calendar_end':through,'reference_policy':'isolate-identified'}
    usable = {}
    for spec, use in zip(specifications, plan['requests']):
        slot, api, params, _, _, pages = spec
        item = report['queries'][slot]
        if report['stopped']:
            item['reason_code'] = 'GLOBAL_SOURCE_STOP'; emit(); continue
        if api == 'stock_basic' and not usable.get(params['exchange'] + '_calendar'):
            continue
        if api in QUOTE_FIELDS and not any(usable.get(v + '_master') for v in ('SSE','SZSE')):
            continue
        item.update(status='in-progress',reason_code=None);emit()
        initial = client.requests
        try:
            capture = capture_registered(use, registry, session, max_pages=pages)
            report['stopped'] = ((capture.get('failure') or {}).get('classification') in {'auth-or-entitlement','rate-limit'}
                                or capture['error'] in {'RELAY_RUN_DEADLINE','RELAY_SECRET_REFLECTED'})
            item['last_attempt_failure'] = _last_attempt_failure(capture)
            item['retry_context'] = retry_context(capture)
            result = {'case_id':'market-source-check','use':use,
                'status':'captured' if capture['complete'] else 'capture-incomplete','capture':capture}
            mapping = 'calendar_results' if api == 'trade_cal' else 'master_results' if api == 'stock_basic' else 'session_results'
            payload[mapping][params['exchange'] if 'exchange' in params else api] = result
            view = inspect_result(result, registry)
            item.update(row_count=view['row_count'],page_count=view['page_count'])
            if not view['complete']:
                item.update(status='capture-incomplete',reason_code=_code(capture['error']));continue
            if api == 'trade_cal':
                sessions = qualified_trade_calendar(result,registry=registry,exchange=params['exchange'],start=start,end=through)
                require(_range(start,end)[1] in sessions, 'MARKET_RELAY_END_NOT_MARKET_SESSION')
                usable[slot] = True
            elif api == 'stock_basic':
                master = inspect_security_master({'L':result},registry=registry,exchange=params['exchange'])
                item['reference_quality'] = master['quality'];usable[slot] = bool(master['quality']['eligible_count'])
            else:
                quality = session_quotes(result,registry,api,end)
                item['quarantined_count'] = quality['quarantined_count']
            item.update(status='checked' if view['row_count'] else 'checked-empty',reason_code=None)
        except (RecordError, MarketInputError) as exc:
            reason = str(exc)
            item.update(status='rejected',reason_code=reason if reason in {
                'MARKET_RELAY_SESSION_IDENTITY','MARKET_RELAY_SESSION_DATE','MARKET_RELAY_SESSION_ROW_BOUND'
            } else _code(reason))
        finally:
            item['http_attempts'] = client.requests-initial; report['http_attempts'] = client.requests-before;emit()
    context = read_market_payload(payload, registry)
    # Explicit projection. Never emit context entries, lineage, prices or hashes.
    report['surfaces']['session'] = {k: deepcopy(context[k]) for k in (
        'scope_count','by_exchange','qualified_scope_covered','strict_reference_passed','outside_sh_sz')}
    complete = context['qualified_scope_covered'] and all(q['status']=='checked' for q in report['queries'].values())
    report['status'] = ('checked-session-coverage' if context['strict_reference_passed'] else 'available-with-quarantine') if complete else 'completed-with-gaps'
    emit()
    return report
