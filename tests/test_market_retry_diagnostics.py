"""Safe retry evidence reaches the real market consumers and fixed host receipt."""
from copy import deepcopy
import json

import pytest

from decision_kernel.application.market_retry_diagnostics import retry_diagnostics
from decision_kernel.application.market_source_check import check_sources
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces.market_source_check_cli import execute
from test_market_history_continuity import HistoryOpener, CODES, START, run as history_run
from test_market_session_coverage import host_data, NOW, END
from test_market_relay_inputs import REGISTRY, REGISTRY_PATH
from test_market_source_check import Opener, START as SAMPLE_START, END as SAMPLE_END, SECRET_TEXT
from test_relay_retry_diagnostics import HeadersOpener
from test_research_sources import busy


def test_fourth_page_rate_limit_is_one_attempt_on_that_page_and_stops_history():
    op = HistoryOpener()
    original = op.rows['stock_basic', 'SZSE'][0]
    op.rows['stock_basic', 'SZSE'] = [{**original, 'ts_code': f'{i:06d}.SZ', 'symbol': f'{i:06d}'}
                                     for i in range(1, 1502)]
    def before(api, params):
        if api == 'stock_basic' and params.get('exchange') == 'SZSE' and params.get('offset') == '1500':
            raise busy(429, 'rate_limited', {'Retry-After': '900', 'X-RateLimit-Remaining': '0',
                                            'X-RateLimit-IP-Remaining': '4', 'X-Request-ID': SECRET_TEXT})
    op.before = before
    out, _, receipts, waits = history_run(op, selection='stock-history-reconcile')
    q = out['queries']['SZSE_master']; d = q['retry_diagnostics']
    assert out['stopped'] and out['status'] == 'completed-with-gaps' and waits == []
    assert (q['http_attempts'], q['page_count'], q['row_count']) == (4, 3, 1500)
    assert d['recorded_attempts'] == 4 and d['last_failure']['page_number'] == 4 and d['last_failure']['attempt'] == 1
    assert d['last_failure']['retry_after'] == {'state': 'valid', 'seconds': 900}
    assert d['last_request']['retry_decision'] == {'reason': 'wait-budget', 'required_wait_seconds': 900}
    assert d['last_failure']['key_remaining']['remaining'] == 0 and d['last_failure']['ip_remaining']['remaining'] == 4
    assert q['retry_context'] == {'eligible': False}
    untouched = [v for v in out['queries'].values() if v['status'] == 'not-attempted']
    assert len(untouched) == 6 and all(v['http_attempts'] == 0 and v['retry_diagnostics'] is None for v in untouched)
    assert all(v['reason_code'] == 'GLOBAL_SOURCE_STOP' for v in untouched)
    assert all(not v['cohort_history_qualified'] for v in out['surfaces'].values())
    assert len(op.calls) == out['http_attempts'] == 7
    for receipt in receipts:
        text = json.dumps(receipt)
        assert all(s not in text for s in (SECRET_TEXT, 'PRIVATE_SOURCE_NAME', 'synthetic-key-only',
                                           'wire_sha256', 'x_request_id', 'Retry-After', 'X-RateLimit-', '"items"'))


def test_recovered_failure_and_latest_response_remain_separate_in_normal_market_result():
    class Once(Opener):
        def open(self, request, timeout):
            if not self.calls:
                self.calls.append(('synthetic-limit', {}))
                raise busy(429, 'ip_rate_limited', {'Retry-After': '0', 'X-RateLimit-IP-Remaining': '0'})
            response = super().open(request, timeout)
            response.headers['X-RateLimit-IP-Remaining'] = '8'
            return response
    op = Once(); waits, receipts = [], []
    out = check_sources(registry=REGISTRY, exchange='SSE', start=SAMPLE_START, end=SAMPLE_END,
        access_confirmed=True, selection='catalogues', client=RelayHTTP(opener=op, credential='synthetic-key-only'),
        now=NOW, sleep=waits.append, clock=lambda: NOW.isoformat(), progress=receipts.append)
    q = out['queries']['sector_catalog']; d = q['retry_diagnostics']
    assert out['status'] == 'checked-catalogues' and not out['stopped'] and waits == [30]
    assert q['status'] == 'checked' and q['reason_code'] is None
    assert q['last_attempt_failure'] == {'classification': 'rate-limit', 'http_status': 429, 'business_error': 'ip_rate_limited'}
    assert d['last_failure']['retry_after']['seconds'] == 0 and d['completed_wait_seconds'] == 30
    assert d['last_failure']['request_recovered'] and d['last_failure']['ip_remaining']['remaining'] == 0
    assert d['last_response']['attempt'] == 2 and d['last_response']['ip_remaining']['remaining'] == 8
    assert out['queries']['concept_catalog']['retry_diagnostics']['last_failure'] is None
    assert all(SECRET_TEXT not in json.dumps(x) for x in receipts)


@pytest.mark.parametrize('selection,module', [
    ('catalogues', 'market_source_check'), ('members', 'market_source_check'),
    ('session-coverage', 'market_session_check'), ('stock-history', 'market_history_check'),
    ('stock-history-reconcile', 'market_history_check'), ('factor-audit', 'market_factor_check'),
])
def test_original_host_routes_every_market_consumer_and_writes_only_safe_receipt(tmp_path, monkeypatch, selection, module):
    event, env = host_data(tmp_path)
    event['inputs']['selection'] = selection
    if selection in ('stock-history', 'stock-history-reconcile', 'factor-audit'):
        event['inputs'].update(start=START, stock_codes=json.dumps(CODES), explain_quotes=selection != 'factor-audit')
    elif selection in ('catalogues', 'members'):
        event['inputs'].update(exchange='SSE', start=SAMPLE_START, end=SAMPLE_END,
                               request_context_confirmed=selection == 'members',
                               member_code='885800.TI' if selection == 'members' else '')
    op = HeadersOpener(busy(429, 'rate_limited', {'Retry-After': '181', 'X-RateLimit-Remaining': '0',
                                                 'X-Request-ID': SECRET_TEXT}))
    client = RelayHTTP(opener=op, credential='synthetic-key-only')
    monkeypatch.setattr('decision_kernel.application.' + module + '.RelayHTTP', lambda: client)
    out = tmp_path / 'out'
    assert execute(event, env, out, now=NOW, registry_path=REGISTRY_PATH) == 2
    assert {p.name for p in out.iterdir()} == {'result.json'}
    receipt = json.loads((out / 'result.json').read_bytes())
    assert receipt['status'] == 'completed-with-gaps' and receipt['stopped'] and receipt['http_attempts'] == 1
    assert receipt['identity']['code_sha'] == env['GITHUB_SHA'] and receipt['identity']['run_id'] == '123'
    failed = next(q for q in receipt['queries'].values() if q['status'] == 'capture-incomplete')
    assert failed['retry_diagnostics']['last_request']['retry_decision']['reason'] == 'wait-budget'
    assert failed['retry_diagnostics']['last_failure']['retry_after'] == {'state': 'valid', 'seconds': 181}
    if selection != 'catalogues':
        assert failed['retry_context'] == {'eligible': False}
    assert all(q['retry_diagnostics'] is None for q in receipt['queries'].values() if q['status'] == 'not-attempted')
    assert len(op.calls) == 1 and not receipt['supplier_data_retained'] and not receipt['source_refs_created']
    raw = (out / 'result.json').read_text()
    assert all(s not in raw for s in (SECRET_TEXT, 'synthetic-key-only', 'x_request_id', '"capture"', 'Retry-After', 'X-RateLimit-'))


def test_projection_masks_untrusted_diagnostic_prose_types_and_missing_old_facts():
    attempt = {'attempt': True, 'error': SECRET_TEXT, 'completed_wait_seconds': float('nan'),
        'headers': {'retry_after': {'state': 'valid', 'seconds': True},
                    'key_remaining': {'state': SECRET_TEXT, 'remaining': 7},
                    'ip_remaining': {'state': 'valid', 'remaining': -1}, SECRET_TEXT: SECRET_TEXT},
        'retry_decision': {'reason': SECRET_TEXT, 'required_wait_seconds': 45}, 'message': SECRET_TEXT}
    capture = {'pages': [], 'attempts': [attempt],
               'request_execution': {'page_number': True, 'retry_decision': attempt['retry_decision'], 'headers': SECRET_TEXT}}
    before = deepcopy(capture); out = retry_diagnostics(capture)
    assert SECRET_TEXT not in json.dumps(out, allow_nan=False)
    assert out['recorded_attempts'] is out['completed_wait_seconds'] is None
    failure = out['last_failure']
    assert failure['attempt'] is None and failure['completed_wait_seconds'] is None
    assert failure['retry_after'] == {'state': 'invalid', 'seconds': None}
    assert failure['key_remaining'] == {'state': 'not-recorded', 'remaining': None}
    assert failure['ip_remaining'] == {'state': 'invalid', 'remaining': None}
    assert out['last_request']['retry_decision'] == {'reason': 'not-recorded', 'required_wait_seconds': None}
    assert capture == before
    old = retry_diagnostics({'pages': [], 'attempts': [{'attempt': 1, 'error': 'RELAY_HTTP_429',
                                                       'failure': {'classification': 'rate-limit'}}]})
    assert old['last_failure']['retry_after'] == {'state': 'not-recorded', 'seconds': None}
    assert old['last_failure']['completed_wait_seconds'] is None
