"""Explicit calendar repair, original Git history and real same-source transport."""
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from decision_kernel.application import daily_calendar_repair as repair
from decision_kernel.application import daily_market as daily
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, UnknownWrite, encoded, digest
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import daily_market_cli as cli

NOW = '2026-10-20T06:30:00+00:00'
AUTHORITY = 'https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6284820778'
REGISTRY = RelayRegistry(Path(__file__).resolve().parents[1] / 'docs/sources/relay-registry.json')


def wire(value):
    return json.loads(encoded(value))


def approved_for(base):
    return {'schema': daily.POLICY_SCHEMA, 'enabled': True, 'schedule_utc': '35 10 * * 1-5',
        'not_before_local': '18:35', 'lookback_days': 119, 'max_calendar_requests': 2,
        'max_price_requests': 2, 'symbols': ['600000.SH'], 'seed_reference': base}


@pytest.fixture
def history(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / 'records.git')
    store = VersionedStore(backend)
    base = wire(asdict(store.put_source('base', SourceInput('base', 'content', 'Synthetic base',
        'https://example.org/synthetic', 'Synthetic fixture; not a price qualification.'))))
    cycle = {'schema': daily.SCHEMA, 'date': '20261019', 'revision': 0,
        'created_at': '2026-10-20T05:14:00+00:00', 'updated_at': '2026-10-20T05:14:00+00:00',
        'predecessor': None, 'policy_sha256': daily.policy_digest(approved_for(base)), 'base_market_reference': base,
        'calendar_start': '20261016', 'calendar_end': '20261019', 'price_end': None,
        'market_generation': 'daily-20261019', 'market_reference': None,
        'calendars': {v: {'status': 'pending', 'reserved_requests': 0, 'reference': None,
            'reason_code': None} for v in daily.VENUES}, 'status': 'calendar-pending', 'reason_code': None}
    def append(state):
        daily.validate_cycle(state)
        source = daily._source(state)
        ref = store.put_source('cycle-' + str(state['revision']), source)
        return {'cycle': wire(state), 'reference': wire(asdict(ref))}
    item = append(cycle)
    rows = deepcopy(cycle['calendars']); rows['SSE'].update(status='in-flight', reserved_requests=1)
    item = append(daily.next_state(item, at=NOW, calendars=rows))
    rows['SSE'].update(status='failed', reason_code='RELAY_HTTP_UNAVAILABLE')
    item = append(daily.next_state(item, at=NOW, calendars=rows, status='blocked', reason_code='RELAY_HTTP_UNAVAILABLE'))
    grant = {'schema': repair.PROOF_SCHEMA, 'original_reference': item['reference'],
             'authorization_ref': AUTHORITY, 'venue': 'SSE', 'additional_requests': 1}
    return store, item, grant, base, append


def test_repair_reservation_keeps_spent_attempt_and_all_original_fields(history):
    store, old, grant, _, _ = history
    before = encoded(old)
    new = repair.reserve(old, grant, at=NOW)
    assert encoded(old) == before
    assert new['calendars']['SSE']['reserved_requests'] == 2
    assert new['calendars']['SZSE']['reserved_requests'] == 0
    assert new['predecessor'] == old['reference'] and new['calendar_repair'] == grant
    daily._transition(old['cycle'], new)
    assert 'NO_RETRY_OR_CROSS_DAY_FAILURE_RESET' in daily._source(old['cycle']).limitations[2]
    assert 'ORIGINAL_FAILURE_PRESERVED' in daily._source(new).limitations[2]


@pytest.mark.parametrize('reason', ['RELAY_HTTP_401', 'RELAY_HTTP_403', 'RELAY_HTTP_429',
    'RELAY_AUTH_OR_ENTITLEMENT', 'DAILY_MARKET_FAILED', 'RELAY_CREDENTIAL_REQUIRED'])
def test_other_failures_cannot_inherit_transport_repair(history, reason):
    _, old, grant, _, _ = history
    old['cycle']['reason_code'] = old['cycle']['calendars']['SSE']['reason_code'] = reason
    with pytest.raises(RecordError, match='NOT_TRANSPORT_FAILURE'):
        repair.reserve(old, grant, at=NOW)


@pytest.mark.parametrize('change', [
    {'additional_requests': 2}, {'additional_requests': True}, {'venue': 'NYSE'}, {'venue': []},
    {'authorization_ref': 'source says retry'}, {'extra': True}])
def test_grant_is_finite_and_not_a_source_directive(history, change):
    grant = {**history[2], **change}
    with pytest.raises(RecordError):
        repair.proof(grant)


@pytest.mark.parametrize('change', ['target', 'date', 'generation', 'base', 'clock', 'counts', 'proof', 'skip-reservation'])
def test_repair_edge_cannot_change_identity_or_skip_reservation(history, change):
    _, old, grant, _, _ = history
    state = repair.reserve(old, grant, at=NOW)
    if change == 'target': state['predecessor']['manifest']['commit'] = 'f' * 40
    elif change == 'date': state['date'] = '20261020'
    elif change == 'generation': state['market_generation'] = 'new-generation'
    elif change == 'base': state['base_market_reference']['manifest']['commit'] = 'f' * 40
    elif change == 'clock': state['updated_at'] = '2026-10-19T00:00:00Z'
    elif change == 'counts': state['calendars']['SSE']['reserved_requests'] = 1
    elif change == 'proof': state['calendar_repair']['additional_requests'] = 2
    else: state['calendars']['SSE'].update(status='complete', reference=history[3])
    with pytest.raises(RecordError):
        daily._transition(old['cycle'], state)


def test_original_blocked_cycle_has_no_implicit_transition(history):
    _, old, _, _, _ = history
    state = {**deepcopy(old['cycle']), 'revision': 3, 'status': 'calendar-pending', 'reason_code': None}
    with pytest.raises(RecordError, match='TERMINAL'):
        daily._transition(old['cycle'], state)


def test_real_git_history_replays_repair_without_live_grant_and_never_resets(history, monkeypatch, tmp_path):
    store, old, grant, _, append = history
    original = store.read_source(daily.SourceRef.from_dict(old['reference']))
    state = repair.reserve(old, grant, at=NOW)
    item = append(state)
    assert daily.latest_cycle(store) == item
    monkeypatch.setattr(repair, 'POLICY_FILE', tmp_path / 'disabled.json')
    assert daily.latest_cycle(store) == item  # Revocation is not historical deletion.
    assert repair.select_repair(item, grant, at=NOW) is False
    with pytest.raises(RecordError, match='ALREADY_SPENT'):
        repair.reserve(item, grant, at=NOW)
    assert store.read_source(daily.SourceRef.from_dict(old['reference'])) == original


def test_disabled_grant_and_expired_or_changed_target_fail_before_source(history, monkeypatch, tmp_path):
    _, old, grant, _, _ = history
    path = tmp_path / 'grant.json'; path.write_text(json.dumps({'enabled': False, 'repair': grant}))
    monkeypatch.setattr(repair, 'POLICY_FILE', path)
    with pytest.raises(RecordError, match='NOT_AUTHORIZED'):
        repair.load_grant()
    with pytest.raises(RecordError, match='WINDOW_EXPIRED'):
        repair.select_repair(old, grant, at='2026-10-28T00:00:00Z')
    bad = deepcopy(grant); bad['original_reference']['manifest']['commit'] = 'f' * 40
    with pytest.raises(RecordError, match='TARGET_CHANGED'):
        repair.select_repair(old, bad, at=NOW)


class Opener:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
    def open(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        if self.failure == 'timeout': raise TimeoutError('DO_NOT_ECHO_SYNTHETIC_SECRET')
        if self.failure == 'denied':
            raise HTTPError(request.full_url, 403, 'denied', {}, io.BytesIO(b'{"error":"forbidden"}'))
        parameters = parse_qs(urlparse(request.full_url).query)
        venue = parameters['exchange'][0]
        raw = encoded({'code': 0, 'data': {'fields': ['exchange', 'cal_date', 'is_open'],
            'items': [[venue, d, n] for d, n in [('20261016', 1), ('20261017', 0), ('20261018', 0), ('20261019', 1)]]}})
        result = io.BytesIO(raw); result.status = 200
        result.headers = {'Content-Type': 'application/json'}
        result.geturl = lambda: request.full_url
        return result


def hosted(history, monkeypatch, opener, *, grant=True):
    store, old, configured, base, _ = history
    fresh = lambda: VersionedStore(GitStore(store.backend.root))
    frontier_checks = []
    # Price qualification/execution is separately covered by the existing FT suite.
    # This fixture exercises real calendar HTTP parsing, custody and cycle CAS.
    monkeypatch.setattr(daily, 'check_cycle_frontier', lambda *a, **k: frontier_checks.append(a[2].backend.head()))
    monkeypatch.setattr(cli, 'completed_session', lambda *a: {'price_end': '20261019', 'new_session': True})
    monkeypatch.setattr(cli, 'price_configuration', lambda *a: {'synthetic-price-owner': True})
    def current(reader, *a):
        latest = daily.latest_cycle(reader)
        return {'cycle': latest['cycle'], 'cycle_reference': latest['reference'], 'reference': base,
            'frontier_reason_code': None, 'frontier_blocking_reference': None, 'frontier_successor_references': []}
    monkeypatch.setattr(cli, 'daily_market_reference', current)
    monkeypatch.setattr(cli, 'read_ftshare_collection_version', lambda *a, **k: {})
    monkeypatch.setattr(cli, 'health_receipt', lambda *a: {'status': 'synthetic-price-owner-not-evaluated'})
    def original(*a, **k):
        return {'existing': daily.latest_cycle(fresh()), 'candidate': None, 'admission': {}}
    monkeypatch.setattr(cli, 'initial_cycle', original)
    price_calls = []
    def prices(**kwargs):
        price_calls.append(kwargs)
        assert len(kwargs['calendar_references']) == 2
        return {'receipt': {'status': 'complete', 'source_requests': 0, 'reason_code': None}, 'reference': base}
    approved = approved_for(base)
    def run():
        return cli.run_daily(approved=approved, registry=REGISTRY,
            open_unit=lambda write: (store, fresh, None), clock=lambda: NOW,
            capacity=lambda reader: {'admit_work_unit': True}, sleep=lambda seconds: None,
            execute_prices=prices, calendar_repair=configured if grant else None,
            calendar_client_factory=lambda: RelayHTTP(opener=opener, credential='SYNTHETIC_KEY_ONLY', request_timeout_seconds=90))
    return run, price_calls, frontier_checks


def test_real_calendar_recovery_keeps_one_extra_request_then_normal_price_handoff(history, monkeypatch):
    opener = Opener(); run, prices, checks = hosted(history, monkeypatch, opener)
    result = run()['receipt']
    assert result['status'] == 'complete', result
    assert result['calendar_source_requests'] == 2 and len(opener.calls) == 2
    assert result['calendar_reserved_requests'] == 3 and result['calendar_source_budget'] == 3
    assert result['calendar_original_source_budget'] == 2 and result['calendar_additional_source_budget'] == 1
    assert len(prices) == 1 and len(checks) == 2
    assert all(url.startswith('https://pcd.mobcvb.cn/tushare/pro/trade_cal?') and timeout == 90 for url, timeout in opener.calls)
    latest = daily.latest_cycle(history[0]); assert latest['cycle']['revision'] == 8
    assert latest['cycle']['calendar_repair']['original_reference'] == history[1]['reference']
    assert run()['receipt']['status'] == 'already-complete' and len(opener.calls) == 2 and len(prices) == 1


@pytest.mark.parametrize('failure,category', [('timeout', 'timeout'), ('denied', 'auth-or-entitlement')])
def test_repair_failure_is_diagnostic_and_terminal_not_another_attempt(history, monkeypatch, failure, category):
    opener = Opener(failure); run, prices, _ = hosted(history, monkeypatch, opener)
    result = run()['receipt']
    assert result['status'] == 'blocked' and len(opener.calls) == 1 and prices == []
    assert result['calendar_last_failure']['classification'] == category
    assert 'DO_NOT_ECHO' not in json.dumps(result)
    assert daily.latest_cycle(history[0])['cycle']['calendars']['SSE']['reserved_requests'] == 2
    assert run()['receipt']['status'] == 'source-held' and len(opener.calls) == 1


def test_ordinary_update_still_cannot_spend_the_explicit_repair(history, monkeypatch):
    opener = Opener(); run, prices, checks = hosted(history, monkeypatch, opener, grant=False)
    assert run()['receipt']['status'] == 'source-held' and not opener.calls and not prices and not checks


def test_unknown_reservation_write_cannot_request_or_restart(history, monkeypatch):
    opener = Opener(); run, prices, _ = hosted(history, monkeypatch, opener)
    original = history[0].put_source
    def uncertain(*a, **k):
        original(*a, **k)
        raise UnknownWrite('REF_UPDATE_UNCONFIRMED')
    monkeypatch.setattr(history[0], 'put_source', uncertain)
    assert run()['receipt']['status'] == 'persistence-unconfirmed' and not opener.calls
    monkeypatch.setattr(history[0], 'put_source', original)
    assert run()['receipt']['status'] == 'persistence-unconfirmed' and not opener.calls and not prices


def test_stale_cas_before_repair_reservation_prevents_source(history, monkeypatch):
    opener = Opener(); run, prices, _ = hosted(history, monkeypatch, opener)
    original = history[0].put_source
    def competing(*a, **k):
        original('other-intent', SourceInput('other', 'content', 'Synthetic competitor',
            'https://example.org/synthetic', 'Other writer advanced the same store.'))
        return original(*a, **k)
    monkeypatch.setattr(history[0], 'put_source', competing)
    assert run()['receipt']['status'] == 'blocked' and not opener.calls and not prices
    assert daily.latest_cycle(history[0]) == history[1]


@pytest.mark.parametrize('value', ['SECRET_PROSE', {'secret': 'x'}, ['x'], 5, None])
def test_failure_diagnostics_only_export_typed_allowlisted_facts(value):
    client = SimpleNamespace(last_error_meta={'classification': value, 'http_status': value, 'body': 'SECRET'},
                             request_timeout_seconds=value)
    result = repair.diagnostic('SSE', client, elapsed_seconds=float('nan'))
    assert result['classification'] == 'unclassified'
    assert result['elapsed_seconds'] is None and 'SECRET' not in json.dumps(result)


def test_repair_rejects_changed_scope_policy_before_any_reservation_or_source(history, monkeypatch):
    opener = Opener(); run, prices, checks = hosted(history, monkeypatch, opener)
    monkeypatch.setattr(daily, 'policy_digest', lambda value: 'f' * 64)
    result = run()['receipt']
    assert result['status'] == 'blocked' and result['reason_code'] == 'DAILY_MARKET_POLICY_CHANGED'
    assert not opener.calls and not prices and not checks
    assert daily.latest_cycle(history[0]) == history[1]


def native_context():
    from decision_kernel.infrastructure.runtime_publication import REPOSITORY, REPOSITORY_ID
    sha = 'a' * 40
    return {'inputs': {'operation': 'repair-calendar', 'code_sha': sha}}, {
        'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REPOSITORY_ID': str(REPOSITORY_ID),
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_WORKFLOW_REF': REPOSITORY + '/.github/workflows/daily-market.yml@refs/heads/main',
        'GITHUB_RUN_ID': '8001', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': sha}


def test_workflow_uses_the_existing_serial_writer_and_no_new_source_secret_route(history):
    import yaml
    from decision_kernel.interfaces import daily_visibility_cli as visibility
    root = Path(__file__).resolve().parents[1]
    flow = yaml.load((root / '.github/workflows/daily-market.yml').read_text(), Loader=yaml.BaseLoader)
    assert flow['on']['workflow_dispatch']['inputs']['operation']['options'] == [
        'status', 'update', 'recover-late', 'repair-calendar']
    assert set(flow['jobs']) == {'visibility', 'status', 'daily'} and flow['permissions'] == {}
    assert flow['concurrency'] == {'group': 'decision-os-relay-source', 'cancel-in-progress': 'false'}
    assert "inputs.operation == 'repair-calendar'" in flow['jobs']['daily']['if']
    assert 'secrets.' not in json.dumps(flow['jobs']['visibility'])
    assert flow['jobs']['daily']['needs'] == 'visibility'
    assert 'runtime_authorized' in flow['jobs']['daily']['if']
    event, env = native_context()
    selected = cli.settings(event, env, approved_for(history[3]))
    assert selected['operation'] == 'update' and selected['invocation'] == 'repair-calendar'
    assert visibility.selection(event, env)['operation'] == 'repair-calendar'
    event['inputs']['extra_budget'] = 100
    with pytest.raises(RecordError, match='DAILY_MARKET_INPUT'):
        cli.settings(event, env, approved_for(history[3]))


@pytest.mark.parametrize('enabled', [True, False])
def test_public_preflight_requires_both_deployment_and_explicit_calendar_grant(history, monkeypatch, tmp_path, enabled):
    from decision_kernel.interfaces import daily_visibility_cli as visibility
    from decision_kernel.infrastructure import runtime_publication as publication
    event, env = native_context()
    policy_file = tmp_path / 'public.json'
    policy_file.write_bytes(encoded({'schema': 'public-runtime-authorization-v1', 'enabled': True,
        'repository': publication.REPOSITORY, 'repository_id': publication.REPOSITORY_ID,
        'branch': 'records', 'anchor': publication.ANCHOR, 'store_id': publication.STORE_ID,
        'authorization_ref': 'https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674',
        'scopes': sorted(publication.SCOPES)}))
    monkeypatch.setattr(publication, 'AUTHORIZATION_FILE', policy_file)
    grant_file = tmp_path / 'repair.json'; grant_file.write_bytes(encoded({'enabled': enabled, 'repair': history[2]}))
    monkeypatch.setattr(repair, 'POLICY_FILE', grant_file)
    calls = []
    class Native:
        def request(self, method, path):
            calls.append(path)
            assert method == 'GET'
            if path == visibility.PREFIX:
                return {'id': publication.REPOSITORY_ID, 'full_name': publication.REPOSITORY,
                    'private': False, 'archived': False, 'default_branch': 'main'}
            assert path == visibility.PREFIX + '/actions/runs/8001'
            return {'id': 8001, 'head_sha': env['GITHUB_SHA'], 'head_branch': 'main',
                'event': 'workflow_dispatch', 'run_attempt': 1, 'path': visibility.WORKFLOW,
                'name': visibility.WORKFLOW_NAME, 'status': 'in_progress',
                'repository': {'id': publication.REPOSITORY_ID}, 'head_repository': {'id': publication.REPOSITORY_ID}}
    if enabled:
        report, code = visibility.preflight(event, env, Native(), use_authorization=True)
        assert code == 0 and report['runtime_authorized'] and report['repository_visibility'] == 'public'
        assert report['source_requests'] == 0 and report['records_read'] is False
    else:
        with pytest.raises(RecordError, match='NOT_AUTHORIZED'):
            visibility.preflight(event, env, Native(), use_authorization=True)
    assert len(calls) == 3  # No source, Store, archive or provider credential call.


def test_normal_reading_summary_preserves_repair_budget_and_safe_failure_category():
    from decision_kernel.interfaces import daily_reading_cli as reading
    assert {'calendar_last_failure', 'calendar_repair', 'calendar_additional_source_budget'} <= set(reading.UPDATE_FIELDS)
    model = {'daily_update': {'read_as_of': NOW, 'source_receipt': {'status': 'blocked',
        'calendar_additional_source_budget': 1, 'calendar_last_failure': {'classification': 'timeout'}},
        'market_cycle': None, 'market_resolution': 'resolved'}, 'brief': {'status': 'no-saved-brief'},
        'catalogue': {'total_cases': 0, 'returned': 0}}
    summary = '\n'.join(reading._summary_lines(model))
    assert 'timeout' in summary and '显式追加1次' in summary and '没有自动重试' in summary
    model['daily_update']['source_receipt']['calendar_last_failure']['classification'] = 'TOKEN_DO_NOT_ECHO'
    assert 'TOKEN_DO_NOT_ECHO' not in '\n'.join(reading._summary_lines(model))
