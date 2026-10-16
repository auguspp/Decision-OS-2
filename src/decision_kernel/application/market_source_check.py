"""Non-publishing checks over existing Relay and market readers.

Supplier rows live only within this explicit call. The result is an engineering
receipt, NOT a capture, SourceRef, reproducible market snapshot or discovery.
"""
from __future__ import annotations

from copy import deepcopy
import math
from datetime import date, datetime, timedelta, timezone
import time

from decision_kernel.application.market_group_inputs import (
    CATALOG_FIELDS, GROUP_TYPES, LEVEL_FIELDS, MEMBER_FIELDS, qualified_ths_catalog,
    qualified_ths_members, read_three_market_surfaces, CLASSIFICATION_CODES, _classification_checks,
)
from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _range, _rows, qualified_daily, qualified_trade_calendar,
)
from decision_kernel.application.market_security_inputs import (
    FACTOR_FIELDS, MASTER_FIELDS, qualified_adj_factors, qualified_security_master,
)
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_registered, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_failure import KNOWN_ERRORS
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
    'MARKET_RELAY_GROUP_DUPLICATE_CODE', 'MARKET_RELAY_GROUP_EXCHANGE_MISMATCH',
    'MARKET_RELAY_GROUP_TYPE_MISMATCH',
    'MARKET_RELAY_GROUP_MEMBER_IDENTITY', 'MARKET_RELAY_DAILY_ROW',
    'MARKET_RELAY_DAILY_CLOSE', 'MARKET_RELAY_FACTOR_ROW', 'MARKET_RELAY_FACTOR_VALUE',
    'MARKET_RELAY_END_NOT_MARKET_SESSION',
    'MARKET_RELAY_GROUP_NAME', 'MARKET_RELAY_GROUP_COUNT',
    'MARKET_RELAY_MASTER_NAME', 'MARKET_RELAY_MASTER_DUPLICATE_SECURITY',
    'MARKET_RELAY_GROUP_KIND', 'MARKET_RELAY_QUERY_SCOPE',
    'MARKET_RELAY_FIELDS_NOT_REQUESTED', 'MARKET_RELAY_CAPTURE_CONTEXT_CHANGED',
    'MARKET_RELAY_CAPTURE_INCOMPLETE', 'MARKET_RELAY_RESULT_INVALID',
    'RELAY_AUTH_OR_ENTITLEMENT', 'RELAY_RATE_LIMIT', 'RELAY_JSON_INVALID',
    'RELAY_HTTP_404', 'RELAY_HTTP_405', 'RELAY_HTTP_RESPONSE_INVALID',
    'SOURCE_COUNT_INVALID', 'SOURCE_CELL_INVALID', 'SOURCE_TEXT_TOO_LARGE',
})


def _code(value: object) -> str:
    return value if isinstance(value, str) and value in PUBLIC_CODES else 'CHECK_REJECTED'


SELECTIONS = ('sample', 'catalogues')
FAILURE_CLASSES = frozenset({
    'auth-or-entitlement', 'rate-limit', 'source-unavailable', 'request-rejected',
    'temporary-upstream', 'temporary-gateway', 'http-unclassified',
    'tls-certificate', 'tls', 'dns-temporary', 'dns', 'timeout',
    'incomplete-read', 'connection', 'http-protocol', 'transport-unclassified',
})


def _last_attempt_failure(capture: dict) -> dict | None:
    """Project the last failed attempt, which may precede a later successful retry.

    Reuse recorded transport classifications; never infer them from error prose
    or emit the original headers, request identity, body or retry diagnostics.
    """
    attempts = [a for page in capture.get('pages', []) for a in page.get('attempts', [])]
    attempts.extend(capture.get('attempts', []))
    for attempt in reversed(attempts):
        failure = attempt.get('failure') if isinstance(attempt, dict) else None
        if not isinstance(failure, dict):
            continue
        category, status = failure.get('classification'), failure.get('http_status')
        business = failure.get('business_error')
        return {
            'classification': category if isinstance(category, str) and category in FAILURE_CLASSES else 'unclassified',
            'http_status': status if type(status) is int and 100 <= status <= 599 else None,
            'business_error': business if isinstance(business, str) and business in KNOWN_ERRORS else None,
        }
    return None


def _catalogue_shape(view: dict, expected_type: str) -> dict:
    """Fixed structural counters only. Does not qualify, coerce or filter rows."""
    names = {type(None): 'null', bool: 'boolean', int: 'integer', float: 'number', str: 'string'}
    types = {field: {kind: 0 for kind in ('missing', 'null', 'boolean', 'integer', 'number', 'string', 'other')}
             for field in sorted(CATALOG_FIELDS)}
    out = {'field_types': types, 'padded_names': 0, 'nonprintable_names': 0,
           'integral_numeric_counts': 0, 'fractional_numeric_counts': 0}
    classification = {
        'coverage': 'complete-capture-not-qualified' if view['complete'] else 'received-pages-only',
        'rows_checked': 0, 'rows_with_issues': 0,
        'reason_counts': {'MARKET_RELAY_' + code: 0 for code in CLASSIFICATION_CODES},
        'duplicate_identical_rows': 0, 'duplicate_different_rows': 0,
        'missing_exchange_rows': 0, 'missing_type_rows': 0,
    }
    out['classification'] = classification
    seen = {}
    for row in _rows(view):
        checks = dict(_classification_checks(row, seen, expected_type))
        classification['rows_checked'] += 1
        classification['rows_with_issues'] += int(not all(checks.values()))
        for reason, passed in checks.items():
            classification['reason_counts']['MARKET_RELAY_' + reason] += int(not passed)
        classification['missing_exchange_rows'] += int(row.get('exchange') in (None, ''))
        classification['missing_type_rows'] += int(row.get('type') in (None, ''))
        if checks['GROUP_CODE']:
            code = row['ts_code']
            # Only the fixed requested columns; bytes stay in memory, never in a receipt.
            representation = encoded({field: row.get(field) for field in sorted(CATALOG_FIELDS)})
            if code in seen:
                key = 'duplicate_identical_rows' if representation == seen[code] else 'duplicate_different_rows'
                classification[key] += 1
            else:
                seen[code] = representation

        for field, counts in types.items():
            kind = names.get(type(row[field]), 'other') if field in row else 'missing'
            counts[kind] += 1
        name, count = row.get('name'), row.get('count')
        if isinstance(name, str):
            out['padded_names'] += int(name != name.strip())
            out['nonprintable_names'] += int(not name.isprintable())
        if type(count) is float and math.isfinite(count):
            out['integral_numeric_counts' if count.is_integer() else 'fractional_numeric_counts'] += 1
    return out


def check_scope(exchange: str, start: str, end: str, now: datetime) -> None:
    require(isinstance(exchange, str) and exchange in EXCHANGES, 'MARKET_CHECK_EXCHANGE')
    lo, hi = _range(start, end)
    require(isinstance(now, datetime) and now.utcoffset() is not None, 'MARKET_CHECK_CLOCK')
    today = now.astimezone(timezone(timedelta(hours=8))).date()
    require(date.fromisoformat(hi) < today, 'MARKET_CHECK_PAST_DAYS_ONLY')
    require((date.fromisoformat(hi) - date.fromisoformat(lo)).days < 120, 'MARKET_CHECK_RANGE')


def check_sources(*, registry: RelayRegistry, exchange: str, start: str, end: str,
                  access_confirmed: bool = False, selection: str = 'sample', client=None, now: datetime | None = None,
                  progress=None, sleep=time.sleep, monotonic=time.monotonic, clock=None) -> dict:
    """Check one current listing slice and one source-selected member of each channel.

    Access authorization is not publication authorization. Unlike capture_plan,
    this use case has no capture/export result or writer. Its only output is the
    allowlisted receipt below. Existing publication-gated paths remain unchanged.
    No fallback identity, host, API or source retry budget is created.
    """
    require(access_confirmed is True, 'MARKET_CHECK_ACCESS_REQUIRED')
    require(isinstance(registry, RelayRegistry), 'MARKET_CHECK_REGISTRY')
    require(isinstance(selection, str) and selection in SELECTIONS, 'MARKET_CHECK_SELECTION')
    selected_slots = SLOTS if selection == 'sample' else {k: SLOTS[k] for k in ('sector_catalog', 'concept_catalog')}
    now = datetime.now(timezone.utc) if now is None else now
    check_scope(exchange, start, end, now)
    report = {'schema': 'market-source-check-v1', 'status': 'in-progress', 'selection': selection,
        'scope': {'exchange': exchange, 'start': start, 'end': end, 'list_status': 'L'},
        'sample_policy': ('first-sorted-code-per-qualified-source-catalogue-not-candidates' if selection == 'sample'
                          else 'catalogue-qualification-only-no-sample-or-dated-membership'),
        'queries': {k: {'api': api, 'status': 'not-attempted', 'reason_code': 'DEPENDENCY_UNAVAILABLE',
                        'row_count': None, 'page_count': None, 'http_attempts': 0, 'last_attempt_failure': None}
                    for k, api in selected_slots.items()},
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
            item['last_attempt_failure'] = _last_attempt_failure(capture)
            result = {'case_id': 'market-source-check', 'use': use,
                      'status': 'captured' if capture['complete'] else 'capture-incomplete', 'capture': capture}
            view = inspect_result(result, registry)
            item.update(row_count=view['row_count'], page_count=view['page_count'])
            if api == 'ths_index':
                item['catalogue_shape'] = _catalogue_shape(view, params['type'])
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

    if selection == 'catalogues':
        # No calendar, master, price, factor, members or three-surface invocation.
        # Dates are operation context only: these directories have no dated scope.
        for kind in GROUP_TYPES:
            take(kind + '_catalog', {'exchange': 'A', 'type': GROUP_TYPES[kind]}, CATALOG_FIELDS,
                 lambda r, k=kind: qualified_ths_catalog(r, registry=registry, kind=k))
        report['status'] = ('checked-catalogues' if all(q['status'] == 'checked'
                            for q in report['queries'].values()) else 'completed-with-gaps')
        emit()
        return report

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
