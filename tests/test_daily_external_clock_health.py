"""External entry metadata does not certify a provider or a business delivery."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from decision_kernel.application import runtime_health as health
from decision_kernel.application.daily_clock import EXTERNAL_RUN_TITLE, PROBE_RUN_TITLE
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.interfaces import runtime_health_commands as command
from test_daily_schedule_health import CURRENT, inspect, natural, schedules
from test_runtime_health import (
    API, BINDING, CLOCK_CONTENT, DISPATCH_PAGE, NOW, POLICY_CONTENT, SHA, STAMP,
    add_details, clock_content, fixture, policy_content, run,
)

ORDINARY_PAGE = '/actions/workflows/daily-market.yml/runs?branch=main&per_page=20'


def external(ident=951, *, at='2026-10-21T10:36:00Z', conclusion='success',
             status='completed', attempt=1, probe=False):
    value = run(4, 'daily-market.yml', ident, conclusion, stamp=at, status=status, attempt=attempt)
    value['display_title'] = PROBE_RUN_TITLE if probe else EXTERNAL_RUN_TITLE
    return value


def entries(values, rows, *, total=None, ordinary=True):
    values[DISPATCH_PAGE] = {'total_count': len(rows) if total is None else total, 'workflow_runs': rows}
    for row in rows:
        operation = ('clock-probe' if row['display_title'] == PROBE_RUN_TITLE else
                     'daily' if row['display_title'] == EXTERNAL_RUN_TITLE else 'status')
        add_details(values, row, operation, 'dos2-daily-market')
    if ordinary:
        values[ORDINARY_PAGE] = deepcopy(values[DISPATCH_PAGE])


def external_fixture(rows=None):
    values = fixture()
    values[CLOCK_CONTENT] = clock_content('external-cron')
    entries(values, [external()] if rows is None else rows)
    return values


def test_external_primary_checks_daily_job_without_requiring_retired_native_slot():
    daily, report, api = inspect(external_fixture())
    observed = daily['external_entry']
    assert daily['schedule']['status'] == 'not-observed'
    assert observed['status'] == 'daily-job-success'
    assert observed['native_entry_status'] == 'native-success'
    assert observed['latest_detail']['observed_operation'] == 'daily'
    assert observed['natural_origin'] == 'unverified'
    assert observed['provider_configuration'] == observed['business_result'] == 'not-inspected'
    assert observed['historical_gaps_resolved'] is False
    assert observed['clock_policy']['main'] == SHA
    assert daily['status'] == report['status'] == 'metadata-ok'
    assert api.calls.count(CLOCK_CONTENT) == 1 and api.calls.count(DISPATCH_PAGE) == 2
    assert all(path == '' or path.startswith('/git/ref/heads/') or path.startswith('/actions/')
               or path in {CLOCK_CONTENT, POLICY_CONTENT} for path in api.calls)
    assert report['writes'] == report['dispatches'] == report['notifications'] == 0
    text = encoded(report).decode()
    assert 'PRIVATE_SENTINEL' not in text and 'SECRET' not in text
    markdown = command.markdown(report)
    assert '外部配置与自然来源未核验' in markdown and '当前主时钟：external-cron' in markdown


def test_unselected_external_entry_does_not_replace_or_pollute_native_acceptance():
    values = fixture(); schedules(values, [natural()]); entries(values, [external()])
    values['/actions/runs/951/attempts/1/jobs?per_page=100']['jobs'][0]['name'] = 'visibility'
    daily, report, _ = inspect(values)
    assert daily['external_entry']['status'] == 'daily-not-successful'
    assert daily['schedule']['status'] == 'native-success'
    assert report['status'] == 'metadata-ok'
    assert '当前不作为主时钟验收依据' in command.markdown(report)


def test_native_success_does_not_fill_missing_selected_external_entry():
    values = fixture(); schedules(values, [natural()]); values[CLOCK_CONTENT] = clock_content('external-cron')
    daily, report, _ = inspect(values)
    assert daily['schedule']['status'] == 'native-success'
    assert daily['external_entry']['status'] == 'not-observed'
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('only_probe', [False, True])
def test_probe_uses_its_artifact_but_never_counts_as_external_production(only_probe):
    probe = external(952, at='2026-10-21T10:50:00Z', probe=True)
    values = external_fixture([probe] if only_probe else [probe, external()])
    daily, report, _ = inspect(values)
    assert daily['latest']['observed_operation'] == 'clock-probe'
    assert daily['latest']['artifact']['name'] == 'dos2-daily-clock-952-1'
    assert daily['latest']['artifact_state'] == 'metadata-available'
    observed = daily['external_entry']
    assert observed['latest_probe']['id'] == 952
    assert observed['matched_entries'] == (0 if only_probe else 1)
    assert observed['status'] == ('not-observed' if only_probe else 'daily-job-success')
    assert report['status'] == ('needs-attention' if only_probe else 'metadata-ok')


@pytest.mark.parametrize('problem', ['skipped-daily', 'only-visibility', 'old-main', 'missing-artifact', 'expired-artifact'])
def test_native_success_marker_alone_cannot_certify_selected_production(problem):
    row = external()
    if problem == 'old-main': row['head_sha'] = 'b' * 40
    values = external_fixture([row])
    jobs = values['/actions/runs/951/attempts/1/jobs?per_page=100']
    if problem == 'skipped-daily':
        jobs['jobs'][0]['conclusion'] = 'skipped'
        jobs['jobs'].append({**jobs['jobs'][0], 'id': 9511, 'name': 'visibility', 'conclusion': 'success'})
        jobs['total_count'] = 2
    elif problem == 'only-visibility': jobs['jobs'][0]['name'] = 'visibility'
    elif problem == 'missing-artifact':
        values['/actions/runs/951/artifacts?per_page=100'] = {'total_count': 0, 'artifacts': []}
    elif problem == 'expired-artifact':
        values['/actions/runs/951/artifacts?per_page=100']['artifacts'][0]['expired'] = True
    daily, report, _ = inspect(values)
    expected = ('policy-at-run-unverified' if problem == 'old-main' else
                'delivery-metadata-unavailable' if 'artifact' in problem else 'daily-not-successful')
    assert daily['external_entry']['native_entry_status'] == 'native-success'
    assert daily['external_entry']['status'] == expected
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('earlier,want', [
    (external(950, at='2026-10-21T10:35:10Z', conclusion=None, status='in_progress'), 'in-progress'),
    (external(950, at='2026-10-21T10:35:10Z', attempt=2), 'unknown'),
])
def test_other_same_slot_active_or_unproven_attempt_is_not_hidden_by_later_success(earlier, want):
    daily, report, _ = inspect(external_fixture([external(), earlier]))
    assert daily['external_entry']['status'] == want
    assert len(daily['external_entry']['current_slot_runs']) == 2
    assert report['status'] == 'needs-attention'


def test_later_success_retains_failed_external_attempt_without_claiming_recovery():
    failure = external(950, at='2026-10-20T10:36:00Z', conclusion='failure')
    daily, _, _ = inspect(external_fixture([external(), failure]))
    observed = daily['external_entry']
    assert observed['status'] == 'daily-job-success'
    assert observed['last_visible_non_success']['run']['id'] == 950
    assert observed['historical_gaps_resolved'] is False
    failed = external(952, at='2026-10-21T10:50:00Z', conclusion='failure')
    daily, report, _ = inspect(external_fixture([failed, external(), failure]))
    assert daily['external_entry']['status'] == 'native-non-success'
    assert daily['external_entry']['last_visible_success']['run']['id'] == 951
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('covers_slot', [False, True])
def test_slot_coverage_is_computed_from_all_dispatches_before_entry_filter(covers_slot):
    values = external_fixture([])
    base = datetime.fromisoformat('2026-10-20T10:36:00+00:00' if covers_slot else '2026-10-21T10:36:00+00:00')
    manuals = [run(4, 'daily-market.yml', 800 + i, stamp=(base + timedelta(seconds=i)).isoformat()) for i in range(20)]
    entries(values, manuals, total=30)
    daily, report, _ = inspect(values)
    observed = daily['external_entry']
    assert observed['matched_entries'] == 0
    assert observed['recent_scope']['returned'] == 20
    assert observed['recent_scope']['complete_history'] is False
    assert observed['recent_scope']['expected_slot_covered'] is covers_slot
    assert observed['status'] == ('not-observed' if covers_slot else 'unknown')
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('primary', ['external-cron', 'github-schedule'])
def test_invalid_external_page_remains_local_to_its_selected_dependency(primary):
    values = fixture(); schedules(values, [natural()]); values[CLOCK_CONTENT] = clock_content(primary)
    values[DISPATCH_PAGE]['total_count'] = 100
    daily, report, _ = inspect(values)
    assert daily['external_entry'] == {'status': 'unknown', 'error': 'HEALTH_LIST_INCOMPLETE'}
    assert report['status'] == ('needs-attention' if primary == 'external-cron' else 'metadata-ok')


def test_same_entry_conflicting_across_pages_cannot_be_selected():
    values = external_fixture()
    values[DISPATCH_PAGE]['workflow_runs'][0]['display_title'] = PROBE_RUN_TITLE
    daily, report, _ = inspect(values)
    assert daily['external_entry']['status'] == daily['status'] == 'changed-during-inspection'
    assert report['status'] == 'needs-attention'


def test_changed_dispatch_page_is_not_accepted_or_retried():
    values = external_fixture(); api = API(values)
    def mutate(path, calls):
        if path == DISPATCH_PAGE and calls.count(path) == 2:
            return {'total_count': 2, 'workflow_runs': [external(952), external()]}
    api.mutate = mutate
    daily, report, _ = inspect(values, api=api)
    assert daily['external_entry']['status'] == 'changed-during-inspection'
    assert report['status'] == 'needs-attention'
    assert api.calls.count(DISPATCH_PAGE) == 2


def test_extra_external_artifact_clock_is_checked_even_outside_ordinary_details():
    values = external_fixture()
    rows = [natural(1000 + i, at='2026-10-21T10:58:00Z') for i in range(20)]
    values[ORDINARY_PAGE] = {'total_count': 21, 'workflow_runs': rows}
    for row in rows: add_details(values, row, 'daily', 'dos2-daily-market')
    values['/actions/runs/951/artifacts?per_page=100']['artifacts'][0]['created_at'] = '2026-10-21T12:00:00Z'
    daily, report, _ = inspect(values)
    assert daily['external_entry'] == {'status': 'unknown', 'error': 'HEALTH_FUTURE_EXTERNAL_ARTIFACT'}
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('kind', ['wrong-path', 'bad-blob', 'wrong-primary', 'extra-field', 'duplicate-field'])
def test_clock_policy_is_fixed_main_strict_and_not_a_caller_claim(kind):
    values = fixture(); schedules(values, [natural()])
    if kind == 'wrong-path': values[CLOCK_CONTENT]['path'] = 'foreign.json'
    elif kind == 'bad-blob': values[CLOCK_CONTENT]['sha'] = 'f' * 40
    else:
        raw = b'{"schema":"daily-market-clock-v1","primary":"github-schedule"}'
        if kind == 'wrong-primary': raw = raw.replace(b'github-schedule', b'caller-choice')
        if kind == 'extra-field': raw = raw.replace(b'}', b',"provider_enabled":true}')
        if kind == 'duplicate-field': raw = raw.replace(b'}', b',"primary":"external-cron"}')
        values[CLOCK_CONTENT] = policy_content(raw, path='config/daily-market-clock.json')
    daily, report, api = inspect(values)
    assert daily['clock_policy']['status'] == 'unknown'
    assert daily['schedule']['status'] == 'native-success'
    assert report['status'] == 'needs-attention'
    assert api.calls.count(CLOCK_CONTENT) == 1 and DISPATCH_PAGE not in api.calls


@pytest.mark.parametrize('target,occurrence', [(CLOCK_CONTENT, 1), (DISPATCH_PAGE, 1), (DISPATCH_PAGE, 2)])
def test_clock_or_dispatch_denial_stops_all_later_reads(tmp_path, target, occurrence):
    api = API()
    def deny(path, calls):
        if path == target and calls.count(path) == occurrence:
            raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    api.mutate = deny
    with pytest.raises(RecordError, match='REMOTE_NOT_AUTHORIZED'):
        command.execute(BINDING, api, tmp_path / 'out', as_of=CURRENT)
    assert api.calls[-1] == target and api.calls.count(target) == occurrence
    assert not (tmp_path / 'out/health.md').exists()


def test_worst_case_one_extra_external_detail_keeps_62_gets_and_original_byte_bounds():
    values = fixture(failure=True, self_run=True)
    values[CLOCK_CONTENT] = clock_content('external-cron')
    rows = [natural(1000 + i, at=STAMP, conclusion='failure' if i == 19 else 'success') for i in range(20)]
    values[ORDINARY_PAGE] = {'total_count': 23, 'workflow_runs': rows}
    schedules(values, rows, total=21)
    for row in rows: add_details(values, row, 'daily', 'dos2-daily-market')
    original = values[DISPATCH_PAGE]['workflow_runs']
    entries(values, [*original, external(at='2026-10-16T10:36:00Z')], ordinary=False)
    api = API(values)
    report = health.inspect_runtime(BINDING, api, as_of=NOW, backup_run_id=101,
                                    self_run={'id': 999, 'run_attempt': 1})
    assert report['native_reads'] == len(api.calls) == health.MAX_READS == 62
    assert report['workflows'][-1]['external_entry']['latest_detail']['run']['id'] == 951
    assert api.calls.count('/actions/runs/951') == 1
    assert report['native_metadata_bytes'] < 8 * 1024 * 1024
    assert len(encoded(report)) < 256 * 1024
    assert report['writes'] == report['dispatches'] == report['notifications'] == 0
