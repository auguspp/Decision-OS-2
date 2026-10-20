"""Finite, evidenced goods qualification over exact existing industry readers.

The declaration supplies identity/source curation, never prices or a qualified
flag. Retention verifies references and bindings, not the curator's semantics or
third-party rights. This reader has no collector, writer, or global registry.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import date, timedelta, timezone

from decision_kernel.application.brief_industry import version_id
from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import BASIS, _change, _clock, _document
from decision_kernel.application.industry_uses import _evidence, _read_input
from decision_kernel.domain.records import (
    RecordError, Selection, SourceRef, StoreUnavailable, checked_text, digest, encoded, identifier, require,
)

SCHEMA = 'industry-goods-v1'
MAX_INPUTS = 8
MAX_CANDIDATES = MAX_SELECTIONS = MAX_SOURCES = 32
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
DISPOSITIONS = ('physical-good', 'statistical-aggregate', 'service', 'identity-unresolved', 'source-blocked')
RIGHTS = ('retained-use-permitted', 'restricted', 'unknown')
TERMINAL_CODES = ('PRIVATE_BINDING_MISMATCH', 'PUBLIC_BINDING_MISMATCH',
                  'PRIVATE_UPGRADE_REVERSED', 'OPERATING_SOURCE_SCOPE_BOUND',
                  'READING_SCOPE_CLOSED', 'SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED')


def _shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), 'INDUSTRY_GOODS_SHAPE')


def _list(value, maximum, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum, 'INDUSTRY_GOODS_BOUND')
    return value


def _whole(value):
    ref = value if isinstance(value, SourceRef) else SourceRef.from_dict(value)
    require(ref.selection == Selection(), 'INDUSTRY_WHOLE_PACKET_REQUIRED')
    return ref


def _proofs(value):
    for proof in _list(value, 4, 1):
        fields = ('reference', 'quote', 'note')
        require(isinstance(proof, dict), 'INDUSTRY_GOODS_SHAPE')
        _shape(proof, (*fields, 'record_id') if 'record_id' in proof else fields)
        ref = SourceRef.from_dict(proof['reference'])
        if 'record_id' in proof:
            identifier(proof['record_id'])
            require(ref.selection == Selection(), 'INDUSTRY_WHOLE_PACKET_REQUIRED')
        else:
            require(ref.selection.kind == 'lines', 'INDUSTRY_GOODS_EVIDENCE_LINES')
        checked_text(proof['quote'], limit=4096)
        checked_text(proof['note'], limit=4096)


def packet_input(value):
    """Check the whole finite declaration before opening any dependency."""
    _shape(value, ('schema', 'prepared_at', 'as_of', 'coverage_note', 'inputs', 'sources', 'candidates'))
    require(value['schema'] == SCHEMA, 'INDUSTRY_GOODS_SCHEMA')
    require(_clock(value['prepared_at']) <= _clock(value['as_of']), 'INDUSTRY_PREPARATION_AFTER_CUTOFF')
    checked_text(value['coverage_note'], limit=4096)
    inputs, packets = {}, set()
    for item in _list(value['inputs'], MAX_INPUTS):
        _shape(item, ('input_id', 'reference', 'view'))
        key = identifier(item['input_id'])
        require(item['view'] == 'industry', 'INDUSTRY_GOODS_ORIGINAL_VIEW_REQUIRED')
        ref = _whole(item['reference'])
        # Same reachable packet at another commit does not become another input.
        identity = encoded([ref.manifest.repository, ref.manifest.path, ref.manifest.sha256])
        require(key not in inputs and identity not in packets, 'INDUSTRY_GOODS_DUPLICATE_INPUT')
        inputs[key] = item
        packets.add(identity)
    sources, families = set(), {}
    for source in _list(value['sources'], MAX_SOURCES):
        _shape(source, ('source_id', 'origin', 'family_id', 'rights', 'freshness', 'evidence', 'reopen_when'))
        identifier(source['source_id'])
        checked_text(source['origin'], limit=4096)
        family = identifier(source['family_id'])
        key = (source['source_id'], source['origin'])
        require(key not in sources, 'INDUSTRY_GOODS_DUPLICATE_SOURCE')
        sources.add(key)
        require(source['rights'] in RIGHTS, 'INDUSTRY_GOODS_RIGHTS')
        if source['rights'] != 'retained-use-permitted':
            checked_text(source['reopen_when'], limit=4096)
        else:
            require(source['reopen_when'] is None, 'INDUSTRY_GOODS_REOPEN')
        rule = source['freshness']
        if rule is not None:
            _shape(rule, ('max_period_age_days', 'utc_offset_minutes'))
            require(type(rule['max_period_age_days']) is int and 1 <= rule['max_period_age_days'] <= 20
                    and type(rule['utc_offset_minutes']) is int
                    and -720 <= rule['utc_offset_minutes'] <= 840, 'INDUSTRY_GOODS_FRESHNESS_RULE')
            if source['source_id'] == 'nbs-material-prices':
                require(rule['utc_offset_minutes'] == 480, 'INDUSTRY_GOODS_NBS_CLOCK')
        # Mirrors of one upstream cannot silently adopt looser clocks.
        require(family not in families or families[family] == rule, 'INDUSTRY_GOODS_FAMILY_CLOCK_CONFLICT')
        families[family] = rule
        _proofs(source['evidence'])
    ids, used_inputs, count = set(), set(), 0
    for candidate in _list(value['candidates'], MAX_CANDIDATES, 1):
        _shape(candidate, ('candidate_id', 'title', 'subject_id', 'commodity_id', 'disposition',
                           'identity_evidence', 'selections', 'missing', 'reopen_when'))
        key = identifier(candidate['candidate_id'])
        require(key not in ids, 'INDUSTRY_GOODS_DUPLICATE_CANDIDATE')
        ids.add(key)
        checked_text(candidate['title'], limit=1024)
        require(candidate['disposition'] in DISPOSITIONS, 'INDUSTRY_GOODS_DISPOSITION')
        physical = candidate['disposition'] == 'physical-good'
        if physical:
            identifier(candidate['subject_id'])
            identifier(candidate['commodity_id'])
        else:
            require(candidate['commodity_id'] is None, 'INDUSTRY_GOODS_EXCLUDED_ID')
            if candidate['subject_id'] is not None:
                identifier(candidate['subject_id'])
        blocked = candidate['disposition'] == 'source-blocked'
        selections = _list(candidate['selections'], MAX_SELECTIONS, 0 if blocked else 1)
        require(not blocked or not selections, 'INDUSTRY_GOODS_BLOCKED_SELECTION')
        local = set()
        for selection in selections:
            _shape(selection, ('input_id', 'series_id'))
            input_id, series_id = identifier(selection['input_id']), identifier(selection['series_id'])
            require(input_id in inputs and (input_id, series_id) not in local, 'INDUSTRY_GOODS_SELECTION')
            local.add((input_id, series_id))
            used_inputs.add(input_id)
            count += 1
        _proofs(candidate['identity_evidence'])
        for note in _list(candidate['missing'], 16, 1):
            checked_text(note, limit=4096)
        if candidate['disposition'] in ('identity-unresolved', 'source-blocked'):
            checked_text(candidate['reopen_when'], limit=4096)
        else:
            require(candidate['reopen_when'] is None, 'INDUSTRY_GOODS_REOPEN')
    require(used_inputs == set(inputs), 'INDUSTRY_GOODS_UNUSED_INPUT')
    require(count <= MAX_SELECTIONS, 'INDUSTRY_GOODS_SELECTION_BOUND')
    return deepcopy(value)


def _terminal(error):
    if isinstance(error, StoreUnavailable):
        return error
    if str(error) in TERMINAL_CODES:
        return error
    return None


def _proof_results(proofs, reader, prepared, *, source_id=None):
    results = []
    for proof in proofs:
        try:
            result = _evidence(proof, reader, prepared)
            if source_id is not None and 'record_id' in proof:
                require(proof['record_id'] == source_id and result['record']['source_id'] == source_id,
                        'INDUSTRY_GOODS_SOURCE_EVIDENCE_BINDING')
            results.append(result)
        except RecordError as error:
            if _terminal(error):
                raise _terminal(error)
            results.append({**deepcopy(proof), 'status': 'unavailable', 'reason_code': str(error)})
    return results


def _baseline(series):
    """Reuse the original comparator; adjacent revisions add no observed period.

    Collapse only contiguous revisions of the final period. Never skip a
    missing point or an intervening rejected comparison to find a good value.
    """
    points = series['observations']
    last = len(points) - 1
    current = points[last]
    if current['status'] != 'read':
        return None, {'status': 'unavailable', 'reason_code': 'INDUSTRY_POINT_UNAVAILABLE'}
    previous = last - 1
    while previous >= 0:
        point = points[previous]
        if point['status'] != 'read':
            break
        same = (point['period_start'], point['period_end']) == (current['period_start'], current['period_end'])
        if not same:
            break
        revision = _change(point, points[previous + 1])
        if revision['status'] == 'unavailable':
            return None, revision
        previous -= 1
    if previous < 0:
        return None, {'status': 'unavailable', 'reason_code': 'INDUSTRY_GOODS_TWO_PERIODS_REQUIRED'}
    comparison = _change(points[previous], current)
    if comparison['status'] != 'unavailable' and current['source']['source_id'] == 'nbs-material-prices':
        adjacent = date.fromisoformat(points[previous]['period_end']) + timedelta(days=1)
        if adjacent.isoformat() != current['period_start']:
            return None, {'status': 'unavailable', 'reason_code': 'INDUSTRY_GOODS_NBS_PERIOD_GAP'}
    return (previous, last), comparison


def _selection(selection, inputs, sources, now):
    opened = inputs.get(selection['input_id'])
    series = opened['series'].get(selection['series_id']) if opened else None
    out = {**deepcopy(selection), 'status': 'unavailable', 'reasons': [], 'series': deepcopy(series),
           'comparable': False, 'qualified_series': False, 'fresh': None, 'freshness': None,
           'source_family': None, 'series_key': None, 'packet_reference': None}
    if series is None:
        out['reasons'].append('INDUSTRY_GOODS_SERIES_UNAVAILABLE')
        return out
    ref = opened['summary']['reference']
    out.update(status='read', packet_reference=deepcopy(ref),
               series_key=version_id({'reference': ref, 'view': 'industry', 'series_id': series['series_id']}))
    pair, comparison = _baseline(series)
    out['comparison'] = {'from_index': pair[0] if pair else None,
                         'to_index': pair[1] if pair else None, **comparison}
    out['comparable'] = comparison['status'] != 'unavailable'
    if not out['comparable']:
        out['reasons'].append(comparison['reason_code'])
    point = series['observations'][-1]
    if point['status'] != 'read':
        return out
    # Commodity labels cannot turn an index or an explicitly forecast value
    # into an observed physical good. Other semantics remain evidenced curation.
    forecast = any(word in point['assessment'].casefold()
                   for word in ('forecast', 'projection', 'prediction', 'scenario'))
    observation = point['period_kind'] != 'index-level' and point['market'] != 'futures' and not forecast
    if not observation:
        out['reasons'].append('INDUSTRY_GOODS_NOT_OBSERVED_PHYSICAL_MEASURE')
    source = sources.get((point['source']['source_id'], point['source']['origin']))
    if source is None:
        out['reasons'].append('INDUSTRY_GOODS_SOURCE_QUALIFICATION_MISSING')
        return out
    out['source_family'] = source['family_id']
    out['source_qualification'] = {key: deepcopy(source[key]) for key in
                                  ('source_id', 'origin', 'family_id', 'rights', 'reopen_when', 'status')}
    if source['status'] != 'read':
        out['reasons'].append('INDUSTRY_GOODS_SOURCE_EVIDENCE_UNAVAILABLE')
    if source['rights'] != 'retained-use-permitted':
        out['reasons'].append('INDUSTRY_GOODS_RIGHTS_' + source['rights'].upper())
    out['qualified_series'] = (out['comparable'] and observation and source['status'] == 'read'
                               and source['rights'] == 'retained-use-permitted')
    rule = source['freshness']
    if rule is None:
        out['reasons'].append('INDUSTRY_GOODS_FRESHNESS_UNKNOWN')
    else:
        local_day = now.astimezone(timezone(timedelta(minutes=rule['utc_offset_minutes']))).date()
        age = (local_day - date.fromisoformat(point['period_end'])).days
        out['freshness'] = {**rule, 'period_end': point['period_end'], 'period_age_days': age,
                            'evaluation_date': local_day.isoformat(),
                            'meaning': 'declared-age-rule-not-proof-of-latest-publication'}
        out['fresh'] = 0 <= age <= rule['max_period_age_days']
        if not out['fresh']:
            out['reasons'].append('INDUSTRY_GOODS_STALE' if age >= 0 else 'INDUSTRY_GOODS_FUTURE_PERIOD')
    return out


def _fact_keys(selection, sources):
    """Two curations cannot turn the same retained/origin fact into two goods."""
    if selection['series'] is None:
        return []
    keys = []
    for point in selection['series']['observations']:
        if point['status'] != 'read':
            continue
        ref = point['reference']
        manifest = ref['manifest']
        keys.append(encoded(['retained-row', manifest['repository'], manifest['path'],
                             manifest['sha256'], ref['selection']]))
        source = sources.get((point['source']['source_id'], point['source']['origin']))
        if source is not None:
            keys.append(encoded(['upstream-fact', source['family_id'],
                                 [point[field] for field in BASIS if field != 'subject_id'],
                                 point['period_start'], point['period_end']]))
    return keys


def _qualify(body, inputs, sources, reader, now):
    entities, conflicts, bound_subjects = {}, set(), set()
    for opened in inputs.values():
        for series in opened['series'].values():
            entity = series['subject']
            key = entity['entity_id']
            if key in entities and entities[key] != entity:
                conflicts.add(key)
            entities[key] = entity
            if any(p['status'] == 'read' for p in series['observations']):
                bound_subjects.add(key)
    entries, owners = [], {}
    for candidate in body['candidates']:
        entry = {**deepcopy(candidate), 'identity_evidence': _proof_results(
            candidate['identity_evidence'], reader, _clock(body['prepared_at'])),
            'selections': [], 'relations': [], 'reasons': [], 'qualified': False, 'fresh': False}
        subject, canonical = candidate['subject_id'], candidate['commodity_id']
        physical = candidate['disposition'] == 'physical-good'
        if not physical:
            entry['reasons'].append('INDUSTRY_GOODS_' + candidate['disposition'].replace('-', '_').upper())
        if any(proof['status'] != 'read' for proof in entry['identity_evidence']):
            entry['reasons'].append('INDUSTRY_GOODS_IDENTITY_EVIDENCE_UNAVAILABLE')
        if physical:
            if subject not in entities or canonical not in entities:
                entry['reasons'].append('INDUSTRY_GOODS_IDENTITY_NOT_IN_RETAINED_INPUTS')
            elif any(entities[key]['kind'] != 'commodity' for key in (subject, canonical)):
                entry['reasons'].append('INDUSTRY_GOODS_NOT_PHYSICAL_SUBJECT')
            if subject in conflicts or canonical in conflicts:
                entry['reasons'].append('INDUSTRY_GOODS_SUBJECT_IDENTITY_CONFLICT')
            if not {subject, canonical} <= bound_subjects:
                entry['reasons'].append('INDUSTRY_GOODS_IDENTITY_NOT_SOURCE_BOUND')
            # Proof metadata must bind both original IDs, not just a similar name.
            bound = {entity['entity_id'] for proof in entry['identity_evidence'] if proof['status'] == 'read'
                     for entity in proof['source']['entities']}
            if not {subject, canonical} <= bound:
                entry['reasons'].append('INDUSTRY_GOODS_IDENTITY_EVIDENCE_BINDING')
        role_ids = set()
        for selection in candidate['selections']:
            outcome = _selection(selection, inputs, sources, now)
            series = outcome['series']
            if series is not None and series['subject']['entity_id'] != subject:
                entry['reasons'].append('INDUSTRY_GOODS_SELECTION_SUBJECT_MISMATCH')
            entry['selections'].append(outcome)
            opened = inputs.get(selection['input_id'])
            if opened:
                for role in opened['result']['relations']:
                    key = (selection['input_id'], role['relation_id'])
                    if role['subject_id'] == subject and key not in role_ids:
                        entry['relations'].append({**deepcopy(role), 'input_id': selection['input_id']})
                        role_ids.add(key)
            if physical:
                for key in _fact_keys(outcome, sources):
                    owners.setdefault(key, set()).add(canonical)
        if physical:
            owners.setdefault(encoded(['subject', subject]), set()).add(canonical)
        entries.append(entry)
    for entry in entries:
        canonical, subject = entry['commodity_id'], entry['subject_id']
        if canonical is not None:
            keys = [encoded(['subject', subject])] + [key for s in entry['selections'] for key in _fact_keys(s, sources)]
            if any(len(owners[key]) > 1 for key in keys):
                entry['reasons'].append('INDUSTRY_GOODS_COMMODITY_IDENTITY_CONFLICT')
            canonical_targets = owners.get(encoded(['subject', canonical]), {canonical})
            if canonical_targets != {canonical}:
                entry['reasons'].append('INDUSTRY_GOODS_CANONICAL_NOT_ROOT')
        entry['identity_resolution'] = {
            'status': ('evidenced-curation' if canonical is not None and not entry['reasons']
                       else 'unresolved' if entry['disposition'] in ('physical-good', 'identity-unresolved')
                       else entry['disposition']),
            'original_subject': deepcopy(entities.get(subject)),
            'canonical_subject': deepcopy(entities.get(canonical)),
            'inheritance': ('same-id' if subject == canonical else 'curated-alias') if canonical else None,
            'reasons': sorted(set(entry['reasons'])), 'original_verified': False}
        usable = [s for s in entry['selections'] if s['qualified_series']]
        entry['qualified'] = not entry['reasons'] and bool(usable)
        entry['fresh'] = (True if entry['qualified'] and any(s['fresh'] is True for s in usable)
                          else None if entry['qualified'] and any(s['fresh'] is None for s in usable)
                          else False)
        if not usable and entry['disposition'] == 'physical-good':
            entry['reasons'].append('INDUSTRY_GOODS_NO_QUALIFIED_SERIES')
        entry['reasons'] = sorted(set(entry['reasons']))
        entry['status'] = ('fresh-qualified' if entry['fresh'] is True
                           else 'qualified-freshness-unknown' if entry['qualified'] and entry['fresh'] is None
                           else 'qualified-not-fresh' if entry['qualified']
                           else 'source-blocked' if entry['disposition'] == 'source-blocked'
                           else 'unresolved' if entry['disposition'] in ('physical-good', 'identity-unresolved')
                           else 'excluded')
    return entries


def read_industry_goods(reference, store, *, evaluation_at, subject_id=None, company_id=None):
    """Read the whole finite batch before filtering; all scope exits precede return."""
    require(subject_id is None or company_id is None, 'INDUSTRY_ONE_QUERY_DIRECTION')
    for selector in (subject_id, company_id):
        if selector is not None:
            identifier(selector)
    now = _clock(evaluation_at)
    ref = _whole(reference)
    try:
        with industry_source_scope(store) as scoped:
            total, cache, fatal = 0, {}, None
            class Reader:
                def read_source(self, source_ref):
                    nonlocal total, fatal
                    if fatal is not None:
                        raise fatal
                    key = encoded(asdict(source_ref))
                    if key not in cache:
                        try:
                            value = scoped.read_source(source_ref)
                        except RecordError as error:
                            fatal = _terminal(error)
                            raise fatal or error
                        total += len(encoded(value))
                        if total > MAX_READ_BYTES:
                            fatal = StoreUnavailable('INDUSTRY_GOODS_READING_BOUND')
                            raise fatal
                        cache[key] = value
                    return deepcopy(cache[key])
            reader = Reader()
            root = reader.read_source(ref)
            require(root['manifest']['input']['kind'] == 'content' and isinstance(root['data'], str),
                    'INDUSTRY_GOODS_PACKET_REQUIRED')
            body = packet_input(_document(root['data']))
            prepared, cutoff = _clock(body['prepared_at']), _clock(body['as_of'])
            require(cutoff <= now, 'INDUSTRY_GOODS_EVALUATION_BEFORE_CUTOFF')
            inputs, summaries, sources = {}, [], {}
            for item in body['inputs']:
                try:
                    opened = _read_input(item, reader, prepared, cutoff)
                    inputs[item['input_id']] = opened
                    summaries.append(opened['summary'])
                except RecordError as error:
                    if _terminal(error):
                        raise _terminal(error)
                    summaries.append({**deepcopy(item), 'status': 'unavailable', 'reason_code': str(error)})
                if fatal is not None:
                    raise fatal
            for source in body['sources']:
                proofs = _proof_results(source['evidence'], reader, prepared, source_id=source['source_id'])
                sources[(source['source_id'], source['origin'])] = {
                    **deepcopy(source), 'evidence': proofs,
                    'status': 'read' if all(p['status'] == 'read' for p in proofs) else 'unavailable'}
            entries = _qualify(body, inputs, sources, reader, now)
            if fatal is not None:
                raise fatal
            comparable = sorted({s['series_key'] for e in entries for s in e['selections'] if s['comparable']})
            goods = sorted({e['commodity_id'] for e in entries if e['qualified']})
            fresh = sorted({e['commodity_id'] for e in entries if e['fresh']})
            unknown_fresh = sorted({e['commodity_id'] for e in entries
                                    if e['qualified'] and e['fresh'] is None} - set(fresh))
            outcomes = [{key: deepcopy(e[key]) for key in
                         ('candidate_id', 'title', 'subject_id', 'commodity_id', 'disposition',
                          'status', 'identity_resolution', 'qualified', 'fresh', 'reasons', 'missing', 'reopen_when')}
                        for e in entries]
            scope = {'input_packets': len(summaries), 'candidate_records': len(entries),
                     'unique_selected_series': len({s['series_key'] for e in entries for s in e['selections']
                                                    if s['series_key'] is not None}),
                     'comparable_series': len(comparable), 'distinct_qualified_goods': len(goods),
                     'fresh_goods': len(fresh), 'unresolved_candidates': sum(e['status'] == 'unresolved' for e in entries),
                     'freshness_unknown_goods': len(unknown_fresh),
                     'excluded_candidates': sum(e['status'] == 'excluded' for e in entries),
                     'source_blocked_candidates': sum(e['status'] == 'source-blocked' for e in entries),
                     'unavailable_input_packets': sum(s['status'] == 'unavailable' for s in summaries),
                     'global_qualified_goods': None, 'global_fresh_goods': None}
            incomplete = (any(e['status'] in ('unresolved', 'source-blocked', 'qualified-not-fresh',
                                              'qualified-freshness-unknown') for e in entries)
                          or any(s['status'] != 'read' for s in summaries)
                          or any(s['status'] != 'read' for s in sources.values())
                          or any(s['reasons'] for e in entries if e['disposition'] == 'physical-good'
                                 for s in e['selections']))
            result = {'schema': 'industry-goods-result-v1', 'reference': asdict(ref),
                      'prepared_at': body['prepared_at'], 'as_of': body['as_of'], 'evaluation_at': evaluation_at,
                      'coverage_note': body['coverage_note'], 'status': 'partial' if incomplete else 'read',
                      'scope': scope, 'sets': {'candidate_ids': [e['candidate_id'] for e in entries],
                          'comparable_series': comparable, 'qualified_goods': goods, 'fresh_goods': fresh,
                          'freshness_unknown_goods': unknown_fresh},
                      'outcomes': outcomes, 'entries': entries, 'input_outcomes': summaries,
                      'source_qualifications': list(sources.values()),
                      'query': {'subject_id': None, 'company_id': None},
                      'quantity_milestone': {'target': 500, 'status': 'not-established',
                                             'reason': 'finite-declared-batch-not-global-coverage'},
                      'qualification': 'evidenced-curation-and-retained-comparison-not-independent-authentication',
                      'limitations': ['IDENTITY_AND_RIGHTS_SEMANTICS_REQUIRE_CURATOR_REVIEW',
                          'RETAINED_USE_EVIDENCE_DOES_NOT_GRANT_PUBLIC_OUTPUT_PERMISSION',
                          'READING_OR_REPACKAGING_DOES_NOT_REFRESH_OBSERVATION_PERIODS',
                          'GLOBAL_COVERAGE_AND_UNREAD_BATCHES_REMAIN_UNKNOWN',
                          'SERIES_REGIONS_STATISTICS_REVISIONS_AND_MIRRORS_ARE_NOT_ADDITIONAL_GOODS',
                          'NO_SOURCE_RETRY_RESEARCH_BRIEF_ADOPTION_OR_HUMAN_ACCEPTANCE'],
                      'source_requests': 0, 'writes': 0, 'research_executed': False}
            result['view_version'] = digest(encoded(result))
            if len(encoded(result)) > MAX_RESULT_BYTES:
                raise StoreUnavailable('INDUSTRY_GOODS_RESULT_BOUND')
            if company_id is not None:
                for entry in entries:
                    entry['relations'] = [r for r in entry['relations'] if r['company']['entity_id'] == company_id]
                    role_inputs = {r['input_id'] for r in entry['relations']}
                    entry['selections'] = [s for s in entry['selections'] if s['input_id'] in role_inputs]
                result['entries'] = [e for e in entries if e['relations']]
            elif subject_id is not None:
                result['entries'] = [e for e in entries if subject_id in (e['subject_id'], e['commodity_id'])]
            result['query'] = {'subject_id': subject_id, 'company_id': company_id}
            if len(encoded(result)) > MAX_RESULT_BYTES:
                raise StoreUnavailable('INDUSTRY_GOODS_RESULT_BOUND')
    except RecordError as error:
        terminal = _terminal(error)
        if terminal is not None:
            raise terminal
        raise
    return result
