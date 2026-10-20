"""Finite exact-source T+1 roots in the existing market SourceInput custody.

Only one explicit predecessor is opened. Its effective seed/calendar/quote
evidence is independently requalified; no recursive history replay or new store.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json

from decision_kernel.application.auction_next_session_inputs import (
    READING_SCHEMA, VIEW_SCHEMA, MAX_QUERIES, calendar_input, checked_payload,
    daily_input, merge_calendars, reference,
)
from decision_kernel.application.market_reading_scope import SourceReads
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import MAX_BYTES, MissingObject, RecordError, SourceRef, digest, encoded
from decision_kernel.observations.auction_next_session import (
    MAX_COHORTS, clock, dated_result, project, require, retain_cohorts, seed, target_session,
)

SEED_KEYS = {'id', 'profile', 'market_session', 'cohort_version', 'auction_reference',
             'cohort_denominator', 'members', 'members_hash', 'original_groups',
             'prior_temperature', 'original_coverage', 'original_coverage_gaps',
             'auction_timeliness', 'first_recorded_at'}
RECIPE_KEYS = {'schema', 'checked_at', 'auction_reference', 'previous_reference',
               'calendar_keys', 'current_quotes', 'snapshot'}


@contextmanager
def _scope(store):
    from decision_kernel.application.market_custody import market_read_scope
    with market_read_scope(store) as scoped:
        backend = SourceReads(scoped.backend)
        try:
            yield VersionedStore(backend)
            backend.check()
        finally:
            backend.active = False


def _base(cohort):
    return {k: deepcopy(cohort[k]) for k in SEED_KEYS}


def _report(checked_at, current, previous_reference, cohorts, archived):
    require(len(cohorts) <= MAX_COHORTS and len(archived) <= MAX_COHORTS, 'ACTIVE_COHORT_BOUND')
    result = {'schema': VIEW_SCHEMA, 'checked_at': checked_at, 'current_auction': deepcopy(current),
              'previous_reference': deepcopy(previous_reference), 'cohorts': cohorts,
              'archived_cohorts': archived, 'source_requests': 0,
              'status': 'readable' if cohorts and all(
                  c['result']['status'] == 'NEXT_SESSION_OBSERVED' and not c['original_coverage_gaps']
                  for c in cohorts) else 'partial',
              'limitations': ['SOURCE_CAPTURE_CLOCK_NOT_EXCHANGE_TIMESTAMP_OR_HUMAN_RECEIPT',
                  'FIRST_EXPLICIT_SSE_OPEN_DATE_INDEPENDENT_OF_QUOTE_OR_COMPLETION',
                  'NO_CROSS_PACKAGE_PRICE_SPLICE_OR_CROSS_SESSION_RETURN',
                  'ONE_EXACT_PREDECESSOR_AND_EFFECTIVE_SOURCES_NOT_UNBOUNDED_HISTORY_AUDIT',
                  'NO_0940_OR_NATURAL_UPDATE_OR_TRADING_EFFECT_ACCEPTANCE'],
              'human_attention_authority': 'NONE', 'research_authority': 'NONE', 'investment_authority': 'NONE'}
    result['view_version'] = digest(encoded(result))
    require(len(encoded(result)) <= MAX_BYTES, 'REPORT_BOUND')
    return result


def _assemble(checked_at, current, current_meta, previous, previous_reference, calendars, quotes):
    if previous is not None:
        require(clock(previous['checked_at']) <= clock(checked_at), 'PREVIOUS_IN_FUTURE')
    candidates, archived = retain_cohorts(previous, current, previous_reference)
    previous_by_id = {c['id']: c for c in (previous or {}).get('cohorts', [])}
    values, through = merge_calendars(calendars)
    cohorts = [project(_base(c), values, through, quotes, checked_at, previous_by_id.get(c['id']))
               for c in candidates]
    require(quotes.keys() <= {c['target_session'] for c in cohorts if c['target_session'] is not None},
            'QUOTE_TARGET_NOT_ESTABLISHED')
    return _report(checked_at, current_meta, previous_reference, cohorts, archived)


class _Sources:
    """Per-operation exact-object reuse; admission and API allowance stay outside."""
    def __init__(self, registry, store):
        self.registry, self.store = registry, store
        self.queries, self.auctions = {}, {}

    def query(self, key, value):
        from decision_kernel.application.market_custody import restore_market_query, _prepare_query
        ref = reference(value)
        identity = encoded(ref)
        if identity not in self.queries:
            require(len(self.queries) < MAX_QUERIES, 'QUERY_BOUND')
            restored = restore_market_query(ref, self.registry, self.store)
            header, _ = _prepare_query(restored['result'], self.registry)
            require(digest(encoded(header)) == key, 'QUERY_CHANGED')
            self.queries[identity] = (key, restored['result'])
        stored_key, result = self.queries[identity]
        require(stored_key == key, 'QUERY_CHANGED')
        return result

    def auction(self, value, checked_at):
        from decision_kernel.application.auction_inputs import AUCTION_READING_SCHEMA
        from decision_kernel.application.market_custody import read_stored_market, _read_manifest, READING_SCHEMA as ROOT_SCHEMA
        ref = reference(value)
        identity = encoded(ref)
        if identity not in self.auctions:
            require(len(self.auctions) < MAX_COHORTS + 1, 'ORIGINAL_ROOT_BOUND')
            _, original = _read_manifest(self.store, ref, ROOT_SCHEMA)
            require(isinstance(original.get('reading'), dict)
                    and original['reading'].get('schema') == AUCTION_READING_SCHEMA, 'ORIGINAL_ROOT_REQUIRED')
            restored = read_stored_market(ref, self.registry, self.store, limit=100)
            require(restored['view'] is not None and all(
                item['status'] == 'restored' or item['path'] == ['close_result'] for item in restored['sources']),
                'ORIGINAL_SOURCE_UNAVAILABLE')
            self.auctions[identity] = restored['view']
        view = self.auctions[identity]
        current = seed(view, ref, checked_at)
        meta = {'reference': ref, 'cohort_id': current['id'] if current else None,
                'status': view['status'], 'cohort_denominator': view['cohort_denominator']}
        return current, meta

    def root(self, value):
        from decision_kernel.application.market_custody import _read_manifest, READING_SCHEMA as ROOT_SCHEMA
        ref, root = _read_manifest(self.store, value, ROOT_SCHEMA)
        return self.root_shape(ref, root)

    def root_shape(self, value, root):
        from decision_kernel.application.market_custody import _manifest_source, READING_SCHEMA as ROOT_SCHEMA
        ref = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
        require(set(root) == {'schema', 'reading', 'queries'} and isinstance(root['reading'], dict)
                and set(root['reading']) == RECIPE_KEYS and root['reading']['schema'] == READING_SCHEMA,
                'ROOT_REQUIRED')
        recipe, queries = root['reading'], root['queries']
        require(len(encoded(asdict(_manifest_source(ROOT_SCHEMA, root)))) <= MAX_BYTES
                and isinstance(queries, dict) and len(queries) <= MAX_QUERIES,
                'ROOT_BOUND')
        clock(recipe['checked_at'])
        for name in ('auction_reference', 'previous_reference'):
            if recipe[name] is not None:
                reference(recipe[name])
        require(isinstance(recipe['calendar_keys'], list)
                and len(set(recipe['calendar_keys'])) == len(recipe['calendar_keys'])
                and isinstance(recipe['current_quotes'], dict), 'ROOT_QUERIES')
        snapshot = recipe['snapshot']
        require(isinstance(snapshot, dict) and snapshot.get('schema') == VIEW_SCHEMA
                and isinstance(snapshot.get('cohorts'), list) and len(snapshot['cohorts']) <= MAX_COHORTS
                and snapshot.get('checked_at') == recipe['checked_at']
                and snapshot.get('previous_reference') == recipe['previous_reference'], 'ROOT_SNAPSHOT')
        require(snapshot.get('view_version') == digest(encoded({k: v for k, v in snapshot.items()
                if k != 'view_version'})), 'SNAPSHOT_CHANGED')
        used = set(recipe['calendar_keys']) | set(recipe['current_quotes'].values())
        for c in snapshot['cohorts']:
            require(isinstance(c, dict) and SEED_KEYS <= c.keys() and isinstance(c.get('result'), dict),
                    'COHORT_SHAPE')
            if c['result'].get('quote_key') is not None:
                used.add(c['result']['quote_key'])
        require(used == queries.keys(), 'ROOT_QUERY_ASSOCIATION')
        for value in queries.values():
            require(SourceRef.from_dict(value).manifest.repository == ref.manifest.repository, 'FOREIGN_QUERY')
        require(all(SourceRef.from_dict(c['auction_reference']).manifest.repository == ref.manifest.repository
                    for c in snapshot['cohorts']), 'FOREIGN_ORIGINAL')
        return json.loads(encoded(asdict(ref))), root

    def inputs(self, root):
        recipe, queries = root['reading'], root['queries']
        checked_at = recipe['checked_at']
        calendars = [calendar_input(self.query(key, queries[key]), self.registry, checked_at, key)
                     for key in recipe['calendar_keys']]
        quotes = {day: daily_input(self.query(key, queries[key]), self.registry, day, checked_at, key)
                  for day, key in recipe['current_quotes'].items()}
        return calendars, quotes

    def snapshot(self, root):
        """Verify the effective previous state directly, never follow its parent.

        Metadata first-recorded claims are retained immutable recording claims;
        actual quote values, target, groups and completion are recomputed from
        this root's exact original sources, not accepted from its saved hash.
        """
        recipe, queries = root['reading'], root['queries']
        old, checked_at = recipe['snapshot'], recipe['checked_at']
        calendars, quotes = self.inputs(root)
        values, through = merge_calendars(calendars)
        ids = set()
        for cohort in old['cohorts']:
            require(cohort['id'] not in ids, 'DUPLICATE_COHORT')
            ids.add(cohort['id'])
            require(clock(cohort['first_recorded_at']) <= clock(checked_at), 'PREVIOUS_IN_FUTURE')
            basis, _ = self.auction(cohort['auction_reference'], cohort['first_recorded_at'])
            require(basis is not None and encoded(basis) == encoded(_base(cohort)), 'PREVIOUS_SEED_CHANGED')
            target, prefix, gap = target_session(basis, values, through)
            require(target == cohort['target_session'] and prefix == cohort['calendar_prefix']
                    and gap == cohort['calendar_gap'], 'PREVIOUS_CALENDAR_CHANGED')
            saved = cohort['result']
            key = saved['quote_key']
            quote = daily_input(self.query(key, queries[key]), self.registry, target, checked_at, key) if key else None
            derived = dated_result(basis, target, gap, quote, checked_at)
            require(encoded({k: v for k, v in saved.items() if k != 'first_recorded_at'})
                    == encoded({k: v for k, v in derived.items() if k != 'first_recorded_at'}), 'PREVIOUS_RESULT_CHANGED')
            require(clock(basis['first_recorded_at']) <= clock(saved['first_recorded_at']) <= clock(checked_at),
                    'PREVIOUS_RESULT_CLOCK')
            retained = cohort['observation'] == 'PREVIOUS_DATED_OBSERVATIONS_RETAINED'
            require(cohort['observation'] in ('PREVIOUS_DATED_OBSERVATIONS_RETAINED',
                    'UNCHANGED_DATED_OBSERVATIONS', 'DATED_OBSERVATIONS_UPDATED'), 'PREVIOUS_OBSERVATION')
            check = project(basis, values, through, quotes, checked_at, cohort if retained else None)
            require(encoded(check['current_attempt']) == encoded(cohort['current_attempt']), 'PREVIOUS_ATTEMPT_CHANGED')
            require(not retained or bool(check['current_attempt']['unavailable_previously_observed_fields']),
                    'PREVIOUS_RETENTION_CHANGED')
            if not retained:
                require(derived['quote_key'] == check['result']['quote_key'], 'PREVIOUS_QUOTE_CHANGED')
        current = None
        if recipe['auction_reference'] is not None:
            _, current = self.auction(recipe['auction_reference'], checked_at)
        require(encoded(current) == encoded(old['current_auction']), 'PREVIOUS_CURRENT_SEED_CHANGED')
        require(encoded(_report(checked_at, current, recipe['previous_reference'], old['cohorts'], old['archived_cohorts']))
                == encoded(old), 'PREVIOUS_REPORT_CHANGED')
        return deepcopy(old)

    def transition(self, root):
        recipe = root['reading']
        previous = None
        if recipe['previous_reference'] is not None:
            _, prior = self.root(recipe['previous_reference'])
            previous = self.snapshot(prior)  # One parent, direct effective sources only.
        current = meta = None
        if recipe['auction_reference'] is not None:
            current, meta = self.auction(recipe['auction_reference'], recipe['checked_at'])
        calendars, quotes = self.inputs(root)
        view = _assemble(recipe['checked_at'], current, meta, previous, recipe['previous_reference'], calendars, quotes)
        require(encoded(view) == encoded(recipe['snapshot']), 'RESTORED_READING_CHANGED')
        return view


def _prepared(payload, registry):
    from decision_kernel.application.market_custody import _prepare_query
    checked_payload(payload)
    prepared, calendars, quotes = {}, [], {}
    for kind, day, result in ([('calendar', None, payload['calendar_result'])] +
                              [('quote', day, result) for day, result in payload['quote_results'].items()]):
        if result is None:
            continue
        header, pages = _prepare_query(result, registry)
        key = digest(encoded(header))
        prepared.setdefault(key, (header, pages))
        if kind == 'calendar':
            calendars.append(calendar_input(result, registry, payload['checked_at'], key))
        else:
            quotes[day] = daily_input(result, registry, day, payload['checked_at'], key)
    return prepared, calendars, quotes


def project_payload(payload, registry):
    from decision_kernel.application.market_custody import _document, _reading_envelope
    prepared, _, _ = _prepared(payload, registry)
    from decision_kernel.application.market_custody import _prepare_query
    def one(value):
        header, _ = _prepare_query(value, registry)
        _, pages = prepared[digest(encoded(header))]
        return _reading_envelope(header, [{**_document(s.text.encode())['page'], 'attempts': []} for s in pages])
    return {**deepcopy(payload), 'calendar_result': one(payload['calendar_result']) if payload['calendar_result'] is not None else None,
            'quote_results': {day: one(value) for day, value in payload['quote_results'].items()}}


def _build(payload, registry, sources):
    prepared, calendars, quotes = _prepared(payload, registry)
    previous = prior_root = None
    queries, old_calendars = {}, []
    if payload['previous_reference'] is not None:
        _, prior_root = sources.root(payload['previous_reference'])
        previous = sources.snapshot(prior_root)
        queries = deepcopy(prior_root['queries'])
        old_calendars, _ = sources.inputs(prior_root)
    current = meta = None
    if payload['auction_reference'] is not None:
        current, meta = sources.auction(payload['auction_reference'], payload['checked_at'])
    require(len(set(queries) | set(prepared)) <= MAX_QUERIES, 'QUERY_BOUND')
    all_calendars = old_calendars + [item for item in calendars if item['key'] not in {c['key'] for c in old_calendars}]
    view = _assemble(payload['checked_at'], current, meta, previous, payload['previous_reference'], all_calendars, quotes)
    # Keep the current exact calendar and enough old originals to prove every
    # retained prefix. Redundant earlier calendars need not accumulate forever.
    needed = {r['session'] for c in view['cohorts'] for r in c['calendar_prefix']}
    selected = [c['key'] for c in calendars]
    for c in reversed(all_calendars):
        if c['key'] in selected or needed & c['days'].keys():
            if c['key'] not in selected:
                selected.append(c['key'])
            needed -= c['days'].keys()
    # With no new calendar, preserve an original establishing an explicit gap
    # or end even when its prefix is empty.
    if not selected and all_calendars:
        selected.append(all_calendars[-1]['key'])
    selected_calendars = [c for key in selected for c in all_calendars if c['key'] == key]
    view = _assemble(payload['checked_at'], current, meta, previous, payload['previous_reference'], selected_calendars, quotes)
    used = set(selected) | {q['key'] for q in quotes.values()} | {
        c['result']['quote_key'] for c in view['cohorts'] if c['result']['quote_key'] is not None}
    queries = {key: value for key, value in queries.items() if key in used}
    prepared = {key: value for key, value in prepared.items() if key in used}
    recipe = {'schema': READING_SCHEMA, 'checked_at': payload['checked_at'],
              'auction_reference': deepcopy(payload['auction_reference']), 'previous_reference': deepcopy(payload['previous_reference']),
              'calendar_keys': selected, 'current_quotes': {day: q['key'] for day, q in quotes.items()}, 'snapshot': view}
    return recipe, queries, prepared


def read_payload(payload, registry, store, *, offset=0, limit=50, entity_id=None):
    checked_payload(payload)
    require(isinstance(store, VersionedStore), 'STORE_REQUIRED')
    with _scope(store) as reader:
        recipe, _, _ = _build(payload, registry, _Sources(registry, reader))
    return paginate(recipe['snapshot'], offset=offset, limit=limit, entity_id=entity_id)


def paginate(view, *, offset=0, limit=50, entity_id=None):
    from decision_kernel.observations.opening_auction import CODE
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 100, 'PAGE_INVALID')
    require(entity_id is None or isinstance(entity_id, str) and CODE.fullmatch(entity_id), 'ENTITY_INVALID')
    result = deepcopy(view)
    rows = [{**r, 'cohort_id': c['id'], 'market_session': c['market_session'], 'target_session': c['target_session'],
             'member_id': r['cohort_id']} for c in view['cohorts'] for r in c['result']['rows']
            if entity_id is None or r['entity_id'] == entity_id]
    result['entries'] = rows[offset:offset + limit]
    result['pagination'] = {'offset': offset, 'limit': limit, 'entity_id': entity_id, 'matched_count': len(rows),
                            'returned': len(result['entries']), 'next_offset': offset + limit if offset + limit < len(rows) else None}
    return result


def retain(payload, registry, store, *, fresh_reader, custody_authorized=False, public_rights_confirmed=False):
    from decision_kernel.application.market_custody import (
        READING_SCHEMA as ROOT_SCHEMA, authorize_market_custody, fresh_market_reader,
        _retain_prepared_query, _manifest_source, _put,
    )
    authorize_market_custody(store, custody_authorized=custody_authorized, public_rights_confirmed=public_rights_confirmed)
    checked_payload(payload)
    with _scope(fresh_market_reader(store, fresh_reader)) as reader:
        recipe, queries, prepared = _build(payload, registry, _Sources(registry, reader))
    query_reader = fresh_market_reader(store, fresh_reader)
    pages = {}
    for key, (header, originals) in prepared.items():
        if key not in queries:
            queries[key] = _retain_prepared_query(header, originals, registry, store, query_reader, pages)['reference']
    root = {'schema': ROOT_SCHEMA, 'reading': recipe, 'queries': queries}
    source = _manifest_source(ROOT_SCHEMA, root)
    require(len(encoded(asdict(source))) <= MAX_BYTES, 'ROOT_BOUND')
    ref = _put(store, source)
    final = fresh_market_reader(store, fresh_reader)
    require(final is not query_reader and final.backend is not query_reader.backend, 'FRESH_READER_REQUIRED')
    restored = read(ref, registry, final)
    require(restored['status'] == 'restored'
            and encoded({k: v for k, v in restored['view'].items() if k not in ('entries', 'pagination')})
            == encoded(recipe['snapshot']), 'RESTORED_READING_CHANGED')
    authorize_market_custody(store, custody_authorized=custody_authorized, public_rights_confirmed=public_rights_confirmed)
    return {'status': 'stored-and-independently-restored', 'reference': asdict(ref),
            'query_count': len(queries), 'page_count': len(pages), 'restored': restored}


def restore_root(ref, root, registry, store):
    sources = _Sources(registry, store)
    # Validate the same canonical root shape even when the common dispatcher
    # already loaded its SourceInput. Its exact bytes are supplied, not guessed.
    value, _ = sources.root_shape(ref, root)
    outcomes = [{'path': ['queries', key], 'reference': deepcopy(reference), 'query': key}
                for key, reference in root['queries'].items()]
    try:
        view = sources.transition(root)
    except MissingObject as error:
        return {'reference': value, 'payload': {'schema': READING_SCHEMA}, 'next_session_view': None,
                'query_count': len(root['queries']), 'unavailable_queries': 1, 'sources': outcomes,
                'next_session_error': str(error)}
    return {'reference': value, 'payload': {'schema': READING_SCHEMA}, 'next_session_view': view,
            'query_count': len(root['queries']), 'unavailable_queries': 0, 'sources': outcomes,
            'next_session_error': None}


def read(value, registry, store, *, offset=0, limit=50, entity_id=None):
    with _scope(store) as reader:
        sources = _Sources(registry, reader)
        ref, root = sources.root(value)
        restored = restore_root(ref, root, registry, reader)
        return readout(restored, offset=offset, limit=limit, entity_id=entity_id)


def readout(restored, *, offset=0, limit=50, entity_id=None):
    view = restored['next_session_view']
    return {'status': 'partial-restoration' if restored['unavailable_queries'] else 'restored',
            'reference': restored['reference'], 'query_count': restored['query_count'],
            'unavailable_queries': restored['unavailable_queries'], 'sources': restored['sources'],
            'view': paginate(view, offset=offset, limit=limit, entity_id=entity_id) if view is not None else None,
            'reading_error': restored['next_session_error'], 'original_http_restored': False,
            'execution_diagnostics': 'not-retained', 'source_requests': 0,
            'qualification': 'exact-original-auction-and-dated-raw-fields-not-trading-effect'}
