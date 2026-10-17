"""Original files, source pages and predecessors through real temporary Git."""
import base64
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import subprocess

import pytest

from decision_kernel.application import reading_materials as materials
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_delivery import ResearchDelivery
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceInput, StoreUnavailable, encoded
from decision_kernel.domain.research_artifacts import ArtifactInput
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces import reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_research_delivery import store, delivery_payload, continued
from test_research_runtime import host, invoke

AT = '2026-10-17T08:00:00+08:00'


def payload():
    data = delivery_payload()
    data['items'][0]['artifact'] = asdict(ArtifactInput.from_bytes(
        'html-original', '原报告.HTML', 'text/html', b'<!doctype html>\r\n<script>window.UNTRUSTED=1</script>\x00'))
    return data


def room_plan(ref, **options):
    return {'as_of': AT, 'reference': ref, 'materials': {'reference': ref, **options}}


def saved(store, *, include_zip=False):
    p = payload()
    if include_zip:
        from test_zip_artifacts import zip_item
        p['items'].append(zip_item())
    return p, ResearchDelivery(store).deliver(p, submission_id='materials-one')['reference']


@pytest.mark.parametrize('selected,index', [('html', 0), ('model', 1), ('archive', 3)])
def test_original_bytes_download_and_fresh_reader_without_writes(store, tmp_path, selected, index):
    p, ref = saved(store, include_zip=selected == 'archive')
    before = store.backend.head()
    first = ReadingRoom(store).build(room_plan(ref, attachment_id=selected))
    fresh = VersionedStore(GitStore(tmp_path/'records.git'))
    second = ReadingRoom(fresh).build(first['plan'])
    assert encoded(first) == encoded(second)
    view = second['materials']['view']
    attachment = view['attachment']
    assert base64.b64decode(attachment['data_base64'], validate=True) == base64.b64decode(p['items'][index]['artifact']['data_base64'])
    assert attachment['bytes_verified'] is True and attachment['executed'] is False
    assert view['research']['reference'] == ref and fresh.backend.head() == before
    assert first['persistence'] == view['persistence'] == 'not-written'


def test_delta_history_is_exact_and_does_not_rewrite_corrections(store):
    p, first = saved(store)
    p2 = continued(store, p, {'reference': first})
    second = ResearchDelivery(store).deliver(p2, submission_id='materials-two')['reference']
    p3 = continued(store, p2, {'reference': second}); p3['revision_id'] = 'r3'
    third = ResearchDelivery(store).deliver(p3, submission_id='materials-three')['reference']
    view = materials.open_materials(store, {'reference': third, 'history_limit': 1})
    assert view['inventory']['base_revision'] == second
    assert view['history'][0]['reference'] == second and view['next_history_reference'] == first
    assert view['history'][0]['body_sha256'] == store.read_revision(BlobRef(**second))['record']['body']['sha256']
    assert 'not-established' in view['correction_acceptance']
    assert materials.open_materials(store, {'reference': first})['history'] == []
    assert asdict(store.current(p['request']['case_id'])) == third


def test_source_cursor_pages_same_version_after_new_revision(store):
    p = payload()
    for n in range(5):
        p['items'].append({'id': f'extra-{n}', 'role': 'source-evidence', 'status': 'include',
            'required': True, 'reason': '', 'source': {'source_id': f's-{n}', 'kind': 'content',
            'title': f'Source {n}', 'origin': 'synthetic:materials', 'text': f'Original {n}'}})
    ref = ResearchDelivery(store).deliver(p, submission_id='source-pages')['reference']
    first = materials.open_materials(store, {'reference': ref})
    assert first['source_page']['returned'] == 4 and first['source_page']['remaining'] == 2
    next_p = continued(store, p, {'reference': ref})
    later = ResearchDelivery(store).deliver(next_p, submission_id='later')['reference']
    second = materials.open_materials(store, {'reference': ref, 'source_cursor': first['next_source_cursor']})
    assert [x['index'] for x in second['source_page']['items']] == [4, 5]
    assert second['research']['reference'] == ref != later and second['next_source_cursor'] is None
    assert second['source_page']['items'][0]['open_request']['payload'] == second['source_page']['items'][0]['reference']


def test_omissions_and_external_locator_stay_visible_without_fabricated_originals(store):
    p = payload()
    p['items'][2]['source'].update(kind='content', custody='external', text=None, limitations=['Original absent'])
    for n, state in enumerate(('missing', 'rights-excluded', 'unsupported', 'not-applicable')):
        p['items'].append({'id': f'gap-{n}', 'role': 'supporting-file', 'status': state,
                           'required': state != 'not-applicable', 'reason': 'Preserve original gap'})
    ref = ResearchDelivery(store).deliver(p, submission_id='gaps')['reference']
    view = materials.open_materials(store, {'reference': ref})
    assert view['status'] == 'partial' and view['inventory']['status'] == 'partial'
    assert [x['status'] for x in view['inventory']['items'][-4:]] == ['missing', 'rights-excluded', 'unsupported', 'not-applicable']
    src = view['source_page']['items'][0]
    assert src['status'] == 'external-only' and src['view']['data'] is None
    assert src['view']['original_verified'] is False and not view['executed']


@pytest.mark.parametrize('selected', ['absent', 'filing'])
def test_attachment_selection_cannot_open_an_unlisted_file_or_source(store, selected):
    _, ref = saved(store)
    unrelated = ResearchArtifacts(store).put('unrelated', ArtifactInput.from_bytes('absent', 'other.txt', 'text/plain', b'NOT IN RESEARCH'))
    view = materials.open_materials(store, {'reference': ref, 'attachment_id': selected})
    assert view['attachment']['status'] == 'unavailable' and 'data_base64' not in view['attachment']
    assert view['source_page']['returned'] == 1 and view['status'] == 'partial'
    assert asdict(unrelated) not in [i.get('reference') for i in view['inventory']['items']]


def test_source_failure_does_not_hide_the_valid_original_file(store, monkeypatch):
    _, ref = saved(store)
    def fail(_):
        raise StoreUnavailable('PRIVATE BACKEND DETAIL')
    monkeypatch.setattr(store, 'read_source', fail)
    view = materials.open_materials(store, {'reference': ref, 'attachment_id': 'html'})
    assert view['attachment']['status'] == 'readable' and view['source_page']['items'][0]['status'] == 'unavailable'
    assert view['status'] == 'partial' and b'PRIVATE BACKEND DETAIL' not in encoded(view)


def test_file_failure_preserves_sources_and_never_embeds_unverified_data(store, monkeypatch):
    _, ref = saved(store)
    original = ResearchArtifacts.read
    def fail(self, target, **kwargs):
        path = target['path'] if isinstance(target, dict) else target.path
        if '/html-original/' in path:
            raise StoreUnavailable('PRIVATE FILE DETAIL')
        return original(self, target, **kwargs)
    monkeypatch.setattr(ResearchArtifacts, 'read', fail)
    view = materials.open_materials(store, {'reference': ref, 'attachment_id': 'html'})
    assert view['attachment']['status'] == 'unavailable' and 'data_base64' not in view['attachment']
    assert view['source_page']['items'][0]['status'] == 'stored-representation'
    assert b'PRIVATE FILE DETAIL' not in encoded(view)


def test_source_display_budget_omits_whole_view_not_original_or_reference(store, monkeypatch):
    _, ref = saved(store)
    monkeypatch.setattr(materials, 'MAX_SOURCE_DISPLAY_BYTES', 1)
    view = materials.open_materials(store, {'reference': ref})
    row = view['source_page']['items'][0]
    assert row['view'] is None and row['display_status'] == 'not-included'
    assert row['status'] == 'stored-representation' and view['source_page']['total'] == 1
    assert row['open_request']['operation'] == 'read-source'


def test_panel_capacity_is_local_and_keeps_research_body(store, monkeypatch):
    _, ref = saved(store)
    monkeypatch.setattr(materials, 'MAX_MATERIAL_BYTES', 1)
    room = ReadingRoom(store).build(room_plan(ref))
    assert room['materials']['status'] == 'not-included' and room['status'] == 'partial'
    assert room['readings'][0]['status'] == 'readable'


def test_legacy_body_has_no_invented_inventory(store):
    ref = store.submit_revision('plain', RevisionInput('plain', 'r1', 'full', 'Original body', 'Mandate', 'legacy', unresolved=('Missing',)))
    view = materials.open_materials(store, {'reference': asdict(ref)})
    assert view['inventory']['status'] == 'not-declared' and view['attachment']['status'] == 'not-requested'
    assert view['history'] == []


@pytest.mark.parametrize('change', [
    {'attachment_id': '../private'}, {'history_limit': True}, {'source_limit': 0}, {'source_limit': 5},
    {'artifact': {}}, {'source_cursor': {'schema': 'wrong'}}, {'reference': {}},
])
def test_invalid_material_requests_reject_before_store_access(change):
    ref = {'repository': 'example/repo', 'commit': 'a'*40,
           'path': 'research/cases/example/revisions/r1/record.json', 'sha256': 'b'*64}
    with pytest.raises(RecordError):
        materials.material_plan({'reference': ref, **change})


def test_cursor_for_another_revision_is_not_reinterpreted(store):
    _, ref = saved(store)
    cursor = {'schema': 'research-resume-cursor-v1', 'reference': {**ref, 'commit': 'b'*40}, 'source_offset': 0}
    with pytest.raises(RecordError, match='CURSOR_MISMATCH'):
        materials.open_materials(store, {'reference': ref, 'source_cursor': cursor})


@pytest.mark.parametrize('confirmed', [None, False, 1, 'true'])
def test_public_material_permission_cannot_be_inherited_and_leaves_no_host_output(host, tmp_path, confirmed):
    ref = {'repository': host.binding.repository, 'commit': 'a'*40,
           'path': 'research/cases/example/revisions/r1/record.json', 'sha256': 'b'*64}
    data = {'plan': room_plan(ref), 'public_research_custody_confirmed': True,
            'public_feedback_custody_confirmed': True, 'public_material_custody_confirmed': confirmed}
    before = host.git.head()
    with pytest.raises(RecordError, match='PUBLIC_MATERIAL_NOT_CONFIRMED'):
        invoke(host, tmp_path/'must-not-exist', 'reading-room', data)
    assert not (tmp_path/'must-not-exist').exists() and host.git.head() == before


@pytest.mark.parametrize('selected,index', [('model', 1), ('archive', 3)])
def test_native_host_saves_no_data_and_reopens_selected_original(host, tmp_path, selected, index):
    p = payload()
    if selected == 'archive':
        from test_zip_artifacts import zip_item
        p['items'].append(zip_item())
    saved = invoke(host, tmp_path/'delivery', 'deliver', p, 'original')['result']['reference']
    before, writes = host.git.head(), host.writes
    data = {'plan': room_plan(saved, attachment_id=selected), 'public_research_custody_confirmed': True,
            'public_material_custody_confirmed': True}
    report = invoke(host, tmp_path/'room', 'reading-room', data)
    assert report['status'] == 'completed' and report['write'] == 'not-requested'
    assert host.git.head() == before and host.writes == writes
    view = report['result']['materials']['view']
    assert base64.b64decode(view['attachment']['data_base64']) == base64.b64decode(p['items'][index]['artifact']['data_base64'])
    assert view['attachment']['metadata']['media_type'] == p['items'][index]['artifact']['media_type']
    page = (tmp_path/'room/reading-room.html').read_text()
    assert '下载已核验原附件' in page and 'renderMaterials();' in page
    assert '<iframe' not in page and 'window.UNTRUSTED=1</script>' not in page
    again = invoke(host, tmp_path/'fresh', 'reading-room', data)
    assert encoded(again['result']) == encoded(report['result']) and host.writes == writes


def test_final_permission_loss_prevents_export(store, monkeypatch):
    _, ref = saved(store)
    original = materials.open_materials
    def lose(*args, **kwargs):
        value = original(*args, **kwargs)
        def denied():
            raise StoreUnavailable('ACCESS REVOKED')
        monkeypatch.setattr(store.backend, 'head', denied)
        return value
    monkeypatch.setattr(materials, 'open_materials', lose)
    with pytest.raises(StoreUnavailable):
        ReadingRoom(store).build(room_plan(ref))


def exercise_installed_materials(outside, python, env, store):
    """Reuse the one existing wheel installation; a second process only reads."""
    script = r'''
import base64,json,sys
from pathlib import Path
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.research_delivery import ResearchDelivery
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.interfaces.reading_page import render_page
s=VersionedStore(GitStore(Path(sys.argv[1])))
p=json.load(sys.stdin)
ref=ResearchDelivery(s).deliver(p,submission_id='installed-materials')['reference']
plan={'as_of':'2026-10-17T08:00:00+08:00','reference':ref,'materials':{'reference':ref,'attachment_id':'model'}}
head=s.backend.head()
r=ReadingRoom(s).build(plan)
assert base64.b64decode(r['materials']['view']['attachment']['data_base64'])==base64.b64decode(p['items'][1]['artifact']['data_base64'])
assert s.backend.head()==head
Path('material-plan.json').write_text(json.dumps(plan))
Path('material-view.json').write_text(json.dumps(r,sort_keys=True))
Path('installed-materials.html').write_text(render_page(r))
'''
    data = payload()
    data['request']['case_id'] = 'installed-material-navigation'
    result = subprocess.run([str(python), '-I', '-c', script, str(store)], cwd=outside, env=env,
                            input=json.dumps(data), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    second = r'''
import json,sys
from pathlib import Path
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.reading_room import ReadingRoom
s=VersionedStore(GitStore(Path(sys.argv[1])));h=s.backend.head()
r=ReadingRoom(s).build(json.loads(Path('material-plan.json').read_text()))
assert json.dumps(r,sort_keys=True)==Path('material-view.json').read_text()
assert s.backend.head()==h and r['materials']['view']['attachment']['executed'] is False
'''
    result = subprocess.run([str(python), '-I', '-c', second, str(store)], cwd=outside, env=env,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
