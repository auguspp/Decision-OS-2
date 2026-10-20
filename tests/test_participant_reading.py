"""Pinned participant packets, whole-scope diagnostics and independent recovery."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
import base64
import json

import pytest

from decision_kernel.application import participant_reading as reading
from decision_kernel.application.research_sources import capture_plan
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, StoreUnavailable, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.github_reading import reading_scope
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.observations import participant_observations as observations
from test_github_store import GitAPI
from test_participant_observations import page, estimates, CLOCK
from test_reading_scope import transport
from test_research_sources import registry, http, busy, plan as source_plan, request

PREPARED = '2026-10-20T08:00:00+08:00'


def put(store, source):
    return store.put_source('participants-' + digest(encoded(asdict(source)))[:50], source)


def packet(store, body):
    return put(store, SourceInput('synthetic-participant-packet', 'content', 'Synthetic participant packet',
        'synthetic:participants', encoded(body).decode(), acquired_at=PREPARED))


def make_packet(inputs):
    return {'schema': reading.SCHEMA, 'prepared_at': PREPARED, 'as_of': PREPARED,
        'coverage_note': 'Synthetic declared scope; not real-world evidence.', 'inputs': inputs,
        'families': [{'family': family, 'title': 'Synthetic ' + family, 'scope_note': 'Synthetic finite rows.',
            'input_ids': [i['input_id'] for i in inputs if observations.CONTRACTS[i['api']][0] == family],
            'missing': ['Complete holdings, authenticated people, current contracts and motives remain unknown.'],
            'reopen_when': None if any(observations.CONTRACTS[i['api']][0] == family for i in inputs)
                else 'Obtain an authorized retained input.'} for family in observations.FAMILIES]}


def prepare(store):
    rows = [page(), page('top_inst', [{'ts_code': '002384.SZ', 'trade_date': '20261016', 'exalter': '机构专用', 'buy': 2}]),
        page('hsgt_top10', [{'ts_code': '002384.SZ', 'trade_date': '20261016', 'market_type': 3, 'amount': 100}]),
        page('top10_floatholders', [
            {'ts_code': '002384.SZ', 'ann_date': day, 'end_date': period, 'holder_name': name, 'hold_amount': amount}
            for day, period, name, amount in [('20260428', '20260331', 'Synthetic holder', 100),
                ('20260822', '20260630', 'Synthetic holder', 80), ('20260428', '20260331', 'Absent next period', 5)]]),
        page('stk_surv', [{'ts_code': '002384.SZ', 'surv_date': '20261008', 'rece_org': 'Synthetic firm'}]),
        page('report_rc', estimates()),
        page('repurchase', [{'ts_code': '600000.SH', 'ann_date': '20261016', 'end_date': '20260930', 'proc': '实施', 'vol': 100}]),
        page('placement', [{'ts_code': '600000.SH', 'ann_date': '20261016', 'issue_date': '20261010',
                            'subscriber': 'Synthetic subscriber', 'lock_end_date': '20281010', 'issue_amount': 100}])]
    inputs = []
    for index, source_page in enumerate(rows):
        refs = [asdict(put(store, source)) for source in observations.page_sources(source_page,
            source_id='synthetic-input-' + str(index), origin='synthetic:page', title='Synthetic participant page')]
        inputs.append({'input_id': 'input-' + str(index), 'api': source_page['api'], 'format': 'page-parts', 'references': refs})
    body = make_packet(inputs)
    return packet(store, body), body


@pytest.fixture(scope='module')
def saved(tmp_path_factory):
    store = VersionedStore(GitStore.init_synthetic(tmp_path_factory.mktemp('participants') / 'records.git'))
    ref, body = prepare(store)
    return store, ref, body


@pytest.fixture(scope='module')
def saved_page_custody(tmp_path_factory):
    """Sequential ordinary source writes, with a distinct real commit per part."""
    model = GitAPI(tmp_path_factory.mktemp('participant-page-custody') / 'records.git')
    model.binding = replace(model.binding, visibility='public', private_upgrade_allowed=True,
                            write_enabled=False)
    anchor = model.git.commit({'store.json': model.binding.marker()}, expected=model.git.head())
    model.binding = replace(model.binding, anchor=anchor)
    model.git.repository = model.binding.repository
    store = VersionedStore(model.git)
    original = page('hkex_shareholding', [
        {'ts_code': '510300.SH' if index % 10 == 0 else '600000.SH',
         'holding_date': '20260930', 'channel': 'Synthetic channel', 'shareholding': index,
         'company_name': 'Synthetic company description. ' * 7, 'trace': digest(str(index).encode())}
        for index in range(1600)], offset=125, reported_total=1700, request_complete=False,
        error='SYNTHETIC_TERMINAL_FAILURE', wire_sha256=digest(b'Synthetic original bytes; no source request.'))
    original['query'] = {'holding_date': '20260930'}
    neighbor = observations.page_sources(page(), source_id='synthetic-small-neighbor',
        origin='synthetic:page-custody', title='Synthetic independent input')[0]
    after = {'input_id': 'neighbor', 'api': 'hm_detail', 'format': 'page-parts',
             'references': [asdict(put(store, neighbor))]}
    cases = {}
    for name, producer in [('plain', observations.page_sources), ('gzip', observations.gzip_page_sources)]:
        sources = producer(original, source_id='synthetic-large-' + name,
            origin='synthetic:page-custody', title='Synthetic complete physical page')
        refs = [asdict(put(store, source)) for source in sources]
        body = make_packet([{'input_id': 'large-page', 'api': 'hkex_shareholding',
                             'format': 'page-parts', 'references': refs}, after])
        cases[name] = packet(store, body), body
        if name == 'gzip':
            compressed_sources = sources
    # Retain a valid SourceRef to a corrupted final piece. Git integrity passes;
    # the page reader must reject all rows from this page after joining its tail.
    tail = json.loads(compressed_sources[-1].text)
    raw = base64.b64decode(tail['data'], validate=True)
    tail['data'] = base64.b64encode(raw[:-1] + bytes([raw[-1] ^ 1])).decode()
    bad = put(store, replace(compressed_sources[-1], source_id='synthetic-damaged-tail', text=encoded(tail).decode()))
    body = deepcopy(cases['gzip'][1])
    body['inputs'][0]['references'][-1] = asdict(bad)
    cases['damaged'] = packet(store, body), body
    model.requests.clear()
    return model, original, cases


def test_compressed_physical_page_reopens_within_native_commit_scope(saved_page_custody):
    model, original, cases = saved_page_custody
    ref, body = cases['gzip']
    references = [ref] + [SourceRef.from_dict(r) for i in body['inputs'] for r in i['references']]
    assert 1 < len(body['inputs'][0]['references'])
    assert len({r.manifest.commit for r in references}) == len(references) <= 32
    before, api = model.git.head(), transport(model)
    with reading_scope(GitHubStore(model.binding, api)) as backend:
        result = reading.read_participants(ref, VersionedStore(backend))
    opened, neighbor = result['input_outcomes']
    assert encoded(opened['references']) == encoded(body['inputs'][0]['references'])
    assert opened['physical_pages'] == [original['physical_page']]
    assert opened['status'] == 'incomplete' and opened['control']['error'] == 'SYNTHETIC_TERMINAL_FAILURE'
    assert neighbor['status'] == 'read' and result['scope']['raw_rows'] == 1601
    assert result['scope']['accepted_rows'] == 1441 and result['scope']['rejected_rows'] == 160
    assert [r['source_row'] for r in opened['rejected_rows']] == list(range(125, 1725, 10))
    assert {r['reason_code'] for r in opened['rejected_rows']} == {'PARTICIPANT_NON_A_SHARE'}
    retained = next(f for f in result['families'] if f['family'] == 'northbound')['rows']
    assert [r['raw'] for r in retained] == [r for i, r in enumerate(original['rows']) if i % 10]
    assert len(encoded(result)) < 8 * 1024 * 1024 and 0 < api._requests < 512
    assert result['source_requests'] == result['writes'] == model.writes == 0
    assert model.git.head() == before


def test_uncompressed_large_page_cannot_bypass_original_commit_scope_with_filter(saved_page_custody):
    model, _, cases = saved_page_custody
    ref, body = cases['plain']
    references = [ref] + [SourceRef.from_dict(r) for i in body['inputs'] for r in i['references']]
    assert len({r.manifest.commit for r in references}) == len(references) > 32
    before, api = model.git.head(), transport(model)
    with pytest.raises(RecordError, match='^OPERATING_SOURCE_SCOPE_BOUND$'):
        with reading_scope(GitHubStore(model.binding, api)) as backend:
            reading.read_participants(ref, VersionedStore(backend), company_id='no-matching-company')
    assert 0 < api._requests < 512 and model.git.head() == before and model.writes == 0


def test_corrupted_compressed_tail_rejects_whole_page_and_keeps_independent_input(saved_page_custody):
    model, _, cases = saved_page_custody
    ref, body = cases['damaged']
    before, api = model.git.head(), transport(model)
    with reading_scope(GitHubStore(model.binding, api)) as backend:
        result = reading.read_participants(ref, VersionedStore(backend), company_id='no-matching-company')
    bad, neighbor = result['input_outcomes']
    assert bad['status'] == 'unavailable' and bad['reason_code'] == 'PARTICIPANT_GZIP_DIGEST'
    assert encoded(bad['references']) == encoded(body['inputs'][0]['references'])
    assert neighbor['status'] == 'read' and result['scope']['accepted_rows'] == result['scope']['raw_rows'] == 1
    assert result['scope']['unavailable_inputs'] == 1 and result['scope']['declared_inputs'] == 2
    assert result['scope']['declared_references'] == sum(len(i['references']) for i in body['inputs'])
    assert all(not family['rows'] for family in result['families']) and len(result['families']) == 8
    assert model.git.head() == before and model.writes == result['source_requests'] == 0


def test_compression_does_not_bypass_unfiltered_result_bound(saved):
    store, _, _ = saved
    original = page(rows=[{'ts_code': '002384.SZ', 'trade_date': '20261016',
        'hm_name': 'Synthetic label', 'detail': 'Synthetic scalar detail. ' * 120, 'ordinal': i}
        for i in range(2200)])
    assert len(encoded(original)) < 8 * 1024 * 1024
    refs = [asdict(put(store, source)) for source in observations.gzip_page_sources(original,
        source_id='synthetic-result-bound', origin='synthetic:result-bound', title='Synthetic large result')]
    root = packet(store, make_packet([{'input_id': 'dense-page', 'api': 'hm_detail',
                                      'format': 'page-parts', 'references': refs}]))
    with pytest.raises(RecordError, match='^PARTICIPANT_RESULT_BOUND$'):
        reading.read_participants(root, store, company_id='no-matching-company')


def test_eight_families_exact_refs_and_independent_store_reopen(saved):
    store, ref, _ = saved
    before = store.backend.head()
    first = reading.read_participants(ref, store)
    recovered = reading.read_participants(ref, VersionedStore(GitStore(store.backend.root)))
    assert first == recovered and before == store.backend.head()
    assert first['status'] == 'read' and first['scope']['declared_families'] == 8
    assert first['scope']['accepted_rows'] == 11
    assert first['source_requests'] == first['writes'] == 0 and first['research_executed'] is False
    assert [x for x in first['estimate_changes'] if x['metric'] == 'eps'][0]['delta'] == '0.10'
    history = next(h for h in first['history'] if h['family'] == 'holders-funds' and len(h['observations']) == 2)
    assert {h['report_period'] for h in history['observations']} == {'20260331', '20260630'}
    assert history['summed_amount'] is None and history['absence_means_exit'] is False
    assert all(h['inferred_position'] is None for h in first['history'])


@pytest.mark.parametrize('query', [{'company_id': 'security-002384-sz'}, {'company_id': 'no-matching-company'}])
def test_company_query_preserves_whole_scope_and_every_family(saved, query):
    store, ref, _ = saved
    full = reading.read_participants(ref, store)
    result = reading.read_participants(ref, store, **query)
    assert full['scope'] == result['scope'] and full['input_outcomes'] == result['input_outcomes']
    assert full['view_version'] == result['view_version'] and len(result['families']) == 8
    for before, after in zip(full['families'], result['families']):
        assert {k: v for k, v in before.items() if k != 'rows'} == {k: v for k, v in after.items() if k != 'rows'}
        assert all(row['company_id'] == query['company_id'] for row in after['rows'])


def test_participant_query_finds_observed_companies_and_history_only(saved):
    store, ref, _ = saved
    full = reading.read_participants(ref, store)
    owner = next(h for h in full['history'] if h['family'] == 'holders-funds' and len(h['observations']) == 2)
    result = reading.read_participants(ref, store, participant_id=owner['participant_id'])
    assert result['scope'] == full['scope'] and result['history'] == [owner]
    rows = [row for family in result['families'] for row in family['rows']]
    assert len(rows) == 2 and all(r['company_id'] == 'security-002384-sz' for r in rows)
    with pytest.raises(RecordError, match='ONE_QUERY_DIRECTION'):
        reading.read_participants(ref, store, company_id='security-002384-sz', participant_id=owner['participant_id'])


def test_unavailable_input_and_missing_family_survive_irrelevant_filter(saved):
    store, _, original = saved
    body = deepcopy(original)
    source = body['inputs'][0]['references'][0]
    source['manifest']['sha256'] = 'f' * 64
    body['inputs'] = [i for i in body['inputs'] if i['api'] != 'placement']
    body['families'][-1]['input_ids'] = []
    body['families'][-1]['reopen_when'] = 'Recover the original source bytes; do not invent a receipt.'
    ref = packet(store, body)
    result = reading.read_participants(ref, store, company_id='no-matching-company')
    assert result['status'] == 'partial' and result['scope']['unavailable_inputs'] == 1
    assert result['scope']['not_retained_families'] == 1 and result['families'][-1]['status'] == 'not-retained'
    assert all(not f['rows'] for f in result['families']) and result['input_outcomes'][0]['status'] == 'unavailable'


@pytest.mark.parametrize('failure', ['REMOTE_REQUEST_LIMIT', 'PRIVATE_BINDING_MISMATCH'])
def test_operation_wide_access_and_budget_failures_are_not_hidden(saved, monkeypatch, failure):
    store, ref, _ = saved
    calls = []
    class Reader:
        def read_source(self, reference):
            calls.append(reference)
            if len(calls) == 2:
                raise StoreUnavailable(failure)
            return store.read_source(reference)
    @contextmanager
    def scope(_): yield Reader()
    monkeypatch.setattr(reading, 'industry_source_scope', scope)
    with pytest.raises(StoreUnavailable, match=failure):
        reading.read_participants(ref, store, company_id='no-matching-company')
    assert len(calls) == 2


def test_scope_exit_and_read_bound_precede_output(saved, monkeypatch):
    store, ref, _ = saved
    @contextmanager
    def scope(_):
        yield store
        raise StoreUnavailable('PRIVATE_UPGRADE_REVERSED')
    monkeypatch.setattr(reading, 'industry_source_scope', scope)
    with pytest.raises(StoreUnavailable, match='PRIVATE_UPGRADE_REVERSED'):
        reading.read_participants(ref, store)
    monkeypatch.undo()
    monkeypatch.setattr(reading, 'MAX_READ_BYTES', 1)
    with pytest.raises(RecordError, match='READING_BOUND'):
        reading.read_participants(ref, store, company_id='no-matching-company')


@pytest.mark.parametrize('change', ['duplicate-reference', 'duplicate-family', 'wrong-family', 'unknown-format', 'unused-input'])
def test_invalid_packet_fails_before_dependency_reads(saved, monkeypatch, change):
    store, _, original = saved
    body = deepcopy(original)
    if change == 'duplicate-reference': body['inputs'][1]['references'] = body['inputs'][0]['references']
    if change == 'duplicate-family': body['families'][1]['family'] = body['families'][0]['family']
    if change == 'wrong-family': body['families'][0]['input_ids'] = ['input-1']
    if change == 'unknown-format': body['inputs'][0]['format'] = 'any-url'
    if change == 'unused-input':
        body['families'][0]['input_ids'] = []
        body['families'][0]['reopen_when'] = 'Synthetic missing'
    ref = packet(store, body)
    calls, read = [], store.read_source
    def counted(reference):
        calls.append(reference)
        return read(reference)
    monkeypatch.setattr(store, 'read_source', counted)
    with pytest.raises(RecordError): reading.read_participants(ref, store)
    assert len(calls) == 1


def test_registered_failed_capture_preserves_terminal_receipt_without_recapture(saved):
    store, _, _ = saved
    client, opener = http(*(busy(504) for _ in range(4)))
    req = request(api='top_inst', fields='ts_code,trade_date,exalter,buy', ts_code='002384.SZ')
    result = list(capture_plan(source_plan(req), registry(), client=client, public_rights_confirmed=True,
        sleep=lambda _: None, clock=lambda: CLOCK))[0]
    assert len(opener.calls) == 4 and result['capture']['complete'] is False
    ref = put(store, SourceInput('synthetic-failed-top-inst', 'content', 'Synthetic failed original capture',
        'synthetic:relay-capture', encoded(result).decode(), acquired_at=CLOCK))
    body = make_packet([{'input_id': 'terminal-inst', 'api': 'top_inst', 'format': 'registered-relay',
                         'references': [asdict(ref)]}])
    root = packet(store, body)
    read = reading.read_participants(root, store, registry=registry())
    control = read['input_outcomes'][0]['control']
    assert control['complete'] is False and control['retained_attempt_count'] == 4
    assert control['error'] == result['capture']['error'] and read['scope']['accepted_rows'] == 0
    assert read['scope']['incomplete_inputs'] == 1 and read['scope']['not_retained_families'] == 7
    assert len(opener.calls) == 4 and read['source_requests'] == 0


def test_failed_capture_cannot_be_declared_as_another_family_even_without_pages(saved):
    store, _, _ = saved
    client, _ = http(*(busy(504) for _ in range(4)))
    req = request(api='top_inst', fields='ts_code,trade_date,exalter,buy', ts_code='002384.SZ')
    original = list(capture_plan(source_plan(req), registry(), client=client, public_rights_confirmed=True,
        sleep=lambda _: None, clock=lambda: CLOCK))[0]
    refs = [asdict(put(store, s)) for s in observations.registered_capture_sources(encoded(original),
        source_id='synthetic-failure-parts', origin='synthetic:old-capture', title='Synthetic captured failure', acquired_at=CLOCK)]
    body = make_packet([{'input_id': 'mislabeled', 'api': 'report_rc',
                         'format': 'registered-relay-parts', 'references': refs}])
    result = reading.read_participants(packet(store, body), store, registry=registry())
    assert result['input_outcomes'][0]['status'] == 'unavailable'
    assert result['input_outcomes'][0]['reason_code'] == 'PARTICIPANT_INPUT_SCOPE_MISMATCH'
    body['inputs'][0]['api'] = 'top_inst'
    body = make_packet(body['inputs'])
    good = reading.read_participants(packet(store, body), store, registry=registry())
    assert good['input_outcomes'][0]['control']['retained_attempt_count'] == 4
    assert good['input_outcomes'][0]['control']['complete'] is False
    assert good['source_requests'] == 0
