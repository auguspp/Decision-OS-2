"""Private native preparation custody, original-intent recovery and normal Room/CLI."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application.decision_preparation import DecisionPreparation
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    BlobRef, IntegrityError, MissingObject, RecordError, RevisionInput,
    SourceInput, SourceRef, StoreUnavailable, UnknownWrite, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure import github_store as remote
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import cli, decision_preparation_commands as commands, reading_commands, research_runtime
from decision_kernel.interfaces.reading_decision_page import JS
from decision_kernel.interfaces.reading_page import render_page
from test_decision_preparation import PREPARED, LATER, authorized, context, horizon, plan, save_revision
from test_research_runtime import CODE, host, inputs


@pytest.fixture
def native(host, monkeypatch):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = True
    monkeypatch.setattr('decision_kernel.application.versioned_store._now', lambda: '2026-01-01T08:00:00Z')
    store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    source = store.put_source('preparation-source', SourceInput('s', 'content', 'Synthetic private source',
        'synthetic:private-preparation', 'Delivery facts', acquired_at='2026-01-01T08:00:00Z'))
    ref = save_revision(store, source)
    monkeypatch.setattr('decision_kernel.application.versioned_store._now', lambda: '2026-01-11T08:00:00Z')
    return host, store, plan(ref)


def run(native, output, operation, payload, submission='', expected=None):
    host, _, _ = native
    head = host.git.head() if expected is None and operation in research_runtime.WRITE_OPERATIONS else expected or ''
    return research_runtime.run_command(host.binding, api=host, code_sha=CODE, output=output,
        inputs=inputs(operation, payload, submission, head), run_id='123', attempt='1')


def test_native_prepare_save_fresh_read_and_reconcile_preserve_formal_boundary(native, tmp_path):
    host, _, value = native
    before = host.git.head()
    prepared = run(native, tmp_path / 'prepare', 'prepare-decision', value)
    assert host.git.head() == before and prepared['write'] == 'not-requested'
    saved = run(native, tmp_path / 'save', 'save-decision-preparation', authorized(value), 'prepared-one')
    assert saved['write'] == 'published' and saved['result']['readback']['status'] == 'verified'
    ref = saved['result']['reference']
    read = run(native, tmp_path / 'read', 'read-decision-preparation', {'reference': ref, 'at': LATER})
    original = read['result']['document']
    assert original == prepared['result']['document']
    assert original['formal_decision'] == 'not-created' and original['human_confirmation'] == 'not-established'
    writes = host.writes
    again = run(native, tmp_path / 'reconcile', 'reconcile-decision-preparation', authorized(value), 'prepared-one')
    assert again['write'] == 'reconciled-without-write' and host.writes == writes
    assert again['result']['reference'] == ref and again['result']['readback']['status'] == 'verified'
    assert all(x['repository_visibility'] == 'private' for x in [prepared, saved, read, again])


@pytest.mark.parametrize('operation', commands.COMMANDS + commands.RECONCILES)
def test_private_custody_is_checked_before_any_output_even_with_save_rights(native, tmp_path, operation):
    host, store, value = native
    ref = DecisionPreparation(store).save(authorized(value), submission_id='fixture-preparation')['reference']
    payload = (authorized(value) if operation in commands.WRITES + commands.RECONCILES else
               value if operation == 'prepare-decision' else {'reference': ref, 'at': LATER})
    host.meta['private'] = False
    before, writes = host.git.head(), host.writes
    with pytest.raises(RecordError, match='PRIVATE_CUSTODY_REQUIRED'):
        run(native, tmp_path / 'denied', operation, payload,
            'new-private-preparation' if operation in commands.WRITES + commands.RECONCILES else '')
    assert not (tmp_path / 'denied').exists() and host.git.head() == before and host.writes == writes


def test_unknown_save_retains_original_intent_and_reconciles_without_new_write(native, tmp_path):
    host, _, value = native
    def lose(_):
        raise UnknownWrite('REMOTE_WRITE_UNCONFIRMED')
    host.after_write = lose
    before = host.git.head()
    with pytest.raises(UnknownWrite):
        run(native, tmp_path / 'lost', 'save-decision-preparation', authorized(value), 'original-intent', before)
    original = json.loads((tmp_path / 'lost/inputs.json').read_text())
    assert original['submission_id'] == 'original-intent' and original['expected_head'] == before
    assert json.loads(original['payload']) == authorized(value)
    host.after_write = None
    writes = host.writes
    restored = run(native, tmp_path / 'recover', 'reconcile-decision-preparation',
                   authorized(value), 'original-intent', before)
    assert restored['write'] == 'reconciled-without-write' and restored['result']['readback']['status'] == 'verified'
    assert host.writes == writes
    with pytest.raises(RecordError, match='SUBMISSION_NOT_FOUND_NO_WRITE'):
        run(native, tmp_path / 'absent', 'reconcile-decision-preparation', authorized(value), 'absent-intent')
    assert host.writes == writes


def test_private_read_reversal_suppresses_result_and_input_export(native, tmp_path, monkeypatch):
    host, _, value = native
    original = commands.perform
    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        host.meta['private'] = False
        return result
    monkeypatch.setattr(commands, 'perform', revoke)
    with pytest.raises(RecordError, match='PRIVATE_UPGRADE_REVERSED'):
        run(native, tmp_path / 'reversed', 'prepare-decision', value)
    report = json.loads((tmp_path / 'reversed/report.json').read_text())
    assert 'result' not in report and not (tmp_path / 'reversed/inputs.json').exists()


def test_normal_private_room_recovers_original_document_without_another_write(native, tmp_path):
    host, store, value = native
    receipt = DecisionPreparation(store).save(authorized(value), submission_id='room-preparation')
    reading_plan = {'as_of': LATER, 'decision_preparation': {'reference': receipt['reference'], 'at': LATER}}
    before, writes = host.git.head(), host.writes
    report = run(native, tmp_path / 'room', 'reading-room', {'plan': reading_plan})
    model = report['result']
    assert model['decision_preparation']['status'] == 'readable'
    assert model['decision_preparation']['view']['document']['input'] == value
    assert (tmp_path / 'room/reading-room.html').read_text() == render_page(model)
    assert host.git.head() == before and host.writes == writes
    request = model['decision_preparation']['open_request']
    commands.preflight(request['operation'], request['payload'])
    assert model['human_acceptance'] == 'not-established'
    with pytest.raises(RecordError, match='PUBLIC_DECISION_PREPARATION'):
        reading_commands.preflight('reading-room', {'plan': reading_plan, 'public_research_custody_confirmed': True})


def test_main_cli_save_and_reopen_exact_preparation(context, tmp_path, capsys):
    store, _, research = context
    input_path = tmp_path / 'preparation.json'
    input_path.write_text(json.dumps(authorized(plan(research))))
    base = ['store', '--path', str(store.backend.root)]
    assert cli.main(base + ['save-decision-preparation', '--input', str(input_path), '--submission-id', 'cli-preparation']) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['readback']['status'] == 'verified'
    read_path = tmp_path / 'read.json'
    read_path.write_text(json.dumps({'reference': receipt['reference'], 'at': LATER}))
    before = store.backend.head()
    assert cli.main(base + ['read-decision-preparation', '--input', str(read_path)]) == 0
    read = json.loads(capsys.readouterr().out)
    assert read['document']['input'] == plan(research) and before == store.backend.head()


def test_existing_room_shape_and_absent_panel_javascript_remain_valid(context):
    store, _, _ = context
    old = ReadingRoom(store).build({'as_of': LATER})
    assert ReadingRoom(store).build({'as_of': LATER, 'decision_preparation': None}) == old
    assert 'decision_preparation' not in old
    assert 'id="decision-preparation-content"' not in render_page(old)
    subprocess.run(['node', '-e', 'const D={},$=()=>null;\n' + JS + '\nrenderDecisionPreparation();'], check=True)


def probability_plan(store, fresh):
    from test_conditional_odds import authorized as odds_authorized
    from test_probability_odds import EVALUATED, build_context
    store, probability, spec, assessment, market = build_context(store, fresh)
    frozen = probability.freeze(odds_authorized('assessment', assessment), submission_id='existing-assessment')
    saved = probability.save(odds_authorized('calculation', {'assessment_ref': frozen['reference'],
        'market': market, 'evaluated_at': EVALUATED}), submission_id='existing-probability')
    value = plan(BlobRef(**spec['research_ref']), question_ids=[], horizons=[horizon(question_ids=[])],
        target={'security': spec['security'], 'expression': 'Synthetic shares for human consideration'},
        analysis_context={'valuation_ref': None, 'odds': {'kind': 'probability', 'reference': saved['reference']}})
    return value, assessment, saved['reference']


@pytest.mark.parametrize('entry', ['local-cli', 'native-cli', 'native-runtime'])
def test_default_registry_restores_real_retained_probability_after_save(host, tmp_path, monkeypatch, capsys, entry):
    monkeypatch.setattr('decision_kernel.application.versioned_store._now', lambda: '2026-01-03T02:30:00+00:00')
    if entry == 'local-cli':
        store = VersionedStore(GitStore.init_synthetic(tmp_path / 'probability.git'))
        fresh = lambda: VersionedStore(GitStore(store.backend.root))
        base = ['store', '--path', str(store.backend.root)]
    else:
        host.binding = replace(host.binding, private_upgrade_allowed=True)
        host.meta['private'] = True
        store = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
        fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
        binding_path = tmp_path / 'binding.json'
        binding_path.write_text(json.dumps(asdict(replace(host.binding, write_enabled=True))))
        base = ['store', '--binding', str(binding_path)]
        monkeypatch.setattr(remote.GitHubAPI, 'from_environment', classmethod(lambda cls: host))
    value, assessment, odds_ref = probability_plan(store, fresh)
    if entry == 'native-runtime':
        native = host, store, value
        saved = run(native, tmp_path / 'save', 'save-decision-preparation', authorized(value), 'probability-preparation')
        receipt = saved['result']
        assert saved['write'] == 'published'
        writes = host.writes
        restored = run(native, tmp_path / 'reconcile', 'reconcile-decision-preparation',
                       authorized(value), 'probability-preparation')
        assert restored['result']['reference'] == receipt['reference'] and host.writes == writes
        assert restored['result']['readback']['status'] == 'verified'
        read = run(native, tmp_path / 'read', 'read-decision-preparation',
                   {'reference': receipt['reference'], 'at': LATER})['result']
    else:
        input_path = tmp_path / 'probability-preparation.json'
        input_path.write_text(json.dumps(authorized(value)))
        assert cli.main(base + ['save-decision-preparation', '--input', str(input_path),
                               '--submission-id', 'probability-preparation']) == 0
        receipt = json.loads(capsys.readouterr().out)
        input_path.write_text(json.dumps({'reference': receipt['reference'], 'at': LATER}))
        assert cli.main(base + ['read-decision-preparation', '--input', str(input_path)]) == 0
        read = json.loads(capsys.readouterr().out)
    assert receipt['readback']['status'] == 'verified'
    analysis = read['document']['analysis_context']
    assert analysis['probability_assessment']['assessment'] == assessment
    assert analysis['odds']['reference'] == odds_ref and analysis['source_requests'] == 0
    assert read['document']['human_confirmation'] == 'not-established'


def two_source_plan(native, monkeypatch):
    _, store, value = native
    first = SourceRef.from_dict(store.read_revision(BlobRef(**value['research_ref']))['record']['input']['sources'][0])
    monkeypatch.setattr('decision_kernel.application.versioned_store._now', lambda: '2026-01-01T08:00:00+00:00')
    second = store.put_source('second-source', SourceInput('second', 'content', 'Second synthetic source',
        'synthetic:second', 'Second independent evidence'))
    revision = store.submit_revision('two-source-research', RevisionInput('two-source-case', 'r1', 'full',
        'Synthetic evidence comparison', 'Synthetic mandate', 'synthetic', (first, second)))
    monkeypatch.setattr('decision_kernel.application.versioned_store._now', lambda: '2026-01-11T08:00:00+00:00')
    return plan(revision, question_ids=[], horizons=[horizon(question_ids=[])], source_indices=[0, 1]), first, second


def test_native_preparation_keeps_original_privacy_error_and_does_not_read_next_source(native, monkeypatch):
    host, store, _ = native
    value, first, _ = two_source_plan(native, monkeypatch)
    original, calls = VersionedStore.read_source, []
    def revoke(self, reference):
        calls.append(reference)
        host.meta['private'] = False
        return original(self, reference)
    monkeypatch.setattr(VersionedStore, 'read_source', revoke)
    before, writes = host.git.head(), host.writes
    with pytest.raises(RecordError, match='^PRIVATE_UPGRADE_REVERSED$'):
        commands.perform(store, 'prepare-decision', value)
    assert calls == [first] and host.git.head() == before and host.writes == writes


@pytest.mark.parametrize('fault', ['permission', 'request-budget'])
def test_private_room_aborts_at_original_source_failure_without_later_io(native, tmp_path, monkeypatch, fault):
    from test_reading_scope import transport
    host, store, _ = native
    value, first, _ = two_source_plan(native, monkeypatch)
    receipt = DecisionPreparation(store).save(authorized(value), submission_id='two-source-preparation')
    api = transport(host)
    wire_open, source_read, perform = api._opener.open, VersionedStore.read_source, commands.perform
    state = {'decision': False, 'pending': False, 'failed': False, 'after': [], 'sources': [], 'failed_at': None}
    def open_wire(request, timeout):
        if state['pending']:
            state.update(pending=False, failed=True)
            raise HTTPError(request.full_url, 403, 'Synthetic permission revoked', {}, None)
        if state['failed']:
            state['after'].append(request.full_url)
        return wire_open(request, timeout)
    def in_decision(*args, **kwargs):
        state['decision'] = True
        return perform(*args, **kwargs)
    def read_source(self, reference):
        if state['decision']:
            state['sources'].append(reference)
            if fault == 'permission':
                state['pending'] = True
            else:
                api._requests = remote.MAX_REQUESTS
        try:
            return source_read(self, reference)
        except StoreUnavailable:
            state['failed_at'] = len(state['sources'])
            raise
    monkeypatch.setattr(api._opener, 'open', open_wire)
    monkeypatch.setattr(commands, 'perform', in_decision)
    monkeypatch.setattr(VersionedStore, 'read_source', read_source)
    output = tmp_path / fault
    reading_plan = {'as_of': LATER, 'decision_preparation': {'reference': receipt['reference'], 'at': LATER}}
    before, writes = host.git.head(), host.writes
    with pytest.raises(StoreUnavailable):
        research_runtime.run_command(host.binding, api=api, code_sha=CODE, output=output,
            inputs=inputs('reading-room', {'plan': reading_plan}), run_id='123', attempt='1')
    assert state['sources'][0] == first and len(state['sources']) == state['failed_at']
    assert state['after'] == []
    if fault == 'request-budget':
        assert api._requests == remote.MAX_REQUESTS + 1
    assert 'result' not in json.loads((output / 'report.json').read_text())
    assert not (output / 'inputs.json').exists() and not (output / 'reading-room.html').exists()
    assert host.git.head() == before and host.writes == writes


@pytest.mark.parametrize('entry', ['native-cli', 'native-runtime'])
def test_fresh_private_reader_rechecks_admission_before_preparation_bytes(native, tmp_path, monkeypatch, capsys, entry):
    host, _, value = native
    original, observed = commands.verify_fresh, []
    def public_during_fresh(receipt, reader_factory, registry):
        reference = receipt['reference']
        oid = host.git._run('rev-parse', reference['commit'] + ':' + reference['path']).stdout.decode().strip()
        start = len(host.requests)
        host.meta['private'] = False
        try:
            result = original(receipt, reader_factory, registry)
            observed.append((result['readback']['status'], oid, host.requests[start:]))
            return result
        finally:
            host.meta['private'] = True
    monkeypatch.setattr(commands, 'verify_fresh', public_during_fresh)
    if entry == 'native-runtime':
        report = run(native, tmp_path / 'save', 'save-decision-preparation', authorized(value), 'fresh-privacy')
        receipt = report['result']
        assert report['write'] == 'published' and report['status'] == 'decision-preparation-readback-failed'
        retained = json.loads((tmp_path / 'save/inputs.json').read_text())
        assert retained['submission_id'] == 'fresh-privacy'
    else:
        binding_path, input_path = tmp_path / 'binding.json', tmp_path / 'preparation.json'
        binding_path.write_text(json.dumps(asdict(replace(host.binding, write_enabled=True))))
        input_path.write_text(json.dumps(authorized(value)))
        monkeypatch.setattr(remote.GitHubAPI, 'from_environment', classmethod(lambda cls: host))
        assert cli.main(['store', '--binding', str(binding_path), 'save-decision-preparation',
                         '--input', str(input_path), '--submission-id', 'fresh-privacy']) == 2
        receipt = json.loads(capsys.readouterr().out)
    assert receipt['readback']['status'] == 'failed' and len(observed) == 1
    status, manifest_oid, requests = observed[0]
    assert status == 'failed' and not any(path.endswith('/git/blobs/' + manifest_oid) for _, path, _ in requests)


@pytest.mark.parametrize('fault,expected,error_type', [
    ('missing', 'missing', MissingObject), ('digest', 'invalid', IntegrityError),
])
def test_room_keeps_an_ordinary_missing_or_corrupt_preparation_visible(context, fault, expected, error_type):
    store, _, research = context
    receipt = DecisionPreparation(store).save(authorized(plan(research)), submission_id='panel-fixture')
    reference = deepcopy(receipt['reference'])
    if fault == 'missing':
        parts = reference['path'].split('/')
        parts[-2] = '0' * 64
        reference['path'] = '/'.join(parts)
    else:
        reference['sha256'] = '0' * 64
    with pytest.raises(error_type):
        commands.perform(store, 'read-decision-preparation', {'reference': reference, 'at': LATER})
    result = ReadingRoom(store).build({'as_of': LATER,
        'decision_preparation': {'reference': reference, 'at': LATER}})
    assert result['decision_preparation']['status'] == expected and result['status'] == 'partial'


def test_room_preparation_read_budget_is_an_operation_failure(context, monkeypatch):
    from decision_kernel.application import decision_preparation as preparation
    store, _, research = context
    receipt = DecisionPreparation(store).save(authorized(plan(research)), submission_id='budget-fixture')
    monkeypatch.setattr(preparation, 'MAX_READ_BYTES', 1)
    with pytest.raises(RecordError, match='^DECISION_PREPARATION_READ_BOUND$'):
        ReadingRoom(store).build({'as_of': LATER,
            'decision_preparation': {'reference': receipt['reference'], 'at': LATER}})
