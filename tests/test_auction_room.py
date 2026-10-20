"""Complete original scope before normal Room/CLI navigation and terminal output."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_kernel.application import reading_auction
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.reading_market import open_market
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MissingObject, RecordError, SourceInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import auction_commands, reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_auction_custody import saved_fixture
from test_auction_inputs import ROOT, REGISTRY, auction_payload
from test_global_room import retain_global, source_query

CLOCK = '2026-10-21T12:00:00+08:00'


def room_plan(reference, **navigation):
    return {'as_of': CLOCK, 'auction': {'reference': reference, **navigation}}


def test_normal_room_keeps_all_rows_groups_clocks_and_versions_before_filter():
    store, fresh, saved = saved_fixture()
    reference = saved['reference']
    original = ReadingRoom(store, market_registry=REGISTRY).build(room_plan(reference, limit=1))
    part = original['auction']
    expected = read_stored_market(reference, REGISTRY, fresh(), limit=100)
    assert encoded(part['view']) == encoded(expected) and part['view']['view']['cohort_denominator'] == 8
    assert part['status'] == original['status'] == 'partial' and part['page']['returned'] == 1
    assert encoded(part['open_request']) == encoded({'operation': 'read-source', 'payload': reference})
    assert part['source_requests'] == part['writes'] == 0
    next_page = ReadingRoom(fresh(), market_registry=REGISTRY).build({'as_of': CLOCK, 'auction': part['next_plan']})
    filtered = ReadingRoom(fresh(), market_registry=REGISTRY).build(room_plan(reference, entity_id='600002.SH'))
    for view in (next_page['auction'], filtered['auction']):
        assert view['view'] == part['view'] and view['status'] == 'partial'
    assert filtered['auction']['page']['entries'][0]['status'] == 'SHADOW_PROFILE_MATCH'
    assert next_page['auction']['page']['entries'][0]['entity_id'] != part['page']['entries'][0]['entity_id']
    assert filtered['auction']['view']['view']['market_session'] != CLOCK[:10]
    assert encoded(auction_commands.perform(fresh(), {'reference': reference}, registry=REGISTRY)['view']) == encoded(expected)


def test_later_close_version_does_not_change_frozen_original_cohort_or_old_root():
    store, fresh, saved = saved_fixture(auction_payload(close=False))
    old = reading_auction.open_auction(store, {'reference': saved['reference']}, registry=REGISTRY)
    new = retain_market_reading(auction_payload(), REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    current = reading_auction.open_auction(fresh(), {'reference': new['reference']}, registry=REGISTRY)
    a, b = old['view']['view'], current['view']['view']
    assert a['cohort_version'] == b['cohort_version'] and a['view_version'] != b['view_version']
    assert a['cohort_denominator'] == b['cohort_denominator'] == 8
    assert {k: v['denominator'] for k, v in a['groups'].items()} == {k: v['denominator'] for k, v in b['groups'].items()}
    assert reading_auction.open_auction(fresh(), {'reference': saved['reference']}, registry=REGISTRY) == old


def test_no_plan_adds_no_source_work_and_invalid_plan_permission_precede_io(monkeypatch):
    store, _, saved = saved_fixture()
    def forbidden(*args, **kwargs):
        pytest.fail('unrequested source or registry I/O')
    monkeypatch.setattr(reading_auction, 'RelayRegistry', forbidden)
    monkeypatch.setattr(VersionedStore, 'read_source', forbidden)
    plain = ReadingRoom(store, market_registry=object()).build({'as_of': CLOCK}, market_export=False)
    assert 'auction' not in plain and 'auction' not in plain['plan']
    assert 'id="auction-content"' not in render_page(plain)
    monkeypatch.setattr(store.backend, 'head', forbidden)
    for value in ({'reference': saved['reference'], 'limit': True}, {'reference': saved['reference'], 'url': 'https://invalid'},
                  {'reference': saved['reference'], 'entity_id': 'SPX'}, {'reference': saved['reference'], 'offset': -1}):
        with pytest.raises(RecordError):
            ReadingRoom(store).build({'as_of': CLOCK, 'auction': value})
    plan = room_plan(saved['reference'])
    with pytest.raises(RecordError, match='ROOM_PUBLIC_AUCTION_NOT_CONFIRMED'):
        reading_commands.perform(store, {'plan': plan, 'public_research_custody_confirmed': True})
    with pytest.raises(RecordError, match='ROOM_PUBLIC_AUCTION_NOT_CONFIRMED'):
        ReadingRoom(store).build(plan, market_export=False)


def test_wrong_roots_refuse_before_queries_and_existing_global_and_ashare_contracts_survive(monkeypatch):
    store, fresh, saved = saved_fixture()
    global_refs = retain_global(store, fresh)
    receipt = asdict(store.put_source('auction-count-only', SourceInput('auction-count-only', 'content',
        'Synthetic count receipt', 'synthetic:auction', '{"returned_pool_rows":13,"matched":2}')))
    original, calls = VersionedStore.read_source, []
    def read(owner, reference):
        calls.append(reference)
        return original(owner, reference)
    monkeypatch.setattr(VersionedStore, 'read_source', read)
    for reference in (global_refs['us_indices'], receipt):
        calls.clear()
        result = reading_auction.open_auction(store, {'reference': reference}, registry=REGISTRY)
        assert result['status'] == 'invalid' and len(calls) == 1
    calls.clear()
    with pytest.raises(RecordError, match='ROOM_MARKET_ASHARE_ROOT_REQUIRED'):
        open_market(store, {'reference': saved['reference']}, registry=REGISTRY)
    assert len(calls) == 1
    plan = {**room_plan(saved['reference']), 'global_context': {k: {'reference': v} for k, v in global_refs.items()}}
    result = ReadingRoom(store, market_registry=REGISTRY).build(plan)
    assert result['global_context']['status'] == 'readable' and result['auction']['view']['view']['cohort_denominator'] == 8


@pytest.mark.parametrize('failure', [StoreUnavailable('REMOTE_REQUEST_LIMIT'), RecordError('READING_SCOPE_CLOSED'),
    RecordError('SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED'), RecordError('PUBLIC_BINDING_MISMATCH')], ids=str)
def test_original_operation_error_identity_zero_later_backend_reads(monkeypatch, failure):
    store, _, saved = saved_fixture()
    bad, _ = source_query(store, saved['reference'], api='limit_list_d')
    original, calls, after, failed = store.backend.resolve, [], [], False
    def resolve(reference):
        nonlocal failed
        calls.append(reference)
        if failed:
            after.append(reference)
        if reference == bad.manifest and not failed:
            failed = True
            raise failure
        return original(reference)
    monkeypatch.setattr(store.backend, 'resolve', resolve)
    with pytest.raises(type(failure)) as caught:
        ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved['reference']))
    assert caught.value is failure and failed and after == [] and calls[-1] == bad.manifest


def test_missing_query_is_local_but_final_scope_failure_prevents_any_html(tmp_path, monkeypatch):
    store, _, saved = saved_fixture()
    close, _ = source_query(store, saved['reference'], api='daily')
    store.backend.shared['missing'].add((close.manifest.commit, close.manifest.path))
    out = ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved['reference']))
    assert out['auction']['status'] == 'partial' and out['auction']['view']['view']['matched'] == 2
    assert out['auction']['view']['view']['groups']['MATCHED']['denominator'] == 2
    original, failure, completed = reading_auction.market_read_scope, RecordError('PUBLIC_BINDING_MISMATCH'), []
    @contextmanager
    def scope(owner):
        with original(owner) as reader:
            yield reader
            completed.append(True)
            raise failure
    monkeypatch.setattr(reading_auction, 'market_read_scope', scope)
    monkeypatch.setattr(reading_auction, 'MAX_AUCTION_BYTES', 1)
    args = SimpleNamespace(market_registry=ROOT / 'docs/sources/relay-registry.json',
                           input=tmp_path / 'plan.json', out=tmp_path / 'must-not-exist.html')
    with pytest.raises(RecordError) as caught:
        reading_commands.execute(args, store, lambda p: room_plan(saved['reference']))
    assert caught.value is failure and completed == [True] and not args.out.exists()


def test_main_cli_room_and_source_free_auction_command_reopen_independent_processes(tmp_path):
    path = tmp_path / 'synthetic.git'
    store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    saved = retain_market_reading(auction_payload(), REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    plan = tmp_path / 'plan.json'; plan.write_bytes(encoded(room_plan(saved['reference'], limit=1)))
    direct = tmp_path / 'direct.json'; direct.write_bytes(encoded({'reference': saved['reference'], 'limit': 1}))
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    before, results = store.backend.head(), []
    for name in ('first', 'reopen'):
        run = subprocess.run([sys.executable, '-m', 'decision_kernel', 'store', '--path', str(path),
            'research-room', '--input', str(plan), '--out', str(tmp_path / (name + '.html')),
            '--market-registry', str(ROOT / 'docs/sources/relay-registry.json')],
            cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
        results.append(json.loads(run.stdout))
    run = subprocess.run([sys.executable, '-m', 'decision_kernel', 'store', '--path', str(path),
        'read-opening-auction', '--input', str(direct), '--market-registry', str(ROOT / 'docs/sources/relay-registry.json')],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True)
    assert results[0] == results[1] and json.loads(run.stdout) == results[0]['auction']
    assert (tmp_path / 'first.html').read_bytes() == (tmp_path / 'reopen.html').read_bytes()
    assert store.backend.head() == before


def exercise_installed_auction_room(outside, python, env, store):
    """Extend the original one-wheel install; no new build, source import or gate."""
    directory = outside / 'auction-room'; directory.mkdir()
    registry_dir = directory / 'registry'; registry_dir.mkdir()
    registry_source = ROOT / 'docs/sources/relay-registry.json'
    metadata = json.loads(registry_source.read_text())
    for relative in ('relay-registry.json', metadata['manual']['path'], metadata['live_catalogue']['path'],
                     metadata['live_catalogue']['receipt_path']):
        destination = registry_dir / relative; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(registry_source.parent / relative, destination)
    registry = registry_dir / 'relay-registry.json'
    source = directory / 'capture.json'; source.write_bytes(encoded(auction_payload()))
    plan = directory / 'plan.json'
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.domain.records import encoded
store,registry,source,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(store))
ref=retain_market_reading(json.loads(source.read_bytes()),RelayRegistry(registry),fresh(),fresh_reader=fresh,custody_authorized=True)['reference']
output.write_bytes(encoded({'as_of':'2026-10-21T12:00:00+08:00','auction':{'reference':ref,'limit':1}}))
'''
    subprocess.run([str(python), '-I', '-c', script, str(store), str(registry), str(source), str(plan)],
                   cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    source.unlink()
    before, results = GitStore(store).head(), []
    for name in ('first', 'reopen'):
        run = subprocess.run([str(python), '-I', '-m', 'decision_kernel', 'store', '--path', str(store),
            'research-room', '--input', str(plan), '--out', str(directory / (name + '.html')),
            '--market-registry', str(registry)], cwd=outside, env=env, check=True,
            capture_output=True, text=True, timeout=60)
        results.append(json.loads(run.stdout))
    assert results[0] == results[1]
    part = results[0]['auction']; view = part['view']['view']
    assert view['cohort_denominator'] == 8 and view['prior_temperature']['returned_pool_rows'] == 13
    assert view['matched'] == 2 and part['page']['returned'] == 1 and part['next_plan']
    assert view['timeliness'] == 'RECEIVED_BEFORE_CONTINUOUS_OPEN'
    assert (directory / 'first.html').read_bytes() == (directory / 'reopen.html').read_bytes()
    direct = directory / 'direct.json'; direct.write_bytes(encoded(part['plan']))
    run = subprocess.run([str(python), '-I', '-m', 'decision_kernel', 'store', '--path', str(store),
        'read-opening-auction', '--input', str(direct), '--market-registry', str(registry)],
        cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    assert json.loads(run.stdout) == part and results[0]['catalogue']['total_cases'] > 0
    assert GitStore(store).head() == before
