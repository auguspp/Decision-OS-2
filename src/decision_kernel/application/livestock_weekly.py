"""Collection-day livestock prices and publisher comparisons on the existing store.

No acquisition, weekly-average invention, backfilled observations or issuer mapping.
The extra comparison cells remain part of each exact retained dataset version.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from decimal import Context, Decimal, ROUND_HALF_UP, localcontext

from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import (
    COLUMNS, _change, _clock, _day, _decimal, read_industry,
)
from decision_kernel.domain.records import RecordError, Selection, SourceRef, digest, encoded, identifier, require

FAMILY = 'mara-livestock-weekly'
REPORT_COLUMNS = ('reported_previous', 'reported_year_ago', 'reported_wow_pct', 'reported_yoy_pct')
LIMITS = [
    'COLLECTION_DAY_NOT_WEEK_AVERAGE_OR_COMPANY_TRANSACTION_PRICE',
    'NATIONAL_AND_MAIN_PRODUCING_PROVINCES_ARE_DIFFERENT_POPULATIONS',
    'PUBLISHED_BASES_NOT_INDEPENDENT_OBSERVATIONS_OR_BACKFILLED_WEEKS',
    'ROUNDING_CHECK_USES_DISPLAYED_PRICES_NOT_UNROUNDED_PUBLISHER_INPUTS',
    'REPORTED_YEAR_AGO_HAS_NO_ASSUMED_CALENDAR_DATE_OR_HISTORICAL_PIT',
    'RETAINED_EXTRACT_AND_DECLARED_BASIS_NOT_ORIGINAL_AUTHENTICATION',
    'NO_CROSS_PROVIDER_PRICE_SPLICE_FEED_RATIO_OR_COMPANY_NET_BENEFIT',
]


def _missing(reason):
    return {'status': 'unavailable', 'reason_code': reason}


def _measure(point):
    out = deepcopy(point)
    if point['status'] != 'read':
        return {**out, 'value': None}
    try:
        require(point['metric'] == 'survey-price' and point['unit'] in ('CNY/kg', 'CNY/bird'),
                'LIVESTOCK_METRIC_OR_UNIT')
        require(point['currency'] == 'CNY' and point['frequency'] == 'weekly'
                and point['period_kind'] == 'price-level' and point['assessment'] == 'collection-day'
                and point['price_kind'] == 'survey' and point['market'] == 'spot'
                and point['contract'] == 'not-applicable', 'LIVESTOCK_PRICE_BASIS')
        require(point['period_start'] == point['period_end'], 'LIVESTOCK_COLLECTION_DAY')
        require(all(point[k].casefold() not in ('unknown', 'unspecified', 'not-applicable')
                    for k in ('specification', 'region', 'tax_basis')), 'LIVESTOCK_SCOPE_UNKNOWN')
        require(point['value'] is not None, 'LIVESTOCK_PRICE_MISSING')
        require(_decimal(point['value']) > 0, 'LIVESTOCK_NONPOSITIVE_PRICE')
    except RecordError as exc:
        return {**out, **_missing(str(exc)), 'value': None}
    return out


def _reported(point, reader):
    """An absent/invalid optional comparison never erases a readable current price."""
    if point['status'] != 'read':
        return {**_missing('LIVESTOCK_POINT_UNAVAILABLE'), 'cells': None, 'reference': None}
    ref = SourceRef.from_dict(point['reference'])
    extra = SourceRef(ref.manifest, Selection('rows', ref.selection.start, ref.selection.end,
                                              (*COLUMNS, *REPORT_COLUMNS)))
    try:
        view = reader.read_source(extra)
        require(isinstance(view['data'], list) and len(view['data']) == 1,
                'LIVESTOCK_REPORT_ROW')
        row = view['data'][0]
        # Same manifest and core cells; do not accept an alternate table masquerading as extra fields.
        require(set(row) == set((*COLUMNS, *REPORT_COLUMNS))
                and all(row[k] == point[k] for k in COLUMNS), 'LIVESTOCK_REPORT_DIFFERS')
        cells = {}
        for field in REPORT_COLUMNS:
            value = row[field]
            cells[field] = value or None
            if value:
                number = _decimal(value)
                require(field.endswith('_pct') or number > 0, 'LIVESTOCK_REPORTED_BASE_NONPOSITIVE')
        return {'status': 'read', 'reason_code': None, 'reference': asdict(extra), 'cells': cells}
    except RecordError:
        return {**_missing('LIVESTOCK_REPORT_UNAVAILABLE_OR_INVALID'), 'cells': None, 'reference': asdict(extra)}


def _check(point, base_field, growth_field):
    report = point['reported']
    out = {'current_reference': point.get('reference'), 'reported_reference': report['reference'],
           'base_field': base_field, 'growth_field': growth_field, 'computed_pct': None,
           'rounded_pct': None, 'published_pct': None, 'rounding': '0.1-percentage-point-half-up',
           'publisher_unrounded_inputs_verified': False}
    if point['status'] != 'read' or report['status'] != 'read':
        return {**out, **_missing('LIVESTOCK_REPORT_UNAVAILABLE_OR_INVALID')}
    cells = report['cells']
    out['published_pct'] = cells[growth_field]
    if cells[base_field] is None or cells[growth_field] is None:
        return {**out, **_missing('LIVESTOCK_COMPARISON_CELL_MISSING')}
    with localcontext(Context(prec=128)):
        a, b, published = map(_decimal, (cells[base_field], point['value'], cells[growth_field]))
        value = (b - a) / a * 100
        rounded = value.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
    return {**out, 'computed_pct': format(value, 'f'), 'rounded_pct': format(rounded, 'f'),
            'status': 'matches-displayed-rounding' if rounded == published else 'rounding-difference',
            'reason_code': None if rounded == published else 'LIVESTOCK_DISPLAYED_PRICES_NOT_UNROUNDED_INPUTS'}


def _transition(before, after):
    out = {'from_reference': before.get('reference'), 'to_reference': after.get('reference'),
           'from_date': before.get('period_end'), 'to_date': after.get('period_end'),
           'delta': None, 'percent_change': None, 'elapsed_days': None,
           'published_previous_matches_prior_observation': None}
    if before['status'] != 'read' or after['status'] != 'read':
        return {**out, **_missing('LIVESTOCK_POINT_UNAVAILABLE')}
    days = (_day(after['period_end']) - _day(before['period_end'])).days
    out['elapsed_days'] = days
    old, new = (p['source']['published_at'] for p in (before, after))
    regressed = _clock(new) < _clock(old) if 'T' in old and 'T' in new else _day(new[:10]) < _day(old[:10])
    if regressed:
        return {**out, **_missing('LIVESTOCK_RELEASE_REGRESSED')}
    if days not in (0, 7):
        return {**out, **_missing('LIVESTOCK_NOT_ADJACENT_WEEK')}
    with localcontext(Context(prec=128)):
        change = _change(before, after)
    if days == 7 and change['status'] != 'unavailable' and after['reported']['status'] == 'read':
        base = after['reported']['cells']['reported_previous']
        if base is not None:
            out['published_previous_matches_prior_observation'] = _decimal(base) == _decimal(before['value'])
    return {**out, **change, 'meaning': 'same-day-vintage-not-weekly-change' if days == 0
            else 'seven-day-collection-price-change-not-quantity-or-profit'}


def read_livestock_weekly(reference, store, *, subject_id=None, company_id=None):
    """Read one original industry packet, then scope all auxiliary rows before return."""
    require(subject_id is None or company_id is None, 'INDUSTRY_ONE_QUERY_DIRECTION')
    for selector in (subject_id, company_id):
        if selector is not None:
            identifier(selector)
    with industry_source_scope(store) as reader:
        retained = read_industry(reference, reader)
        entries, rejected, seen = [], [], set()
        for series in retained['series']:
            subject = series['subject']
            if subject['kind'] != 'commodity' or dict(subject['attributes']).get('observation_family') != FAMILY:
                rejected.append({'series_id': series['series_id'], 'reason_code': 'LIVESTOCK_UNCLASSIFIED_SERIES'})
                continue
            require(subject['entity_id'] not in seen, 'LIVESTOCK_DUPLICATE_SUBJECT_SERIES')
            seen.add(subject['entity_id'])
            points = [_measure(p) for p in series['observations']]
            for p in points:
                p['reported'] = _reported(p, reader)
                p['publisher_comparisons'] = {
                    'wow': _check(p, 'reported_previous', 'reported_wow_pct'),
                    'yoy': _check(p, 'reported_year_ago', 'reported_yoy_pct'),
                }
            changes = [_transition(a, b) for a, b in zip(points, points[1:])]
            entries.append({'series_id': series['series_id'], 'subject': deepcopy(subject),
                            'points': points, 'changes': changes, 'latest_change': changes[-1]})
        relations = [deepcopy(r) for r in retained['relations'] if r['subject_id'] in seen]
        invalid_relations = [r['relation_id'] for r in retained['relations'] if r['subject_id'] not in seen]
        points = [p for e in entries for p in e['points']]
        checks = [c for p in points for c in p['publisher_comparisons'].values()]
        counts = {s: sum(c['status'] == s for c in checks)
                  for s in ('matches-displayed-rounding', 'rounding-difference', 'unavailable')}
        unavailable = sum(p['status'] != 'read' for p in points)
        result = {'schema': 'industry-livestock-weekly-v1', 'reference': deepcopy(retained['reference']),
            'as_of': retained['as_of'], 'prepared_at': retained['prepared_at'],
            'scope': {**retained['scope'], 'qualified_series': len(entries), 'price_points': len(points),
                      'unavailable_price_points': unavailable, 'publisher_checks': counts},
            'status': 'partial' if retained['status'] == 'partial' or rejected or invalid_relations
                      or unavailable or counts['unavailable'] else 'read',
            'unclassified_series': rejected, 'unclassified_relations': invalid_relations,
            'limitations': list(LIMITS), 'retained': retained, 'source_requests': 0, 'writes': 0,
            'company_benefit': 'not-assessed', 'research_executed': False}
        result['view_version'] = digest(encoded({**result, 'entries': entries, 'relations': relations}))
        selected = None
        if company_id is not None:
            relations = [r for r in relations if r['company']['entity_id'] == company_id]
            selected = {r['subject_id'] for r in relations}
        elif subject_id is not None:
            selected = {subject_id}
            relations = [r for r in relations if r['subject_id'] == subject_id]
        result.update(entries=[e for e in entries if selected is None or e['subject']['entity_id'] in selected],
                      relations=relations, query={'subject_id': subject_id, 'company_id': company_id})
    return result  # Every native access/scope-exit check has completed before exporting.
