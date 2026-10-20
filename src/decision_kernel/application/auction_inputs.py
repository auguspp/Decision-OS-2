"""Exact registered Relay inputs for the finite prior-pool auction profile.

No collection, retry, latest selection or storage. Full original qualification
and the U/D/Z pool precede navigation; incomplete inputs remain explicit gaps.
"""
from copy import deepcopy
import re

from decision_kernel.application.market_relay_inputs import _view, _rows, _lineage
from decision_kernel.application.market_session_inputs import session_quotes
from decision_kernel.application.research_sources import _declared_rows, _more_rows
from decision_kernel.domain.records import MAX_BYTES, RecordError, digest, encoded
from decision_kernel.observations.market_daily import MarketInputError, trade_day
from decision_kernel.observations.opening_auction import (
    PROFILE, CODE, GROUPS, STATIC_UNKNOWN, calendar_start, previous_session, prior_pool, apply_auction,
    same_session, timeliness, close_timeliness, group, require,
)

AUCTION_READING_SCHEMA = 'relay-auction-reading-v1'
AUCTION_VIEW_SCHEMA = 'relay-auction-context-v1'
AUCTION_REQUIRED = {'profile', 'market_session', 'calendar_result'}
AUCTION_OPTIONAL = {'pool_result', 'auction_result', 'close_result'}
POOL_FIELDS = {'ts_code', 'trade_date', 'limit'}
AUCTION_FIELDS = {'ts_code', 'trade_date'}
MAX_ROWS = 10000


def _coverage(view):
    """Generic short-tail completion is distinct from retained source evidence."""
    pages = view['pages']
    known = all('coverage_evidence' in page['table'] for page in pages)
    if not known:
        state = 'unknown'
    else:
        count = sum(len(page['table']['items']) for page in pages)
        evidence = [page['table']['coverage_evidence'] for page in pages]
        incomplete = (_more_rows(pages[-1]['table']) or any(_declared_rows(page['table']) > count for page in pages)
                      or any(e.get(name, {}).get('truncated', False) for e in evidence for name in ('body', 'data')))
        state = 'incomplete' if incomplete else 'returned-query-complete'
    return {'status': state, 'generic_capture_complete': view['complete'],
            'received_rows': view['row_count'], 'received_pages': view['page_count'],
            'page_limit': int(view['parameters']['limit']),
            'page_evidence': [{'offset': p['offset'], 'received_rows': len(p['table']['items']),
                              'evidence': deepcopy(p['table'].get('coverage_evidence'))} for p in pages],
            'independent_market_exhaustiveness': 'not-established', 'unreturned_count': None}


def _located_rows(view):
    rows = []
    for page in view['pages']:
        table = page['table']
        for index, values in enumerate(table['items']):
            rows.append({'source_row_index': len(rows), 'page_offset': page['offset'],
                         'page_row_index': index, 'values': dict(zip(table['fields'], values))})
    return rows


def _reason(error):
    value = str(error)
    return value if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', value) else 'AUCTION_INPUT_INVALID'


def read_auction_context(*, profile, market_session, calendar_result, registry,
                         pool_result=None, auction_result=None, close_result=None,
                         offset=0, limit=50, entity_id=None):
    require(profile == PROFILE, 'PROFILE_INVALID')
    target = trade_day(market_session)
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, 'PAGE_INVALID')
    require(entity_id is None or isinstance(entity_id, str) and CODE.fullmatch(entity_id), 'ENTITY_INVALID')
    require(len(encoded([calendar_result, pool_result, auction_result, close_result])) <= MAX_BYTES,
            'INPUT_BOUND')
    day, start = target.strftime('%Y%m%d'), calendar_start(target).strftime('%Y%m%d')
    queries, lineage, coverage, clocks = {}, {}, {}, {}

    def take(slot, supplied, api, scope, fields):
        if supplied is None:
            queries[slot] = {'status': 'not-supplied', 'reason_code': None}
            return None
        try:
            view = _view(supplied, registry, api, scope, fields)
            require(view['row_count'] <= MAX_ROWS, 'ROW_BOUND')
        except (RecordError, MarketInputError) as error:
            queries[slot] = {'status': 'rejected', 'reason_code': _reason(error)}
            return None
        queries[slot] = {'status': 'checked', 'reason_code': None}
        lineage[slot] = _lineage(view)
        if api in ('limit_list_d', 'stk_auction'):
            coverage[slot] = _coverage(view)
            clocks[slot] = [deepcopy(p['transport'].get('request_clock')) for p in view['pages']]
        return view

    calendar = take('calendar', calendar_result, 'trade_cal',
                    {'exchange': 'SSE', 'start_date': start, 'end_date': day}, {'exchange', 'cal_date', 'is_open'})
    session = None
    if calendar is not None:
        try:
            session = previous_session(list(_rows(calendar)), market_session)
        except MarketInputError as error:
            queries['calendar'] = {'status': 'rejected', 'reason_code': _reason(error)}
    status = 'CALENDAR_UNAVAILABLE' if session is None else (
        'TARGET_IS_CLOSED_SESSION' if not session['open'] else 'PRIOR_POOL_UNAVAILABLE')
    entries, pool_rows, temperature, auction_scope, static_count = [], None, None, None, None
    cohort, close, close_clock = [], None, 'NOT_ESTABLISHED'
    groups = {key: {'denominator': None, 'close_readable': None, 'close_available': None, 'comparable': None,
                                'above_auction': None, 'below_auction': None, 'equal_to_auction': None,
                                'median_change': None} for key in GROUPS}
    prior = session['previous_session'] if session else None
    if session is not None and session['open']:
        pool = take('pool', pool_result, 'limit_list_d', {'trade_date': prior.replace('-', '')}, POOL_FIELDS)
        if pool is not None:
            pool_rows = _located_rows(pool)
            entries, temperature = prior_pool([row['values'] for row in pool_rows], prior)
            static_count = sum(e['status'] == 'STATIC_ELIGIBLE' for e in entries)
            auction = take('auction', auction_result, 'stk_auction', {'trade_date': day, 'ts_type': 'STK'}, AUCTION_FIELDS)
            entries, auction_scope = apply_auction(entries, list(_rows(auction)) if auction is not None else None, market_session)
            status = ('NO_STATIC_MATCH_IN_RETURNED_POOL' if not static_count else
                      'SHADOW_AUCTION_READING' if auction is not None else 'AUCTION_INPUT_UNAVAILABLE')
            # Freeze membership and original groups before the optional later
            # close join, so follow-on sessions can reuse this exact cohort.
            cohort = [{**deepcopy(row), 'group': group(row)} for row in entries]
            if close_result is not None:
                try:
                    # This existing qualifier checks whole source context, then
                    # isolates each exact security's raw-close defects.
                    close = session_quotes(close_result, registry, 'daily', day)
                except (RecordError, MarketInputError) as error:
                    queries['close'] = {'status': 'rejected', 'reason_code': _reason(error)}
                else:
                    queries['close'] = {'status': 'checked', 'reason_code': None}
                    lineage['close'] = close['lineage']
                    clocks['close'] = [deepcopy(p['transport'].get('request_clock'))
                                       for p in close['lineage']['pages']]
                    close_clock = close_timeliness(clocks['close'], market_session)
            else:
                queries['close'] = {'status': 'not-supplied', 'reason_code': None}
            entries, groups = same_session(entries, close['entries'] if close else None, close_clock)
    for name, supplied in (('pool', pool_result), ('auction', auction_result), ('close', close_result)):
        if name not in queries:
            queries[name] = {'status': 'not-supplied' if supplied is None else 'dependency-unavailable',
                             'reason_code': None}
    known = pool_rows is not None
    comparable = sum(e['gap_pct'] is not None for e in entries) if known else None
    matched = sum(e['matched_profile'] for e in entries) if known else None
    gaps = (not known or coverage.get('pool', {}).get('status') != 'returned-query-complete'
            or bool(temperature and (temperature['invalid_row_indices'] or temperature['ambiguous_entities']
                                     or temperature['unknown_type_row_indices']))
            or any(e['status'] in STATIC_UNKNOWN for e in entries)
            or known and static_count > 0 and (comparable < static_count
                or coverage.get('auction', {}).get('status') != 'returned-query-complete'
                or bool(auction_scope['invalid_row_indices'])))
    result = {'schema': AUCTION_VIEW_SCHEMA, 'profile': PROFILE, 'market_session': market_session,
        'previous_session': prior, 'status': status, 'cohort_denominator': len(entries) if known else None,
        'static_eligible': static_count, 'auction_comparable': comparable, 'matched': matched,
        'prior_temperature': temperature, 'pool_rows': pool_rows, 'auction_scope': auction_scope,
        'cohort_rows': entries, 'groups': groups, 'coverage': coverage, 'coverage_gaps': bool(gaps),
        'same_session': {'status': ('SAVED_SAME_SESSION_OUTCOMES'
                if close and close_clock == 'RECEIVED_AFTER_SESSION_COMPLETION' else
                'CLOSE_COMPLETION_NOT_ESTABLISHED' if close else 'SAME_SESSION_CLOSE_NOT_RETAINED'),
            'completion_clock': close_clock, 'completed_session_cutoff': market_session + 'T15:30:00+08:00',
            'close_readable': sum(e['readable_close'] is not None for e in entries) if known else None,
            'close_available': sum(e['same_session_close'] is not None for e in entries) if known else None,
            'comparable': sum(e['auction_to_close_change'] is not None for e in entries) if known else None,
            'formula': 'same_session_close / auction_price - 1',
            'price_basis': 'SAME_RELAY_SAME_SECURITY_SAME_SESSION_RAW_PRICE_RATIO_NOT_STRATEGY_RETURN',
            'fraction_rounding_decimal_places': 12},
        'timeliness': timeliness(clocks.get('auction'), market_session), 'acquisition_clocks': clocks,
        'queries': queries, 'source_lineage': lineage, 'source_requests': 0,
        'units': {'auction_price': 'CNY', 'auction_pre_close': 'CNY', 'readable_close': 'CNY', 'same_session_close': 'CNY',
                  'prior_turnover_pct': 'percent', 'gap_pct': 'percent', 'auction_to_close_change': 'fraction'},
        'limitations': ['REGISTERED_PROFILE_NOT_WEAK_TO_STRONG_PROOF_OR_TRADE',
            'PREVIOUS_SOURCE_NAME_NOT_CURRENT_ST_LISTING_OR_TRADABILITY_CERTIFICATION',
            'U_COHORT_NOT_ALL_UDZ_POOL; UNKNOWN_UNRETURNED_COUNT_NOT_ZERO',
            'SOURCE_CAPTURE_CLOCK_NOT_EXCHANGE_TIMESTAMP_OR_HUMAN_RECEIPT',
            'DAILY_RAW_VALUE_REQUIRES_ORIGINAL_REQUEST_AND_RECEIPT_AFTER_1530_FOR_COMPLETED_SESSION_USE',
            '0940_INTRADAY_PATH_NEXT_SESSION_AND_TRADING_EFFECT_NOT_ESTABLISHED'],
        'human_attention_authority': 'NONE', 'research_authority': 'NONE', 'investment_authority': 'NONE'}
    # The source-only cohort identity excludes the optional later close values.
    result['cohort_version'] = digest(encoded({'profile': PROFILE, 'market_session': market_session,
        'previous_session': prior, 'cohort_denominator': result['cohort_denominator'],
        'static_eligible': static_count, 'auction_comparable': comparable, 'matched': matched,
        'lineage': {k: v for k, v in lineage.items() if k != 'close'},
        'rows': cohort}))
    result['view_version'] = digest(encoded(result))
    selected = [e for e in entries if entity_id is None or e['entity_id'] == entity_id]
    result['entries'] = deepcopy(selected[offset:offset + limit])
    result['pagination'] = {'offset': offset, 'limit': limit, 'entity_id': entity_id,
        'matched_count': len(selected), 'returned': len(result['entries']),
        'next_offset': offset + limit if offset + limit < len(selected) else None}
    return result
