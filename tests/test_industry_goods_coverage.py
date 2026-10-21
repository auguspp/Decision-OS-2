"""Cross-batch goods from retained synthetic Git inputs; no real collection."""
from contextlib import contextmanager
from copy import deepcopy
import csv
from dataclasses import asdict, replace
import io

import pytest

from decision_kernel.application import industry_goods as goods
from decision_kernel.application import industry_goods_coverage as coverage
from decision_kernel.application.industry_reading import COLUMNS
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Entity, RecordError, Selection, SourceInput, SourceRef, StoreUnavailable, digest, encoded,
)
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from test_github_store import GitAPI
from test_industry_goods import PREPARED, EVALUATION, prepare, packet, proof, variant, store
from test_industry_reading import SUBJECT, BUYER, packet as original_packet, put
from test_news_read_budget import transport


def save_coverage(store, refs, *, aliases=(), prepared=PREPARED):
    body = {'schema': coverage.SCHEMA, 'prepared_at': prepared, 'as_of': prepared,
            'coverage_note': 'Synthetic fixed cohort; not global market coverage or source rights.',
            'batches': [{'batch_id': 'batch-' + str(i), 'reference': asdict(ref)} for i, ref in enumerate(refs)],
            'aliases': list(aliases)}
    source = SourceInput('synthetic-goods-coverage', 'content', 'Synthetic fixed coverage',
                        'synthetic:goods-coverage', encoded(body).decode())
    return put(store, source), body


def read(store, root, **kwargs):
    return coverage.read_industry_goods_coverage(root, store,
        evaluation_at=kwargs.pop('evaluation_at', EVALUATION), **kwargs)


def alias_leaf(store, body, raw, *, entity=None, different_specification=False):
    entity = entity or Entity('synthetic-copper-alias', 'commodity', 'Synthetic copper alias')
    changes = {0: {'specification': entity.name}, 1: {'specification': entity.name}} if different_specification else None
    original, new_raw = variant(store, raw, subject=entity, changes=changes, series_id=entity.entity_id)
    other = deepcopy(body)
    other['coverage_note'] = 'Another retained synthetic batch.'
    other['inputs'][0]['reference'] = asdict(original)
    candidate = other['candidates'][0]
    candidate.update(subject_id=entity.entity_id, commodity_id=entity.entity_id,
                     identity_evidence=[proof(store, entities=(entity,))[0]])
    candidate['selections'][0]['series_id'] = new_raw['series'][0]['series_id']
    return packet(store, other), other, new_raw


def test_same_good_across_batches_is_one_and_subject_query_keeps_complete_sets(store):
    first, body, _ = prepare(store)
    second = deepcopy(body)
    second['coverage_note'] = 'Second declaration of the same retained observations.'
    second = packet(store, second)
    root, _ = save_coverage(store, [first, second])
    head = store.backend.head()
    full = read(store, root)
    assert full['scope']['candidate_records'] == 2
    assert full['scope']['comparable_series'] == full['scope']['distinct_qualified_goods'] == 1
    assert full['sets']['qualified_goods'] == full['sets']['fresh_goods'] == [SUBJECT.entity_id]
    assert full['sets']['candidate_ids'] == [['batch-0', 'first-candidate'], ['batch-1', 'first-candidate']]
    assert full['scope']['global_qualified_goods'] is full['scope']['global_fresh_goods'] is None
    assert full['quantity_milestone']['status'] == 'not-established'
    empty = read(store, root, subject_id='absent-synthetic-subject')
    assert empty['entries'] == []
    for key in ('scope', 'sets', 'outcomes', 'batches', 'view_version'):
        assert empty[key] == full[key]
    request = full['batches'][0]['open_request']
    assert request == {'operation': 'industry-read', 'payload': {
        'reference': asdict(first), 'view': 'industry-goods', 'evaluation_at': EVALUATION}}
    assert 'source_qualifications' not in full['entries'][0]
    assert all('series' not in s for e in full['entries'] for s in e['selections'])
    assert full['source_requests'] == full['writes'] == 0 and store.backend.head() == head


def test_equivalent_reachable_commit_does_not_make_a_second_batch(store):
    leaf, _, _ = prepare(store)
    first, _ = save_coverage(store, [leaf])
    equivalent = SourceRef(store.backend.reference(first.manifest.commit, leaf.manifest.path))
    root, _ = save_coverage(store, [leaf, equivalent])
    with pytest.raises(RecordError, match='INDUSTRY_COVERAGE_DUPLICATE_BATCH'):
        read(store, root)


def test_same_upstream_facts_in_separate_leaves_conflict_until_evidenced_alias(store):
    first, body, raw = prepare(store)
    alias = Entity('synthetic-copper-alias', 'commodity', 'Synthetic copper alias')
    second, _, _ = alias_leaf(store, body, raw, entity=alias)
    root, _ = save_coverage(store, [first, second])
    conflicted = read(store, root)
    assert conflicted['scope']['distinct_qualified_goods'] == conflicted['scope']['fresh_goods'] == 0
    assert conflicted['scope']['comparable_series'] == 2
    assert all('INDUSTRY_COVERAGE_FACT_CONFLICT' in e['reasons'] for e in conflicted['entries'])
    identity = proof(store, entities=(SUBJECT, alias))[0]
    root, _ = save_coverage(store, [first, second], aliases=[{
        'commodity_id': alias.entity_id, 'canonical_id': SUBJECT.entity_id, 'evidence': [identity]}])
    resolved = read(store, root)
    assert resolved['sets']['qualified_goods'] == resolved['sets']['fresh_goods'] == [SUBJECT.entity_id]
    assert resolved['aliases'][0]['status'] == 'read' and not resolved['conflicts']
    assert resolved['entries'][1]['commodity_id'] == alias.entity_id
    assert resolved['entries'][1]['canonical_id'] == SUBJECT.entity_id


def test_same_subject_cannot_point_to_different_retained_roots_in_different_batches(store):
    first, body, raw = prepare(store)
    other = Entity('synthetic-zinc', 'commodity', 'Synthetic zinc')
    _, other_raw = variant(store, raw, subject=other, changes={
        0: {'specification': 'Synthetic zinc'}, 1: {'specification': 'Synthetic zinc'}}, series_id='zinc-price')
    combined = deepcopy(raw)
    combined['series'].extend(other_raw['series'])
    original = original_packet(store, combined)
    next_body = deepcopy(body)
    next_body['inputs'][0]['reference'] = asdict(original)
    next_body['candidates'][0]['commodity_id'] = other.entity_id
    next_body['candidates'][0]['identity_evidence'] = [proof(store, entities=(SUBJECT, other))[0]]
    second = packet(store, next_body)
    assert goods.read_industry_goods(second, store, evaluation_at=EVALUATION)['scope']['distinct_qualified_goods'] == 1
    root, _ = save_coverage(store, [first, second])
    result = read(store, root)
    assert result['scope']['distinct_qualified_goods'] == 0
    assert all('INDUSTRY_COVERAGE_SUBJECT_CONFLICT' in e['reasons'] for e in result['entries'])


@pytest.mark.parametrize('bad', ['quote', 'metadata-binding', 'conflicting-metadata', 'unretained-target'])
def test_aliases_require_actual_retained_entities_and_exact_bound_evidence(store, bad):
    first, body, raw = prepare(store)
    alias = Entity('synthetic-copper-alias', 'commodity', 'Synthetic copper alias')
    second, _, _ = alias_leaf(store, body, raw, entity=alias)
    identity = proof(store, entities=(SUBJECT, alias))[0]
    target = SUBJECT.entity_id
    if bad == 'quote':
        identity['quote'] = 'This sentence does not exist in the retained evidence.'
    elif bad == 'metadata-binding':
        identity = proof(store, entities=(SUBJECT,))[0]
    elif bad == 'conflicting-metadata':
        identity = proof(store, entities=(SUBJECT, replace(alias, name='Conflicting proof metadata')))[0]
    else:
        absent = Entity('unretained-gold', 'commodity', 'Unretained synthetic gold')
        identity = proof(store, entities=(absent, alias))[0]
        target = absent.entity_id
    root, _ = save_coverage(store, [first, second], aliases=[{
        'commodity_id': alias.entity_id, 'canonical_id': target, 'evidence': [identity]}])
    result = read(store, root)
    assert result['aliases'][0]['status'] == 'unavailable'
    assert result['quantity_milestone']['status'] == 'not-established'
    assert not result['entries'][1]['qualified'] and result['status'] == 'partial'


@pytest.mark.parametrize('declared_alias', [False, True])
def test_canonical_root_is_checked_across_leaf_identity_declarations(store, declared_alias):
    first, body, raw = prepare(store)
    second_entity = Entity('synthetic-zinc', 'commodity', 'Synthetic zinc')
    third_entity = Entity('synthetic-aluminium', 'commodity', 'Synthetic aluminium')
    second, second_body, second_raw = alias_leaf(
        store, body, raw, entity=second_entity, different_specification=True)
    _, _, third_raw = alias_leaf(store, body, raw, entity=third_entity, different_specification=True)
    identity = proof(store, entities=(SUBJECT, second_entity, third_entity))[0]
    second_raw['series'].extend(third_raw['series'])
    second_body['inputs'][0]['reference'] = asdict(original_packet(store, second_raw))
    second_body['candidates'][0].update(commodity_id=third_entity.entity_id, identity_evidence=[identity])
    second = packet(store, second_body)
    aliases = []
    if declared_alias:
        aliases.append(dict(commodity_id=SUBJECT.entity_id, canonical_id=second_entity.entity_id,
                            evidence=[identity]))
    else:
        raw['series'].extend(second_raw['series'])
        body['inputs'][0]['reference'] = asdict(original_packet(store, raw))
        body['candidates'][0].update(commodity_id=second_entity.entity_id, identity_evidence=[identity])
        first = packet(store, body)
    assert all(goods.read_industry_goods(ref, store, evaluation_at=EVALUATION)[
        'scope']['distinct_qualified_goods'] == 1 for ref in (first, second))
    root, _ = save_coverage(store, [first, second], aliases=aliases)
    result = read(store, root)
    assert result['sets']['qualified_goods'] == [third_entity.entity_id]
    assert not result['entries'][0]['qualified']
    if declared_alias:
        assert result['aliases'][0]['status'] == 'unavailable'
        assert 'INDUSTRY_COVERAGE_ALIAS_NOT_DIRECT_ROOT' in result['aliases'][0]['reasons']
    else:
        assert 'INDUSTRY_COVERAGE_CANONICAL_NOT_ROOT' in result['entries'][0]['reasons']


@pytest.mark.parametrize('cycle', [False, True])
def test_alias_chains_and_cycles_are_not_followed_or_repaired(store, cycle):
    first, body, raw = prepare(store)
    second_entity = Entity('synthetic-zinc', 'commodity', 'Synthetic zinc')
    third_entity = Entity('synthetic-aluminium', 'commodity', 'Synthetic aluminium')
    second, _, _ = alias_leaf(store, body, raw, entity=second_entity, different_specification=True)
    third, _, _ = alias_leaf(store, body, raw, entity=third_entity, different_specification=True)
    identity = proof(store, entities=(SUBJECT, second_entity, third_entity))[0]
    aliases = [dict(commodity_id=SUBJECT.entity_id, canonical_id=second_entity.entity_id, evidence=[identity]),
               dict(commodity_id=second_entity.entity_id,
                    canonical_id=SUBJECT.entity_id if cycle else third_entity.entity_id, evidence=[identity])]
    root, _ = save_coverage(store, [first, second, third], aliases=aliases)
    result = read(store, root)
    assert result['sets']['qualified_goods'] == [third_entity.entity_id]
    assert all(a['status'] == 'unavailable' for a in result['aliases'])
    assert all('INDUSTRY_COVERAGE_ALIAS_NOT_DIRECT_ROOT' in a['reasons'] for a in result['aliases'])


@pytest.mark.parametrize('conflict', ['family', 'rights', 'clock'])
def test_same_source_declaration_conflicts_across_batches_invalidate_both(store, conflict):
    first, body, _ = prepare(store)
    other = deepcopy(body)
    if conflict == 'family':
        other['sources'][0]['family_id'] = 'different-claimed-upstream'
    elif conflict == 'rights':
        other['sources'][0].update(rights='unknown', reopen_when='Obtain actual retained-use evidence.')
    else:
        other['sources'][0]['freshness']['max_period_age_days'] = 19
    second = packet(store, other)
    root, _ = save_coverage(store, [first, second])
    result = read(store, root)
    assert result['scope']['distinct_qualified_goods'] == result['scope']['fresh_goods'] == 0
    assert all('INDUSTRY_COVERAGE_SOURCE_CONFLICT' in e['reasons'] for e in result['entries'])
    assert any(c['kind'] == 'source' for c in result['conflicts'])


def test_conflicting_original_entity_metadata_is_not_erased_by_same_id(store):
    first, body, raw = prepare(store)
    changed = Entity(SUBJECT.entity_id, 'commodity', 'A conflicting synthetic subject name')
    second, _, _ = alias_leaf(store, body, raw, entity=changed)
    root, _ = save_coverage(store, [first, second])
    result = read(store, root)
    assert result['scope']['distinct_qualified_goods'] == 0
    assert all('INDUSTRY_COVERAGE_ENTITY_CONFLICT' in e['reasons'] for e in result['entries'])


def test_missing_batch_and_new_clock_preserve_known_goods_without_global_completion(store):
    leaf, _, _ = prepare(store)
    missing = SourceRef(replace(leaf.manifest,
        path='source-views/missing/' + '0' * 64 + '/manifest.json'))
    root, _ = save_coverage(store, [leaf, missing])
    result = read(store, root, evaluation_at='2026-10-21T09:30:00+08:00')
    assert result['scope']['declared_batches'] == 2 and result['scope']['read_batches'] == 1
    assert result['scope']['unavailable_batches'] == 1
    assert result['scope']['distinct_qualified_goods'] == 1 and result['scope']['fresh_goods'] == 0
    assert result['batches'][0]['status'] == 'partial'
    assert result['batches'][1]['status'] == 'unavailable'
    assert 'DECLARED_BATCHES_UNAVAILABLE' in result['quantity_milestone']['reasons']
    assert 'FEWER_THAN_500_FRESH_QUALIFIED_GOODS' in result['quantity_milestone']['reasons']
    assert result['status'] == 'partial'


def test_child_after_root_cutoff_is_an_unavailable_batch_not_silently_read(store):
    leaf, _, _ = prepare(store)
    root, _ = save_coverage(store, [leaf], prepared='2026-10-15T10:00:00+08:00')
    result = read(store, root)
    assert result['batches'][0]['reason_code'] == 'INDUSTRY_COVERAGE_BATCH_AFTER_CUTOFF'
    assert result['scope']['read_batches'] == 0 and result['scope']['unavailable_batches'] == 1


def test_read_leaf_with_unavailable_original_input_keeps_goods_but_not_complete_milestone(store):
    leaf, body, _ = prepare(store)
    original = SourceRef.from_dict(body['inputs'][0]['reference'])
    missing = SourceRef(replace(original.manifest,
        path='source-views/missing/' + '0' * 64 + '/manifest.json'))
    body['inputs'].append({'input_id': 'missing-original', 'view': 'industry', 'reference': asdict(missing)})
    body['candidates'][0]['selections'].append({'input_id': 'missing-original', 'series_id': 'absent'})
    leaf = packet(store, body)
    root, _ = save_coverage(store, [leaf])
    result = read(store, root)
    assert result['scope']['distinct_qualified_goods'] == result['scope']['fresh_goods'] == 1
    assert result['scope']['read_batches'] == 1 and result['scope']['unavailable_batches'] == 0
    assert result['scope']['unavailable_input_packets'] == 1
    assert 'DECLARED_BATCH_INPUTS_UNAVAILABLE' in result['quantity_milestone']['reasons']


def test_company_query_and_invalid_root_clock_fail_before_reads(store):
    class NoRead:
        def read_source(self, ref):
            pytest.fail('invalid query opened storage')
    leaf, _, _ = prepare(store)
    root, _ = save_coverage(store, [leaf])
    with pytest.raises(RecordError, match='INDUSTRY_COVERAGE_COMPANY_QUERY_USE_BATCH'):
        read(NoRead(), root, company_id=BUYER.entity_id)
    with pytest.raises(RecordError, match='INDUSTRY_TIMESTAMP'):
        read(NoRead(), root, evaluation_at='2026-10-21T09:30:00')


@pytest.mark.parametrize('failure', ['remote', 'scope-record', 'exit'])
def test_one_outer_scope_stops_every_leaf_after_terminal_failure(store, monkeypatch, failure):
    first, body, _ = prepare(store)
    body['coverage_note'] = 'Second independent root for shared scope failure test.'
    second = packet(store, body)
    root, _ = save_coverage(store, [first, second])
    trace, calls = [], []
    @contextmanager
    def scope(store):
        calls.append('enter')
        class Reader:
            def read_source(self, ref):
                trace.append(ref)
                if ref == first and failure != 'exit':
                    if failure == 'remote':
                        raise StoreUnavailable('REMOTE_ACCESS_DENIED')
                    raise RecordError('SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED')
                return store.read_source(ref)
        yield Reader()
        calls.append('exit')
        if failure == 'exit':
            raise RecordError('PUBLIC_BINDING_MISMATCH')
    monkeypatch.setattr(coverage, 'industry_source_scope', scope)
    expected = {'remote': 'REMOTE_ACCESS_DENIED', 'scope-record': 'SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED',
                'exit': 'PUBLIC_BINDING_MISMATCH'}[failure]
    with pytest.raises(RecordError, match=expected):
        read(store, root)
    assert calls.count('enter') == 1
    if failure != 'exit':
        assert trace == [root, first]
    else:
        assert calls == ['enter', 'exit']


def test_shared_cache_and_read_budget_cover_all_leaves_not_each_leaf(store, monkeypatch):
    first, body, _ = prepare(store)
    body['coverage_note'] = 'Second declaration shares exact original dependencies.'
    second = packet(store, body)
    root, _ = save_coverage(store, [first, second])
    seen = {}
    class Measured:
        backend = object()
        def read_source(self, ref):
            key = encoded(asdict(ref))
            assert key not in seen, 'shared exact dependency was fetched twice'
            value = store.read_source(ref)
            seen[key] = len(encoded(value))
            return value
    result = read(Measured(), root)
    assert result['scope']['distinct_qualified_goods'] == 1
    total = sum(seen.values())
    monkeypatch.setattr(coverage, 'MAX_READ_BYTES', total - 1)
    seen.clear()
    with pytest.raises(StoreUnavailable, match='INDUSTRY_COVERAGE_READING_BOUND'):
        read(Measured(), root)


def test_complete_result_budget_checked_before_subject_filter(store, monkeypatch):
    leaf, _, _ = prepare(store)
    root, _ = save_coverage(store, [leaf])
    monkeypatch.setattr(coverage, 'MAX_RESULT_BYTES', 200)
    with pytest.raises(StoreUnavailable, match='INDUSTRY_COVERAGE_RESULT_BOUND'):
        read(store, root, subject_id='nothing-displayed')


def many_goods(store):
    """512 actual synthetic two-period series, staged at four reachable refs.

    Equivalent-reachable references are obtained from the real Git backend;
    no fake qualified results, budget constants, or alternate store are used.
    """
    raw, identities, groups = [], [], []
    for part in range(16):
        entities = [Entity(f'synthetic-good-{part * 32 + i:04}', 'commodity',
                           f'Synthetic physical good {part * 32 + i:04}') for i in range(32)]
        groups.append(entities)
        rows = []
        for period, start, end in ((0, '2026-08-01', '2026-08-31'), (1, '2026-09-01', '2026-09-30')):
            for entity in entities:
                rows.append(dict(subject_id=entity.entity_id, metric='price', specification=entity.name,
                    region='synthetic-region', currency='CNY', unit='CNY/tonne', price_kind='survey', market='spot',
                    contract='not-applicable', tax_basis='tax-inclusive', frequency='monthly',
                    period_kind='price-level', assessment='period-average', period_start=start,
                    period_end=end, value=str(100 + period)))
        stream = io.StringIO(newline='')
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        raw.append(put(store, SourceInput('synthetic-coverage-prices', 'dataset', 'Synthetic prices',
            'synthetic:coverage-prices', stream.getvalue(), entities=tuple(entities),
            acquired_at='2026-10-10', published_at='2026-10-01')))
        identities.append(put(store, SourceInput('synthetic-coverage-evidence', 'content', 'Synthetic curation',
            'synthetic:coverage-evidence', 'Synthetic physical identities.\nSynthetic retained-use permission.\n',
            entities=tuple(entities), acquired_at='2026-10-10', published_at='2026-10-01')))
    def at(ref, snapshot):
        return SourceRef(store.backend.reference(snapshot, ref.manifest.path))
    original_snapshot = store.backend.head()
    raw = [at(r, original_snapshot) for r in raw]
    identities = [at(r, original_snapshot) for r in identities]
    readings = []
    for entities, source in zip(groups, raw):
        body = dict(schema='industry-reading-v1', prepared_at=PREPARED, as_of=PREPARED,
            coverage_note='Synthetic cohort page, not actual market data.', use='Synthetic capacity and identity check.',
            relations=[], series=[{'series_id': e.entity_id, 'subject': asdict(e), 'observations': [
                asdict(SourceRef(source.manifest, Selection('rows', i + offset, i + offset, COLUMNS)))
                for offset in (1, 33)]} for i, e in enumerate(entities)])
        readings.append(original_packet(store, body))
    reading_snapshot = store.backend.head()
    readings = [at(r, reading_snapshot) for r in readings]
    leaves = []
    for entities, original, identity in zip(groups, readings, identities):
        evidence = lambda line, quote: {'reference': asdict(SourceRef(identity.manifest, Selection('lines', line, line))),
                                       'quote': quote, 'note': 'Synthetic evidence only.'}
        body = {'schema': goods.SCHEMA, 'prepared_at': PREPARED, 'as_of': PREPARED,
            'coverage_note': 'Synthetic fixed cohort page.',
            'inputs': [{'input_id': 'prices', 'reference': asdict(original), 'view': 'industry'}],
            'sources': [{'source_id': 'synthetic-coverage-prices', 'origin': 'synthetic:coverage-prices',
                'family_id': 'synthetic-coverage-upstream', 'rights': 'retained-use-permitted',
                'freshness': {'max_period_age_days': 20, 'utc_offset_minutes': 480},
                'evidence': [evidence(2, 'Synthetic retained-use permission.')], 'reopen_when': None}],
            'candidates': [{'candidate_id': e.entity_id, 'title': e.name, 'subject_id': e.entity_id,
                'commodity_id': e.entity_id, 'disposition': 'physical-good',
                'identity_evidence': [evidence(1, 'Synthetic physical identities.')],
                'selections': [{'input_id': 'prices', 'series_id': e.entity_id}],
                'missing': ['Synthetic data cannot certify actual market coverage.'], 'reopen_when': None} for e in entities]}
        leaves.append(packet(store, body))
    goods_snapshot = store.backend.head()
    return [at(r, goods_snapshot) for r in leaves]


def test_512_real_synthetic_series_pass_original_remote_request_and_size_limits(tmp_path):
    model = GitAPI(tmp_path / 'coverage-scale.git')
    model.git.repository = model.binding.repository
    store = VersionedStore(model.git)
    leaves = many_goods(store)
    root, _ = save_coverage(store, leaves)
    head = model.git.head()
    api = transport(model)
    reader = VersionedStore(GitHubStore(replace(model.binding, write_enabled=False), api))
    result = read(reader, root)
    assert result['scope']['declared_batches'] == result['scope']['read_batches'] == 16
    assert result['scope']['candidate_records'] == result['scope']['comparable_series'] == 512
    assert result['scope']['distinct_qualified_goods'] == result['scope']['fresh_goods'] == 512
    assert len(result['sets']['qualified_goods']) == len(set(result['sets']['qualified_goods'])) == 512
    assert result['scope']['global_fresh_goods'] is None and not result['conflicts']
    assert result['quantity_milestone']['status'] == 'established-in-declared-scope'
    assert result['status'] == 'read' and len(encoded(result)) <= coverage.MAX_RESULT_BYTES == 2 * 1024 * 1024
    assert 0 < api._requests <= MAX_REQUESTS == 512
    assert model.writes == 0 and model.git.head() == head
