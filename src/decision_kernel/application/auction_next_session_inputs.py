"""Qualify exact supplied Relay calendars and daily fields for auction T+1.

No transport, custody, source substitution or completed-day caller flag.
"""
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.market_relay_inputs import _day, _view, _rows, _lineage
from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import MAX_BYTES, Selection, SourceRef, encoded
from decision_kernel.observations.market_daily import MarketInputError, trade_day
from decision_kernel.observations.opening_auction import CODE, number, text, close_timeliness
from decision_kernel.observations.auction_next_session import MAX_CALENDAR_DAYS, clock, require

READING_SCHEMA = 'relay-auction-next-session-reading-v1'
VIEW_SCHEMA = 'auction-next-session-context-v1'
MAX_QUERIES = 32


def reference(value):
    ref = SourceRef.from_dict(value)
    require(ref.selection == Selection(), 'WHOLE_SOURCE_REQUIRED')
    return json.loads(encoded(asdict(ref)))


def checked_payload(payload):
    fields = {'schema', 'auction_reference', 'previous_reference', 'checked_at', 'calendar_result', 'quote_results'}
    require(isinstance(payload, dict) and set(payload) == fields and payload['schema'] == READING_SCHEMA,
            'PAYLOAD_INVALID')
    require(len(encoded(payload)) <= MAX_BYTES, 'INPUT_BOUND')
    clock(payload['checked_at'])
    for field in ('auction_reference', 'previous_reference'):
        if payload[field] is not None:
            reference(payload[field])
    require(payload['auction_reference'] is not None or payload['previous_reference'] is not None,
            'ORIGINAL_REFERENCE_REQUIRED')
    quotes = payload['quote_results']
    require(isinstance(quotes, dict) and len(quotes) + int(payload['calendar_result'] is not None) <= MAX_QUERIES,
            'QUOTE_MAP_BOUND')
    for day in quotes:
        trade_day(day)
        require(quotes[day] is not None, 'QUOTE_RESULT_REQUIRED')
    return payload


def _source_clock(view, checked_at):
    end = clock(checked_at)
    for page in view['pages']:
        require(clock(page['captured_at']) <= end, 'SOURCE_IN_FUTURE')
        for value in page['transport'].get('request_clock', {}).values():
            require(clock(value) <= end, 'SOURCE_IN_FUTURE')


def calendar_input(result, registry, checked_at, key):
    inspected = inspect_result(result, registry)
    params = inspected['parameters']
    start, end = params.get('start_date'), params.get('end_date')
    lo, hi = _day(start), _day(end)
    require(0 <= (trade_day(hi) - trade_day(lo)).days < MAX_CALENDAR_DAYS, 'CALENDAR_BOUND')
    view = _view(result, registry, 'trade_cal', {'exchange': 'SSE', 'start_date': start, 'end_date': end},
                 {'exchange', 'cal_date', 'is_open'})
    _source_clock(view, checked_at)
    days = {}
    for row in _rows(view):
        day = _day(row.get('cal_date'))
        require(row.get('exchange') == 'SSE' and lo <= day <= hi and day not in days
                and type(row.get('is_open')) in (str, int) and row['is_open'] in (0, 1, '0', '1'),
                'CALENDAR_IDENTITY')
        days[day] = str(row['is_open']) == '1'
    # Missing explicit dates remain gaps. No weekday or completion inference.
    return {'key': key, 'days': days, 'start': lo, 'end': hi, 'lineage': _lineage(view)}


def daily_input(result, registry, day, checked_at, key):
    target = trade_day(day)
    compact = f'{target.year:04d}{target.month:02d}{target.day:02d}'
    fields = {'ts_code', 'trade_date', 'close'}
    view = _view(result, registry, 'daily', {'trade_date': compact}, fields)
    requested = set(view['parameters']['fields'].split(','))
    require(requested in (fields, fields | {'open'}), 'DAILY_FIELDS')
    require(view['row_count'] <= 10000, 'ROW_BOUND')
    _source_clock(view, checked_at)
    located = defaultdict(list)
    for row in _rows(view):
        code = row.get('ts_code')
        require(isinstance(code, str) and CODE.fullmatch(code) and row.get('trade_date') == compact,
                'QUOTE_IDENTITY')
        located[code].append(row)
    entries = {}
    for code, rows in located.items():
        values = {}
        for field in ('open', 'close'):
            value, status = None, 'FIELD_NOT_REQUESTED'
            if field in requested:
                status = 'DUPLICATE_SECURITY_ROWS' if len(rows) != 1 else 'FIELD_NOT_RETURNED'
                if len(rows) == 1 and rows[0].get(field) is not None:
                    try:
                        n = number(rows[0][field])
                        require(n > 0, 'QUOTE_VALUE_INVALID')
                        value, status = text(n), 'RAW_QUOTE_READABLE'
                    except MarketInputError:
                        status = 'QUOTE_VALUE_INVALID'
            values[field] = {'value': value, 'status': status}
        entries[code] = values
    clocks = [deepcopy(p['transport'].get('request_clock')) for p in view['pages']]
    received = [c['received_at'] for c in clocks if c is not None]
    return {'key': key, 'entries': entries, 'lineage': _lineage(view),
            'completion': close_timeliness(clocks, day),
            'received_through': max(received, key=clock) if received else None,
            'coverage': {'received_rows': view['row_count'], 'unique_entities': len(entries),
                         'requested_fields': sorted(requested),
                         'missing_requested_fields': sorted({f for p in view['pages'] for f in p['table']['missing_requested_fields']}),
                         'generic_capture_complete': view['complete'],
                         'independent_market_exhaustiveness': 'not-established'}}


def merge_calendars(calendars):
    values, through = {}, None
    for item in calendars:
        require(all(values[day] is value for day, value in item['days'].items() if day in values),
                'CALENDAR_REVISED')
        values.update(item['days'])
        through = max(through or item['end'], item['end'])
    return values, through
