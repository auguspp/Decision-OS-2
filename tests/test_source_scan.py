"""Bounded research scans over the real GitHubStore/512-call transport, synthetic wire only."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil

import pytest

from decision_kernel.application.research_evidence import ResearchEvidence
from decision_kernel.application.versioned_store import VersionedStore, MAX_SCAN_VERSIONS
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, encoded
from decision_kernel.infrastructure import github_store as remote
from test_github_store import GitAPI
from test_github_transport import Response
from test_research_evidence import request, plan


class Wire:
    def __init__(self, model):
        self.model = model

    def open(self, req, timeout):
        assert req.method == 'GET' and req.data is None
        assert timeout == 30
        path = req.full_url.removeprefix(remote.API_ROOT)
        return Response(encoded(self.model.request('GET', path)), req.full_url)


def reader(model):
    api = remote.GitHubAPI('synthetic-scan-token')
    api._opener = Wire(model)
    return VersionedStore(remote.GitHubStore(model.binding, api)), api


@pytest.fixture(scope='module')
def seed(tmp_path_factory):
    model = GitAPI(tmp_path_factory.mktemp('source-scan-seed')/'store.git')
    model.binding = replace(model.binding, visibility='public', write_enabled=False)
    anchor = model.git.commit({'store.json':model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.meta['private'] = False
    model.git.repository = model.binding.repository
    local = VersionedStore(model.git)
    refs = [local.put_source('seed-'+str(n), SourceInput('row-'+str(n).zfill(3), 'content',
        'Synthetic source', 'synthetic:scan', 'unrelated material' if n < 38 else '铜与现金')) for n in range(40)]
    return model, refs


@pytest.fixture
def seeded(seed, tmp_path):
    template, refs = seed
    model = deepcopy(template)
    path = tmp_path/'store.git';shutil.copytree(template.git.root, path)
    model.git.root = path
    model.requests.clear()
    return model, refs


def test_old_full_scan_exhausts_real_budget_but_cold_bounded_pages_finish(seeded):
    model, refs = seeded
    old, api = reader(model)
    with pytest.raises(RecordError, match='REMOTE_REQUEST_LIMIT'):
        old.search('铜')
    assert api._requests == remote.MAX_REQUESTS + 1 == 513
    # Each explicit reader page is a new operation, never a hidden counter reset.
    cursor, pages, counts = None, [], []
    for _ in range(6):
        store, api = reader(model)
        page = ResearchEvidence(store).page(request(), plan('铜', '现金'), cursor=cursor, limit=8)
        assert page['status'] != 'search-unavailable'
        assert page['coverage']['scanned_versions_this_page'] <= MAX_SCAN_VERSIONS
        assert api._requests < remote.MAX_REQUESTS
        pages.append(page); counts.append(api._requests)
        cursor = page['next_cursor']
        if cursor is None: break
    assert cursor is None and len(pages) == 3
    assert pages[0]['matches'] == [] and not pages[0]['coverage']['query_plan_complete']
    assert pages[0]['next_cursor']['source_cursor']['scan_offset'] == 32
    assert len({p['snapshot'] for p in pages}) == 1
    assert [m['reference']['manifest']['path'] for p in pages for m in p['matches']] == [r.manifest.path for r in refs[-2:]] * 2
    assert model.writes == 0
    print('native-budget per explicit page:', counts)


def test_scope_revalidates_at_exit_and_cannot_escape_or_write(seeded):
    model, refs = seeded
    store, api = reader(model); backend = store.backend; snap=model.git.head()
    with backend.source_read_scope(snap) as scoped:
        ref = replace(refs[-1], manifest=replace(refs[-1].manifest, commit=snap))
        assert VersionedStore(scoped).read_source(ref)['data'] == '铜与现金'
        before=api._requests
        assert VersionedStore(scoped).read_source(ref)['data'] == '铜与现金'
        assert api._requests == before
        with pytest.raises(RecordError, match='READ_ONLY'): scoped.commit({}, expected=snap)
        with pytest.raises(RecordError, match='OUTSIDE_SCOPE'): scoped.read(snap, 'store.json')
        with pytest.raises(RecordError, match='CLOSED_OR_CHANGED'): scoped.paths(model.binding.anchor, 'source-views/')
    assert api._requests > before
    with pytest.raises(RecordError, match='CLOSED_OR_CHANGED'): scoped.resolve(ref.manifest)
    assert backend.read(snap, ref.manifest.path) and api._requests > before
    assert model.writes == 0


@pytest.mark.parametrize('changed', ['privacy','binding','reachability'])
def test_mutable_access_change_during_scan_cannot_release_results(seeded, changed):
    model, refs = seeded
    store, _ = reader(model)
    original = model.corrupt_response
    changed_once=False
    def mutate(path, value):
        nonlocal changed_once
        if '/git/blobs/' in path and not changed_once:
            changed_once=True
            if changed == 'privacy': model.meta['private']=True
            elif changed == 'binding': model.meta['id']=-1
            else:
                model.git._run('update-ref', 'refs/heads/records', model.binding.anchor)
        return value
    model.corrupt_response=mutate
    with pytest.raises(RecordError): store.search('铜', scan_limit=32)
    assert changed_once and model.writes == 0


def test_bounded_scan_cursor_stays_on_original_snapshot_after_new_source(seeded):
    model, _ = seeded; store, _ = reader(model)
    first=store.search('铜', scan_limit=32)
    local=VersionedStore(model.git)
    local.put_source('later', SourceInput('aaa-late','content','late','synthetic:late','铜'))
    cold, _ = reader(model)
    last=cold.search('铜',cursor=first['next_cursor'],scan_limit=32)
    assert last['snapshot']==first['snapshot'] and last['next_cursor'] is None
    assert last['scan']['start']==32 and last['scan']['total_versions']==40
    assert {i['source_id'] for i in last['items']}=={'row-038','row-039'}
    assert last['total_scope']=='this-scan' and last['total']==2


@pytest.mark.parametrize('change',[{'scan_offset':True},{'scan_offset':999},{'scan_offset':-1},
    {'schema':'other'},{'snapshot':'main'},{'query_sha256':'0'*64},{'offset':0}])
def test_scan_cursor_shape_or_bounds_never_silently_restart(seeded, change):
    model, _=seeded; store,_=reader(model)
    first=store.search('铜',scan_limit=32)
    with pytest.raises(RecordError): store.search('铜',cursor={**first['next_cursor'],**change},scan_limit=32)


def test_legacy_match_offset_retained_and_unread_corruption_not_called_healthy(seeded):
    model, refs=seeded
    local=VersionedStore(model.git)
    first=local.search('铜',limit=1)
    last=local.search('铜',cursor=first['next_cursor'],scan_limit=32)
    assert last['total']==2 and 'scan' not in last and last['items'][0]['source_id']=='row-039'
    store,_=reader(model)
    initial=store.search('铜',scan_limit=32)
    body_path=refs[-1].manifest.path.removesuffix('manifest.json')+'content.txt'
    blob=model.git._run('rev-parse',initial['snapshot']+':'+body_path).stdout.decode().strip()
    def corrupt(path,value):
        if path.endswith('/git/blobs/'+blob): value['content']='Zm9yZ2Vk'
        return value
    model.corrupt_response=corrupt
    cold,_=reader(model)
    with pytest.raises(RecordError): cold.search('铜',cursor=initial['next_cursor'],scan_limit=32)
    assert initial['scan']['complete'] is False


def exercise_installed_scan(command, outside):
    """Same cold-wheel test, use its existing sources and a one-match page."""
    req=asdict(request()); p=plan('合成','合成')
    req_path=outside/'scan-request.json'; req_path.write_bytes(encoded(req))
    plan_path=outside/'scan-plan.json'; plan_path.write_bytes(encoded(p))
    first=command('prepare-research','--request',str(req_path),'--evidence-plan',str(plan_path),'--evidence-limit','1')
    page=first['evidence']; assert page['query_pages'][0]['scan']['end'] > 0
    cursor=outside/'scan-cursor.json'; cursor.write_bytes(encoded(page['next_cursor']))
    second=command('prepare-research','--request',str(req_path),'--evidence-plan',str(plan_path),
                   '--evidence-cursor',str(cursor),'--evidence-limit','1')
    assert second['evidence']['snapshot']==page['snapshot']
    assert second['persistence']=='not-written'
