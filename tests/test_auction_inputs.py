"""Synthetic original captures, fixed-condition counterexamples and full denominators."""
from copy import deepcopy
from datetime import date, timedelta
from io import BytesIO
import json
from pathlib import Path

import pytest

from decision_kernel.application.auction_inputs import AUCTION_READING_SCHEMA, read_auction_context
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import capture_plan, inspect_result, validate_plan, _table
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.opening_auction import PROFILE, prior_pool, apply_auction, previous_session

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = RelayRegistry(ROOT / 'docs/sources/relay-registry.json')
TARGET = '2026-10-20'
PRIOR = '2026-10-19'
STAMP = '2026-10-20T01:27:00+00:00'
CLOSE_STAMP = '2026-10-20T07:30:00+00:00'
POOL_FIELDS = ['trade_date', 'ts_code', 'name', 'close', 'turnover_ratio', 'limit_times', 'limit']
AUCTION_FIELDS = ['ts_code', 'trade_date', 'price', 'pre_close']


class Reply(BytesIO):
    status = 200
    def __init__(self, body, url):
        super().__init__(json.dumps(body).encode())
        self.url, self.headers = url, {}
    def geturl(self):
        return self.url


class Pages:
    def __init__(self, api, fields, rows, limit, metadata=None):
        self.api, self.fields, self.rows, self.limit = api, fields, rows, limit
        self.metadata, self.calls = metadata, []
    def open(self, req, timeout):
        offset = len(self.calls) * self.limit
        self.calls.append(req)
        body = {'code': 0, 'api_name': self.api, 'data': {'fields': self.fields,
                'items': self.rows[offset:offset + self.limit]}, 'count': len(self.rows)}
        if self.metadata is not None:
            body.update(deepcopy(self.metadata[len(self.calls) - 1] if isinstance(self.metadata, list) else self.metadata))
        return Reply(body, req.full_url)


def capture(api, fields, rows, parameters, *, limit=100, stamp=STAMP, metadata=None, max_pages=100):
    request = {'api': api, 'stage': 'baseline', 'purpose': '合成竞价原池与同日原价测试',
               'parameters': {**parameters, 'fields': ','.join(fields), 'limit': limit}}
    opener = Pages(api, fields, rows, limit, metadata)
    results = list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-auction',
                                'requests': [request]}, REGISTRY,
        client=RelayHTTP(opener=opener, credential='synthetic-auction-only'),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: stamp, max_pages=max_pages))
    return results[0]


def auction_payload(*, close=True, close_stamp=CLOSE_STAMP):
    calendar = capture('trade_cal', ['exchange', 'cal_date', 'is_open'],
        [['SSE', '20261019', 1], ['SSE', '20261020', 1]],
        {'exchange': 'SSE', 'start_date': '20260919', 'end_date': '20261020'})
    rows = [
        ['20261019', '600001.SH', '合成甲', '1', '10', '2.0', 'U'],
        ['20261019', '600002.SH', '合成乙', None, '30', 2.0, 'U'],
        ['20261019', '600003.SH', '合成丙', None, '20', 1, 'U'],
        ['20261019', '600004.SH', '合成丁', None, '20', None, 'U'],
        ['20261019', '300001.SZ', '合成排除板', None, '20', 1, 'U'],
        ['20261019', '600005.SH', '合成未知', None, None, 2.5, 'U'],
        ['20261019', '600006.SH', '合成跌停', None, '20', None, 'D'],
        ['20261019', '600007.SH', '合成炸板', None, '20', None, 'Z'],
        ['20261019', '600001.SH', '合成甲', '999', '10.0', 2, 'U'],
        ['20261019', None, '合成未识别', None, '20', 1, 'U'],
        ['20261019', '600008.SH', '合成冲突', None, '20', 1, 'U'],
        ['20261019', '600008.SH', '合成冲突', None, '20', 1, 'D'],
        ['20261019', '600009.SH', '合成未知类型', None, '20', None, 'Q'],
    ]
    pool = capture('limit_list_d', POOL_FIELDS, rows, {'trade_date': '20261019'})
    auction = capture('stk_auction', AUCTION_FIELDS,
        [['600001.SH', '20261020', '103', '100'], ['600002.SH', '20261020', '109', '100'],
         ['600003.SH', '20261020', '102.9999', '100']], {'trade_date': '20261020', 'ts_type': 'STK'})
    payload = {'schema': AUCTION_READING_SCHEMA, 'profile': PROFILE, 'market_session': TARGET,
               'calendar_result': calendar, 'pool_result': pool, 'auction_result': auction}
    if close:
        payload['close_result'] = capture('daily', ['ts_code', 'trade_date', 'close'],
            [['600001.SH', '20261020', '104'], ['600002.SH', '20261020', '108'],
             ['600003.SH', '20261020', '102.9999'], ['600004.SH', '20261020', '105'],
             ['300001.SZ', '20261020', '101'], ['600005.SH', '20261020', '99']],
            {'trade_date': '20261020'}, stamp=close_stamp)
    return payload


def test_full_ud_z_pool_u_denominator_five_groups_and_exact_thresholds():
    payload = auction_payload()
    original = encoded(payload)
    out = read_market_payload(payload, REGISTRY, limit=1, entity_id='600001.SH')
    assert out['cohort_denominator'] == 8 and len(out['cohort_rows']) == 8
    assert out['prior_temperature']['returned_pool_rows'] == 13
    assert out['prior_temperature']['unique_recognized'] == {'U': 6, 'D': 1, 'Z': 1}
    assert out['prior_temperature']['ambiguous_entities'] == ['600008.SH', '600009.SH']
    assert out['prior_temperature']['unidentified_u_row_indices'] == [9]
    assert out['static_eligible'] == 4 and out['auction_comparable'] == 3 and out['matched'] == 2
    assert {k: v['denominator'] for k, v in out['groups'].items()} == {
        'MATCHED': 2, 'NOT_MATCHED': 1, 'AUCTION_UNRESOLVED': 1, 'STATIC_EXCLUDED': 1, 'STATIC_UNRESOLVED': 3}
    assert out['groups']['MATCHED']['above_auction'] == out['groups']['MATCHED']['below_auction'] == 1
    assert out['groups']['NOT_MATCHED']['equal_to_auction'] == 1
    assert out['groups']['AUCTION_UNRESOLVED']['close_available'] == 1
    assert out['groups']['STATIC_EXCLUDED']['close_available'] == 1
    assert out['prior_temperature']['maximum_consecutive'] == 2
    assert out['prior_temperature']['missing_streak_entities'] == ['600004.SH', '600005.SH']
    assert out['timeliness'] == 'RECEIVED_BEFORE_CONTINUOUS_OPEN'
    assert out['coverage']['pool']['status'] == 'returned-query-complete'
    assert out['coverage_gaps'] and len(out['pool_rows']) == 13 and len(out['entries']) == 1
    whole = read_market_payload(payload, REGISTRY, offset=2, limit=3)
    assert whole['view_version'] == out['view_version'] and whole['cohort_rows'] == out['cohort_rows']
    assert encoded(payload) == original


@pytest.mark.parametrize('turnover,price,expected', [
    ('10', '103', 'SHADOW_PROFILE_MATCH'), ('30', '109', 'SHADOW_PROFILE_MATCH'),
    ('9.999999', '103', 'OUTSIDE_PRIOR_TURNOVER_PROFILE'),
    ('30.000001', '109', 'OUTSIDE_PRIOR_TURNOVER_PROFILE'),
    ('20', '102.999999999999', 'OUTSIDE_AUCTION_GAP_PROFILE'),
    ('20', '109.000000000001', 'OUTSIDE_AUCTION_GAP_PROFILE'),
    (True, '104', 'PRIOR_TURNOVER_UNAVAILABLE'), ('20', False, 'AUCTION_VALUE_UNAVAILABLE_OR_CONFLICT'),
    ('20', '0', 'AUCTION_VALUE_UNAVAILABLE_OR_CONFLICT')])
def test_unrounded_conditions_and_independent_invalid_values(turnover, price, expected):
    rows, _ = prior_pool([{'ts_code': '600001.SH', 'trade_date': '20261019', 'name': '合成',
                          'turnover_ratio': turnover, 'limit': 'U', 'limit_times': False}], PRIOR)
    result, _ = apply_auction(rows, [{'ts_code': '600001.SH', 'trade_date': '20261020',
                                     'price': price, 'pre_close': '100'}], TARGET)
    assert result[0]['status'] == expected


def test_calendar_requires_only_exact_prior_interval_and_target_open():
    rows = [['20261016', 1], ['20261017', 0], ['20261018', 0], ['20261019', 1]]
    source = [{'exchange': 'SSE', 'cal_date': d, 'is_open': v} for d, v in rows]
    assert previous_session(source, '2026-10-19') == {'open': True, 'previous_session': '2026-10-16'}
    with pytest.raises(MarketInputError, match='PREVIOUS_TO_TARGET_CALENDAR_GAP'):
        previous_session(source[:2] + source[3:], '2026-10-19')
    assert previous_session(source[:3], '2026-10-18')['open'] is False
    with pytest.raises(MarketInputError, match='CALENDAR_IDENTITY'):
        previous_session(source + [source[-1]], '2026-10-19')


def test_optional_returned_columns_legacy_metadata_and_clocks_do_not_self_qualify():
    payload = auction_payload(close=False)
    raw = capture('limit_list_d', ['trade_date', 'ts_code', 'limit'],
                  [['20261019', '600001.SH', 'U']], {'trade_date': '20261019'})
    # These columns were requested but independently unavailable in the response.
    from decision_kernel.application.research_sources import parameters_for
    params, _ = parameters_for(REGISTRY, 'limit_list_d', {'trade_date': '20261019', 'fields': ','.join(POOL_FIELDS), 'limit': 100})
    body = {'code': 0, 'data': {'fields': ['trade_date', 'ts_code', 'limit'], 'items': [['20261019', '600001.SH', 'U']]}}
    table = _table('limit_list_d', params, body)
    assert table['missing_requested_fields'] == ['name', 'close', 'turnover_ratio', 'limit_times']
    payload['pool_result'] = raw
    out = read_market_payload(payload, REGISTRY)
    assert out['cohort_denominator'] == 1 and out['static_eligible'] == 0
    assert out['cohort_rows'][0]['status'] == 'PRIOR_NAME_UNAVAILABLE'
    assert out['groups']['MATCHED']['median_change'] is None
    payload = auction_payload()
    for slot in ('pool_result', 'auction_result'):
        for page in payload[slot]['capture']['pages']:
            page['table'].pop('coverage_evidence')
            page['transport'].pop('request_clock')
            page['attempts'] = []
    out = read_market_payload(payload, REGISTRY)
    assert out['coverage']['pool']['status'] == out['coverage']['auction']['status'] == 'unknown'
    assert out['matched'] == 2 and out['timeliness'] == 'NOT_ESTABLISHED' and out['coverage_gaps']
    payload['preopen'] = True
    with pytest.raises(MarketInputError):
        read_market_payload(payload, REGISTRY)


def test_same_day_same_relay_close_only_and_later_close_does_not_rewrite_cohort():
    payload = auction_payload()
    before = read_market_payload(payload, REGISTRY)
    payload['close_result'] = capture('daily', ['ts_code', 'trade_date', 'close'],
        [['600001.SH', '20261021', '105']], {'trade_date': '20261021'})
    out = read_market_payload(payload, REGISTRY)
    assert out['queries']['close']['status'] == 'rejected' and out['same_session']['comparable'] == 0
    assert out['cohort_version'] == before['cohort_version'] and out['matched'] == before['matched']
    payload['close_result'] = auction_payload()['close_result']
    payload['close_result']['capture']['source_identity'] = 'OTHER_SOURCE'
    assert read_market_payload(payload, REGISTRY)['queries']['close']['status'] == 'rejected'
    payload = auction_payload()
    payload['close_result']['capture']['pages'][0]['table']['items'][1][-1] = None
    out = read_market_payload(payload, REGISTRY)
    assert out['groups']['MATCHED']['denominator'] == 2 and out['groups']['MATCHED']['comparable'] == 1
    assert out['queries']['close']['status'] == 'checked'


def test_narrow_stk_parameter_and_5000_tail_proof_preserve_original_paging():
    request = {'api': 'stk_auction', 'stage': 'baseline', 'purpose': '合成',
        'parameters': {'trade_date': '20261020', 'ts_type': 'STK', 'fields': ','.join(AUCTION_FIELDS), 'limit': 5000}}
    for api, ts_type, limit in [('stk_auction', 'ETF', 5000), ('daily', 'STK', 100), ('stk_auction', 'STK', 5001)]:
        bad = deepcopy(request)
        bad['api'] = api
        bad['parameters'].update(ts_type=ts_type, limit=limit)
        with pytest.raises(RecordError):
            validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic', 'requests': [bad]}, REGISTRY)
    rows = [[f'{600000+i:06}.SH', '20261020', '104', '100'] for i in range(5000)]
    stopped = capture('stk_auction', AUCTION_FIELDS, rows, {'trade_date': '20261020', 'ts_type': 'STK'}, limit=5000, max_pages=1)
    assert not stopped['capture']['complete'] and stopped['capture']['error'] == 'SOURCE_PAGE_BOUND_REACHED'
    assert not inspect_result(stopped, REGISTRY)['complete']
    completed = capture('stk_auction', AUCTION_FIELDS, rows, {'trade_date': '20261020', 'ts_type': 'STK'}, limit=5000, max_pages=2)
    view = inspect_result(completed, REGISTRY)
    assert view['complete'] and view['page_count'] == 2 and view['row_count'] == 5000
    assert len(view['pages'][-1]['table']['items']) == 0


def test_explicit_continuation_truncation_and_conflicting_count_cannot_complete():
    for metadata, error in [({'has_more': True}, 'SOURCE_COUNT_SUGGESTS_TRUNCATION'),
                            ({'truncated': True}, 'SOURCE_EXPLICIT_TRUNCATION')]:
        result = capture('stk_auction', AUCTION_FIELDS, [['600001.SH', '20261020', '104', '100']],
                         {'trade_date': '20261020', 'ts_type': 'STK'}, metadata=metadata)
        assert not result['capture']['complete'] and result['capture']['error'] == error
        assert not inspect_result(result, REGISTRY)['complete']
    params = {'trade_date': '20261020', 'ts_type': 'STK', 'fields': ','.join(AUCTION_FIELDS), 'limit': '100'}
    body = {'code': 0, 'count': 1, 'data': {'fields': AUCTION_FIELDS,
            'items': [['600001.SH', '20261020', '104', '100']], 'count': 2}}
    with pytest.raises(RecordError, match='COUNT_CONFLICT'):
        _table('stk_auction', params, body)


def test_total_cannot_change_between_pages_or_be_smaller_than_received_scope():
    rows = [[f'60000{i}.SH', '20261020', '104', '100'] for i in range(1, 4)]
    parameters = {'trade_date': '20261020', 'ts_type': 'STK'}
    for first, last in ((3, 2), (2, 2)):
        result = capture('stk_auction', AUCTION_FIELDS, rows, parameters, limit=2,
            metadata=[{'count': 2, 'total': first}, {'count': 1, 'total': last}])
        assert not result['capture']['complete']
        assert result['capture']['error'] == 'SOURCE_PAGE_COUNT_CONFLICT'
        assert not inspect_result(result, REGISTRY)['complete']
    control = capture('stk_auction', AUCTION_FIELDS, rows, parameters, limit=2,
        metadata=[{'count': 2, 'total': 3}, {'count': 1, 'total': 3}])
    assert inspect_result(control, REGISTRY)['complete']
    control['capture']['pages'][-1]['table']['coverage_evidence']['body']['total'] = 2
    with pytest.raises(RecordError, match='SOURCE_PAGE_COUNT_CONFLICT'):
        inspect_result(control, REGISTRY)


def test_late_and_missing_clocks_are_separate_from_read_time_and_corrupt_clock_rejected():
    payload = auction_payload()
    payload['auction_result'] = capture('stk_auction', AUCTION_FIELDS,
        [['600001.SH', '20261020', '104', '100']], {'trade_date': '20261020', 'ts_type': 'STK'},
        stamp='2026-10-20T01:30:00+00:00')
    assert read_market_payload(payload, REGISTRY)['timeliness'] == 'HISTORICAL_OR_LATE_NOT_PREOPEN_DISCOVERY'
    page = payload['auction_result']['capture']['pages'][0]
    page['transport']['request_clock']['received_at'] = '2026-10-20T01:26:00+00:00'
    assert read_market_payload(payload, REGISTRY)['queries']['auction']['reason_code'] == 'SOURCE_REQUEST_CLOCK_INVALID'


@pytest.mark.parametrize('stamp', [
    '0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-12:00'])
def test_extreme_original_clocks_remain_readable_without_preopen_qualification(stamp):
    payload = auction_payload()
    before = read_market_payload(payload, REGISTRY)
    page = payload['auction_result']['capture']['pages'][0]
    page['captured_at'] = stamp
    page['transport']['request_clock'] = {'requested_at': stamp, 'received_at': stamp}
    out = read_market_payload(payload, REGISTRY)
    assert out['queries']['auction']['status'] == 'checked'
    assert out['timeliness'] == 'HISTORICAL_OR_LATE_NOT_PREOPEN_DISCOVERY'
    assert out['cohort_denominator'] == before['cohort_denominator'] == 8
    assert out['matched'] == before['matched'] == 2
    assert out['cohort_rows'] == before['cohort_rows']


def test_auction_clock_cannot_be_attached_to_an_unrelated_source_api():
    daily = capture('daily_basic', ['ts_code', 'trade_date', 'turnover_rate'],
        [['600001.SH', '20261020', '12']], {'trade_date': '20261020'})
    assert 'request_clock' not in daily['capture']['pages'][0]['transport']
    daily['capture']['pages'][0]['transport']['request_clock'] = deepcopy(
        auction_payload()['auction_result']['capture']['pages'][0]['transport']['request_clock'])
    with pytest.raises(RecordError, match='SOURCE_TRANSPORT_RECEIPT_INVALID'):
        inspect_result(daily, REGISTRY)
