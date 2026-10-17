"""Date-local evidence for original stock histories; no imputation or source I/O."""
from collections import defaultdict

from decision_kernel.application.market_relay_inputs import _day, _lineage, _require, _rows, _view
from decision_kernel.application.market_security_inputs import _within_listing
from decision_kernel.application.market_suspension_inputs import (
    EXPLANATIONS, MISSING_EXPLANATIONS, SUSPENSION_FIELDS, _explanation, _suspension_group,
)
from decision_kernel.application.market_session_inputs import _reason
from decision_kernel.observations.market_daily import MarketInputError


def period_suspensions(result, *, registry, code, start, end, sessions):
    """One exact security/range, no S-only filter, no carry-forward across dates."""
    view = _view(result, registry, 'suspend_d',
        {'ts_code': code, 'start_date': start, 'end_date': end}, SUSPENSION_FIELDS)
    _require(view['row_count'] <= 10000, 'SUSPENSION_ROW_BOUND')
    groups = defaultdict(list)
    for page_no, page in enumerate(view['pages'], 1):
        for row_no, row in enumerate(_rows({'pages': [page]}), 1):
            _require(row.get('ts_code') == code, 'SUSPENSION_IDENTITY')
            day = _day(row.get('trade_date'))
            _require(day in sessions, 'SUSPENSION_DATE')
            groups[day].append((row, {'page': page_no, 'row': row_no}))
    entries = {day: _suspension_group(group) for day, group in sorted(groups.items())}
    return {'entries': entries, 'lineage': _lineage(view), 'row_count': view['row_count'],
            'quarantined_day_count': sum(e['status'] == 'quarantined' for e in entries.values())}


def explain_history_quotes(references, prices, qualifications, results, *, inspected_codes,
                           registry, start, end, sessions):
    """Diagnose supplied histories without changing any raw/qfq window or denominator.

    Uninspected master identities remain explicitly uninspected; avoid creating a
    huge matrix of invented unknown day records for thousands of unrequested codes.
    Evidence is scoped to its original query, day and source row positions.
    """
    entries, total = {}, dict.fromkeys(EXPLANATIONS, 0)
    for code in sorted(inspected_codes):
        reference, qualification = references[code], qualifications[code]
        if reference is None:
            entries[code] = {'status': 'reference-quarantined', 'days': [], 'counts': None,
                             'evidence_status': 'not-assessed', 'lineage': None}
            continue
        evidence, reason, state = None, None, 'not-supplied'
        if results.get(code) is not None:
            try:
                evidence = period_suspensions(results[code], registry=registry, code=code,
                    start=start, end=end, sessions=set(sessions))
            except MarketInputError as exc:
                state, reason = 'rejected', _reason(exc)
            else:
                state = 'checked'
        empty = {'status': 'input-unavailable' if evidence is None else 'no-record',
            'reason_code': reason, 'source_row_count': None if evidence is None else 0,
            'source_rows': [], 'all_day_confirmed': False}
        seen = {row['trade_date'] for row in prices.get(code, [])}
        counts, days = dict.fromkeys(EXPLANATIONS, 0), []
        for day in sessions:
            eligible = _within_listing(day, reference)
            quote_state = ('input-unavailable' if qualification['price']['status'] != 'checked'
                           else 'observed' if day in seen else 'missing')
            proof = empty if evidence is None else evidence['entries'].get(day, empty)
            explanation = _explanation({'availability': 'eligible' if eligible else 'outside-listing',
                                       'raw': {'status': quote_state}}, proof)
            days.append({'trade_date': day, 'quote_status': quote_state if eligible else 'outside-listing',
                         'status': explanation, 'suspension': proof})
            if eligible:
                counts[explanation] += 1
                total[explanation] += 1
        entries[code] = {'status': 'diagnosed', 'evidence_status': state, 'reason_code': reason,
            'lineage': evidence['lineage'] if evidence is not None else None,
            'evidence_quarantined_day_count': evidence['quarantined_day_count'] if evidence is not None else None,
            'eligible_session_count': sum(counts.values()), 'counts': counts,
            'missing_quote_count': (None if counts['quote-input-unavailable'] else
                sum(counts[k] for k in MISSING_EXPLANATIONS)), 'days': days}
    return {'schema': 'stock-history-quote-explanations-v1', 'scope_count': len(references),
        'inspected_count': len(inspected_codes), 'not_inspected_count': len(references) - len(inspected_codes),
        'counts': total, 'entries': entries, 'raw_qfq_reclassified': False,
        'limitations': ['DATE_LOCAL_S_SUPPORT_NOT_FULL_DAY_HALT_CERTIFICATION',
            'NO_HALT_CARRY_FORWARD_OR_PRICE_FILL; FACTOR_AND_WINDOW_RULES_UNCHANGED',
            'UNREQUESTED_SECURITIES_NOT_INSPECTED; CURRENT_MASTER_NOT_HISTORICAL_UNIVERSE',
            'SOURCE_ROW_POSITIONS_REQUIRE_ORIGINAL_AUTHORIZED_INPUT_OR_CUSTODY']}
