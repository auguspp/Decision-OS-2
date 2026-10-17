"""Synthetic multi-month history through original capture, raw/qfq and custody."""
from copy import deepcopy
from datetime import date, timedelta
from dataclasses import asdict
from decimal import Decimal
import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from io import BytesIO

import pytest

from decision_kernel.application.market_history_check import check_stock_history_sources, history_codes
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.market_suspension_inputs import SUSPENSION_FIELDS
from decision_kernel.application.market_security_inputs import MASTER_FIELDS, FACTOR_FIELDS
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_session_coverage import SessionOpener, capture, host_data, NOW, END
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_custody import MemoryBackend

START = '20260701'
CODES = ['600000.SH', '000001.SZ']
DAYS = [(date(2026, 7, 1) + timedelta(days=n)).strftime('%Y%m%d') for n in range(108)
        if (date(2026, 7, 1) + timedelta(days=n)).weekday() < 5]


class HistoryOpener(SessionOpener):
    def __init__(self):
        super().__init__()
        self.history = {}
        for code in CODES:
            self.history['daily', code] = [dict(ts_code=code, trade_date=d, close='20' if n < 30 else '10')
                                          for n, d in enumerate(DAYS)]
            self.history['adj_factor', code] = [dict(ts_code=code, trade_date=d, adj_factor='1' if n < 30 else '2')
                                               for n, d in enumerate(DAYS)]
            self.history['suspend_d', code] = []
    def open(self, request, timeout):
        u = urlsplit(request.full_url); api = u.path.rsplit('/', 1)[-1]
        p = {k: v[0] for k, v in parse_qs(u.query).items()}
        if api in ('daily', 'adj_factor', 'suspend_d'):
            self.rows[api, None] = self.history[api, p['ts_code']]
        return super().open(request, timeout)


def inputs(op=None, *, include=True):
    op = op or HistoryOpener(); code = CODES[0]
    result = {'schema': 'relay-security-stock-reading-v1', 'exchange': 'SSE', 'start': START, 'end': END,
        'reference_policy': 'isolate-identified',
        'calendar_result': capture(op, 'trade_cal', {'exchange': 'SSE', 'start_date': START, 'end_date': END},
                                   {'exchange', 'cal_date', 'is_open'}),
        'master_results': {'L': capture(op, 'stock_basic', {'exchange': 'SSE', 'list_status': 'L'}, MASTER_FIELDS)},
        'stock_results': {code: capture(op, 'daily', {'ts_code': code, 'start_date': START, 'end_date': END},
                                       {'ts_code', 'trade_date', 'close'})},
        'factor_results': {code: capture(op, 'adj_factor', {'ts_code': code, 'start_date': START, 'end_date': END}, FACTOR_FIELDS)}}
    if include:
        result['suspension_results'] = {code: capture(op, 'suspend_d',
            {'ts_code': code, 'start_date': START, 'end_date': END}, SUSPENSION_FIELDS, limit=2)}
    return result


def entry(view, mode='raw'):
    return next(e for e in view[mode]['entries'] if e['entity_id'] == CODES[0])


def run(op=None, **extra):
    op = op or HistoryOpener(); receipts, waits = [], []
    out = check_stock_history_sources(**{'registry': REGISTRY, 'exchange': 'CN', 'start': START, 'end': END,
        'stock_codes': CODES, 'access_confirmed': True, 'request_context_confirmed': True, 'now': NOW,
        'client': RelayHTTP(opener=op, credential='synthetic-key-only'), 'progress': receipts.append,
        'sleep': waits.append, 'clock': lambda: NOW.isoformat(), **extra})
    return out, op, receipts, waits


def test_original_raw_qfq_windows_unchanged_and_no_unrequested_security_dropped():
    data = inputs(); before = encoded(data); new = read_market_payload(data, REGISTRY)
    old = read_market_payload({k: v for k, v in data.items() if k != 'suspension_results'}, REGISTRY)
    for mode in ('raw', 'qfq'):
        assert entry(new, mode)['horizons'] == entry(old, mode)['horizons']
        assert new[mode]['horizon_counts'] == old[mode]['horizon_counts']
        assert new[mode]['scope_count'] == 2
    assert Decimal(entry(new)['horizons'][-1]['change_pct']) == -50
    assert Decimal(entry(new, 'qfq')['horizons'][-1]['change_pct']) == 0
    assert new['history_quote_explanations']['inspected_count'] == 1
    assert new['history_quote_explanations']['not_inspected_count'] == 1
    assert encoded(data) == before


@pytest.mark.parametrize('event,timing,status', [('S', None, 'suspension-supported-missing'),
    ('S', '09:30-10:00', 'intraday-only-unexplained'), ('R', None, 'resumption-unexplained'),
    ('P', None, 'suspension-evidence-quarantined')])
def test_exact_gap_explained_but_not_filled_or_propagated(event, timing, status):
    op = HistoryOpener(); gap, next_day = DAYS[-8], DAYS[-7]
    op.history['daily', CODES[0]] = [r for r in op.history['daily', CODES[0]] if r['trade_date'] not in (gap, next_day)]
    op.history['suspend_d', CODES[0]] = [dict(ts_code=CODES[0], trade_date=gap, suspend_type=event, suspend_timing=timing)]
    view = read_market_payload(inputs(op), REGISTRY); e = entry(view)
    rows = {d['trade_date'].replace('-', ''): d for d in e['history_quote_explanation']['days']}
    assert rows[gap]['status'] == status
    assert rows[next_day]['status'] == 'no-suspension-record'  # Never carry S forward.
    assert rows[gap]['suspension']['source_rows'] == [{'page': 1, 'row': 1}]
    assert rows[gap]['suspension']['all_day_confirmed'] is False
    assert [h['status'] for h in e['horizons']] == ['observed', 'missing-market-sessions', 'missing-market-sessions']
    assert e['history_quote_explanation']['missing_quote_count'] == 2


@pytest.mark.parametrize('fault', ['missing', 'incomplete', 'wrong-code', 'wrong-day', 'filtered', 'bad-date'])
def test_unavailable_evidence_is_not_empty_or_an_excuse_for_prices(fault):
    op = HistoryOpener(); gap = DAYS[-8]
    op.history['daily', CODES[0]] = [r for r in op.history['daily', CODES[0]] if r['trade_date'] != gap]
    data = inputs(op)
    if fault == 'filtered':
        with pytest.raises(RecordError, match='SOURCE_PARAMETER_NOT_ALLOWED'):
            capture(op, 'suspend_d', {'ts_code': CODES[0], 'start_date': START,
                'end_date': END, 'suspend_type': 'S'}, SUSPENSION_FIELDS)
        return  # Original source admission is stricter than the period reader.
    if fault == 'missing': data['suspension_results'] = {}
    elif fault == 'incomplete':
        op.fail = 'suspend_d'; data['suspension_results'][CODES[0]] = capture(op, 'suspend_d',
            {'ts_code': CODES[0], 'start_date': START, 'end_date': END}, SUSPENSION_FIELDS)
    else:
        day = '20260704' if fault == 'wrong-day' else 'bad' if fault == 'bad-date' else gap
        code = CODES[1] if fault == 'wrong-code' else CODES[0]
        op.history['suspend_d', CODES[0]] = [dict(ts_code=code, trade_date=day, suspend_type='S', suspend_timing=None)]
        data['suspension_results'][CODES[0]] = capture(op, 'suspend_d',
            {'ts_code': CODES[0], 'start_date': START, 'end_date': END, **({'suspend_type': 'S'} if fault == 'filtered' else {})}, SUSPENSION_FIELDS)
    view = read_market_payload(data, REGISTRY); e = entry(view)
    assert e['history_quote_explanation']['counts']['suspension-input-unavailable'] == 1
    assert e['horizons'][0]['status'] == 'observed'
    assert e['history_quote_explanation']['lineage'] is None


def test_duplicate_day_isolated_and_original_multi_page_positions_retained():
    op = HistoryOpener(); op.history['suspend_d', CODES[0]] = [
        dict(ts_code=CODES[0], trade_date=d, suspend_type='R', suspend_timing=None) for d in DAYS[:3]]
    op.history['suspend_d', CODES[0]].append(deepcopy(op.history['suspend_d', CODES[0]][2]))
    e = entry(read_market_payload(inputs(op), REGISTRY))['history_quote_explanation']
    day = e['days'][2]
    assert day['suspension']['status'] == 'quarantined'
    assert day['suspension']['source_rows'] == [{'page': 2, 'row': 1}, {'page': 2, 'row': 2}]
    assert e['evidence_quarantined_day_count'] == 1


@pytest.mark.parametrize('day', [-1, -8])
def test_missing_factor_only_invalidates_original_adjusted_dependency(day):
    op = HistoryOpener(); op.history['adj_factor', CODES[0]].pop(day)
    view = read_market_payload(inputs(op), REGISTRY)
    assert all(h['status'] == 'observed' for h in entry(view)['horizons'])
    assert entry(view)['history_quote_explanation']['missing_quote_count'] == 0
    assert entry(view, 'qfq')['horizons'][-1]['status'] != 'observed'


def test_unknown_price_is_not_zero_missing_and_master_quarantine_cannot_be_excused():
    op = HistoryOpener(); data = inputs(op); data['stock_results'] = {}
    history = entry(read_market_payload(data, REGISTRY))['history_quote_explanation']
    assert history['missing_quote_count'] is None
    assert history['counts']['quote-input-unavailable'] == len(DAYS)
    op.rows['stock_basic', 'SSE'][0]['delist_date'] = '20261020'
    view = read_market_payload(inputs(op), REGISTRY)
    assert entry(view)['history_quote_explanation']['status'] == 'reference-quarantined'
    assert view['raw']['scope_count'] == 2


def test_pagination_and_three_surface_consumer_preserve_whole_denominator():
    data = inputs(); page = read_market_payload(data, REGISTRY, offset=1, limit=1)
    assert page['raw']['scope_count'] == 2 and page['history_quote_explanations']['inspected_count'] == 1
    payload = {'schema': 'relay-three-market-reading-v1', 'surfaces': {'stock': {k:v for k,v in data.items() if k != 'schema'}}}
    view = read_market_payload(payload, REGISTRY)
    assert view['surfaces']['stock']['status'] == 'read'


def test_live_host_composition_generates_every_request_before_first_http():
    op = HistoryOpener(); receipts = []
    op.before = lambda api, params: (len(receipts[-1]['queries']) == 10 or pytest.fail('missing preflight context'))
    out, _, _, _ = run(op, progress=receipts.append)
    assert out['status'] == 'checked-stock-history' and out['cohort_count'] == 2
    assert all(s['cohort_history_qualified'] for s in out['surfaces'].values())
    assert all(s['factor_change_count'] == 1 for s in out['surfaces'].values())
    assert all(v['scope_count'] == 2 and v['cohort_count'] == 1 for v in out['reference_scope'].values())
    assert [p['ts_code'] for a,p in op.calls if a == 'daily'] == CODES
    raw = json.dumps(out)
    assert 'PRIVATE_SOURCE_NAME' not in raw and 'change_pct' not in raw and 'anchor_factor' not in raw
    assert 'source_rows' not in raw and 'view_sha256' not in raw
    assert out['http_attempts'] == 10 and not out['supplier_data_retained']


def test_one_failed_stock_does_not_stop_peer_or_disappear_from_cohort():
    op = HistoryOpener()
    def fail(api, p):
        if api == 'daily' and p.get('ts_code') == CODES[0]: raise URLError(TimeoutError('PRIVATE_FAILURE'))
    op.before = fail
    out, _, _, waits = run(op)
    assert out['status'] == 'completed-with-gaps' and out['cohort_count'] == 2
    assert out['surfaces']['stock_2']['cohort_history_qualified']
    assert not out['surfaces']['stock_1']['cohort_history_qualified']
    assert waits == [30,30,30] and out['queries']['stock_1_daily']['retry_context']['eligible']
    assert 'PRIVATE_FAILURE' not in json.dumps(out)


def test_permission_refusal_stops_all_source_traffic_and_preserves_unknown_scope():
    op = HistoryOpener(); op.fail = 'denied'
    out, _, _, waits = run(op)
    assert out['http_attempts'] == 1 and out['stopped'] and not waits
    assert all(v['scope_count'] is None for v in out['reference_scope'].values())
    assert len(out['surfaces']) == 2


@pytest.mark.parametrize('bad', [[], CODES*2, ['830001.BJ'], ['bad'], [True], ['600000.SH']*9])
def test_bad_cohorts_rejected_without_source(bad):
    op = HistoryOpener()
    with pytest.raises(ValueError): run(op, stock_codes=bad)
    assert not op.calls


def test_missing_reference_is_not_replaced_with_first_healthy_stock():
    out, op, _, _ = run(stock_codes=['600999.SH', CODES[1]])
    assert out['status'] == 'completed-with-gaps' and out['cohort_count'] == 2
    assert out['surfaces']['stock_1']['status'] == 'unavailable'
    assert all(p.get('ts_code') != '600999.SH' for _,p in op.calls)
    assert out['surfaces']['stock_2']['cohort_history_qualified']


def test_custody_is_idempotent_and_lost_suspension_does_not_erase_history():
    data = inputs(); backend = MemoryBackend(); store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    with pytest.raises(RecordError): retain_market_reading(data, REGISTRY, store, fresh_reader=fresh)
    assert backend.shared['count'] == 0
    saved = retain_market_reading(data, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    assert saved['query_count'] == 5
    head = backend.head()
    assert retain_market_reading(data, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)['reference'] == saved['reference']
    assert backend.head() == head
    root = json.loads(store.read_source(SourceRef.from_dict(saved['reference']))['data'])
    key = root['reading']['suspension_results'][CODES[0]]['query']
    ref = SourceRef.from_dict(root['queries'][key]); backend.shared['missing'].add((ref.manifest.commit, ref.manifest.path))
    restored = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert restored['status'] == 'partial-restoration' and restored['source_requests'] == 0
    assert entry(restored['view'])['history_quote_explanation']['evidence_status'] == 'rejected'
    assert all(h['status'] == 'observed' for h in entry(restored['view'], 'qfq')['horizons'])


def test_new_process_reads_same_raw_qfq_and_witnesses_after_input_deletion(tmp_path):
    source = tmp_path/'input.json'; source.write_bytes(encoded(inputs()))
    env = {k:v for k,v in os.environ.items() if k not in ('TUSHARE_PROXY_API_KEY','GH_TOKEN','GITHUB_TOKEN','DECISION_OS_STORE_TOKEN')}
    env['PYTHONPATH'] = str(ROOT_DIR/'src')
    def cli(name, *args):
        out = subprocess.run([sys.executable,'-m','decision_kernel.interfaces.'+name,*args], cwd=tmp_path,
                             env=env, text=True, capture_output=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    reg = ['--registry',str(REGISTRY_PATH)]
    direct = cli('market_relay_cli', *reg, '--input',str(source),'--entity-id',CODES[0])
    path=tmp_path/'synthetic.git'; GitStore.init_synthetic(path)
    saved=cli('market_custody_cli','save',*reg,'--path',str(path),'--input',str(source),'--public-rights-confirmed')
    root=tmp_path/'root.json';root.write_bytes(encoded(saved['reference']));source.unlink()
    restored=cli('market_custody_cli','read',*reg,'--path',str(path),'--input',str(root),'--entity-id',CODES[0])
    assert restored['view'] == direct and restored['source_requests'] == 0


def test_existing_manual_host_routes_actual_history_consumer(monkeypatch, tmp_path):
    event, env = host_data(tmp_path)
    event['inputs'].update(selection='stock-history',start=START,explain_quotes='true',stock_codes=json.dumps(CODES))
    op=HistoryOpener();client=RelayHTTP(opener=op,credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.market_history_check.RelayHTTP', lambda:client)
    assert execute(event,env,tmp_path/'out',now=NOW,registry_path=REGISTRY_PATH) == 0
    report=json.loads((tmp_path/'out/result.json').read_bytes())
    assert report['identity']['run_id']=='123' and report['status']=='checked-stock-history'
    assert len(op.calls)==10 and list((tmp_path/'out').iterdir())==[tmp_path/'out/result.json']


@pytest.mark.parametrize('bad', [{'stock_codes':'[true]'}, {'stock_codes':'["600000.SH", "600000.SH"]'},
    {'stock_codes':'{}'}, {'stock_codes':'[]'}, {'explain_quotes':'false'}, {'request_context_confirmed':'false'},
    {'resume':'{}'}, {'member_code':'885800.TI'}, {'exchange':'SSE'}, {'selection':'stock'}])
def test_host_rejects_unused_ambiguous_or_unapproved_history_input(tmp_path,bad):
    event,env=host_data(tmp_path);event['inputs'].update(selection='stock-history',start=START,explain_quotes='true',stock_codes=json.dumps(CODES))
    event['inputs'].update(bad)
    with pytest.raises(ValueError): settings(event,env,NOW)


def test_long_retry_after_is_not_shortened_or_hidden_by_new_cohort():
    op = HistoryOpener()
    def unavailable(api, p):
        if api == 'daily' and p.get('ts_code') == CODES[0]:
            raise HTTPError('https://synthetic.invalid', 504, 'PRIVATE_FAILURE',
                            {'Retry-After': '7200'}, BytesIO(b'{"error":"upstream_timeout"}'))
    op.before = unavailable
    out, _, _, waits = run(op)
    query = out['queries']['stock_1_daily']
    assert query['http_attempts'] == 1 and query['retry_context']['retry_after_seconds'] == 7200
    assert not waits and out['status'] == 'completed-with-gaps'
    assert out['surfaces']['stock_2']['cohort_history_qualified']


def test_historical_end_has_calendar_measured_lag_not_latest_claim():
    op = HistoryOpener(); end = '20261015'
    for key, rows in op.history.items():
        op.history[key] = [r for r in rows if r['trade_date'] <= end]
    out, _, _, _ = run(op, end=end)
    assert all(v['session_lag'] == 1 for v in out['surfaces'].values())
    assert all(all(h['status'] == 'observed' for h in v['qfq_horizons']) for v in out['surfaces'].values())
    assert out['status'] == 'completed-with-gaps' and len(out['queries']) == 12


def test_unknown_freshness_calendar_does_not_fill_zero_lag():
    op = HistoryOpener(); end = '20261015'
    for key, rows in op.history.items():
        op.history[key] = [r for r in rows if r['trade_date'] <= end]
    def fail_freshness(api, p):
        if api == 'trade_cal' and p['start_date'] == end and p['exchange'] == 'SSE':
            raise URLError(TimeoutError('PRIVATE_FRESHNESS_FAILURE'))
    op.before = fail_freshness
    out, _, _, _ = run(op, end=end)
    assert out['surfaces']['stock_1']['session_lag'] is None
    assert out['surfaces']['stock_1']['status'] == 'read'
    assert out['surfaces']['stock_2']['session_lag'] == 1
    assert out['status'] == 'completed-with-gaps'


def test_quarantined_suspension_day_not_green_even_with_observed_prices():
    op = HistoryOpener()
    row = dict(ts_code=CODES[0],trade_date=DAYS[0],suspend_type='R',suspend_timing=None)
    op.history['suspend_d', CODES[0]] = [row, deepcopy(row)]
    out, _, _, _ = run(op)
    assert out['queries']['stock_1_suspend_d']['quarantined_day_count'] == 1
    assert out['surfaces']['stock_1']['quote_counts']['quote-observed'] == len(DAYS)
    assert out['status'] == 'completed-with-gaps'
    assert not out['surfaces']['stock_1']['cohort_history_qualified']


def test_eight_explicit_codes_stay_within_original_query_budget():
    op = HistoryOpener(); codes = ['%06d.SH' % (600000 + n) for n in range(8)]
    base = op.rows['stock_basic','SSE'][0]
    op.rows['stock_basic','SSE'] = [{**base,'ts_code':c,'symbol':c[:6]} for c in codes]
    for code in codes:
        for api in ('daily','adj_factor','suspend_d'):
            op.history[api,code] = [{**r,'ts_code':code} for r in op.history[api,CODES[0]]]
    out, _, _, _ = run(op, stock_codes=codes)
    assert out['cohort_count'] == 8 and len(out['queries']) == 26
    assert out['http_attempts'] == 26 and out['status'] == 'checked-stock-history'
    assert len({q['request_context']['query_key'] for q in out['queries'].values()}) == 26
