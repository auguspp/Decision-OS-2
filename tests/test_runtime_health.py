"""Native-shaped metadata fixtures; no source collection, mutation or timer."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from decision_kernel.application import runtime_health as h
from decision_kernel.domain.records import RecordError, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import GitHubAPI, GitHubBinding
from decision_kernel.interfaces import runtime_health_commands as command

ROOT = Path(__file__).resolve().parents[1]
BINDING = GitHubBinding.from_dict(json.loads((ROOT / 'config/store-binding.json').read_text()))
NOW = '2026-10-17T10:00:00Z'
STAMP = '2026-10-17T09:00:00Z'
OLD = '2026-10-16T09:00:00Z'
SHA = 'a' * 40
SCHEDULE_PAGE = '/actions/workflows/daily-market.yml/runs?branch=main&event=schedule&per_page=20'
DISPATCH_PAGE = '/actions/workflows/daily-market.yml/runs?branch=main&event=workflow_dispatch&per_page=20'
POLICY_CONTENT = '/contents/config/daily-market.json?ref=' + SHA
CLOCK_CONTENT = '/contents/config/daily-market-clock.json?ref=' + SHA
RUNTIME_CONTENT = '/contents/config/daily-runtime-version.json?ref=' + SHA


def policy_content(raw=None, *, path='config/daily-market.json'):
    raw = (ROOT / 'config/daily-market.json').read_bytes() if raw is None else raw
    return {'type': 'file', 'path': path, 'encoding': 'base64',
            'size': len(raw), 'content': base64.b64encode(raw).decode(),
            'sha': hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest(),
            'download_url': 'https://PRIVATE_SENTINEL.invalid/signed?token=SECRET'}


def clock_content(primary='github-schedule'):
    return policy_content(encoded({'schema': 'daily-market-clock-v1', 'primary': primary}),
                          path='config/daily-market-clock.json')


def runtime_content(enabled=True):
    # Protocol fixtures remain enabled independently of the paused deployment.
    return policy_content(encoded({'schema': 'daily-runtime-policy-v1', 'enabled': enabled,
                                   'mode': 'current-main', 'pinned': None}),
                          path='config/daily-runtime-version.json')


def run(wid, filename, ident, conclusion='success', *, attempt=1, stamp=STAMP, status='completed'):
    return {'id': ident, 'workflow_id': wid, 'run_attempt': attempt, 'head_sha': SHA,
            'head_branch': 'main', 'path': '.github/workflows/' + filename,
            'head_repository': {'full_name': BINDING.repository, 'id': BINDING.repository_id},
            'event': 'workflow_dispatch', 'status': status, 'conclusion': conclusion,
            'created_at': stamp, 'run_started_at': stamp, 'updated_at': stamp,
            'display_title': 'PRIVATE_SENTINEL', 'actor': {'login': 'PRIVATE_SENTINEL'}}


def fixture(*, failure=False, self_run=False):
    values = {'': {'id': BINDING.repository_id, 'full_name': BINDING.repository, 'private': False,
                   'default_branch': 'main'}, POLICY_CONTENT: policy_content(), CLOCK_CONTENT: clock_content(),
              RUNTIME_CONTENT: runtime_content()}
    for name in ('main', BINDING.branch):
        values['/git/ref/heads/' + name] = {'ref': 'refs/heads/' + name, 'object': {'type': 'commit', 'sha': SHA}}
    for wid, (filename, prefix) in enumerate(h.WORKFLOWS.items(), 1):
        base = '/actions/workflows/' + filename
        values[base] = {'id': wid, 'path': '.github/workflows/' + filename, 'state': 'active'}
        rows = [run(wid, filename, wid * 100 + 2, 'failure' if failure else 'success'),
                run(wid, filename, wid * 100 + 1, stamp=OLD)]
        if self_run and wid == 1:
            rows.insert(0, run(wid, filename, 999, None, status='in_progress'))
        values[base + '/runs?branch=main&per_page=20'] = {'total_count': len(rows), 'workflow_runs': rows}
        for r in rows:
            operation = 'health' if r['id'] == 999 else {'store-runtime.yml': 'backup', 'research-runtime.yml': 'read',
                'news-rolling.yml': 'build', 'daily-market.yml': 'status'}[filename]
            add_details(values, r, operation, prefix)
        if filename == 'daily-market.yml':
            values[DISPATCH_PAGE] = {'total_count': len(rows), 'workflow_runs': rows}
            natural = run(wid, filename, 490, stamp='2026-10-16T10:36:00Z')
            natural['event'] = 'schedule'
            values[SCHEDULE_PAGE] = {'total_count': 1, 'workflow_runs': [natural]}
    return values


def add_details(values, r, operation, prefix):
    ident = r['id']; attempt = r['run_attempt']
    values[f'/actions/runs/{ident}'] = r
    values[f'/actions/runs/{ident}/attempts/{attempt}/jobs?per_page=100'] = {'total_count': 1, 'jobs': [
        {'id': ident * 10, 'name': operation, 'run_id': ident, 'run_attempt': attempt,
         'head_sha': r['head_sha'], 'status': r['status'], 'conclusion': r['conclusion']}]}
    prefix = 'dos2-health' if operation == 'health' else 'dos2-daily-clock' if operation == 'clock-probe' else prefix
    values[f'/actions/runs/{ident}/artifacts?per_page=100'] = {'total_count': 1, 'artifacts': [
        {'id': ident * 100, 'name': f'{prefix}-{ident}-{attempt}', 'workflow_run': {'id': ident, 'head_sha': r['head_sha']},
         'expired': False, 'created_at': r['created_at'], 'expires_at': '2026-11-16T09:00:00Z',
         'size_in_bytes': 123, 'digest': 'sha256:' + 'b' * 64,
         'archive_download_url': 'https://PRIVATE_SENTINEL.invalid/signed?token=SECRET'}]}


class API:
    def __init__(self, values=None):
        self.values = fixture() if values is None else values
        self.calls = []
        self.mutate = None

    def request(self, method, path, payload=None):
        assert method == 'GET' and payload is None
        assert path.startswith('/repos/' + BINDING.repository)
        relative = path[len('/repos/' + BINDING.repository):]
        self.calls.append(relative)
        if self.mutate:
            changed = self.mutate(relative, self.calls)
            if changed is not None:
                return changed
        return deepcopy(self.values[relative])


def test_latest_failure_is_not_replaced_by_old_success_and_bytes_not_certified():
    api = API(fixture(failure=True)); report = h.inspect_runtime(BINDING, api, as_of=NOW, backup_run_id=101)
    assert report['status'] == 'needs-attention'
    assert report['native_reads'] == len(api.calls) == 57
    for p in report['workflows']:
        assert p['latest']['run']['conclusion'] == 'failure'
        assert p['last_visible_success']['run']['conclusion'] == 'success'
        assert p['latest']['run']['id'] > p['last_visible_success']['run']['id']
        assert p['latest']['artifact_bytes_verified'] is False
    assert report['specified_backup']['observed_operation'] == 'backup'
    assert report['specified_backup']['status'] == 'inspected'
    assert 'PRIVATE_SENTINEL' not in encoded(report).decode()
    assert 'SECRET' not in command.markdown(report)


def test_daily_manual_success_does_not_fill_unobserved_natural_slot():
    report = h.inspect_runtime(BINDING, API(), as_of='2026-10-21T11:00:00Z')
    panels = {p['path']: p for p in report['workflows']}
    assert '.github/workflows/daily-market.yml' in panels
    daily = panels['.github/workflows/daily-market.yml']
    assert daily['latest']['run']['event'] == 'workflow_dispatch'
    assert daily['latest']['run']['conclusion'] == 'success'
    assert daily['schedule']['status'] == 'not-observed'
    assert daily['schedule']['expected_slot']['business_date'] == '20261021'
    assert daily['status'] == report['status'] == 'needs-attention'
    assert '目标槽原生运行：未观察到' in command.markdown(report)


def test_actual_self_only_is_excluded_and_explicit_backup_is_separate():
    api = API(fixture(self_run=True))
    report = h.inspect_runtime(BINDING, api, as_of=NOW, self_run={'id': 999, 'run_attempt': 1}, backup_run_id=101)
    p = report['workflows'][0]
    assert p['latest']['run']['id'] == 102 and p['excluded_self'][0]['id'] == 999
    assert p['observed_active'] == [] and report['excluded_inspection']['id'] == 999
    assert report['specified_backup']['run']['id'] == 101
    assert '当前巡检自身' in command.markdown(report)


def test_green_health_run_cannot_be_called_a_backup():
    values = fixture(); r = values['/actions/runs/102']
    add_details(values, r, 'health', 'dos2-store')
    report = h.inspect_runtime(BINDING, API(values), as_of=NOW, backup_run_id=102)
    assert report['workflows'][0]['latest']['observed_operation'] == 'health'
    assert report['workflows'][0]['latest']['artifact']['name'] == 'dos2-health-102-1'
    assert report['specified_backup'] == {'status': 'unknown', 'error': 'HEALTH_NOT_SUCCESSFUL_BACKUP'}
    assert report['status'] == 'needs-attention'


@pytest.mark.parametrize('conclusion', ['failure','cancelled','timed_out','action_required','neutral','skipped','stale'])
def test_non_success_native_result_never_health(conclusion):
    values = fixture(); values['/actions/runs/102']['conclusion'] = conclusion
    values['/actions/runs/102/attempts/1/jobs?per_page=100']['jobs'][0]['conclusion'] = conclusion
    report = h.inspect_runtime(BINDING, API(values), as_of=NOW)
    assert report['workflows'][0]['status'] == 'needs-attention'
    assert report['workflows'][0]['last_visible_success']['run']['id'] == 101


@pytest.mark.parametrize('state', sorted(h.ACTIVE))
def test_in_progress_is_not_a_successful_zero_result(state):
    values = fixture(); r = values['/actions/runs/102']; r.update(status=state, conclusion=None)
    add_details(values,r,'backup','dos2-store')
    p = h.inspect_runtime(BINDING, API(values), as_of=NOW)['workflows'][0]
    assert p['status'] == 'in-progress' and p['observed_active'][0]['id'] == 102


@pytest.mark.parametrize('expired,expiry,want', [(True,'2026-11-16T09:00:00Z','expired'),
    (False,NOW,'expired'),(False,'2026-10-18T10:00:00Z','expiring')])
def test_retention_never_trusts_expired_false_alone(expired, expiry, want):
    values = fixture(); a = values['/actions/runs/102/artifacts?per_page=100']['artifacts'][0]
    a.update(expired=expired,expires_at=expiry)
    report = h.inspect_runtime(BINDING, API(values), as_of=NOW, backup_run_id=102)
    assert report['workflows'][0]['latest']['artifact_state'] == want
    assert report['specified_backup']['artifact_state'] == want
    assert report['status'] == 'needs-attention'


def test_empty_history_is_not_run_disabled_is_not_unconfigured():
    values = fixture(); key='/actions/workflows/store-runtime.yml'
    values[key+'/runs?branch=main&per_page=20']={'total_count':0,'workflow_runs':[]}
    result=h.inspect_runtime(BINDING,API(values),as_of=NOW)
    assert result['workflows'][0]['status']=='not-run'
    values[key]['state']='disabled_manually'
    assert h.inspect_runtime(BINDING,API(values),as_of=NOW)['workflows'][0]['status']=='disabled'
    assert result['automatic_schedule']=='not-established-by-workflow-active-state'


@pytest.mark.parametrize('where', ['workflow','run','repository','job','artifact','list','duplicate','job-page'])
def test_foreign_or_incomplete_metadata_is_not_fabricated(where):
    values=fixture(); key='/actions/workflows/store-runtime.yml'
    if where=='workflow': values[key]['path']='.github/workflows/foreign.yml'
    if where=='run': values['/actions/runs/102']['workflow_id']=999
    if where=='repository': values['/actions/runs/102']['head_repository']['id']=123
    if where=='job': values['/actions/runs/102/attempts/1/jobs?per_page=100']['jobs'][0]['run_attempt']=2
    if where=='artifact': values['/actions/runs/102/artifacts?per_page=100']['artifacts'][0]['workflow_run']['head_sha']='c'*40
    if where=='list': values[key+'/runs?branch=main&per_page=20']['total_count']=4
    if where=='duplicate': values[key+'/runs?branch=main&per_page=20']['workflow_runs'][1]['id']=102
    if where=='job-page':
        v=values['/actions/runs/102/attempts/1/jobs?per_page=100'];v['jobs']=[{**v['jobs'][0],'id':i+1} for i in range(100)];v['total_count']=101
    report=h.inspect_runtime(BINDING,API(values),as_of=NOW)
    assert report['workflows'][0]['status']=='unknown'
    assert report['workflows'][1]['status']=='metadata-ok'
    assert report['status']=='needs-attention'


def test_attempt_changes_during_read_do_not_mix_artifacts():
    api=API()
    def mutate(path,calls):
        if path=='/actions/runs/102':
            return {**deepcopy(api.values[path]),'run_attempt':2}
    api.mutate=mutate
    p=h.inspect_runtime(BINDING,api,as_of=NOW)['workflows'][0]
    assert p['status']=='changed-during-inspection'
    assert p['latest']['run']['run_attempt']==1


def test_newer_run_arrives_before_final_listing_is_not_ignored():
    api=API();key='/actions/workflows/store-runtime.yml/runs?branch=main&per_page=20'
    def mutate(path,calls):
        if path==key and calls.count(key)==2:
            return {'total_count':3,'workflow_runs':[run(1,'store-runtime.yml',103),*deepcopy(api.values[key]['workflow_runs'])]}
    api.mutate=mutate
    assert h.inspect_runtime(BINDING,api,as_of=NOW)['workflows'][0]['status']=='changed-during-inspection'


def test_last_attempt_time_not_run_id_controls_order_within_declared_window():
    values=fixture();r=values['/actions/runs/101'];r['run_started_at']=r['updated_at']='2026-10-17T09:59:00Z'
    r['run_attempt']=2;r['conclusion']='failure';add_details(values,r,'backup','dos2-store')
    p=h.inspect_runtime(BINDING,API(values),as_of=NOW)['workflows'][0]
    assert p['latest']['run']['id']==101 and p['last_visible_success']['run']['id']==102
    assert p['status']=='needs-attention'


def test_truncated_history_does_not_claim_global_no_writer_or_last_business_success():
    values=fixture();key='/actions/workflows/store-runtime.yml/runs?branch=main&per_page=20'
    values[key]={'total_count':25,'workflow_runs':[run(1,'store-runtime.yml',102),run(1,'store-runtime.yml',101,stamp=OLD),
        *[run(1,'store-runtime.yml',i,stamp='2026-10-15T09:00:00Z') for i in range(1,19)]]}
    p=h.inspect_runtime(BINDING,API(values),as_of=NOW)['workflows'][0]
    assert p['recent_scope']=={'limit':20,'returned':20,'total':25,'complete_history':False}


@pytest.mark.parametrize('field,value', [('as_of','2026-10-17'),('backup_run_id',True),('expected_head','latest'),('self_run',{'id':1,'run_attempt':False})])
def test_invalid_control_input_before_native_io(field,value):
    api=API()
    with pytest.raises(RecordError):h.inspect_runtime(BINDING,api,**{field:value})
    assert api.calls==[]


def test_write_binding_and_shared_api_exhaustion_rejected():
    api=API()
    with pytest.raises(RecordError):h.inspect_runtime(replace(BINDING,write_enabled=True),api,as_of=NOW)
    assert api.calls==[]
    native=GitHubAPI('synthetic-only');native._requests=512
    with pytest.raises(StoreUnavailable,match='REMOTE_REQUEST_LIMIT'):h.inspect_runtime(BINDING,native,as_of=NOW)
    assert native._requests==513


def test_final_permission_failure_stops_and_leaves_only_unknown_receipt(tmp_path):
    api=API()
    def mutate(path,calls):
        if path=='' and calls.count('')==2:raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    api.mutate=mutate
    with pytest.raises(RecordError,match='REMOTE_NOT_AUTHORIZED'):
        command.execute(BINDING,api,tmp_path/'out',as_of=NOW)
    receipt=json.loads((tmp_path/'out/health.json').read_bytes())
    assert receipt['status']=='unknown' and 'workflows' not in receipt
    assert not (tmp_path/'out/health.md').exists()
    assert api.calls[-1]==''


def test_unavailable_workflow_stops_not_marked_never_run():
    api=API()
    def mutate(path,calls):
        if 'workflows/store-runtime' in path:raise StoreUnavailable('REMOTE_NOT_FOUND_OR_NOT_AUTHORIZED')
    api.mutate=mutate
    with pytest.raises(StoreUnavailable):h.inspect_runtime(BINDING,api,as_of=NOW)
    assert len(api.calls)==4


def test_heads_change_and_expected_snapshot_are_not_ignored():
    api=API();key='/git/ref/heads/'+BINDING.branch
    def mutate(path,calls):
        if path==key and calls.count(key)==2:return {'ref':'refs/heads/'+BINDING.branch,'object':{'type':'commit','sha':'d'*40}}
    api.mutate=mutate
    assert h.inspect_runtime(BINDING,api,as_of=NOW)['heads_changed_or_unexpected']
    assert h.inspect_runtime(BINDING,API(),as_of=NOW,expected_head='d'*40)['heads_changed_or_unexpected']


def test_future_run_and_resource_bounds_are_explicit(monkeypatch):
    with pytest.raises(RecordError,match='FUTURE_RUN'):h.inspect_runtime(BINDING,API(),as_of='2026-10-14T00:00:00Z')
    monkeypatch.setattr(h,'MAX_METADATA_BYTES',100)
    with pytest.raises(RecordError,match='METADATA_BOUND'):h.inspect_runtime(BINDING,API(),as_of=NOW)


def test_missing_current_artifact_not_replaced_by_old_attempt():
    values=fixture();a=values['/actions/runs/102/artifacts?per_page=100']['artifacts'][0];a['name']='dos2-store-102-0'
    p=h.inspect_runtime(BINDING,API(values),as_of=NOW)['workflows'][0]
    assert p['latest']['artifact_state']=='not-found' and p['status']=='needs-attention'


def test_markup_is_literal_and_existing_output_not_overwritten(tmp_path):
    values=fixture();j=values['/actions/runs/202/attempts/1/jobs?per_page=100']['jobs'][0]
    j['name']='<script>fetch("secret")</script>|![x](https://evil.invalid)'
    report=command.execute(BINDING,API(values),tmp_path/'out',as_of=NOW)
    md=(tmp_path/'out/health.md').read_text(); assert '<script>' not in md and '![' not in md
    assert encoded(report)==(tmp_path/'out/health.json').read_bytes()
    api=API()
    with pytest.raises(RecordError,match='NEW_OUTPUT'):command.execute(BINDING,api,tmp_path/'out',as_of=NOW)
    assert api.calls==[]


def test_real_host_route_reads_only_metadata_and_original_backup_jobs_are_unchanged(tmp_path,monkeypatch):
    import yaml
    from decision_kernel.interfaces import store_runtime
    source=yaml.safe_load((ROOT/'.github/workflows/store-runtime.yml').read_text())
    trigger=source.get('on',source.get(True))
    assert set(trigger)=={'workflow_dispatch'}
    assert source['jobs']['health']['permissions']=={'contents':'read','actions':'read'}
    assert source['jobs']['backup']['permissions']=={'contents':'read'}
    assert source['jobs']['retain-guide']['permissions']=={'contents':'write'}
    monkeypatch.chdir(ROOT)
    for key,value in {'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main',
        'GITHUB_REPOSITORY':BINDING.repository,'GITHUB_SHA':SHA,'GITHUB_RUN_ID':'999','GITHUB_RUN_ATTEMPT':'1'}.items():monkeypatch.setenv(key,value)
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(NOW.replace('Z', '+00:00'))
    monkeypatch.setattr(h, 'datetime', FrozenClock)
    api = API(fixture(self_run=True))
    with patch.object(store_runtime.GitHubAPI,'from_environment',return_value=api):
        assert store_runtime.main(['operate','--operation','health','--expected-head',SHA,'--backup-run-id','101','--out',str(tmp_path/'host')])==0
    doc=json.loads((tmp_path/'host/health.json').read_bytes())
    assert doc['dispatches']==doc['writes']==0 and doc['specified_backup']['observed_operation']=='backup'
    assert doc['workflows'][-1]['schedule']['status'] == 'native-success'
    assert api.calls.count(SCHEDULE_PAGE) == 2 and api.calls.count(POLICY_CONTENT) == 1


def exercise_installed_runtime_health(outside, python, env, store):
    """Extend the original ONE wheel; no new installation or business fixture."""
    import subprocess
    from decision_kernel.infrastructure.git_store import GitStore
    head = GitStore(store).head()
    values=fixture(failure=True)
    (outside/'health-native-input.json').write_bytes(encoded(values))
    (outside/'health-binding.json').write_bytes(encoded(asdict(BINDING)))
    script=r'''
import json,sys
from pathlib import Path
from unittest.mock import patch
from decision_kernel.interfaces import store_runtime
values=json.loads(Path('health-native-input.json').read_bytes())
class API:
 def request(self,method,path,payload=None):
  assert method=='GET' and payload is None
  return values[path[len('/repos/auguspp/Decision-OS-2'):]]
with patch.object(store_runtime.GitHubAPI,'from_environment',return_value=API()):
 code=store_runtime.main(['runtime-health','--binding','health-binding.json','--as-of','2026-10-17T10:00:00Z','--backup-run-id','101','--out',sys.argv[1]])
assert code==2
r=json.loads(Path(sys.argv[1],'health.json').read_bytes())
assert r['workflows'][0]['latest']['run']['conclusion']=='failure'
assert r['specified_backup']['observed_operation']=='backup'
daily=r['workflows'][-1]
assert daily['path']=='.github/workflows/daily-market.yml'
assert daily['schedule']['status']=='native-success'
assert daily['schedule']['expected_slot']['business_date']=='20261016'
assert daily['schedule']['policy']['main']=='a'*40
assert daily['schedule']['business_result']=='not-inspected'
assert 'seed_reference' not in json.dumps(r) and 'PRIVATE_SENTINEL' not in json.dumps(r)
assert r['writes']==r['dispatches']==0
'''
    for name in ('health-first','health-fresh'):
        completed=subprocess.run([str(python),'-I','-c',script,name],cwd=outside,env=env,capture_output=True,text=True)
        assert completed.returncode==0,completed.stderr
    assert (outside/'health-first/health.json').read_bytes()==(outside/'health-fresh/health.json').read_bytes()
    assert GitStore(store).head() == head


def test_a_backup_run_cannot_be_hidden_as_the_inspector():
    report=h.inspect_runtime(BINDING,API(),as_of=NOW,self_run={"id":102,"run_attempt":1})
    assert report["excluded_inspection"] is None
    assert report["workflows"][0]["error"]=="HEALTH_SELF_NOT_INSPECTION"
    assert report["status"]=="needs-attention"

 
def test_native_health_private_upgrade_reads_same_runs_without_metadata_relabeling():
    upgraded = replace(BINDING, private_upgrade_allowed=True)
    values = fixture()
    values[""]["private"] = True
    api = API(values)
    report = h.inspect_runtime(upgraded, api, as_of=NOW, backup_run_id=101)
    assert report["status"] == "metadata-ok"
    baseline = h.inspect_runtime(BINDING, API(), as_of=NOW, backup_run_id=101)
    assert report["native_reads"] == len(api.calls) == baseline["native_reads"]
    assert report["specified_backup"]["status"] == "inspected"
    assert report["writes"] == report["dispatches"] == 0


def test_native_health_private_to_public_reversal_is_rejected():
    upgraded = replace(BINDING, private_upgrade_allowed=True)
    values = fixture()
    values[""]["private"] = True
    api = API(values)
    def reverse(path, calls):
        if path == "" and calls.count("") == 2:
            return {**api.values[""], "private": False}
    api.mutate = reverse
    with pytest.raises(RecordError, match="HEALTH_REPOSITORY_MISMATCH"):
        h.inspect_runtime(upgraded, api, as_of=NOW)
    assert api.calls.count("") == 2


def test_native_health_default_public_binding_still_rejects_private():
    values = fixture()
    values[""]["private"] = True
    with pytest.raises(RecordError, match="HEALTH_REPOSITORY_MISMATCH"):
        h.inspect_runtime(replace(BINDING, private_upgrade_allowed=False), API(values), as_of=NOW)


@pytest.mark.parametrize('primary', ['github-schedule', 'external-cron'])
def test_paused_deployment_retires_trigger_reads_not_ordinary_history(primary):
    values = fixture()
    values[CLOCK_CONTENT] = clock_content(primary)
    enabled = API(values)
    h.inspect_runtime(BINDING, enabled, as_of='2026-10-21T11:00:00Z')
    values[RUNTIME_CONTENT] = runtime_content(False)
    # Retired observations must not be needed to recognize a native pause.
    for path in (POLICY_CONTENT, CLOCK_CONTENT, SCHEDULE_PAGE, DISPATCH_PAGE):
        del values[path]
    api = API(values)
    report = h.inspect_runtime(BINDING, api, as_of='2026-10-21T11:00:00Z')
    daily = report['workflows'][-1]
    assert daily['status'] == 'paused' and report['status'] == 'metadata-ok'
    assert daily['runtime_policy']['enabled'] is False
    assert daily['runtime_policy']['main'] == SHA
    assert api.calls.count(RUNTIME_CONTENT) == 1
    assert len(enabled.calls) - len(api.calls) == 6  # Two configs plus four trigger-page reads.
    for section in ('schedule', 'external_entry'):
        assert daily[section]['status'] == 'deployment-paused'
        assert daily[section]['observation'] == 'not-requested'
        assert 'expected_slot' not in daily[section]
        assert daily[section]['historical_gaps_resolved'] is False
    assert daily['latest']['run']['id'] == 402
    assert daily['external_entry']['provider_configuration'] == 'not-inspected'
    assert report['writes'] == report['dispatches'] == report['notifications'] == 0
    assert report['source_freshness'] == report['business_acceptance'] == 'not-inspected'
    md = command.markdown(report)
    assert '旧日更已主动暂停' in md and '目标槽原生运行：未观察到' not in md
    assert 'PRIVATE_SENTINEL' not in md and 'SECRET' not in md


@pytest.mark.parametrize('problem', ['failure', 'expired', 'active-older-run'])
def test_pause_does_not_erase_failure_expiry_or_active_runs(problem):
    values = fixture()
    values[RUNTIME_CONTENT] = runtime_content(False)
    if problem == 'failure':
        values['/actions/runs/402']['conclusion'] = 'failure'
        add_details(values, values['/actions/runs/402'], 'daily', 'dos2-daily-market')
    elif problem == 'expired':
        values['/actions/runs/402/artifacts?per_page=100']['artifacts'][0]['expired'] = True
    else:
        values['/actions/runs/401'].update(status='in_progress', conclusion=None)
    report = h.inspect_runtime(BINDING, API(values), as_of=NOW)
    daily = report['workflows'][-1]
    assert daily['runtime_policy']['status'] == 'paused'
    assert daily['status'] == ('in-progress' if problem == 'active-older-run' else 'needs-attention')
    assert report['status'] == 'needs-attention'
    if problem == 'failure':
        assert daily['latest']['run']['conclusion'] == 'failure'
        assert daily['last_visible_success']['run']['id'] == 401
    elif problem == 'expired':
        assert daily['latest']['artifact_state'] == 'expired'
    else:
        assert [run['id'] for run in daily['observed_active']] == [401]


@pytest.mark.parametrize('fault', ['not-bool', 'missing', 'blob', 'wrong-path', 'duplicate', 'pinned-shape'])
def test_unverified_deployment_is_unknown_not_paused_or_enabled(fault):
    values = fixture()
    deployed = values[RUNTIME_CONTENT] = runtime_content(False)
    path = 'config/daily-runtime-version.json'
    if fault == 'not-bool': values[RUNTIME_CONTENT] = runtime_content(0)
    elif fault == 'missing': values[RUNTIME_CONTENT] = {'type': 'missing'}
    elif fault == 'blob': deployed['sha'] = 'b' * 40
    elif fault == 'wrong-path': deployed['path'] = 'config/daily-market.json'
    elif fault == 'duplicate':
        values[RUNTIME_CONTENT] = policy_content(b'{"enabled":true,"enabled":false}', path=path)
    else:
        values[RUNTIME_CONTENT] = policy_content(encoded({'schema': 'daily-runtime-policy-v1',
            'enabled': False, 'mode': 'pinned', 'pinned': None}), path=path)
    api = API(values)
    report = h.inspect_runtime(BINDING, api, as_of=NOW)
    daily = report['workflows'][-1]
    assert daily['runtime_policy']['status'] == 'unknown'
    assert daily['status'] == report['status'] == 'needs-attention'
    assert SCHEDULE_PAGE not in api.calls and DISPATCH_PAGE not in api.calls
    assert '部署策略无法核验' in command.markdown(report)


@pytest.mark.parametrize('change', ['main-moves', 'default-changes', 'wrong-default'])
def test_pause_requires_stable_native_main_and_default_branch(change):
    values = fixture()
    values[RUNTIME_CONTENT] = runtime_content(False)
    if change == 'wrong-default': values['']['default_branch'] = 'other'
    api = API(values)
    def mutate(path, calls):
        if change == 'main-moves' and path == '/git/ref/heads/main' and calls.count(path) == 2:
            return {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'c' * 40}}
        if change == 'default-changes' and path == '' and calls.count(path) == 2:
            return {**values[''], 'default_branch': 'other'}
    api.mutate = mutate
    report = h.inspect_runtime(BINDING, api, as_of=NOW)
    daily = report['workflows'][-1]
    expected = 'unknown' if change == 'wrong-default' else 'changed-during-inspection'
    assert daily['runtime_policy']['status'] == expected
    assert daily['status'] != 'paused' and report['status'] == 'needs-attention'
    assert '旧日更已主动暂停' not in command.markdown(report)


@pytest.mark.parametrize('where', [RUNTIME_CONTENT, ''])
def test_pause_does_not_bypass_permission_failure_or_final_output_guard(where, tmp_path):
    values = fixture()
    values[RUNTIME_CONTENT] = runtime_content(False)
    api = API(values)
    def deny(path, calls):
        if path == where and (where or calls.count('') == 2):
            raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    api.mutate = deny
    with pytest.raises(RecordError, match='REMOTE_NOT_AUTHORIZED'):
        command.execute(BINDING, api, tmp_path/'paused-health', as_of=NOW)
    assert api.calls[-1] == where
    receipt = json.loads((tmp_path/'paused-health/health.json').read_bytes())
    assert receipt['status'] == 'unknown' and 'workflows' not in receipt
    assert not (tmp_path/'paused-health/health.md').exists()
