"""Normal Room consumers and export boundaries, using retained synthetic inputs."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import subprocess

import pytest

from decision_kernel.application import reading_participant
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import reading_commands, reading_participant_page, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_participant_reading import PREPARED, packet, prepare, saved
from test_research_runtime import CODE, host, inputs


def plan(ref, **query):
    return {'as_of': PREPARED, 'participants': {'reference': asdict(ref), **query}}


def test_normal_room_reopens_exact_family_and_preserves_whole_scope(saved):
    store, ref, _ = saved
    before = store.backend.head()
    value = plan(ref, family='holders-funds', limit=1)
    original = ReadingRoom(store).build(value)
    fresh = VersionedStore(GitStore(store.backend.root))
    restored = reading_commands.perform(fresh, {'plan': value}, public=False)
    assert restored['participants'] == original['participants']
    assert original['participants']['view']['scope']['accepted_rows'] == 11
    next_plan = deepcopy(value)
    next_plan['participants'] = original['participants']['next_plan']
    next_page = ReadingRoom(fresh).build(next_plan)
    assert next_page['participants']['view']['scope'] == original['participants']['view']['scope']
    assert next_page['participants']['view']['page']['offset'] == 1
    assert before == fresh.backend.head() and restored['persistence'] == 'not-written'


@pytest.mark.parametrize('flag', [None, False, 'true', 1])
def test_public_source_gate_precedes_any_native_io(host, tmp_path, flag):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    ref, _ = prepare(writer)
    payload = {'plan': plan(ref), 'public_research_custody_confirmed': True,
               'public_material_custody_confirmed': True, 'public_sources_custody_confirmed': flag}
    calls, writes, before = len(host.requests), host.writes, host.git.head()
    with pytest.raises(RecordError, match='PUBLIC_PARTICIPANT'):
        research_runtime.run_command(host.binding, api=host, inputs=inputs('reading-room', payload),
                                     code_sha=CODE, output=tmp_path / 'denied')
    assert not (tmp_path / 'denied').exists()
    assert (len(host.requests), host.writes, host.git.head()) == (calls, writes, before)


def test_native_private_room_writes_matching_html_without_public_rights_claim(host, tmp_path):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    ref, _ = prepare(writer)
    before, writes = host.git.head(), host.writes
    report = research_runtime.run_command(host.binding, api=host,
        inputs=inputs('reading-room', {'plan': plan(ref, family='holders-funds')}),
        code_sha=CODE, output=tmp_path / 'room', run_id='123', attempt='1')
    assert report['status'] == 'completed' and report['repository_visibility'] == 'private'
    assert report['write'] == 'not-requested' and host.writes == writes and host.git.head() == before
    result = report['result']
    assert result['participants']['view']['scope']['declared_families'] == 8
    assert result['participants']['view']['writes'] == result['participants']['view']['source_requests'] == 0
    assert (tmp_path / 'room/reading-room.html').read_text() == render_page(result)
    assert result['human_acceptance'] == 'not-established'


def test_no_participant_plan_keeps_original_shape_and_renders_without_container(saved, monkeypatch):
    store, _, _ = saved
    monkeypatch.setattr(reading_participant, 'open_participants', lambda *a, **k: pytest.fail('unexpected reader'))
    original = ReadingRoom(store).build({'as_of': PREPARED})
    assert ReadingRoom(store).build({'as_of': PREPARED, 'participants': None}) == original
    assert 'participants' not in original and 'id="participants-content"' not in render_page(original)
    # The conditional panel must not make existing Room pages throw at load time.
    subprocess.run(['node', '-e', 'const D={},$=()=>null;\n' + reading_participant_page.JS], check=True)


def test_literal_source_text_is_not_executable_and_model_is_exact(saved):
    store, _, original = saved
    body = deepcopy(original)
    body['coverage_note'] = '</script><img src=x onerror=alert(1)>'
    ref = packet(store, body)
    model = ReadingRoom(store).build(plan(ref))
    page = render_page(model)
    assert '</script><img' not in page
    raw = page.split('<script id="data" type="application/json">', 1)[1].split('</script>', 1)[0]
    assert encoded(json.loads(raw)) == encoded(model)


def test_display_limit_omits_whole_panel_and_retains_read_plan(saved, monkeypatch):
    store, ref, _ = saved
    monkeypatch.setattr(reading_participant, 'MAX_PARTICIPANT_PANEL_BYTES', 1)
    model = ReadingRoom(store).build(plan(ref))
    assert model['status'] == 'partial' and model['participants']['status'] == 'not-included'
    assert 'view' not in model['participants']
    assert encoded(model['participants']['plan']['reference']) == encoded(asdict(ref))


def test_private_visibility_reversal_cannot_release_participant_output(host, tmp_path, monkeypatch):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    ref, _ = prepare(writer)
    actual = reading_participant.open_participants
    def reverse(*args, **kwargs):
        result = actual(*args, **kwargs)
        host.meta['private'] = False
        return result
    monkeypatch.setattr(reading_participant, 'open_participants', reverse)
    with pytest.raises(RecordError, match='PRIVATE_UPGRADE_REVERSED'):
        research_runtime.run_command(host.binding, api=host,
            inputs=inputs('reading-room', {'plan': plan(ref)}), code_sha=CODE, output=tmp_path / 'reversed')
    report = json.loads((tmp_path / 'reversed/report.json').read_text())
    assert report['status'] != 'completed' and 'result' not in report
    assert not (tmp_path / 'reversed/reading-room.html').exists()
    assert not (tmp_path / 'reversed/inputs.json').exists()
