"""Same-input quote explanations, not imputed bars or a new suspension feed.

A date-matched S with no reported intraday window supports investigation of a
missing quote. It does not prove a full-day halt, source finality or a zero return.
"""
from collections import defaultdict
import re

from decision_kernel.application.market_relay_inputs import (
    EXCHANGES, _day, _lineage, _require, _rows, _view,
)
from decision_kernel.observations.market_daily import MarketInputError

SUSPENSION_FIELDS = {'ts_code', 'trade_date', 'suspend_type', 'suspend_timing'}
EXPLANATIONS = (
    'quote-observed', 'quote-suspension-conflict', 'quote-input-unavailable',
    'quote-quarantined', 'suspension-supported-missing', 'intraday-only-unexplained',
    'resumption-unexplained', 'no-suspension-record', 'suspension-input-unavailable',
    'suspension-evidence-quarantined',
)
MISSING_EXPLANATIONS = EXPLANATIONS[4:]


def _timing(value):
    if value is None or value == '':
        return 'not-reported'
    if not isinstance(value, str):
        return 'invalid'
    match = re.fullmatch(r'([0-2][0-9]):([0-5][0-9])-([0-2][0-9]):([0-5][0-9])', value)
    if match is None:
        return 'invalid'
    sh, sm, eh, em = map(int, match.groups())
    return 'intraday-window' if sh < 24 and eh < 24 and (sh, sm) < (eh, em) else 'invalid'


def session_suspensions(result, registry, day):
    """Full unfiltered S/R date slice; retain all row witnesses, never first/last.

    Wrong/unknown identities and dates reject the shared evidence. Multiple or
    invalid records isolate that code's explanation, not its original quote.
    Positions are one-based within the original captured table pages.
    """
    iso = _day(day)
    view = _view(result, registry, 'suspend_d', {'trade_date': day}, SUSPENSION_FIELDS)
    _require(view['row_count'] <= 10000, 'SUSPENSION_ROW_BOUND')
    groups = defaultdict(list)
    for page_no, page in enumerate(view['pages'], 1):
        for row_no, row in enumerate(_rows({'pages': [page]}), 1):
            code = row.get('ts_code')
            _require(isinstance(code, str) and re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code), 'SUSPENSION_IDENTITY')
            _require(_day(row.get('trade_date')) == iso, 'SUSPENSION_DATE')
            groups[code].append((row, {'page': page_no, 'row': row_no}))
    entries = {code: _suspension_group(group) for code, group in sorted(groups.items())}
    return {'entries': entries, 'row_count': view['row_count'], 'lineage': _lineage(view),
        'quarantined_count': sum(e['status'] == 'quarantined' for e in entries.values()),
        'outside_sh_sz_count': sum(c.endswith('.BJ') for c in entries)}


def _suspension_group(group):
    """One identity/day's original row witnesses; shared by day and period readers."""
    row = group[0][0]
    event, timing = row.get('suspend_type'), _timing(row.get('suspend_timing'))
    if len(group) != 1:
        state, reason = 'quarantined', 'MULTIPLE_SUSPENSION_RECORDS_NOT_ORDERED'
    elif event not in ('S', 'R') or timing == 'invalid':
        state, reason = 'quarantined', 'SUSPENSION_TYPE_OR_TIMING_INVALID'
    else:
        state = 'resumption' if event == 'R' else 'intraday-suspension' if timing == 'intraday-window' else 'suspension-no-window'
        reason = None
    return {'status': state, 'reason_code': reason,
        'source_row_count': len(group), 'source_rows': [p for _, p in group],
        'all_day_confirmed': False}


def _explanation(entry, evidence):
    if entry['availability'] != 'eligible':
        return 'not-eligible'
    raw = entry['raw']['status']
    if raw == 'input-unavailable':
        return 'quote-input-unavailable'
    if raw == 'quarantined':
        return 'quote-quarantined'
    state = evidence['status']
    if raw == 'observed':
        return 'quote-suspension-conflict' if state == 'suspension-no-window' else 'quote-observed'
    return {'suspension-no-window': 'suspension-supported-missing',
        'intraday-suspension': 'intraday-only-unexplained', 'resumption': 'resumption-unexplained',
        'no-record': 'no-suspension-record', 'input-unavailable': 'suspension-input-unavailable',
        'quarantined': 'suspension-evidence-quarantined'}[state]


def reconcile_suspensions(context, entries, result, *, registry, day):
    """Annotate newly built session output before paging; input captures untouched.

    The original scope/coverage/missing/factor counts and booleans are immutable
    to this diagnostic layer. Its counts cover the full eligible scope, not the
    first page. Missing/invalid evidence never certifies the absence of halts.
    """
    evidence = None
    query = {'status': 'not-supplied', 'reason_code': None}
    if result is not None:
        try:
            evidence = session_suspensions(result, registry, day)
        except (MarketInputError, ValueError) as exc:
            text = str(exc)
            query = {'status': 'rejected', 'reason_code': text if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', text) else 'SUSPENSION_INPUT_REJECTED'}
        else:
            query = {'status': 'checked', 'reason_code': None}
            context['source_lineage']['suspend_d'] = evidence['lineage']
    context['queries']['suspend_d'] = query
    counts = {v: dict.fromkeys(EXPLANATIONS, 0) if c['eligible_count'] is not None else None
              for v, c in context['by_exchange'].items()}
    empty = {'status': 'input-unavailable' if evidence is None else 'no-record',
        'reason_code': query['reason_code'], 'source_row_count': None if evidence is None else 0,
        'source_rows': [], 'all_day_confirmed': False}
    for entry in entries:
        proof = empty if evidence is None else evidence['entries'].get(entry['entity_id'], empty)
        status = _explanation(entry, proof)
        entry['quote_explanation'] = {'status': status, 'suspension': proof,
            'quote_witness': ('absence-in-complete-daily-capture' if entry['availability'] == 'eligible'
                              and entry['raw']['status'] == 'missing' else 'original-quote-status-unchanged')}
        if status != 'not-eligible':
            counts[entry['exchange']][status] += 1
    by_exchange = {}
    declared = {v: {e['entity_id'] for e in entries if e['exchange'] == v and e['availability'] not in
                   ('unreferenced', 'reference-unavailable')} for v in EXCHANGES}
    for venue, suffix in EXCHANGES.items():
        base, values = context['by_exchange'][venue], counts[venue]
        raw = base['raw_counts']
        missing_known = raw is not None and raw['input-unavailable'] == 0
        by_exchange[venue] = {'eligible_count': base['eligible_count'], 'explanation_counts': values,
            'missing_quote_count': raw['missing'] if missing_known else None,
            'missing_breakdown': {k: values[k] for k in MISSING_EXPLANATIONS} if missing_known else None,
            'unreferenced_suspension_count': (sum(c.endswith('.' + suffix) and c not in
                declared[venue] for c in evidence['entries'])
                if evidence is not None and base['scope_count'] is not None else None)}
    context['quote_explanations'] = {'schema': 'session-quote-explanations-v1',
        'evidence_status': query['status'], 'source_row_count': evidence['row_count'] if evidence is not None else None,
        'quarantined_evidence_count': evidence['quarantined_count'] if evidence is not None else None,
        'outside_sh_sz_count': evidence['outside_sh_sz_count'] if evidence is not None else None,
        'quarantined_sh_sz_count': (sum(not c.endswith('.BJ') and e['status'] == 'quarantined'
            for c, e in evidence['entries'].items()) if evidence is not None else None),
        'by_exchange': by_exchange, 'strict_coverage_reclassified': False,
        'limitations': ['S_WITHOUT_INTRADAY_WINDOW_SUPPORTS_NOT_PROVES_FULL_DAY_HALT',
            'INTRADAY_OR_RESUMPTION_DOES_NOT_EXCUSE_MISSING_DAILY',
            'EXACT_DAY_SOURCE_RECORDS_NOT_EXCHANGE_CERTIFICATION_OR_HISTORICAL_HALT_INFERENCE',
            'IDENTITIES_AND_ROW_LOCATIONS_REQUIRE_ORIGINAL_AUTHORIZED_INPUT_OR_CUSTODY']}
