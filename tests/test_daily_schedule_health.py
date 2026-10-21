"""Only native-shaped metadata/configuration; no producer, source or remote write."""
import base64
from datetime import datetime, timedelta
import json

import pytest

from decision_kernel.application import runtime_health as health
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.interfaces import runtime_health_commands as command
from test_runtime_health import (
    API, BINDING, CLOCK_CONTENT, DISPATCH_PAGE, NOW, POLICY_CONTENT, SCHEDULE_PAGE,
    add_details, fixture, policy_content, run,
)

CURRENT = '2026-10-21T11:00:00Z'


def natural(ident=901, *, at='2026-10-21T10:36:00Z', conclusion='success', status='completed', attempt=1):
    value = run(4, 'daily-market.yml', ident, conclusion, stamp=at, status=status, attempt=attempt)
    value['event'] = 'schedule'
    return value


def inspect(values, *, at=CURRENT, api=None):
    api = API(values) if api is None else api
    report = health.inspect_runtime(BINDING, api, as_of=at)
    return report['workflows'][-1], report, api


def schedules(values, rows, *, total=None):
    values[SCHEDULE_PAGE] = {'total_count': len(rows) if total is None else total, 'workflow_runs': rows}


def test_schedule_page_is_independent_of_twenty_manual_successes():
    values = fixture()
    manuals = [run(4, 'daily-market.yml', 800 + i, stamp='2026-10-21T10:58:00Z') for i in range(20)]
    values['/actions/workflows/daily-market.yml/runs?branch=main&per_page=20'] = {
        'total_count': 100, 'workflow_runs': manuals}
    for item in manuals:
        add_details(values, item, 'status', 'dos2-daily-market')
    schedules(values, [natural()])
    daily, report, api = inspect(values)
    assert daily['latest']['run']['event'] == 'workflow_dispatch'
    assert daily['latest']['observed_operation'] == 'status'
    assert daily['schedule']['status'] == 'native-success'
    assert daily['schedule']['current_slot_runs'][0]['run']['id'] == 901
    assert report['status'] == 'metadata-ok'
    assert api.calls.count(SCHEDULE_PAGE) == 2
    assert not any('/actions/runs/901' in path for path in api.calls)
    assert daily['schedule']['jobs_and_artifacts'] == 'not-inspected-for-this-observation'
    assert daily['schedule']['business_result'] == 'not-inspected'


@pytest.mark.parametrize('status,conclusion,want', [
    ('completed', 'failure', 'native-non-success'), ('completed', 'timed_out', 'native-non-success'),
    ('queued', None, 'in-progress'), ('in_progress', None, 'in-progress'),
])
def test_manual_green_cannot_hide_natural_failure_or_active_run(status, conclusion, want):
    values = fixture()
    schedules(values, [natural(conclusion=conclusion, status=status),
                       natural(900, at='2026-10-20T10:36:00Z')])
    daily, report, _ = inspect(values)
    schedule = daily['schedule']
    assert schedule['status'] == want
    assert schedule['latest']['run']['id'] == 901
    assert schedule['last_visible_success']['run']['id'] == 900
    assert daily['latest']['run']['conclusion'] == 'success'
    assert daily['status'] == report['status'] == 'needs-attention'
    assert schedule['root_cause'] == 'not-established'
    markdown = command.markdown(report)
    assert r'workflow\_dispatch' in markdown and 'schedule' in markdown
    assert '自然子节只核原生运行摘要' in markdown


def test_later_success_does_not_claim_old_failure_was_recovered():
    values = fixture()
    schedules(values, [natural(), natural(900, at='2026-10-20T10:36:00Z', conclusion='failure')])
    daily, _, _ = inspect(values)
    schedule = daily['schedule']
    assert schedule['status'] == 'native-success'
    assert schedule['last_visible_non_success']['run']['id'] == 900
    assert schedule['historical_gaps_resolved'] is False


@pytest.mark.parametrize('earlier,want', [
    (natural(900, at='2026-10-21T10:35:10Z', conclusion=None, status='in_progress'), 'in-progress'),
    (natural(900, at='2026-10-21T10:35:10Z', attempt=2), 'unknown'),
])
def test_later_same_slot_success_cannot_hide_other_active_or_unproven_attempt(earlier, want):
    values = fixture()
    schedules(values, [natural(), earlier])
    daily, report, _ = inspect(values)
    assert daily['schedule']['latest']['run']['conclusion'] == 'success'
    assert len(daily['schedule']['current_slot_runs']) == 2
    assert daily['schedule']['status'] == want
    assert daily['status'] == report['status'] == 'needs-attention'


def test_same_run_conflicting_between_filtered_and_ordinary_pages_is_changed():
    values = fixture()
    schedules(values, [natural(402)])
    daily, report, _ = inspect(values)
    assert daily['schedule']['status'] == daily['status'] == 'changed-during-inspection'
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('at,business_date', [
    ('2026-10-17T10:34:59Z', '20261016'), ('2026-10-17T10:35:00Z', None),
    ('2026-10-18T15:00:00Z', None), ('2026-10-19T10:34:59Z', None),
    ('2026-10-19T10:35:00Z', '20261019'), ('2026-10-20T10:34:59Z', '20261019'),
    ('2026-10-20T10:35:00Z', '20261020'),
])
def test_expected_slot_preserves_original_weekday_and_strict_24h_admission(at, business_date):
    daily, _, _ = inspect(fixture(), at=at)
    slot = daily['schedule']['expected_slot']
    assert (slot['business_date'] if slot else None) == business_date
    if business_date is None:
        assert daily['schedule']['status'] == 'no-recent-slot'
    assert daily['schedule']['historical_gaps_resolved'] is False


def test_run_uses_creation_slot_even_if_queue_wait_crosses_next_slot():
    values = fixture()
    queued = natural(at='2026-10-20T10:36:00Z')
    queued['run_started_at'] = queued['updated_at'] = '2026-10-21T10:36:00Z'
    schedules(values, [queued])
    daily, _, _ = inspect(values)
    assert daily['schedule']['latest']['admission_slot']['business_date'] == '20261020'
    assert daily['schedule']['expected_slot']['business_date'] == '20261021'
    assert daily['schedule']['status'] == 'not-observed'


def test_changed_observation_slot_is_not_reported_as_freshly_missing(monkeypatch):
    values = fixture()
    clocks = iter(['2026-10-21T10:34:59Z', '2026-10-21T10:35:00Z'])
    class CrossingClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(next(clocks).replace('Z', '+00:00'))
    monkeypatch.setattr(health, 'datetime', CrossingClock)
    report = health.inspect_runtime(BINDING, API(values))
    daily = report['workflows'][-1]
    assert daily['schedule']['observation_slot_changed'] is True
    assert daily['schedule']['status'] == daily['status'] == 'changed-during-inspection'
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('field,value', [
    ('path', 'config/foreign.json'), ('type', 'symlink'), ('encoding', 'utf-8'),
    ('size', True), ('sha', 'b' * 40), ('content', '!invalid-base64'),
])
def test_pinned_policy_native_shape_and_original_blob_are_required(field, value):
    values = fixture(); values[POLICY_CONTENT][field] = value
    daily, report, api = inspect(values)
    assert daily['schedule']['status'] == 'unknown'
    assert daily['status'] == report['status'] == 'needs-attention'
    assert api.calls.count(POLICY_CONTENT) == 1 and SCHEDULE_PAGE not in api.calls
    assert 'seed_reference' not in encoded(report).decode()


@pytest.mark.parametrize('kind', ['duplicate', 'constant', 'policy'])
def test_valid_blob_cannot_hide_invalid_json_or_unadopted_policy(kind):
    values = fixture()
    raw = json.dumps(json.loads(base64.b64decode(values[POLICY_CONTENT]['content']))).encode()
    if kind == 'duplicate':
        raw = raw.replace(b'"enabled": true', b'"enabled": true, "enabled": true')
    elif kind == 'constant':
        raw = raw.replace(b'"lookback_days": 119', b'"lookback_days": NaN')
    else:
        raw = raw.replace(b'35 10 * * 1-5', b'0 0 * * *')
    values[POLICY_CONTENT] = policy_content(raw)
    daily, report, _ = inspect(values)
    assert daily['schedule']['status'] == 'unknown'
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('kind', ['empty-count-drift', 'short-page', 'wrong-event', 'wrong-workflow', 'rerun'])
def test_incomplete_or_ineligible_schedule_evidence_is_unknown(kind):
    values = fixture(); item = natural()
    if kind == 'empty-count-drift':
        schedules(values, [], total=1)
    else:
        if kind == 'wrong-event': item['event'] = 'workflow_dispatch'
        if kind == 'wrong-workflow': item['workflow_id'] = 99
        if kind == 'rerun': item['run_attempt'] = 2
        schedules(values, [item], total=21 if kind == 'short-page' else None)
    daily, report, _ = inspect(values)
    assert daily['schedule']['status'] == 'unknown'
    assert daily['status'] == report['status'] == 'needs-attention'


@pytest.mark.parametrize('covers_slot', [True, False])
def test_twenty_row_history_needs_only_proof_of_current_window(covers_slot):
    values = fixture()
    base = datetime.fromisoformat('2026-10-20T10:36:00+00:00' if covers_slot else '2026-10-21T10:36:00+00:00')
    rows = [natural(901 + i, at=(base + timedelta(seconds=i)).isoformat()) for i in range(20)]
    schedules(values, rows, total=30)
    daily, _, _ = inspect(values)
    assert daily['schedule']['recent_scope']['complete_history'] is False
    assert daily['schedule']['recent_scope']['expected_slot_covered'] is covers_slot
    assert daily['schedule']['status'] == ('not-observed' if covers_slot else 'unknown')


def test_schedule_and_main_changes_are_visible_without_retry():
    for target in ('schedule', 'main'):
        values = fixture(); schedules(values, [natural()]); api = API(values)
        def mutate(path, calls):
            if target == 'schedule' and path == SCHEDULE_PAGE and calls.count(path) == 2:
                return {'total_count': 2, 'workflow_runs': [natural(902), natural()]}
            if target == 'main' and path == '/git/ref/heads/main' and calls.count(path) == 2:
                return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'd' * 40}}
        api.mutate = mutate
        daily, report, _ = inspect(values, api=api)
        assert daily['schedule']['status'] == daily['status'] == 'changed-during-inspection'
        assert report['status'] == 'needs-attention'
        assert api.calls.count(SCHEDULE_PAGE) == 2


def test_paused_policy_and_non_default_main_never_report_missing_trigger():
    for field in ('paused', 'default'):
        values = fixture()
        if field == 'paused':
            raw = base64.b64decode(values[POLICY_CONTENT]['content']).replace(b'"enabled": true', b'"enabled": false')
            values[POLICY_CONTENT] = policy_content(raw)
        else:
            values['']['default_branch'] = 'other'
        daily, report, _ = inspect(values)
        assert daily['schedule']['status'] == ('policy-paused' if field == 'paused' else 'unknown')
        assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('target,occurrence', [(POLICY_CONTENT, 1), (SCHEDULE_PAGE, 1), (SCHEDULE_PAGE, 2)])
def test_policy_or_schedule_transport_denial_stops_all_later_reads(tmp_path, target, occurrence):
    api = API()
    def deny(path, calls):
        if path == target and calls.count(path) == occurrence:
            raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    api.mutate = deny
    with pytest.raises(RecordError, match='REMOTE_NOT_AUTHORIZED'):
        command.execute(BINDING, api, tmp_path / 'out', as_of=CURRENT)
    assert api.calls[-1] == target and api.calls.count(target) == occurrence
    result = json.loads((tmp_path / 'out/health.json').read_bytes())
    assert result['status'] == 'unknown' and 'workflows' not in result
    assert not (tmp_path / 'out/health.md').exists()


def test_original_worst_case_plus_clock_observation_uses_59_gets_without_sensitive_payload():
    api = API(fixture(failure=True, self_run=True))
    report = health.inspect_runtime(BINDING, api, as_of=NOW,
        backup_run_id=101, self_run={'id': 999, 'run_attempt': 1})
    assert report['native_reads'] == len(api.calls) == 59
    assert health.MAX_READS == 62  # At most one additional external-entry detail.
    assert api.calls.count(POLICY_CONTENT) == 1 and api.calls.count(SCHEDULE_PAGE) == 2
    assert api.calls.count(CLOCK_CONTENT) == 1 and api.calls.count(DISPATCH_PAGE) == 2
    text = encoded(report).decode()
    assert all(value not in text for value in ('seed_reference', 'SECRET', 'PRIVATE_SENTINEL', 'archive_download_url'))
    assert report['writes'] == report['dispatches'] == report['notifications'] == 0
    assert report['native_metadata_bytes'] < health.MAX_METADATA_BYTES
    assert len(encoded(report)) < 256 * 1024
