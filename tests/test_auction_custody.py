"""Exact existing root/query/page custody, recovery and legacy excerpt compatibility."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
import subprocess
import sys

import pytest

from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market, restore_market_query, market_reading_projection,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.reading_market import read_market_reference
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from test_market_custody import MemoryBackend
from test_auction_inputs import ROOT, REGISTRY, STAMP, CLOSE_STAMP, auction_payload


def saved_fixture(payload=None):
    backend = MemoryBackend()
    store = VersionedStore(backend)
    def fresh():
        return VersionedStore(MemoryBackend(backend.shared))
    saved = retain_market_reading(auction_payload() if payload is None else payload, REGISTRY, store,
        fresh_reader=fresh, custody_authorized=True)
    return store, fresh, saved


def test_original_custody_replays_exact_metadata_clocks_and_full_cohort():
    payload = auction_payload()
    store, fresh, saved = saved_fixture(payload)
    out = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert encoded(out['view']) == encoded(read_market_payload(payload, REGISTRY))
    assert out['view']['cohort_denominator'] == 8 and out['view']['prior_temperature']['returned_pool_rows'] == 13
    assert out['view']['timeliness'] == 'RECEIVED_BEFORE_CONTINUOUS_OPEN'
    root = json.loads(store.read_source(SourceRef.from_dict(saved['reference']))['data'])
    assert 'items' not in encoded(root).decode() and len(root['queries']) == 4
    for reference in root['queries'].values():
        restored = restore_market_query(reference, REGISTRY, fresh())
        assert restored['execution_diagnostics'] == 'not-retained'
        assert all(p['attempts'] == [] for p in restored['result']['capture']['pages'])
    first_head = store.backend.head()
    repeated = retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    assert repeated['reference'] == saved['reference'] and store.backend.head() == first_head


def test_projection_and_legacy_excerpt_remain_exact_and_unknown_after_recovery():
    payload = auction_payload()
    for slot in ('pool_result', 'auction_result'):
        for page in payload[slot]['capture']['pages']:
            page['table'].pop('coverage_evidence')
            page['transport'].pop('request_clock')
            page['attempts'] = []
    projection = market_reading_projection(payload, REGISTRY)
    store, fresh, saved = saved_fixture(projection)
    out = read_stored_market(saved['reference'], REGISTRY, fresh())['view']
    assert out['coverage']['pool']['status'] == 'unknown' and out['timeliness'] == 'NOT_ESTABLISHED'
    assert out['matched'] == 2 and out['coverage_gaps']
    assert out == read_market_payload(payload, REGISTRY)


@pytest.mark.parametrize('stamp,legacy,expected', [
    (STAMP, False, 'REQUESTED_BEFORE_SESSION_COMPLETION'),
    ('2026-10-20T07:29:59+00:00', False, 'REQUESTED_BEFORE_SESSION_COMPLETION'),
    (CLOSE_STAMP, False, 'RECEIVED_AFTER_SESSION_COMPLETION'),
    (CLOSE_STAMP, True, 'NOT_ESTABLISHED')])
def test_raw_daily_value_and_completed_close_clock_remain_distinct_after_new_reader(stamp, legacy, expected):
    from decision_kernel.application.market_session_inputs import session_quotes
    payload = auction_payload(close_stamp=stamp)
    before = read_market_payload(auction_payload(close=False), REGISTRY)
    if legacy:
        # A later captured_at and even the original in-memory attempt do not
        # backfill a missing finite projection when the old excerpt is read.
        for page in payload['close_result']['capture']['pages']:
            page['transport'].pop('request_clock')
    ordinary = session_quotes(payload['close_result'], REGISTRY, 'daily', '20261020')
    assert ordinary['entries']['600001.SH']['value'] == '104'
    store, fresh, saved = saved_fixture(payload)
    out = read_stored_market(saved['reference'], REGISTRY, fresh())['view']
    assert out['same_session']['completion_clock'] == expected
    assert out['cohort_version'] == before['cohort_version'] and out['matched'] == 2
    assert out['same_session']['close_readable'] == 6
    assert out['groups']['MATCHED']['denominator'] == 2
    assert out['groups']['MATCHED']['close_readable'] == 2
    qualified = expected == 'RECEIVED_AFTER_SESSION_COMPLETION'
    assert out['groups']['MATCHED']['close_available'] == (2 if qualified else 0)
    assert out['same_session']['comparable'] == (3 if qualified else 0)
    row = next(r for r in out['cohort_rows'] if r['entity_id'] == '600001.SH')
    assert row['readable_close'] == '104'
    assert row['same_session_close'] == ('104' if qualified else None)
    assert encoded(out) == encoded(read_market_payload(payload, REGISTRY))
    assert out['same_session']['completed_session_cutoff'] == '2026-10-20T15:30:00+08:00'


def test_daily_reader_uses_saved_clock_without_promoting_execution_diagnostics():
    payload = auction_payload()
    page = payload['close_result']['capture']['pages'][0]
    # The original execution owner binds its attempts, but the source reader
    # must not reconstruct a later clock from editable/discarded diagnostics.
    page['transport']['request_clock']['requested_at'] = '2026-10-20T07:29:59+00:00'
    page['attempts'].append({'private': 'NOT_FOR_SOURCE_OR_CLOCK_QUALIFICATION'})
    out = read_market_payload(payload, REGISTRY)
    assert out['queries']['close'] == {'status': 'checked', 'reason_code': None}
    assert out['same_session']['completion_clock'] == 'REQUESTED_BEFORE_SESSION_COMPLETION'
    assert out['same_session']['comparable'] == 0 and out['cohort_denominator'] == 8
    assert 'NOT_FOR_SOURCE_OR_CLOCK_QUALIFICATION' not in encoded(out).decode()


@pytest.mark.parametrize('api', ['stk_auction', 'daily'])
def test_finite_clock_projection_uses_original_checkpoint_without_reset_or_transport_change(api):
    from decision_kernel.application.relay_capture_checkpoint import validate_capture_checkpoint
    from decision_kernel.application.research_sources import validate_plan, capture_registered
    from decision_kernel.infrastructure.relay_retry import RetryingRelay
    from test_relay_capture_checkpoint import run
    fields = ['ts_code', 'trade_date', 'price', 'pre_close'] if api == 'stk_auction' else ['ts_code', 'trade_date', 'close']
    values = ['600001.SH', '20261020', '104', '100'] if api == 'stk_auction' else ['600001.SH', '20261020', '104']
    parameters = {'trade_date': '20261020', 'fields': ','.join(fields), 'limit': 100}
    if api == 'stk_auction':
        parameters['ts_type'] = 'STK'
    request = {'api': api, 'stage': 'baseline', 'purpose': '合成原checkpoint接续', 'parameters': parameters}
    use = validate_plan({'schema': 'research-source-plan-v1', 'case_id': 'synthetic-checkpoint',
                         'requests': [request]}, REGISTRY)['requests'][0]
    body = {'code': 0, 'api_name': api, 'data': {'fields': fields, 'items': [values]}, 'count': 1}
    capture, writes, client, opener, clock = run(body, use=use)
    checked = validate_capture_checkpoint(writes[-1], use, REGISTRY, max_pages=4)
    assert checked['control']['state'] == 'complete'
    page = checked['capture']['pages'][0]
    assert page['transport']['request_clock']['received_at'] == page['attempts'][-1]['received_at']
    before = len(opener.calls)
    client._credential = ''
    resumed = capture_registered(use, REGISTRY, RetryingRelay(client, clock=clock.now),
                                 max_pages=4, checkpoint=checked)
    assert resumed == capture and len(opener.calls) == before
    bad = deepcopy(checked)
    bad['capture']['pages'][0]['transport']['request_clock']['requested_at'] = '2026-10-15T01:00:00+00:00'
    with pytest.raises(RecordError, match='SOURCE_REQUEST_CLOCK_INVALID'):
        validate_capture_checkpoint(bad, use, REGISTRY, max_pages=4)


@pytest.mark.parametrize('slot', ['pool_result', 'auction_result', 'close_result'])
def test_missing_exact_dependency_preserves_other_rows_and_original_denominator(slot):
    store, fresh, saved = saved_fixture()
    root = json.loads(store.read_source(SourceRef.from_dict(saved['reference']))['data'])
    key = root['reading'][slot]['query']
    query_ref = SourceRef.from_dict(root['queries'][key])
    store.backend.shared['missing'].add((query_ref.manifest.commit, query_ref.manifest.path))
    out = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert out['status'] == 'partial-restoration' and out['unavailable_queries'] == 1
    view = out['view']
    if slot == 'pool_result':
        assert view['cohort_denominator'] is None and view['groups']['MATCHED']['denominator'] is None
        assert view['pool_rows'] is None
    else:
        assert view['cohort_denominator'] == 8 and view['prior_temperature']['returned_pool_rows'] == 13
        if slot == 'auction_result':
            assert view['auction_comparable'] == 0 and view['groups']['AUCTION_UNRESOLVED']['denominator'] == 4
        else:
            assert view['matched'] == 2 and view['same_session']['comparable'] == 0


def test_revision_and_selected_bad_page_never_substitute_an_older_good_root():
    store, fresh, first = saved_fixture()
    payload = auction_payload()
    payload['auction_result']['capture']['pages'][0]['table']['items'][0][2] = '110'
    second = retain_market_reading(payload, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    one = read_stored_market(first['reference'], REGISTRY, fresh())['view']
    two = read_stored_market(second['reference'], REGISTRY, fresh())['view']
    assert one['matched'] == 2 and two['matched'] == 1 and one['cohort_version'] != two['cohort_version']
    root = json.loads(store.read_source(SourceRef.from_dict(second['reference']))['data'])
    query = root['queries'][root['reading']['auction_result']['query']]
    page = restore_market_query(query, REGISTRY, fresh())['page_references'][0]
    ref = SourceRef.from_dict(page)
    store.backend.shared['missing'].add((ref.manifest.commit, ref.manifest.path))
    failed = read_stored_market(second['reference'], REGISTRY, fresh())
    assert failed['status'] == 'partial-restoration' and failed['view']['auction_comparable'] == 0
    assert read_stored_market(first['reference'], REGISTRY, fresh())['view']['matched'] == 2
    with pytest.raises(RecordError, match='ROOM_MARKET_ASHARE_ROOT_REQUIRED'):
        read_market_reference(first['reference'], REGISTRY, fresh(), ashare_only=True)


def test_native_git_and_existing_cli_recover_after_capture_is_removed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    path = outside / 'synthetic.git'
    store = VersionedStore(GitStore.init_synthetic(path))
    payload = auction_payload()
    capture_file = outside / 'capture.json'
    capture_file.write_bytes(encoded(payload))
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    command = [sys.executable, '-m', 'decision_kernel.interfaces.market_custody_cli']
    saved = subprocess.run([*command, 'save', '--path', str(path), '--registry',
        str(ROOT / 'docs/sources/relay-registry.json'), '--input', str(capture_file), '--custody-authorized'],
        cwd=outside, env=env, capture_output=True, text=True, check=True)
    ref_file = outside / 'root-ref.json'
    ref_file.write_bytes(encoded(json.loads(saved.stdout)['reference']))
    capture_file.unlink()
    before = store.backend.head()
    outputs = []
    for _ in range(2):
        read = subprocess.run([*command, 'read', '--path', str(path), '--registry',
            str(ROOT / 'docs/sources/relay-registry.json'), '--input', str(ref_file), '--entity-id', '600001.SH', '--limit', '1'],
            cwd=outside, env=env, capture_output=True, text=True, check=True)
        outputs.append(json.loads(read.stdout))
    assert outputs[0] == outputs[1] and store.backend.head() == before
    view = outputs[0]['view']
    assert view['cohort_denominator'] == 8 and len(view['entries']) == 1 and len(view['pool_rows']) == 13
