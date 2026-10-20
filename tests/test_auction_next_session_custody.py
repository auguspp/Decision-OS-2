"""Exact next-session root recovery, full-result retention and bounded history."""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta
import json

import pytest

from decision_kernel.application.market_custody import (
    retain_market_reading, read_stored_market, market_reading_projection,
    _manifest_source, READING_SCHEMA as ROOT_SCHEMA,
)
from decision_kernel.domain.records import RecordError, SourceRef, UnknownWrite, digest, encoded
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.observations.market_daily import MarketInputError
from test_auction_inputs import auction_payload, capture, REGISTRY
from test_auction_custody import saved_fixture
from test_auction_next_session_inputs import CHECKED, RECEIVED, FIELDS, calendar, daily, payload


def save(store, fresh, value):
    return retain_market_reading(value, REGISTRY, store, fresh_reader=fresh, custody_authorized=True)


def next_fixture(value=None):
    store, fresh, original = saved_fixture(auction_payload(close=False))
    value = payload(original['reference']) if value is None else value(original['reference'])
    return store, fresh, original, save(store, fresh, value)


def root(store, ref):
    return json.loads(store.read_source(SourceRef.from_dict(ref))['data'])


def forged(store, value):
    snapshot = value['reading']['snapshot']
    snapshot['view_version'] = digest(encoded({k: v for k, v in snapshot.items() if k != 'view_version'}))
    return asdict(store.put_source('synthetic-t1-tamper-' + digest(encoded(value)), _manifest_source(ROOT_SCHEMA, value)))


def test_normal_save_fresh_reader_and_repeated_intent_keep_full_original_refs():
    store, fresh, original, saved = next_fixture()
    out = read_stored_market(saved['reference'], REGISTRY, fresh(), entity_id='600001.SH', limit=1)
    assert saved['status'] == 'stored-and-independently-restored' and out['status'] == 'restored'
    c = out['view']['cohorts'][0]
    assert encoded(c['auction_reference']) == encoded(original['reference']) and c['cohort_denominator'] == 8
    assert out['view']['entries'][0]['next_open'] == '110' and len(out['sources']) == 2
    before = store.backend.head()
    again = save(store, fresh, payload(original['reference']))
    assert encoded(again['reference']) == encoded(saved['reference']) and store.backend.head() == before


def test_another_next_session_root_cannot_recursively_act_as_original_auction(monkeypatch):
    store, _, _, saved = next_fixture()
    document = root(store, saved['reference'])
    queries = [SourceRef.from_dict(value).manifest for value in document['queries'].values()]
    actual, attempted = store.backend.resolve, []
    def observed(ref):
        attempted.append(ref)
        return actual(ref)
    monkeypatch.setattr(store.backend, 'resolve', observed)
    with pytest.raises(MarketInputError, match='ORIGINAL_ROOT_REQUIRED'):
        read_market_payload(payload(saved['reference'], cal=None, quotes={}), REGISTRY, store=store)
    assert not any(ref in queries for ref in attempted)


@pytest.mark.parametrize('variant', ['no-table', 'missing-row', 'missing-open', 'invalid-close', 'old-clock'])
def test_successor_loss_keeps_whole_prior_result_and_current_gap(variant):
    store, fresh, original, old = next_fixture()
    prior = old['restored']['view']['cohorts'][0]['result']
    if variant == 'no-table':
        quotes = {}
    elif variant == 'missing-row':
        quotes = {'2026-10-21': daily([['600002.SH', '20261021', '212', '209']])}
    elif variant == 'missing-open':
        quotes = {'2026-10-21': daily([['600001.SH', '20261021', '211'], ['600002.SH', '20261021', '212']], returned=FIELDS[:-1])}
    elif variant == 'invalid-close':
        quotes = {'2026-10-21': daily([['600001.SH', '20261021', 'bad', '210'], ['600002.SH', '20261021', '212', '209']])}
    else:
        source = daily([['600001.SH', '20261021', '211', '210'], ['600002.SH', '20261021', '212', '209']])
        for p in source['capture']['pages']:
            p['transport'].pop('request_clock')
        quotes = {'2026-10-21': source}
    new = save(store, fresh, payload(original['reference'], previous=old['reference'], quotes=quotes,
                                     checked='2026-10-22T08:00:00+00:00'))
    c = read_stored_market(new['reference'], REGISTRY, fresh())['view']['cohorts'][0]
    assert encoded(c['result']) == encoded(prior)
    assert c['observation'] == 'PREVIOUS_DATED_OBSERVATIONS_RETAINED'
    assert c['current_attempt']['unavailable_previously_observed_fields']
    assert c['result']['rows'][1]['readable_open'] != '210'
    again = save(store, fresh, payload(None, previous=new['reference'], quotes={}, cal=None,
                                       checked='2026-10-23T08:00:00+00:00'))
    assert encoded(again['restored']['view']['cohorts'][0]['result']) == encoded(prior)
    assert encoded(again['restored']['view']['previous_reference']) == encoded(new['reference'])


def test_first_partial_full_revision_and_unchanged_result_first_recorded_time():
    def initial(ref):
        return payload(ref, quotes={'2026-10-21': daily([['600001.SH', '20261021', '111']], returned=FIELDS[:-1])})
    store, fresh, original, old = next_fixture(initial)
    first = old['restored']['view']['cohorts'][0]['result']
    assert next(r for r in first['rows'] if r['entity_id'] == '600001.SH')['next_close'] == '111'
    changed = save(store, fresh, payload(None, previous=old['reference'], checked='2026-10-22T08:00:00+00:00'))
    result = changed['restored']['view']['cohorts'][0]['result']
    assert result['result_hash'] != first['result_hash'] and result['first_recorded_at'] == '2026-10-22T08:00:00+00:00'
    unchanged = save(store, fresh, payload(None, previous=changed['reference'],
        quotes={'2026-10-21': daily(stamp='2026-10-23T07:30:00+00:00')}, checked='2026-10-23T08:00:00+00:00'))
    c = unchanged['restored']['view']['cohorts'][0]
    assert c['result']['result_hash'] == result['result_hash'] and c['result']['first_recorded_at'] == result['first_recorded_at']
    assert c['observation'] == 'UNCHANGED_DATED_OBSERVATIONS' and c['current_attempt']['unavailable_previously_observed_fields'] == []


@pytest.mark.parametrize('field', ['price', 'members', 'calendar', 'query'])
def test_previous_model_hash_or_reference_is_not_a_substitute_for_exact_sources(field):
    store, fresh, original, old = next_fixture()
    value = root(store, old['reference']); c = value['reading']['snapshot']['cohorts'][0]
    if field == 'price':
        row = next(r for r in c['result']['rows'] if r['entity_id'] == '600001.SH')
        row['next_open'] = row['readable_open'] = '999'
        c['result']['result_hash'] = digest(encoded({'target_session': c['target_session'], 'rows': c['result']['rows']}))
    elif field == 'members':
        c['members'][0]['entity_id'] = '600999.SH'
        c['members_hash'] = digest(encoded(c['members']))
    elif field == 'calendar':
        c['calendar_prefix'][0]['is_open'] = False
    else:
        keys = list(value['queries']); value['queries'][keys[0]] = value['queries'][keys[1]]
    bad = forged(store, value); before = store.backend.head()
    with pytest.raises((RecordError, MarketInputError)):
        save(store, fresh, payload(original['reference'], previous=bad, quotes={}))
    assert store.backend.head() == before


def test_calendar_revision_or_future_source_cannot_be_hidden_behind_retention():
    store, fresh, original, old = next_fixture()
    before = store.backend.head()
    revised = calendar([['SSE', '20261021', 0], ['SSE', '20261022', 1]], end='20261022')
    with pytest.raises(MarketInputError, match='CALENDAR_REVISED'):
        save(store, fresh, payload(None, previous=old['reference'], quotes={}, cal=revised))
    with pytest.raises(MarketInputError, match='SOURCE_IN_FUTURE'):
        save(store, fresh, payload(None, previous=old['reference'], quotes={'2026-10-21': daily(stamp='2027-01-01T00:00:00Z')}))
    assert store.backend.head() == before


def later_auction(day='2026-10-22'):
    target = date.fromisoformat(day); prior = (target - timedelta(days=1)).isoformat()
    out = auction_payload(close=False); out['market_session'] = day
    stamp = day + 'T01:27:00+00:00'
    out['calendar_result'] = capture('trade_cal', ['exchange', 'cal_date', 'is_open'],
        [['SSE', prior.replace('-', ''), 1], ['SSE', day.replace('-', ''), 1]],
        {'exchange': 'SSE', 'start_date': (target - timedelta(days=31)).strftime('%Y%m%d'), 'end_date': day.replace('-', '')}, stamp=stamp)
    for slot, date_value in (('pool_result', prior), ('auction_result', day)):
        old = out[slot]; fields = old['capture']['pages'][0]['table']['fields']
        rows = deepcopy(old['capture']['pages'][0]['table']['items']); index = fields.index('trade_date')
        for row in rows: row[index] = date_value.replace('-', '')
        out[slot] = capture(old['use']['api'], fields, rows,
            {'trade_date': date_value.replace('-', ''), **({'ts_type': 'STK'} if slot == 'auction_result' else {})}, stamp=stamp)
    return out


def test_pending_cohort_survives_new_seed_and_completed_old_cohort_archives_exactly():
    store, fresh, original, pending = next_fixture(lambda ref: payload(ref, quotes={}))
    current = save(store, fresh, later_auction())
    cal = calendar([['SSE', '20261021', 1], ['SSE', '20261022', 1], ['SSE', '20261023', 1]], end='20261023')
    value = payload(current['reference'], previous=pending['reference'], quotes={}, cal=cal, checked='2026-10-23T08:00:00+00:00')
    saved = save(store, fresh, value)
    assert [c['market_session'] for c in saved['restored']['view']['cohorts']] == ['2026-10-20', '2026-10-22']
    assert saved['restored']['view']['archived_cohorts'] == []
    completed = save(store, fresh, payload(None, previous=pending['reference']))
    archived = save(store, fresh, {**value, 'previous_reference': completed['reference']})
    view = archived['restored']['view']
    assert len(view['cohorts']) == 1 and view['cohorts'][0]['market_session'] == '2026-10-22'
    assert encoded(view['archived_cohorts'][0]['reference']) == encoded(completed['reference'])
    assert view['archived_cohorts'][0]['target_session'] == '2026-10-21'


def test_diagnostics_projection_and_unknown_write_stop_original_intent(monkeypatch):
    store, fresh, original = saved_fixture(auction_payload(close=False))
    value = payload(original['reference'])
    for query in [value['calendar_result'], *value['quote_results'].values()]:
        for page in query['capture']['pages']:
            page['attempts'] = [{'edited': 'never-retained-synthetic-prose'}]
    projected = market_reading_projection(value, REGISTRY)
    assert 'never-retained-synthetic-prose' not in encoded(projected).decode()
    failure = UnknownWrite('UNKNOWN_SYNTHETIC_T1_WRITE'); calls = []
    def fail(*args, **kwargs):
        calls.append(True); raise failure
    monkeypatch.setattr(store, 'put_source', fail)
    with pytest.raises(UnknownWrite) as caught:
        save(store, fresh, projected)
    assert caught.value is failure and calls == [True]
