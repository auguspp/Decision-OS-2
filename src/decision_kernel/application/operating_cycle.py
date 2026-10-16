"""Operating-cycle arithmetic over retained industry points, never source acquisition.

NBS revenue/cost/total-profit YTD, end stocks, published comparable growth and
published turnover days remain different measures. No reconstructed monthly flow.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date
from decimal import Context, Decimal, localcontext

from decision_kernel.application.industry_reading import BASIS, _clock, _day, _decimal
from decision_kernel.domain.records import RecordError, digest, encoded, require

# unit, period kind, assessment. Monetary amounts are explicitly in 100m CNY.
METRICS = {
    **{k: ('CNY-100m', 'ytd-flow', 'cumulative') for k in ('revenue', 'cost', 'profit')},
    **{k + '_yoy': ('percent', 'index-level', 'ytd-comparable-yoy')
       for k in ('revenue', 'cost', 'profit')},
    **{k: ('CNY-100m', 'stock', 'period-end') for k in ('receivables', 'inventory')},
    **{k + '_yoy': ('percent', 'index-level', 'period-end-comparable-yoy')
       for k in ('receivables', 'inventory')},
    **{k + '_days': ('day', 'index-level', 'reported-average-days')
       for k in ('receivables', 'inventory')},
}
SCOPE = ('subject_id', 'specification', 'region', 'currency', 'tax_basis', 'frequency', 'period_end')
UNSIGNED = {'revenue', 'cost', 'receivables', 'inventory', 'receivables_days', 'inventory_days'}
LIMITS = [
    'YTD_AMOUNTS_NOT_SUBTRACTED_INTO_MONTHLY_FLOWS',
    'PUBLISHED_YOY_COMPARABLE_BASE_NOT_RECONSTRUCTED_FROM_OLD_RELEASE_AMOUNTS',
    'STOCK_CHANGE_NOT_CASH_FLOW_OR_COMPLETE_NET_WORKING_CAPITAL',
    'PUBLISHED_AVERAGE_DAYS_NOT_RECALCULATED_FROM_END_STOCKS',
    'MARGINS_USE_TOTAL_PROFIT_NOT_OPERATING_OR_ATTRIBUTABLE_PROFIT',
    'DIFFERENT_CUMULATIVE_WINDOWS_AND_STOCK_FLOW_GROWTH_GAPS_NOT_CAUSAL',
    'DECLARED_POPULATIONS_MAY_OVERLAP_NO_AGGREGATION_OR_BENEFICIARY_SCORE',
    'RETAINED_SOURCE_AND_CURATED_MEANING_NOT_AUTHENTICATION_OR_RESEARCH_ACCEPTANCE',
]


def _unavailable(code):
    return {'status': 'unavailable', 'reason_code': code, 'value': None}


def _measure(point, metric):
    if point['status'] != 'read':
        return {**deepcopy(point), 'value': None}
    out = deepcopy(point)
    try:
        unit, kind, assessment = METRICS[metric]
        require((point['metric'], point['unit'], point['period_kind'], point['assessment'])
                == (metric, unit, kind, assessment), 'OPERATING_METRIC_BASIS')
        require(point['currency'] == 'CNY' and point['frequency'] == 'monthly'
                and point['price_kind'] == point['market'] == point['contract'] == 'not-applicable',
                'OPERATING_NONMARKET_BASIS')
        require(all(point[k].casefold() not in ('unknown', 'unspecified') for k in SCOPE),
                'OPERATING_SCOPE_UNKNOWN')
        end = _day(point['period_end'])
        require(end.day == monthrange(end.year, end.month)[1], 'OPERATING_MONTH_END')
        ytd = kind == 'ytd-flow' or assessment == 'ytd-comparable-yoy'
        start = date(end.year, 1, 1) if ytd else end
        require(_day(point['period_start']) == start, 'OPERATING_PERIOD_BASIS')
        if point['value'] is None:
            return {**out, **_unavailable('OPERATING_VALUE_MISSING')}
        value = _decimal(point['value'])
        require(metric not in UNSIGNED or value >= 0, 'OPERATING_NEGATIVE_AMOUNT_OR_DAYS')
    except RecordError as exc:
        return {**out, **_unavailable(str(exc))}
    return out


def _calculation(points, names, operation, unit):
    operands = [points.get(k) for k in names]
    sources = [p.get('reference') if p else None for p in operands]
    out = {'operation': operation, 'operands': list(names), 'unit': unit, 'sources': sources}
    if any(p is None for p in operands):
        return {**out, **_unavailable('OPERATING_METRIC_NOT_SUPPLIED')}
    if any(p['status'] != 'read' or p['value'] is None for p in operands):
        return {**out, **_unavailable('OPERATING_OPERAND_UNAVAILABLE')}
    first = operands[0]
    if any(p['reference']['manifest'] != first['reference']['manifest'] for p in operands):
        return {**out, **_unavailable('OPERATING_DIFFERENT_SOURCE_VERSIONS')}
    if any(any(p[k] != first[k] for k in SCOPE) for p in operands):
        return {**out, **_unavailable('OPERATING_DIFFERENT_POPULATIONS_OR_PERIODS')}
    with localcontext(Context(prec=128)):
        a, b = (_decimal(p['value']) for p in operands)
        if operation == 'ratio-times-100':
            if b <= 0:
                return {**out, **_unavailable('OPERATING_NONPOSITIVE_DENOMINATOR')}
            value = a / b * 100
        else:
            value = a + b if operation == 'sum-selected-stocks' else a - b
    return {**out, 'status': 'calculated', 'value': format(value, 'f'), 'reason_code': None}


def _transition(before, after, metric):
    out = {'from_reference': before.get('reference'), 'to_reference': after.get('reference'),
           'from_period': before.get('period_end'), 'to_period': after.get('period_end'),
           'delta': None, 'unit': METRICS[metric][0], 'percent_change': None}
    def unavailable(code):
        return {**out, 'status': 'unavailable', 'reason_code': code}
    if before['status'] != 'read' or after['status'] != 'read':
        return unavailable('OPERATING_POINT_UNAVAILABLE')
    if any(before[k] != after[k] for k in BASIS) or any(
        before['source'][k] != after['source'][k] for k in ('source_id', 'origin')):
        return unavailable('OPERATING_BASIS_OR_SOURCE_CHANGED')
    # Chronology is release chronology, not a reconstructed historical PIT.
    old_stamp, new_stamp = (p['source']['published_at'] for p in (before, after))
    regressed = (_clock(new_stamp) < _clock(old_stamp) if 'T' in old_stamp and 'T' in new_stamp
                 else _day(new_stamp[:10]) < _day(old_stamp[:10]))
    if regressed:
        return unavailable('OPERATING_RELEASE_REGRESSED')
    lo, hi = _day(before['period_end']), _day(after['period_end'])
    same = (before['period_start'], lo) == (after['period_start'], hi)
    if not same:
        if hi.year * 12 + hi.month - (lo.year * 12 + lo.month) != 1:
            return unavailable('OPERATING_NONADJACENT_MONTHS')
        if METRICS[metric][1] == 'ytd-flow':
            return unavailable('OPERATING_CUMULATIVE_NOT_MONTHLY')
        if METRICS[metric][2] == 'ytd-comparable-yoy' and hi.year != lo.year:
            return unavailable('OPERATING_CUMULATIVE_YEAR_RESET')
    with localcontext(Context(prec=128)):
        a, b = _decimal(before['value']), _decimal(after['value'])
        delta = b - a
        percent = delta / abs(a) * 100 if METRICS[metric][1] == 'stock' and a else None
    return {**out, 'status': ('unchanged' if delta == 0 else 'same-period-revision') if same else 'changed',
        'delta': format(delta, 'f'), 'unit': 'percentage-point' if METRICS[metric][0] == 'percent' else out['unit'],
        'percent_change': format(percent, 'f') if percent is not None else None,
        'reason_code': 'OPERATING_ZERO_STOCK_BASE' if METRICS[metric][1] == 'stock' and not a else None,
        'meaning': 'release-revision-not-period-flow' if same else
                   'reported-stock-change-not-cash-flow' if METRICS[metric][1] == 'stock' else
                   'published-indicator-change-not-monthly-business-growth'}


def operating_cycle(reading: dict, *, subject_id=None) -> dict:
    """Derive from read_industry's exact points; keep its full original evidence.

    Observation slots are explicit in the original packet. Missing, changed or
    mixed slots are never sorted, forward-filled or joined to manufacture a pair.
    """
    require(reading.get('schema') == 'industry-reading-result-v1'
            and reading.get('query') == {'subject_id': None, 'company_id': None}, 'OPERATING_FULL_INPUT_REQUIRED')
    groups, rejected = {}, []
    for series in reading['series']:
        subject = series['subject']
        sid = subject['entity_id']
        group = groups.setdefault(sid, {'subject': subject, 'metrics': {}})
        names = {p['metric'] for p in series['observations'] if p['status'] == 'read'}
        if subject['kind'] != 'industry' or len(names) != 1 or next(iter(names), None) not in METRICS:
            rejected.append({'series_id': series['series_id'], 'reason_code': 'OPERATING_UNCLASSIFIED_SERIES'})
            continue
        metric = names.pop()
        require(metric not in group['metrics'], 'OPERATING_DUPLICATE_SUBJECT_METRIC')
        points = [_measure(p, metric) for p in series['observations']]
        group['metrics'][metric] = {'series_id': series['series_id'], 'points': points,
            'changes': [_transition(a, b, metric) for a, b in zip(points, points[1:])]}
    entries, unavailable = [], len(rejected)
    for sid, group in sorted(groups.items()):
        metrics = group['metrics']
        unavailable += sum(p['status'] != 'read' for m in metrics.values() for p in m['points'])
        snapshots = []
        for slot in range(max((len(m['points']) for m in metrics.values()), default=0)):
            points = {k: m['points'][slot] for k, m in metrics.items() if slot < len(m['points'])}
            calculated = {
                'profit_margin_pct': _calculation(points, ('profit', 'revenue'), 'ratio-times-100', 'percent'),
                'cost_per_100_revenue': _calculation(points, ('cost', 'revenue'), 'ratio-times-100', 'CNY-per-100-CNY'),
                'receivables_plus_inventory': _calculation(points, ('receivables', 'inventory'), 'sum-selected-stocks', 'CNY-100m'),
                'profit_less_revenue_growth_pp': _calculation(points, ('profit_yoy', 'revenue_yoy'), 'subtract-growth-rates', 'percentage-point'),
                'cost_less_revenue_growth_pp': _calculation(points, ('cost_yoy', 'revenue_yoy'), 'subtract-growth-rates', 'percentage-point'),
                'inventory_less_revenue_growth_pp': _calculation(points, ('inventory_yoy', 'revenue_yoy'), 'subtract-growth-rates', 'percentage-point'),
                'receivables_less_revenue_growth_pp': _calculation(points, ('receivables_yoy', 'revenue_yoy'), 'subtract-growth-rates', 'percentage-point'),
            }
            questions = []
            for key, label in (
                ('cost_less_revenue_growth_pp', '成本同比快于收入：需核价格、产品结构及费用。'),
                ('inventory_less_revenue_growth_pp', '库存余额同比快于累计收入：需核滞销、备货和价格因素。'),
                ('receivables_less_revenue_growth_pp', '应收余额同比快于累计收入：需核账期、回款与客户结构。'),
            ):
                value = calculated[key]
                if value['status'] == 'calculated' and Decimal(value['value']) > 0:
                    questions.append({'basis': key, 'question': label, 'assessment': 'not-performed'})
            periods = sorted({p['period_end'] for p in points.values() if p['status'] == 'read'})
            snapshots.append({'slot': slot, 'period_ends': periods, 'calculations': calculated,
                              'questions': questions})
        entries.append({**group, 'snapshots': snapshots, 'declared_metrics': sorted(metrics),
                        'not_supplied_metrics': sorted(set(METRICS) - set(metrics))})
    result = {'schema': 'industry-operating-cycle-v1', 'reference': reading['reference'],
        'as_of': reading['as_of'], 'status': 'partial' if unavailable or reading['status'] == 'partial' else 'read',
        'scope': {'subjects': len(entries), 'series': reading['scope']['series'],
                  'unavailable_metric_points_or_series': unavailable},
        'unclassified_series': rejected, 'limitations': list(LIMITS),
        'source_requests': 0, 'writes': 0, 'company_benefit': 'not-assessed',
        'research_executed': False, 'retained': reading}
    result['view_version'] = digest(encoded({**result, 'entries': entries}))
    result['entries'] = [e for e in entries if subject_id is None or e['subject']['entity_id'] == subject_id]
    result['query'] = {'subject_id': subject_id}
    return result
