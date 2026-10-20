"""Synthetic original captures through the normal T+1 dispatcher, no live data."""
from copy import deepcopy
from datetime import date, timedelta

import pytest

from decision_kernel.application.auction_next_session_inputs import READING_SCHEMA
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.research_sources import capture_plan, _table
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy
from decision_kernel.observations.auction_next_session import target_session
from decision_kernel.observations.market_daily import MarketInputError
from test_auction_inputs import ROOT, REGISTRY, Pages, capture, auction_payload
from test_auction_custody import saved_fixture

CHECKED = '2026-10-21T08:00:00+00:00'
RECEIVED = '2026-10-21T07:30:00+00:00'
FIELDS = ['ts_code', 'trade_date', 'close', 'open']


def daily(rows=None, *, day='2026-10-21', stamp=RECEIVED, requested=FIELDS, returned=None, parameters=None):
    returned = list(requested) if returned is None else list(returned)
    rows = [['600001.SH', day.replace('-', ''), '111', '110'],
            ['600002.SH', day.replace('-', ''), '112', '109']] if rows is None else rows
    opener = Pages('daily', returned, rows, 100)
    request = {'api': 'daily', 'stage': 'baseline', 'purpose': '合成原名单下一开市日独立字段',
               'parameters': {**({'trade_date': day.replace('-', '')} if parameters is None else parameters),
                              'fields': ','.join(requested), 'limit': 100}}
    return list(capture_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-auction-next-session',
        'requests': [request]}, REGISTRY, client=RelayHTTP(opener=opener, credential='synthetic-only'),
        public_rights_confirmed=True, policy=RetryPolicy(max_attempts=1, max_wait_seconds=0),
        clock=lambda: stamp))[0]


def calendar(rows=None, *, start='20261021', end='20261021'):
    return capture('trade_cal', ['exchange', 'cal_date', 'is_open'],
        [['SSE', '20261021', 1]] if rows is None else rows,
        {'exchange': 'SSE', 'start_date': start, 'end_date': end})


def payload(auction_reference, *, previous=None, quotes=None, cal=True, checked=CHECKED):
    return {'schema': READING_SCHEMA, 'auction_reference': auction_reference,
            'previous_reference': previous, 'checked_at': checked,
            'calendar_result': calendar() if cal is True else cal,
            'quote_results': {'2026-10-21': daily()} if quotes is None else quotes}


def read(value, store):
    return read_market_payload(value, REGISTRY, store=store, limit=1, entity_id='600001.SH')


def test_exact_original_full_u_cohort_groups_and_full_scope_before_paging():
    store, _, original = saved_fixture()
    out = read(payload(original['reference']), store)
    c = out['cohorts'][0]
    assert c['cohort_denominator'] == 8 and len(c['members']) == len(c['result']['rows']) == 8
    assert c['prior_temperature']['returned_pool_rows'] == 13
    assert c['original_groups'] == {'MATCHED': 2, 'NOT_MATCHED': 1, 'AUCTION_UNRESOLVED': 1,
                                   'STATIC_EXCLUDED': 1, 'STATIC_UNRESOLVED': 3}
    assert c['target_session'] == '2026-10-21' and c['result']['completion_clock'] == 'RECEIVED_AFTER_SESSION_COMPLETION'
    assert out['entries'][0]['next_open'] == '110' and out['entries'][0]['next_close'] == '111'
    assert len(out['entries']) == 1 and c['result']['groups']['MATCHED']['open_observed'] == 2
    unidentified = next(r for r in c['result']['rows'] if r['entity_id'] is None)
    assert unidentified['next_open'] is None and unidentified['open_status'] == 'COHORT_IDENTITY_UNRESOLVED'


@pytest.mark.parametrize('open_value,close_value,expected_open,expected_close', [
    ('110', 'bad', '110', None), ('bad', '111', None, '111'), (None, '111', None, '111'),
    ('0', '111', None, '111'), ('1e999999999', '111', None, '111')])
def test_raw_open_and_close_are_qualified_independently(open_value, close_value, expected_open, expected_close):
    store, _, original = saved_fixture()
    source = daily([['600001.SH', '20261021', close_value, open_value]])
    row = read(payload(original['reference'], quotes={'2026-10-21': source}), store)['entries'][0]
    assert row['next_open'] == expected_open and row['next_close'] == expected_close


def test_only_declared_marketwide_open_can_be_missing_without_daily_coverage_expansion():
    store, _, original = saved_fixture()
    for requested, expected in ((FIELDS, 'FIELD_NOT_RETURNED'), (FIELDS[:-1], 'FIELD_NOT_REQUESTED')):
        source = daily([['600001.SH', '20261021', '111']], requested=requested, returned=FIELDS[:-1])
        assert source['status'] == 'captured'
        table = source['capture']['pages'][0]['table']
        assert 'coverage_evidence' not in table
        row = read(payload(original['reference'], quotes={'2026-10-21': source}), store)['entries'][0]
        assert row['next_close'] == '111' and row['next_open'] is None and row['open_status'] == expected
    body = {'code': 0, 'data': {'fields': FIELDS[:-1], 'items': [['600001.SH', '20261021', '111']]}}
    with pytest.raises(RecordError, match='SOURCE_REQUESTED_FIELDS_MISSING'):
        _table('daily', {'ts_code': '600001.SH', 'trade_date': '20261021', 'limit': '100', 'fields': ','.join(FIELDS)}, body)
    with pytest.raises(RecordError, match='SOURCE_REQUESTED_FIELDS_MISSING'):
        _table('daily', {'trade_date': '20261021', 'limit': '100', 'fields': ','.join(FIELDS)},
               {'code': 0, 'data': {'fields': ['ts_code', 'trade_date', 'open'], 'items': [['600001.SH', '20261021', '110']]}})


@pytest.mark.parametrize('stamp,legacy,completion,qualified', [
    ('2026-10-21T07:29:59+00:00', False, 'REQUESTED_BEFORE_SESSION_COMPLETION', False),
    (RECEIVED, True, 'NOT_ESTABLISHED', False),
    (RECEIVED, False, 'RECEIVED_AFTER_SESSION_COMPLETION', True),
    ('2026-10-22T01:00:00+00:00', False, 'RECEIVED_AFTER_SESSION_COMPLETION', True)])
def test_first_open_target_is_independent_of_completed_source_clock(stamp, legacy, completion, qualified):
    store, _, original = saved_fixture()
    source = daily(stamp=stamp)
    if legacy:
        for page in source['capture']['pages']:
            page['transport'].pop('request_clock')
    out = read(payload(original['reference'], quotes={'2026-10-21': source}, checked='2026-10-22T08:00:00+00:00'), store)
    c = out['cohorts'][0]
    assert c['target_session'] == '2026-10-21' and c['result']['completion_clock'] == completion
    assert out['entries'][0]['readable_open'] == '110'
    assert out['entries'][0]['next_open'] == ('110' if qualified else None)
    if stamp.startswith('2026-10-22'):
        assert c['result']['acquisition_timing'] == 'LATER_ACQUISITION_NOT_HISTORICAL_KNOWLEDGE'
    missing = read(payload(original['reference'], quotes={}), store)['cohorts'][0]
    assert missing['target_session'] == '2026-10-21' and missing['result']['completion_clock'] == 'NOT_ESTABLISHED'


def test_explicit_holiday_prefix_gap_and_no_quote_deferral():
    store, _, original = saved_fixture()
    cal = calendar([['SSE', '20261021', 0], ['SSE', '20261022', 0], ['SSE', '20261023', 1]], end='20261023')
    c = read(payload(original['reference'], quotes={}, cal=cal), store)['cohorts'][0]
    assert c['target_session'] == '2026-10-23' and len(c['calendar_prefix']) == 3
    gap = calendar([['SSE', '20261021', 0], ['SSE', '20261023', 1]], end='20261023')
    c = read(payload(original['reference'], quotes={}, cal=gap), store)['cohorts'][0]
    assert c['target_session'] is None and c['calendar_gap'] == '2026-10-22'
    with pytest.raises(MarketInputError, match='CALENDAR_WINDOW_INVALID'):
        target_session({'market_session': '9999-12-31'}, {}, None)
    start = date(2026, 10, 21)
    values = {(start + timedelta(days=n)).isoformat(): False for n in range(513)}
    with pytest.raises(MarketInputError, match='CALENDAR_BOUND'):
        target_session({'market_session': '2026-10-20'}, values, max(values))


def test_wrong_date_scope_future_clock_and_caller_old_model_are_rejected_before_writes():
    store, _, original = saved_fixture()
    before = store.backend.head()
    source = daily(stamp='2026-10-22T07:30:00+00:00')
    with pytest.raises(MarketInputError, match='SOURCE_IN_FUTURE'):
        read(payload(original['reference'], quotes={'2026-10-21': source}), store)
    invalid = payload(original['reference']); invalid['previous_model'] = {'qualified': True}
    with pytest.raises(MarketInputError, match='PAYLOAD_INVALID'):
        read(invalid, store)
    wrong = payload(original['reference']); wrong['quote_results'] = {'2026-10-22': daily()}
    with pytest.raises(MarketInputError, match='QUERY_SCOPE'):
        read(wrong, store)
    assert store.backend.head() == before


def test_required_checked_clock_rejects_null_before_any_source_read(monkeypatch):
    store, _, original = saved_fixture()
    before = store.backend.head()
    def forbidden(_):
        pytest.fail('invalid mandatory clock must fail before exact source reads')
    monkeypatch.setattr(store.backend, 'resolve', forbidden)
    with pytest.raises(MarketInputError, match='AUCTION_NEXT_CLOCK_INVALID'):
        read(payload(original['reference'], checked=None), store)
    assert store.backend.head() == before
