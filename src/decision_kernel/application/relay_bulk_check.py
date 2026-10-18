"""Fixed, one-shot daily row-limit experiment; not a production capture adapter.

The original transport owns HTTP, secret protection and byte bounds. The original
source table qualifier owns the response envelope; this module measures rows and
partitions without increasing any normal source-plan or market-custody limit.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from hashlib import sha256
import json
import re

from decision_kernel.application.research_sources import _table
from decision_kernel.domain.records import require
from decision_kernel.observations.market_daily import _price

STUDY = 'relay-bulk-20261018'
FIELDS = 'ts_code,trade_date,close'
MAX_CALLS = 4
MAX_BODY = 4 * 1024 * 1024
DAYS = ('20261015', '20261016')

# A separately authorized OHLCV test; the original one-shot study is immutable.
FULL_STUDY = 'relay-ohlcv-bulk-retest-20261018'
FULL_FIELDS = 'ts_code,trade_date,open,high,low,close,vol,amount'
FULL_MAX_CALLS = 5


def full_plan():
    """Fresh bounded study: small control, normal page, full day, then two days."""
    common = {'fields': FULL_FIELDS, 'offset': '0'}
    return [
        {'id': 'ohlcv-capability', 'kind': 'capability', 'api': 'daily', 'parameters': {}},
        {'id': 'ohlcv-single-1000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'trade_date': DAYS[1], 'limit': '1000'}},
        {'id': 'ohlcv-single-5000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'trade_date': DAYS[1], 'limit': '5000'}},
        {'id': 'ohlcv-single-6000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'trade_date': DAYS[1], 'limit': '6000'}},
        {'id': 'ohlcv-two-days-12000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'start_date': DAYS[0], 'end_date': DAYS[1], 'limit': '12000'}},
    ]


def plan():
    """Not caller-selectable dates/URLs/limits; this authorization is finite."""
    common = {'fields': FIELDS, 'offset': '0'}
    return [
        {'id': 'daily-capability', 'kind': 'capability', 'api': 'daily', 'parameters': {}},
        {'id': 'single-6000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'trade_date': DAYS[1], 'limit': '6000'}},
        {'id': 'single-12000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'trade_date': DAYS[1], 'limit': '12000'}},
        {'id': 'two-days-12000', 'kind': 'quote', 'api': 'daily',
         'parameters': {**common, 'start_date': DAYS[0], 'end_date': DAYS[1], 'limit': '12000'}},
    ]


def _decode(raw, *, decimals=False):
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY, 'BULK_RESPONSE_BOUND')
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, 'BULK_DUPLICATE_JSON_KEY')
            out[key] = value
        return out
    def reject(_):
        raise ValueError('BULK_NONFINITE_JSON')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=reject,
                      **({'parse_float': Decimal} if decimals else {}))


def price_text(value):
    """Normalize decimal scale without ambient-context rounding."""
    text = format(_price(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def rows_digest(rows):
    return sha256(json.dumps(sorted(rows), ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def inspect_quote(raw, spec):
    require(spec in plan()[1:], 'BULK_REQUEST_OUTSIDE_STUDY')
    # Reuse normal response validation, but not its unrelated plan's 5000 cap.
    _table('daily', spec['parameters'], _decode(raw))
    body = _decode(raw, decimals=True)
    table = body['data']
    fields, items = table['fields'], table['items']
    require(set(FIELDS.split(',')) <= set(fields), 'BULK_FIELDS_MISSING')
    columns = [fields.index(f) for f in FIELDS.split(',')]
    allowed = {DAYS[1]} if 'trade_date' in spec['parameters'] else set(DAYS)
    rows, seen = [], set()
    for item in items:
        code, day, close = [item[i] for i in columns]
        require(isinstance(code, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code),
                'BULK_SECURITY_IDENTITY')
        require(isinstance(day, str) and day in allowed, 'BULK_DATE_OUTSIDE_REQUEST')
        require((code, day) not in seen, 'BULK_DUPLICATE_SECURITY_DAY')
        seen.add((code, day))
        rows.append([code, day, price_text(close)])
    counts = Counter(row[1] for row in rows)
    partitions = {day: {'rows': counts[day],
                        'rows_sha256': rows_digest([row for row in rows if row[1] == day])}
                  for day in sorted(allowed)}
    return {'rows': len(rows), 'returned_above_5000': len(rows) > 5000,
            'returned_above_6000': len(rows) > 6000,
            'requested_limit': int(spec['parameters']['limit']),
            'reported_count': body.get('count'), 'partitions': partitions,
            'rows_sha256': rows_digest(rows),
            'completeness': 'requires-independent-retained-baseline-comparison',
            'market_qualification_granted': False}



def _nonnegative_text(value):
    """Exact displayed supplier quantity, not an inferred shares/yuan conversion."""
    from decimal import InvalidOperation
    require(type(value) in (int, str, Decimal) and len(str(value)) <= 128, 'BULK_QUANTITY_INVALID')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        require(False, 'BULK_QUANTITY_INVALID')
    require(number.is_finite() and number >= 0 and -99 <= number.adjusted() <= 99,
            'BULK_QUANTITY_INVALID')
    text = format(number, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def inspect_full_quote(raw, spec):
    """Validate actual OHLCV supplier fields without granting market/PIT qualification."""
    require(spec in full_plan()[1:], 'BULK_REQUEST_OUTSIDE_STUDY')
    _table('daily', spec['parameters'], _decode(raw))
    body = _decode(raw, decimals=True)
    table = body['data']
    fields, items = table['fields'], table['items']
    required = FULL_FIELDS.split(',')
    require(set(required) <= set(fields), 'BULK_FIELDS_MISSING')
    indices = {name: fields.index(name) for name in required}
    allowed = {DAYS[1]} if 'trade_date' in spec['parameters'] else set(DAYS)
    records, seen = [], set()
    for item in items:
        code, day = item[indices['ts_code']], item[indices['trade_date']]
        require(isinstance(code, str) and re.fullmatch(r'[0-9]{6}\\.(SH|SZ|BJ)', code),
                'BULK_SECURITY_IDENTITY')
        require(isinstance(day, str) and day in allowed, 'BULK_DATE_OUTSIDE_REQUEST')
        require((code, day) not in seen, 'BULK_DUPLICATE_SECURITY_DAY')
        seen.add((code, day))
        quotes = {key: _price(item[indices[key]]) for key in ('open', 'high', 'low', 'close')}
        require(quotes['low'] <= quotes['open'] <= quotes['high']
                and quotes['low'] <= quotes['close'] <= quotes['high'], 'BULK_OHLC_RANGE')
        values = [code, day, *(price_text(item[indices[key]])
                              for key in ('open', 'high', 'low', 'close')),
                  _nonnegative_text(item[indices['vol']]),
                  _nonnegative_text(item[indices['amount']])]
        records.append(values)
    close_rows = [[row[0], row[1], row[5]] for row in records]
    counts = Counter(row[1] for row in records)
    partitions = {day: {'rows': counts[day],
                        'close_rows_sha256': rows_digest([row for row in close_rows if row[1] == day]),
                        'full_rows_sha256': rows_digest([row for row in records if row[1] == day])}
                  for day in sorted(allowed)}
    return {'rows': len(records), 'fields': required, 'requested_limit': int(spec['parameters']['limit']),
            'reported_count': body.get('count'), 'returned_above_5000': len(records) > 5000,
            'returned_above_6000': len(records) > 6000, 'partitions': partitions,
            'close_rows_sha256': rows_digest(close_rows), 'full_rows_sha256': rows_digest(records),
            'quantity_units': 'supplier-vol-and-amount-unconverted',
            'completeness': 'requires-independent-retained-baseline-comparison',
            'market_qualification_granted': False}


def inspect_capability(raw):
    body = _decode(raw)
    require(isinstance(body, dict), 'BULK_CAPABILITY_SHAPE')
    require(body.get('code', 0) == 0 and body.get('ok', True) is True
            and body.get('success', True) is True and body.get('error') in (None, ''),
            'BULK_CAPABILITY_BUSINESS_FAILURE')
    # Record only explicit declarations; an unrecognized wrapper is not a
    # permission or size inference and need not prevent the authorized tests.
    candidates = [body]
    for key in ('data', 'interface'):
        if isinstance(body.get(key), dict):
            candidates.append(body[key])
    if isinstance(body.get('interfaces'), list):
        candidates.extend(x for x in body['interfaces'] if isinstance(x, dict))
    matches = [x for x in candidates if x.get('name') == 'daily']
    if len(matches) != 1:
        return {'status': 'declaration-not-recognized', 'max_limit_state': 'unknown',
                'max_limit': None, 'business_result': False}
    item = matches[0]
    value = item.get('max_limit')
    require(value is None or type(value) is int and value > 0, 'BULK_CAPABILITY_LIMIT')
    require(item.get('enabled') is True and item.get('methods') == ['GET'],
            'BULK_CAPABILITY_NOT_ENABLED')
    return {'status': 'service-declaration', 'max_limit_state':
            'missing' if 'max_limit' not in item else 'null' if value is None else 'value',
            'max_limit': value, 'business_result': False}
