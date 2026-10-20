"""Participant family paging and checked command composition on retained sources."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from decision_kernel.application import reading_participant as panel
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.interfaces import participant_commands
from test_participant_reading import saved


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
