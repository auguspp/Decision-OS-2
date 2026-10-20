"""Normal Room/CLI/full-scope projection and exact operation-level failures."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from decision_kernel.application import auction_next_session_custody as custody
from decision_kernel.application import reading_auction_next_session as reading
from decision_kernel.application.market_custody import retain_market_reading, read_stored_market
from decision_kernel.application.reading_market import open_market
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands, auction_next_session_commands
from decision_kernel.interfaces.reading_page import render_page
from test_auction_inputs import ROOT, REGISTRY, auction_payload
from test_auction_next_session_custody import next_fixture, root
from test_auction_next_session_inputs import payload, CHECKED
from test_global_room import source_query


def room_plan(reference, **navigation):
    return {'as_of': CHECKED, 'auction_next_session': {'reference': reference, **navigation}}


@pytest.mark.parametrize('failure', [StoreUnavailable('REMOTE_REQUEST_LIMIT'), RecordError('READING_SCOPE_CLOSED'),
    RecordError('SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED'), RecordError('PUBLIC_BINDING_MISMATCH')], ids=str)
def test_original_operation_error_from_seed_is_same_object_and_zero_later_backend_reads(monkeypatch, failure):
    store, _, original, saved = next_fixture()
    bad, _ = source_query(store, original['reference'], api='limit_list_d')
    resolve, after, raised = store.backend.resolve, [], False
    def intercept(reference):
        nonlocal raised
        if raised:
            after.append(reference)
        if reference == bad.manifest and not raised:
            raised = True
            raise failure
        return resolve(reference)
    monkeypatch.setattr(store.backend, 'resolve', intercept)
    with pytest.raises(type(failure)) as caught:
        ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved['reference']))
    assert raised and caught.value is failure and after == []


def test_normal_room_qualifies_whole_scope_before_filter_and_original_guard_stays():
    store, fresh, original, saved = next_fixture()
    out = ReadingRoom(store, market_registry=REGISTRY).build(room_plan(saved['reference'], limit=1))
    part = out['auction_next_session']
    assert part['status'] == out['status'] == 'partial' and part['page']['returned'] == 1
    assert part['view']['view']['cohorts'][0]['cohort_denominator'] == 8
    assert encoded(part['open_request']) == encoded({'operation': 'read-source', 'payload': saved['reference']})
    for plan in (part['next_plan'], {**part['plan'], 'offset': 0, 'entity_id': '600001.SH'}):
        again = ReadingRoom(fresh(), market_registry=REGISTRY).build({'as_of': CHECKED, 'auction_next_session': plan})
        assert encoded(again['auction_next_session']['view']) == encoded(part['view'])
    direct = auction_next_session_commands.perform(fresh(), part['plan'], registry=REGISTRY)
    assert encoded(direct) == encoded(part)
    with pytest.raises(RecordError, match='ROOM_MARKET_ASHARE_ROOT_REQUIRED'):
        open_market(fresh(), {'reference': saved['reference']}, registry=REGISTRY)
    wrong = reading.open_next_session(fresh(), {'reference': original['reference']}, registry=REGISTRY)
    assert wrong['status'] == 'invalid'


def test_no_plan_zero_source_work_invalid_plan_and_source_permission_precede_io(monkeypatch):
    store, _, _, saved = next_fixture()
    def forbidden(*args, **kwargs): pytest.fail('unrequested source read')
    monkeypatch.setattr(reading, 'RelayRegistry', forbidden)
    monkeypatch.setattr(VersionedStore, 'read_source', forbidden)
    plain = ReadingRoom(store, market_registry=object()).build({'as_of': CHECKED}, market_export=False)
    assert 'auction_next_session' not in plain and 'id="auction-next-content"' not in render_page(plain)
    for bad in ({'reference': saved['reference'], 'limit': True}, {'reference': saved['reference'], 'offset': -1},
                {'reference': saved['reference'], 'entity_id': 'SPX'}, {'reference': saved['reference'], 'qualified': True}):
        with pytest.raises(RecordError): ReadingRoom(store).build({'as_of': CHECKED, 'auction_next_session': bad})
    with pytest.raises(RecordError, match='ROOM_PUBLIC_AUCTION_NEXT_NOT_CONFIRMED'):
        ReadingRoom(store).build(room_plan(saved['reference']), market_export=False)
    with pytest.raises(RecordError, match='ROOM_PUBLIC_AUCTION_NEXT_NOT_CONFIRMED'):
        reading_commands.perform(store, {'plan': room_plan(saved['reference']), 'public_research_custody_confirmed': True})


def test_missing_quote_is_local_and_final_scope_failure_withholds_html(tmp_path, monkeypatch):
    store, _, original, saved = next_fixture()
    document = root(store, saved['reference']); key = document['reading']['current_quotes']['2026-10-21']
    missing = SourceRef.from_dict(document['queries'][key])
    store.backend.shared['missing'].add((missing.manifest.commit, missing.manifest.path))
    out = ReadingRoom(store, market_registry=REGISTRY).build({**room_plan(saved['reference']), 'auction': {'reference': original['reference']}})
    assert out['auction_next_session']['status'] == 'partial' and out['auction_next_session']['view']['view'] is None
    assert out['auction']['view']['view']['cohort_denominator'] == 8
    scope, failure = custody._scope, RecordError('READING_SCOPE_CLOSED')
    @contextmanager
    def final_check(owner):
        with scope(owner) as reader:
            yield reader
            raise failure
    monkeypatch.setattr(custody, '_scope', final_check)
    args = SimpleNamespace(market_registry=ROOT/'docs/sources/relay-registry.json', input=tmp_path/'plan.json', out=tmp_path/'absent.html')
    with pytest.raises(RecordError) as caught:
        reading_commands.execute(args, store, lambda _: room_plan(saved['reference']))
    assert caught.value is failure and not args.out.exists()


def test_main_cli_and_exact_root_reopen_outside_checkout_without_capture_files(tmp_path):
    path = tmp_path/'synthetic.git'; store = VersionedStore(GitStore.init_synthetic(path))
    fresh = lambda: VersionedStore(GitStore(path))
    original = retain_market_reading(auction_payload(close=False), REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    capture_file = tmp_path/'capture.json'; capture_file.write_bytes(encoded(payload(original['reference'])))
    saved = retain_market_reading(json.loads(capture_file.read_bytes()), REGISTRY, store, fresh_reader=fresh, custody_authorized=True)
    capture_file.unlink()
    plan = tmp_path/'plan.json'; plan.write_bytes(encoded(room_plan(saved['reference'], limit=1)))
    direct = tmp_path/'direct.json'; direct.write_bytes(encoded({'reference': saved['reference'], 'limit': 1}))
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src')}
    before, results = store.backend.head(), []
    for name in ('first', 'reopen'):
        run = subprocess.run([sys.executable, '-m', 'decision_kernel', 'store', '--path', str(path), 'research-room',
            '--input', str(plan), '--out', str(tmp_path/(name+'.html')), '--market-registry', str(ROOT/'docs/sources/relay-registry.json')],
            cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=60)
        results.append(json.loads(run.stdout))
    run = subprocess.run([sys.executable, '-m', 'decision_kernel', 'store', '--path', str(path), 'read-auction-next-session',
        '--input', str(direct), '--market-registry', str(ROOT/'docs/sources/relay-registry.json')],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True, timeout=60)
    assert results[0] == results[1] and json.loads(run.stdout) == results[0]['auction_next_session']
    assert (tmp_path/'first.html').read_bytes() == (tmp_path/'reopen.html').read_bytes() and store.backend.head() == before


def exercise_installed_auction_next_session(outside, python, env, store):
    """Extend the original single wheel install; no new gate or source imports."""
    directory = outside/'auction-next-session'; directory.mkdir()
    registry_dir = directory/'registry'; registry_dir.mkdir()
    original_registry = ROOT/'docs/sources/relay-registry.json'
    metadata = json.loads(original_registry.read_text())
    for relative in ('relay-registry.json', metadata['manual']['path'], metadata['live_catalogue']['path'], metadata['live_catalogue']['receipt_path']):
        dest = registry_dir/relative; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original_registry.parent/relative, dest)
    auction_file, capture_file, plan = directory/'auction.json', directory/'capture.json', directory/'plan.json'
    auction_file.write_bytes(encoded(auction_payload(close=False))); capture_file.write_bytes(encoded(payload(None)))
    script = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import encoded
from decision_kernel.infrastructure.git_store import GitStore
path,registry,auction,source,output=map(Path,sys.argv[1:])
fresh=lambda:VersionedStore(GitStore(path))
r=RelayRegistry(registry)
original=retain_market_reading(json.loads(auction.read_bytes()),r,fresh(),fresh_reader=fresh,custody_authorized=True)['reference']
value=json.loads(source.read_bytes());value['auction_reference']=original
ref=retain_market_reading(value,r,fresh(),fresh_reader=fresh,custody_authorized=True)['reference']
output.write_bytes(encoded({'as_of':'2026-10-21T08:00:00+00:00','auction_next_session':{'reference':ref,'limit':1}}))
'''
    registry = registry_dir/'relay-registry.json'
    subprocess.run([str(python), '-I', '-c', script, str(store), str(registry), str(auction_file), str(capture_file), str(plan)],
        cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
    auction_file.unlink(); capture_file.unlink()
    before, models = GitStore(store).head(), []
    for name in ('first', 'reopen'):
        run = subprocess.run([str(python), '-I', '-m', 'decision_kernel', 'store', '--path', str(store), 'research-room',
            '--input', str(plan), '--out', str(directory/(name+'.html')), '--market-registry', str(registry)],
            cwd=outside, env=env, check=True, capture_output=True, text=True, timeout=60)
        models.append(json.loads(run.stdout))
    assert models[0] == models[1] and (directory/'first.html').read_bytes() == (directory/'reopen.html').read_bytes()
    part = models[0]['auction_next_session']; c = part['view']['view']['cohorts'][0]
    assert c['cohort_denominator'] == 8 and c['target_session'] == '2026-10-21'
    assert c['result']['groups']['MATCHED']['open_observed'] == 2 and part['page']['returned'] == 1 and part['next_plan']
    direct = directory/'direct.json'; direct.write_bytes(encoded(part['plan']))
    run = subprocess.run([str(python), '-I', '-m', 'decision_kernel', 'store', '--path', str(store), 'read-auction-next-session',
        '--input', str(direct), '--market-registry', str(registry)], cwd=outside, env=env, check=True,
        capture_output=True, text=True, timeout=60)
    assert json.loads(run.stdout) == part and models[0]['catalogue']['total_cases'] > 0 and GitStore(store).head() == before
