"""Synthetic retained goods: identity, eligibility, time and independent recovery."""
from contextlib import contextmanager
from copy import deepcopy
import csv
from dataclasses import asdict, replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from decision_kernel.application import industry_goods as goods
from decision_kernel.application.industry_reading import COLUMNS, read_industry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import (
    Entity, MissingObject, RecordError, Selection, SourceInput, SourceRef, StoreUnavailable, digest, encoded,
)
from decision_kernel.infrastructure.git_store import GitStore
from test_industry_reading import SUBJECT, BUYER, prepare as original, packet as original_packet, put

PREPARED = '2026-10-16T10:00:00+08:00'
EVALUATION = '2026-10-17T09:30:00+08:00'
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return VersionedStore(GitStore.init_synthetic(tmp_path / 'goods.git'))


def proof(store, *, entities=(SUBJECT,)):
    identity = 'Synthetic curation: retained subject IDs designate physical goods or explicit aliases.'
    rights = 'Synthetic curation: the bound source permits retained reading; no real rights claim.'
    source = SourceInput('synthetic-goods-evidence', 'content', 'Synthetic identity and use evidence',
        'synthetic:goods-evidence', identity + '\n' + rights + '\n', entities=entities,
        published_at='2026-10-16', acquired_at='2026-10-16')
    ref = put(store, source)
    return [{'reference': asdict(SourceRef(ref.manifest, Selection('lines', i, i))),
             'quote': text, 'note': 'Synthetic curator interpretation, not independent authentication.'}
            for i, text in enumerate((identity, rights), 1)]


def packet(store, body):
    source = SourceInput('synthetic-goods-packet', 'content', 'Synthetic finite goods batch',
        'synthetic:goods-packet', encoded(body).decode(), acquired_at=PREPARED, published_at=PREPARED)
    return store.put_source('goods-' + digest(encoded(asdict(source))), source)


def prepare(store, *, raw=None, entities=(SUBJECT,)):
    ref, original_body = raw or original(store)
    series = original_body['series'][0]
    source = store.read_source(SourceRef.from_dict(series['observations'][-1]))['manifest']['input']
    identity, rights = proof(store, entities=entities)
    body = {'schema': goods.SCHEMA, 'prepared_at': PREPARED, 'as_of': PREPARED,
        'coverage_note': 'Finite synthetic goods only; the global 500 target is not assessed.',
        'inputs': [{'input_id': 'original', 'reference': asdict(ref), 'view': 'industry'}],
        'sources': [{'source_id': source['source_id'], 'origin': source['origin'], 'family_id': 'synthetic-upstream',
            'rights': 'retained-use-permitted', 'freshness': {'max_period_age_days': 20, 'utc_offset_minutes': 480},
            'evidence': [rights], 'reopen_when': None}],
        'candidates': [{'candidate_id': 'first-candidate', 'title': 'Synthetic physical good',
            'subject_id': series['subject']['entity_id'], 'commodity_id': series['subject']['entity_id'],
            'disposition': 'physical-good', 'identity_evidence': [identity],
            'selections': [{'input_id': 'original', 'series_id': series['series_id']}],
            'missing': ['Business exposure is unknown; the full market has not been audited.'], 'reopen_when': None}]}
    return packet(store, body), body, original_body


def read(store, ref, **kwargs):
    return goods.read_industry_goods(ref, store, evaluation_at=kwargs.pop('evaluation_at', EVALUATION), **kwargs)


def variant(store, raw_body, *, subject=SUBJECT, changes=None, append=None, series_id=None):
    """Retain changed CSV bytes, never inject a qualified flag or reader result."""
    body = deepcopy(raw_body)
    old = SourceRef.from_dict(body['series'][0]['observations'][0])
    view = store.read_source(SourceRef(old.manifest))
    rows = deepcopy(view['data'])
    for row in rows:
        row['subject_id'] = subject.entity_id
    for index, changed in (changes or {}).items():
        rows[index].update(changed)
    if append:
        rows.append({**rows[-1], **append})
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    meta = view['manifest']['input']
    source = SourceInput(meta['source_id'], 'dataset', 'Synthetic altered survey', meta['origin'],
        stream.getvalue(), entities=(subject,), acquired_at=meta['acquired_at'], published_at=meta['published_at'])
    ref = put(store, source)
    body['series'][0] = {'series_id': series_id or raw_body['series'][0]['series_id'],
        'subject': asdict(subject), 'observations': [asdict(SourceRef(ref.manifest, Selection('rows', i, i, COLUMNS)))
                                                 for i in range(1, len(rows) + 1)]}
    body['relations'] = []
    return original_packet(store, body), body


def test_original_reader_drives_four_sets_without_claiming_global_coverage(store):
    ref, body, _ = prepare(store)
    head = store.backend.head()
    result = read(store, ref)
    expected = read_industry(SourceRef.from_dict(body['inputs'][0]['reference']), store)
    assert result['entries'][0]['selections'][0]['series'] == expected['series'][0]
    assert result['input_outcomes'][0]['retained_scope'] == expected['scope']
    assert result['sets']['candidate_ids'] == ['first-candidate']
    assert len(result['sets']['comparable_series']) == 1
    assert result['sets']['qualified_goods'] == result['sets']['fresh_goods'] == [SUBJECT.entity_id]
    assert result['scope']['global_qualified_goods'] is result['scope']['global_fresh_goods'] is None
    assert result['entries'][0]['identity_resolution']['status'] == 'evidenced-curation'
    assert result['entries'][0]['identity_resolution']['canonical_subject']['entity_id'] == SUBJECT.entity_id
    assert result['quantity_milestone']['status'] == 'not-established'
    assert result['status'] == 'read' and result['source_requests'] == result['writes'] == 0
    assert result['entries'][0]['relations'][0]['net_benefit'] is None
    assert store.backend.head() == head


def test_same_good_multiple_packets_and_company_query_keep_all_denominators(store):
    _, body, raw = prepare(store)
    second = deepcopy(raw)
    second['relations'] = []
    second['series'][0]['series_id'] = 'other-packet-same-observations'
    other = original_packet(store, second)
    body['inputs'].append({'input_id': 'other', 'reference': asdict(other), 'view': 'industry'})
    body['candidates'][0]['selections'].append({'input_id': 'other', 'series_id': second['series'][0]['series_id']})
    duplicate = deepcopy(body['candidates'][0])
    duplicate['candidate_id'] = 'same-good-again'
    body['candidates'].append(duplicate)
    root = packet(store, body)
    whole, filtered = read(store, root), read(store, root, company_id=BUYER.entity_id)
    assert whole['scope']['candidate_records'] == 2
    assert whole['scope']['distinct_qualified_goods'] == whole['scope']['fresh_goods'] == 1
    for key in ('scope', 'sets', 'outcomes', 'input_outcomes', 'view_version'):
        assert filtered[key] == whole[key]
    assert all(s['input_id'] == 'original' for e in filtered['entries'] for s in e['selections'])
    assert all(r['company']['entity_id'] == BUYER.entity_id for e in filtered['entries'] for r in e['relations'])
    absent = read(store, root, subject_id='not-present')
    assert absent['entries'] == [] and absent['scope'] == whole['scope']


def test_20_day_boundary_uses_original_period_and_source_timezone(store):
    root, body, _ = prepare(store)
    before = read(store, root, evaluation_at='2026-10-20T15:59:59Z')
    after = read(store, root, evaluation_at='2026-10-20T16:00:00Z')
    assert before['scope']['fresh_goods'] == 1 and after['scope']['fresh_goods'] == 0
    assert before['entries'][0]['selections'][0]['freshness']['period_age_days'] == 20
    assert after['entries'][0]['selections'][0]['freshness']['period_age_days'] == 21
    assert after['scope']['distinct_qualified_goods'] == 1 and after['status'] == 'partial'
    body['prepared_at'] = body['as_of'] = '2026-10-21T09:00:00+08:00'
    repacked = read(store, packet(store, body), evaluation_at=body['as_of'])
    assert repacked['scope']['fresh_goods'] == 0
    assert repacked['entries'][0]['selections'][0]['series'] == before['entries'][0]['selections'][0]['series']


def test_unknown_clock_is_not_zero_age_or_current_coverage(store):
    _, body, _ = prepare(store)
    body['sources'][0]['freshness'] = None
    result = read(store, packet(store, body))
    assert result['scope']['distinct_qualified_goods'] == 1 and result['scope']['fresh_goods'] == 0
    assert result['entries'][0]['selections'][0]['fresh'] is None
    assert result['entries'][0]['fresh'] is None
    assert result['scope']['freshness_unknown_goods'] == 1
    assert result['sets']['freshness_unknown_goods'] == [SUBJECT.entity_id]
    assert 'INDUSTRY_GOODS_FRESHNESS_UNKNOWN' in result['entries'][0]['selections'][0]['reasons']


def test_revisions_add_no_period_but_keep_an_existing_independent_baseline(store):
    raw = original(store, row_changes={0: {'period_start': '2026-09-01', 'period_end': '2026-09-30'}})
    ref, _, _ = prepare(store, raw=raw)
    result = read(store, ref)
    selected = result['entries'][0]['selections'][0]
    assert result['scope']['comparable_series'] == result['scope']['distinct_qualified_goods'] == 0
    assert selected['series']['latest_change']['status'] == 'same-period-revision'
    assert selected['comparison']['reason_code'] == 'INDUSTRY_GOODS_TWO_PERIODS_REQUIRED'
    _, raw = original(store)
    root, _, _ = prepare(store, raw=variant(store, raw, append={'value': '115'}))
    result = read(store, root)
    selected = result['entries'][0]['selections'][0]
    assert result['scope']['distinct_qualified_goods'] == 1
    assert selected['comparison']['from_index'] == 0 and selected['comparison']['to_index'] == 2
    assert selected['comparison']['delta'] == '15'
    assert selected['series']['latest_change']['status'] == 'same-period-revision'


@pytest.mark.parametrize('change,reason', [({'value': ''}, 'INDUSTRY_VALUE_MISSING'),
    ({'tax_basis': 'unknown'}, 'INDUSTRY_BASIS_CHANGED'),
    ({'specification': 'different-grade'}, 'INDUSTRY_BASIS_CHANGED')])
def test_missing_or_changed_basis_never_enters_qualified_goods(store, change, reason):
    ref, _, _ = prepare(store, raw=original(store, row_changes={1: change}))
    result = read(store, ref)
    assert result['scope']['distinct_qualified_goods'] == 0
    assert result['entries'][0]['selections'][0]['comparison']['reason_code'] == reason
    assert result['scope']['global_fresh_goods'] is None


@pytest.mark.parametrize('rights', ['restricted', 'unknown'])
def test_rights_keep_readable_values_and_original_reopen_condition(store, rights):
    _, body, _ = prepare(store)
    body['sources'][0].update(rights=rights, reopen_when='The original owner supplies applicable evidence.')
    result = read(store, packet(store, body))
    assert result['scope']['comparable_series'] == 1 and result['scope']['distinct_qualified_goods'] == 0
    selected = result['entries'][0]['selections'][0]
    assert selected['series']['observations'][-1]['value'] == '110'
    assert selected['source_qualification']['reopen_when'] == body['sources'][0]['reopen_when']
    assert 'INDUSTRY_GOODS_RIGHTS_' + rights.upper() in selected['reasons']


@pytest.mark.parametrize('bad', ['quote', 'entity-binding', 'source-binding'])
def test_evidence_binds_exact_text_entity_and_source(store, bad):
    _, body, _ = prepare(store)
    if bad == 'quote':
        body['candidates'][0]['identity_evidence'][0]['quote'] = 'Not present in that source.'
    elif bad == 'entity-binding':
        other = Entity('unrelated', 'commodity', 'Unrelated synthetic identity')
        body['candidates'][0]['identity_evidence'] = [proof(store, entities=(other,))[0]]
    else:
        body['sources'][0]['origin'] = 'synthetic:another-upstream'
    result = read(store, packet(store, body))
    assert result['scope']['distinct_qualified_goods'] == 0
    assert result['entries'][0]['status'] == 'unresolved'


@pytest.mark.parametrize('same_source', [False, True])
def test_inventory_evidence_must_bind_the_declared_source_record(store, same_source):
    _, body, _ = prepare(store)
    source_id, other = body['sources'][0]['source_id'], 'unrelated-licensed-source'
    inventory = {'schema': 'source-qualification-inventory-part-v1',
                 'records': [{'source_id': key, 'rights': 'retained-use-permitted'}
                             for key in (source_id, other)]}
    ref = put(store, SourceInput('synthetic-source-inventory', 'content', 'Synthetic rights records',
        'synthetic:source-inventory', encoded(inventory).decode(), acquired_at='2026-10-16',
        published_at='2026-10-16'))
    record_id = source_id if same_source else other
    body['sources'][0]['evidence'] = [{'reference': asdict(ref), 'record_id': record_id,
        'quote': 'retained-use-permitted', 'note': 'Synthetic record binding, no real rights claim.'}]
    root = packet(store, body)
    before = store.backend.head()
    result = read(store, root)
    qualification = result['source_qualifications'][0]
    proof = qualification['evidence'][0]
    assert result['scope']['comparable_series'] == 1
    assert result['scope']['distinct_qualified_goods'] == result['scope']['fresh_goods'] == int(same_source)
    assert proof['record_id'] == record_id and SourceRef.from_dict(proof['reference']) == ref
    assert qualification['status'] == ('read' if same_source else 'unavailable')
    if same_source:
        assert proof['record']['source_id'] == source_id
    else:
        assert result['status'] == 'partial'
        assert proof['reason_code'] == 'INDUSTRY_GOODS_SOURCE_EVIDENCE_BINDING'
        assert result['entries'][0]['selections'][0]['series']['observations'][-1]['value'] == '110'
    assert result['source_requests'] == result['writes'] == 0 and store.backend.head() == before


def test_four_channels_of_one_statistical_population_are_not_four_goods(store):
    entity = replace(SUBJECT, kind='industry', name='Synthetic statistical population')
    _, raw = original(store)
    _, raw = variant(store, raw, subject=entity)
    for index in range(3):
        raw['series'].append({**deepcopy(raw['series'][0]), 'series_id': 'channel-' + str(index)})
    root = original_packet(store, raw)
    _, body, _ = prepare(store, raw=(root, raw), entities=(entity,))
    candidate = body['candidates'][0]
    candidate['selections'] = [{'input_id': 'original', 'series_id': s['series_id']} for s in raw['series']]
    claimed = read(store, packet(store, body))
    assert claimed['scope']['comparable_series'] == 4 and claimed['scope']['distinct_qualified_goods'] == 0
    assert 'INDUSTRY_GOODS_NOT_PHYSICAL_SUBJECT' in claimed['entries'][0]['reasons']
    candidate.update(commodity_id=None, disposition='statistical-aggregate')
    excluded = read(store, packet(store, body))
    assert excluded['scope']['excluded_candidates'] == 1 and excluded['scope']['fresh_goods'] == 0


def test_same_upstream_copied_row_ids_require_one_canonical_good(store):
    _, body, raw = prepare(store)
    alias = Entity('synthetic-copper-alias', 'commodity', 'A source alias for the same synthetic good')
    alias_root, alias_body = variant(store, raw, subject=alias)
    body['inputs'].append({'input_id': 'alias', 'reference': asdict(alias_root), 'view': 'industry'})
    identity, _ = proof(store, entities=(SUBJECT, alias))
    second = deepcopy(body['candidates'][0])
    second.update(candidate_id='alias-candidate', subject_id=alias.entity_id, commodity_id=alias.entity_id,
        identity_evidence=[identity], selections=[{'input_id': 'alias', 'series_id': alias_body['series'][0]['series_id']}])
    body['candidates'][0]['identity_evidence'] = [identity]
    body['candidates'].append(second)
    conflict = read(store, packet(store, body))
    assert conflict['scope']['distinct_qualified_goods'] == 0
    assert all('INDUSTRY_GOODS_COMMODITY_IDENTITY_CONFLICT' in e['reasons'] for e in conflict['entries'])
    second['commodity_id'] = SUBJECT.entity_id
    merged = read(store, packet(store, body))
    assert merged['scope']['candidate_records'] == 2 and merged['scope']['distinct_qualified_goods'] == 1
    assert merged['sets']['qualified_goods'] == [SUBJECT.entity_id]


def test_distinct_observed_goods_share_an_upstream_but_alias_chains_do_not_add_goods(store):
    _, body, raw = prepare(store)
    other = Entity('synthetic-zinc', 'commodity', 'Synthetic zinc')
    last = Entity('synthetic-aluminium', 'commodity', 'Synthetic aluminium')
    evidence, _ = proof(store, entities=(SUBJECT, other, last))
    body['candidates'][0]['identity_evidence'] = [evidence]
    for entity in (other, last):
        root, original_body = variant(store, raw, subject=entity, changes={
            0: {'specification': entity.name}, 1: {'specification': entity.name}})
        key = entity.entity_id
        body['inputs'].append({'input_id': key, 'reference': asdict(root), 'view': 'industry'})
        candidate = deepcopy(body['candidates'][0])
        candidate.update(candidate_id=key, subject_id=key, commodity_id=key,
            selections=[{'input_id': key, 'series_id': original_body['series'][0]['series_id']}])
        body['candidates'].append(candidate)
    separate = read(store, packet(store, body))
    assert separate['sets']['qualified_goods'] == sorted(e.entity_id for e in (SUBJECT, other, last))
    # A -> B -> C is not an instruction to count B and C separately.
    body['candidates'][0]['commodity_id'] = other.entity_id
    body['candidates'][1]['commodity_id'] = last.entity_id
    chain = read(store, packet(store, body))
    assert chain['sets']['qualified_goods'] == [last.entity_id]
    assert 'INDUSTRY_GOODS_CANONICAL_NOT_ROOT' in chain['entries'][0]['reasons']
    body['candidates'][2]['commodity_id'] = SUBJECT.entity_id
    cycle = read(store, packet(store, body))
    assert cycle['scope']['distinct_qualified_goods'] == 0
    assert all('INDUSTRY_GOODS_CANONICAL_NOT_ROOT' in e['reasons'] for e in cycle['entries'])


@pytest.mark.parametrize('change', [{'period_kind': 'index-level'}, {'assessment': 'forecast'},
                                  {'market': 'futures', 'contract': 'synthetic-december-contract'}])
def test_index_or_explicit_forecast_cannot_qualify_a_physical_good_label(store, change):
    ref, _, _ = prepare(store, raw=original(store, row_changes={0: change, 1: change}))
    result = read(store, ref)
    assert result['scope']['comparable_series'] == 1 and result['scope']['distinct_qualified_goods'] == 0
    assert 'INDUSTRY_GOODS_NOT_OBSERVED_PHYSICAL_MEASURE' in result['entries'][0]['selections'][0]['reasons']


def test_nbs_requires_adjacent_original_periods_and_its_china_clock(store):
    changes = {0: {'frequency': 'ten-day', 'period_start': '2026-09-01', 'period_end': '2026-09-10'},
               1: {'frequency': 'ten-day', 'period_start': '2026-09-21', 'period_end': '2026-09-30'}}
    raw = original(store, row_changes=changes, source_changes={'source_id': 'nbs-material-prices'})
    root, body, _ = prepare(store, raw=raw)
    result = read(store, root)
    assert result['entries'][0]['selections'][0]['series']['latest_change']['status'] == 'changed'
    assert result['entries'][0]['selections'][0]['comparison']['reason_code'] == 'INDUSTRY_GOODS_NBS_PERIOD_GAP'
    assert result['scope']['distinct_qualified_goods'] == 0
    body['sources'][0]['freshness']['utc_offset_minutes'] = 0
    with pytest.raises(RecordError, match='INDUSTRY_GOODS_NBS_CLOCK'):
        read(store, packet(store, body))
    changes[0].update(period_start='2026-09-11', period_end='2026-09-20')
    root, _, _ = prepare(store, raw=original(store, row_changes=changes,
                                           source_changes={'source_id': 'nbs-material-prices'}))
    assert read(store, root)['scope']['distinct_qualified_goods'] == 1


def test_unselected_missing_branch_stays_in_whole_input_scope(store):
    _, body, raw = prepare(store)
    missing = deepcopy(raw['series'][0])
    missing['series_id'] = 'unselected-missing-series'
    for point in missing['observations']:
        point['manifest']['path'] = point['manifest']['path'].replace('source-views/', 'source-views/missing-')
    raw['series'].append(missing)
    body['inputs'][0]['reference'] = asdict(original_packet(store, raw))
    result = read(store, packet(store, body))
    assert result['scope']['distinct_qualified_goods'] == 1 and result['status'] == 'partial'
    original_scope = result['input_outcomes'][0]['retained_scope']
    assert original_scope['series'] == 2 and original_scope['unavailable_inputs'] == 2
    assert result['scope']['unique_selected_series'] == 1


def test_source_block_remains_in_full_outcomes_after_filter(store):
    _, body, _ = prepare(store)
    blocked = deepcopy(body['candidates'][0])
    blocked.update(candidate_id='blocked-memory', title='Synthetic blocked memory specification',
        subject_id=None, commodity_id=None, disposition='source-blocked', selections=[],
        reopen_when='The original rights restriction must actually be removed.')
    body['candidates'].append(blocked)
    result = read(store, packet(store, body), subject_id=SUBJECT.entity_id)
    assert result['scope']['source_blocked_candidates'] == 1 and result['status'] == 'partial'
    assert len(result['entries']) == 1
    assert result['outcomes'][1]['status'] == 'source-blocked'
    assert result['outcomes'][1]['reopen_when'] == blocked['reopen_when']
    assert result['source_requests'] == result['writes'] == 0


@pytest.mark.parametrize('change', [lambda b: b.update(qualified=True),
    lambda b: b['candidates'][0].update(qualified=True),
    lambda b: b['candidates'][0].update(value='999'),
    lambda b: b['sources'][0]['freshness'].update(max_period_age_days=21),
    lambda b: b['sources'].append({**b['sources'][0], 'source_id': 'same-upstream-mirror',
        'freshness': {'max_period_age_days': 10, 'utc_offset_minutes': 480}}),
    lambda b: b['inputs'][0].update(view='industry-uses'),
    lambda b: b['candidates'][0]['identity_evidence'][0]['reference']['selection'].update(kind='all', start=None, end=None),
    lambda b: b['inputs'].append({**b['inputs'][0], 'input_id': 'equivalent', 'reference': {
        **b['inputs'][0]['reference'], 'manifest': {**b['inputs'][0]['reference']['manifest'], 'commit': 'a' * 40}}})])
def test_untrusted_result_or_bad_plan_is_rejected_before_dependencies(store, monkeypatch, change):
    _, body, _ = prepare(store)
    change(body)
    root = packet(store, body)
    actual, seen = store.read_source, []
    def read_source(ref):
        seen.append(ref)
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read_source)
    with pytest.raises(RecordError):
        read(store, root)
    assert seen == [root]


def test_missing_original_is_partial_and_never_replaced_by_newer_copy(store, monkeypatch):
    root, body, _ = prepare(store)
    missing = body['inputs'][0]['reference']['manifest']['path']
    actual = store.read_source
    def read_source(ref):
        if ref.manifest.path == missing:
            raise MissingObject('SOURCE_NOT_FOUND')
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read_source)
    result = read(store, root)
    assert result['status'] == 'partial' and result['scope']['unavailable_input_packets'] == 1
    assert result['scope']['distinct_qualified_goods'] == 0 and result['scope']['global_fresh_goods'] is None
    assert encoded(result['input_outcomes'][0]['reference']) == encoded(body['inputs'][0]['reference'])


@pytest.mark.parametrize('code', ['REMOTE_NOT_AUTHORIZED', 'API_REQUEST_BOUND'])
def test_terminal_failure_caught_by_child_stays_sticky_without_later_reads(store, monkeypatch, code):
    root, _, raw = prepare(store)
    failing = SourceRef.from_dict(raw['series'][0]['observations'][0])
    actual, after, failed = store.read_source, [], False
    error = StoreUnavailable(code)
    def read_source(ref):
        nonlocal failed
        if failed:
            after.append(ref)
        if ref == failing:
            failed = True
            raise error
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read_source)
    with pytest.raises(StoreUnavailable) as raised:
        read(store, root)
    assert raised.value is error and failed and after == []


@pytest.mark.parametrize('code', ['READING_SCOPE_CLOSED', 'SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED', None])
def test_scope_record_errors_stop_reads_while_ordinary_missing_remains_local(store, monkeypatch, code):
    root, _, raw = prepare(store)
    first = SourceRef.from_dict(raw['series'][0]['observations'][0])
    original, after, failed = store.read_source, [], False
    error = RecordError(code) if code else MissingObject('SOURCE_NOT_FOUND')
    def read_source(ref):
        nonlocal failed
        if failed:
            after.append(ref)
        if ref == first and not failed:
            failed = True
            raise error
        return original(ref)
    before = store.backend.head()
    monkeypatch.setattr(store, 'read_source', read_source)
    if code:
        with pytest.raises(RecordError) as caught:
            read(store, root)
        assert caught.value is error and after == []
    else:
        result = read(store, root)
        assert result['status'] == 'partial' and after
        assert result['scope']['distinct_qualified_goods'] == 0
        assert result['entries'][0]['selections'][0]['series']['observations'][1]['value'] == '110'
        assert len(result['entries'][0]['relations']) == 2
    assert failed and store.backend.head() == before


def test_scope_exit_and_unfiltered_capacity_check_precede_output(store, monkeypatch):
    root, _, _ = prepare(store)
    @contextmanager
    def revoked(store):
        yield store
        raise StoreUnavailable('REMOTE_NOT_AUTHORIZED')
    with monkeypatch.context() as patch:
        patch.setattr(goods, 'industry_source_scope', revoked)
        with pytest.raises(StoreUnavailable, match='REMOTE_NOT_AUTHORIZED'):
            read(store, root)
    monkeypatch.setattr(goods, 'MAX_RESULT_BYTES', 200)
    with pytest.raises(StoreUnavailable, match='INDUSTRY_GOODS_RESULT_BOUND'):
        read(store, root, subject_id='no-matching-result')


def test_combined_read_budget_is_sticky_and_query_cannot_hide_it(store, monkeypatch):
    root, _, _ = prepare(store)
    limit = len(encoded(store.read_source(root))) + 1
    monkeypatch.setattr(goods, 'MAX_READ_BYTES', limit)
    actual, calls = store.read_source, []
    def read_source(ref):
        calls.append(ref)
        return actual(ref)
    monkeypatch.setattr(store, 'read_source', read_source)
    with pytest.raises(StoreUnavailable, match='INDUSTRY_GOODS_READING_BOUND'):
        read(store, root, subject_id='no-matching-result')
    assert len(calls) == 2 and calls[0] == root


def test_exact_packet_reopens_in_new_cli_process_without_a_write(store, tmp_path):
    root, _, _ = prepare(store)
    ref_path = tmp_path / 'goods-ref.json'
    ref_path.write_bytes(encoded(asdict(root)))
    before = store.backend.head()
    command = [sys.executable, '-m', 'decision_kernel.interfaces.industry_cli', '--path', str(store.backend.root),
               '--ref', str(ref_path), '--industry-goods', '--evaluation-at', EVALUATION]
    run = subprocess.run(command, cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0 and run.stderr == ''
    assert encoded(json.loads(run.stdout)) == encoded(read(store, root))
    stale_clock = '2026-10-21T09:00:00+08:00'
    stale = subprocess.run([*command[:-1], stale_clock], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')}, capture_output=True, text=True, timeout=30)
    assert stale.returncode == 2 and stale.stderr == ''
    assert encoded(json.loads(stale.stdout)) == encoded(read(store, root, evaluation_at=stale_clock))
    assert store.backend.head() == before
