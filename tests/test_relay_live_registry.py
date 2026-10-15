"""Captured service metadata is not API execution, a live healthcheck, or authorization."""
from copy import deepcopy
from hashlib import sha256
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
REGISTRY = ROOT / 'docs/sources/relay-registry.json'


def copy_registry(tmp_path):
    shutil.copytree(REGISTRY.parent, tmp_path / 'sources')
    return tmp_path / 'sources/relay-registry.json'


def rewrite_snapshot(path, transform):
    data = json.loads(path.read_bytes()); spec = data['live_catalogue']
    raw_path = path.parent / spec['path']
    body = json.loads(raw_path.read_bytes())
    transform(body)
    raw = json.dumps(body, ensure_ascii=False).encode()
    raw_path.write_bytes(raw)
    spec.update(sha256=sha256(raw).hexdigest(), bytes=len(raw))
    receipt_path = path.parent / spec['receipt_path']
    receipt = json.loads(receipt_path.read_bytes())
    receipt.update(sha256=spec['sha256'], bytes=len(raw))
    receipt['transport']['wire_sha256'] = spec['sha256']
    rr = json.dumps(receipt).encode(); receipt_path.write_bytes(rr)
    spec['receipt_sha256'] = sha256(rr).hexdigest()
    path.write_text(json.dumps(data))


def test_real_catalogue_counts_changes_and_original_metadata_retained():
    reader = RelayRegistry(REGISTRY); summary = reader.summary()
    assert summary['registered'] == summary['captured_service_count'] == 298
    assert summary['enabled_at_capture'] == len(reader.select(enabled=True)) == 259
    assert summary['disabled_at_capture'] == len(reader.select(enabled=False)) == 39
    assert summary['unknown_at_capture'] == 0 and len(summary['added_vs_manual']) == 27
    assert summary['not_in_snapshot'] == [] and summary['status_differences'] == ['news_cctv']
    original = json.loads((REGISTRY.parent / reader.data['live_catalogue']['path']).read_bytes())
    for source in original['interfaces']:
        item = reader.lookup(source['name'])
        assert item['service_metadata'] == source
        assert item['snapshot']['sha256'] == 'f541ddd2efbe50fe0c77a47e49d00d6c8e6c949d24a31feb20c59520fd6111c1'
        assert item['snapshot']['captured_at'] == '2026-10-15T22:51:29.274280+00:00'
        assert item['execution_authority'] == 'not-granted-by-registry'
    assert reader.data['previous_catalogue_evidence'][0]['attempts'][-1]['error'] == 'RELAY_HTTP_429'


def test_real_descriptions_resolve_twelve_but_do_not_invent_the_member():
    reader = RelayRegistry(REGISTRY)
    assert reader.lookup('npr')['group'] == 'content-events'
    assert '国家政策库' in reader.lookup('npr')['service_metadata']['description']
    assert reader.lookup('wz_index')['group'] == 'macro-rates'
    assert reader.lookup('yc_cb')['group'] == 'fixed-income'
    assert reader.lookup('the_member')['service_metadata']['description'] == ''
    assert reader.lookup('the_member')['group'] == 'needs-review'
    assert reader.lookup('the_member')['api'] != 'ths_member'
    new = reader.lookup('hk_quote')
    assert new['manual_present'] is False and new['raw_row'] is None and new['source_line'] is None
    assert new['service_metadata']['description'] == '' and new['group'] == 'needs-review'
    assert new['shared_relay_client_admits_api'] is False
    news = reader.lookup('news_cctv')
    assert news['manual_enabled'] is True and news['live_enabled'] is False


@pytest.mark.parametrize('api,params,valid,reason', [
    ('income', {'ts_code':'600519.SH'}, True, None),
    ('income', {'period':'20251231'}, True, None),
    ('income', {'fields':'ts_code', 'limit':'5'}, False, 'required-alternative-unsatisfied'),
    ('income', {'ts_code':'  '}, False, 'required-alternative-unsatisfied'),
    ('top_list', {}, False, 'required-parameters-missing'),
    ('top_list', {'trade_date':'20261015'}, True, None),
    ('news_cctv', {}, False, 'disabled-or-unknown-at-capture'),
    ('p_get', {'portfolio_id':'example'}, False, 'private-or-write-scope-not-admitted'),
    ('p_delete', {'portfolio_id':'example'}, False, 'private-or-write-scope-not-admitted'),
])
def test_gateway_preflight_does_not_dispatch_or_grant_permission(api, params, valid, reason, monkeypatch):
    monkeypatch.delenv('TUSHARE_PROXY_API_KEY', raising=False)
    result = RelayRegistry(REGISTRY).check_parameters(api, params)
    assert result['gateway_requirements_satisfied'] is valid
    if reason: assert reason in result['reasons']
    assert result['dispatch_performed'] is False and result['execution_authority'] == 'not-granted-by-registry'
    assert 'not-upstream-fields-values-coverage-or-permission' in result['qualification']


def test_actual_multi_required_parameters_and_method():
    reader = RelayRegistry(REGISTRY)
    fail = reader.check_parameters('major_news', {'src':'cls', 'start_date':'20261001'})
    assert fail['missing_required'] == ['end_date']
    assert reader.check_parameters('major_news', {'src':'cls','start_date':'20261001','end_date':'20261015'})['gateway_requirements_satisfied']
    assert not reader.check_parameters('income', {'ts_code':'600519.SH'}, method='POST')['gateway_requirements_satisfied']
    # Requirements are not value-type or upstream field validation.
    limited = reader.check_parameters('income', {'ts_code':'not-a-validated-security'})
    assert limited['gateway_requirements_satisfied'] and limited['dispatch_performed'] is False


@pytest.mark.parametrize('mutation', ['raw', 'receipt', 'count', 'enabled-count', 'path', 'override'])
def test_snapshot_integrity_rejected(tmp_path, mutation):
    path = copy_registry(tmp_path); data = json.loads(path.read_bytes()); spec = data['live_catalogue']
    if mutation == 'raw': (path.parent/spec['path']).write_bytes(b'corrupted')
    if mutation == 'receipt': (path.parent/spec['receipt_path']).write_bytes(b'{}')
    if mutation == 'count': spec['count'] = 297
    if mutation == 'enabled-count': spec['enabled_count'] = 258
    if mutation == 'path': spec['path'] = '../untrusted.gz'
    if mutation == 'override': data['live_group_overrides']['the_member'] = 'indices-sectors'
    path.write_text(json.dumps(data))
    with pytest.raises(RecordError): RelayRegistry(path)


@pytest.mark.parametrize('mutation', ['duplicate', 'count', 'boolean', 'fields', 'alternatives'])
def test_actual_schema_validation_beyond_hashes(tmp_path, mutation):
    path = copy_registry(tmp_path)
    def transform(body):
        if mutation == 'duplicate': body['interfaces'][1] = body['interfaces'][0]
        if mutation == 'count': body['count'] = 297
        if mutation == 'boolean': body['interfaces'][0]['enabled'] = 1
        if mutation == 'fields': body['interfaces'][0]['required'] = 'ts_code'
        if mutation == 'alternatives': body['interfaces'][0]['required_any'] = [[]]
    rewrite_snapshot(path, transform)
    with pytest.raises(RecordError): RelayRegistry(path)


def test_missing_null_and_empty_constraints_are_separate(tmp_path):
    path = copy_registry(tmp_path)
    def transform(body):
        row = body['interfaces'][0]
        row.pop('required'); row['required_any'] = None; row['methods'] = []
    rewrite_snapshot(path, transform)
    reader = RelayRegistry(path); item = reader.lookup('daily')
    assert item['machine_field_presence']['required'] == 'missing'
    assert item['machine_field_presence']['required_any'] == 'null'
    assert item['machine_field_presence']['methods'] == 'value' and item['live_methods'] == []
    assert not reader.check_parameters('daily', {'ts_code':'600519.SH'})['gateway_requirements_satisfied']


def test_receipt_identity_not_relabelled_with_updated_hash(tmp_path):
    path = copy_registry(tmp_path); data = json.loads(path.read_bytes()); spec = data['live_catalogue']
    rp = path.parent / spec['receipt_path']; receipt = json.loads(rp.read_bytes())
    receipt['code_sha'] = 'f' * 40
    rr = json.dumps(receipt).encode(); rp.write_bytes(rr); spec['receipt_sha256'] = sha256(rr).hexdigest()
    path.write_text(json.dumps(data))
    with pytest.raises(RecordError, match='IDENTITY'): RelayRegistry(path)


def test_raw_size_bound(tmp_path):
    path = copy_registry(tmp_path); data = json.loads(path.read_bytes()); spec = data['live_catalogue']
    raw = b' ' * (2 * 1024 * 1024 + 1)
    (path.parent/spec['path']).write_bytes(raw)
    spec.update(bytes=len(raw), sha256=sha256(raw).hexdigest())
    path.write_text(json.dumps(data))
    with pytest.raises(RecordError, match='FILE_TOO_LARGE'): RelayRegistry(path)


def test_archived_v1_registry_still_reads_without_live_claims(tmp_path):
    path = copy_registry(tmp_path); data = json.loads(path.read_bytes())
    data['schema'] = 'relay-functional-registry-v1';data['basis'] = 'archived-manual-not-live-capabilities'
    data['live_catalogue'] = data['previous_catalogue_evidence'][0]
    path.write_text(json.dumps(data)); shutil.rmtree(path.parent/'snapshots')
    reader = RelayRegistry(path)
    assert len(reader.select()) == 271 and reader.lookup('income')['live_enabled'] is None
    assert reader.lookup('npr')['group'] == 'needs-review'
    assert reader.select(enabled=True) == []
    assert reader.check_parameters('income', {'ts_code':'600519.SH'})['gateway_requirements_satisfied'] is False


def test_new_cli_summary_filters_and_copy_isolation(capsys):
    args = ['--registry', str(REGISTRY)]
    assert main(args+['--summary']) == 0
    assert json.loads(capsys.readouterr().out)['captured_service_count'] == 298
    assert main(args+['--disabled']) == 0
    assert len(json.loads(capsys.readouterr().out)) == 39
    assert main(args+['--summary','--enabled']) == 2
    capsys.readouterr()
    reader = RelayRegistry(REGISTRY); item = reader.lookup('income')
    item['service_metadata']['required_any'][0][0] = 'edited'
    assert reader.lookup('income')['required_any'][0] == ['ts_code']


def test_outside_repository_real_snapshot_without_secret(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')};env.pop('TUSHARE_PROXY_API_KEY', None)
    run = subprocess.run([sys.executable,'-m','decision_kernel.interfaces.relay_registry_cli',
        '--registry',str(REGISTRY),'--summary'],cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)['enabled_at_capture'] == 259


def test_truncated_json_is_explicit_record_error(tmp_path):
    path = copy_registry(tmp_path);data = json.loads(path.read_bytes());spec = data['live_catalogue']
    raw = b'{'
    (path.parent/spec['path']).write_bytes(raw)
    spec.update(sha256=sha256(raw).hexdigest(), bytes=len(raw))
    path.write_text(json.dumps(data))
    with pytest.raises(RecordError): RelayRegistry(path)
