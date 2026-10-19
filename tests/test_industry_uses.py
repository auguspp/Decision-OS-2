"""Finite question composition on exact retained readers; all business data synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application import industry_uses as uses
from decision_kernel.application.industry_reading import read_industry
from decision_kernel.application.industry_operations import read_industry_operations
from decision_kernel.application.vehicle_flows import read_vehicle_flows
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    MissingObject, RecordError, Selection, SourceInput, SourceRef, StoreUnavailable, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from test_industry_reading import BUYER, SUBJECT, prepare as industry_data, put

CLOCK = '2026-10-17T09:00:00+08:00'


def packet(store, body):
    return put(store, SourceInput('synthetic-six-uses', 'content', 'Synthetic declared industry questions',
        'synthetic:industry-uses', encoded(body).decode(), published_at='2026-10-17',
        acquired_at='2026-10-17'))


def prepare(store):
    original, _ = industry_data(store)
    quote = 'Synthetic DDR5 16Gb qualification only; no authorized price observations.'
    qualification = put(store, SourceInput('synthetic-memory-qualification', 'content',
        'Synthetic source qualification', 'synthetic:qualification', quote + '\n',
        published_at='2026-10-16', acquired_at='2026-10-16'))
    declarations = []
    for family in uses.FAMILIES:
        blocked = family == 'memory'
        declarations.append({'use_id': 'synthetic-' + family, 'family': family,
            'title': 'Synthetic ' + family, 'question': 'Synthetic question; not a real market assessment.',
            'scope_note': 'One synthetic source reused to test selection; no industry coverage claim.',
            'disposition': 'source-blocked' if blocked else 'selected-evidence',
            'selections': [] if blocked else [{'input_id': 'original', 'series_ids': ['synthetic-copper-price']}],
            'evidence': [{'reference': asdict(SourceRef(qualification.manifest, Selection('lines', 1, 1))),
                'quote': quote, 'note': 'Synthetic qualification does not supply price values.'}] if blocked else [],
            'missing': ['Current contracts, reverse costs, timing and net exposure remain unknown.'],
            'reopen_when': 'Obtain authorized same-generation and same-specification source values.' if blocked else None})
    body = {'schema': uses.SCHEMA, 'prepared_at': CLOCK, 'as_of': CLOCK,
        'coverage_note': 'Six synthetic declared questions; five reuse one fixture, one remains source-blocked.',
        'inputs': [{'input_id': 'original', 'reference': asdict(original), 'view': 'industry'}],
        'uses': declarations}
    return packet(store, body), body


@pytest.fixture(scope='module')
def saved(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp('uses') / 'records.git'))
    root, body = prepare(store)
    return store, root, body


def test_exact_original_series_roles_and_independent_reopen(saved):
    store, root, body = saved
    before = store.backend.head()
    original = read_industry(SourceRef.from_dict(body['inputs'][0]['reference']), store)
    first = uses.read_industry_uses(root, store)
    second = uses.read_industry_uses(root, VersionedStore(GitStore(store.backend.root)))
    assert first == second and store.backend.head() == before
    assert first['status'] == 'partial' and first['scope']['read_uses'] == 5
    assert first['scope']['source_blocked_uses'] == 1 and first['scope']['unique_selected_series'] == 1
    assert first['scope']['resolved_selections'] == 5
    for use in first['uses']:
        if use['family'] == 'memory':
            assert use['status'] == 'source-blocked' and not use['series']
            assert use['evidence'][0]['original_verified'] is False
            assert encoded(use['evidence'][0]['reference']) == encoded(body['uses'][3]['evidence'][0]['reference'])
            continue
        selected = use['series'][0]
        assert {k: selected[k] for k in original['series'][0]} == original['series'][0]
        assert encoded(selected['packet_reference']) == encoded(body['inputs'][0]['reference'])
        assert [{k: r[k] for k in o} for r, o in zip(use['relations'], original['relations'])] == original['relations']
    assert first['input_outcomes'][0]['scope'] == original['scope']
    assert first['input_outcomes'][0]['relation_outcomes'] == original['relation_outcomes']
    assert first['writes'] == first['source_requests'] == 0 and first['research_executed'] is False


@pytest.mark.parametrize('query', [{'subject_id': SUBJECT.entity_id}, {'company_id': BUYER.entity_id},
                                  {'company_id': 'no-matching-role'}])
def test_queries_keep_six_uses_original_scope_blocks_and_unknowns(saved, query):
    store, root, _ = saved
    whole = uses.read_industry_uses(root, store)
    selected = uses.read_industry_uses(root, store, **query)
    assert selected['scope'] == whole['scope'] and selected['view_version'] == whole['view_version']
    assert selected['input_outcomes'] == whole['input_outcomes']
    for before, after in zip(whole['uses'], selected['uses']):
        assert {k: v for k, v in before.items() if k not in ('series', 'relations')} == {
            k: v for k, v in after.items() if k not in ('series', 'relations')}
        if query.get('company_id') == 'no-matching-role':
            assert after['series'] == after['relations'] == []
        for role in after['relations']:
            assert role['applicability_at_cutoff'] == 'historical'
            assert role['current_business_freshness'] == 'not-revalidated'
            assert all(role[k] is None for k in ('business_share', 'elasticity', 'pass_through', 'lag', 'net_benefit'))
            if query.get('company_id') == BUYER.entity_id:
                assert role['role'] == 'purchaser' and 'procurement costs' in role['transmission']['cost']['hypothesis']
    assert selected['uses'][3] == whole['uses'][3]


def test_missing_selector_does_not_hide_independent_uses_or_failures(saved):
    store, _, body = saved
    body = deepcopy(body)
    body['uses'][0]['selections'][0]['series_ids'].append('not-supplied')
    out = uses.read_industry_uses(packet(store, body), store)
    assert out['uses'][0]['status'] == 'partial' and out['scope']['read_uses'] == 4
    assert out['uses'][0]['selection_outcomes'][1]['reason_code'] == 'INDUSTRY_USES_SERIES_NOT_FOUND'
    assert out['uses'][0]['series'][0]['latest_change']['delta'] == '10'
    assert out['scope']['unique_selected_series'] == 2 and out['scope']['resolved_selections'] == 5


def test_failed_role_preserved_even_when_selected_values_are_readable(saved, monkeypatch):
    store, root, body = saved
    original = read_industry(SourceRef.from_dict(body['inputs'][0]['reference']), store)
    missing = original['relations'][0]['evidence']['reference']['manifest']
    actual = store.read_source
    def read(ref):
        if asdict(ref.manifest) == missing:
            raise MissingObject('OBJECT_MISSING')
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read)
    out = uses.read_industry_uses(root, store, company_id='no-matching-role')
    assert out['scope']['partial_uses'] == 5 and out['scope']['source_blocked_uses'] == 1
    assert out['input_outcomes'][0]['retained_scope']['declared_relations'] == 2
    assert out['input_outcomes'][0]['relation_outcomes'][0]['reason_code'] == 'OBJECT_MISSING'
    assert all(not use['series'] for use in out['uses'])


def test_unselected_failed_points_still_have_original_diagnostics(saved):
    from test_industry_reading import packet as industry_packet
    store, _, body = saved
    body = deepcopy(body)
    original = json.loads(store.read_source(SourceRef.from_dict(body['inputs'][0]['reference']))['data'])
    bad = deepcopy(original['series'][0]); bad['series_id'] = 'unselected-bad-series'
    for reference in bad['observations']:
        reference['manifest']['sha256'] = 'f' * 64
    original['series'].append(bad)
    body['inputs'][0]['reference'] = asdict(industry_packet(store, original))
    result = uses.read_industry_uses(packet(store, body), store, company_id='no-matching-role')
    assert result['scope']['partial_uses'] == 5
    source = result['input_outcomes'][0]
    assert source['retained_scope']['series'] == 2 and source['retained_scope']['unavailable_inputs'] == 2
    assert [(row['series_id'], row['observation_index'], row['reason_code'])
            for row in source['unavailable_observations']] == [
        ('unselected-bad-series', i, 'BLOB_DIGEST_MISMATCH') for i in (0, 1)]
    assert all(not use['series'] for use in result['uses'])


def test_blocked_qualification_requires_exact_retained_quote(saved):
    store, _, body = saved
    body = deepcopy(body)
    body['uses'][3]['evidence'][0]['quote'] = 'A fabricated authorized DDR5 price.'
    out = uses.read_industry_uses(packet(store, body), store)
    blocked = out['uses'][3]
    assert blocked['status'] == 'source-blocked' and blocked['series'] == []
    assert blocked['evidence'][0]['reason_code'] == 'INDUSTRY_USES_EVIDENCE_QUOTE'
    assert out['scope']['read_uses'] == 5 and out['scope']['unavailable_inputs'] == 1


def test_named_qualification_selects_original_record_without_inventing_lines(saved):
    store, _, body = saved
    body = deepcopy(body)
    retained = {'source_id': 'synthetic-ddr5', 'state': 'rights_authorization_required',
        'necessary_factual_samples': [], 'next_step_or_gate': 'Obtain the documented price licence.'}
    inventory = {'schema': 'source-qualification-inventory-part-v1', 'records': [
        {'source_id': 'other-provider', 'state': 'Other provider has a licence.', 'body': 'x' * 40000}, retained]}
    ref = put(store, SourceInput('synthetic-qualification-inventory', 'content', 'Synthetic compact inventory',
        'synthetic:qualification', encoded(inventory).decode(), acquired_at='2026-10-16', published_at='2026-10-16'))
    evidence = {'reference': asdict(ref), 'record_id': 'synthetic-ddr5',
                'quote': retained['next_step_or_gate'], 'note': 'Keep this exact named record and its whole root.'}
    body['uses'][3]['evidence'] = [evidence]
    out = uses.read_industry_uses(packet(store, body), store)
    opened = out['uses'][3]['evidence'][0]
    assert opened['record'] == retained and SourceRef.from_dict(opened['reference']) == ref
    assert 'other-provider' not in opened['text'] and opened['status'] == 'read'
    body['uses'][3]['evidence'][0]['quote'] = 'Other provider has a licence.'
    out = uses.read_industry_uses(packet(store, body), store)
    assert out['uses'][3]['evidence'][0]['reason_code'] == 'INDUSTRY_USES_EVIDENCE_QUOTE'
    assert out['uses'][3]['status'] == 'source-blocked'


@pytest.mark.parametrize('change', ['unknown-view', 'selected-lines', 'duplicate-input', 'missing-family',
                                   'blocked-values', 'undeclared-input', 'price-field'])
def test_invalid_composition_is_rejected_before_any_dependency(saved, monkeypatch, change):
    store, _, original = saved
    body = deepcopy(original)
    if change == 'unknown-view':
        body['inputs'][0]['view'] = 'industry-uses'
    elif change == 'selected-lines':
        body['inputs'][0]['reference']['selection'].update(kind='lines', start=1, end=1)
    elif change == 'duplicate-input':
        body['inputs'].append({**body['inputs'][0], 'input_id': 'alias'})
    elif change == 'missing-family':
        body['uses'][0]['family'] = 'freight'
    elif change == 'blocked-values':
        body['uses'][3]['selections'] = deepcopy(body['uses'][0]['selections'])
    elif change == 'undeclared-input':
        body['uses'][0]['selections'][0]['input_id'] = 'undeclared'
    else:
        body['uses'][0]['price'] = '999'
    root = packet(store, body)
    actual, seen = store.read_source, []
    def read(ref):
        seen.append(ref)
        assert ref == root, 'invalid declaration opened a dependency'
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read)
    with pytest.raises(RecordError):
        uses.read_industry_uses(root, store)
    assert seen == [root]


@pytest.mark.parametrize('view', ['vehicle-flows', 'operating-cycle'])
def test_delegated_series_keep_original_period_rules_and_whole_scope(saved, view):
    store, _, body = saved
    body = deepcopy(body)
    if view == 'vehicle-flows':
        from test_vehicle_flows import retain
        ref = retain(store)[0]
        expected = read_vehicle_flows(ref, store)
        selected = [expected['entries'][0], expected['entries'][4]]
        reason = 'INDUSTRY_CUMULATIVE_NOT_PERIOD_FLOW'
    else:
        from test_industry_operations import retain
        ref = retain(store)[0]
        expected = read_industry_operations(ref, store)
        entry = expected['entries'][0]
        selected = [{**entry['metrics'][name], 'metric': name, 'subject': entry['subject']}
                    for name in ('inventory', 'revenue')]
        reason = 'OPERATING_CUMULATIVE_NOT_MONTHLY'
    body['inputs'].append({'input_id': 'delegated', 'reference': asdict(ref), 'view': view})
    body['uses'][5]['selections'] = [{'input_id': 'delegated', 'series_ids': [s['series_id'] for s in selected]}]
    out = uses.read_industry_uses(packet(store, body), store)
    for original, projected in zip(selected, out['uses'][5]['series']):
        assert {k: projected[k] for k in original} == original
    assert out['uses'][5]['series'][1]['changes'][0]['reason_code'] == reason
    assert out['input_outcomes'][1]['scope'] == expected['scope']
    assert out['input_outcomes'][1]['retained_scope'] == expected['retained']['scope']


def test_child_cutoff_is_not_rewritten_by_composition(saved):
    store, _, body = saved
    body = deepcopy(body)
    body['as_of'] = body['prepared_at'] = '2026-10-12T10:00:00+08:00'
    out = uses.read_industry_uses(packet(store, body), store)
    assert out['input_outcomes'][0]['reason_code'] == 'INDUSTRY_USES_INPUT_AFTER_CUTOFF'
    assert out['scope']['partial_uses'] == 5 and out['scope']['read_uses'] == 0


@pytest.mark.parametrize('failure', ['budget', 'access', 'visibility'])
def test_operation_failure_caught_by_child_still_prevents_whole_result(saved, monkeypatch, failure):
    store, root, body = saved
    actual = store.read_source
    if failure == 'budget':
        child = SourceRef.from_dict(body['inputs'][0]['reference'])
        monkeypatch.setattr(uses, 'MAX_READ_BYTES', len(encoded(actual(root))) + len(encoded(actual(child))))
        expected = 'INDUSTRY_USES_READING_BOUND'
    else:
        error = StoreUnavailable('REMOTE_REQUEST_LIMIT') if failure == 'access' else RecordError('PRIVATE_UPGRADE_REVERSED')
        expected = str(error)
        def read(ref):
            if ref.selection.kind == 'rows':
                raise error
            return actual(ref)
        monkeypatch.setattr(store, 'read_source', read)
    with pytest.raises(RecordError, match=expected):
        uses.read_industry_uses(root, store)


def test_scope_exit_and_whole_result_budget_precede_return(saved, monkeypatch):
    store, root, _ = saved
    @contextmanager
    def revoke(_):
        yield store
        raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    with monkeypatch.context() as patch:
        patch.setattr(uses, 'industry_source_scope', revoke)
        with pytest.raises(StoreUnavailable, match='REMOTE_NOT_AUTHORIZED'):
            uses.read_industry_uses(root, store)
    monkeypatch.setattr(uses, 'MAX_RESULT_BYTES', 1)
    with pytest.raises(RecordError, match='INDUSTRY_USES_RESULT_BOUND'):
        uses.read_industry_uses(root, store, company_id='no-matching-role')


def test_nonmarket_quote_keeps_values_and_unknown_tax_comparison(saved):
    store, _, body = saved
    body = deepcopy(body)
    basis = {'metric': 'synthetic-route-benchmark', 'specification': 'synthetic-route-and-cargo',
             'currency': 'USD', 'unit': 'USD/metric-ton', 'price_kind': 'quote',
             'market': 'not-applicable', 'contract': 'not-applicable', 'tax_basis': 'unknown'}
    root, _ = industry_data(store, row_changes={0: basis, 1: basis})
    body['inputs'][0]['reference'] = asdict(root)
    result = uses.read_industry_uses(packet(store, body), store)
    series = result['uses'][2]['series'][0]
    assert [p['value'] for p in series['observations']] == ['100', '110']
    assert all(p['market'] == 'not-applicable' and p['tax_basis'] == 'unknown' for p in series['observations'])
    assert series['latest_change']['reason_code'] == 'INDUSTRY_BASIS_UNKNOWN'
    assert series['latest_change']['delta'] is series['latest_change']['percent_change'] is None


def test_cli_named_uses_reopens_exact_packet_and_preserves_partial(saved, tmp_path):
    store, root, _ = saved
    reference = tmp_path / 'reference.json'; reference.write_bytes(encoded(asdict(root)))
    env = {'PATH': os.environ.get('PATH', os.defpath),
           'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    command = [sys.executable, '-m', 'decision_kernel.interfaces.industry_cli',
               '--path', str(store.backend.root), '--ref', str(reference), '--industry-uses']
    before = store.backend.head()
    for query in ([], ['--company-id', BUYER.entity_id]):
        result = subprocess.run([*command, *query], cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=45)
        assert result.returncode == 2 and result.stderr == ''
        expected = uses.read_industry_uses(root, store, company_id=BUYER.entity_id if query else None)
        assert encoded(json.loads(result.stdout)) == encoded(expected)
    invalid = subprocess.run([*command, '--vehicle-flows'], cwd=tmp_path, env=env,
                             capture_output=True, text=True, timeout=45)
    assert invalid.returncode == 2 and invalid.stdout == ''
    assert store.backend.head() == before
