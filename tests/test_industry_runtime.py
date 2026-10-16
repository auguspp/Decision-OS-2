"""Existing native research host with synthetic Git; not live source/issuer acceptance."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest
import yaml

from decision_kernel.application.industry_operations import read_industry_operations
from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.vehicle_flows import read_vehicle_flows
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, Selection, SourceRef, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import industry_commands as commands, research_runtime as runtime
from test_research_runtime import CODE, host, inputs, invoke, native_environment
from test_vehicle_flows import COMPANY, SUBJECT, retain as retain_vehicles
from test_industry_operations import retain as retain_operations

OP = commands.OPERATION
DUMMY = asdict(SourceRef(BlobRef('example/private-store', 'b' * 40,
    'source-views/sample/' + 'c' * 64 + '/manifest.json', 'd' * 64)))


def payload(reference=DUMMY, view='vehicle-flows', **changes):
    return dict(reference=deepcopy(reference), view=view,
                public_sources_custody_confirmed=True, **changes)


@pytest.fixture
def vehicle(host):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root, sources, issuer = retain_vehicles(writer)
    host.requests.clear()
    return root, sources, issuer


@pytest.mark.parametrize('change', [
    {'view': 'delete'}, {'view': []}, {'reference': 'https://example.invalid/source'},
    {'reference': None}, {'public_sources_custody_confirmed': False},
    {'public_sources_custody_confirmed': 'true'}, {'public_sources_custody_confirmed': 1},
    {'subject_id': '../outside'}, {'subject_id': ''}, {'company_id': True},
    {'subject_id': 'industry', 'company_id': 'company'},
    {'view': 'operating-cycle', 'company_id': 'company'},
    {'url': 'https://example.invalid'}, {'code': 'print(1)'}, {'latest': True},
    {'reference': {**DUMMY, 'selection': {'kind': 'lines', 'start': 1, 'end': 1}}},
])
def test_preflight_rejects_before_store_or_public_artifacts(host, tmp_path, change):
    chosen = payload(); chosen.update(change)
    with pytest.raises(RecordError):
        invoke(host, tmp_path / 'out', OP, chosen)
    assert not host.requests and host.writes == 0 and not (tmp_path / 'out').exists()


@pytest.mark.parametrize('missing', ['reference', 'view', 'public_sources_custody_confirmed'])
def test_required_fields_not_guessed(host, tmp_path, missing):
    chosen = payload(); chosen.pop(missing)
    with pytest.raises(RecordError, match='INDUSTRY_HOST_PAYLOAD'):
        invoke(host, tmp_path / 'out', OP, chosen)
    assert not host.requests and not (tmp_path / 'out').exists()


def test_read_operation_never_accepts_write_fields(host, tmp_path):
    with pytest.raises(RecordError, match='UNEXPECTED_WRITE_FIELDS'):
        invoke(host, tmp_path / 'out', OP, payload(), 'not-a-write', 'a' * 40)
    assert not host.requests and not (tmp_path / 'out').exists()


@pytest.mark.parametrize('view', ['industry', 'vehicle-flows'])
def test_host_returns_original_consumer_after_exact_read(host, vehicle, tmp_path, view):
    root, _, _ = vehicle
    reader = VersionedStore(GitHubStore(host.binding, host))
    direct = (read_industry if view == 'industry' else read_vehicle_flows)(root, reader)
    host.requests.clear(); before, writes = host.git.head(), host.writes
    result = invoke(host, tmp_path / 'read', OP, payload(asdict(root), view))
    assert result['status'] == 'completed' and result['write'] == 'not-requested'
    assert encoded(result['result']) == encoded(direct)
    assert host.git.head() == before and host.writes == writes
    assert 0 < len(host.requests) < 512 and all(r[0] == 'GET' for r in host.requests)
    assert not result['research_executed'] and result['human_acceptance'] == 'not-confirmed'
    disk = json.loads((tmp_path / 'read/report.json').read_bytes())
    assert encoded(disk) == encoded(result)


def test_operating_consumer_uses_original_formulas_and_limits(host, tmp_path):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root, _, _, _ = retain_operations(writer)
    direct = read_industry_operations(root, VersionedStore(GitHubStore(host.binding, host)))
    before, writes = host.git.head(), host.writes
    result = invoke(host, tmp_path / 'read', OP, payload(asdict(root), 'operating-cycle'))
    assert encoded(result['result']) == encoded(direct)
    assert result['result']['entries'][0]['snapshots'][1]['calculations']['profit_margin_pct']['value'] == '5.00'
    assert result['write'] == 'not-requested' and host.git.head() == before and host.writes == writes


def test_company_and_subject_queries_keep_same_version_denominator_and_unknowns(host, vehicle, tmp_path):
    root, _, _ = vehicle
    outputs = [invoke(host, tmp_path / str(i), OP, payload(asdict(root), **query))['result']
               for i, query in enumerate(({}, {'subject_id': SUBJECT.entity_id},
                       {'company_id': COMPANY.entity_id}, {'company_id': 'not-covered'}))]
    assert all(r['view_version'] == outputs[0]['view_version'] and r['scope'] == outputs[0]['scope'] for r in outputs)
    assert outputs[1]['relations'] == outputs[2]['relations'] and not outputs[3]['relations']
    relation = outputs[2]['relations'][0]
    assert relation['applicability_at_cutoff'] == 'historical'
    assert relation['current_business_freshness'] == 'not-revalidated'
    assert all(relation[k] is None for k in ('business_share', 'elasticity', 'pass_through', 'lag', 'net_benefit'))


def lose_month(monkeypatch, sources):
    original = runtime._CommandStore._read
    def read(self, commit, path, **kw):
        if path == sources[1].manifest.path:
            raise MissingObject('OBJECT_MISSING')
        return original(self, commit, path, **kw)
    monkeypatch.setattr(runtime._CommandStore, '_read', read)


def test_native_main_partial_is_nonzero_with_good_branches_preserved(host, vehicle, tmp_path, monkeypatch, capsys):
    root, sources, _ = vehicle
    lose_month(monkeypatch, sources)
    native_environment(monkeypatch, host, tmp_path, inputs(OP, payload(asdict(root))))
    writes, before = host.writes, host.git.head()
    assert runtime.main(['--out', str(tmp_path / 'out')]) == 2
    console = capsys.readouterr()
    assert 'Synthetic passenger cars' not in console.out and console.err == ''
    report = json.loads((tmp_path / 'out/report.json').read_bytes())
    assert report['status'] == 'industry-reading-partial' and report['write'] == 'not-requested'
    result = report['result']
    assert result['status'] == 'partial' and result['scope']['unavailable_vehicle_points'] == 8
    assert result['entries'][0]['points'][2]['value'] == '121'
    assert result['relations'] and host.writes == writes and host.git.head() == before


def test_scope_exit_revocation_never_leaks_intermediate_result(host, vehicle, tmp_path, monkeypatch):
    from decision_kernel.application import vehicle_flows as module
    root, _, _ = vehicle; interpret = module.vehicle_flows
    def revoke(value, **kwargs):
        out = interpret(value, **kwargs); host.meta['private'] = True; return out
    monkeypatch.setattr(module, 'vehicle_flows', revoke)
    writes = host.writes
    with pytest.raises(RecordError, match='PUBLIC_BINDING_MISMATCH'):
        invoke(host, tmp_path / 'out', OP, payload(asdict(root)))
    report = json.loads((tmp_path / 'out/report.json').read_bytes())
    assert report['status'] == 'failed' and 'result' not in report and host.writes == writes


def test_bad_root_digest_is_not_replaced_with_latest(host, vehicle, tmp_path):
    root, _, _ = vehicle
    broken = asdict(root); broken['manifest']['sha256'] = '0' * 64
    with pytest.raises(RecordError, match='BLOB_DIGEST_MISMATCH'):
        invoke(host, tmp_path / 'out', OP, payload(broken))
    report = json.loads((tmp_path / 'out/report.json').read_bytes())
    assert report['status'] == 'failed' and 'result' not in report


def test_result_bound_rejects_without_truncating_or_writing(host, vehicle, tmp_path, monkeypatch):
    root, _, _ = vehicle; writes = host.writes
    monkeypatch.setattr(runtime, 'MAX_RESULT_BYTES', 4096)
    with pytest.raises(RecordError, match='COMMAND_RESULT_TOO_LARGE'):
        invoke(host, tmp_path / 'out', OP, payload(asdict(root)))
    report = json.loads((tmp_path / 'out/report.json').read_bytes())
    assert report['status'] == 'failed' and 'result' not in report and host.writes == writes


def test_original_source_command_does_not_acquire_new_required_flag(host, vehicle, tmp_path):
    root, _, _ = vehicle
    commands.preflight('read-source', {})
    result = invoke(host, tmp_path / 'out', 'read-source', asdict(root))
    assert result['status'] == 'completed' and json.loads(result['result']['data'])['schema'] == 'industry-reading-v1'


def test_workflow_adds_read_choice_without_write_permission_or_new_job():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / runtime.WORKFLOW).read_text())
    options = workflow.get('on', workflow.get(True))['workflow_dispatch']['inputs']['operation']['options']
    assert options.count(OP) == 1 and OP in runtime.READ_OPERATIONS
    assert OP not in runtime.WRITE_OPERATIONS and OP not in runtime.RECONCILE_OPERATIONS
    assert set(workflow['jobs']) == {'read', 'write'}
    assert workflow['jobs']['read']['permissions']['contents'] == 'read'
    assert OP not in workflow['jobs']['write']['if']
