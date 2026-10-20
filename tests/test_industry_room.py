"""Industry panels consume exact original readers; all data here is synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.application import reading_industry as component
from decision_kernel.application.reading_room import ReadingRoom, plan_input
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, MissingObject, RecordError, RevisionInput, SourceRef, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import reading_commands, industry_commands, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_industry_reading import prepare, packet, SUBJECT, BUYER
from test_industry_operations import retain as operating_data
from test_research_runtime import host, inputs, CODE

CLOCK = '2026-10-17T09:30:00+08:00'


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / 'industry-room.git'))


def plan(ref, view='industry', **query):
    if view == 'industry-goods':
        query = {'evaluation_at': CLOCK, **query}
    return {'as_of': CLOCK, 'catalogue': {'limit': 1},
            'industry': {'reference': asdict(ref), 'view': view, **query}}


def public(value, **extra):
    return {'plan': value, 'public_research_custody_confirmed': True,
            'public_sources_custody_confirmed': True, **extra}


def root_for(store, view):
    if view == 'industry-goods':
        from test_industry_goods import prepare as goods_data
        return goods_data(store)[0]
    if view == 'industry':
        return prepare(store)[0]
    if view == 'operating-cycle':
        return operating_data(store)[0]
    if view == 'industry-uses':
        from test_industry_uses import prepare as use_data
        return use_data(store)[0]
    if view == 'livestock-weekly':
        from test_livestock_weekly import retain
        return retain(store)[0]
    from test_vehicle_flows import retain
    return retain(store)[0]


@pytest.mark.parametrize('view', component.VIEWS)
def test_original_reader_result_is_unchanged_and_reopens(store, view):
    ref = root_for(store, view)
    before = store.backend.head()
    value = plan(ref, view)
    expected = industry_commands.perform(store, {**value['industry'], 'public_sources_custody_confirmed': True})
    first = ReadingRoom(store).build(value)
    fresh = VersionedStore(GitStore(store.backend.root))
    second = ReadingRoom(fresh).build(value)
    assert encoded(first['industry']['view']) == encoded(expected)
    assert first == second and value == plan(ref, view)
    assert first['industry']['status'] == 'readable'
    assert first['status'] == ('partial' if view == 'industry-uses' else 'readable')
    assert first['persistence'] == 'not-written' and store.backend.head() == before
    assert first['industry']['view']['source_requests'] == first['industry']['view']['writes'] == 0
    assert 'public_sources_custody_confirmed' not in first['industry']['open_request']['payload']


@pytest.mark.parametrize('selector', [{'subject_id': SUBJECT.entity_id}, {'company_id': BUYER.entity_id},
                                      {'company_id': 'not-in-this-packet'}])
def test_server_query_does_not_change_scope_or_infer_company_links(store, selector):
    ref, _ = prepare(store)
    whole = ReadingRoom(store).build(plan(ref))['industry']['view']
    part = ReadingRoom(store).build(plan(ref, **selector))['industry']['view']
    assert part['scope'] == whole['scope']
    assert part['query'] == {'subject_id': None, 'company_id': None, **selector}
    assert part['relation_outcomes'] == whole['relation_outcomes']
    if selector.get('company_id') == 'not-in-this-packet':
        assert part['relations'] == part['series'] == []
    else:
        assert part['relations'] and all(r['net_benefit'] is None for r in part['relations'])
        assert all(r['applicability_at_cutoff'] == 'historical' for r in part['relations'])


@pytest.mark.parametrize('view', ['industry', 'industry-uses', 'livestock-weekly', 'industry-goods'])
@pytest.mark.parametrize('flag', [None, False, 'true', 1])
def test_native_permission_is_independent_before_any_output_or_store(host, tmp_path, flag, view):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    ref = root_for(writer, view)
    value = public(plan(ref, view), public_sources_custody_confirmed=flag,
                   public_material_custody_confirmed=True, public_feedback_custody_confirmed=True)
    head, calls, writes = host.git.head(), len(host.requests), host.writes
    out = tmp_path / 'denied'
    with pytest.raises(RecordError, match='PUBLIC_INDUSTRY'):
        research_runtime.run_command(host.binding, api=host, inputs=inputs('reading-room', value),
                                     code_sha=CODE, output=out)
    assert not out.exists() and host.git.head() == head and len(host.requests) == calls and host.writes == writes


@pytest.mark.parametrize('change', [ {'view': 'PMI'}, {'url': 'https://example.invalid'},
    {'company_id': BUYER.entity_id, 'subject_id': SUBJECT.entity_id},
    {'view': 'operating-cycle', 'company_id': BUYER.entity_id}, {'company_id': ''},
    {'reference': 'latest'}, {'view': []}])
def test_invalid_plan_rejected_before_reader(store, change, monkeypatch):
    ref, _ = prepare(store)
    value = plan(ref); value['industry'].update(change)
    monkeypatch.setattr(store, 'read_source', lambda *a: pytest.fail('unexpected source read'))
    with pytest.raises((RecordError, TypeError)):
        reading_commands.perform(store, public(value))


def test_whole_ref_is_required_and_material_flag_cannot_authorize_sources(store):
    ref, _ = prepare(store)
    value = plan(ref)
    value['industry']['reference']['selection'].update(kind='lines', start=1, end=1)
    with pytest.raises(RecordError, match='WHOLE_PACKET'):
        plan_input(value)
    with pytest.raises(RecordError, match='PUBLIC_INDUSTRY'):
        ReadingRoom(store).build(plan(ref), material_export=True, industry_export=False)


def test_no_industry_plan_preserves_old_shape_and_has_no_industry_io(store, monkeypatch):
    old = ReadingRoom(store).build({'as_of': CLOCK})
    monkeypatch.setattr(component, 'open_industry', lambda *a: pytest.fail('unexpected industry read'))
    assert ReadingRoom(store).build({'as_of': CLOCK, 'industry': None}) == old
    assert 'industry' not in old and 'industry' not in old['plan']


def test_missing_child_keeps_partial_and_good_branches(store, monkeypatch):
    ref, body = prepare(store)
    missing = body['relations'][0]['evidence']['reference']['manifest']
    actual = store.read_source
    def read(r):
        if asdict(r.manifest) == missing:
            raise MissingObject('OBJECT_MISSING')
        return actual(r)
    monkeypatch.setattr(store, 'read_source', read)
    result = ReadingRoom(store).build(plan(ref))
    v = result['industry']['view']
    assert result['status'] == v['status'] == 'partial'
    assert v['series'][0]['latest_change']['status'] == 'changed'
    assert len(v['relations']) == 1 and v['scope']['declared_relations'] == 2
    assert v['relation_outcomes'][0]['reason_code'] == 'OBJECT_MISSING'


def test_bad_root_does_not_replace_research_or_return_empty_success(store):
    ref, _ = prepare(store)
    stored = store.submit_revision('industry-room-research', RevisionInput('synthetic-room', 'r1', 'quick',
        'Synthetic retained research.', 'Synthetic mandate', 'synthetic', unresolved=('Unresolved',)))
    value = plan(ref)
    value['industry']['reference']['manifest']['sha256'] = 'f' * 64
    result = ReadingRoom(store).build(value)
    assert result['industry']['status'] == 'invalid' and result['status'] == 'partial'
    assert result['readings'][0]['body'] == 'Synthetic retained research.'
    assert 'view' not in result['industry'] and result['industry']['open_request']['payload'] == result['plan']['industry']


def test_new_packet_does_not_update_original_or_use_review_clock_as_data_cutoff(store):
    original, body = prepare(store)
    first = ReadingRoom(store).build(plan(original))['industry']['view']
    body['coverage_note'] = 'New synthetic release'
    latest = packet(store, body)
    assert latest != original
    value = plan(original); value['as_of'] = '2030-01-01T00:00:00Z'
    assert ReadingRoom(store).build(value)['industry']['view'] == first


def test_capacity_omits_whole_panel_but_keeps_original_request(store, monkeypatch):
    ref, _ = prepare(store)
    monkeypatch.setattr(component, 'MAX_INDUSTRY_BYTES', 1)
    result = ReadingRoom(store).build(plan(ref))
    assert result['status'] == 'partial' and result['industry']['status'] == 'not-included'
    assert 'view' not in result['industry'] and SourceRef.from_dict(result['industry']['reference']) == ref
    assert result['industry']['open_request']['operation'] == 'industry-read'


def test_final_access_loss_prevents_entire_export(store, monkeypatch):
    ref, _ = prepare(store)
    actual = component.open_industry
    def revoke(*args):
        view = actual(*args)
        monkeypatch.setattr(store.backend, 'head', lambda: (_ for _ in ()).throw(RecordError('ACCESS_REVOKED')))
        return view
    monkeypatch.setattr(component, 'open_industry', revoke)
    with pytest.raises(RecordError, match='ACCESS_REVOKED'):
        ReadingRoom(store).build(plan(ref))


def test_original_scope_exit_failure_never_exports_inner_result(store, monkeypatch):
    ref, _ = prepare(store)
    @contextmanager
    def denied(_):
        yield store
        raise RecordError('SOURCE_ACCESS_REVOKED')
    monkeypatch.setattr(component, 'industry_source_scope', denied)
    result = ReadingRoom(store).build(plan(ref))
    assert result['status'] == 'partial' and result['industry']['status'] == 'invalid'
    assert 'view' not in result['industry']


def test_paging_keeps_industry_ref_and_no_inferred_research_adoption(store):
    ref, _ = prepare(store)
    for case in ('a', 'b'):
        store.submit_revision('room-' + case, RevisionInput(case, 'r1', 'quick', 'Synthetic.', 'Mandate', 'synthetic', unresolved=('Not assessed',)))
    first = ReadingRoom(store).build(plan(ref))
    assert first['next_plan']['industry'] == first['plan']['industry']
    second = ReadingRoom(store).build(first['next_plan'])
    assert first['industry']['view'] == second['industry']['view']
    assert first['brief']['status'] == 'not-supplied' and first['human_acceptance'] == 'not-established'


@pytest.mark.parametrize('view', component.VIEWS)
def test_existing_native_host_and_safe_page(host, tmp_path, view):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    ref = root_for(writer, view)
    original_head, writes = host.git.head(), host.writes
    value = public(plan(ref, view))
    report = research_runtime.run_command(host.binding, api=host, inputs=inputs('reading-room', value),
        code_sha=CODE, output=tmp_path / 'out', run_id='456', attempt='1')
    assert report['status'] == 'completed' and report['write'] == 'not-requested'
    assert host.git.head() == original_head and host.writes == writes
    result = report['result']
    assert SourceRef.from_dict(result['industry']['view']['reference']) == ref
    assert (tmp_path / 'out' / 'reading-room.html').read_text() == render_page(result)
    assert result['news_source_reads']['verified_source_reads'] == 0


def test_literal_payload_and_output_does_not_overwrite(store, tmp_path):
    ref, body = prepare(store); body['coverage_note'] = '</script><img src=x onerror=alert(1)>'
    ref = packet(store, body); result = ReadingRoom(store).build(plan(ref))
    original = encoded(result); path = tmp_path / 'room.html'
    reading_commands.write_page(path, result)
    assert '</script><img' not in path.read_text()
    assert '产业观察与公司经营角色' in path.read_text()
    with pytest.raises(RecordError, match='OUTPUT_EXISTS'):
        reading_commands.write_page(path, result)
    assert encoded(result) == original


def exercise_installed_industry_room(outside, python, env, store):
    """Extend the one existing wheel installation, preserving all previous consumers."""
    fixture_store = VersionedStore(GitStore(Path(store)))
    executable = Path(python).parent / 'decision-os'
    for view in ('industry', 'livestock-weekly', 'industry-uses', 'industry-goods'):
        ref = root_for(fixture_store, view)
        head = fixture_store.backend.head()
        source = outside / (view + '-room-plan.json')
        source.write_text(json.dumps(plan(ref, view)), encoding='utf-8')
        outputs, pages = [], []
        for suffix in ('', '-reopened'):
            path = outside / (view + '-room' + suffix + '.html')
            run = subprocess.run([str(executable), 'store', '--path', str(store), 'research-room',
                '--input', str(source), '--out', str(path)], cwd=outside, env=env,
                capture_output=True, text=True)
            assert run.returncode == 0, run.stderr
            outputs.append(json.loads(run.stdout)); pages.append(path.read_bytes())
        assert outputs[0] == outputs[1] and pages[0] == pages[1] and fixture_store.backend.head() == head
        result = outputs[0]['industry']['view']
        if view == 'industry':
            assert len(result['relations']) == 2 and result['relations'][0]['net_benefit'] is None
        elif view == 'livestock-weekly':
            assert result['scope']['price_points'] == 9 and not result['relations']
        elif view == 'industry-uses':
            assert len(result['uses']) == 6 and result['scope']['source_blocked_uses'] == 1
            assert result['uses'][3]['series'] == [] and result['uses'][0]['relations'][0]['net_benefit'] is None
        else:
            assert result['scope']['distinct_qualified_goods'] == result['scope']['fresh_goods'] == 1
            assert result['scope']['global_fresh_goods'] is None
            assert result['quantity_milestone']['status'] == 'not-established'
