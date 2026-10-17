"""Actual synthetic captures, gap-only joins, native host and source-free restoration."""
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from decision_kernel.application.market_factor_check import check_factor_sources
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_security_inputs import FACTOR_FIELDS, reconcile_adj_factors
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_history_continuity import HistoryOpener, inputs, entry, CODES, DAYS, START
from test_market_session_coverage import SessionOpener, capture, host_data, NOW, END
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_custody import MemoryBackend


class FactorOpener(HistoryOpener):
    def __init__(self):
        super().__init__()
        self.points = {(c, r['trade_date']): [deepcopy(r)] for c in CODES for r in self.history['adj_factor', c]}
    def open(self, request, timeout):
        u = urlsplit(request.full_url); api = u.path.rsplit('/', 1)[-1]
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        if api == 'adj_factor' and 'trade_date' in p:
            self.rows[api, None] = self.points.get((p['ts_code'], p['trade_date']), [])
            return SessionOpener.open(self, request, timeout)
        return super().open(request, timeout)


def point(op, code, day):
    return capture(op, 'adj_factor', {'ts_code': code, 'trade_date': day}, FACTOR_FIELDS)


def gap_input(indices=(-8, -1)):
    op = FactorOpener(); gaps = [DAYS[n] for n in indices]
    op.history['adj_factor', CODES[0]] = [r for r in op.history['adj_factor', CODES[0]] if r['trade_date'] not in gaps]
    data = inputs(op)
    data['factor_supplements'] = {CODES[0]: {d: point(op, CODES[0], d) for d in gaps}}
    return op, data, gaps


def run(op=None, **extra):
    op = op or FactorOpener(); receipts, waits = [], []
    out = check_factor_sources(**{'registry': REGISTRY, 'exchange': 'CN', 'start': START, 'end': END,
        'stock_codes': CODES, 'access_confirmed': True, 'request_context_confirmed': True, 'now': NOW,
        'client': RelayHTTP(opener=op, credential='synthetic-key-only'), 'progress': receipts.append,
        'sleep': waits.append, 'clock': lambda: NOW.isoformat(), **extra})
    return out, op, receipts, waits


def test_real_point_observations_restore_original_qfq_formula_without_rewriting_capture():
    _, data, gaps = gap_input(); original = encoded(data)
    before = read_market_payload({k: v for k, v in data.items() if k != 'factor_supplements'}, REGISTRY)
    after = read_market_payload(data, REGISTRY)
    assert entry(before, 'qfq')['horizons'][-1]['status'] != 'observed'
    assert all(h['status'] == 'observed' for h in entry(after, 'qfq')['horizons'])
    assert Decimal(entry(after, 'qfq')['horizons'][-1]['change_pct']) == 0
    assert entry(after)['horizons'] == entry(before)['horizons']
    audit = entry(after)['qualification']['factor']['reconciliation']
    assert len(audit['baseline_missing_sessions']) == 2 and audit['remaining_missing_sessions'] == []
    assert len(audit['supplemented_sessions']) == 2 and audit['original_capture_rewritten'] is False
    assert set(entry(after, 'qfq')['factor_lineage']['supplements']) == set(gaps)
    assert after['qfq']['source_version'] != before['qfq']['source_version']
    assert encoded(data) == original


@pytest.mark.parametrize('bad', ['empty', 'duplicate', 'wrong-code', 'wrong-day', 'incomplete', 'zero', 'negative', 'nan', 'bool', 'null'])
def test_bad_point_never_fills_gap_or_erases_independent_valid_point(bad):
    op, data, gaps = gap_input(); d = gaps[0]
    row = deepcopy(op.points[CODES[0], d][0])
    if bad == 'empty': op.points[CODES[0], d] = []
    elif bad == 'duplicate': op.points[CODES[0], d] = [row, row]
    elif bad == 'wrong-code': op.points[CODES[0], d] = [{**row, 'ts_code': CODES[1]}]
    elif bad == 'wrong-day': op.points[CODES[0], d] = [{**row, 'trade_date': DAYS[-7]}]
    elif bad == 'incomplete': op.fail = 'adj_factor'
    else: op.points[CODES[0], d] = [{**row, 'adj_factor': {'zero': 0, 'negative': -1, 'nan': 'NaN', 'bool': True, 'null': None}[bad]}]
    data['factor_supplements'][CODES[0]][d] = point(op, CODES[0], d)
    e = entry(read_market_payload(data, REGISTRY))
    audit = e['qualification']['factor']['reconciliation']
    assert len(audit['remaining_missing_sessions']) == 1 and len(audit['supplemented_sessions']) == 1
    assert audit['points'][d]['status'] == ('checked-empty' if bad == 'empty' else 'rejected')
    assert all(h['status'] == 'observed' for h in e['horizons'])
    assert e['qualification']['adjustment']['status'] == 'factor-gaps'


def test_overlapping_point_cannot_replace_even_equal_original_factor():
    op, data, _ = gap_input(indices=()); d = DAYS[-1]
    data['factor_supplements'][CODES[0]][d] = point(op, CODES[0], d)
    e = entry(read_market_payload(data, REGISTRY), 'qfq')
    audit = e['qualification']['factor']['reconciliation']
    assert audit['points'][d]['reason_code'] == 'MARKET_RELAY_FACTOR_SUPPLEMENT_NOT_BASELINE_GAP'
    assert audit['supplemented_sessions'] == [] and audit['remaining_missing_sessions'] == []
    assert Decimal(e['horizons'][-1]['change_pct']) == 0


@pytest.mark.parametrize('bad', ['query', 'baseline-incomplete', 'baseline-missing', 'bad-map'])
def test_no_point_can_substitute_for_unavailable_or_invalid_baseline(bad):
    op, data, gaps = gap_input()
    if bad == 'query': data['factor_results'][CODES[0]] = point(op, CODES[0], gaps[0])
    elif bad == 'baseline-missing': data['factor_results'] = {}
    elif bad == 'bad-map': data['factor_supplements'][CODES[0]] = []
    else:
        op.fail = 'adj_factor'
        data['factor_results'][CODES[0]] = capture(op, 'adj_factor',
            {'ts_code': CODES[0], 'start_date': START, 'end_date': END}, FACTOR_FIELDS)
    if bad == 'baseline-missing':
        with pytest.raises(ValueError, match='FACTOR_SUPPLEMENT_SCOPE'): read_market_payload(data, REGISTRY)
    else:
        e = entry(read_market_payload(data, REGISTRY), 'qfq')
        assert e['qualification']['factor']['status'] == 'rejected'
        assert e['qualification']['adjustment']['status'] == 'inputs-unavailable'
        assert e['factor_lineage'] is None


def test_normal_three_surface_and_paginated_readers_keep_original_denominator():
    _, data, _ = gap_input(); all_view = read_market_payload(data, REGISTRY)
    page = read_market_payload(data, REGISTRY, limit=1, entity_id=CODES[0])
    assert all_view['qfq']['source_version'] == page['qfq']['source_version'] and page['scope_count'] == 2
    three = {'schema': 'relay-three-market-reading-v1', 'surfaces': {'stock': {k: v for k, v in data.items() if k != 'schema'}}}
    assert read_market_payload(three, REGISTRY)['surfaces']['stock']['status'] == 'read'


def test_host_records_all_generated_point_requests_before_point_io_and_no_values():
    op = FactorOpener(); gaps = [DAYS[-8], DAYS[-1]]
    op.history['adj_factor', CODES[0]] = [r for r in op.history['adj_factor', CODES[0]] if r['trade_date'] not in gaps]
    receipts = []
    def preflight(api, p):
        if 'trade_date' in p:
            planned = [v['request_context']['parameters']['trade_date'] for v in receipts[-1]['queries'].values()
                       if 'trade_date' in v['request_context']['parameters']]
            assert set(planned) == set(gaps)
    op.before = preflight
    out, _, _, _ = run(op, progress=receipts.append)
    assert out['status'] == 'checked-factor-audit' and out['original_batch_revalidated'] is False
    assert out['surfaces']['factor_1']['baseline_gap_count'] == 2
    assert out['surfaces']['factor_1']['point_observed_count'] == 2
    assert all(s['combined_calendar_covered'] for s in out['surfaces'].values())
    assert len(op.calls) == 6 and {a for a, p in op.calls} == {'trade_cal', 'adj_factor'}
    raw = json.dumps(out)
    assert all(v not in raw for v in ['PRIVATE_SOURCE_NAME', 'anchor_factor', 'close', 'source_rows', 'view_sha256', 'change_pct'])


def test_complete_interval_never_triggers_extra_point_requests():
    out, op, _, _ = run()
    assert out['status'] == 'checked-factor-audit' and len(op.calls) == 4
    assert all('trade_date' not in p for a, p in op.calls)


def test_budget_exhaustion_preserves_all_missing_counts_without_truncated_success():
    op = FactorOpener(); op.history['adj_factor', CODES[0]] = op.history['adj_factor', CODES[0]][17:]
    out, op, _, _ = run(op)
    assert out['point_budget_exceeded'] and out['status'] == 'completed-with-gaps'
    assert out['surfaces']['factor_1']['remaining_gap_count'] == 17
    assert len(op.calls) == 4 and not any('trade_date' in p for a, p in op.calls)
    assert out['surfaces']['factor_2']['combined_calendar_covered']


def test_calendar_failure_only_blocks_its_venue_and_never_counts_unknown_as_zero():
    op = FactorOpener(); op.fail = ('trade_cal', 'SSE')
    out, _, _, _ = run(op)
    assert out['surfaces']['factor_1']['baseline_gap_count'] is None
    assert out['surfaces']['factor_1']['calendar_session_count'] is None
    assert out['surfaces']['factor_2']['combined_calendar_covered']
    assert out['queries']['factor_1']['http_attempts'] == 0


def test_point_timeout_respects_long_retry_after_and_leaves_original_gap():
    op = FactorOpener(); op.history['adj_factor', CODES[0]].pop(-1)
    def deny(api, p):
        if 'trade_date' in p:
            raise HTTPError('https://synthetic.invalid', 504, 'PRIVATE_FAILURE', {'Retry-After': '7200'},
                            BytesIO(b'{"error":"upstream_timeout"}'))
    op.before = deny
    out, _, _, waits = run(op)
    item = out['queries']['factor_1_point_' + DAYS[-1]]
    assert item['http_attempts'] == 1 and item['retry_context']['retry_after_seconds'] == 7200
    assert not waits and out['surfaces']['factor_1']['remaining_gap_count'] == 1
    assert out['surfaces']['factor_2']['combined_calendar_covered']


def test_global_auth_refusal_stops_further_io_without_replacing_target():
    op = FactorOpener(); op.fail = 'denied'
    out, _, _, _ = run(op)
    assert out['stopped'] and out['http_attempts'] == 1 and out['cohort_count'] == 2
    assert all(s['baseline_gap_count'] is None for s in out['surfaces'].values())


@pytest.mark.parametrize('bad', [{'stock_codes': '[]'}, {'stock_codes': '[true]'}, {'stock_codes': '["600000.SH","600000.SH"]'},
    {'stock_codes': '["600000.SH","600001.SH","600002.SH","600003.SH","600004.SH"]'},
    {'request_context_confirmed': 'false'}, {'explain_quotes': 'true'}, {'member_code': '885800.TI'}, {'resume': '{}'}, {'exchange': 'SSE'}])
def test_host_rejects_unapproved_or_ambiguous_diagnostic_inputs(tmp_path, bad):
    event, env = host_data(tmp_path)
    event['inputs'].update(selection='factor-audit', start=START, stock_codes=json.dumps(CODES), explain_quotes='false')
    event['inputs'].update(bad)
    with pytest.raises(ValueError): settings(event, env, NOW)


def test_original_host_routes_factor_audit_without_mocking_success(monkeypatch, tmp_path):
    event, env = host_data(tmp_path)
    event['inputs'].update(selection='factor-audit', start=START, stock_codes=json.dumps(CODES), explain_quotes='false')
    op = FactorOpener(); client = RelayHTTP(opener=op, credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.market_factor_check.RelayHTTP', lambda: client)
    assert execute(event, env, tmp_path/'out', now=NOW, registry_path=REGISTRY_PATH) == 0
    report = json.loads((tmp_path/'out/result.json').read_bytes())
    assert report['status'] == 'checked-factor-audit' and len(op.calls) == 4


def test_original_custody_is_idempotent_and_lost_point_restores_only_its_gap():
    _, data, _ = gap_input(); backend = MemoryBackend(); store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    with pytest.raises(RecordError, match='PUBLIC_RIGHTS'):
        retain_market_reading(data, REGISTRY, store, fresh_reader=fresh)
    saved = retain_market_reading(data, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    again = retain_market_reading(data, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    assert saved['reference'] == again['reference']
    assert saved['query_count'] == 7  # calendar, master, price, base factor, suspension, two points
    point_ref = next(s['reference'] for s in saved['restored']['sources'] if s['path'][0] == 'factor_supplements')
    backend.shared['missing'].add((point_ref['manifest']['commit'], point_ref['manifest']['path']))
    restored = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert restored['status'] == 'partial-restoration' and restored['source_requests'] == 0
    assert len(entry(restored['view'])['qualification']['adjustment']['missing_factor_sessions']) == 1
    assert all(h['status'] == 'observed' for h in entry(restored['view'])['horizons'])


def test_fresh_process_reads_actual_retained_point_captures_after_original_input_deletion(tmp_path):
    _, data, _ = gap_input(); source = tmp_path/'input.json'; source.write_bytes(encoded(data))
    env = {k: v for k, v in os.environ.items() if k not in ('TUSHARE_PROXY_API_KEY','GH_TOKEN','GITHUB_TOKEN','DECISION_OS_STORE_TOKEN')}
    env['PYTHONPATH'] = str(ROOT_DIR/'src')
    def cli(name, *args):
        out = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.' + name, *args],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    reg = ['--registry', str(REGISTRY_PATH)]
    direct = cli('market_relay_cli', *reg, '--input', str(source), '--entity-id', CODES[0])
    path = tmp_path/'synthetic.git'; GitStore.init_synthetic(path)
    saved = cli('market_custody_cli', 'save', *reg, '--path', str(path), '--input', str(source), '--public-rights-confirmed')
    root = tmp_path/'root.json'; root.write_bytes(encoded(saved['reference'])); source.unlink()
    restored = cli('market_custody_cli', 'read', *reg, '--path', str(path), '--input', str(root), '--entity-id', CODES[0])
    assert restored['view'] == direct and restored['source_requests'] == 0
