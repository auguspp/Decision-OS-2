"""Synthetic real-Git custody/room consumers, not live market or rights acceptance."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from html import unescape
from pathlib import Path
import shutil
import subprocess

import pytest

from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market,
)
from decision_kernel.application.reading_market import market_plan, open_market
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MissingObject, RecordError, RevisionInput, SourceInput, SourceRef, StoreUnavailable, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_market_custody import security_payload, raw_payload, extra, REGISTRY, REGISTRY_PATH, CODE
from test_research_runtime import host, invoke

CLOCK = "2026-10-17T12:00:00+08:00"


def two_securities():
    value = security_payload()
    fields = ['ts_code','symbol','name','exchange','curr_type','list_status','list_date','delist_date']
    value['master_results']['L'] = extra('stock_basic', fields, [
        ['600000.SH', '600000', '合成未提供报价', 'SSE', 'CNY', 'L', '20000101', None],
        [CODE, CODE[:6], '合成证券', 'SSE', 'CNY', 'L', '20000101', None]],
        {'exchange':'SSE','list_status':'L'})
    return value


def retain(store, factory, value=None):
    return json.loads(encoded(retain_market_reading(two_securities() if value is None else value, REGISTRY, store,
        fresh_reader=factory, public_rights_confirmed=True)['reference']))


@pytest.fixture
def saved(tmp_path):
    path = tmp_path / 'records.git'
    store = VersionedStore(GitStore.init_synthetic(path))
    factory = lambda: VersionedStore(GitStore(path))
    ref = retain(store, factory)
    research = store.submit_revision('room-research', RevisionInput('room-research','r1','quick',
        'Synthetic unchanged research.\r\n', 'Synthetic mandate', 'synthetic',
        unresolved=('No economic or real market acceptance.',)))
    return store, factory, ref, research


def plan(reference, **changes):
    return {'as_of':CLOCK, 'market':{'reference':reference, **changes}}


def build(saved, **changes):
    store, _, ref, _ = saved
    return ReadingRoom(store, market_registry=REGISTRY).build(plan(ref, **changes))


def test_exact_original_reader_output_and_research_unchanged(saved):
    store, factory, ref, research = saved
    head = store.backend.head(); raw = store.read_revision(research)['body']
    room = build(saved)
    expected = read_stored_market(SourceRef.from_dict(ref), REGISTRY, factory(), limit=20)
    assert encoded(room['market']['view']) == encoded(expected)
    assert room['market']['partial'] and room['status'] == 'partial'
    assert room['readings'][0]['body'] == raw
    assert room['persistence'] == 'not-written' and room['human_acceptance'] == 'not-established'
    assert store.backend.head() == head


def test_pagination_pins_root_across_new_source_version(saved):
    store, factory, ref, _ = saved
    first = build(saved, limit=1)
    assert first['market']['next_plan']['reference'] == ref
    value = two_securities(); value['horizons'] = [1]
    newer = retain(store, factory, value)
    assert newer != ref
    second = ReadingRoom(factory(), market_registry=REGISTRY).build(
        {'as_of':CLOCK, 'market':first['market']['next_plan']})
    for kind in ('raw','qfq'):
        a = first['market']['view']['view'][kind]; b = second['market']['view']['view'][kind]
        assert a['scope_count'] == b['scope_count'] == 2
        assert a['entries'][0]['entity_id'] != b['entries'][0]['entity_id']
        assert a['source_version'] == b['source_version']
    assert second['market']['next_plan'] is None


@pytest.mark.parametrize('entity', ['600000.SH', CODE, '600999.SH'])
def test_exact_security_query_preserves_scope_and_unknown_is_not_empty_universe(saved, entity):
    part = build(saved, entity_id=entity)['market']
    assert part['view']['view']['scope_count'] == 2
    for kind in ('raw','qfq'):
        view = part['view']['view'][kind]
        assert view['scope_count'] == 2
        assert len(view['entries']) == (0 if entity == '600999.SH' else 1)
        assert all(e['entity_id'] == entity for e in view['entries'])


@pytest.mark.parametrize('changes', [dict(offset=-1),dict(offset=True),dict(limit=0),dict(limit=51),
    dict(limit=True),dict(entity_id='600519'),dict(entity_id='600519.SH;anything'),dict(url='https://invalid'),
    dict(registry='/tmp/untrusted')])
def test_bad_plan_rejected_without_store_access(saved, changes, monkeypatch):
    store, _, ref, _ = saved
    monkeypatch.setattr(store.backend, 'head', lambda: pytest.fail('input rejection must precede I/O'))
    with pytest.raises(RecordError): ReadingRoom(store).build(plan(ref, **changes))


def test_nonwhole_reference_and_counts_are_not_market_roots(saved):
    store, _, ref, _ = saved
    wrong = deepcopy(ref); wrong['selection'] = dict(kind='lines',start=1,end=1,columns=[])
    with pytest.raises(RecordError, match='WHOLE_SOURCE'): market_plan({'reference':wrong})
    count = store.put_source('counts-only', SourceInput('counts-only','content','Synthetic counts','synthetic:counts',
        '{"status":"checked-factor-audit","source_refs_created":false}'))
    assert build((store, None, asdict(count), None))['market']['status'] == 'invalid'
    with pytest.raises(RecordError): market_plan({'reference':{'status':'checked-factor-audit'}})


def test_other_market_families_rejected_before_query_read(saved, monkeypatch):
    store, factory, _, _ = saved
    ref = retain(store, factory, raw_payload())
    original = store.read_source; calls = []
    def read(ref): calls.append(ref); return original(ref)
    monkeypatch.setattr(store, 'read_source', read)
    with pytest.raises(RecordError, match='ASHARE_ROOT_REQUIRED'):
        open_market(store, {'reference':ref}, registry=REGISTRY)
    assert len(calls) == 1  # only original root authentication, no query restoration


def test_missing_factor_preserves_raw_not_synthetic_zero(saved, monkeypatch):
    store, _, ref, _ = saved
    root = json.loads(store.read_source(SourceRef.from_dict(ref))['data'])
    bad = next(r for r in root['queries'].values()
        if json.loads(store.read_source(SourceRef.from_dict(r))['data'])['use']['api'] == 'adj_factor')
    original = store.read_source
    def read(ref):
        if encoded(asdict(ref)) == encoded(bad): raise MissingObject('SYNTHETIC_MISSING_FACTOR_QUERY')
        return original(ref)
    monkeypatch.setattr(store, 'read_source', read)
    out = build(saved, entity_id=CODE)['market']['view']
    assert out['status'] == 'partial-restoration' and out['unavailable_queries'] == 1
    assert out['view']['raw']['entries'][0]['horizons'][0]['status'] == 'observed'
    assert out['view']['qfq']['entries'][0]['input_status'] == 'not-supplied'
    assert 'change_pct' not in out['view']['qfq']['entries'][0]['horizons'][0]


def test_missing_root_only_blocks_market(saved, monkeypatch):
    store, _, ref, _ = saved
    original = store.read_source
    def read(r):
        if encoded(asdict(r)) == encoded(ref): raise MissingObject('SYNTHETIC_ROOT_MISSING')
        return original(r)
    monkeypatch.setattr(store, 'read_source', read)
    room = build(saved)
    assert room['market']['status'] == 'missing' and room['readings'][0]['status'] == 'readable'
    assert room['status'] == 'partial'


def test_panel_bound_omits_whole_result_and_preserves_root(saved, monkeypatch):
    import decision_kernel.application.reading_market as owner
    monkeypatch.setattr(owner, 'MAX_MARKET_BYTES', 1)
    room = build(saved)
    assert room['market']['status'] == 'not-included' and 'view' not in room['market']
    assert room['market']['reference'] == saved[2] and room['market']['next_plan'] is None


def test_final_access_loss_cannot_release_completed_room(saved, monkeypatch):
    import decision_kernel.application.reading_market as owner
    store = saved[0]; original = owner.open_market
    def lose(*a, **kw):
        result = original(*a, **kw)
        def deny(): raise StoreUnavailable('SYNTHETIC_ACCESS_REVOKED')
        monkeypatch.setattr(store.backend, 'head', deny)
        return result
    monkeypatch.setattr(owner, 'open_market', lose)
    with pytest.raises(StoreUnavailable): build(saved)


@pytest.mark.parametrize('permission', [None,False,'true',1])
def test_independent_source_permission_precedes_store(saved, permission, monkeypatch):
    store, _, ref, _ = saved
    payload = {'plan':plan(ref), 'public_research_custody_confirmed':True,
        'public_material_custody_confirmed':True,'public_feedback_custody_confirmed':True,
        'public_sources_custody_confirmed':permission}
    monkeypatch.setattr(store.backend, 'head', lambda: pytest.fail('permission check must precede I/O'))
    with pytest.raises(RecordError, match='PUBLIC_MARKET'):
        reading_commands.perform(store, payload)
    with pytest.raises(RecordError, match='PUBLIC_MARKET'):
        ReadingRoom(store).build(plan(ref), market_export=permission)


def test_without_market_does_not_load_registry_or_change_old_model(saved):
    store = saved[0]
    room = ReadingRoom(store, market_registry=object()).build({'as_of':CLOCK}, market_export=False)
    assert 'market' not in room and 'market' not in room['plan']
    assert room['readings'][0]['body'] == 'Synthetic unchanged research.\r\n'


def test_literal_page_and_public_request_has_no_permission_by_default(saved):
    room = build(saved); room['market']['view']['view']['raw']['entries'][0]['security_reference']['name'] = '</script><script>alert(9)</script>'
    html = render_page(room)
    assert '</script><script>alert(9)' not in html
    assert '\\u003c/script\\u003e' in html and 'market-content' in html
    assert "connect-src 'none'" in unescape(html) and 'innerHTML' not in html and 'localStorage' not in html
    assert 'public_sources_custody_confirmed' not in json.dumps(room['market']['open_request'])


def test_native_host_reads_original_root_and_denies_unconfirmed_export(host, tmp_path):
    # Entire existing authenticated host; executed on the exact current native tree.
    def factory(): return VersionedStore(GitHubStore(host.binding, host))
    writer = VersionedStore(GitHubStore(replace(host.binding,write_enabled=True), host))
    ref = retain(writer, factory)
    head, writes = host.git.head(), host.writes
    payload = {'plan':plan(ref),'public_research_custody_confirmed':True,'public_sources_custody_confirmed':True}
    out = invoke(host, tmp_path/'read', 'reading-room', payload)
    assert out['status'] == 'completed' and out['write'] == 'not-requested'
    expected = open_market(factory(), {'reference':ref}, registry=REGISTRY)
    assert encoded(out['result']['market']) == encoded(expected)
    assert host.git.head() == head and host.writes == writes
    before = len(host.requests); payload['public_sources_custody_confirmed'] = False
    with pytest.raises(RecordError, match='PUBLIC_MARKET'):
        invoke(host, tmp_path/'denied', 'reading-room', payload)
    assert not (tmp_path/'denied').exists() and len(host.requests) == before


def exercise_installed_market_room(outside, python, env, store):
    """Extend the original single wheel consumer; two package-external processes."""
    scope = outside / 'market-room'; scope.mkdir()
    directory = scope / 'registry'; directory.mkdir()
    meta = json.loads(REGISTRY_PATH.read_text())
    for relative in ['relay-registry.json',meta['manual']['path'],meta['live_catalogue']['path'],meta['live_catalogue']['receipt_path']]:
        dest = directory/relative; dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(REGISTRY_PATH.parent/relative,dest)
    registry = directory / 'relay-registry.json'
    payload = scope/'input.json'; payload.write_bytes(encoded(two_securities()))
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import encoded
p,reg,source,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(p))
out=retain_market_reading(json.loads(source.read_text()),RelayRegistry(reg),fresh(),fresh_reader=fresh,public_rights_confirmed=True)
output.write_bytes(encoded({'as_of':'2026-10-17T12:00:00+08:00','market':{'reference':out['reference'],'limit':1}}))
'''
    input_plan = scope/'plan.json'
    subprocess.run([str(python),'-I','-c',script,str(store),str(registry),str(payload),str(input_plan)],
        cwd=outside,env=env,check=True,capture_output=True,text=True)
    payload.unlink()  # No capture file remains for either reader process.
    before = GitStore(store).head()
    results = []
    for slot in ('first','reopen'):
        result = subprocess.run([str(python),'-I','-m','decision_kernel','store','--path',str(store),
            'research-room','--input',str(input_plan),'--out',str(scope/(slot+'.html')),
            '--market-registry',str(registry)],cwd=outside,env=env,check=True,capture_output=True,text=True)
        results.append(json.loads(result.stdout))
    assert results[0] == results[1] and results[0]['market']['view']['view']['scope_count'] == 2
    assert (scope/'first.html').read_bytes() == (scope/'reopen.html').read_bytes()
    assert results[0]['catalogue']['total_cases'] > 0 and GitStore(store).head() == before
