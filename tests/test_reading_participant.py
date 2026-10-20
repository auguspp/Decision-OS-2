"""Participant family paging and checked command composition on retained sources."""
from copy import deepcopy
from dataclasses import asdict
import io
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application import reading_participant as panel
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT
from decision_kernel.interfaces import participant_commands, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_participant_reading import PREPARED, saved, saved_page_custody
from test_reading_scope import transport
from test_research_runtime import CODE, inputs


def test_family_pages_keep_whole_scope_refs_and_next_plan(saved):
    store, ref, _ = saved
    plan = {'reference': asdict(ref), 'family': 'holders-funds', 'limit': 1}
    first = panel.open_participants(store, plan)
    second = panel.open_participants(store, first['next_plan'])
    assert first['status'] == second['status'] == 'readable'
    a, b = first['view'], second['view']
    assert a['scope'] == b['scope'] and a['input_outcomes'] == b['input_outcomes']
    assert a['view_version'] == b['view_version'] and a['families'] == b['families']
    assert len(a['families']) == 8 and a['page']['returned'] == b['page']['returned'] == 1
    assert first['next_plan']['offset'] == 1 and a['rows'][0]['source_row'] != b['rows'][0]['source_row']
    assert a['source_requests'] == b['source_requests'] == 0
    assert a['history'] and all(h['family'] == 'holders-funds' for h in a['history'])


def test_missing_company_does_not_remove_family_status_or_coverage(saved):
    store, ref, _ = saved
    result = panel.open_participants(store, {'reference': asdict(ref), 'company_id': 'missing-company',
                                           'family': 'holders-funds'})
    view = result['view']
    assert not view['rows'] and view['scope']['accepted_rows'] == 11
    assert len(view['families']) == 8 and view['page']['query_rows'] == 0 and result['next_plan'] is None


@pytest.mark.parametrize('change', [{'offset': -1}, {'offset': True}, {'limit': 51}, {'family': 'all-people'},
                                   {'url': 'https://example.org'}, {'company_id': 'a', 'participant_id': 'b'}])
def test_invalid_panel_requests_fail_before_reading(saved, monkeypatch, change):
    store, ref, _ = saved
    monkeypatch.setattr(panel, 'read_participants', lambda *a, **k: pytest.fail('read before plan validation'))
    with pytest.raises(RecordError): panel.open_participants(store, {'reference': asdict(ref), **change})


def test_panel_size_does_not_silently_drop_original_failures(saved, monkeypatch):
    store, ref, _ = saved
    monkeypatch.setattr(panel, 'MAX_PARTICIPANT_PANEL_BYTES', 1)
    with pytest.raises(RecordError, match='DISPLAY_BOUND'):
        panel.open_participants(store, {'reference': asdict(ref)})


def test_command_uses_exact_ref_and_pure_reader_without_registering_new_host_action(saved):
    store, ref, _ = saved
    result = participant_commands.perform(store, {'reference': asdict(ref), 'company_id': 'security-002384-sz'})
    assert result['reference'] == asdict(ref) and result['query']['company_id'] == 'security-002384-sz'
    assert all(row['company_id'] == 'security-002384-sz' for f in result['families'] for row in f['rows'])
    with pytest.raises(RecordError, match='PAYLOAD'):
        participant_commands.perform(store, {'reference': asdict(ref), 'execute': True})
    assert not hasattr(participant_commands, 'OPERATION')


def test_native_room_reads_large_compressed_page_tail_with_original_scope(saved_page_custody, tmp_path):
    model, original, cases = saved_page_custody
    ref, body = cases['gzip']
    before, api = model.git.head(), transport(model)
    plan = {'as_of': PREPARED, 'participants': {'reference': asdict(ref), 'family': 'northbound',
                                              'offset': 1439, 'limit': 1}}
    output = tmp_path / 'compressed-room'
    report = research_runtime.run_command(model.binding, api=api,
        inputs=inputs('reading-room', {'plan': plan}), code_sha=CODE, output=output)
    part = report['result']['participants']
    view = part['view']
    assert report['status'] == 'completed' and report['repository_visibility'] == 'private'
    assert part['status'] == 'readable' and part['partial'] and part['next_plan'] is None
    assert view['page']['query_rows'] == 1440 and view['page']['returned'] == 1
    assert view['rows'][0]['raw'] == original['rows'][-1] and view['rows'][0]['source_row'] == 1724
    assert view['scope']['raw_rows'] == 1601 and view['scope']['accepted_rows'] == 1441
    assert view['scope']['rejected_rows'] == 160 and view['scope']['incomplete_inputs'] == 1
    assert len(view['families']) == 8
    assert encoded(view['input_outcomes'][0]['references']) == encoded(body['inputs'][0]['references'])
    assert view['input_outcomes'][0]['physical_pages'] == [original['physical_page']]
    assert view['input_outcomes'][0]['control']['error'] == 'SYNTHETIC_TERMINAL_FAILURE'
    assert len(encoded(view)) <= 768 * 1024
    assert (output / 'reading-room.html').read_text() == render_page(report['result'])
    assert view['source_requests'] == view['writes'] == model.writes == 0
    assert model.git.head() == before and 0 < api._requests < 512


def test_native_tail_authorization_failure_stops_room_before_neighbor_or_export(
        saved_page_custody, tmp_path):
    model, _, cases = saved_page_custody
    ref, body = cases['gzip']
    tail = body['inputs'][0]['references'][-1]['manifest']
    blob = model.git._run('rev-parse', tail['commit'] + ':' + tail['path']).stdout.decode().strip()
    target = API_ROOT + '/repos/' + model.binding.repository + '/git/blobs/' + blob
    api, calls = transport(model), []
    original, denied = api._opener, []
    class OneDeniedTail:
        def open(self, request, timeout):
            calls.append(request.full_url)
            if not denied and request.full_url == target:
                denied.append(len(calls) - 1)
                raise HTTPError(target, 403, 'Synthetic one-shot refusal', {}, io.BytesIO(b'Synthetic refusal'))
            return original.open(request, timeout)
    api._opener = OneDeniedTail()
    before, output = model.git.head(), tmp_path / 'denied-tail'
    plan = {'as_of': PREPARED, 'participants': {'reference': asdict(ref), 'family': 'public-seats',
                                              'company_id': 'no-matching-company'}}
    with pytest.raises(StoreUnavailable, match='^REMOTE_NOT_AUTHORIZED$'):
        research_runtime.run_command(model.binding, api=api,
            inputs=inputs('reading-room', {'plan': plan}), code_sha=CODE, output=output)
    assert denied == [len(calls) - 1] and calls[-1] == target
    report = json.loads((output / 'report.json').read_bytes())
    assert report['status'] == 'failed' and report['error'] == 'REMOTE_NOT_AUTHORIZED' and 'result' not in report
    assert sorted(path.name for path in output.iterdir()) == ['report.json']
    assert model.git.head() == before and model.writes == 0 and 0 < api._requests < 512
