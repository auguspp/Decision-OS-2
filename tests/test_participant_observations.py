"""Finite participant semantics and physical-response custody; all rows synthetic."""
from copy import deepcopy
import base64
from dataclasses import asdict
import gzip
import json
import zlib

import pytest

from decision_kernel.domain.records import RecordError, digest, encoded
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


def large_gzip_page(count=1600):
    rows = [{'ts_code': '600000.SH' if index % 2 else '510300.SH', 'holding_date': '20260930',
             'channel': 'synthetic-sh-connect', 'holding': index,
             'name': '合成证券 ' + digest(str(index).encode())} for index in range(count)]
    return page('hkex_shareholding', rows, offset=125, reported_total=count + 125,
                request_complete=False, error='RELAY_HTTP_UNAVAILABLE', wire_sha256='1' * 64)


def gzip_sources(original):
    return observations.gzip_page_sources(original, source_id='synthetic-gzip-page',
        origin='synthetic:retained-page', title='Synthetic compressed structured page')


def sealed_gzip_parts(raw, *, packed=None, page_byte_size=None):
    """Seal arbitrary test bytes, so inner corruption is not hidden by an outer digest error."""
    packed = gzip.compress(raw, mtime=0) if packed is None else packed
    chunks = [packed[start:start + observations.GZIP_PART_BYTES]
              for start in range(0, len(packed), observations.GZIP_PART_BYTES)]
    return [{'schema': observations.GZIP_PART_SCHEMA, 'page_sha256': digest(raw),
        'page_byte_size': len(raw) if page_byte_size is None else page_byte_size,
        'gzip_sha256': digest(packed), 'gzip_byte_size': len(packed),
        'part_index': index, 'part_count': len(chunks),
        'byte_start': index * observations.GZIP_PART_BYTES,
        'data': base64.b64encode(chunk).decode('ascii')} for index, chunk in enumerate(chunks)]


def test_gzip_whole_page_preserves_rows_controls_and_real_host_payload():
    from decision_kernel.interfaces.research_payload import decode_payload, MAX_PAYLOAD_BYTES
    original = large_gzip_page(3000)
    before = deepcopy(original)
    sources = gzip_sources(original)
    old_sources = observations.page_sources(original, source_id='synthetic-old-page',
        origin='synthetic:retained-page', title='Synthetic uncompressed structured page')
    assert 1 < len(sources) < len(old_sources) <= observations.MAX_PARTS
    parts = []
    for source in sources:
        payload = encoded(asdict(source))
        assert len(payload) <= MAX_PAYLOAD_BYTES == observations.HOST_PAYLOAD_BYTES
        assert decode_payload(payload.decode()) == json.loads(payload)
        assert source.acquired_at == original['captured_at']
        assert 'STRUCTURED_REPRESENTATION_NOT_ORIGINAL_HTTP_BYTES' in source.limitations
        assert 'NO_PUBLICATION_RIGHTS_PIT_OR_RESEARCH_CERTIFICATION' in source.limitations
        parts.append(json.loads(source.text))
    joined = observations.join_page(parts)
    assert joined == original == before
    assert observations.join_page([json.loads(s.text) for s in old_sources]) == before
    assert all(json.loads(s.text)['schema'] == observations.PART_SCHEMA for s in old_sources)
    result = observations.normalize_rows(joined)
    assert result == observations.normalize_rows(before)
    assert result['raw_row_count'] == 3000 and result['accepted_row_count'] == 1500
    assert len(result['rejected_rows']) == 1500 and result['rejected_rows'][0]['source_row'] == 125
    assert result['physical_page'] == before['physical_page'] and result['source_requests'] == 0


@pytest.mark.parametrize('failed', [False, True])
def test_gzip_empty_page_keeps_original_completion_and_failure(failed):
    original = page(rows=[], reported_total=None, request_complete=not failed,
                    error='RELAY_HTTP_UNAVAILABLE' if failed else None)
    sources = gzip_sources(original)
    assert len(sources) == 1
    assert observations.join_page([json.loads(sources[0].text)]) == original


@pytest.fixture
def gzip_parts():
    parts = [json.loads(source.text) for source in gzip_sources(large_gzip_page())]
    assert len(parts) > 1
    return parts


@pytest.mark.parametrize('change', [
    'missing', 'duplicate', 'reordered', 'byte-gap', 'bool-index', 'bool-count', 'bool-offset',
    'page-identity', 'gzip-identity', 'byte-edited', 'invalid-base64', 'empty-base64',
    'noncanonical-base64', 'stale-page-digest', 'stale-gzip-digest',
    'mixed-old-first', 'mixed-old-last', 'extra-field',
])
def test_gzip_parts_cannot_drop_reorder_mix_or_rewrite_whole_page(gzip_parts, change):
    if change == 'missing': gzip_parts.pop()
    if change == 'duplicate': gzip_parts.append(deepcopy(gzip_parts[-1]))
    if change == 'reordered': gzip_parts.reverse()
    if change == 'byte-gap': gzip_parts[-1]['byte_start'] += 1
    if change == 'bool-index': gzip_parts[0]['part_index'] = False
    if change == 'bool-count': gzip_parts[0]['part_count'] = True
    if change == 'bool-offset': gzip_parts[0]['byte_start'] = False
    if change == 'page-identity': gzip_parts[-1]['page_sha256'] = 'f' * 64
    if change == 'gzip-identity': gzip_parts[-1]['gzip_sha256'] = 'f' * 64
    if change == 'byte-edited': gzip_parts[0]['data'] = 'eA==' + gzip_parts[0]['data'][4:]
    if change == 'invalid-base64': gzip_parts[0]['data'] = '合成!'
    if change == 'empty-base64': gzip_parts[0]['data'] = ''
    if change == 'noncanonical-base64': gzip_parts[-1]['data'] += '='
    if change == 'stale-page-digest':
        for part in gzip_parts: part['page_sha256'] = 'f' * 64
    if change == 'stale-gzip-digest':
        for part in gzip_parts: part['gzip_sha256'] = 'f' * 64
    if change.startswith('mixed-old-'):
        old = json.loads(observations.page_sources(page(), source_id='synthetic-old-mixed',
            origin='synthetic:old', title='Synthetic old page')[0].text)
        gzip_parts[0 if change == 'mixed-old-first' else -1] = old
    if change == 'extra-field': gzip_parts[0]['original_http_retained'] = True
    with pytest.raises(RecordError): observations.join_page(gzip_parts)


@pytest.mark.parametrize('change', ['truncated', 'crc', 'multiple-members', 'trailing-zero',
    'trailing-text', 'zlib-format', 'smaller-declaration', 'larger-declaration'])
def test_gzip_stream_must_be_one_complete_member_with_exact_size(change):
    raw = encoded(page())
    packed, declared = gzip.compress(raw, mtime=0), len(raw)
    if change == 'truncated': packed = packed[:-1]
    if change == 'crc': packed = packed[:-8] + bytes([packed[-8] ^ 1]) + packed[-7:]
    if change == 'multiple-members': packed += gzip.compress(b'', mtime=0)
    if change == 'trailing-zero': packed += b'\0'
    if change == 'trailing-text': packed += b'synthetic trailing bytes'
    if change == 'zlib-format': packed = zlib.compress(raw)
    if change == 'smaller-declaration': declared -= 1
    if change == 'larger-declaration': declared += 1
    parts = sealed_gzip_parts(raw, packed=packed, page_byte_size=declared)
    with pytest.raises(RecordError): observations.join_page(parts)


def test_gzip_bomb_is_bounded_before_parsing_or_unbounded_flush(monkeypatch):
    declared = observations.MAX_PAGE_BYTES
    raw = b'x' * (declared + 1)
    parts = sealed_gzip_parts(raw, page_byte_size=declared)
    decompressor = zlib.decompressobj
    limits = []

    class BoundedDecoder:
        def __init__(self, *args): self.decoder = decompressor(*args)
        def decompress(self, packed, maximum):
            limits.append(maximum)
            return self.decoder.decompress(packed, maximum)
        def __getattr__(self, key):
            assert key != 'flush', 'No unbounded decompression flush is permitted'
            return getattr(self.decoder, key)

    monkeypatch.setattr(observations.zlib, 'decompressobj', BoundedDecoder)
    with pytest.raises(RecordError, match='DECOMPRESSION_BOUND'):
        observations.join_page(parts)
    assert limits == [declared + 1]


@pytest.mark.parametrize('raw', [
    b' ' + encoded(page()), encoded(page()) + b'\n', encoded(page())[:-1],
    b'{"rows":[],"rows":[]}\n', b'{"rows":[{"value":NaN}]}\n',
    b'\xff\n', b'\xef\xbb\xbf' + encoded(page()),
], ids=['leading-space', 'extra-newline', 'missing-newline', 'duplicate-key', 'nan', 'invalid-utf8', 'bom'])
def test_gzip_decoded_json_must_be_canonical_utf8_without_duplicate_keys(raw):
    with pytest.raises(RecordError, match='GZIP_JSON'):
        observations.join_page(sealed_gzip_parts(raw))


def test_gzip_excessive_json_nesting_is_a_public_record_error():
    raw = b'[' * 10000 + b']' * 10000 + b'\n'
    with pytest.raises(RecordError): observations.join_page(sealed_gzip_parts(raw))


@pytest.mark.parametrize('change', ['wrong-shape', 'wrong-row-type', 'wrong-physical-count', 'invalid-field'])
def test_gzip_canonical_json_still_rechecks_page_contract(change):
    original = page()
    if change == 'wrong-shape': original = [original]
    if change == 'wrong-row-type': original['rows'] = 1
    if change == 'wrong-physical-count': original['physical_page']['row_count'] += 1
    if change == 'invalid-field': original['rows'][0]['unqualified-field'] = 'Synthetic'
    with pytest.raises(RecordError): observations.join_page(sealed_gzip_parts(encoded(original)))


@pytest.mark.parametrize('field,value', [
    ('page_byte_size', 0), ('page_byte_size', True), ('page_byte_size', observations.MAX_PAGE_BYTES + 1),
    ('gzip_byte_size', observations.MAX_GZIP_BYTES + 1), ('gzip_byte_size', False),
    ('page_sha256', 'F' * 64), ('gzip_sha256', 'synthetic-invalid-hash'),
])
def test_gzip_declared_limits_reject_before_allocating_or_decoding(field, value, monkeypatch):
    parts = sealed_gzip_parts(encoded(page()))
    parts[0][field] = value

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid declared limits must stop before byte decoding')

    monkeypatch.setattr(observations.base64, 'b64decode', forbidden)
    with pytest.raises(RecordError, match='GZIP_IDENTITY'): observations.join_page(parts)


def test_gzip_part_and_aggregate_limits_are_not_bypassed(gzip_parts):
    parts = deepcopy(gzip_parts)
    parts[0]['data'] += 'AAAA'
    with pytest.raises(RecordError, match='GZIP_PART_BOUND'): observations.join_page(parts)
    parts = deepcopy(gzip_parts)
    for part in parts: part['gzip_byte_size'] = 1
    with pytest.raises(RecordError, match='GZIP_BYTES_BOUND'): observations.join_page(parts)
    with pytest.raises(RecordError, match='PART_BOUND'):
        observations.join_page([gzip_parts[0]] * (observations.MAX_PARTS + 1))


def test_gzip_producer_counts_whole_page_and_full_source_metadata(monkeypatch):
    original = page()
    with monkeypatch.context() as patch:
        patch.setattr(observations, 'MAX_PAGE_BYTES', len(encoded(original)) - 1)
        with pytest.raises(RecordError, match='PAGE_BOUND'): gzip_sources(original)
    with monkeypatch.context() as patch:
        patch.setattr(observations, 'MAX_GZIP_BYTES', 1)
        with pytest.raises(RecordError, match='GZIP_BYTES_BOUND'): gzip_sources(original)
    original = large_gzip_page()
    original['coverage'] = 'c' * 4096
    with pytest.raises(RecordError, match='HOST_PAYLOAD_BOUND'):
        observations.gzip_page_sources(original, source_id='synthetic-large-metadata',
            origin='synthetic:' + 'x' * 4086, title='t' * 4096)
