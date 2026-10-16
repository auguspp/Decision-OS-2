"""Synthetic business replies through the actual registry/HTTP/capture owners; no live PMI."""
from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from decision_kernel.application.industry_pmi import pmi_source_plan, read_pmi_context
from decision_kernel.application.research_sources import capture_plan, source_inputs
from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.observations.manufacturing_pmi import FIELDS, MAX_MONTHS, month_range, read_monthly_pmi
from test_research_sources import http, registry

STAMP = '2026-10-16T00:00:00+00:00'
AS_OF = '2026-10-16T01:00:00+00:00'
SCOPE = {'start_month': '202601', 'end_month': '202603'}


def row(month, **values):
    return dict(zip(FIELDS, [month, 49.2, 50.0, 49.5, 48.1, 48.7])) | values


def captured(rows=None, *, limit=200, fields=FIELDS, stamp=STAMP, scope=None, api='cn_pmi', extra=None):
    """Use the real shared capture flow and existing synthetic HTTP fixture, not handcrafted receipts."""
    rows = [row('202601'), row('202602'), row('202603')] if rows is None else rows
    reg = registry()
    plan = pmi_source_plan(reg, case_id='synthetic-pmi', limit=limit, **(scope or SCOPE))
    plan['requests'][0]['api'] = api
    plan['requests'][0]['parameters'].update({'fields': ','.join(fields), **(extra or {})})
    bodies = []
    for start in range(0, len(rows) + 1, limit):
        batch = rows[start:start + limit]
        bodies.append({'code': 0, 'api_name': api, 'count': len(batch),
                       'data': {'fields': list(fields), 'items': [[r[f] for f in fields] for r in batch]}})
        if len(batch) < limit:
            break
    client, opener = http(*bodies)
    result = list(capture_plan(plan, reg, client=client, public_rights_confirmed=True, clock=lambda: stamp))[0]
    assert result['capture']['complete']
    return result, reg, opener


def reading(result, reg, **kwargs):
    return read_pmi_context(result, reg, **(SCOPE | {'as_of': AS_OF} | kwargs))


def test_fields_and_fractional_change_are_not_price_returns():
    out = read_monthly_pmi([row('202601'), row('202602', pmi010000=49.5, pmi010700=48.7)],
                           start_month='202601', end_month='202602')
    assert FIELDS == ('month', 'pmi010000', 'pmi010400', 'pmi010500', 'pmi010700', 'pmi011400')
    assert out['status'] == 'read'
    assert out['series'][0]['points'][1]['change_pp'] == 0.3
    inventory = out['series'][3]['points'][1]
    assert inventory['regime'] == 'below-50' and inventory['direction'] == 'up' and inventory['change_pp'] == 0.6
    assert out['series'][0]['method'] == 'weighted-diffusion-composite'
    assert all(s['unit'] == 'index-percent' and s['change_unit'] == 'percentage-point' for s in out['series'])


@pytest.mark.parametrize('level,regime', [(0,'below-50'), (49.9,'below-50'), (50,'at-50'), (50.1,'above-50'), (100,'above-50')])
def test_threshold_including_endpoints(level, regime):
    out = read_monthly_pmi([row('202601', pmi010000=level)], start_month='202601', end_month='202601')
    assert out['series'][0]['points'][0]['regime'] == regime


def test_zero_baseline_has_point_change_without_division():
    out = read_monthly_pmi([row('202601', pmi010000=0), row('202602', pmi010000=1)],
                          start_month='202601', end_month='202602')
    assert out['series'][0]['points'][1]['change_pp'] == 1.0


def test_missing_month_not_bridged_and_order_is_canonical():
    rows = [row('202603'), row('202601')]
    out = read_monthly_pmi(rows, **SCOPE)
    assert out == read_monthly_pmi(list(reversed(rows)), **SCOPE)
    assert out['status'] == 'partial' and out['coverage']['missing_months'] == ['202602']
    assert out['coverage']['available_cells'] == 10
    for series in out['series']:
        assert series['points'][1]['status'] == 'missing-month'
        assert series['points'][2]['change_pp'] is None
        assert series['points'][2]['change_status'] == 'previous-unavailable'


@pytest.mark.parametrize('value,status', [(None,'missing-value'), (True,'invalid-value'), (False,'invalid-value'),
    ('49.2','invalid-value'), ('49.2%','invalid-value'), (-1,'invalid-value'), (101,'invalid-value'),
    (float('nan'),'invalid-value'), (float('inf'),'invalid-value'), (10**400,'invalid-value'), ([], 'invalid-value')])
def test_bad_cell_is_not_zero_or_a_company_fact(value, status):
    out = read_monthly_pmi([row('202601', pmi010700=value), row('202602')],
                           start_month='202601', end_month='202602')
    assert out['status'] == 'partial' and out['coverage']['available_cells'] == 9
    assert out['series'][3]['points'][0]['status'] == status
    assert out['series'][3]['points'][0]['level'] is None
    assert out['series'][3]['points'][1]['change_pp'] is None
    assert out['series'][0]['points'][1]['change_status'] == 'available'


def test_empty_vs_unavailable_are_explicit():
    assert read_monthly_pmi([], **SCOPE)['status'] == 'empty'
    assert read_monthly_pmi([row('202601', **dict.fromkeys(FIELDS[1:]))], **SCOPE)['status'] == 'unavailable'


@pytest.mark.parametrize('month', [None, 202601, True, '202600', '202613', '000001', '2026-01', '２０２６０１', '202601\n'])
def test_invalid_month(month):
    with pytest.raises(RecordError, match='MONTH'):
        month_range(month, '202601')


def test_month_range_crosses_year_and_has_a_bound():
    assert month_range('202512', '202602') == ['202512','202601','202602']
    assert len(month_range('201601', '202512')) == MAX_MONTHS
    for start, end in [('202602','202601'), ('201601','202601')]:
        with pytest.raises(RecordError, match='RANGE'):
            month_range(start, end)


@pytest.mark.parametrize('rows,code', [([row('202601'),row('202601')], 'DUPLICATE'),
    ([row('202601'),row('202601',pmi010000=50)], 'DUPLICATE'),
    ([row('202512')], 'OUTSIDE'), ([{'month':'202601'}], 'FIELDS'), ([row('202601') | {'pmi020100':50}], 'FIELDS')])
def test_ambiguous_rows_never_silently_selected(rows, code):
    with pytest.raises(RecordError, match=code):
        read_monthly_pmi(rows, **SCOPE)


def test_real_registry_capture_and_existing_source_export_agree():
    result, reg, opener = captured(limit=1, fields=tuple(reversed(FIELDS)))
    original = deepcopy(result)
    out = reading(result, reg)
    assert result == original and len(opener.calls) == 4
    assert out['status'] == 'read' and out['source']['pagination_complete'] is True
    assert out['network_requests'] == 0 and out['records_written'] is False
    assert out['source']['published_at'] is None and out['company_exposure'] == 'not-assessed'
    assert out['economic_assessment'] == 'not-performed'
    point = out['series'][0]['points'][2]
    assert point['source']['page_offset'] == 2 and point['source']['row_1based'] == 1
    assert [s['page_offset'] for s in point['change_sources']] == [1,2]
    exported = source_inputs(result, reg)
    assert len(exported) == 4
    stored_page = json.loads(asdict(exported[2])['text'])['page']
    assert digest(encoded(stored_page)) == point['source']['projection_sha256']
    assert out['view_version'] == reading(result, reg)['view_version']
    assert 'SOURCEREF' in ' '.join(out['limitations'])


def test_capture_version_changes_even_if_reading_values_do_not():
    first, reg, _ = captured()
    second, _, _ = captured(stamp='2026-10-16T00:01:00+00:00')
    assert reading(first, reg)['view_version'] != reading(second, reg)['view_version']
    revised, _, _ = captured([row('202601', pmi010000=49.3), row('202602'), row('202603')])
    assert reading(first, reg)['view_version'] != reading(revised, reg)['view_version']


@pytest.mark.parametrize('extra', [{'offset':1}, {'month':'202601'}, {'market':'CN'}])
def test_semantically_wrong_scope_rejected_after_generic_validation(extra):
    result, reg, _ = captured(extra=extra)
    with pytest.raises(RecordError, match='SCOPE'):
        reading(result, reg)


def test_api_fields_and_requested_period_not_interchangeable():
    for options, code in [({'api':'cn_cpi'},'API'), ({'fields':FIELDS[:-1]},'FIELDS'),
                           ({'scope':{'start_month':'202512','end_month':'202603'}},'SCOPE')]:
        result, reg, _ = captured(**options)
        with pytest.raises(RecordError, match=code):
            reading(result, reg)


def test_incomplete_capture_not_empty_or_usable():
    result, reg, _ = captured(limit=1)
    result['capture']['pages'].pop()
    result['capture'].update(complete=False, error='SOURCE_PAGE_LIMIT')
    with pytest.raises(RecordError, match='COMPLETE'):
        reading(result, reg)


@pytest.mark.parametrize('mutation', ['catalogue','offset','clock','clock-null','receipt','unit','month-duplicate'])
def test_capture_integrity_and_semantics(mutation):
    result, reg, _ = captured()
    page = result['capture']['pages'][0]
    if mutation == 'catalogue': result['capture']['snapshot'] = {}
    if mutation == 'offset': page['offset'] = 5
    if mutation == 'clock': page['captured_at'] = '2026-10-16'
    if mutation == 'clock-null': page['captured_at'] = None
    if mutation == 'receipt': page['transport']['wire_sha256'] = 'bad'
    if mutation == 'unit': page['table']['units'] = 'yuan'
    if mutation == 'month-duplicate': page['table']['items'][1][0] = '202601'
    with pytest.raises(RecordError):
        reading(result, reg)


@pytest.mark.parametrize('as_of', [None, '2026-10-16', '2026-10-16T01:00:00', '2026-10-15T23:59:59Z'])
def test_as_of_required_and_does_not_backdate_capture(as_of):
    result, reg, _ = captured()
    with pytest.raises(RecordError):
        reading(result, reg, as_of=as_of)


def test_future_periods_and_explicit_cutoff():
    result, reg, _ = captured([row('202611')], scope={'start_month':'202610','end_month':'202611'})
    with pytest.raises(RecordError, match='FUTURE_REQUEST'):
        reading(result, reg, start_month='202610', end_month='202611')
    with pytest.raises(RecordError, match='FUTURE_PERIOD'):
        reading(result, reg, start_month='202610', end_month='202611', as_of='2026-12-01T00:00:00Z')


def test_month_boundary_uses_china_time_not_utc_month():
    scope = {'start_month':'202605','end_month':'202605'}
    result, reg, _ = captured([row('202605')], stamp='2026-04-30T17:00:00Z', scope=scope)
    assert reading(result, reg, **scope, as_of='2026-04-30T17:00:00Z')['status'] == 'read'


def test_complete_empty_capture_is_not_positive_evidence():
    result, reg, _ = captured([])
    out = reading(result, reg)
    assert out['status'] == 'empty' and out['coverage']['missing_months'] == ['202601','202602','202603']
    assert not out['coverage']['all_requested_cells_available']
    assert all(p['source'] is None for s in out['series'] for p in s['points'])


@pytest.mark.parametrize('limit', [0, True, 2001])
def test_plan_has_source_specific_limit(limit):
    with pytest.raises(RecordError, match='LIMIT'):
        pmi_source_plan(registry(), case_id='synthetic-pmi', **SCOPE, limit=limit)


def test_decimal_calculation_does_not_depend_on_callers_context():
    from decimal import localcontext
    with localcontext() as context:
        context.prec = 1
        out = read_monthly_pmi([row('202601', pmi010000=49.211), row('202602', pmi010000=49.334)],
                               start_month='202601', end_month='202602')
    assert out['series'][0]['points'][1]['change_pp'] == 0.123


def test_current_missing_value_breaks_only_its_own_comparison():
    result, reg, _ = captured([row('202601'), row('202602', pmi010000=None), row('202603')])
    out = reading(result, reg)
    assert out['series'][0]['points'][1]['change_status'] == 'current-unavailable'
    assert out['series'][0]['points'][1]['change_sources'] == []
    assert out['series'][1]['points'][1]['change_status'] == 'available'
    assert out['series'][0]['points'][2]['change_status'] == 'previous-unavailable'
