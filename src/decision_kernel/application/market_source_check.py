"""Non-publishing checks over existing Relay and market readers.

Supplier rows live only within this explicit call. The result is an engineering
receipt, NOT a capture, SourceRef, reproducible market snapshot or discovery.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import time

from decision_kernel.application.market_group_inputs import (
    CATALOG_FIELDS, GROUP_TYPES, LEVEL_FIELDS, MEMBER_FIELDS, qualified_ths_catalog,
    qualified_ths_members, read_three_market_surfaces,
)
from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _range, qualified_daily, qualified_trade_calendar,
)
from decision_kernel.application.market_security_inputs import (
    FACTOR_FIELDS, MASTER_FIELDS, qualified_adj_factors, qualified_security_master,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from decision_kernel.observations.market_daily import MarketInputError

SLOTS = {
    'calendar': 'trade_cal', 'master': 'stock_basic',
    'sector_catalog': 'ths_index', 'concept_catalog': 'ths_index',
    'stock_daily': 'daily', 'stock_factor': 'adj_factor',
    'sector_daily': 'ths_daily', 'sector_members': 'ths_member',
    'concept_daily': 'ths_daily', 'concept_members': 'ths_member',
}
# Only selected code-owned errors may leave the process; arbitrary exception
# text, source prose, response IDs, code/name/price values and hashes never do.
PUBLIC_CODES = frozenset({
    'SOURCE_PUBLIC_GET_NOT_ADMITTED', 'SOURCE_REQUESTED_FIELDS_MISSING',
    'SOURCE_RETURNED_SCOPE_MISMATCH', 'SOURCE_ROWS_INVALID', 'SOURCE_FIELDS_INVALID',
    'SOURCE_ROW_WIDTH_INVALID', 'SOURCE_TABLE_INVALID', 'SOURCE_BUSINESS_FAILED',
    'SOURCE_PAGINATION_STALLED', 'SOURCE_COUNT_SUGGESTS_TRUNCATION',
    'SOURCE_PAGE_BOUND_REACHED', 'SOURCE_COMPLETENESS_MISMATCH',
    'RELAY_HTTP_400', 'RELAY_HTTP_401', 'RELAY_HTTP_403', 'RELAY_HTTP_429',
    'RELAY_HTTP_502', 'RELAY_HTTP_503', 'RELAY_HTTP_504', 'RELAY_HTTP_UNAVAILABLE',
    'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED', 'RELAY_BUSINESS_REFUSAL',
    'MARKET_RELAY_CALENDAR_GAP', 'MARKET_RELAY_CALENDAR_EMPTY',
    'MARKET_RELAY_CALENDAR_ROW', 'MARKET_RELAY_DATE_INVALID',
    'MARKET_RELAY_MASTER_CURRENCY_UNSUPPORTED', 'MARKET_RELAY_MASTER_IDENTITY',
    'MARKET_RELAY_MASTER_LIST_DATE_REQUIRED', 'MARKET_RELAY_MASTER_DELIST_DATE',
    'MARKET_RELAY_GROUP_CLASSIFICATION', 'MARKET_RELAY_GROUP_CODE',
    'MARKET_RELAY_GROUP_MEMBER_IDENTITY', 'MARKET_RELAY_DAILY_ROW',
    'MARKET_RELAY_DAILY_CLOSE', 'MARKET_RELAY_FACTOR_ROW', 'MARKET_RELAY_FACTOR_VALUE',
    'MARKET_RELAY_END_NOT_MARKET_SESSION',
})


def _code(value: object) -> str:
    return value if isinstance(value, str) and value in PUBLIC_CODES else 'CHECK_REJECTED'


def check_scope(exchange: str, start: str, end: str, now: datetime) -> None:
    require(isinstance(exchange, str) and exchange in EXCHANGES, 'MARKET_CHECK_EXCHANGE')
    lo, hi = _range(start, end)
    require(isinstance(now, datetime) and now.utcoffset() is not None, 'MARKET_CHECK_CLOCK')
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    require(date.fromisoformat(hi) < today, 'MARKET_CHECK_PAST_DAYS_ONLY')
    require((date.fromisoformat(hi) - date.fromisoformat(lo)).days < 120, 'MARKET_CHECK_RANGE')


def check_sources(*, registry: RelayRegistry, exchange: str, start: str, end: str,
                  access_confirmed: bool = False, client=None, now: datetime | None = None,
                  progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None) -> dict:
    """Check one current listing slice and one source-selected member of each channel.

    Access authorization is not publication authorization. Unlike capture_plan,
    this use case has no capture/export result or writer. Its only output is the
    allowlisted receipt below. Existing publication-gated paths remain unchanged.
    No fallback identity, host, API or source retry budget is created.
    """
    require(access_confirmed is True, 'MARKET_CHECK_ACCESS_REQUIRED')
    require(isinstance(registry, RelayRegistry), 'MARKET_CHECK_REGISTRY')
    now = datetime.now(timezone.utc) if now is None else now
    check_scope(exchange, start, end, now)
    report = {'schema': 'market-source-check-v1', 'status': 'in-progress',
        'scope': {'exchange': exchange, 'start': start, 'end': end, 'list_status': 'L'},
        'sample_policy': 'first-sorted-code-per-qualified-source-catalogue-not-candidates',
        'queries': {k: {'api': api, 'status': 'not-attempted', 'reason_code': 'DEPENDENCY_UNAVAILABLE',
                        'row_count': None, 'page_count': None, 'http_attempts': 0}
                    for k, api in SLOTS.items()},
        'surfaces': {}, 'http_attempts': 0, 'stopped': False,
        'supplier_data_retained': False, 'source_refs_created': False,
        'limitations': ['DIAGNOSTICS_NOT_REPLAYABLE_CAPTURE_OR_PUBLICATION_LICENSE',
            'ONE_SOURCE_SORTED_SAMPLE_NOT_FULL_MARKET_DISCOVERY_OR_RESEARCH_ADOPTION',
            'CURRENT_DIRECTORY_NOT_HISTORICAL_MEMBERSHIP_OR_PIT',
            'NO_SOURCE_VALUES_NAMES_IDENTIFIERS_OR_DIGESTS_IN_RECEIPT']}
    def emit():
        if progress is not None:
            progress(deepcopy(report))
    emit()
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, sleep=sleep, monotonic=monotonic, clock=clock,
                            deadline=monotonic() + 600)
    initial_requests = client.requests
    stopped = False

    def take(slot, params, fields, qualify):
        nonlocal stopped
        item = report['queries'][slot]
        if stopped:
            item['reason_code'] = 'GLOBAL_SOURCE_STOP'
            emit()
            return None, None
        api = SLOTS[slot]
        request = {'stage': 'baseline', 'purpose': 'Transient market field check; no source export',
                   'api': api, 'parameters': {**params, 'fields': ','.join(sorted(fields)), 'limit': '5000'}}
        before = client.requests
        result = None
        try:
            use = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'market-source-check',
                                 'requests': [request]}, registry)['requests'][0]
            item.update(status='in-progress', reason_code=None)
            emit()
            capture = capture_registered(use, registry, session, max_pages=2)
            stopped = ((capture.get('failure') or {}).get('classification') in {'auth-or-entitlement', 'rate-limit'}
                       or capture['error'] in {'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED'})
            report['stopped'] = stopped
            result = {'case_id': 'market-source-check', 'use': use,
                      'status': 'captured' if capture['complete'] else 'capture-incomplete', 'capture': capture}
            view = inspect_result(result, registry)
            item.update(row_count=view['row_count'], page_count=view['page_count'])
            if not view['complete']:
                item.update(status='capture-incomplete', reason_code=_code(capture['error']))
                return result, None
            qualified = qualify(result)
            item.update(status='checked' if view['row_count'] else 'checked-empty', reason_code=None)
            return result, qualified
        except (RecordError, MarketInputError) as exc:
            item.update(status='rejected', reason_code=_code(str(exc)))
            return result, None
        finally:
            item['http_attempts'] = client.requests - before
            report['http_attempts'] = client.requests - initial_requests
            emit()

    period = {'start_date': start, 'end_date': end}
    cal, sessions = take('calendar', {'exchange': exchange, **period}, {'exchange', 'cal_date', 'is_open'},
        lambda r: qualified_trade_calendar(r, registry=registry, exchange=exchange, start=start, end=end))
    master_result, master = take('master', {'exchange': exchange, 'list_status': 'L'}, MASTER_FIELDS,
        lambda r: qualified_security_master({'L': r}, registry=registry, exchange=exchange))
    catalog_results, catalogs = {}, {}
    for kind in GROUP_TYPES:
        catalog_results[kind], catalogs[kind] = take(kind + '_catalog',
            {'exchange': 'A', 'type': GROUP_TYPES[kind]}, CATALOG_FIELDS,
            lambda r, k=kind: qualified_ths_catalog(r, registry=registry, kind=k))
    stocks, factors = {}, {}
    has_end = _range(start, end)[1] in (sessions or [])
    if master and master['securities'] and has_end:
        code = min(master['securities'])
        for slot, fields, qualifier, target in (
                ('stock_daily', LEVEL_FIELDS, qualified_daily, stocks),
                ('stock_factor', FACTOR_FIELDS, qualified_adj_factors, factors)):
            result, _ = take(slot, {'ts_code': code, **period}, fields,
                lambda r, q=qualifier: q(r, registry=registry, ts_code=code, start=start, end=end, calendar=sessions))
            if result is not None:
                target[code] = result
    surfaces = {'stock': {'calendar_result': cal or {}, 'master_results': {'L': master_result or {}},
        'stock_results': stocks, 'factor_results': factors, 'exchange': exchange, 'start': start, 'end': end}}
    for kind in GROUP_TYPES:
        levels, members = {}, {}
        catalog = catalogs[kind]
        if catalog and catalog['groups']:
            code = min(catalog['groups'])
            if has_end:
                result, _ = take(kind + '_daily', {'ts_code': code, **period}, LEVEL_FIELDS,
                    # THS uses source index points, not CNY; _levels enforces its existing contract.
                    lambda r: _group_levels(r, registry, code, start, end, sessions))
                if result is not None:
                    levels[code] = result
            result, _ = take(kind + '_members', {'ts_code': code}, MEMBER_FIELDS,
                lambda r: qualified_ths_members(r, registry=registry, ts_code=code))
            if result is not None:
                members[code] = result
        surfaces[kind] = {'catalog_result': catalog_results[kind] or {}, 'member_results': members,
            'index_results': levels, 'calendar_result': cal or {}, 'exchange': exchange, 'start': start, 'end': end}
    views = read_three_market_surfaces(surfaces=surfaces, registry=registry, limit=1)
    for kind, outcome in views['surfaces'].items():
        item = {'status': outcome['status'], 'scope_count': None, 'observed_count': None}
        if outcome['status'] == 'read':
            view = outcome['view']
            source = view['qfq'] if kind == 'stock' else view
            item.update(scope_count=source['scope_count'], observed_count=source['observed_count'])
        else:
            item['reason_code'] = _code(outcome.get('reason_code'))
        report['surfaces'][kind] = item
    good = (all(q['status'] == 'checked' for q in report['queries'].values())
            and all((s['observed_count'] or 0) > 0 for s in report['surfaces'].values()))
    if stopped:
        for item in report['queries'].values():
            if item['status'] == 'not-attempted':
                item['reason_code'] = 'GLOBAL_SOURCE_STOP'
    report['status'] = 'checked-sample' if good else 'completed-with-gaps'
    emit()
    return report


def _group_levels(result, registry, code, start, end, sessions):
    from decision_kernel.application.market_group_inputs import _levels
    return _levels(result, registry, code, start, end, set(sessions))
