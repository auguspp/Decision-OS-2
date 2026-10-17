"""One SH/SZ session: complete source slices, original qualifiers, no source I/O.

A missing daily row is not a zero return or proof of suspension. Current L
masters are retrieval snapshots, not a historical or exchange-certified universe.
"""
from collections import defaultdict
import re

from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, MarketRelayError, _day, _range, _require, _view, _rows, _lineage,
    _daily, qualified_trade_calendar,
)
from decision_kernel.application.market_security_inputs import (
    FACTOR_FIELDS, _factors, _within_listing,
    qualified_security_master, inspect_security_master,
)
from decision_kernel.domain.records import MAX_BYTES, digest, encoded
from decision_kernel.observations.market_daily import MarketInputError

SESSION_SCHEMA = 'relay-ashare-session-reading-v1'
SESSION_REQUIRED = {'calendar_results', 'master_results', 'session_results', 'start', 'end', 'calendar_end'}
QUOTE_FIELDS = {'daily': {'ts_code', 'trade_date', 'close'}, 'adj_factor': FACTOR_FIELDS}
NO_SUSPENSION = object()
VALUE_STATES = ('observed', 'missing', 'quarantined', 'input-unavailable')


def _reason(exc):
    text = str(exc)
    return text if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', text) else 'MARKET_SESSION_INPUT_REJECTED'


def session_quotes(result, registry, api, day):
    """Validate the original whole capture, then isolate identifiable bad groups.

    Group tables are in-memory inputs to the original pure row qualifiers, NOT
    invented registered captures or substituted SourceRefs. Whole-capture lineage
    is retained. Every duplicate occurrence belongs to the quarantined group.
    """
    _require(api in QUOTE_FIELDS, 'SESSION_API')
    iso = _day(day)
    view = _view(result, registry, api, {'trade_date': day}, QUOTE_FIELDS[api])
    _require(view['row_count'] <= 10000, 'SESSION_ROW_BOUND')
    groups = defaultdict(list)
    for row in _rows(view):
        code = row.get('ts_code')
        _require(isinstance(code, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code), 'SESSION_IDENTITY')
        _require(_day(row.get('trade_date')) == iso, 'SESSION_DATE')
        groups[code].append(row)
    entries = {}
    field = 'close' if api == 'daily' else 'adj_factor'
    qualifier = _daily if api == 'daily' else _factors
    for code, rows in sorted(groups.items()):
        fields = ['ts_code', 'trade_date', field]
        table = {'pages': [{'table': {'fields': fields, 'items': [[r[f] for f in fields] for r in rows]}}]}
        try:
            value = qualifier(table, code, {iso})[0][field]
        except MarketInputError as exc:
            entry = {'status': 'quarantined', 'value': None, 'reason_code': _reason(exc)}
        else:
            entry = {'status': 'observed', 'value': value, 'reason_code': None}
        entries[code] = {**entry, 'source_row_count': len(rows)}
    return {'entries': entries, 'lineage': _lineage(view), 'row_count': view['row_count'],
            'quarantined_count': sum(e['status'] == 'quarantined' for e in entries.values()),
            'outside_sh_sz_count': sum(c.endswith('.BJ') for c in entries)}


def _value(data, code):
    if data is None:
        return {'status': 'input-unavailable', 'value': None, 'reason_code': None, 'source_row_count': None}
    return data['entries'].get(code, {'status': 'missing', 'value': None, 'reason_code': None, 'source_row_count': 0})


def read_session_coverage(*, calendar_results, master_results, session_results,
                          registry, start, end, calendar_end, reference_policy='strict',
                          offset=0, limit=50, entity_id=None, suspension_result=NO_SUSPENSION):
    """Read all declared and received SH/SZ identities; paginate only the view.

    Calendar lag is relative to the supplied full calendar's explicit end, not
    wall-clock latest certification. Missing shared inputs produce unknown counts.
    No 5/20/60 history, adjustment arithmetic, halt or economic claim is made.
    """
    lo, day = _range(start, end)
    _, through = _range(end, calendar_end)
    from datetime import date
    _require((date.fromisoformat(through) - date.fromisoformat(lo)).days < 120, 'SESSION_RANGE')
    _require(reference_policy in ('strict', 'isolate-identified'), 'REFERENCE_POLICY')
    for value, allowed in ((calendar_results, set(EXCHANGES)), (master_results, set(EXCHANGES)),
                           (session_results, set(QUOTE_FIELDS))):
        _require(isinstance(value, dict) and value.keys() <= allowed, 'SESSION_RESULT_MAP')
    _require(len(encoded([calendar_results, master_results, session_results,
                          None if suspension_result is NO_SUSPENSION else suspension_result])) <= MAX_BYTES, 'SESSION_INPUT_BOUND')
    _require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, 'PAGE_INVALID')
    _require(entity_id is None or isinstance(entity_id, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ)', entity_id), 'SECURITY_INVALID')
    sources, queries, calendars, masters, quotes = {}, {}, {}, {}, {}
    def take(slot, supplied, action):
        if supplied is None:
            queries[slot] = {'status': 'not-supplied', 'reason_code': None}
            return None
        try:
            value = action()
        except (MarketInputError, ValueError) as exc:
            queries[slot] = {'status': 'rejected', 'reason_code': _reason(exc)}
            return None
        queries[slot] = {'status': 'checked', 'reason_code': None}
        return value
    for venue in EXCHANGES:
        result = calendar_results.get(venue)
        calendars[venue] = take(venue + '_calendar', result, lambda: qualified_trade_calendar(
            result, registry=registry, exchange=venue, start=start, end=calendar_end))
        if calendars[venue] is not None:
            sources[venue + '_calendar'] = _lineage(_view(result, registry, 'trade_cal',
                {'exchange': venue, 'start_date': start, 'end_date': calendar_end}, {'exchange','cal_date','is_open'}))
        result = master_results.get(venue)
        def master():
            reader = inspect_security_master if reference_policy == 'isolate-identified' else qualified_security_master
            value = reader({'L': result}, registry=registry, exchange=venue)
            _require(value['status_counts']['L'] <= 10000, 'SESSION_ROW_BOUND')
            return value
        masters[venue] = take(venue + '_master', result, master)
        if masters[venue] is not None:
            sources[venue + '_master'] = masters[venue]['lineage']
    for api in QUOTE_FIELDS:
        result = session_results.get(api)
        quotes[api] = take(api, result, lambda: session_quotes(result, registry, api, end))
        if quotes[api] is not None:
            sources[api] = quotes[api]['lineage']
    by_venue, entries = {}, []
    for venue, suffix in EXCHANGES.items():
        master, calendar = masters[venue], calendars[venue]
        refs = master['securities'] if master is not None else {}
        received = {c for q in quotes.values() if q is not None for c in q['entries'] if c.endswith('.' + suffix)}
        declared = set(refs)
        calendar_ok = calendar is not None and day in calendar
        eligible = {c for c, r in refs.items() if r is not None and _within_listing(day, r)}
        raw_counts = {s: 0 for s in VALUE_STATES} if master is not None and calendar_ok else None
        factor_counts = dict(raw_counts) if raw_counts is not None else None
        matched = 0 if raw_counts is not None else None
        for code in sorted(declared | received):
            reference = refs.get(code)
            state = ('reference-unavailable' if master is None else 'unreferenced' if code not in refs
                     else 'reference-quarantined' if reference is None else 'outside-listing' if code not in eligible
                     else 'calendar-unavailable' if not calendar_ok else 'eligible')
            raw, factor = _value(quotes['daily'], code), _value(quotes['adj_factor'], code)
            if state == 'eligible':
                raw_counts[raw['status']] += 1
                factor_counts[factor['status']] += 1
                matched += int(raw['status'] == factor['status'] == 'observed')
            entries.append({'entity_id': code, 'exchange': venue, 'availability': state,
                'reference_qualification': master.get('qualification', {}).get(code) if master is not None else None,
                'raw': raw if state == 'eligible' else None, 'factor': factor if state == 'eligible' else None})
        quarantined = sum(r is None for r in refs.values()) if master is not None else None
        lag = sum(d > day for d in calendar) if calendar_ok else None
        by_venue[venue] = {'scope_count': len(refs) if master is not None else None,
            'eligible_count': len(eligible) if raw_counts is not None else None,
            'reference_quarantined_count': quarantined,
            'outside_listing_count': sum(r is not None and c not in eligible for c, r in refs.items()) if master is not None else None,
            'unreferenced_count': len(received - declared) if master is not None else None,
            'calendar_status': 'checked-open' if calendar_ok else 'closed' if calendar is not None else 'unavailable',
            'session_lag': lag, 'raw_counts': raw_counts, 'factor_counts': factor_counts, 'matched_count': matched,
            'qualified_scope_covered': bool(master is not None and calendar_ok and eligible and lag == 0
                and matched == len(eligible) and not received - declared)}
    rows = sorted(entries, key=lambda e: e['entity_id'])
    known_scope = all(m is not None for m in masters.values())
    scope = sum(m['scope_count'] for m in masters.values()) if known_scope else None
    result = {'schema': 'ashare-session-context-v1', 'as_of': day, 'calendar_through': through,
        'reference_policy': reference_policy, 'scope_count': scope, 'by_exchange': by_venue,
        'qualified_scope_covered': all(v['qualified_scope_covered'] for v in by_venue.values()),
        'strict_reference_passed': all(m is not None and not any(r is None for r in m['securities'].values()) for m in masters.values()),
        'queries': queries, 'source_lineage': sources,
        'outside_sh_sz': {api: q['outside_sh_sz_count'] if q is not None else None for api, q in quotes.items()},
        'source_requests': 0, 'units': {'raw': 'CNY', 'factor': 'dimensionless-factor-not-adjusted-price'},
        'limitations': ['CURRENT_L_SNAPSHOTS_NOT_ATOMIC_OR_HISTORICAL_UNIVERSE',
            'MISSING_DAILY_IS_NOT_ZERO_OR_CONFIRMED_SUSPENSION', 'SINGLE_SESSION_NOT_5_20_60_HISTORY',
            'CALENDAR_LAG_ONLY_THROUGH_DECLARED_END; NO_LATEST_OR_LONG_TERM_HEALTH_CERTIFICATION',
            'BJ_OUTSIDE_SUPPORTED_SCOPE; UNREFERENCED_SH_SZ_RETAINED_NOT_QUALIFIED']}
    if suspension_result is not NO_SUSPENSION:
        from decision_kernel.application.market_suspension_inputs import reconcile_suspensions
        reconcile_suspensions(result, rows, suspension_result, registry=registry, day=end)
    result['view_version'] = digest(encoded({'context': result, 'entries': rows}))
    selected = rows if entity_id is None else [e for e in rows if e['entity_id'] == entity_id]
    result.update(entries=selected[offset:offset + limit], page={'offset': offset, 'limit': limit,
        'selected_count': len(selected), 'entries_count': len(rows),
        'next_offset': offset + limit if offset + limit < len(selected) else None})
    return result
