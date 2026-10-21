"""Native routing and the normal reader preserve the one-use write proof."""
from copy import deepcopy
import json

import pytest

from decision_kernel.application import daily_write_reconciliation as reconciliation
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.interfaces import daily_market_cli as daily
from decision_kernel.interfaces import daily_reading_cli as reading
from decision_kernel.interfaces import daily_visibility_cli as visibility
from test_daily_visibility import NativeAPI, context
from test_runtime_publication import grant as public_grant


@pytest.mark.parametrize('enabled', [True, False])
def test_reconciliation_needs_deployment_and_fixed_grant_before_records_or_credentials(
        tmp_path, monkeypatch, public_grant, enabled):
    configured = json.loads(reconciliation.POLICY_FILE.read_text())
    configured['enabled'] = enabled
    path = tmp_path / 'reconciliation.json'; path.write_bytes(encoded(configured))
    monkeypatch.setattr(reconciliation, 'POLICY_FILE', path)
    event, env = context('reconcile-write')
    api = NativeAPI()
    if enabled:
        receipt, code = visibility.preflight(event, env, api, use_authorization=True)
        assert code == 0 and receipt['runtime_authorized'] is True
        assert receipt['operation'] == 'update' and receipt['invocation'] == 'reconcile-write'
        assert receipt['source_requests'] == 0
        assert receipt['records_read'] is receipt['records_written'] is False
    else:
        with pytest.raises(RecordError, match='NOT_AUTHORIZED'):
            visibility.preflight(event, env, api, use_authorization=True)
    assert len(api.calls) == 3  # Native metadata only, before Store/artifact/source access.


def test_unapproved_public_reconciliation_cannot_load_its_local_grant(monkeypatch):
    event, env = context('reconcile-write')
    monkeypatch.setattr(reconciliation, 'load_grant', lambda: pytest.fail('unauthorized grant load'))
    receipt, code = visibility.preflight(event, env, NativeAPI(), use_authorization=False)
    assert code == 2 and receipt['status'] == 'blocked' and receipt['source_requests'] == 0


@pytest.mark.parametrize('invocation', ['schedule', 'update', 'status', 'recover-late', 'reconcile-write'])
def test_native_host_only_binds_fixed_reconciliation_to_explicit_invocation(
        tmp_path, monkeypatch, invocation):
    from test_daily_market import Host
    from test_daily_market_schedule import native_runtime
    from decision_kernel.interfaces import daily_write_reconciliation_cli as native
    host = Host()
    event, env, api = native_runtime(tmp_path, monkeypatch, host.approved, invocation=invocation,
        admitted_at='2026-10-20T13:00:00Z', actual_at='2026-10-20T13:05:00Z')
    grant = reconciliation.load_grant()
    calls = []
    def load():
        calls.append('load')
        return deepcopy(grant)
    def verify(actual_api, configured, *, token):
        assert actual_api is api and configured == grant and token == env[daily.TOKEN_ENV]
        calls.append('verify')
        return {'status': 'verified'}
    monkeypatch.setattr(reconciliation, 'load_grant', load)
    monkeypatch.setattr(native, 'verify_native_reconciliation', verify)
    def execute(**options):
        if invocation == 'reconcile-write':
            assert options['write_reconciliation'] == grant and 'calendar_repair' not in options
            assert options['reconciliation_verifier'](grant) == {'status': 'verified'}
        else:
            assert not {'write_reconciliation', 'reconciliation_verifier', 'calendar_repair'} & set(options)
        return {'receipt': {'schema': 'daily-market-runtime-v1', 'status': 'read',
            'source_requests': 0}, 'control': {}}
    out = tmp_path / 'result'
    assert daily.run_runtime(event, env, out, api=api, execute=execute) == 0
    result = json.loads((out / 'result.json').read_text())
    assert result['invocation'] == invocation
    assert result['operation'] == ('status' if invocation == 'status' else 'update')
    assert calls == (['load', 'verify'] if invocation == 'reconcile-write' else [])


def test_explicit_route_accepts_no_caller_target_budget_or_date():
    event, env = context('reconcile-write')
    approved = json.loads(daily.POLICY_FILE.read_text())
    assert daily.settings(event, env, approved)['invocation'] == 'reconcile-write'
    for key, value in [('date', '20261020'), ('source_budget', 13), ('original_run_id', 5)]:
        changed = deepcopy(event); changed['inputs'][key] = value
        with pytest.raises(RecordError, match='DAILY_MARKET_INPUT'):
            daily.settings(changed, env, approved)
        with pytest.raises(RecordError, match='DAILY_VISIBILITY_INPUT'):
            visibility.selection(changed, env)


def test_normal_summary_shows_zero_extra_requests_and_only_known_write_phase():
    model = {'daily_update': {'read_as_of': '2026-10-20T13:00:00Z', 'source_receipt': {
        'status': 'persistence-unconfirmed', 'write_reconciliation': {
            'schema': 'daily-write-reconciliation-v1', 'additional_source_requests': 0},
        'write_last_failure': {'schema': 'github-write-diagnostic-v1', 'phase': 'readback',
            'classification': 'UNTRUSTED_TEXT', 'body': 'DO_NOT_ECHO'}},
        'market_cycle': None, 'market_resolution': 'resolved'},
        'brief': {'status': 'no-saved-brief'}, 'catalogue': {'total_cases': 0, 'returned': 0}}
    summary = '\n'.join(reading._summary_lines(model))
    assert '额外来源预算为0' in summary and '原失败保留' in summary and '原始字节读回' in summary
    assert 'UNTRUSTED_TEXT' not in summary and 'DO_NOT_ECHO' not in summary
    model['daily_update']['source_receipt']['write_last_failure']['phase'] = 'DO_NOT_ECHO'
    assert 'DO_NOT_ECHO' not in '\n'.join(reading._summary_lines(model))
    model['daily_update']['source_receipt']['write_reconciliation']['additional_source_requests'] = False
    assert '额外来源预算为0' not in '\n'.join(reading._summary_lines(model))
