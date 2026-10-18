"""A separately authorized OHLCV probe: isolated synthetic wires, never live sources."""
import json
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

from decision_kernel.application import relay_bulk_check as core
from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces import relay_bulk_check_cli as host
from decision_kernel.infrastructure.relay_http import RelayHTTP
from test_relay_bulk_check import API, Response, inputs


def full_rows(n=2, days=('20261016',)):
    return [[f'{i // len(days):06d}.SZ', days[i % len(days)],
             '10.000', '11', '9', '10.50', '400.00', '420.99']
            for i in range(n)]


def full_wire(rows):
    return json.dumps({'code': 0, 'api_name': 'daily', 'count': len(rows),
                       'data': {'fields': core.FULL_FIELDS.split(','), 'items': rows}},
                      separators=(',', ':')).encode()


def test_original_one_shot_study_unchanged_and_full_plan_frozen():
    assert len(core.plan()) == core.MAX_CALLS == 4
    assert core.FIELDS == 'ts_code,trade_date,close'
    specs = core.full_plan()
    assert len(specs) == core.FULL_MAX_CALLS == 5
    assert [s['parameters'].get('limit') for s in specs] == [None, '1000', '5000', '6000', '12000']
    assert all(s['parameters'].get('fields') == core.FULL_FIELDS for s in specs[1:])
    assert specs[-1]['parameters']['start_date'] == core.DAYS[0]
    assert specs[-1]['parameters']['end_date'] == core.DAYS[1]


def test_full_fields_and_two_day_count_never_imply_market_admission():
    one = core.inspect_full_quote(full_wire(full_rows(5567)), core.full_plan()[3])
    assert one['rows'] == 5567 and one['returned_above_5000']
    assert not one['returned_above_6000'] and not one['market_qualification_granted']
    assert set(one['partitions']) == {'20261016'}
    both = core.inspect_full_quote(full_wire(full_rows(11132, core.DAYS)), core.full_plan()[4])
    assert both['rows'] == 11132 and both['returned_above_6000']
    assert [both['partitions'][d]['rows'] for d in core.DAYS] == [5566, 5566]
    assert both['close_rows_sha256'] == core.rows_digest(
        [[r[0], r[1], '10.5'] for r in full_rows(11132, core.DAYS)])
    assert both['full_rows_sha256'] != both['close_rows_sha256']
    assert both['quantity_units'] == 'supplier-vol-and-amount-unconverted'
    assert not both['market_qualification_granted']


def test_decimal_full_precision_and_zero_volume_does_not_invent_trading():
    original = full_rows(1)
    original[0][2] = '9.9999999999999999999999'
    original[0][4] = '9.9999999999999999999998'
    original[0][5] = '10.11111111111111111111111'
    original[0][6] = '0'
    original[0][7] = '0'
    with localcontext() as ctx:
        ctx.prec = 3
        actual = core.inspect_full_quote(full_wire(original), core.full_plan()[1])
    assert actual['rows'] == 1 and not actual['market_qualification_granted']
    # The full canonical row keeps the exact decimal regardless of ambient context.
    assert actual['full_rows_sha256'] == core.rows_digest(
        [[original[0][0], original[0][1], original[0][2], '11',
          original[0][4], original[0][5], '0', '0']])


@pytest.mark.parametrize('fault', ['duplicate', 'bad-day', 'bad-code', 'bad-open', 'bad-high',
    'low-above-close', 'negative-vol', 'negative-amount', 'nan-amount', 'no-volume',
    'unknown-field', 'code-error', 'too-many', 'missing-range'])
def test_full_input_rejections(fault):
    spec = core.full_plan()[1]
    obj = json.loads(full_wire(full_rows(2)))
    items = obj['data']['items']
    if fault == 'duplicate': items[1] = list(items[0])
    elif fault == 'bad-day': items[0][1] = '20261015'
    elif fault == 'bad-code': items[0][0] = 'outside'
    elif fault == 'bad-open': items[0][2] = '0'
    elif fault == 'bad-high': items[0][3] = '8'
    elif fault == 'low-above-close': items[0][4] = '11'
    elif fault == 'negative-vol': items[0][6] = '-1'
    elif fault == 'negative-amount': items[0][7] = '-0.01'
    elif fault == 'nan-amount': items[0][7] = 'NaN'
    elif fault == 'no-volume': items[0].pop(6)
    elif fault == 'unknown-field': obj['data']['fields'][6] = 'last'
    elif fault == 'code-error': obj['code'] = 403
    elif fault == 'too-many': obj = json.loads(full_wire(full_rows(1001)))
    elif fault == 'missing-range': obj['data']['fields'][3] = 'higest'
    with pytest.raises((RecordError, ValueError)):
        core.inspect_full_quote(json.dumps(obj).encode(), spec)


def test_full_spec_cannot_be_used_as_original_old_approval():
    with pytest.raises(RecordError, match='BULK_REQUEST_OUTSIDE_STUDY'):
        core.inspect_quote(full_wire(full_rows(1)), core.full_plan()[1])
    with pytest.raises(RecordError, match='BULK_REQUEST_OUTSIDE_STUDY'):
        core.inspect_full_quote(full_wire(full_rows(1)), core.plan()[1])


class FullAPI(API):
    def request(self, method, path):
        item = super().request(method, path)
        if path.endswith('/actions/runs/123'):
            item['display_title'] = host.FULL_TITLE
        elif '/actions/workflows/relay-source-runtime.yml/runs?' in path:
            for run in item['workflow_runs']:
                run['display_title'] = host.FULL_TITLE
        return item


class FullWire:
    def __init__(self, fail=None, wrong_field=False):
        self.calls, self.fail, self.wrong_field = [], fail, wrong_field

    def open(self, request, timeout):
        assert timeout == 90
        self.calls.append(request.full_url)
        if len(self.calls) == self.fail:
            raise TimeoutError('synthetic transport failure')
        if '/capabilities/daily' in request.full_url:
            raw = b'{"name":"daily","enabled":true,"methods":["GET"],"max_limit":null}'
        else:
            params = parse_qs(urlparse(request.full_url).query)
            days = core.DAYS if 'start_date' in params else ('20261016',)
            raw = full_wire(full_rows(2 * len(days), days))
        return Response(raw, request.full_url)


def make(tmp_path, *, fail=None, prior_title=False):
    event, env = inputs(tmp_path)
    event['inputs']['family'] = host.FULL_FAMILY
    event['inputs']['source_plan'] = json.dumps({'study': core.FULL_STUDY})
    api = API() if prior_title else FullAPI()
    source = FullWire(fail=fail)
    def factory(**kwargs):
        return RelayHTTP(opener=source, credential='synthetic-bulk-secret', **kwargs)
    return event, env, api, source, factory, tmp_path / 'result'


def test_real_host_full_study_isolated_five_requests_no_store_write(tmp_path):
    event, env, api, source, factory, out = make(tmp_path)
    assert host.execute('prepare', event, env, out, api=api, client_factory=factory) == 0
    slept = []
    assert host.execute('capture', event, env, out, api=api,
                        client_factory=factory, sleep=slept.append) == 0
    report = json.loads((out / 'result.json').read_bytes())
    assert report['study'] == core.FULL_STUDY
    assert report['status'] == 'completed-diagnostic-not-production'
    assert report['source_requests'] == report['transport_requests'] == len(source.calls) == 5
    assert slept == [30, 30, 30, 30]
    assert report['records_written'] is False and report['market_qualification_granted'] is False
    assert report['results'][-1]['finding']['rows'] == 4
    assert report['results'][1]['finding']['fields'] == core.FULL_FIELDS.split(',')
    with pytest.raises(RecordError, match='OUTPUT_ALREADY_CONSUMED'):
        host.execute('capture', event, env, out, api=api, client_factory=factory)
    assert len(source.calls) == 5


@pytest.mark.parametrize('at', [1, 2, 3, 4, 5])
def test_full_failure_stops_and_no_followup_calls(tmp_path, at):
    event, env, api, source, factory, out = make(tmp_path, fail=at)
    assert host.execute('prepare', event, env, out, api=api, client_factory=factory) == 0
    assert host.execute('capture', event, env, out, api=api,
                        client_factory=factory, sleep=lambda _: None) == 2
    report = json.loads((out / 'result.json').read_bytes())
    assert len(source.calls) == at and report['transport_requests'] == at
    assert report['results'][at - 1]['status'] == 'stopped'
    assert all(row['status'] == 'not-requested' for row in report['results'][at:])


def test_native_identity_and_one_shot_not_aliased_to_original_study(tmp_path):
    event, env, api, source, factory, out = make(tmp_path, prior_title=True)
    with pytest.raises(RecordError, match='BULK_NATIVE_RUN_IDENTITY'):
        host.execute('prepare', event, env, out, api=api, client_factory=factory)
    assert source.calls == [] and not out.exists()


def test_wrong_study_rejected_before_source_and_no_dynamic_urls(tmp_path):
    event, env, api, source, factory, out = make(tmp_path)
    event['inputs']['source_plan'] = json.dumps({'study': core.STUDY})
    with pytest.raises(RecordError, match='BULK_FIXED_STUDY'):
        host.execute('prepare', event, env, out, api=api, client_factory=factory)
    assert source.calls == [] and not out.exists()


def test_workflow_keeps_single_serial_group_and_original_route():
    import yaml
    workflow = yaml.safe_load((Path(__file__).parents[1] / host.WORKFLOW).read_text())
    trigger = workflow.get('on', workflow.get(True))
    assert host.FULL_FAMILY in trigger['workflow_dispatch']['inputs']['family']['options']
    assert workflow['concurrency']['group'] == 'decision-os-relay-source'
    assert workflow['jobs']['bulk-check']['permissions'] == {'contents': 'read', 'actions': 'read'}
    assert "inputs.family == 'market-bulk-check'" in workflow['jobs']['bulk-check']['if']
    assert "inputs.family == 'market-ohlcv-check'" in workflow['jobs']['bulk-check']['if']
    assert "inputs.family != 'market-ohlcv-check'" in workflow['jobs']['source']['if']
    assert 'schedule' not in trigger
