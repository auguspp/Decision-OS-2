"""Monthly manufacturing PMI levels, not prices, quantity growth or issuer exposure.

Field owner: Tushare cn_pmi doc_id=325. Method: NBS diffusion/composite indices.
Only the five reviewed national manufacturing fields are interpreted here.
"""
from __future__ import annotations

from decimal import Context, Decimal, localcontext
import math
import re

from decision_kernel.domain.records import require

CONTRACT = 'cn-manufacturing-pmi-v1'
INDICATORS = (
    ('pmi010000', 'manufacturing-pmi', '制造业PMI', 'weighted-diffusion-composite'),
    ('pmi010400', 'production', '生产指数', 'diffusion'),
    ('pmi010500', 'new-orders', '新订单指数', 'diffusion'),
    ('pmi010700', 'raw-material-inventory', '原材料库存指数', 'diffusion'),
    ('pmi011400', 'finished-goods-inventory', '产成品库存指数', 'diffusion'),
)
FIELDS = ('month', *(item[0] for item in INDICATORS))
MAX_MONTHS = 120


def month_number(value: str) -> int:
    require(isinstance(value, str) and re.fullmatch(r'[0-9]{6}', value) is not None,
            'PMI_MONTH_INVALID')
    year, month = int(value[:4]), int(value[4:])
    require(1 <= year <= 9999 and 1 <= month <= 12, 'PMI_MONTH_INVALID')
    return year * 12 + month - 1


def month_range(start_month: str, end_month: str) -> list[str]:
    start, end = month_number(start_month), month_number(end_month)
    require(1 <= end - start + 1 <= MAX_MONTHS, 'PMI_MONTH_RANGE_INVALID')
    return [f'{number // 12:04d}{number % 12 + 1:02d}' for number in range(start, end + 1)]


def _level(value):
    if value is None:
        return None, 'missing-value'
    # No coercion from booleans, strings, percentages or other vendor representations.
    if type(value) not in (int, float) or not 0 <= value <= 100 or not math.isfinite(value):
        return None, 'invalid-value'
    return float(value), 'available'


def read_monthly_pmi(rows: list[dict], *, start_month: str, end_month: str) -> dict:
    """Pure observations; a missing month/value never becomes zero or a skipped month."""
    months = month_range(start_month, end_month)
    require(isinstance(rows, list) and len(rows) <= MAX_MONTHS, 'PMI_ROWS_INVALID')
    indexed = {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == set(FIELDS), 'PMI_ROW_FIELDS_INVALID')
        month = row['month']
        month_number(month)
        require(month in months, 'PMI_RETURNED_MONTH_OUTSIDE_REQUEST')
        # Even identical duplicates could be versions/overlap. Never silently select one.
        require(month not in indexed, 'PMI_DUPLICATE_MONTH')
        indexed[month] = row
    series, available = [], 0
    for field, indicator, label, method in INDICATORS:
        points = []
        for index, month in enumerate(months):
            level, status = _level(indexed[month][field]) if month in indexed else (None, 'missing-month')
            previous = points[-1]['level'] if points else None
            change = None
            if index == 0:
                change_status = 'first-requested-month'
            elif level is None:
                change_status = 'current-unavailable'
            elif previous is None:
                change_status = 'previous-unavailable'
            else:
                # Decimal subtraction avoids 49.5 - 49.2 becoming a noisy binary fraction.
                with localcontext(Context(prec=40)):
                    change = float(Decimal(str(level)) - Decimal(str(previous)))
                change_status = 'available'
            available += level is not None
            points.append({'month': month, 'level': level, 'status': status,
                'regime': None if level is None else 'above-50' if level > 50 else 'below-50' if level < 50 else 'at-50',
                'previous_month': months[index - 1] if index else None,
                'change_pp': change, 'change_status': change_status,
                'direction': None if change is None else 'up' if change > 0 else 'down' if change < 0 else 'flat'})
        series.append({'field': field, 'indicator': indicator, 'label': label,
            'method': method, 'unit': 'index-percent', 'change_unit': 'percentage-point',
            'threshold': 50.0, 'points': points})
    total = len(months) * len(INDICATORS)
    return {'contract': CONTRACT,
        'status': 'empty' if not rows else 'unavailable' if not available else 'read' if available == total else 'partial',
        'scope': {'start_month': start_month, 'end_month': end_month, 'frequency': 'monthly',
                  'population': 'china-national-manufacturing'},
        'coverage': {'expected_months': months, 'returned_months': sorted(indexed),
            'missing_months': [m for m in months if m not in indexed],
            'available_cells': available, 'unavailable_cells': total - available,
            'all_requested_cells_available': available == total},
        'series': series}
