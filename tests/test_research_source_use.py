"""Synthetic native metadata, real Git/source reads; never claims a live company study."""
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
import base64
import json
from pathlib import Path
import zipfile

import pytest

from decision_kernel.application.research_evidence import ResearchEvidence
from decision_kernel.application.research_source_use import SourceUseAudit, execution_contract, acceptance_pending
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, digest, encoded
from decision_kernel.domain.research_work import ResearchRequest, ResearchQuestion
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.infrastructure.research_receipts import NativeResearchReceipts, download_archive
from decision_kernel.interfaces.research_payload import decode_payload, pack_payload

REPO, CODE = 'auguspp/Decision-OS-2', 'a' * 40


def request():
    return ResearchRequest('source-use-test', 'full', 'Synthetic full source-use acceptance, not investing',
                           (ResearchQuestion('cash', 'Explain cash'),))


def progress(*refs):
    return {'body': 'Synthetic analysis only', 'findings': [{'question_id': 'cash', 'status': 'open',
        'finding': 'Available material still needs economic analysis', 'remaining_kind': 'acquired-unanalysed',
        'next_action': 'Review economic meaning', 'evidence': list(range(len(refs)))}],
        'sources': [asdict(s) for s in refs], 'stop': 'continue', 'stop_reason': 'Not a concluded study',
        'limitations': ['Synthetic acceptance data, no financial conclusions']}


def plan(query='company'):
    return {'schema': 'research-evidence-plan-v1', 'case_id': request().case_id, 'queries': [
        {'query_id': 'company', 'question_id': 'cash', 'query': query, 'purpose': 'Synthetic source selection'}]}


class NativeAPI:
    """Model exact native GETs independently of the caller's proof dictionary."""
    def __init__(self):
        self.binding = GitHubBinding(REPO, 1234, 'records', 'b'*40, 'test', visibility='public')
        self.replies, self.archives, self.calls = {}, {}, []
        self.replies[''] = {'id': 1234, 'full_name': REPO, 'private': False}

    def request(self, method, path, payload=None):
        assert method == 'GET' and payload is None and path.startswith('/repos/' + REPO)
        self.calls.append(path)
        return deepcopy(self.replies[path.removeprefix('/repos/' + REPO)])

    def add(self, docs, family='pool', ident=100, conclusion='success', capture_step='success'):
        stream = BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in docs.items():
                raw = (b''.join(encoded(v) for v in value) if name.endswith('.jsonl') else encoded(value))
                archive.writestr(name, raw)
        raw = stream.getvalue(); self.archives[ident] = raw
        prefix, workflow = ('dos2-research', 'research-runtime.yml') if family == 'pool' else ('dos2-relay-source', 'relay-source-runtime.yml')
        self.replies[f'/actions/runs/{ident}/attempts/1'] = {'id': ident, 'run_attempt': 1, 'status': 'completed',
            'event': 'workflow_dispatch', 'head_branch': 'main', 'head_sha': CODE,
            'path': '.github/workflows/' + workflow, 'repository': {'id': 1234}, 'conclusion': conclusion,
            'run_started_at': '2026-10-16T00:00:00Z'}
        self.replies[f'/actions/artifacts/{ident}'] = {'id': ident, 'name': f'{prefix}-{ident}-1',
            'expired': False, 'size_in_bytes': len(raw), 'digest': 'sha256:' + digest(raw),
            'workflow_run': {'id': ident, 'repository_id': 1234, 'head_repository_id': 1234, 'head_sha': CODE}}
        jobs = ([{'name': 'read', 'conclusion': conclusion}, {'name': 'write', 'conclusion': 'skipped'}]
            if family == 'pool' else [{'name': 'source', 'conclusion': conclusion, 'steps': [{
                'name': 'Explicit source capture with the repository secret', 'conclusion': capture_step}]}])
        self.replies[f'/actions/runs/{ident}/attempts/1/jobs?per_page=100'] = {
            'total_count': len(jobs), 'jobs': [{'run_id': ident, 'status': 'completed', **j} for j in jobs]}
        return {'run_id': ident, 'run_attempt': 1, 'artifact_id': ident,
                'archive_base64': base64.b64encode(raw).decode()}

    def reader(self):
        return NativeResearchReceipts(self.binding, self,
            archive_loader=lambda repo, ident: self.archives[ident] if repo == REPO else pytest.fail('foreign repo'))


@pytest.fixture
def setup(tmp_path):
    backend = GitStore.init_synthetic(tmp_path / 'source-use.git')
    # A logical repository name for this synthetic native-metadata model; all bytes
    # remain in a temporary real bare Git repository, with synthetic origins.
    backend.repository = REPO
    return VersionedStore(backend), NativeAPI()


def add_source(store, ident='company', text='company synthetic cash'):
    return store.put_source(ident, SourceInput(ident, 'content', 'Synthetic source', 'synthetic:source-use', text))


def pool_proof(store, api, *, p=None, cursor=None, limit=8, ident=100, req=None, packed=False):
    req, p = req or request(), p or plan()
    page = ResearchEvidence(store).page(req, p, cursor=cursor, limit=limit)
    payload = {'request': asdict(req), 'evidence_plan': p, 'evidence_limit': limit}
    if cursor is not None:
        payload['evidence_cursor'] = cursor
    inputs = {'operation': 'prepare', 'code_sha': CODE,
              'payload': pack_payload(encoded(payload)) if packed else encoded(payload).decode()}
    report = {'schema': 'research-operation-v1', 'repository': REPO, 'branch': 'records', 'code_sha': CODE,
        'run_id': str(ident), 'run_attempt': '1', 'operation': 'prepare', 'status': 'completed',
        'write': 'not-requested', 'payload_sha256': digest(encoded(payload)),
        'result': {'request': asdict(req), 'evidence': page}}
    return api.add({'inputs.json': inputs, 'report.json': report}, ident=ident), page


def audit(store, api, paper=None, receipts=(), **changes):
    return SourceUseAudit(store, receipt_reader=api.reader(), payload_decoder=decode_payload).audit({
        'request': asdict(request()), 'progress': paper or progress(), 'receipts': list(receipts), **changes})


def test_report_and_method_without_actual_use_cannot_pass(setup):
    store, api = setup
    before = store.backend.head()
    result = audit(store, api)
    assert result['status'] == 'needs-action' and result['pool'] == result['relay'] == 'not-executed'
    assert 'NO_READABLE_ADOPTED_SOURCE' in result['gaps']
    assert result['full_product_acceptance'] == 'not-performed' and store.backend.head() == before
    assert api.calls == []
    contract = execution_contract(request())
    assert contract['required_for_full'] and contract['pool'] == 'not-executed'
    assert contract['required_actions'][0]['operation'] == 'prepare'
    assert contract['required_actions'][-1]['operation'] == 'audit-source-use'
    assert contract['partial_research_save_allowed']


def test_real_query_and_source_adoption_are_distinct_from_relay(setup):
    store, api = setup; ref = add_source(store)
    proof, page = pool_proof(store, api)
    result = audit(store, api, progress(ref), [proof])
    assert result['pool'] == 'queried' and result['relay'] == 'not-executed'
    assert result['sources'][0]['pool_match'] and result['sources'][0]['adopted_by_author']
    assert 'POOL_MATCHES_NOT_ADOPTED' not in result['gaps'] and result['status'] == 'needs-action'


def test_zero_match_is_executed_empty_not_skipped(setup):
    store, api = setup
    proof, _ = pool_proof(store, api)
    result = audit(store, api, receipts=[proof])
    assert result['pool'] == 'queried-empty' and 'POOL_USE_NOT_COMPLETE' not in result['gaps']


def test_reference_only_receipts_use_native_archive_reader(setup):
    store, api = setup; add_source(store)
    proof, _ = pool_proof(store, api)
    proof.pop('archive_base64')
    assert audit(store, api, receipts=[proof])['pool'] == 'queried'


def test_compressed_original_payload_checked_by_existing_decoder(setup):
    store, api = setup; add_source(store)
    proof, _ = pool_proof(store, api, packed=True)
    assert audit(store, api, receipts=[proof])['pool'] == 'queried'


def test_page_chain_requires_first_intermediate_and_last_pages(setup):
    store, api = setup
    for ident in ('a', 'b', 'c'): add_source(store, ident)
    a, page = pool_proof(store, api, limit=1)
    b, page2 = pool_proof(store, api, cursor=page['next_cursor'], limit=1, ident=101)
    c, _ = pool_proof(store, api, cursor=page2['next_cursor'], limit=1, ident=102)
    assert audit(store, api, receipts=[a, b, c])['pool'] == 'queried'
    assert audit(store, api, receipts=[a, c])['pool'] == 'query-partial'
    assert audit(store, api, receipts=[c])['pool'] == 'query-coverage-unproven'
    assert audit(store, api, receipts=[a, b])['pool'] == 'query-partial'


def test_native_query_failure_not_silently_zero(setup, monkeypatch):
    store, api = setup
    def fail(*a, **k): raise RecordError('QUERY_VERSION_LIMIT_EXCEEDED')
    monkeypatch.setattr(store, 'search', fail)
    proof, _ = pool_proof(store, api)
    result = audit(store, api, receipts=[proof])
    assert result['pool'] == 'query-failed' and not result['rejected_receipts']


def test_unadopted_sources_cannot_fill_acceptance(setup):
    store, api = setup; ref = add_source(store)
    proof, _ = pool_proof(store, api)
    paper = progress(ref); paper['findings'][0]['evidence'] = []
    result = audit(store, api, paper, [proof])
    assert result['sources'][0]['pool_match'] and not result['sources'][0]['adopted_by_author']
    assert 'POOL_MATCHES_NOT_ADOPTED' in result['gaps']


@pytest.mark.parametrize('target,field,value', [
    ('run', 'event', 'pull_request'), ('run', 'head_branch', 'other'),
    ('run', 'run_attempt', 2), ('run', 'path', '.github/workflows/ci.yml'),
    ('run', 'status', 'in_progress'), ('artifact', 'expired', True),
    ('artifact', 'digest', 'sha256:'+'0'*64), ('artifact', 'name', 'forged-name'),
])
def test_wrong_native_identity_or_digest_rejected(setup, target, field, value):
    store, api = setup; add_source(store); proof, _ = pool_proof(store, api)
    key = '/actions/runs/100/attempts/1' if target == 'run' else '/actions/artifacts/100'
    api.replies[key][field] = value
    result = audit(store, api, receipts=[proof])
    assert result['pool'] == 'not-executed' and result['rejected_receipts']


def test_plain_caller_success_and_unsigned_receipts_never_pass(setup):
    store, api = setup
    with pytest.raises(RecordError):
        audit(store, api, executed=True)
    result = SourceUseAudit(store).audit({'request': asdict(request()), 'progress': progress(),
        'receipts': [{'run_id': 100, 'run_attempt': 1, 'artifact_id': 100}]})
    assert result['status'] == 'needs-action' and result['rejected_receipts']


def test_explanation_does_not_turn_missing_execution_into_success(setup):
    store, api = setup
    result = audit(store, api, exceptions={'custody': 'No case publication permission', 'relay': 'Not applicable, author claim'})
    assert result['status'] == 'exceptions-require-review' and result['pool'] == result['relay'] == 'not-executed'
    assert result['gaps'] and result['publication'] == 'not-authorized-by-audit'


def test_duplicate_receipts_and_wrong_case_do_not_count(setup):
    store, api = setup; add_source(store); proof, _ = pool_proof(store, api)
    assert audit(store, api, receipts=[proof, proof])['rejected_receipts']
    from dataclasses import replace
    other = replace(request(), mandate='Different user scope')
    wrong, _ = pool_proof(store, api, req=other, ident=101)
    assert audit(store, api, receipts=[wrong])['pool'] == 'not-executed'


def test_native_write_job_or_missing_jobs_cannot_be_read_receipt(setup):
    store, api = setup; add_source(store); proof, _ = pool_proof(store, api)
    jobs = api.replies['/actions/runs/100/attempts/1/jobs?per_page=100']
    jobs['jobs'][1]['conclusion'] = 'success'
    assert audit(store, api, receipts=[proof])['rejected_receipts']
    jobs['total_count'] = 3
    assert audit(store, api, receipts=[proof])['rejected_receipts']


def test_unsafe_archive_is_rejected_even_if_digest_matches_native_metadata(setup):
    store, api = setup
    proof = api.add({'../escape.json': {}, 'inputs.json': {}, 'report.json': {}})
    assert audit(store, api, receipts=[proof])['rejected_receipts']


def relay_proof(store, api, *, operation='capture', ident=200, case_id=None, fail=False):
    # Same real registry/capture/export used by the product, synthetic HTTP only.
    from test_research_sources import registry, http, body, request as source_request, plan as source_plan, busy, STAMP
    from decision_kernel.application.research_sources import capture_plan, validate_plan, source_inputs
    p = {**source_plan(source_request()), 'case_id': case_id or request().case_id}
    rows = list(capture_plan(p, registry(), client=http(busy(403, 'forbidden') if fail else body())[0],
                            public_rights_confirmed=True, clock=lambda: STAMP)) if operation == 'capture' else []
    docs = {'request.json': {'family':'research', 'operation':operation, 'code_sha':CODE,
        'public_rights_confirmed': operation == 'capture', 'source_plan':p},
        'source-plan.json': p, 'plan.json': validate_plan(p, registry()),
        'result.json': {'code_sha':CODE, 'status': 'planned-not-captured' if operation == 'plan'
                       else 'capture-incomplete' if fail else 'capture-returned'}}
    if operation == 'capture': docs['capture.jsonl'] = rows
    proof = api.add(docs, family='relay', ident=ident, conclusion='failure' if fail else 'success',
                    capture_step='skipped' if operation == 'plan' else 'failure' if fail else 'success')
    refs = [store.put_source('relay-'+str(ident)+'-'+str(i), s)
            for i, s in enumerate(source_inputs(rows[0], registry()))] if rows and not fail else []
    return proof, refs


def test_relay_capture_and_pool_adoption_complete_usage_not_economic_acceptance(setup):
    store, api = setup
    relay, refs = relay_proof(store, api)
    pool, _ = pool_proof(store, api, p=plan('600519.SH'))
    result = audit(store, api, progress(*refs), [pool, relay])
    assert result['status'] == 'usage-demonstrated' and result['gaps'] == []
    assert result['sources'][0]['pool_match'] and result['sources'][0]['relay_capture_match']
    assert result['research_assessment'] == result['full_product_acceptance'] == 'not-performed'


def test_relay_plan_only_and_real_ref_without_capture_are_not_execution(setup):
    store, api = setup
    relay, _ = relay_proof(store, api, operation='plan')
    result = audit(store, api, receipts=[relay])
    assert result['relay'] == 'planned-not-captured' and result['status'] == 'needs-action'


def test_relay_denial_is_actual_incomplete_attempt_not_zero_or_not_executed(setup):
    store, api = setup
    relay, _ = relay_proof(store, api, fail=True)
    result = audit(store, api, receipts=[relay])
    assert result['relay'] == 'capture-incomplete'
    assert result['relay_results'][0]['state'] == 'incomplete' and not result['rejected_receipts']


def test_relay_cross_case_requires_explicit_reuse_reason_and_exact_source(setup):
    store, api = setup
    relay, refs = relay_proof(store, api, case_id='earlier-case')
    assert audit(store, api, progress(*refs), [relay])['rejected_receipts']
    result = audit(store, api, progress(*refs), [relay], reuse_run_ids=[200], exceptions={'relay':'Same retained query; check freshness and subject'})
    assert result['relay'] == 'captured' and result['sources'][0]['relay_capture_match']
    assert result['receipts'][0]['stage'] == 'relay-reused-native-capture'


def test_relay_ref_without_author_adoption_does_not_pass(setup):
    store, api = setup
    relay, refs = relay_proof(store, api)
    pool, _ = pool_proof(store, api, p=plan('600519.SH'))
    paper = progress(*refs); paper['findings'][0]['evidence'] = []
    result = audit(store, api, paper, [pool, relay])
    assert 'RELAY_CAPTURE_NOT_ADOPTED' in result['gaps']


def test_native_downloader_never_inherits_engineering_auth_or_source_secret(monkeypatch):
    monkeypatch.setenv('DECISION_OS_STORE_TOKEN', 'synthetic-store-credential')
    monkeypatch.setenv('GH_TOKEN', 'engineering-do-not-use')
    monkeypatch.setenv('TUSHARE_PROXY_API_KEY', 'source-do-not-use')
    def run(command, **kwargs):
        assert command == ['gh', 'api', '--method', 'GET', f'repos/{REPO}/actions/artifacts/100/zip']
        assert kwargs['env']['GH_TOKEN'] == 'synthetic-store-credential'
        assert 'TUSHARE_PROXY_API_KEY' not in kwargs['env'] and 'shell' not in kwargs
        kwargs['stdout'].write(b'synthetic-archive')
        return type('Done', (), {'returncode': 0})()
    monkeypatch.setattr('decision_kernel.infrastructure.research_receipts.subprocess.run', run)
    assert download_archive(REPO, 100) == b'synthetic-archive'
