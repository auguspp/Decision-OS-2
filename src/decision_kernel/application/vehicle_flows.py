"""Vehicle production and sales channels over exact retained industry inputs.

No channel subtraction, market share, source acquisition or inferred beneficiary.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date
from decimal import Context, localcontext

from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import read_industry, _change, _clock, _day, _decimal
from decision_kernel.domain.records import RecordError, digest, encoded, identifier, require

CHANNELS = {
    'vehicle-production': '生产量；不是交付、销售收入或库存',
    'vehicle-wholesale': '厂商批发；国内外范围按原发布，不等于国内零售',
    'vehicle-domestic-retail': '国内终端零售；不是厂商含出口销量',
    'vehicle-manufacturer-exports': '厂商出口；整车/CKD按原规格，不等于海关或境外终端销量',
}
STAGES = ('monthly-release', 'preliminary-scan', 'revised-release')
LIMITATIONS = (
    'CHANNEL_DIFFERENCES_NOT_INVENTORY_OR_CASH_FLOW',
    'NO_ADDITION_OF_OVERLAPPING_POPULATIONS_OR_MONTHLY_AND_YTD_FLOWS',
    'MONTHLY_RELEASE_NOT_AUDITED_FINAL_OR_SEASONALLY_ADJUSTED',
    'CROSS_VINTAGE_AMOUNTS_NOT_RECONCILED_TO_PUBLISHER_REPORTED_GROWTH_RATES',
    'EXPORTS_NOT_CUSTOMS_COUNTS_OR_OVERSEAS_RETAIL; CKD_SCOPE_RETAINS_SOURCE_SPECIFICATION',
    'NO_MARKET_SHARE_FROM_ISSUER_AND_INDUSTRY_POPULATIONS',
    'RETAINED_ROLE_NOT_AUTHENTICATION_OR_CURRENT_BUSINESS_REVALIDATION',
    'NO_AUTOMATIC_RESEARCH_EXECUTION_OR_COMPANY_NET_BENEFIT',
)


def _measure(point):
    out = deepcopy(point)
    if point['status'] != 'read':
        return {**out, 'value': None}
    try:
        require(point['metric'] in CHANNELS and point['unit'] in ('vehicle', '10k-vehicles'),
                'VEHICLE_METRIC_OR_UNIT')
        require(point['frequency'] == 'monthly' and all(point[k] == 'not-applicable' for k in
                ('currency', 'price_kind', 'market', 'contract', 'tax_basis')), 'VEHICLE_NONMARKET_BASIS')
        require(all(point[k].casefold() not in ('unknown', 'unspecified', 'not-applicable') for k in
                ('specification', 'region')), 'VEHICLE_SCOPE_UNKNOWN')
        require(point['assessment'] in STAGES, 'VEHICLE_RELEASE_STAGE')
        start, end = _day(point['period_start']), _day(point['period_end'])
        require(end.day == monthrange(end.year, end.month)[1], 'VEHICLE_MONTH_END')
        kind = point['period_kind']
        require(kind in ('period-flow', 'ytd-flow') and start == date(end.year,
                end.month if kind == 'period-flow' else 1, 1), 'VEHICLE_PERIOD_BASIS')
        require(point['value'] is not None, 'VEHICLE_VALUE_MISSING')
        with localcontext(Context(prec=128)):
            value = _decimal(point['value'])
            require(value >= 0, 'VEHICLE_NEGATIVE_COUNT')
            # Published rounded ten-thousand counts retain their precision, not an
            # inferred exact number of cars. Individual-vehicle inputs are integral.
            require(point['unit'] != 'vehicle' or value == value.to_integral_value(),
                    'VEHICLE_FRACTIONAL_INDIVIDUAL_COUNT')
    except RecordError as exc:
        return {**out, 'status': 'unavailable', 'value': None, 'reason_code': str(exc)}
    return out


def _transition(before, after):
    out = {'from_reference': before.get('reference'), 'to_reference': after.get('reference'),
           'from_period': before.get('period_end'), 'to_period': after.get('period_end'),
           'unit': after.get('unit'), 'delta': None, 'percent_change': None}
    def unavailable(code):
        return {**out, 'status': 'unavailable', 'reason_code': code}
    if before['status'] != 'read' or after['status'] != 'read':
        return unavailable('VEHICLE_POINT_UNAVAILABLE')
    lo, hi = _day(before['period_end']), _day(after['period_end'])
    same = (before['period_start'], lo) == (after['period_start'], hi)
    if not same and hi.year * 12 + hi.month - (lo.year * 12 + lo.month) != 1:
        return unavailable('VEHICLE_NONADJACENT_MONTHS')
    old_stamp, new_stamp = (p['source']['published_at'] for p in (before, after))
    regressed = (_clock(new_stamp) < _clock(old_stamp) if 'T' in old_stamp and 'T' in new_stamp
                 else _day(new_stamp[:10]) < _day(old_stamp[:10]))
    if regressed:
        return unavailable('VEHICLE_RELEASE_REGRESSED')
    # Reuse the original full basis, source identity, YTD, overlap and zero guards.
    with localcontext(Context(prec=128)):
        change = _change(before, after)
    return {**out, **change,
        'meaning': 'same-period-vintage-comparison-not-demand-growth' if same else
                   'adjacent-month-published-count-change-not-seasonally-adjusted-or-profit',
        'publisher_reported_growth_verified': False}


def vehicle_flows(reading: dict, *, subject_id=None, company_id=None) -> dict:
    """Interpret a whole read_industry result; selectors never change its denominator."""
    require(subject_id is None or company_id is None, 'INDUSTRY_ONE_QUERY_DIRECTION')
    for value in (subject_id, company_id):
        if value is not None:
            identifier(value)
    require(reading.get('schema') == 'industry-reading-result-v1' and reading.get('query') ==
            {'subject_id': None, 'company_id': None}, 'VEHICLE_WHOLE_INPUT_REQUIRED')
    entries, rejected, subjects, seen = [], [], {}, set()
    for series in reading['series']:
        subject = series['subject']; sid = subject['entity_id']
        attributes = dict(subject['attributes'])
        measures = {(p['metric'], p['period_kind']) for p in series['observations'] if p['status'] == 'read'}
        if (subject['kind'] != 'industry' or attributes.get('observation_family') != 'vehicle-flows'
                or len(measures) != 1 or next(iter(measures), (None,))[0] not in CHANNELS):
            rejected.append({'series_id': series['series_id'], 'reason_code': 'VEHICLE_UNCLASSIFIED_SERIES'})
            continue
        metric, kind = measures.pop()
        require((sid, metric, kind) not in seen, 'VEHICLE_DUPLICATE_SUBJECT_CHANNEL_PERIOD')
        seen.add((sid, metric, kind)); subjects[sid] = deepcopy(subject)
        points = [_measure(p) for p in series['observations']]
        changes = [_transition(a, b) for a, b in zip(points, points[1:])]
        entries.append({'series_id': series['series_id'], 'subject': deepcopy(subject), 'metric': metric,
            'period_kind': kind, 'channel_meaning': CHANNELS[metric], 'points': points,
            'changes': changes, 'latest_change': changes[-1]})
    relations = [deepcopy(r) for r in reading['relations'] if r['subject_id'] in subjects]
    rejected_relations = [r['relation_id'] for r in reading['relations'] if r['subject_id'] not in subjects]
    unavailable = sum(p['status'] != 'read' for e in entries for p in e['points'])
    coverage = [{'subject': subject,
        'monthly_channels_not_supplied': sorted(set(CHANNELS) - {e['metric'] for e in entries
            if e['subject']['entity_id'] == sid and e['period_kind'] == 'period-flow'}),
        'cumulative_channels_not_supplied': sorted(set(CHANNELS) - {e['metric'] for e in entries
            if e['subject']['entity_id'] == sid and e['period_kind'] == 'ytd-flow'})}
        for sid, subject in sorted(subjects.items())]
    result = {'schema': 'industry-vehicle-flows-v1', 'reference': deepcopy(reading['reference']),
        'as_of': reading['as_of'], 'prepared_at': reading['prepared_at'],
        'status': 'partial' if unavailable or rejected or rejected_relations or reading['status'] == 'partial' else 'read',
        'scope': {**reading['scope'], 'qualified_series': len(entries), 'subjects': len(subjects),
                  'unavailable_vehicle_points': unavailable, 'accepted_vehicle_relations': len(relations)},
        'unclassified_series': rejected, 'unclassified_relations': rejected_relations,
        'coverage': coverage, 'limitations': list(LIMITATIONS), 'source_requests': 0, 'writes': 0,
        'company_benefit': 'not-assessed', 'research_executed': False, 'retained': deepcopy(reading)}
    result['view_version'] = digest(encoded({**result, 'entries': entries, 'relations': relations}))
    selected = None
    if company_id is not None:
        relations = [r for r in relations if r['company']['entity_id'] == company_id]
        selected = {r['subject_id'] for r in relations}
    elif subject_id is not None:
        relations = [r for r in relations if r['subject_id'] == subject_id]
        selected = {subject_id}
    result.update(entries=[e for e in entries if selected is None or e['subject']['entity_id'] in selected],
                  relations=relations, query={'subject_id': subject_id, 'company_id': company_id})
    return result


def read_vehicle_flows(reference, store, *, subject_id=None, company_id=None):
    """Original bounded, read-only scopes remain open through interpretation."""
    require(subject_id is None or company_id is None, 'INDUSTRY_ONE_QUERY_DIRECTION')
    for value in (subject_id, company_id):
        if value is not None:
            identifier(value)
    with industry_source_scope(store) as reader:
        result = vehicle_flows(read_industry(reference, reader),
                               subject_id=subject_id, company_id=company_id)
    return result
