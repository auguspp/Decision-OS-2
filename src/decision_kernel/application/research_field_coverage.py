"""Bounded coverage of authenticated retained projections, never a merged financial table.

Called only after the original receipt reader and inspect_result. Fields being
returned (including null) is distinct from row agreement, field meaning and use.
"""
from decision_kernel.domain.records import digest, encoded, require, checked_text
from decision_kernel.observations.relay_financial import STATEMENT
from decision_kernel.application.research_source_recovery import attempt_key, order, _scope, _selector

MAX_PARTS = 8
MAX_ROWS = 128
MAX_CELLS = 4096
MAX_BYTES = 256 * 1024
STATEMENTS = {'cashflow', 'income', 'balancesheet'}


def compose_fields(item, by_attempt, current, captures):
    """One failed query, multiple explicit successors; inputs are NOT caller proof."""
    require(set(item) == {'from', 'parts', 'reason'}, 'SOURCE_COMPOSITION_INPUT')
    checked_text(item['reason'], limit=4096)
    old_key = _selector(item['from'])
    require(old_key in by_attempt, 'SOURCE_COMPOSITION_ORIGINAL_MISSING')
    old = by_attempt[old_key]
    key = (old['case_id'], old['query_key'])
    require(old['state'] not in ('complete', 'planned-not-captured'), 'SOURCE_COMPOSITION_NOT_FAILED')
    require(isinstance(item['parts'], list) and 2 <= len(item['parts']) <= MAX_PARTS,
            'SOURCE_COMPOSITION_PART_LIMIT')
    old_fields = set(old['parameters']['fields'].split(','))
    parts, raw_parts, mappings, seen = [], [], {}, set()
    for part in item['parts']:
        require(isinstance(part, dict) and {'to'} <= set(part) <= {'to', 'field_map'}, 'SOURCE_COMPOSITION_PART')
        target = _selector(part['to'])
        require(target in by_attempt and target in captures and target not in seen,
                'SOURCE_COMPOSITION_TARGET_MISSING_OR_DUPLICATE')
        seen.add(target)
        new = by_attempt[target]
        require(new['state'] == 'complete' and _scope(new) == _scope(old), 'SOURCE_COMPOSITION_SCOPE')
        latest_old = by_attempt[attempt_key(current[key]['selected_attempt'])]
        latest_new = current[(new['case_id'], new['query_key'])]['selected_attempt']
        require(order(new) > order(old) and order(new) >= order(latest_old)
                and attempt_key(latest_new) == target, 'SOURCE_COMPOSITION_STALE_TARGET')
        retained = captures[target]
        require(retained['case_id'] == new['case_id'] and retained['use']['api'] == new['api']
                and retained['use']['parameters'] == new['parameters']
                and retained['use']['query_key'] == new['query_key']
                and retained['capture']['api'] == new['api']
                and retained['capture']['parameters'] == new['parameters']
                and retained['capture']['query_key'] == new['query_key']
                and retained['capture']['complete'] is True, 'SOURCE_COMPOSITION_CAPTURE_MISMATCH')
        fields = set(new['parameters']['fields'].split(','))
        mapping = part.get('field_map', {})
        require(isinstance(mapping, dict) and all(isinstance(a, str) and isinstance(b, str) for a,b in mapping.items())
                and set(mapping) <= old_fields - fields and set(mapping.values()) <= fields
                and len(set(mapping.values())) == len(mapping), 'SOURCE_COMPOSITION_FIELD_MAP')
        for a,b in mapping.items():
            require(a not in mappings or mappings[a] == b, 'SOURCE_COMPOSITION_AMBIGUOUS_MAP')
            mappings[a] = b
        require(len(set(mappings.values())) == len(mappings), 'SOURCE_COMPOSITION_AMBIGUOUS_MAP')
        pages = retained['capture']['pages']
        require(sum(len(p['table']['items']) for p in pages) <= MAX_ROWS, 'SOURCE_COMPOSITION_ROW_LIMIT')
        raw_parts.append(pages)
        parts.append({'to': dict(part['to']), 'field_map': dict(mapping),
                      'query_key': new['query_key'], 'capture_sha256': digest(encoded(retained['capture']))})
    # Alias targets must not also discharge a different original same-name need.
    require(not (set(mappings.values()) & old_fields), 'SOURCE_COMPOSITION_ALIAS_COLLISION')
    all_columns = {f for pages in raw_parts for p in pages for f in p['table']['fields']}
    policy = 'statement-returned-identity-v1' if old['api'] in STATEMENTS else 'unavailable'
    identity_fields = sorted(set(STATEMENT) - {'comp_type'} | ({'comp_type'} if 'comp_type' in all_columns else set()))
    issues = set() if policy != 'unavailable' else {'ROW_IDENTITY_POLICY_UNAVAILABLE'}
    coverage = {f: [] for f in sorted(old_fields)}
    cohorts, populations, cells, compared = {}, [], 0, 0
    for part_index, (part, pages) in enumerate(zip(parts, raw_parts)):
        population, row_count = set(), 0
        for page_index, page in enumerate(pages):
            fields, rows = page['table']['fields'], page['table']['items']
            # inspect_result already checked projection width/types and native paging.
            require(len(fields) == len(set(fields)) and all(len(row) == len(fields) for row in rows),
                    'SOURCE_COMPOSITION_TABLE')
            mapped = {f: part['field_map'].get(f, f) for f in old_fields}
            present = {f: source for f,source in mapped.items() if source in fields}
            for row_index, values in enumerate(rows):
                compared += len(values)
                require(compared <= MAX_CELLS, 'SOURCE_COMPOSITION_COMPARISON_LIMIT')
                data = dict(zip(fields, values)); row_count += 1
                identity = {k: data[k] for k in identity_fields if k in data}
                valid = policy != 'unavailable' and all(isinstance(data.get(k), str) and data[k] for k in identity_fields)
                for name, field in [('period','end_date'), *[(k,k) for k in identity_fields]]:
                    if name in old['parameters'] and field in data and str(data[field]) not in old['parameters'][name].split(','):
                        valid = False
                        issues.add('ROW_VALUES_OUTSIDE_QUERY_SCOPE')
                if not valid:
                    issues.add('ROW_IDENTITY_INCOMPLETE')
                cohort = digest(encoded(identity)) if valid else None
                locator = {'part_index': part_index, 'page_index': page_index, 'row_index': row_index,
                           'captured_at': page['captured_at'], 'identity': identity, 'cohort_sha256': cohort}
                if cohort is not None:
                    if cohort in population:
                        issues.add('ROW_IDENTITY_AMBIGUOUS')
                    population.add(cohort)
                    group = cohorts.setdefault(cohort, {'identity': identity, 'rows': [], 'fields': set(), 'values': {}})
                    group['rows'].append(locator)
                    group['fields'].update(present)
                    # Check all overlapping raw columns, plus declared aliases. No rounding,
                    # latest-nonnull fill, or replacement of a conflicting retained value.
                    effective = {**data, **{a: data[b] for a,b in part['field_map'].items() if b in data}}
                    for f,v in effective.items():
                        group['values'].setdefault(f, set()).add(encoded(v))
                for f, source in present.items():
                    cells += 1
                    require(cells <= MAX_CELLS, 'SOURCE_COMPOSITION_CELL_LIMIT')
                    v = data[source]
                    coverage[f].append({**locator, 'returned_field': source,
                                        'value_state': 'null' if v is None else 'empty-string' if v == '' else 'value'})
        part['row_count'] = row_count
        populations.append(population)
        if not row_count:
            issues.add('EMPTY_CAPTURE_NO_ROW_EVIDENCE')
    if any(p != populations[0] for p in populations[1:]):
        issues.add('ROW_POPULATIONS_DIFFER')
    cohort_results = []
    for cohort, group in sorted(cohorts.items()):
        conflicts = sorted(f for f,v in group['values'].items() if len(v) > 1)
        missing = sorted(old_fields - group['fields'])
        if conflicts:
            issues.add('OVERLAPPING_VALUES_CONFLICT')
        if missing:
            issues.add('COHORT_FIELDS_INCOMPLETE')
        cohort_results.append({'cohort_sha256': cohort, 'identity': group['identity'], 'rows': group['rows'],
                               'missing_fields': missing, 'conflicting_fields': conflicts})
    missing = [f for f, observations in coverage.items() if not observations]
    aligned = bool(cohort_results) and not issues
    state = ('partial-recovery' if missing else 'composition-requires-review' if not aligned
             else 'complete-corrected-fields' if mappings else 'complete')
    result = {'from': dict(item['from']), 'reason': item['reason'], 'parts': parts,
              'state': state, 'missing_fields': missing, 'field_coverage': coverage,
              'field_coverage_complete': not missing, 'cohorts': cohort_results,
              'row_consistency': 'aligned-retained-rows' if aligned else 'requires-review',
              'row_issues': sorted(issues), 'identity_policy': policy, 'identity_fields': identity_fields,
              'field_semantics': 'requires-review' if mappings else 'same-named-fields-only',
              'null_policy': 'returned-null-is-not-zero-or-missing-column',
              'simultaneous_snapshot': 'not-established', 'merged_table': 'not-created',
              'economic_assessment': 'not-performed'}
    require(len(encoded(result)) <= MAX_BYTES, 'SOURCE_COMPOSITION_RESULT_LIMIT')
    return result, key
