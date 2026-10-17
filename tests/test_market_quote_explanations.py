"""Real synthetic capture -> same input diagnosis -> original CLI/custody.

No historical live failure is reconstructed from a count-only receipt. These
fixtures are engineering data, not claims about actual halted securities.
"""
from copy import deepcopy
from datetime import timedelta
import json
import os
import subprocess
import sys

import pytest

from decision_kernel.application.market_suspension_inputs import (
    SUSPENSION_FIELDS, session_suspensions,
)
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.market_member_recovery import selector
from decision_kernel.application.market_session_check import check_session_sources
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute, settings
from test_market_session_coverage import (
    SessionOpener, capture, payload, host_data, NOW, START, END, PRICE, FACTOR,
)
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH, ROOT_DIR
from test_market_custody import MemoryBackend


def record(code='600000.SH', event='S', timing=None, day=END):
    return dict(ts_code=code, trade_date=day, suspend_type=event, suspend_timing=timing)


def opener(rows=None, missing=True):
    op = SessionOpener()
    op.rows[('suspend_d', None)] = [record()] if rows is None else rows
    if missing:
        op.rows[('daily', None)] = [r for r in op.rows[('daily', None)] if r['ts_code'] != '600000.SH']
    return op


def data(op=None):
    op = op or opener()
    out = payload(op)
    out['suspension_result'] = capture(op, 'suspend_d', {'trade_date': END}, SUSPENSION_FIELDS)
    return out


def single(out, code='600000.SH'):
    return next(e for e in out['entries'] if e['entity_id'] == code)


def check(op, **extra):
    receipts, waits = [], []
    result = check_session_sources(**{'registry': REGISTRY, 'exchange': 'CN', 'start': START, 'end': END,
        'now': NOW, 'selection': 'session-reconcile', 'access_confirmed': True, 'request_context_confirmed': True,
        'client': RelayHTTP(opener=op, credential='synthetic-key-only'), 'sleep': waits.append,
        'progress': receipts.append, 'clock': lambda: NOW.isoformat(), **extra})
    return result, receipts, waits


@pytest.mark.parametrize('rows,status', [
    ([record()], 'suspension-supported-missing'),
    ([record(timing='')], 'suspension-supported-missing'),
    ([record(timing='09:30-10:00')], 'intraday-only-unexplained'),
    ([record(event='R')], 'resumption-unexplained'),
    ([], 'no-suspension-record'),
    ([record(), record()], 'suspension-evidence-quarantined'),
    ([record(), record(event='R')], 'suspension-evidence-quarantined'),
    ([record(event='P')], 'suspension-evidence-quarantined'),
])
def test_missing_quote_explanations_never_manufacture_price_or_halt(rows, status):
    inputs = data(opener(rows)); original = encoded(inputs)
    out = read_market_payload(inputs, REGISTRY)
    entry = single(out); explanation = entry['quote_explanation']
    assert explanation['status'] == status and entry['raw']['status'] == 'missing'
    assert entry['raw']['value'] is None and entry['factor']['status'] == 'observed'
    assert explanation['suspension']['all_day_confirmed'] is False
    assert not out['qualified_scope_covered'] and out['scope_count'] == 4
    breakdown = out['quote_explanations']['by_exchange']['SSE']['missing_breakdown']
    assert sum(breakdown.values()) == 1 and breakdown[status] == 1
    assert encoded(inputs) == original and out['quote_explanations']['strict_coverage_reclassified'] is False


@pytest.mark.parametrize('bad', [False, 0, ' ', '24:00-25:00', '10:00-09:30', '9:30-10:00', 'PRIVATE\nTEXT'])
def test_malformed_timing_is_not_a_full_day_suspension(bad):
    out = read_market_payload(data(opener([record(timing=bad)])), REGISTRY)
    assert single(out)['quote_explanation']['status'] == 'suspension-evidence-quarantined'


@pytest.mark.parametrize('event,timing,expected', [('S', None, 'quote-suspension-conflict'),
    ('S', '09:30-10:00', 'quote-observed'), ('R', None, 'quote-observed')])
def test_observed_price_preserved_and_conflicting_halt_not_silently_excused(event, timing, expected):
    out = read_market_payload(data(opener([record(event=event, timing=timing)], missing=False)), REGISTRY)
    entry = single(out)
    assert entry['quote_explanation']['status'] == expected and entry['raw']['value'] == PRICE
    assert out['qualified_scope_covered']  # Original price coverage remains separately true.


@pytest.mark.parametrize('fault', ['missing', 'wrong-day', 'unknown-code', 'filtered', 'incomplete'])
def test_bad_shared_evidence_is_unknown_not_no_halts_and_does_not_erase_prices(fault):
    op = opener()
    if fault == 'wrong-day': op.rows[('suspend_d', None)] = [record(day='20261015')]
    if fault == 'unknown-code': op.rows[('suspend_d', None)] = [record(code='PRIVATE_INVALID')]
    inputs = data(op)
    if fault == 'missing': inputs['suspension_result'] = None
    if fault == 'filtered': inputs['suspension_result'] = capture(op, 'suspend_d',
        {'trade_date': END, 'ts_code': '600000.SH'}, SUSPENSION_FIELDS)
    if fault == 'incomplete':
        op.fail = 'suspend_d'
        inputs['suspension_result'] = capture(op, 'suspend_d', {'trade_date': END}, SUSPENSION_FIELDS)
    out = read_market_payload(inputs, REGISTRY)
    assert single(out)['quote_explanation']['status'] == 'suspension-input-unavailable'
    assert out['by_exchange']['SZSE']['matched_count'] == 2
    assert out['quote_explanations']['source_row_count'] is None


def test_unknown_quote_input_cannot_be_renamed_missing_because_suspension_exists():
    inputs = data(); inputs['session_results'].pop('daily')
    out = read_market_payload(inputs, REGISTRY)
    assert single(out)['quote_explanation']['status'] == 'quote-input-unavailable'
    assert out['quote_explanations']['by_exchange']['SSE']['missing_quote_count'] is None
    assert out['quote_explanations']['by_exchange']['SSE']['missing_breakdown'] is None


def test_ineligible_reference_cannot_be_rehabilitated_by_halt_record():
    op = opener(); op.rows[('stock_basic', 'SSE')][0]['curr_type'] = 'USD'
    out = read_market_payload(data(op), REGISTRY)
    assert single(out)['quote_explanation']['status'] == 'not-eligible'
    assert out['by_exchange']['SSE']['reference_quarantined_count'] == 1
    assert out['quote_explanations']['by_exchange']['SSE']['missing_quote_count'] == 0


def test_missing_master_preserves_unknown_denominator_and_other_venue():
    inputs = data(); inputs['master_results'].pop('SSE')
    out = read_market_payload(inputs, REGISTRY)
    assert out['scope_count'] is None
    assert out['quote_explanations']['by_exchange']['SSE']['explanation_counts'] is None
    assert out['quote_explanations']['by_exchange']['SZSE']['explanation_counts']['quote-observed'] == 2


def test_code_lookup_and_paging_keep_full_diagnostic_denominator_and_content_version():
    inputs = data(); full = read_market_payload(inputs, REGISTRY)
    page = read_market_payload(inputs, REGISTRY, offset=1, limit=1)
    one = read_market_payload(inputs, REGISTRY, entity_id='600000.SH', limit=1)
    assert page['quote_explanations'] == one['quote_explanations'] == full['quote_explanations']
    assert page['view_version'] == one['view_version'] == full['view_version']
    assert one['entries'] == [single(full)] and one['page']['entries_count'] == 4
    assert single(full)['quote_explanation']['suspension']['source_rows'] == [{'page': 1, 'row': 1}]


def test_all_original_suspension_page_row_witnesses_are_preserved():
    op = opener([record(code=f'{600000+i:06d}.SH') for i in range(5001)])
    result = capture(op, 'suspend_d', {'trade_date': END}, SUSPENSION_FIELDS)
    out = session_suspensions(result, REGISTRY, END)
    assert out['row_count'] == 5001 and out['entries']['605000.SH']['source_rows'] == [{'page': 2, 'row': 1}]
    op = opener([record(), record()]); out = read_market_payload(data(op), REGISTRY)
    assert single(out)['quote_explanation']['suspension']['source_rows'] == [{'page': 1, 'row': 1}, {'page': 1, 'row': 2}]


def test_old_six_source_payload_remains_optional_and_unchanged():
    inputs = data(); inputs.pop('suspension_result'); before = encoded(inputs)
    out = read_market_payload(inputs, REGISTRY)
    assert 'quote_explanations' not in out and 'suspend_d' not in out['queries']
    assert all('quote_explanation' not in e for e in out['entries']) and encoded(inputs) == before


def test_same_host_seventh_query_is_not_per_stock_and_projects_only_counts():
    op = opener(); out, receipts, _ = check(op)
    assert len(op.calls) == 7 and op.calls[-1][0] == 'suspend_d'
    assert op.calls[-1][1] == {'trade_date': END, 'fields': ','.join(sorted(SUSPENSION_FIELDS)), 'limit': '5000'}
    assert out['status'] == 'completed-with-gaps'
    assert out['surfaces']['quote_explanations']['by_exchange']['SSE']['missing_breakdown']['suspension-supported-missing'] == 1
    for text in map(json.dumps, receipts + [out]):
        assert all(bad not in text for bad in ('600000.SH', 'PRIVATE_SOURCE_NAME', PRICE, FACTOR,
            'source_rows', 'source_lineage', 'synthetic-key-only', 'wire_sha256'))


def test_seven_generated_requests_exist_before_source_io():
    op = opener(); receipts = []
    op.before = lambda *_: (len(receipts[-1]['queries']) == 7) or pytest.fail('missing pre-I/O context')
    check(op, progress=receipts.append)


def test_empty_evidence_with_full_prices_is_valid_not_unknown():
    out, _, _ = check(opener([], missing=False))
    assert out['status'] == 'checked-session-coverage'
    assert out['queries']['suspend_d']['status'] == 'checked-empty'
    assert out['surfaces']['quote_explanations']['source_row_count'] == 0


def test_bj_evidence_outside_scope_and_unreferenced_sh_sz_is_explicit():
    out, _, _ = check(opener([record('830001.BJ', event='INVALID')], missing=False))
    audit = out['surfaces']['quote_explanations']
    assert audit['outside_sh_sz_count'] == 1 and audit['quarantined_sh_sz_count'] == 0
    assert out['status'] == 'checked-session-coverage'
    out, _, _ = check(opener([record('600099.SH')], missing=False))
    assert out['surfaces']['quote_explanations']['by_exchange']['SSE']['unreferenced_suspension_count'] == 1
    assert out['status'] == 'completed-with-gaps'


def test_evidence_timeout_does_not_erase_successful_daily_or_factor():
    op = opener(); op.fail = 'suspend_d'; out, _, waits = check(op)
    assert waits == [30, 30, 30] and out['http_attempts'] == 10
    assert out['queries']['suspend_d']['status'] == 'capture-incomplete'
    assert out['queries']['suspend_d']['retry_context']['eligible'] is True
    assert out['surfaces']['session']['by_exchange']['SZSE']['matched_count'] == 2
    assert out['surfaces']['quote_explanations']['by_exchange']['SSE']['missing_breakdown']['suspension-input-unavailable'] == 1
    with pytest.raises(RecordError):
        selector({'run_id': 1, 'run_attempt': 1, 'artifact_id': 2, 'slot': 'suspend_d'})


def test_source_refusal_prevents_new_evidence_requests():
    op = opener(); op.fail = 'denied'; out, _, waits = check(op)
    assert out['stopped'] and out['http_attempts'] == 1 and waits == []
    assert out['queries']['suspend_d']['http_attempts'] == 0


def test_existing_default_host_routes_reconciliation_and_requires_explicit_context(tmp_path, monkeypatch):
    op = opener(); client = RelayHTTP(opener=op, credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.market_session_check.RelayHTTP', lambda: client)
    event, env = host_data(tmp_path); event['inputs']['explain_quotes'] = 'true'
    assert execute(event, env, tmp_path/'out', now=NOW, registry_path=REGISTRY_PATH) == 2
    assert len(op.calls) == 7 and {p.name for p in (tmp_path/'out').iterdir()} == {'result.json'}
    event['inputs']['request_context_confirmed'] = 'false'
    with pytest.raises(RecordError): settings(event, env, NOW)


def test_custody_keeps_original_suspend_source_and_failure_is_local():
    inputs = data(); backend = MemoryBackend(); store = VersionedStore(backend)
    fresh = lambda: VersionedStore(MemoryBackend(backend.shared))
    with pytest.raises(RecordError): retain_market_reading(inputs, REGISTRY, store, fresh_reader=fresh)
    assert backend.shared['count'] == 0
    saved = retain_market_reading(inputs, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)
    assert saved['query_count'] == 7 and saved['restored']['view'] == read_market_payload(inputs, REGISTRY)
    head = backend.head()
    assert retain_market_reading(inputs, REGISTRY, store, fresh_reader=fresh, public_rights_confirmed=True)['reference'] == saved['reference']
    assert backend.head() == head
    root = json.loads(store.read_source(SourceRef.from_dict(saved['reference']))['data'])
    ref = SourceRef.from_dict(root['queries'][root['reading']['suspension_result']['query']])
    backend.shared['missing'].add((ref.manifest.commit, ref.manifest.path))
    restored = read_stored_market(saved['reference'], REGISTRY, fresh())
    assert restored['status'] == 'partial-restoration' and restored['source_requests'] == 0
    assert single(restored['view'])['quote_explanation']['status'] == 'suspension-input-unavailable'
    assert restored['view']['by_exchange']['SZSE']['matched_count'] == 2


def test_new_process_restores_diagnostic_by_code_from_root_without_original_input(tmp_path):
    source = tmp_path/'input.json'; source.write_bytes(encoded(data()))
    env = {k: v for k, v in os.environ.items() if k not in ('TUSHARE_PROXY_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'DECISION_OS_STORE_TOKEN')}
    env['PYTHONPATH'] = str(ROOT_DIR/'src')
    def cli(name, *args):
        out = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.' + name, *args],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)
    reg = ['--registry', str(REGISTRY_PATH)]
    direct = cli('market_relay_cli', *reg, '--input', str(source), '--entity-id', '600000.SH')
    path = tmp_path/'synthetic.git'; GitStore.init_synthetic(path)
    saved = cli('market_custody_cli', 'save', *reg, '--path', str(path), '--input', str(source), '--public-rights-confirmed')
    root = tmp_path/'root.json'; root.write_bytes(encoded(saved['reference'])); source.unlink()
    restored = cli('market_custody_cli', 'read', *reg, '--path', str(path), '--input', str(root), '--entity-id', '600000.SH')
    assert restored['view'] == direct and restored['source_requests'] == 0
    assert single(direct)['quote_explanation']['status'] == 'suspension-supported-missing'


@pytest.mark.parametrize('bad', [{'explain_quotes': 1}, {'explain_quotes': 'yes'},
    {'selection': 'members'}, {'selection': 'session-reconcile'}])
def test_host_explanations_flag_does_not_expand_other_operation_modes(tmp_path, bad):
    event, env = host_data(tmp_path); event['inputs']['explain_quotes'] = 'true'
    event['inputs'].update(bad)
    with pytest.raises(RecordError): settings(event, env, NOW)
