"""Finite participant semantics and physical-response custody; all rows synthetic."""
from copy import deepcopy
import json

import pytest

from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.observations import participant_observations as observations
from decision_kernel.observations import relay_participants
from test_relay_participants import capture as legacy_capture

CLOCK = '2026-10-19T16:00:00+00:00'


def page(api='hm_detail', rows=None, **control):
    rows = [{'ts_code': '002384.SZ', 'trade_date': '20261016',
             'hm_name': 'Synthetic source label', 'hm_orgs': 'Synthetic seat', 'buy_amount': 10}] if rows is None else rows
    return {'schema': observations.PAGE_SCHEMA, 'api': api, 'provider': 'synthetic-provider',
        'captured_at': CLOCK, 'query': {'date': '20261016'}, 'coverage': 'Synthetic finite page; not live coverage.',
        'physical_page': {'page_id': 'synthetic-page', 'offset': 0, 'row_count': len(rows),
            'reported_total': len(rows), 'request_complete': True, 'error': None, 'wire_sha256': None, **control},
        'rows': rows}


def test_custody_parts_preserve_all_rows_and_original_physical_page(monkeypatch):
    rows = [{'ts_code': ('600000.SH' if index % 2 else '510300.SH'), 'holding_date': '20260930',
             'channel': 'synthetic-sh-connect', 'holding': index, 'name': 'Synthetic ' * 15}
            for index in range(3000)]
    original = page('hkex_shareholding', rows, offset=125)
    before = deepcopy(original)
    parts = observations.page_sources(original, source_id='synthetic-quarterly',
        origin='synthetic:hkex-page', title='Synthetic quarterly page')
    assert len(parts) > 1 and all(len(p.text.encode()) <= observations.PART_BYTES for p in parts)
    joined = observations.join_page([json.loads(source.text) for source in parts])
    assert joined == original == before and joined['physical_page']['offset'] == 125
    result = observations.normalize_rows(joined)
    assert result['raw_row_count'] == 3000 and result['accepted_row_count'] == 1500
    assert len(result['rejected_rows']) == 1500 and result['rejected_rows'][0]['source_row'] == 125
    assert result['source_requests'] == 0


@pytest.mark.parametrize('change', ['missing-part', 'duplicate-part', 'row-edited', 'count-edited', 'physical-page-edited'])
def test_missing_or_modified_custody_parts_never_become_complete(change, monkeypatch):
    monkeypatch.setattr(observations, 'PART_BYTES', 6000)
    original = page(rows=[{'ts_code': '002384.SZ', 'trade_date': '20261016', 'hm_name': 'Synthetic' * 60,
                           'hm_orgs': 'Synthetic', 'buy_amount': i} for i in range(30)])
    parts = [json.loads(s.text) for s in observations.page_sources(original, source_id='synthetic-parts',
        origin='synthetic:page', title='Synthetic page')]
    assert len(parts) > 1
    if change == 'missing-part': parts.pop()
    if change == 'duplicate-part': parts.append(deepcopy(parts[0]))
    if change == 'row-edited': parts[0]['rows'][0]['buy_amount'] = 999
    if change == 'count-edited': parts[0]['part_count'] += 1
    if change == 'physical-page-edited': parts[-1]['page']['physical_page']['page_id'] = 'other-page'
    with pytest.raises(RecordError): observations.join_page(parts)


def test_generic_units_and_report_dates_do_not_become_daily_positions():
    rows = [{'ts_code': '002384.SZ', 'holding_date': '20260930', 'channel': 'synthetic-connect', 'holding': 100},
            {'ts_code': '002384.SZ', 'holding_date': '20260630', 'channel': 'synthetic-connect', 'holding': 200}]
    result = observations.normalize_rows(page('hkex_shareholding', rows))
    assert len(result['rows']) == 2
    for row in result['rows']:
        assert row['unit_qualification'] == 'UNKNOWN_SOURCE_UNITS'
        assert row['publication_date'] is None and row['position_inferred'] is False
        assert row['historical_available_at'] == 'NOT_ESTABLISHED'
        assert 'NOT_CURRENT_OR_DAILY_NET_BUY' in row['interpretation']
    assert observations.estimate_changes(result['rows']) == []


def test_institution_filter_keeps_ordinary_seat_rejection_and_anonymous_label():
    rows = [{'ts_code': '002384.SZ', 'trade_date': '20261016', 'exalter': 'Synthetic营业部', 'buy': 1},
            {'ts_code': '002384.SZ', 'trade_date': '20261016', 'exalter': '机构专用', 'buy': 2}]
    result = observations.normalize_rows(page('top_inst', rows))
    assert result['raw_row_count'] == 2 and result['accepted_row_count'] == 1
    assert result['rejected_rows'][0]['reason_code'] == 'PARTICIPANT_NON_INSTITUTION_SEAT'
    assert result['rows'][0]['participant'] is None and result['rows'][0]['anonymous_participant'] is True
    assert result['rows'][0]['raw']['exalter'] == '机构专用'


def test_fund_security_and_owned_company_are_distinct_and_label_scope_is_not_global():
    row = {'ts_code': '001753.OF', 'symbol': '002384.SZ', 'ann_date': '20260831',
           'end_date': '20260630', 'amount': 100}
    first = observations.normalize_rows(page('fund_portfolio', [row]))['rows'][0]
    second_page = page('fund_portfolio', [row]); second_page['provider'] = 'another-source'
    second = observations.normalize_rows(second_page)['rows'][0]
    assert first['ts_code'] == '002384.SZ' and first['company_id'] == 'security-002384-sz'
    assert first['participant']['labels'] == [{'field': 'ts_code', 'value': '001753.OF'}]
    assert first['participant']['participant_id'] != second['participant']['participant_id']
    assert first['reported_day'] == '2026-08-31' and first['report_period'] == '20260630'


def test_visits_are_rows_not_event_count_and_buyback_has_no_invented_holder():
    visit = {'ts_code': '002384.SZ', 'surv_date': '20261008', 'rece_org': 'Synthetic investor group', 'rece_mode': '会议'}
    result = observations.normalize_rows(page('stk_surv', [visit, visit]))
    assert result['accepted_row_count'] == 2 and all(r['event_count'] is None for r in result['rows'])
    buyback = observations.normalize_rows(page('repurchase', [{'ts_code': '002384.SZ', 'ann_date': '20261016',
        'end_date': '20260930', 'proc': '实施', 'vol': 100}]))['rows'][0]
    assert buyback['participant'] is None and buyback['raw']['proc'] == '实施'
    assert 'CUMULATIVE' in buyback['interpretation'] and observations.estimate_changes([buyback]) == []


def estimates():
    return [{'ts_code': '002384.SZ', 'org_name': 'Synthetic analyst firm', 'quarter': '2027Q4',
             'report_date': day, 'eps': eps, 'np': None} for day, eps in [('20261001', '1.20'), ('20261016', '1.30')]]


def test_only_same_issuer_source_label_target_period_estimates_compare():
    rows = estimates()
    extra = [{**rows[1], 'org_name': 'Other firm', 'eps': '99'},
             {**rows[1], 'quarter': '2028Q4', 'eps': '100'},
             {**rows[1], 'ts_code': '600000.SH', 'eps': '101'}]
    result = observations.normalize_rows(page('report_rc', [*rows, *extra]))
    changes = observations.estimate_changes(result['rows'])
    eps = [c for c in changes if c['metric'] == 'eps']
    assert len(eps) == 1 and eps[0]['delta'] == '0.10' and eps[0]['target_period'] == '2027Q4'
    assert eps[0]['relative_change'] is None
    assert [c for c in changes if c['metric'] == 'np'][0]['reason_code'] == 'PARTICIPANT_REVISION_VALUE_MISSING'


def test_same_day_conflicts_cannot_create_an_arbitrary_next_revision():
    rows = estimates()
    rows.insert(1, {**rows[0], 'eps': '2.00'})
    parsed = observations.normalize_rows(page('report_rc', rows))['rows']
    changes = [c for c in observations.estimate_changes(parsed) if c['metric'] == 'eps']
    assert len(changes) == 2 and all(c['delta'] is None for c in changes)
    assert all(c['reason_code'] == 'PARTICIPANT_REVISION_DATE_AMBIGUOUS' for c in changes)
    same = observations.normalize_rows(page('report_rc', [rows[0], rows[0]]))['rows']
    assert observations.estimate_changes(same) == []


def test_old_retained_excerpt_reopens_with_missing_termination_still_unknown():
    capture = legacy_capture()
    inspected = relay_participants.inspect_capture(capture)
    source = relay_participants.source_for_page(capture, inspected, 0)
    old = json.loads(source.text)
    parsed = observations.legacy_page(old)
    assert parsed['physical_page']['request_complete'] is False
    result = observations.normalize_rows(parsed)
    assert result['rows'][0]['raw']['eps'] == '1.2300'
    old['page']['rows'][0]['cells']['eps']['raw'] = '999'
    with pytest.raises(RecordError, match='DIGEST'): observations.legacy_page(old)


@pytest.mark.parametrize('api,query,row', [
    ('top10_floatholders', {'ts_code': '002384.SZ', 'period': '20260630'},
     {'ts_code': '600000.SH', 'end_date': '20260630', 'ann_date': '20260831', 'holder_name': 'Synthetic'}),
    ('fund_portfolio', {'ts_code': '001753.OF', 'period': '20260630'},
     {'ts_code': '001754.OF', 'symbol': '002384.SZ', 'end_date': '20260630', 'ann_date': '20260831'}),
    ('hm_detail', {'trade_date': '20261016'},
     {'ts_code': '002384.SZ', 'trade_date': '20261015', 'hm_name': 'Synthetic'}),
    ('stk_surv', {'ts_code': '002384.SZ', 'start_date': '20260801', 'end_date': '20261016'},
     {'ts_code': '002384.SZ', 'surv_date': '20260701', 'rece_org': 'Synthetic'}),
])
def test_generic_table_row_identity_and_period_scope_are_checked_again(api, query, row):
    original = page(api, [row]); original['query'] = query
    result = observations.normalize_rows(original)
    assert result['raw_row_count'] == 1 and result['accepted_row_count'] == 0
    assert result['rejected_rows'][0]['reason_code'] == 'PARTICIPANT_ROW_OUTSIDE_QUERY'
    assert original['rows'] == [row]


def test_capture_parts_preserve_exact_utf8_bytes_and_fit_real_host_payload():
    from dataclasses import asdict
    from decision_kernel.interfaces.research_payload import decode_payload, MAX_PAYLOAD_BYTES
    raw = json.dumps({'capture': {'api': 'top_inst', 'complete': False, 'error': 'RELAY_HTTP_UNAVAILABLE',
        'attempts': [{'attempt': i, 'error': 'RELAY_HTTP_UNAVAILABLE'} for i in range(1, 5)],
        'retained': ('合成字段\\n"\\\\\t' * 15000)}}, ensure_ascii=False, indent=2).encode() + b'\n'
    sources = observations.registered_capture_sources(raw, source_id='synthetic-capture',
        origin='synthetic:retained-capture', title='Synthetic exact capture', acquired_at=CLOCK)
    assert len(sources) > 1
    parts = []
    for source in sources:
        payload = encoded(asdict(source))
        assert len(payload) <= MAX_PAYLOAD_BYTES
        assert decode_payload(payload.decode()) == asdict(source) | {'entities': [], 'limitations': list(source.limitations)}
        parts.append(json.loads(source.text))
    assert observations.join_registered_capture(parts) == raw
    parts[1]['capture_sha256'] = 'f' * 64
    with pytest.raises(RecordError, match='IDENTITY'): observations.join_registered_capture(parts)


@pytest.mark.parametrize('change', ['missing', 'reordered', 'byte-edited', 'size-edited'])
def test_registered_capture_parts_cannot_lose_controls_attempts_or_bytes(change):
    raw = b'{"retained":"' + b'x' * 60000 + b'", "complete": false}\n'
    parts = [json.loads(s.text) for s in observations.registered_capture_sources(raw,
        source_id='synthetic-split', origin='synthetic:capture', title='Synthetic capture', acquired_at=CLOCK)]
    if change == 'missing': parts.pop()
    if change == 'reordered': parts.reverse()
    if change == 'byte-edited': parts[0]['data'] = 'eA==' + parts[0]['data'][4:]
    if change == 'size-edited': parts[0]['byte_size'] += 1
    with pytest.raises(RecordError): observations.join_registered_capture(parts)
