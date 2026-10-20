"""Four bounded public-market source interpretations, independent of acquisition.

Dates, units, nulls and vintage rules belong here. These pure transformations
consume supplied bytes/tables; they never fetch, checkpoint, publish or infer
Research/Investment authority. The existing application owns custody and read scopes separately.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
from urllib.parse import quote, urlencode
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from decision_kernel.domain.records import MAX_BYTES, RecordError, checked_text, require


def clock(value):
    checked_text(value, limit=80)
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require('T' in value and result.tzinfo is not None, 'GP_CLOCK_ZONE')
        return result.astimezone(timezone.utc)
    except ValueError:
        raise RecordError('GP_CLOCK_INVALID') from None


def decode(raw):
    """Keep exact decimal JSON values and reject ambiguous original bodies."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'GP_DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, 'GP_BODY_SIZE')
    def invalid(_):
        raise RecordError('GP_NONFINITE_JSON')
    return json.loads(raw.decode('utf-8-sig'), parse_float=Decimal,
                      parse_constant=invalid, object_pairs_hook=unique)

LEGACY_VERSION = 'global-public-context-v1'
VERSION = 'global-public-context-v2'
FAMILIES = {'treasury': '美国国债期限利率', 'fx': 'ECB 外汇参考价',
            'commodities': '黄金与原油 · 供应商期货日线', 'crypto': 'Coinbase BTC / ETH 日线'}
TENORS = ('1MONTH', '3MONTH', '6MONTH', '1YEAR', '2YEAR', '5YEAR', '10YEAR', '30YEAR')
CURRENCIES = ('USD', 'CNY', 'JPY', 'GBP')
FUTURES = {'GC=F': ('黄金', 'USD_PER_TROY_OUNCE'), 'CL=F': ('WTI原油', 'USD_PER_BARREL'),
           'BZ=F': ('Brent原油', 'USD_PER_BARREL')}
COINS = ('BTC-USD', 'ETH-USD')
AUTHORITY = {'qualification': 'MARKET_CONTEXT_ONLY', 'research_authority': 'NONE',
             'investment_authority': 'NONE', 'human_attention_authority': 'NONE',
             'signal_transition_authority': 'NONE', 'automatic_admission': False,
             'company_mapping': 'NOT_PERFORMED', 'odds_recomputed': False}
ATOM = '{http://www.w3.org/2005/Atom}'
DATA = '{http://schemas.microsoft.com/ado/2007/08/dataservices}'
META = '{http://schemas.microsoft.com/ado/2007/08/dataservices/metadata}'
ECB = '{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}'
GEST = '{http://www.gesmes.org/xml/2002-08-01}'



def day(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value), 'GP_DATE')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise RecordError('GP_DATE') from None

def date_policy(family, started):
    """A request-date ceiling, not proof of publication or a trading calendar.

    Treasury usually publishes by 18:00 Eastern; ECB usually around 16:00 CET.
    The ECB 17:00 local gate is our conservative buffer, not a source timestamp.
    Completed UTC candles and unqualified futures never inherit reference rules.
    """
    require(family in FAMILIES, 'GP_FAMILY')
    instant = clock(started)
    references = {'treasury': ('America/New_York', 18), 'fx': ('Europe/Berlin', 17)}
    if family in references:
        zone, hour = references[family]
        local = instant.astimezone(ZoneInfo(zone))
        ceiling = local.date() - timedelta(days=int(local.hour < hour))
        rule = 'DATED_REFERENCE_RELEASE_OPPORTUNITY_V1'
        not_before = f'{hour:02d}:00'
    else:
        zone, not_before = 'UTC', None
        ceiling = instant.date() - timedelta(days=1)
        rule = ('COMPLETED_UTC_DAY_ONLY' if family == 'crypto'
                else 'PRIOR_UTC_DATE_ONLY_FUTURES_FINALITY_UNKNOWN')
    return {'rule': rule, 'timezone': zone, 'not_before_local': not_before,
            'max_as_of_date': ceiling.isoformat(),
            'meaning': 'REQUEST_CEILING_NOT_ACTUAL_PUBLICATION_OR_SESSION_PROOF'}

def _qualify_date(version, family, as_of, started, policy=None):
    first, end = clock(started), day(as_of)
    require(first.date() - timedelta(days=31) <= end, 'GP_RECENT_DATE')
    if version == LEGACY_VERSION:
        require(policy is None and end < first.date(), 'GP_RECENT_DATE')
    else:
        require(version == VERSION, 'GP_CAPTURE_VERSION')
        require(policy == date_policy(family, started), 'GP_DATE_POLICY')
        require(end <= day(policy['max_as_of_date']), 'GP_RECENT_DATE')

def number(value, *, positive=False):
    require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool)
            and len(str(value)) <= 60, 'GP_NUMBER')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('GP_NUMBER') from exc
    # A tiny exponent in a short JSON number must not expand into unbounded
    # decimal output. Keep finite source precision within the same 60-character
    # qualification used above, before arithmetic or fixed-point formatting.
    require(result.is_finite() and -60 <= result.as_tuple().exponent <= 60
            and abs(result) < Decimal('1e12')
            and (not positive or result > 0), 'GP_NUMBER')
    return result

def textnum(value):
    return None if value is None else format(value, 'f')

def plan(family, as_of):
    require(family in FAMILIES, 'GP_FAMILY')
    end = day(as_of); begin = end - timedelta(days=13)
    def spec(label, host, path, params=None):
        url = 'https://' + host + path
        if params: url += '?' + urlencode(params)
        return {'family': family, 'as_of_date': as_of, 'begin': begin.isoformat(),
                'id': label, 'host': host, 'url': url}
    if family == 'treasury':
        months = sorted({begin.strftime('%Y%m'), end.strftime('%Y%m')})
        return [spec(m, 'home.treasury.gov', '/resource-center/data-chart-center/interest-rates/pages/xml',
                     {'data': 'daily_treasury_yield_curve', 'field_tdr_date_value_month': m}) for m in months]
    if family == 'fx':
        return [spec('EUR', 'www.ecb.europa.eu', '/stats/eurofxref/eurofxref-hist-90d.xml')]
    if family == 'crypto':
        return [spec(c, 'api.exchange.coinbase.com', '/products/' + c + '/candles',
                     {'granularity': 86400, 'start': begin.isoformat() + 'T00:00:00Z',
                      'end': (end + timedelta(days=1)).isoformat() + 'T00:00:00Z'}) for c in COINS]
    def epoch(d): return int(datetime.combine(d, datetime.min.time(), timezone.utc).timestamp())
    return [spec(c, 'query1.finance.yahoo.com', '/v8/finance/chart/' + quote(c, safe=''),
                 {'period1': epoch(begin - timedelta(days=1)), 'period2': epoch(end + timedelta(days=2)),
                  'interval': '1d', 'includePrePost': 'false'}) for c in FUTURES]

def xml(raw):
    # UTF-8-only feed; no DTD/entity loading or parser/network side effects.
    text = raw.decode('utf-8-sig')
    require('<!DOCTYPE' not in text.upper() and '<!ENTITY' not in text.upper(), 'GP_XML_ENTITIES')
    return ET.fromstring(text)

def parse(raw, spec):
    """Return dated source tables. Raw out-of-window rows remain retained, not used."""
    family = spec['family']; rows = []; metadata = {}
    if family == 'treasury':
        root = xml(raw); require(root.tag == ATOM + 'feed', 'GP_TREASURY_ENVELOPE')
        entries = root.findall(ATOM + 'entry'); require(len(entries) <= 31, 'GP_ROW_BOUND')
        for entry in entries:
            props = entry.find('.//' + META + 'properties'); require(props is not None, 'GP_TREASURY_FIELDS')
            require(len({p.tag for p in props}) == len(props), 'GP_DUPLICATE_FIELD')
            fields = {p.tag: p for p in props}
            date_node = fields[DATA + 'NEW_DATE']; value = date_node.text
            require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T00:00:00', value), 'GP_SOURCE_DATE')
            dt = value[:10]; require(day(dt).strftime('%Y%m') == spec['id'], 'GP_MONTH_SCOPE')
            values = {}
            for tenor in TENORS:
                node = fields.get(DATA + 'BC_' + tenor)
                missing = node is None or node.get(META + 'null') == 'true' or not node.text
                v = None if missing else number(node.text)
                require(v is None or abs(v) < 1000, 'GP_RATE_UNIT')
                values['UST:' + tenor] = v
            rows.append((dt, values))
    elif family == 'fx':
        root = xml(raw); require(root.tag == GEST + 'Envelope', 'GP_ECB_ENVELOPE')
        groups = root.findall('./' + ECB + 'Cube/' + ECB + 'Cube')
        require(len(groups) <= 100, 'GP_ROW_BOUND')
        for group in groups:
            dt = group.attrib['time']; day(dt)
            currencies = [v.attrib['currency'] for v in group]
            require(len(currencies) == len(set(currencies)) and len(currencies) <= 64, 'GP_CURRENCY_DUPLICATE')
            values = {v.attrib['currency']: number(v.attrib['rate'], positive=True) for v in group}
            rows.append((dt, {'EUR/' + c: values.get(c) for c in CURRENCIES}))
        metadata = {'base_currency': 'EUR', 'rate_kind': 'ECB_INFORMATION_REFERENCE_NOT_TRANSACTION_QUOTE'}
    elif family == 'crypto':
        body = decode(raw); require(isinstance(body, list) and len(body) <= 300, 'GP_CANDLES')
        for row in body:
            require(isinstance(row, list) and len(row) == 6 and type(row[0]) is int
                    and row[0] > 0 and row[0] % 86400 == 0, 'GP_CANDLE_SHAPE')
            low, high, opening, close, volume = [number(v) for v in row[1:]]
            require(0 < low <= opening <= high and low <= close <= high and volume >= 0, 'GP_OHLC')
            dt = datetime.fromtimestamp(row[0], timezone.utc).date().isoformat()
            rows.append((dt, {spec['id']: close}))
        metadata = {'venue': 'COINBASE_EXCHANGE', 'time_basis': 'UTC_DAILY_BUCKET',
                    'identity_basis': 'EXACT_REQUEST_PRODUCT_NOT_RESPONSE_ECHO', 'missing_buckets_filled': False}
    else:
        body = decode(raw); chart = body['chart']; require(chart['error'] is None, 'GP_YAHOO_ERROR')
        require(isinstance(chart['result'], list) and len(chart['result']) == 1, 'GP_YAHOO_ENVELOPE')
        result = chart['result'][0]; metadata = result['meta']
        require(metadata['symbol'] == spec['id'] and metadata['currency'] == 'USD'
                and metadata['instrumentType'] == 'FUTURE' and metadata.get('dataGranularity') == '1d', 'GP_FUTURE_IDENTITY')
        zone = ZoneInfo(metadata['exchangeTimezoneName'])
        stamps = result.get('timestamp', []); quotes = result['indicators']['quote']
        require(isinstance(stamps, list) and len(stamps) <= 32 and len(quotes) == 1, 'GP_YAHOO_SHAPE')
        closes = quotes[0]['close']; require(isinstance(closes, list) and len(stamps) == len(closes), 'GP_YAHOO_SHAPE')
        for stamp, close in zip(stamps, closes, strict=True):
            require(type(stamp) is int and stamp > 0, 'GP_CANDLE_TIME')
            dt = datetime.fromtimestamp(stamp, zone).date().isoformat()
            rows.append((dt, {spec['id']: None if close is None else number(close, positive=spec['id'] == 'GC=F')}))
    require(len({dt for dt, _ in rows}) == len(rows), 'GP_DUPLICATE_DATE')
    selected = [(dt, values) for dt, values in rows if spec['begin'] <= dt <= spec['as_of_date']]
    for dt, _ in rows: day(dt)
    selected.sort(key=lambda row: row[0])
    return selected, len(rows), metadata

def series_info(family):
    if family == 'treasury': return {f'UST:{t}': ('美国国债 ' + t, 'ANNUAL_PERCENT', 'bp') for t in TENORS}
    if family == 'fx': return {'EUR/' + c: ('1欧元兑' + c, c + '_PER_EUR', '%') for c in CURRENCIES}
    if family == 'crypto': return {c: (c, 'USD_PER_' + c.split('-')[0], '%') for c in COINS}
    return {c: (v[0] + ' · ' + c, v[1], None) for c, v in FUTURES.items()}

def summarize(tables, family, as_of):
    observations = []
    for symbol, (name, unit, change_unit) in series_info(family).items():
        history = sorted([(dt, row[symbol]) for dt, row in tables if symbol in row])
        require(len({dt for dt, _ in history}) == len(history), 'GP_OVERLAPPING_TABLES')
        latest = history[-1] if history else None; previous = history[-2] if len(history) > 1 else None
        value = latest[1] if latest else None; prior = previous[1] if previous else None
        change = None
        with localcontext() as ctx:
            ctx.prec = 36
            if value is not None and prior is not None:
                if change_unit == 'bp': change = (value - prior) * 100
                elif change_unit == '%' and prior > 0: change = ((value / prior - 1) * 100).quantize(Decimal('0.000001'))
        observations.append({'symbol': symbol, 'name': name, 'unit': unit, 'value': textnum(value),
            'source_date': latest[0] if latest else None, 'previous_date': previous[0] if previous else None,
            'previous_value': textnum(prior), 'change': textnum(change), 'change_unit': change_unit,
            'calendar_age_at_requested_end': (day(as_of) - day(latest[0])).days if latest else None,
            'change_scope': ('NOT_COMPUTED_CONTRACT_ROLL_IDENTITY_UNKNOWN' if family == 'commodities'
                             else 'TWO_RETURNED_DATES_NOT_ASSERTED_CONSECUTIVE_SESSIONS'),
            'price_kind': ('VENDOR_FUTURE_DAILY_CLOSE_NOT_SPOT_OR_SETTLEMENT' if family == 'commodities'
                           else 'DATED_SOURCE_OBSERVATION'),
            'publication_time': None, 'market_status': 'UNKNOWN_NOT_INFERRED'})
    return observations
