"""Finite bulk diagnostic: synthetic wires, no source credentials or live requests."""
from copy import deepcopy
from decimal import localcontext
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

from decision_kernel.application import relay_bulk_check as core
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import parameters_for
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import relay_bulk_check_cli as host


SHA = 'a' * 40


def rows(n=2, days=('20261016',)):
    return [[f'{i:06d}.SZ', days[i % len(days)], '2.30'] for i in range(n)]


def wire(items):
    return json.dumps({'code': 0, 'api_name': 'daily', 'count': len(items),
                      'data': {'fields': core.FIELDS.split(','), 'items': items}}).encode()


def test_fixed_plan_does_not_change_normal_source_limit():
    assert len(core.plan()) == core.MAX_CALLS == 4
    assert [p['parameters'].get('limit') for p in core.plan()] == [None, '6000', '12000', '12000']
    registry = RelayRegistry(Path(__file__).parents[1] / 'docs/sources/relay-registry.json')
    with pytest.raises(RecordError, match='SOURCE_LIMIT_REQUIRED'):
        parameters_for(registry, 'daily', core.plan()[1]['parameters'])


def test_realistic_bulk_count_is_observed_not_assumed_from_requested_limit():
    spec = core.plan()[3]
    small = core.inspect_quote(wire(rows(6000, core.DAYS)), spec)
    big = core.inspect_quote(wire(rows(11132, core.DAYS)), spec)
    assert not small['returned_above_6000'] and big['returned_above_6000']
    assert sum(p['rows'] for p in big['partitions'].values()) == big['rows'] == 11132
    assert small['requested_limit'] == 12000
    assert small['completeness'].startswith('requires-independent')
    assert not big['market_qualification_granted']


@pytest.mark.parametrize('damage', ['duplicate', 'date', 'symbol', 'zero', 'nan', 'bool', 'missing-field', 'width', 'code', 'over-limit'])
def test_invalid_quote_never_qualifies(damage):
    obj = json.loads(wire(rows()))
    item = obj['data']['items'][0]
    if damage == 'duplicate': obj['data']['items'][1] = list(item)
    if damage == 'date': item[1] = '20261014'
    if damage == 'symbol': item[0] = 'SPX'
    if damage == 'zero': item[2] = '0'
    if damage == 'nan': item[2] = 'NaN'
    if damage == 'bool': item[2] = True
    if damage == 'missing-field': obj['data']['fields'][2] = 'latest'
    if damage == 'width': item.pop()
    if damage == 'code': obj['code'] = -1
    if damage == 'over-limit': obj = json.loads(wire(rows(6001)))
    with pytest.raises((RecordError, ValueError)):
        core.inspect_quote(json.dumps(obj).encode(), core.plan()[1])


def test_decimal_source_bytes_are_not_rounded_by_float_or_ambient_context():
    raw = b'{"code":0,"data":{"fields":["ts_code","trade_date","close"],"items":[["000001.SZ","20261016",1.234567890123456789]]}}'
    with localcontext() as ctx:
        ctx.prec = 3
        result = core.inspect_quote(raw, core.plan()[1])
    assert result['rows_sha256'] == core.rows_digest([['000001.SZ', '20261016', '1.234567890123456789']])
    assert core.price_text('2.3000') == core.price_text(2.3) == '2.3'


def test_duplicate_keys_and_nonfinite_json_and_oversized_input_fail():
    for raw in [b'{"code":0,"code":1}', b'{"code":NaN}', b' ' * (core.MAX_BODY + 1)]:
        with pytest.raises((RecordError, ValueError)):
            core.inspect_quote(raw, core.plan()[1])


def test_unrecognized_or_null_capability_never_means_unlimited():
    assert core.inspect_capability(b'{"hello":"world"}')['max_limit_state'] == 'unknown'
    result = core.inspect_capability(b'{"name":"daily","enabled":true,"methods":["GET"],"max_limit":null}')
    assert result['max_limit_state'] == 'null' and result['max_limit'] is None


class API:
    def __init__(self):
        self.calls = []
        self.private = True
        self.duplicate = False
        self.ci_ok = True
        self.head = SHA
        # A moved head that no longer reaches this run's code; tests that mean a
        # plain merge point this at an ahead verdict instead.
        self.ancestry = {'status': 'diverged', 'merge_base_commit': {'sha': 'b' * 40}}
    def request(self, method, path):
        assert method == 'GET'
        self.calls.append(path)
        if path == host.PREFIX:
            return {'id': 1420041744, 'full_name': host.REPO, 'private': self.private,
                    'archived': False, 'default_branch': 'main'}
        if path.endswith('/git/ref/heads/main'):
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': self.head}}
        if '/compare/' in path:
            return self.ancestry
        current = {'id': 123, 'head_sha': SHA, 'head_branch': 'main', 'run_attempt': 1,
                   'event': 'workflow_dispatch', 'path': host.WORKFLOW, 'status': 'in_progress',
                   'display_title': host.TITLE, 'repository': {'id': 1420041744}}
        if path.endswith('/actions/runs/123'): return current
        if '/actions/workflows/ci.yml/runs?' in path:
            return {'workflow_runs': [{'id': 120, 'head_sha': SHA, 'head_branch': 'main',
                    'event': 'push', 'path': '.github/workflows/ci.yml', 'status': 'completed',
                    'conclusion': 'success' if self.ci_ok else 'failure'}]}
        if '/actions/workflows/relay-source-runtime.yml/runs?' in path:
            return {'workflow_runs': [current] + ([{**current, 'id': 122}] if self.duplicate else [])}
        raise AssertionError(path)


def inputs(tmp_path):
    env = {'GITHUB_REPOSITORY': host.REPO, 'GITHUB_REF': 'refs/heads/main',
           'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_WORKFLOW_REF':
           host.REPO + '/' + host.WORKFLOW + '@refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1',
           'GITHUB_SHA': SHA, 'GITHUB_RUN_ID': '123', 'RUNNER_TEMP': str(tmp_path)}
    event = {'inputs': {'code_sha': SHA, 'family': host.FAMILY, 'operation': 'check',
                       'access_confirmed': True, 'public_rights_confirmed': False,
                       'source_plan': json.dumps({'study': core.STUDY})}}
    return event, env


class Response:
    def __init__(self, raw, url):
        self.raw, self.url = raw, url
        self.status, self.headers = 200, {'Content-Type': 'application/json', 'Content-Length': str(len(raw))}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def geturl(self): return self.url
    def read(self, size): return self.raw[:size]


class Wire:
    def __init__(self, fail=None, reflect=False):
        self.calls, self.fail, self.reflect = [], fail, reflect
    def open(self, request, timeout):
        assert timeout == 90
        self.calls.append(request.full_url)
        if len(self.calls) == self.fail: raise TimeoutError('synthetic unreadable body')
        if '/capabilities/daily' in request.full_url:
            raw = b'{"name":"daily","enabled":true,"methods":["GET"],"max_limit":null}'
        else:
            query = parse_qs(urlparse(request.full_url).query)
            raw = wire(rows(6001 if 'start_date' in query else 5001,
                            core.DAYS if 'start_date' in query else ('20261016',)))
        if self.reflect: raw = b'{"token":"synthetic-bulk-secret"}'
        return Response(raw, request.full_url)


def runner(tmp_path, *, fail=None, reflect=False):
    event, env = inputs(tmp_path)
    api, source = API(), Wire(fail, reflect)
    out = tmp_path / 'capture'
    def factory(**kwargs):
        return RelayHTTP(opener=source, credential='synthetic-bulk-secret', **kwargs)
    assert host.execute('prepare', event, env, out, api=api, client_factory=factory) == 0
    return event, env, api, source, out, factory


def test_original_transport_performs_four_fixed_requests_and_no_store_writes(tmp_path):
    event, env, api, source, out, factory = runner(tmp_path)
    sleeps = []
    assert host.execute('capture', event, env, out, api=api, client_factory=factory, sleep=sleeps.append) == 0
    result = json.loads((out / 'result.json').read_bytes())
    assert len(source.calls) == result['source_requests'] == result['transport_requests'] == 4
    assert sleeps == [30, 30, 30] and not result['records_written']
    assert result['results'][-1]['finding']['returned_above_6000']
    for row in result['results']:
        raw = (out / row['raw_file']).read_bytes()
        assert len(raw) == row['bytes'] and sha256(raw).hexdigest() == row['sha256']
    with pytest.raises(RecordError, match='OUTPUT_ALREADY_CONSUMED'):
        host.execute('capture', event, env, out, api=api, client_factory=factory)
    assert len(source.calls) == 4


@pytest.mark.parametrize('failure', [1, 2, 3, 4])
def test_first_transport_failure_stops_remaining_experiments_without_retry(tmp_path, failure):
    event, env, api, source, out, factory = runner(tmp_path, fail=failure)
    assert host.execute('capture', event, env, out, api=api, client_factory=factory, sleep=lambda _: None) == 2
    result = json.loads((out / 'result.json').read_bytes())
    assert len(source.calls) == failure and result['transport_requests'] == failure
    assert result['results'][failure - 1]['status'] == 'stopped'
    assert all(r['status'] == 'not-requested' for r in result['results'][failure:])


def test_secret_reflection_never_releases_body(tmp_path):
    event, env, api, source, out, factory = runner(tmp_path, reflect=True)
    assert host.execute('capture', event, env, out, api=api, client_factory=factory, sleep=lambda _: None) == 2
    assert len(source.calls) == 1
    assert all(b'synthetic-bulk-secret' not in p.read_bytes() for p in out.iterdir())


@pytest.mark.parametrize('fault', ['private', 'duplicate', 'ci', 'head'])
def test_native_permission_identity_or_previous_attempt_stops_before_source(tmp_path, fault):
    event, env = inputs(tmp_path); api = API()
    if fault == 'private': api.private = False
    if fault == 'duplicate': api.duplicate = True
    if fault == 'ci': api.ci_ok = False
    if fault == 'head': api.head = 'b' * 40
    def forbidden(**kwargs): raise AssertionError('source must not be constructed')
    with pytest.raises(RecordError):
        host.execute('prepare', event, env, tmp_path / 'denied', api=api, client_factory=forbidden)
    assert not (tmp_path / 'denied').exists()


def test_private_reversal_withholds_all_raw(tmp_path):
    event, env, api, source, out, factory = runner(tmp_path)
    original = source.open
    def flipped(*args, **kwargs):
        response = original(*args, **kwargs)
        api.private = False
        return response
    source.open = flipped
    assert host.execute('capture', event, env, out, api=api, client_factory=factory, sleep=lambda _: None) == 2
    assert len(source.calls) == 1
    assert sorted(p.name for p in out.iterdir()) == ['request.json', 'result.json']


@pytest.mark.parametrize('field,value', [('access_confirmed', False), ('access_confirmed', 1), ('public_rights_confirmed', 0), ('public_rights_confirmed', True),
    ('operation', 'capture'), ('codes', '000001.SZ'), ('page_size', '12000'), ('code_sha', 'b'*40)])
def test_no_generic_selector_or_authorization_escape(tmp_path, field, value):
    event, env = inputs(tmp_path); event['inputs'][field] = value
    with pytest.raises(RecordError): host.settings(event, env)


def test_changed_prepared_plan_and_symlink_are_rejected(tmp_path):
    event, env, api, source, out, factory = runner(tmp_path)
    p = out / 'request.json'; p.write_text('{}')
    with pytest.raises(RecordError): host.execute('capture', event, env, out, api=api, client_factory=factory)
    assert source.calls == []
    p.unlink(); p.symlink_to(tmp_path / 'absent')
    with pytest.raises(RecordError): host.execute('capture', event, env, out, api=api, client_factory=factory)


def test_existing_workflow_keeps_default_routes_and_serialization():
    import yaml
    root = Path(__file__).parents[1]
    data = yaml.safe_load((root / host.WORKFLOW).read_text())
    trigger = data.get('on', data.get(True))
    assert 'schedule' not in trigger
    assert data['concurrency'] == {'group': 'decision-os-relay-source', 'cancel-in-progress': False}
    assert data['jobs']['bulk-check']['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert "inputs.family != 'market-bulk-check'" in data['jobs']['source']['if']
    assert set(trigger['workflow_dispatch']['inputs']['family']['options']) >= {
        'financial', 'participants', 'research', 'industry-pmi', host.FAMILY}
