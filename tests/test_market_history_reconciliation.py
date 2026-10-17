"""Same captured history reaches ordinary reading and existing source custody."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError

import pytest

from decision_kernel.application.market_history_check import _collect_stock_history_sources
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_factor_reconciliation import FactorOpener
from test_market_history_continuity import CODES, DAYS, START, entry, run as original_run
from test_market_session_coverage import host_data, NOW, END
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_custody import MemoryBackend


def with_gaps(days=None):
    op = FactorOpener()
    days = [DAYS[-8], DAYS[-1]] if days is None else days
    op.history['adj_factor', CODES[0]] = [r for r in op.history['adj_factor', CODES[0]]
                                       if r['trade_date'] not in days]
    return op


def collect(op=None, **extra):
    op = op or with_gaps(); receipts, waits = [], []
    report, payloads = _collect_stock_history_sources(**{'registry': REGISTRY, 'exchange': 'CN',
        'start': START, 'end': END, 'stock_codes': CODES, 'selection': 'stock-history-reconcile',
        'access_confirmed': True, 'request_context_confirmed': True, 'now': NOW,
        'client': RelayHTTP(opener=op, credential='synthetic-key-only'), 'progress': receipts.append,
        'sleep': waits.append, 'clock': lambda: NOW.isoformat(), **extra})
    return report, payloads, op, receipts, waits


def test_same_captured_history_is_consumed_without_repeating_baseline_or_rewriting_prices():
    report, payloads, op, receipts, _ = collect()
    data = payloads['SSE']; original = encoded(data)
    before = read_market_payload({k: v for k, v in data.items() if k != 'factor_supplements'}, REGISTRY)
    after = read_market_payload(data, REGISTRY)
    assert report['status'] == 'checked-stock-history-reconcile' and len(op.calls) == 12
    assert report['original_batch_revalidated'] is False
    sample = report['surfaces']['stock_1']
    assert sample['baseline']['factor_gap_count'] == 2 and not sample['baseline']['cohort_history_qualified']
    assert sample['factor_reconciliation'] == {'baseline_gap_count': 2, 'supplemented_count': 2,
        'remaining_gap_count': 0, 'original_capture_rewritten': False}
    assert all(s['cohort_history_qualified'] and s['raw_horizons_unchanged'] for s in report['surfaces'].values())
    assert entry(before)['horizons'] == entry(after)['horizons']
    assert Decimal(entry(after)['horizons'][-1]['change_pct']) == -50
    assert Decimal(entry(after, 'qfq')['horizons'][-1]['change_pct']) == 0
    assert after['scope_count'] == before['scope_count'] == 2 and encoded(data) == original
    assert len({q['request_context']['query_key'] for q in report['queries'].values()}) == 12
    for receipt in receipts:
        raw = json.dumps(receipt)
        assert all(s not in raw for s in ('PRIVATE_SOURCE_NAME', 'synthetic-key-only', 'change_pct',
            'anchor_factor', 'source_rows', 'view_sha256', 'transport_sha256', 'payloads', '"capture":'))


def test_all_point_requests_are_declared_before_point_io_and_completed_intervals_do_not_repeat():
    op = with_gaps(); receipts = []
    def preflight(api, params):
        points = [v for v in receipts[-1]['queries'].values()
                  if 'trade_date' in v['request_context']['parameters']]
        assert len(receipts[0]['queries']) == 10
        if 'trade_date' in params:
            assert {q['request_context']['parameters']['trade_date'] for q in points} == {DAYS[-8], DAYS[-1]}
    op.before = preflight
    collect(op, progress=receipts.append)
    out, _, op, _, _ = collect(with_gaps([]))
    assert out['status'] == 'checked-stock-history-reconcile' and len(op.calls) == 10
    assert not any('trade_date' in p for _, p in op.calls)
    old, op, _, _ = original_run(with_gaps())
    assert old['status'] == 'completed-with-gaps' and len(op.calls) == 10
    assert 'point_budget' not in old and 'baseline' not in old['surfaces']['stock_1']


@pytest.mark.parametrize('end,gaps,budget,exceeded,queries', [
    (END, 16, 16, False, 32), (END, 17, 16, True, 16),
    ('20261015', 14, 14, False, 32), ('20261015', 15, 14, True, 18)])
def test_combined_budget_includes_freshness_and_never_truncates_gap_plan(end, gaps, budget, exceeded, queries):
    op = FactorOpener(); codes = [CODES[0], '600001.SH', CODES[1], '300001.SZ']
    for code, source in zip(codes, [CODES[0], CODES[0], CODES[1], CODES[1]]):
        for api in ('daily', 'adj_factor', 'suspend_d'):
            op.history[api, code] = [{**r, 'ts_code': code} for r in op.history[api, source] if r['trade_date'] <= end]
        for r in op.history['adj_factor', code]:
            op.points[code, r['trade_date']] = [deepcopy(r)]
    op.history['adj_factor', CODES[0]] = op.history['adj_factor', CODES[0]][gaps:]
    out, _, op, _, _ = collect(op, stock_codes=codes, end=end)
    assert out['point_budget'] == budget and out['point_budget_exceeded'] is exceeded
    assert len(out['queries']) == len(op.calls) == queries
    sample = out['surfaces']['stock_1']
    assert sample['factor_reconciliation']['baseline_gap_count'] == gaps
    assert sample['factor_reconciliation']['remaining_gap_count'] == (gaps if exceeded else 0)
    assert sum('trade_date' in p for _, p in op.calls) == (0 if exceeded else gaps)
    assert (out['status'] == 'checked-stock-history-reconcile') is (end == END and not exceeded)
    if end != END:
        assert sample['session_lag'] == 1 and not sample['cohort_history_qualified']


@pytest.mark.parametrize('fault', ['empty', 'wrong-code', 'incomplete'])
def test_one_point_failure_keeps_original_gap_and_independent_valid_point(fault):
    op = with_gaps(); day = DAYS[-8]
    if fault == 'empty':
        op.points[CODES[0], day] = []
    elif fault == 'wrong-code':
        op.points[CODES[0], day][0]['ts_code'] = CODES[1]
    else:
        def fail(api, p):
            if p.get('trade_date') == day:
                raise URLError(TimeoutError('PRIVATE_FAILURE'))
        op.before = fail
    out, payloads, _, _, waits = collect(op)
    assert out['status'] == 'completed-with-gaps'
    assert out['surfaces']['stock_2']['cohort_history_qualified']
    assert out['surfaces']['stock_1']['factor_reconciliation']['supplemented_count'] == 1
    assert out['surfaces']['stock_1']['factor_reconciliation']['remaining_gap_count'] == 1
    assert set(payloads['SSE']['factor_supplements'][CODES[0]]) == {day, DAYS[-1]}
    assert all(h['status'] == 'observed' for h in entry(read_market_payload(payloads['SSE'], REGISTRY))['horizons'])
    assert waits == ([30, 30, 30] if fault == 'incomplete' else [])


@pytest.mark.parametrize('fault', ['missing-price', 'invalid-price', 'suspension-conflict', 'bad-interval', 'master-quarantine', 'unknown-freshness'])
def test_factors_cannot_qualify_unavailable_or_conflicting_history(fault):
    op = with_gaps(); end = END
    if fault == 'missing-price': op.history['daily', CODES[0]].pop(-8)
    elif fault == 'invalid-price': op.history['daily', CODES[0]][-8]['close'] = 'not-a-price'
    elif fault == 'suspension-conflict':
        op.history['suspend_d', CODES[0]] = [dict(ts_code=CODES[0], trade_date=DAYS[-8], suspend_type='S', suspend_timing=None)]
    elif fault == 'bad-interval': op.history['adj_factor', CODES[0]][0]['adj_factor'] = None
    elif fault == 'master-quarantine': op.rows['stock_basic', 'SSE'][0]['delist_date'] = '20261020'
    else:
        end = '20261015'
        for key, rows in op.history.items(): op.history[key] = [r for r in rows if r['trade_date'] <= end]
        def fail(api, p):
            if api == 'trade_cal' and p['start_date'] == end:
                raise URLError(TimeoutError('PRIVATE_FAILURE'))
        op.before = fail
    out, _, op, _, _ = collect(op, end=end)
    assert out['status'] == 'completed-with-gaps' and not out['surfaces']['stock_1']['cohort_history_qualified']
    audit = out['surfaces']['stock_1']['factor_reconciliation']
    if fault in ('bad-interval', 'master-quarantine'):
        assert audit['baseline_gap_count'] is None
        assert not any('trade_date' in p for _, p in op.calls)
    else:
        assert audit['remaining_gap_count'] == 0
    if fault == 'unknown-freshness': assert out['surfaces']['stock_1']['session_lag'] is None
    else: assert out['surfaces']['stock_2']['cohort_history_qualified']


@pytest.mark.parametrize('status', [403, 429])
def test_point_phase_global_refusal_stops_subsequent_points(status):
    op = with_gaps()
    def deny(api, p):
        if 'trade_date' in p:
            raise HTTPError('https://synthetic.invalid', status, 'PRIVATE_REFUSAL', {}, BytesIO(b'private'))
    op.before = deny
    out, _, op, _, waits = collect(op)
    points = [q for q in out['queries'].values() if 'trade_date' in q['request_context']['parameters']]
    assert out['stopped'] and out['http_attempts'] == len(op.calls) == 11 and not waits
    assert points[1]['reason_code'] == 'GLOBAL_SOURCE_STOP' and points[1]['http_attempts'] == 0
    assert out['surfaces']['stock_1']['factor_reconciliation']['remaining_gap_count'] == 2
    assert 'PRIVATE_REFUSAL' not in json.dumps(out)


def test_point_phase_shares_baseline_deadline_and_does_not_shorten_retry_after():
    op = with_gaps(); now = [0.0]
    def consume_budget(api, p):
        if api == 'suspend_d' and p['ts_code'] == CODES[1]: now[0] = 601.0
    op.before = consume_budget
    out, _, op, _, waits = collect(op, monotonic=lambda: now[0])
    assert out['stopped'] and len(op.calls) == 10 and not waits
    assert not any('trade_date' in p for _, p in op.calls)
    assert out['surfaces']['stock_1']['factor_reconciliation']['remaining_gap_count'] == 2
    op = with_gaps()
    def defer(api, p):
        if p.get('trade_date') == DAYS[-8]:
            raise HTTPError('https://synthetic.invalid', 504, 'PRIVATE_FAILURE', {'Retry-After': '7200'},
                            BytesIO(b'{"error":"upstream_timeout"}'))
    op.before = defer
    out, _, _, _, waits = collect(op)
    item = out['queries']['stock_1_factor_point_' + DAYS[-8]]
    assert item['http_attempts'] == 1 and item['retry_context']['retry_after_seconds'] == 7200 and not waits
    assert out['surfaces']['stock_1']['factor_reconciliation']['supplemented_count'] == 1


def test_original_host_routes_new_selection_and_never_serializes_collected_payloads(monkeypatch, tmp_path):
    event, env = host_data(tmp_path)
    event['inputs'].update(selection='stock-history-reconcile', start=START, stock_codes=json.dumps(CODES), explain_quotes='true')
    op = with_gaps(); client = RelayHTTP(opener=op, credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.market_history_check.RelayHTTP', lambda: client)
    assert execute(event, env, tmp_path/'out', now=NOW, registry_path=REGISTRY_PATH) == 0
    raw = (tmp_path/'out/result.json').read_text(); out = json.loads(raw)
    assert out['status'] == 'checked-stock-history-reconcile' and len(op.calls) == 12
    assert set(p.name for p in (tmp_path/'out').iterdir()) == {'result.json'}
    assert all(s not in raw for s in ('PRIVATE_SOURCE_NAME', 'synthetic-key-only', 'change_pct', 'payloads', 'source_rows'))
    for bad in ({'explain_quotes': 'false'}, {'request_context_confirmed': 'false'}, {'exchange': 'SSE'},
                {'resume': '{}'}, {'member_code': '885800.TI'},
                {'stock_codes': json.dumps(['600000.SH', '600001.SH', '600002.SH', '600003.SH', '600004.SH'])}):
        invalid = deepcopy(event); invalid['inputs'].update(bad)
        with pytest.raises(ValueError): settings(invalid, env, NOW)
    assert len(op.calls) == 12


def test_collector_payload_uses_original_custody_permissions_and_missing_point_degradation():
    _, payloads, _, _, _ = collect(); data = payloads['SSE']
    backend = MemoryBackend(); store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    with pytest.raises(RecordError, match='PUBLIC_RIGHTS'):
        retain_market_reading(data, REGISTRY, store, fresh_reader=fresh)
    saved = retain_market_reading(data, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    assert saved['query_count'] == 7
    point_ref = next(s['reference'] for s in saved['restored']['sources'] if s['path'][0] == 'factor_supplements')
    backend.shared['missing'].add((point_ref['manifest']['commit'], point_ref['manifest']['path']))
    restored = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert restored['status'] == 'partial-restoration' and restored['source_requests'] == 0
    assert len(entry(restored['view'])['qualification']['adjustment']['missing_factor_sessions']) == 1
    assert all(h['status'] == 'observed' for h in entry(restored['view'])['horizons'])


def test_actual_collected_payload_reopens_in_fresh_process_after_input_deletion(tmp_path):
    _, payloads, _, _, _ = collect()
    env = {k: v for k, v in os.environ.items() if k not in
        ('TUSHARE_PROXY_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'DECISION_OS_STORE_TOKEN')}
    env['PYTHONPATH'] = str(ROOT_DIR/'src')
    def cli(name, *args):
        out = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.' + name, *args],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    reg = ['--registry', str(REGISTRY_PATH)]
    path = tmp_path/'synthetic.git'; GitStore.init_synthetic(path)
    for venue, code in zip(('SSE', 'SZSE'), CODES):
        source = tmp_path/(venue + '.json'); source.write_bytes(encoded(payloads.pop(venue)))
        direct = cli('market_relay_cli', *reg, '--input', str(source), '--entity-id', code)
        saved = cli('market_custody_cli', 'save', *reg, '--path', str(path), '--input', str(source), '--public-rights-confirmed')
        root = tmp_path/(venue + '-root.json'); root.write_bytes(encoded(saved['reference'])); source.unlink()
        restored = cli('market_custody_cli', 'read', *reg, '--path', str(path), '--input', str(root), '--entity-id', code)
        assert restored['view'] == direct and restored['source_requests'] == 0
        assert all(h['status'] == 'observed' for h in direct['qfq']['entries'][0]['horizons'])
