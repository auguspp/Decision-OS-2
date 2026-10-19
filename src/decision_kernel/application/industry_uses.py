"""Six named industry questions over exact retained readers; no price algorithm.

The packet selects existing series and records source blocks. It never supplies
prices, company roles or claims that a missing input has become zero exposure.
"""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import _available, _clock, _document, read_industry
from decision_kernel.application.operating_cycle import operating_cycle
from decision_kernel.application.vehicle_flows import vehicle_flows
from decision_kernel.domain.records import (
    RecordError, Selection, SourceRef, StoreUnavailable, checked_text, digest, encoded, identifier, require,
)

SCHEMA = 'industry-uses-v1'
FAMILIES = ('chemicals', 'livestock-feed', 'freight', 'memory', 'energy-cycle', 'new-energy')
VIEWS = ('industry', 'operating-cycle', 'vehicle-flows')
MAX_INPUTS = 8
MAX_SELECTIONS = 32
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024


def _shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), 'INDUSTRY_USES_SHAPE')


def _list(value, maximum, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum,
            'INDUSTRY_USES_BOUND')
    return value


def _whole(value):
    ref = SourceRef.from_dict(value)
    require(ref.selection == Selection(), 'INDUSTRY_WHOLE_PACKET_REQUIRED')
    return ref


def packet_input(value):
    """Validate the entire finite selection before opening any dependency."""
    _shape(value, ('schema', 'prepared_at', 'as_of', 'coverage_note', 'inputs', 'uses'))
    require(value['schema'] == SCHEMA, 'INDUSTRY_USES_SCHEMA')
    require(_clock(value['prepared_at']) <= _clock(value['as_of']),
            'INDUSTRY_PREPARATION_AFTER_CUTOFF')
    checked_text(value['coverage_note'], limit=4096)
    inputs, identities = {}, set()
    for item in _list(value['inputs'], MAX_INPUTS, 1):
        _shape(item, ('input_id', 'reference', 'view'))
        key = identifier(item['input_id'])
        require(key not in inputs, 'INDUSTRY_USES_DUPLICATE_INPUT')
        require(isinstance(item['view'], str) and item['view'] in VIEWS, 'INDUSTRY_USES_VIEW')
        ref = _whole(item['reference'])
        identity = encoded((asdict(ref), item['view']))
        require(identity not in identities, 'INDUSTRY_USES_DUPLICATE_INPUT')
        inputs[key] = item
        identities.add(identity)
    families, use_ids, used_inputs, count = set(), set(), set(), 0
    for use in _list(value['uses'], len(FAMILIES), len(FAMILIES)):
        _shape(use, ('use_id', 'family', 'title', 'question', 'scope_note', 'disposition',
                     'selections', 'evidence', 'missing', 'reopen_when'))
        key = identifier(use['use_id'])
        require(key not in use_ids, 'INDUSTRY_USES_DUPLICATE_USE')
        use_ids.add(key)
        require(isinstance(use['family'], str) and use['family'] in FAMILIES
                and use['family'] not in families, 'INDUSTRY_USES_FAMILIES')
        families.add(use['family'])
        for field in ('title', 'question', 'scope_note'):
            checked_text(use[field], limit=4096)
        require(use['disposition'] in ('selected-evidence', 'source-blocked'),
                'INDUSTRY_USES_DISPOSITION')
        blocked = use['disposition'] == 'source-blocked'
        selections = _list(use['selections'], MAX_INPUTS, 0 if blocked else 1)
        require(not blocked or not selections, 'INDUSTRY_USES_BLOCKED_SELECTION')
        if blocked:
            checked_text(use['reopen_when'], limit=4096)
        else:
            require(use['reopen_when'] is None, 'INDUSTRY_USES_REOPEN_NOT_BLOCKED')
        local_inputs = set()
        for selection in selections:
            _shape(selection, ('input_id', 'series_ids'))
            input_id = identifier(selection['input_id'])
            require(input_id in inputs and input_id not in local_inputs, 'INDUSTRY_USES_INPUT_ID')
            local_inputs.add(input_id)
            used_inputs.add(input_id)
            ids = [identifier(s) for s in _list(selection['series_ids'], MAX_SELECTIONS, 1)]
            require(len(ids) == len(set(ids)), 'INDUSTRY_USES_DUPLICATE_SERIES')
            count += len(ids)
        for evidence in _list(use['evidence'], 4, 1 if blocked else 0):
            fields = ('reference', 'quote', 'note')
            require(isinstance(evidence, dict), 'INDUSTRY_USES_SHAPE')
            _shape(evidence, (*fields, 'record_id') if 'record_id' in evidence else fields)
            ref = SourceRef.from_dict(evidence['reference'])
            if 'record_id' in evidence:
                identifier(evidence['record_id'])
                require(ref.selection == Selection(), 'INDUSTRY_WHOLE_PACKET_REQUIRED')
            else:
                require(ref.selection.kind == 'lines', 'INDUSTRY_USES_EVIDENCE_LINES')
            checked_text(evidence['quote'], limit=4096)
            checked_text(evidence['note'], limit=4096)
        for missing in _list(use['missing'], 16, 1):
            checked_text(missing, limit=4096)
    require(families == set(FAMILIES), 'INDUSTRY_USES_FAMILIES')
    require(used_inputs == set(inputs), 'INDUSTRY_USES_UNUSED_INPUT')
    require(count <= MAX_SELECTIONS, 'INDUSTRY_USES_SELECTION_BOUND')
    return deepcopy(value)


def _entries(result, view):
    if view == 'industry':
        return result['series']
    if view == 'vehicle-flows':
        return result['entries']
    return [{**metric, 'metric': name, 'subject': entry['subject']}
            for entry in result['entries'] for name, metric in entry['metrics'].items()]


def _unavailable_points(series):
    return [{'series_id': item['series_id'], 'observation_index': index,
             'reason_code': point.get('reason_code'), 'reference': deepcopy(point.get('reference'))}
            for item in series
            for index, point in enumerate(item.get('observations', item.get('points', [])))
            if point['status'] == 'unavailable']


def _read_input(item, reader, prepared, cutoff):
    retained = read_industry(_whole(item['reference']), reader)
    require(_clock(retained['prepared_at']) <= prepared
            and _clock(retained['as_of']) <= cutoff, 'INDUSTRY_USES_INPUT_AFTER_CUTOFF')
    if item['view'] == 'vehicle-flows':
        result = vehicle_flows(retained)
    elif item['view'] == 'operating-cycle':
        result = operating_cycle(retained)
    else:
        result = retained
    entries = _entries(result, item['view'])
    diagnostics = {key: deepcopy(result[key]) for key in
                   ('unclassified_series', 'unclassified_relations', 'coverage', 'limitations')
                   if key in result}
    return {'result': result, 'series': {s['series_id']: s for s in entries},
            'summary': {**deepcopy(item), 'status': result['status'], 'as_of': result['as_of'],
                        'prepared_at': retained['prepared_at'], 'scope': deepcopy(result['scope']),
                        'retained_scope': deepcopy(retained['scope']),
                        'relation_outcomes': deepcopy(retained['relation_outcomes']),
                        'unavailable_observations': _unavailable_points(retained['series']),
                        'unavailable_view_points': _unavailable_points(entries),
                        'coverage_note': retained['coverage_note'], 'use': retained['use'],
                        'diagnostics': diagnostics}}


def _evidence(item, reader, prepared):
    ref = SourceRef.from_dict(item['reference'])
    view = reader.read_source(ref)
    require(view['manifest']['input']['kind'] == 'content' and isinstance(view['data'], str),
            'INDUSTRY_USES_EVIDENCE_CONTENT')
    text, selected = view['data'], {}
    if 'record_id' in item:
        inventory = _document(text)
        require(isinstance(inventory, dict)
                and inventory.get('schema') == 'source-qualification-inventory-part-v1',
                'INDUSTRY_USES_QUALIFICATION_SCHEMA')
        records = _list(inventory.get('records'), 128)
        matches = [r for r in records if isinstance(r, dict) and r.get('source_id') == item['record_id']]
        require(len(matches) == 1, 'INDUSTRY_USES_QUALIFICATION_RECORD')
        selected = {'record': deepcopy(matches[0])}
        text = encoded(matches[0]).decode('utf-8')
    require(item['quote'] in text, 'INDUSTRY_USES_EVIDENCE_QUOTE')
    require(len(text.encode('utf-8')) <= 32 * 1024, 'INDUSTRY_USES_EVIDENCE_BOUND')
    _available(view['manifest']['input'], prepared)
    return {**deepcopy(item), **selected, 'status': 'read', 'text': text,
            'source': deepcopy(view['manifest']['input']), 'limitations': deepcopy(view['limitations']),
            'original_verified': False,
            'qualification': 'retained-curator-evidence-not-independent-rights-or-original-authentication'}


def read_industry_uses(reference, store, *, subject_id=None, company_id=None):
    """Read all declarations before filtering; source scope exits precede return.

    Original readers own numbers, comparisons and issuer roles. No recursive use
    packets, URLs, new source client, new budget or caller-supplied values exist.
    """
    require(subject_id is None or company_id is None, 'INDUSTRY_ONE_QUERY_DIRECTION')
    for selector in (subject_id, company_id):
        if selector is not None:
            identifier(selector)
    ref = reference if isinstance(reference, SourceRef) else _whole(reference)
    require(ref.selection == Selection(), 'INDUSTRY_WHOLE_PACKET_REQUIRED')
    with industry_source_scope(store) as scoped:
        total_bytes, cache, fatal_error = 0, {}, None
        class Reader:
            def read_source(self, source_ref):
                nonlocal total_bytes, fatal_error
                if fatal_error is not None:
                    raise fatal_error
                key = encoded(asdict(source_ref))
                if key not in cache:
                    try:
                        value = scoped.read_source(source_ref)
                    except RecordError as exc:
                        if isinstance(exc, StoreUnavailable) or str(exc) in (
                            'PRIVATE_BINDING_MISMATCH', 'PUBLIC_BINDING_MISMATCH',
                            'PRIVATE_UPGRADE_REVERSED', 'OPERATING_SOURCE_SCOPE_BOUND'):
                            fatal_error = exc
                        raise
                    total_bytes += len(encoded(value))
                    if total_bytes > MAX_READ_BYTES:
                        fatal_error = RecordError('INDUSTRY_USES_READING_BOUND')
                        raise fatal_error
                    cache[key] = value
                return deepcopy(cache[key])
        reader = Reader()
        root = reader.read_source(ref)
        require(root['manifest']['input']['kind'] == 'content' and isinstance(root['data'], str),
                'INDUSTRY_USES_PACKET_REQUIRED')
        body = packet_input(_document(root['data']))
        prepared, cutoff = _clock(body['prepared_at']), _clock(body['as_of'])
        inputs, summaries = {}, []
        for item in body['inputs']:
            try:
                opened = _read_input(item, reader, prepared, cutoff)
                inputs[item['input_id']] = opened
                summaries.append(opened['summary'])
            except RecordError as exc:
                summaries.append({**deepcopy(item), 'status': 'unavailable', 'reason_code': str(exc)})
            # Original readers retain point failures. An operation-wide budget
            # or access error must still escape even if a child caught it.
            if fatal_error is not None:
                raise fatal_error
        uses, selected_keys, selected_count, failed = [], set(), 0, 0
        for declaration in body['uses']:
            use = deepcopy(declaration)
            use['series'], use['relations'], use['selection_outcomes'], use['evidence'] = [], [], [], []
            relation_keys, relevant_keys = set(), set()
            for evidence in declaration['evidence']:
                try:
                    use['evidence'].append(_evidence(evidence, reader, prepared))
                except RecordError as exc:
                    use['evidence'].append({**deepcopy(evidence), 'status': 'unavailable',
                                            'reason_code': str(exc)})
                if fatal_error is not None:
                    raise fatal_error
            for selection in declaration['selections']:
                input_id = selection['input_id']
                opened = inputs.get(input_id)
                for series_id in selection['series_ids']:
                    key = (input_id, series_id)
                    selected_keys.add(key)
                    series = opened['series'].get(series_id) if opened else None
                    outcome = {'input_id': input_id, 'series_id': series_id}
                    if series is None:
                        use['selection_outcomes'].append({**outcome, 'status': 'unavailable',
                            'reason_code': 'INDUSTRY_USES_SERIES_NOT_FOUND' if opened else
                                           'INDUSTRY_USES_INPUT_UNAVAILABLE'})
                        continue
                    relevant_keys.add(key)
                    points = series.get('observations', series.get('points', []))
                    available = bool(points) and any(p['status'] == 'read' for p in points)
                    use['selection_outcomes'].append({**outcome, 'status': 'read' if available else 'unavailable',
                        'reason_code': None if available else 'INDUSTRY_USES_NO_READABLE_POINTS'})
                    subject = series['subject']['entity_id']
                    roles = [r for r in opened['result'].get('relations', []) if r['subject_id'] == subject]
                    use['series'].append({**deepcopy(series), 'input_id': input_id,
                        'input_view': opened['summary']['view'],
                        'packet_reference': deepcopy(opened['summary']['reference'])})
                    for role in roles:
                        role_key = (input_id, role['relation_id'])
                        if role_key not in relation_keys:
                            use['relations'].append({**deepcopy(role), 'input_id': input_id,
                                                     'packet_reference': deepcopy(opened['summary']['reference'])})
                            relation_keys.add(role_key)
            bad = sum(x['status'] == 'unavailable' for x in use['selection_outcomes'] + use['evidence'])
            partial_inputs = any(inputs[s['input_id']]['summary']['status'] == 'partial'
                                 for s in declaration['selections'] if s['input_id'] in inputs)
            use['status'] = ('source-blocked' if declaration['disposition'] == 'source-blocked' else
                             'partial' if bad or partial_inputs else 'read')
            use['scope'] = {'declared_selections': sum(len(s['series_ids']) for s in declaration['selections']),
                            'resolved_selections': len(relevant_keys), 'unavailable_inputs': bad}
            selected_count += len(relevant_keys)
            failed += bad
            uses.append(use)
        scope = {'declared_uses': len(uses), 'source_blocked_uses': sum(u['status'] == 'source-blocked' for u in uses),
                 'read_uses': sum(u['status'] == 'read' for u in uses),
                 'partial_uses': sum(u['status'] == 'partial' for u in uses),
                 'input_packets': len(summaries), 'unique_selected_series': len(selected_keys),
                 'resolved_selections': selected_count, 'unavailable_inputs': failed}
        result = {'schema': 'industry-uses-result-v1', 'reference': asdict(ref),
                  'as_of': body['as_of'], 'prepared_at': body['prepared_at'],
                  'coverage_note': body['coverage_note'], 'scope': scope, 'uses': uses,
                  'input_outcomes': summaries, 'query': {'subject_id': None, 'company_id': None},
                  'status': 'partial' if scope['source_blocked_uses'] or scope['partial_uses'] else 'read',
                  'source_requests': 0, 'writes': 0, 'research_executed': False,
                  'company_benefit': 'not-assessed',
                  'limitations': ['NAMED_DECLARED_USES_NOT_COMPLETE_INDUSTRY_COVERAGE',
                      'SELECTED_SERIES_KEEP_ORIGINAL_PERIODS_COMPARISONS_AND_WHOLE_PACKET_SCOPE',
                      'SOURCE_BLOCK_AND_MISSING_DIMENSIONS_REMAIN_VISIBLE_AFTER_FILTERING',
                      'NO_CROSS_SERIES_PRICE_PROFIT_STOCK_FLOW_OR_NET_EXPOSURE_ARITHMETIC',
                      'RETAINED_ROLE_IS_NOT_CURRENT_CONTRACT_OR_INDEPENDENT_AUTHENTICATION',
                      'READING_IS_NOT_RESEARCH_BRIEF_ADOPTION_OR_HUMAN_ACCEPTANCE']}
        result['view_version'] = digest(encoded(result))
        require(len(encoded(result)) <= MAX_RESULT_BYTES, 'INDUSTRY_USES_RESULT_BOUND')
        for use in uses:
            if company_id is not None:
                use['relations'] = [r for r in use['relations'] if r['company']['entity_id'] == company_id]
                keys = {(r['input_id'], r['subject_id']) for r in use['relations']}
                use['series'] = [s for s in use['series'] if (s['input_id'], s['subject']['entity_id']) in keys]
            elif subject_id is not None:
                use['relations'] = [r for r in use['relations'] if r['subject_id'] == subject_id]
                use['series'] = [s for s in use['series'] if s['subject']['entity_id'] == subject_id]
        result['query'] = {'subject_id': subject_id, 'company_id': company_id}
    return result
