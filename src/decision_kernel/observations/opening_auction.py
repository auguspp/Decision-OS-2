"""Fixed prior-pool auction observations and an exact same-session left join.

Pure values only. Source admission, custody, transport and clocks have separate
owners. The registered profile is not a trade, ranking or market completeness.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import re
from statistics import median

from decision_kernel.observations.market_daily import MarketInputError, trade_day

PROFILE = 'registered-317-prior-turnover10-30-auction-gap3-9-v1'
CODE = re.compile(r'[0-9]{6}\.(SH|SZ|BJ)\Z')
MAINBOARD = re.compile(r'(?:60[0135][0-9]{3}\.SH|00[0123][0-9]{3}\.SZ)\Z')
GROUPS = ('MATCHED', 'NOT_MATCHED', 'AUCTION_UNRESOLVED', 'STATIC_EXCLUDED', 'STATIC_UNRESOLVED')
STATIC_UNKNOWN = {'PRIOR_SESSION_MISMATCH', 'PRIOR_IDENTITY_CONFLICT', 'PRIOR_NAME_UNAVAILABLE',
                  'PRIOR_TURNOVER_UNAVAILABLE', 'PRIOR_SECURITY_IDENTITY_UNAVAILABLE'}


def require(ok, code):
    if not ok:
        raise MarketInputError('AUCTION_' + code)


def number(value):
    require(type(value) in (str, int, float, Decimal) and len(str(value)) <= 128, 'NUMBER_TYPE')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise MarketInputError('AUCTION_NUMBER_INVALID') from None
    require(result.is_finite() and len(result.as_tuple().digits) <= 28
            and abs(result.as_tuple().exponent) <= 12, 'NUMBER_RANGE')
    return result


def text(value):
    value = format(value, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def previous_session(rows, target):
    """Only the explicit prior-to-target interval must be gap-free."""
    target = trade_day(target)
    start, values = target - timedelta(days=31), {}
    for row in rows:
        raw = row.get('cal_date')
        require(isinstance(raw, str) and re.fullmatch(r'[0-9]{8}', raw), 'CALENDAR_IDENTITY')
        try:
            day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        except ValueError:
            raise MarketInputError('AUCTION_CALENDAR_IDENTITY') from None
        require(row.get('exchange') == 'SSE' and start <= day <= target and day not in values
                and type(row.get('is_open')) is int and row['is_open'] in (0, 1), 'CALENDAR_IDENTITY')
        values[day] = row['is_open']
    require(target in values, 'TARGET_CALENDAR_UNAVAILABLE')
    prior = max((d for d, open_ in values.items() if open_ and d < target), default=None)
    if not values[target]:
        return {'open': False, 'previous_session': prior.isoformat() if prior else None}
    require(prior is not None, 'PREVIOUS_SESSION_UNAVAILABLE')
    require(all(prior + timedelta(days=n) in values for n in range((target - prior).days + 1)),
            'PREVIOUS_TO_TARGET_CALENDAR_GAP')
    return {'open': True, 'previous_session': prior.isoformat()}


def _identity(value):
    return isinstance(value, str) and CODE.fullmatch(value) is not None


def _normalized_turnover(value):
    try:
        return ('number', number(value))
    except MarketInputError:
        return ('invalid', repr(value))


def prior_pool(source, prior):
    """Retain every returned U/D/Z row separately from the unique U cohort."""
    day = trade_day(prior).strftime('%Y%m%d')
    groups, invalid, unidentified = defaultdict(list), [], []
    for index, row in enumerate(source):
        code = row.get('ts_code')
        valid = _identity(code) and row.get('trade_date') == day
        if not valid:
            invalid.append(index)
        if valid or _identity(code) and row.get('limit') == 'U':
            groups[code].append((index, row))
        elif row.get('limit') == 'U':
            unidentified.append(index)
    entries, counts, ambiguous, ladder, missing_streaks = [], Counter(), [], [], []
    for code, located in sorted(groups.items()):
        values = [row for _, row in located]
        types = {r.get('limit') if isinstance(r.get('limit'), str) else '<INVALID>' for r in values}
        wrong_date = any(r.get('trade_date') != day for r in values)
        if wrong_date or len(types) != 1 or not types <= {'U', 'D', 'Z'}:
            ambiguous.append(code)
        else:
            counts[next(iter(types))] += 1
        if 'U' not in types:
            continue
        row, name = values[0], values[0].get('name')
        valid_name = isinstance(name, str) and 0 < len(name.strip()) <= 80 and not any(ord(c) < 32 for c in name)
        entry = {'entity_id': code, 'cohort_id': code, 'name': name if valid_name else None,
                 'prior_turnover_pct': None, 'status': 'STATIC_ELIGIBLE',
                 'auction_price': None, 'auction_pre_close': None, 'gap_pct': None,
                 'matched_profile': False, 'prior_rows': len(values),
                 'prior_row_indices': [index for index, _ in located]}
        consumed = {(repr(r.get('name')), _normalized_turnover(r.get('turnover_ratio')), repr(r.get('limit')))
                    for r in values}
        if wrong_date:
            entry['status'] = 'PRIOR_SESSION_MISMATCH'
        elif len(consumed) != 1:
            entry['status'] = 'PRIOR_IDENTITY_CONFLICT'
        elif not MAINBOARD.fullmatch(code):
            entry['status'] = 'EXCLUDED_BOARD'
        elif not valid_name:
            entry['status'] = 'PRIOR_NAME_UNAVAILABLE'
        elif 'ST' in name.upper():
            entry['status'] = 'EXCLUDED_PRIOR_ST_NAME'
        else:
            try:
                turnover = number(row.get('turnover_ratio'))
                require(turnover >= 0, 'NEGATIVE_TURNOVER')
                entry['prior_turnover_pct'] = text(turnover)
                if not Decimal(10) <= turnover <= Decimal(30):
                    entry['status'] = 'OUTSIDE_PRIOR_TURNOVER_PROFILE'
            except MarketInputError:
                entry['status'] = 'PRIOR_TURNOVER_UNAVAILABLE'
        entries.append(entry)
        if wrong_date or types != {'U'}:
            continue
        try:
            streaks = [number(r.get('limit_times')) for r in values]
            require(all(n >= 1 and n == n.to_integral_value() for n in streaks)
                    and len(set(streaks)) == 1, 'STREAK_UNAVAILABLE_OR_CONFLICT')
            ladder.append({'entity_id': code, 'consecutive_limits': int(streaks[0])})
        except MarketInputError:
            missing_streaks.append(code)
    for index in unidentified:
        entries.append({'entity_id': None, 'cohort_id': 'unidentified-row:' + str(index), 'name': None,
                        'prior_turnover_pct': None, 'status': 'PRIOR_SECURITY_IDENTITY_UNAVAILABLE',
                        'auction_price': None, 'auction_pre_close': None, 'gap_pct': None,
                        'matched_profile': False, 'prior_rows': 1, 'prior_row_indices': [index]})
    return entries, {'market_session': prior, 'returned_pool_rows': len(source),
        'unique_recognized': {kind: counts[kind] for kind in ('U', 'D', 'Z')},
        'returned_type_rows': {kind: sum(r.get('limit') == kind for r in source) for kind in ('U', 'D', 'Z')},
        'unknown_type_row_indices': [i for i, r in enumerate(source) if r.get('limit') not in ('U', 'D', 'Z')],
        'ambiguous_entities': ambiguous, 'invalid_row_indices': invalid,
        'unidentified_u_row_indices': unidentified,
        'maximum_consecutive': max((r['consecutive_limits'] for r in ladder), default=None),
        'ladder_at_least_two': [r for r in ladder if r['consecutive_limits'] >= 2],
        'missing_streak_entities': missing_streaks,
        'scope': 'RETURNED_PRIOR_POOL_EXCLUDES_ST_NOT_INDEPENDENT_MARKET_EXHAUSTIVENESS'}


def apply_auction(entries, source, target):
    """A separate value pair for each eligible security; no daily-close substitute."""
    rows, groups, invalid = deepcopy(entries), defaultdict(list), []
    day = trade_day(target).strftime('%Y%m%d')
    for index, row in enumerate(source or []):
        if not _identity(row.get('ts_code')) or row.get('trade_date') != day:
            invalid.append(index)
        else:
            groups[row['ts_code']].append(row)
    for row in rows:
        if row['status'] != 'STATIC_ELIGIBLE':
            continue
        values = groups.get(row['entity_id'], [])
        if source is None:
            row['status'] = 'AUCTION_INPUT_UNAVAILABLE'
        elif not values:
            row['status'] = 'AUCTION_ROW_UNAVAILABLE'
        else:
            try:
                pairs = {(number(v.get('price')), number(v.get('pre_close'))) for v in values}
                require(len(pairs) == 1, 'PRICE_CONFLICT')
                price, reference = pairs.pop()
                require(price > 0 and reference > 0, 'PRICE_POSITIVE')
                with localcontext() as ctx:
                    ctx.prec = 64
                    matched = reference * 103 <= price * 100 <= reference * 109
                    ctx.prec = 28
                    gap = (price / reference - 1) * 100
                row.update(auction_price=text(price), auction_pre_close=text(reference), gap_pct=text(gap),
                           matched_profile=matched,
                           status='SHADOW_PROFILE_MATCH' if matched else 'OUTSIDE_AUCTION_GAP_PROFILE')
            except MarketInputError:
                row['status'] = 'AUCTION_VALUE_UNAVAILABLE_OR_CONFLICT'
    return rows, {'returned_rows': len(source) if source is not None else None,
                  'invalid_row_indices': invalid,
                  'price_semantics': 'STK_AUCTION_MATCHED_PRICE_OVER_SAME_ROW_PRE_CLOSE'}


def group(row):
    if row['status'] == 'SHADOW_PROFILE_MATCH':
        return 'MATCHED'
    if row['status'] == 'OUTSIDE_AUCTION_GAP_PROFILE':
        return 'NOT_MATCHED'
    if row['status'] in ('AUCTION_INPUT_UNAVAILABLE', 'AUCTION_ROW_UNAVAILABLE', 'AUCTION_VALUE_UNAVAILABLE_OR_CONFLICT'):
        return 'AUCTION_UNRESOLVED'
    if row['status'] in ('EXCLUDED_BOARD', 'EXCLUDED_PRIOR_ST_NAME', 'OUTSIDE_PRIOR_TURNOVER_PROFILE'):
        return 'STATIC_EXCLUDED'
    return 'STATIC_UNRESOLVED'


def same_session(entries, close_entries, close_clock):
    """All five original groups survive, including nonmatches and unresolved rows."""
    rows, grouped = deepcopy(entries), {key: [] for key in GROUPS}
    for row in rows:
        quote = (close_entries or {}).get(row['entity_id'])
        readable, close, change = None, None, None
        state = 'SAME_SESSION_CLOSE_NOT_RETAINED' if close_entries is None else 'CLOSE_IDENTITY_NOT_RETURNED'
        if quote is not None:
            if quote['status'] != 'observed':
                state = 'CLOSE_VALUE_UNAVAILABLE_OR_CONFLICT'
            else:
                readable = quote['value']
                state = ('CLOSE_COMPLETION_TIME_NOT_ESTABLISHED' if close_clock == 'NOT_ESTABLISHED'
                         else 'CLOSE_REQUESTED_BEFORE_SESSION_COMPLETION')
                if close_clock == 'RECEIVED_AFTER_SESSION_COMPLETION':
                    close = readable
                    state = 'PROFILE_AUCTION_PRICE_UNAVAILABLE'
                if close is not None and row['auction_price'] is not None:
                    with localcontext() as ctx:
                        ctx.prec = 40
                        change = format(Decimal(str(close)) / Decimal(row['auction_price']) - 1, '.12f')
                    state = 'SAME_SESSION_COMPARABLE'
        row.update(readable_close=readable, same_session_close=close,
                   auction_to_close_change=change, outcome_status=state, group=group(row))
        grouped[row['group']].append(row)
    stats = {}
    for key, values in grouped.items():
        comparable = [r for r in values if r['auction_to_close_change'] is not None]
        changes = [Decimal(r['auction_to_close_change']) for r in comparable]
        with localcontext() as ctx:
            ctx.prec = 40
            midpoint = format(median(changes), '.12f') if changes else None
        stats[key] = {'denominator': len(values), 'close_readable': sum(r['readable_close'] is not None for r in values),
            'close_available': sum(r['same_session_close'] is not None for r in values),
            'comparable': len(comparable),
            'above_auction': sum(Decimal(str(r['same_session_close'])) > Decimal(r['auction_price']) for r in comparable),
            'below_auction': sum(Decimal(str(r['same_session_close'])) < Decimal(r['auction_price']) for r in comparable),
            'equal_to_auction': sum(Decimal(str(r['same_session_close'])) == Decimal(r['auction_price']) for r in comparable),
            'median_change': midpoint}
    return rows, stats


def close_timeliness(clocks, target):
    """Retain the old completed-day cutoff; never substitute capture/read time."""
    if not clocks or any(clock is None for clock in clocks):
        return 'NOT_ESTABLISHED'
    zone = timezone(timedelta(hours=8))
    cutoff = datetime.combine(trade_day(target), time(15, 30), tzinfo=zone)
    for clock in clocks:
        asked, received = [datetime.fromisoformat(clock[k].replace('Z', '+00:00'))
                           for k in ('requested_at', 'received_at')]
        if not cutoff <= asked <= received:
            return 'REQUESTED_BEFORE_SESSION_COMPLETION'
    return 'RECEIVED_AFTER_SESSION_COMPLETION'


def timeliness(clocks, target):
    """Acquisition clocks only; a Room/read clock cannot establish preopen receipt."""
    if not clocks or any(clock is None for clock in clocks):
        return 'NOT_ESTABLISHED'
    target, zone = trade_day(target), timezone(timedelta(hours=8))
    for clock in clocks:
        asked, received = [datetime.fromisoformat(clock[k].replace('Z', '+00:00')).astimezone(zone)
                           for k in ('requested_at', 'received_at')]
        if not (asked.date() == received.date() == target and time(9, 26) <= asked.time() <= received.time() < time(9, 30)):
            return 'HISTORICAL_OR_LATE_NOT_PREOPEN_DISCOVERY'
    return 'RECEIVED_BEFORE_CONTINUOUS_OPEN'
