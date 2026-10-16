"""Offline CLI handoff consumes an existing capture result, never fetches or overwrites."""
import json
from pathlib import Path

import pytest

from decision_kernel.application.research_sources import validate_plan
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.interfaces import industry_pmi_cli as cli
from test_industry_pmi import AS_OF, SCOPE, captured
from test_research_sources import registry

REGISTRY = Path(__file__).resolve().parents[1] / 'docs/sources/relay-registry.json'


def arguments(operation, output, *extra):
    return ['--registry',str(REGISTRY),operation,'--start-month',SCOPE['start_month'],
            '--end-month',SCOPE['end_month'],'--output',str(output),*extra]


def no_network(*args, **kwargs):
    raise AssertionError('offline reader constructed a network client')


def test_plan_is_directly_consumable_by_existing_capture(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(RelayHTTP, '__init__', no_network)
    output = tmp_path/'plan.json'
    assert cli.main(arguments('plan',output,'--case-id','synthetic-pmi')) == 0
    plan = json.loads(output.read_text())
    assert set(plan) == {'schema','case_id','requests'}
    checked = validate_plan(plan, registry())
    assert checked['requests'][0]['api'] == 'cn_pmi'
    assert checked['requests'][0]['parameters']['start_m'] == '202601'
    assert json.loads(capsys.readouterr().out)['network_requests'] == 0


def test_read_single_original_jsonl_line_without_network(tmp_path, monkeypatch, capsys):
    result, _, _ = captured()
    capture, output = tmp_path/'capture.jsonl', tmp_path/'reading.json'
    capture.write_bytes(encoded(result))
    monkeypatch.setattr(RelayHTTP, '__init__', no_network)
    assert cli.main(arguments('read',output,'--capture',str(capture),'--as-of',AS_OF)) == 0
    out = json.loads(output.read_text())
    assert out['status'] == 'read' and out['records_written'] is False
    assert json.loads(capsys.readouterr().out)['status'] == 'read'


def test_existing_output_never_overwritten(tmp_path, capsys):
    output = tmp_path/'plan.json'; output.write_text('unchanged')
    assert cli.main(arguments('plan',output,'--case-id','synthetic-pmi')) == 2
    assert output.read_text() == 'unchanged'
    assert 'PMI_INPUT_OR_OUTPUT_REJECTED' in capsys.readouterr().err


@pytest.mark.parametrize('raw', [b'{"private":"not-for-logs"}', b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff'])
def test_invalid_input_does_not_create_output_or_echo_prose(tmp_path, raw, capsys):
    capture, output = tmp_path/'capture.jsonl', tmp_path/'reading.json'
    capture.write_bytes(raw)
    assert cli.main(arguments('read',output,'--capture',str(capture),'--as-of',AS_OF)) == 2
    assert not output.exists() and 'not-for-logs' not in capsys.readouterr().err


def test_multiple_results_require_explicit_selection_not_silent_first(tmp_path):
    result, _, _ = captured()
    capture, output = tmp_path/'capture.jsonl', tmp_path/'reading.json'
    capture.write_bytes(encoded(result) * 2)
    assert cli.main(arguments('read',output,'--capture',str(capture),'--as-of',AS_OF)) == 2
    assert not output.exists()


def test_input_bound_is_enforced_before_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'MAX_CAPTURE_BYTES', 8)
    capture, output = tmp_path/'capture.jsonl', tmp_path/'reading.json'
    capture.write_bytes(b' ' * 9)
    assert cli.main(arguments('read',output,'--capture',str(capture),'--as-of',AS_OF)) == 2
    assert not output.exists()
