"""Closed execution counters from the original capture; never a recovery permit."""
from __future__ import annotations

HEADER_STATES = frozenset({'valid', 'missing', 'invalid', 'withheld'})
DECISIONS = frozenset({'response', 'retry', 'not-retryable', 'retry-after-unusable',
                       'attempt-limit', 'wait-budget', 'run-deadline'})
MAX_HEADER_NUMBER = 999_999_999_999  # All original 12-digit waits/counts, without clamping.


def _number(value, maximum=MAX_HEADER_NUMBER):
    return value if type(value) is int and 0 <= value <= maximum else None


def _positive(value, maximum):
    return value if type(value) is int and 1 <= value <= maximum else None


def _measurement(value, field):
    value = value if isinstance(value, dict) else {}
    state = value.get('state')
    if not isinstance(state, str) or state not in HEADER_STATES:
        state = 'not-recorded'
    number = _number(value.get(field)) if state == 'valid' else None
    if state == 'valid' and number is None:
        state = 'invalid'
    return {'state': state, field: number}


def _headers(attempt):
    recorded = attempt.get('headers')
    recorded = recorded if isinstance(recorded, dict) else {}
    # Old raw captures may contain a real Retry-After fact but no new diagnostics.
    # Absence is not the same fact as an explicitly observed missing header.
    failure = attempt.get('failure')
    failure = failure if isinstance(failure, dict) else {}
    status = failure.get('http_status')
    old_response = 'headers' not in attempt and type(status) is int and 100 <= status <= 599
    wait = recorded.get('retry_after', {'state': failure.get('retry_after_state'),
                                       'seconds': failure.get('retry_after_seconds')} if old_response else None)
    return {'retry_after': _measurement(wait, 'seconds'),
            **{name: _measurement(recorded.get(name), 'remaining')
               for name in ('key_remaining', 'ip_remaining')}}


def _decision(value):
    value = value if isinstance(value, dict) else {}
    reason = value.get('reason')
    known = isinstance(reason, str) and reason in DECISIONS
    return {'reason': reason if known else 'not-recorded',
            'required_wait_seconds': _number(value.get('required_wait_seconds')) if known else None}


def retry_diagnostics(capture):
    """Keep the last observed headers/failure and the wrapper's actual decision.

    Page numbers are request ordinals, not source row positions. The final request
    may have no attempts (deadline), or a response later rejected by the reader.
    Neither success here nor a remaining count changes query/recovery admission.
    """
    def sequence(value):
        return value if isinstance(value, list) else []

    execution = capture.get('request_execution')
    execution = execution if isinstance(execution, dict) else {}
    final_page = _positive(execution.get('page_number'), 100)
    groups = [(i, sequence(page.get('attempts'))) for i, page in
              enumerate(sequence(capture.get('pages')), 1) if isinstance(page, dict)]
    terminal = sequence(capture.get('attempts'))
    # A table/count rejection after appending a page keeps the same attempts in
    # both legacy capture fields. Count that original request only once.
    if terminal and not any(page == final_page and attempts == terminal for page, attempts in groups):
        groups.append((final_page, terminal))
    records = [(page, a, attempts[n + 1:]) for page, attempts in groups
               for n, a in enumerate(attempts) if isinstance(a, dict)]

    def location(page, attempt):
        return {'page_number': _positive(page, 100), 'attempt': _positive(attempt.get('attempt'), 20)}

    last_response = last_failure = None
    waits = [_number(a.get('completed_wait_seconds'), 3600) for _, a, _ in records]
    for page, attempt, later in reversed(records):
        if last_response is None and isinstance(attempt.get('headers'), dict):
            last_response = {**location(page, attempt), **_headers(attempt)}
        if last_failure is None and isinstance(attempt.get('error'), str):
            last_failure = {**location(page, attempt), **_headers(attempt),
                'retry_decision': _decision(attempt.get('retry_decision')),
                'completed_wait_seconds': _number(attempt.get('completed_wait_seconds'), 3600),
                'request_recovered': any(isinstance(a, dict) and 'error' in a and a['error'] is None
                                         for a in later)}
    return {'schema': 'market-retry-diagnostics-v1',
            'recorded_attempts': len(records) if final_page is not None else None,
            'completed_wait_seconds': sum(waits) if final_page is not None and all(w is not None for w in waits) else None,
            'last_response': last_response, 'last_failure': last_failure,
            'last_request': {'page_number': final_page, 'retry_decision': _decision(execution.get('retry_decision'))}}
