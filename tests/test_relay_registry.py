"""A byte-bound static index does not imply runtime availability or execution authority."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import os

import pytest
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import RecordError
from decision_kernel.interfaces.relay_registry_cli import main

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'docs/sources/relay-registry.json'


def test_all_documented_interfaces_once_with_original_rows():
    reader = RelayRegistry(REGISTRY)
    rows = [r for r in reader.select() if r['manual_present']]
    assert len(rows) == len({r['api'] for r in rows}) == 271
    assert sum(r['manual_enabled'] is True for r in rows) == 258
    assert sum(r['manual_enabled'] is False for r in rows) == 13
    assert all(type(r['live_enabled']) is bool and isinstance(r['required'], list) for r in rows)
    assert all(r['api'] in r['raw_row'] and r['source_line'] > 350 for r in rows)
    assert len(reader.groups) == 14


def test_three_distinct_evidence_layers():
    reader = RelayRegistry(REGISTRY)
    income = reader.lookup('income')
    assert income['manual_enabled'] and income['shared_relay_client_admits_api']
    assert income['live_enabled'] is True
    assert income['scoped_business_evidence']['nonempty'] == 3
    assert reader.lookup('forecast')['scoped_business_evidence']['nonempty'] == 0
    assert reader.lookup('daily')['scoped_business_evidence'] is None
    assert not reader.lookup('daily')['shared_relay_client_admits_api']
    assert reader.lookup('disclosure_date')['shared_relay_client_admits_api']
    assert reader.lookup('disclosure_date')['scoped_business_evidence'] is None


def test_private_portfolio_not_misclassified_as_ordinary_get():
    reader = RelayRegistry(REGISTRY)
    for name in ('p_get', 'p_list', 'p_save', 'p_delete'):
        item = reader.lookup(name)
        assert item['private_or_write_scope'] and not item['shared_relay_client_admits_api']
        assert item['execution_authority'] == 'not-granted-by-registry'
        assert not item['suggested_consumers']


def test_ambiguous_manual_cells_and_unknown_function_remain_unknown():
    reader = RelayRegistry(REGISTRY)
    row = reader.lookup('adj_factor')
    assert row['table_shape'] == 'ambiguous-see-original'
    assert row['provider_note'] is None and row['parameter_note'] is None
    assert reader.lookup('npr')['group'] == 'content-events'
    assert reader.lookup('the_member')['group'] == 'needs-review'
    assert reader.lookup('income')['parameter_note']


def test_consumers_receive_metadata_not_a_live_request(monkeypatch):
    monkeypatch.delenv('TUSHARE_PROXY_API_KEY', raising=False)
    reader = RelayRegistry(REGISTRY)
    assert len(reader.select(group='financial')) == 18
    assert all('#11' in row['suggested_consumers'] for row in reader.select(module='#11'))
    data = reader.lookup('income')
    data['scoped_business_evidence']['nonempty'] = 900
    assert reader.lookup('income')['scoped_business_evidence']['nonempty'] == 3


def copy_registry(tmp_path):
    (tmp_path/'vendor').mkdir()
    shutil.copy(REGISTRY.parent/'vendor/tushare-relay-v0.5.61.md', tmp_path/'vendor/')
    shutil.copytree(REGISTRY.parent/'snapshots', tmp_path/'snapshots')
    dest = tmp_path/'registry.json'
    shutil.copy(REGISTRY, dest)
    return dest


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'new-name', 'live', 'escape'])
def test_invalid_registry_does_not_silently_change_coverage(tmp_path, mutation):
    path = copy_registry(tmp_path)
    data = json.loads(path.read_bytes())
    if mutation == 'duplicate': data['groups'][0]['apis'].append('income')
    if mutation == 'missing': data['groups'][0]['apis'].remove('income')
    if mutation == 'new-name': data['groups'][0]['apis'].append('made_up_api')
    if mutation == 'live': data['live_catalogue']['enabled_count'] = 271
    if mutation == 'escape': data['manual']['path'] = '../outside.md'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(RecordError): RelayRegistry(path)


def test_changed_manual_and_duplicate_json_rejected(tmp_path):
    path = copy_registry(tmp_path)
    manual = tmp_path/'vendor/tushare-relay-v0.5.61.md'
    manual.write_bytes(manual.read_bytes() + b'changed')
    with pytest.raises(RecordError, match='MANUAL_CHANGED'): RelayRegistry(path)
    path.write_text('{"schema":1,"schema":2}', encoding='utf-8')
    with pytest.raises(RecordError, match='DUPLICATE_KEY'): RelayRegistry(path)


def test_cli_and_invalid_selector(capsys):
    assert main(['--registry', str(REGISTRY), '--api', 'income']) == 0
    assert json.loads(capsys.readouterr().out)['api'] == 'income'
    assert main(['--registry', str(REGISTRY), '--group', 'financial']) == 0
    assert len(json.loads(capsys.readouterr().out)) == 18
    assert main(['--registry', str(REGISTRY), '--api', 'not-found']) == 2
    assert 'NOT_FOUND' in capsys.readouterr().err


def test_cli_outside_repository_no_key(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')}
    env.pop('TUSHARE_PROXY_API_KEY', None)
    run = subprocess.run([sys.executable, '-m', 'decision_kernel.interfaces.relay_registry_cli',
        '--registry', str(REGISTRY), '--api', 'p_delete'], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False, timeout=15)
    assert run.returncode == 0, run.stderr
    item = json.loads(run.stdout)
    assert item['private_or_write_scope'] and item['live_enabled'] is True
