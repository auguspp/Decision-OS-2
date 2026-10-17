"""Actual pool pages and Git reads; synthetic native metadata, never live Full proof."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from decision_kernel.application.research_evidence import ResearchEvidence
from decision_kernel.application.research_source_use import SourceUseAudit
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, digest, encoded
from decision_kernel.domain.research_work import ResearchQuestion, ResearchRequest
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubBinding
from decision_kernel.infrastructure.research_receipts import NativeResearchReceipts
from decision_kernel.interfaces.research_payload import decode_payload

REPO, CODE = 'auguspp/Decision-OS-2', 'a' * 40
STAMP = '2026-10-16T00:00:00Z'


class PoolNativeModel:
    """Only native GET/archive responses; the actual receipt reader verifies them."""
    def __init__(self):
        self.binding = GitHubBinding(REPO, 1234, 'records', 'b' * 40, 'test', visibility='public')
        self.replies = {'': {'id': 1234, 'full_name': REPO, 'private': False}}
        self.archives = {}

    def request(self, method, path, payload=None):
        assert method == 'GET' and payload is None and path.startswith('/repos/' + REPO)
        return deepcopy(self.replies[path.removeprefix('/repos/' + REPO)])

    def add(self, ident, docs, started_at):
        stream = BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in docs.items():
                archive.writestr(name, encoded(value))
        raw = stream.getvalue()
        self.archives[ident] = raw
        self.replies[f'/actions/runs/{ident}/attempts/1'] = {
            'id': ident, 'run_attempt': 1, 'status': 'completed', 'event': 'workflow_dispatch',
            'head_branch': 'main', 'head_sha': CODE, 'path': '.github/workflows/research-runtime.yml',
            'repository': {'id': 1234}, 'conclusion': 'success', 'run_started_at': started_at}
        self.replies[f'/actions/artifacts/{ident}'] = {
            'id': ident, 'name': f'dos2-research-{ident}-1', 'expired': False,
            'size_in_bytes': len(raw), 'digest': 'sha256:' + digest(raw),
            'workflow_run': {'id': ident, 'repository_id': 1234, 'head_repository_id': 1234, 'head_sha': CODE}}
        self.replies[f'/actions/runs/{ident}/attempts/1/jobs?per_page=100'] = {
            'total_count': 2, 'jobs': [
                {'run_id': ident, 'status': 'completed', 'name': 'read', 'conclusion': 'success'},
                {'run_id': ident, 'status': 'completed', 'name': 'write', 'conclusion': 'skipped'}]}
        return {'run_id': ident, 'run_attempt': 1, 'artifact_id': ident}

    def reader(self):
        return NativeResearchReceipts(self.binding, self,
            archive_loader=lambda repo, ident: self.archives[ident] if repo == REPO else None)


class PoolRepaginationTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='dos2-pool-repagination-')
        self.addCleanup(directory.cleanup)
        backend = GitStore.init_synthetic(Path(directory.name) / 'sources.git')
        backend.repository = REPO
        self.store, self.native = VersionedStore(backend), PoolNativeModel()
        self.refs = [self.add_source(sid) for sid in ('a', 'b', 'c')]
        self.request = ResearchRequest('source-use-test', 'full', 'Synthetic source-use diagnostic',
                                       (ResearchQuestion('cash', 'Explain cash'),))
        self.plan = {'schema': 'research-evidence-plan-v1', 'case_id': self.request.case_id, 'queries': [
            {'query_id': 'company', 'question_id': 'cash', 'query': 'company', 'purpose': 'Synthetic selection'}]}

    def add_source(self, sid):
        return self.store.put_source(sid, SourceInput(sid, 'content', 'Synthetic company source',
                                     'synthetic:source-use', 'company material'))

    def page(self, ident, *, limit=8, cursor=None, failed=False, started_at=STAMP):
        failure = patch.object(self.store, 'search', side_effect=RecordError('REMOTE_REQUEST_LIMIT'))
        with failure if failed else nullcontext():
            value = ResearchEvidence(self.store).page(self.request, self.plan, cursor=cursor, limit=limit)
        payload = {'request': asdict(self.request), 'evidence_plan': self.plan, 'evidence_limit': limit}
        if cursor is not None:
            payload['evidence_cursor'] = cursor
        inputs = {'operation': 'prepare', 'code_sha': CODE, 'payload': encoded(payload).decode()}
        report = {'schema': 'research-operation-v1', 'repository': REPO, 'branch': 'records',
            'code_sha': CODE, 'run_id': str(ident), 'run_attempt': '1', 'operation': 'prepare',
            'status': 'completed', 'write': 'not-requested', 'payload_sha256': digest(encoded(payload)),
            'result': {'request': asdict(self.request), 'evidence': value}}
        return self.native.add(ident, {'inputs.json': inputs, 'report.json': report}, started_at), value

    def audit(self, proofs, refs=None):
        refs = self.refs if refs is None else refs
        progress = {'body': 'Synthetic material use only', 'findings': [
            {'question_id': 'cash', 'status': 'open', 'finding': 'Economic analysis remains',
             'remaining_kind': 'acquired-unanalysed', 'next_action': 'Review economic meaning',
             'evidence': list(range(len(refs)))}], 'sources': [asdict(ref) for ref in refs],
            'stop': 'continue', 'stop_reason': 'Not a concluded study', 'limitations': ['Synthetic diagnostic']}
        before = self.store.backend.head()
        result = SourceUseAudit(self.store, receipt_reader=self.native.reader(), payload_decoder=decode_payload).audit({
            'request': asdict(self.request), 'progress': progress, 'receipts': list(proofs)})
        self.assertEqual(result['rejected_receipts'], [])
        self.assertEqual(self.store.backend.head(), before)
        self.assertEqual(result['full_product_acceptance'], 'not-performed')
        return result

    def test_larger_page_replaces_old_complete_route_and_keeps_history(self):
        first, a = self.page(100, limit=1)
        middle, b = self.page(101, limit=1, cursor=a['next_cursor'])
        last, c = self.page(102, limit=1, cursor=b['next_cursor'])
        newer, n = self.page(103)
        self.assertEqual({p['snapshot'] for p in (a, b, c, n)}, {n['snapshot']})
        self.assertTrue(n['coverage']['query_plan_complete'])
        self.assertEqual(self.audit([first, middle, last])['pool'], 'queried')
        self.assertEqual(self.audit([newer])['pool'], 'queried')
        for proofs in ([first, middle, last, newer], [newer, last, middle, first]):
            with self.subTest(order=[p['run_id'] for p in proofs]):
                result = self.audit(proofs)
                self.assertEqual(result['pool'], 'queried')
                self.assertEqual(len(result['pool_history']), 4)
                self.assertNotIn('POOL_USE_NOT_COMPLETE', result['gaps'])

    def test_larger_page_recovers_without_erasing_old_failed_tail(self):
        first, a = self.page(100, limit=1)
        failed, _ = self.page(101, cursor=a['next_cursor'], failed=True)
        newer, _ = self.page(102)
        self.assertEqual(self.audit([first, failed])['pool'], 'query-failed')
        result = self.audit([first, failed, newer])
        self.assertEqual(result['pool'], 'queried')
        self.assertEqual(len(result['pool_history']), 3)
        self.assertEqual([p['run_id'] for p in result['pool_history'] if p['status'] == 'search-unavailable'], [101])

    def test_reachable_old_tail_is_reused_or_keeps_its_failure(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                first, a = self.page(100, limit=1)
                tail, _ = self.page(101, cursor=a['next_cursor'], failed=failed)
                newer, n = self.page(102, limit=1)
                self.assertEqual(n['next_cursor'], a['next_cursor'])
                result = self.audit([newer, first, tail])
                self.assertEqual(result['pool'], 'query-failed' if failed else 'queried')
                self.assertEqual(len(result['pool_history']), 3)

    def test_missing_current_page_cannot_borrow_old_route_or_its_adoption(self):
        first, a = self.page(100, limit=2)
        old_tail, tail = self.page(101, cursor=a['next_cursor'])
        newer, n = self.page(102, limit=1)
        adopted = SourceRef.from_dict(tail['matches'][0]['reference'])
        self.assertNotIn(tail['matches'][0]['version_key'], {m['version_key'] for m in n['matches']})
        result = self.audit([first, old_tail, newer], [adopted])
        self.assertEqual(result['pool'], 'query-partial')
        self.assertFalse(result['sources'][0]['pool_match'])
        self.assertTrue(result['sources'][0]['adopted_by_author'])
        self.assertIn('POOL_MATCHES_NOT_ADOPTED', result['gaps'])
        self.assertEqual(len(result['pool_history']), 3)

    def test_new_root_failure_cannot_borrow_old_complete_scan(self):
        older, _ = self.page(100)
        failed, _ = self.page(101, failed=True)
        self.assertEqual(self.audit([failed, older])['pool'], 'query-failed')

    def test_newer_orphan_success_or_failure_cannot_be_hidden(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                first, a = self.page(100, limit=1)
                newer, _ = self.page(101)
                orphan, _ = self.page(102, cursor=a['next_cursor'], failed=failed)
                result = self.audit([first, newer, orphan])
                self.assertEqual(result['pool'], 'query-failed' if failed else 'query-partial')
                self.assertIn('POOL_USE_NOT_COMPLETE', result['gaps'])
                self.assertEqual(len(result['pool_history']), 3)

    def test_new_snapshot_tail_cannot_borrow_earlier_snapshot_root(self):
        earlier, old = self.page(100)
        self.add_source('d')
        _, new = self.page(101, limit=1)
        tail, _ = self.page(102, cursor=new['next_cursor'])
        self.assertNotEqual(old['snapshot'], new['snapshot'])
        result = self.audit([earlier, tail])
        self.assertEqual(result['pool'], 'query-coverage-unproven')

    def test_native_start_time_precedes_numeric_and_caller_order(self):
        first, a = self.page(100, limit=1)
        tail, _ = self.page(101, cursor=a['next_cursor'], failed=True)
        newer, _ = self.page(90, started_at='2026-10-16T01:00:00Z')
        self.assertEqual(self.audit([newer, first, tail])['pool'], 'queried')


if __name__ == '__main__':
    unittest.main()
