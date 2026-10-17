"""Read original Relay business-mix captures; arithmetic is not issuer exposure.

Uses the existing registered inspector and financial field contract. No source
requests, storage, latest-version selection, segment graph or research execution.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Context, Decimal, localcontext
import re

from decision_kernel.application.research_sources import inspect_result
from decision_kernel.domain.records import RecordError, checked_text, digest, encoded, require
from decision_kernel.observations.relay_financial import FinancialQuery, SPECS, _cell, _clock

FIELDS = frozenset({'ts_code', 'end_date', 'bz_item', 'bz_sales', 'bz_profit',
                    'bz_cost', 'curr_type', 'update_flag'})
AMOUNTS = ('bz_sales', 'bz_cost', 'bz_profit')
LIMITATIONS = (
    'RELAY_CAPTURE_NOT_ISSUER_FILING_OR_AUTHENTICATED_ORIGINAL',
    'END_DATE_IS_REPORT_PERIOD_NOT_PUBLICATION_OR_HISTORICAL_PIT',
    'UPDATE_FLAG_NOT_A_COMPLETE_VINTAGE_OR_AUTOMATIC_LATEST_SELECTOR',
    'SIGNED_ROW_SUM_NOT_CERTIFIED_SEGMENT_PARTITION_OR_EXTERNAL_REVENUE',
    'DIFFERENT_DIMENSIONS_CURRENCIES_OR_CAPTURES_NOT_MERGED',
    'SALES_MINUS_COST_NOT_AUTOMATICALLY_GROSS_PROFIT_OR_ATTRIBUTABLE_NET_INCOME',
    'AMOUNT_MATCH_NOT_UNIT_AUTHENTICATION_OR_SAME_VINTAGE_PROOF',
    'NO_PROCUREMENT_ROLE_MARKET_SHARE_NET_EXPOSURE_OR_BENEFICIARY_INFERENCE',
    'LOCAL_READING_DOES_NOT_GRANT_PUBLICATION_OR_PERMANENT_CUSTODY_RIGHTS',
)


def _view(result, registry, api, code, period, cut, *, dimension=None):
    require(len(encoded(result)) <= 16 * 1024 * 1024, 'MAINBZ_INPUT_BOUND')
    view = inspect_result(result, registry)
    params = view['parameters']
    required = {'ts_code', 'period', 'fields', 'limit'} | ({'type'} if dimension else set())
    extras = {'offset'} | ({'report_type'} if api == 'income' else set())
    require(view['api'] == api and required <= params.keys() <= required | extras
            and params['ts_code'] == code and params['period'] == period
            and int(params.get('offset', 0)) == 0, 'MAINBZ_REQUEST_SCOPE')
    if dimension:
        require(params['type'] == dimension and FIELDS <= set(params['fields'].split(','))
                <= FIELDS | {'bz_code'}, 'MAINBZ_REQUEST_FIELDS_OR_DIMENSION')
    else:
        require({'ts_code', 'end_date', 'report_type', 'revenue', 'oper_cost'}
                <= set(params['fields'].split(','))
                and params.get('report_type', '1') == '1', 'MAINBZ_INCOME_FIELDS_OR_BASIS')
    require(result['capture'].get('snapshot') == registry.snapshot, 'MAINBZ_REGISTRY_VERSION')
    require(result.get('status') in (
        ('captured', 'reused-in-this-batch') if view['complete'] else
        ('capture-incomplete', 'prior-incomplete-in-this-batch')), 'MAINBZ_RESULT_STATUS')
    require(view['row_count'] <= 10000 and view['page_count'] <= 100, 'MAINBZ_READING_BOUND')
    rows = []
    for page in view['pages']:
        stamp = _clock(page['captured_at'])
        require(datetime.strptime(period, '%Y%m%d').date() <= stamp.date()
                and stamp <= cut, 'MAINBZ_CAPTURE_CLOCK')
        table = page['table']
        for n, values in enumerate(table['items'], 1):
            rows.append((dict(zip(table['fields'], values)), {
                'query_key': view['query_key'], 'page_offset': page['offset'], 'row_1based': n,
                'captured_at': page['captured_at'],
                'projection_sha256': digest(encoded(page)),
            }))
    return view, rows


def _amount(raw, field, unit='yuan'):
    cell = _cell(raw.get(field), unit, field in raw)
    if cell['presence'] == 'value':
        text = cell['numeric_text']
        number = Decimal(text)
        require(len(text) <= 64 and abs(number.as_tuple().exponent) <= 64
                and len(number.as_tuple().digits) <= 64, 'MAINBZ_NUMERIC_BOUND')
    return cell


def _number(cell):
    return Decimal(cell['numeric_text']) if cell['presence'] == 'value' else None


def _unavailable(reason):
    return {'status': 'unavailable', 'value': None, 'reason_code': reason}


def _calculations(cells):
    a, b, p = (_number(cells[k]) for k in AMOUNTS)
    if a is None or b is None:
        return {'sales_minus_cost': _unavailable('MAINBZ_OPERAND_MISSING'),
                'difference_ratio_percent': _unavailable('MAINBZ_OPERAND_MISSING'),
                'reported_profit_check': {'status': 'unavailable', 'reason_code': 'MAINBZ_OPERAND_MISSING'}}
    with localcontext(Context(prec=256)):
        difference = a - b
        calculated = {'status': 'calculated', 'value': format(difference, 'f'), 'reason_code': None}
        ratio = ({'status': 'calculated', 'value': format(difference / a * 100, 'f'), 'reason_code': None}
                 if a > 0 and b >= 0 else _unavailable('MAINBZ_NONPOSITIVE_SALES_OR_SIGNED_COST'))
        check = ({'status': 'matched' if p == difference else 'different',
                  'difference': format(difference - p, 'f'), 'reason_code': None}
                 if p is not None else {'status': 'not-reported', 'reason_code': 'MAINBZ_PROFIT_MISSING'})
    return {'sales_minus_cost': calculated, 'difference_ratio_percent': ratio, 'reported_profit_check': check}


def _entry(raw, locator, dimension):
    out = {'source': locator, 'raw': deepcopy(raw), 'status': 'quarantined', 'reason_code': None}
    try:
        checked_text(raw.get('bz_item'), limit=1024)
        currency = raw.get('curr_type')
        require(isinstance(currency, str) and re.fullmatch('[A-Z]{3}', currency), 'MAINBZ_CURRENCY_UNKNOWN')
        if 'bz_code' in raw:
            require(raw['bz_code'] == dimension, 'MAINBZ_RETURNED_DIMENSION')
        require(raw.get('update_flag') in (None, '', '0', '1')
                and type(raw.get('update_flag')) in (str, type(None)), 'MAINBZ_UPDATE_FLAG_INVALID')
        cells = {k: _amount(raw, k) for k in AMOUNTS}
        out.update(status='read', cells=cells,
                   dimension_basis='returned-and-requested' if 'bz_code' in raw else 'request-only',
                   **_calculations(cells))
    except RecordError as error:
        out['reason_code'] = str(error)
    return out


def _sums(entries, complete):
    currencies = sorted({e['raw']['curr_type'] for e in entries if e['status'] == 'read'})
    # Unidentifiable/duplicate rows can affect any denominator. Do not sum only the good subset.
    all_good = complete and bool(entries) and all(e['status'] == 'read' for e in entries)
    groups = []
    for currency in currencies:
        members = [e for e in entries if e['status'] == 'read' and e['raw']['curr_type'] == currency]
        sums = {}
        for field in AMOUNTS:
            values = [_number(e['cells'][field]) for e in members]
            if not all_good:
                sums[field] = _unavailable('MAINBZ_COMPLETE_UNAMBIGUOUS_ROWS_REQUIRED')
            elif any(v is None for v in values):
                sums[field] = _unavailable('MAINBZ_SUM_OPERAND_MISSING')
            else:
                with localcontext(Context(prec=256)):
                    sums[field] = {'status': 'calculated', 'value': format(sum(values, Decimal(0)), 'f'),
                                   'reason_code': None}
        groups.append({'currency': currency, 'row_count': len(members), 'sums': sums,
                       'sources': [e['source'] for e in members],
                       'meaning': 'signed-sum-of-returned-rows-not-certified-business-partition'})
    return groups


def _income(result, registry, code, period, cut, groups):
    if result is None:
        return {'status': 'not-supplied', 'checks': []}
    try:
        view, rows = _view(result, registry, 'income', code, period, cut)
        require(view['complete'] and len(rows) == 1, 'MAINBZ_UNIQUE_COMPLETE_INCOME_REQUIRED')
        raw, locator = rows[0]
        require(raw['report_type'] == '1', 'MAINBZ_CONSOLIDATED_INCOME_REQUIRED')
        for field in ('ann_date', 'f_ann_date'):
            cell = _cell(raw.get(field), 'date', field in raw)
            if cell['presence'] == 'value':
                require(period <= cell['iso'].replace('-', '')
                        and cell['iso'] <= _clock(locator['captured_at']).date().isoformat(),
                        'MAINBZ_INCOME_PUBLICATION_CLOCK')
        # An update flag never resolves duplicate/overlapping statement rows above.
        require(len(groups) == 1 and groups[0]['currency'] == 'CNY', 'MAINBZ_INCOME_CURRENCY_NOT_COMPARABLE')
        checks = []
        for component, control in (('bz_sales', 'revenue'), ('bz_cost', 'oper_cost')):
            cell = _amount(raw, control, SPECS['income'][2][control])
            total, target = groups[0]['sums'][component], _number(cell)
            check = {'component_field': component, 'control_field': control,
                     'control_source': {**locator, 'field': control}, 'control_cell': cell,
                     'component_sources': [{**s, 'field': component} for s in groups[0]['sources']]}
            if total['status'] != 'calculated' or target is None:
                check.update(status='unavailable', difference=None, reason_code='MAINBZ_RECONCILIATION_OPERAND_MISSING')
            else:
                with localcontext(Context(prec=256)):
                    delta = Decimal(total['value']) - target
                check.update(status='matched' if delta == 0 else 'different', difference=format(delta, 'f'), reason_code=None)
            checks.append(check)
        status = ('unavailable' if any(c['status'] == 'unavailable' for c in checks) else
                  'different' if any(c['status'] == 'different' for c in checks) else 'matched')
        return {'status': status, 'query_key': view['query_key'], 'checks': checks,
                'basis': 'unscaled-source-numbers-consolidated-report-type-1',
                'control_unit': 'currency-unit', 'same_currency_and_scale_verified': False,
                'scope': 'same-report-period-arithmetic-only', 'same_vintage_verified': False,
                'partition_completeness_verified': False}
    except RecordError as error:
        return {'status': 'unavailable', 'reason_code': str(error), 'checks': []}


def read_main_business(result, registry, *, ts_code, period, dimension, as_of, income_result=None, item=None,
                       income_selection_error=None):
    """Consume ONE selected original capture; never union P/D/I or choose latest rows."""
    require(income_selection_error is None or income_result is None
            and isinstance(income_selection_error, str) and income_selection_error in {
        'MAINBZ_SELECTED_CAPTURE_MISSING', 'MAINBZ_MULTIPLE_CAPTURE_VERSIONS',
        'MAINBZ_CAPTURE_SELECTION_INVALID'}, 'MAINBZ_INCOME_SELECTION_ERROR')
    FinancialQuery('fina_mainbz', ts_code, period, dimension=dimension)
    cut = _clock(as_of)
    require(datetime.strptime(period, '%Y%m%d').date() <= cut.date(), 'MAINBZ_FUTURE_PERIOD')
    if item is not None:
        checked_text(item, limit=1024)
    view, rows = _view(result, registry, 'fina_mainbz', ts_code, period, cut, dimension=dimension)
    entries = [_entry(raw, locator, dimension) for raw, locator in rows]
    keys = [(e['raw'].get('bz_item'), e['raw'].get('curr_type')) for e in entries]
    duplicates = Counter(k for k in keys if all(isinstance(v, str) for v in k))
    # Rebuild duplicate rows without discarding the original position or raw evidence.
    for index, key in enumerate(keys):
        if all(isinstance(v, str) for v in key) and duplicates[key] > 1:
            raw, locator = rows[index]
            entries[index] = {'raw': deepcopy(raw), 'source': locator, 'status': 'quarantined',
                              'reason_code': 'MAINBZ_DUPLICATE_BUSINESS_CURRENCY'}
    groups = _sums(entries, view['complete'])
    missing = sum(e['status'] == 'read' and any(e['cells'][k]['presence'] != 'value'
                  for k in ('bz_sales', 'bz_cost')) for e in entries)
    quarantined = sum(e['status'] != 'read' for e in entries)
    consistency = Counter(e['reported_profit_check']['status'] for e in entries if e['status'] == 'read')
    out = {'schema': 'relay-main-business-reading-v1', 'as_of': as_of,
           'scope': {'ts_code': ts_code, 'period': period, 'dimension': dimension,
                     'returned_rows': len(entries), 'qualified_rows': len(entries) - quarantined,
                     'quarantined_rows': quarantined, 'missing_sales_or_cost_rows': missing,
                     'pagination_complete': view['complete']},
           'status': 'partial' if not view['complete'] or quarantined or missing else 'read' if rows else 'empty',
           'source': {'query_key': view['query_key'], 'parameters': deepcopy(view['parameters']),
                      'capture_sha256': digest(encoded(result['capture'])), 'snapshot': deepcopy(registry.snapshot),
                      'publication_status': 'not-provided-by-fina_mainbz', 'error': view['error']},
           'field_contract': {k: SPECS['fina_mainbz'][2][k] for k in FIELDS},
           'profit_checks': {k: consistency[k] for k in ('matched', 'different', 'not-reported', 'unavailable')},
           'signed_sums': groups, 'income_reconciliation': _income(income_result, registry, ts_code, period, cut, groups),
           'limitations': list(LIMITATIONS), 'network_requests': 0, 'records_written': False,
           'issuer_roles': [], 'net_benefit': None, 'research_executed': False, 'entries': entries}
    if income_selection_error is not None:
        out['income_reconciliation'] = {'status': 'unavailable', 'reason_code': income_selection_error, 'checks': []}
    out['view_version'] = digest(encoded(out))
    out.update(query={'item': item}, entries=[e for e in entries if item is None or e['raw'].get('bz_item') == item])
    return out


def public_summary(view):
    """Counts only: no row labels, values, source-derived hashes or response identifiers."""
    reconciliation = view['income_reconciliation']
    return {'schema': 'main-business-diagnostic-summary-v1', 'as_of': view['as_of'],
            'status': view['status'], 'scope': deepcopy(view['scope']),
            'query_key': view['source']['query_key'], 'source_error': view['source']['error'],
            'profit_checks': deepcopy(view['profit_checks']),
            'income_reconciliation': {'status': reconciliation['status'],
                'reason_code': reconciliation.get('reason_code'), 'checks': [
                {'component_field': c['component_field'], 'control_field': c['control_field'], 'status': c['status']}
                for c in reconciliation['checks']]},
            'limitations': list(LIMITATIONS), 'network_requests': 0, 'records_written': False,
            'issuer_roles_created': 0, 'source_values_exported': False}
