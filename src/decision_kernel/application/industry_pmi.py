"""Read a bounded cn_pmi capture through the existing registered-source owner.

No network, credentials, store, company mapping or execution authority lives here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.research_sources import SCHEMA, inspect_result, validate_plan
from decision_kernel.domain.records import RecordError, digest, encoded, require, source_time
from decision_kernel.observations.manufacturing_pmi import FIELDS, month_range, read_monthly_pmi

SCHEMA_READING = 'industry-pmi-reading-v1'
CHINA_TIME = timezone(timedelta(hours=8))
LIMITATIONS = (
    'THIRD_PARTY_RELAY_CAPTURE_NOT_AUTHENTICATED_OR_OFFICIAL_RELEASE_VERIFIED',
    'INDEX_LEVEL_AND_PERCENTAGE_POINT_CHANGE_NOT_QUANTITY_OR_REVENUE_GROWTH',
    'INVENTORY_DIFFUSION_NOT_INVENTORY_AMOUNT_OR_WORKING_CAPITAL',
    'NATIONAL_MANUFACTURING_NOT_COMPANY_EXPOSURE_OR_NET_BENEFIT',
    'MONTH_IS_OBSERVATION_PERIOD_NOT_PUBLICATION_OR_POINT_IN_TIME_PROOF',
    'PAGINATION_TERMINATION_NOT_HISTORICAL_COVERAGE_OR_CURRENT_SOURCE_HEALTH',
    'VIEW_HASH_AND_ROW_LOCATORS_NOT_SOURCEREF_OR_PUBLICATION_PERMISSION',
    'NO_CROSS_CAPTURE_REVISION_DETECTION_OR_AUTOMATIC_RESEARCH_APPROVAL',
)


def pmi_source_plan(registry: RelayRegistry, *, case_id: str, start_month: str,
                    end_month: str, limit: int = 200) -> dict:
    """Build an ordinary source plan; creation is offline and grants no capture rights."""
    month_range(start_month, end_month)
    require(type(limit) is int and 1 <= limit <= 2000, 'PMI_LIMIT_INVALID')
    plan = {'schema': SCHEMA, 'case_id': case_id, 'requests': [{
        'stage': 'business', 'purpose': '核对制造业需求、生产及库存景气；不是公司经营暴露或净受益判断',
        'api': 'cn_pmi', 'parameters': {'start_m': start_month, 'end_m': end_month,
                                      'fields': ','.join(FIELDS), 'limit': limit}}]}
    validate_plan(plan, registry)
    return plan


def _stamp(value):
    require(isinstance(value, str) and 'T' in value, 'PMI_AS_OF_REQUIRED')
    source_time(value)
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(stamp.utcoffset() is not None, 'PMI_AS_OF_REQUIRED')
    try:
        stamp.astimezone(CHINA_TIME)
    except OverflowError as exc:
        raise RecordError('PMI_CLOCK_INVALID') from exc
    return stamp


def _china_month(stamp):
    local = stamp.astimezone(CHINA_TIME)
    return f'{local.year:04d}{local.month:02d}'


def read_pmi_context(result: dict, registry: RelayRegistry, *, start_month: str,
                     end_month: str, as_of: str) -> dict:
    """Validate scope, receipt and month semantics before adopting any captured cell."""
    month_range(start_month, end_month)
    cut = _stamp(as_of)
    require(end_month <= _china_month(cut), 'PMI_FUTURE_REQUEST')
    require(isinstance(result, dict) and isinstance(result.get('capture'), dict), 'PMI_CAPTURE_REQUIRED')
    capture_pages = result['capture'].get('pages')
    require(isinstance(capture_pages, list), 'PMI_CAPTURE_REQUIRED')
    for page in capture_pages:
        require(isinstance(page, dict), 'PMI_CAPTURE_REQUIRED')
        require(_stamp(page.get('captured_at')) <= cut, 'PMI_CAPTURE_AFTER_AS_OF')
    view = inspect_result(result, registry)
    params = view['parameters']
    require(view['api'] == 'cn_pmi', 'PMI_API_MISMATCH')
    require(set(params) <= {'start_m', 'end_m', 'fields', 'limit', 'offset'}
            and params.get('start_m') == start_month and params.get('end_m') == end_month
            and int(params.get('offset', 0)) == 0, 'PMI_REQUEST_SCOPE_MISMATCH')
    require(set(params['fields'].split(',')) == set(FIELDS), 'PMI_REQUEST_FIELDS_MISMATCH')
    require(int(params['limit']) <= 2000, 'PMI_LIMIT_INVALID')
    require(result['capture'].get('snapshot') == registry.snapshot, 'PMI_CATALOGUE_VERSION_MISMATCH')
    require(view['complete'], 'PMI_COMPLETE_CAPTURE_REQUIRED')
    rows, locators, pages = [], {}, []
    for page in view['pages']:
        stamp = _stamp(page['captured_at'])
        page_hash = digest(encoded(page))
        pages.append({'offset': page['offset'], 'captured_at': page['captured_at'],
                      'wire_sha256': page['transport']['wire_sha256'], 'projection_sha256': page_hash})
        table = page['table']
        for number, values in enumerate(table['items'], 1):
            row = dict(zip(table['fields'], values))
            # Pure owner validates month type/range and duplicate identity below.
            rows.append(row)
            if isinstance(row.get('month'), str):
                require(row['month'] <= _china_month(stamp), 'PMI_FUTURE_PERIOD')
                locators[row['month']] = {'query_key': view['query_key'], 'page_offset': page['offset'],
                    'row_1based': number, 'projection_sha256': page_hash, 'captured_at': page['captured_at']}
    observation = read_monthly_pmi(rows, start_month=start_month, end_month=end_month)
    for series in observation['series']:
        for point in series['points']:
            month, previous = point['month'], point['previous_month']
            point['source'] = {**locators[month], 'field': series['field']} if month in locators else None
            point['change_sources'] = ([{**locators[previous], 'field': series['field']}, point['source']]
                                       if point['change_status'] == 'available' else [])
    out = {'schema': SCHEMA_READING, **observation, 'as_of': cut.isoformat(),
        'source': {'api': view['api'], 'query_key': view['query_key'], 'parameters': deepcopy(params),
            'catalogue_snapshot': deepcopy(registry.snapshot), 'pages': pages,
            'capture_view_sha256': digest(encoded(view)), 'published_at': None,
            'publication_status': 'not-provided-by-cn_pmi', 'pagination_complete': view['complete']},
        'methodology': {'field_contract': 'https://tushare.pro/document/2?doc_id=325',
            'basis': 'NBS-manufacturing-diffusion-and-composite-index-method',
            'capture_checked_against_official_release': False,
            'seasonal_adjustment': 'not-revalidated-on-capture'},
        'network_requests': 0, 'records_written': False, 'company_exposure': 'not-assessed',
        'economic_assessment': 'not-performed', 'limitations': list(LIMITATIONS)}
    out['view_version'] = digest(encoded(out))
    return out
