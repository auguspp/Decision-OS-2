"""Execution checkpoints for the existing registered capture and retry owner.

No storage, network client or authenticity claim lives here. A trusted host must
read the exact retained state and reconcile unknown writes before resuming it.
The synchronous writer owns persistence. Reading envelopes with empty attempt
lists are deliberately insufficient to restore an execution budget.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from decision_kernel.domain.records import RecordError, encoded, require
from decision_kernel.infrastructure.relay_failure import KNOWN_ERRORS

CHECKPOINT_SCHEMA = 'relay-capture-checkpoint-v1'
POLICY = {'max_attempts': 4, 'max_wait_seconds': 180}
DECISIONS = frozenset({'response', 'retry', 'not-retryable', 'retry-after-unusable',
    'attempt-limit', 'wait-budget', 'run-deadline', 'work-unit-complete'})
PHASES = frozenset({'ready', 'in-flight', 'waiting', 'response', 'paused', 'failed'})
STOP_CLASSES = frozenset({'auth-or-entitlement', 'rate-limit', 'source-unavailable'})
FAILURE_CLASSES = frozenset({'auth-or-entitlement', 'rate-limit', 'source-unavailable',
    'request-rejected', 'temporary-upstream', 'temporary-gateway', 'http-unclassified',
    'tls-certificate', 'tls', 'dns-temporary', 'dns', 'timeout', 'incomplete-read',
    'connection', 'http-protocol', 'transport-unclassified'})
ERROR = re.compile(r'[A-Z][A-Z0-9_]{0,95}\Z')


class CheckpointWriteError(RuntimeError):
    """A persistence failure is not a source failure or permission to retry."""


class CheckpointStateError(RuntimeError):
    """Control validation failed before the injected writer was attempted."""


def checkpoint_time(value):
    require(isinstance(value, str) and 0 < len(value) <= 64, 'RELAY_CHECKPOINT_CLOCK')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(stamp.utcoffset() is not None, 'RELAY_CHECKPOINT_CLOCK')
        return stamp.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RecordError('RELAY_CHECKPOINT_CLOCK') from None


def not_before(received_at, seconds):
    require(type(seconds) is int and 0 <= seconds <= POLICY['max_wait_seconds'],
            'RELAY_CHECKPOINT_WAIT')
    try:
        return (checkpoint_time(received_at) + timedelta(seconds=seconds)).isoformat()
    except OverflowError:
        raise RecordError('RELAY_CHECKPOINT_CLOCK') from None


def pause_reason(callback):
    value = callback() if callback is not None else None
    require(value is None or type(value) is bool or value in ('WORK_UNIT_COMPLETE', 'STORAGE_BUDGET'),
            'RELAY_CHECKPOINT_PAUSE_INVALID')
    return 'WORK_UNIT_COMPLETE' if value is True else value if isinstance(value, str) else None


def _number(value, maximum, code='RELAY_CHECKPOINT_BUDGET'):
    require(type(value) is int and 0 <= value <= maximum, code)


def _decision(value):
    require(isinstance(value, dict) and set(value) == {'reason', 'required_wait_seconds'}
            and isinstance(value['reason'], str) and value['reason'] in DECISIONS, 'RELAY_CHECKPOINT_DECISION')
    wait = value['required_wait_seconds']
    require(wait is None or type(wait) is int and 0 <= wait < 10**12,
            'RELAY_CHECKPOINT_DECISION')
    if value['reason'] in ('response', 'not-retryable', 'retry-after-unusable', 'work-unit-complete'):
        require(wait is None, 'RELAY_CHECKPOINT_DECISION')


def _headers(value):
    if value is None:
        return
    require(isinstance(value, dict) and set(value) == {'retry_after', 'key_remaining', 'ip_remaining'},
            'RELAY_CHECKPOINT_HEADERS')
    for key, field in (('retry_after', 'seconds'), ('key_remaining', 'remaining'), ('ip_remaining', 'remaining')):
        part = value[key]
        require(isinstance(part, dict) and set(part) == {'state', field}
                and part['state'] in ('valid', 'missing', 'invalid', 'withheld'), 'RELAY_CHECKPOINT_HEADERS')
        require(part[field] is None if part['state'] != 'valid' else
                type(part[field]) is int and 0 <= part[field] < 10**12, 'RELAY_CHECKPOINT_HEADERS')


def _transport(value):
    require(isinstance(value, dict) and set(value) == {'http_status', 'x_request_id', 'x_cache', 'wire_sha256'}
            and type(value['http_status']) is int and value['http_status'] == 200
            and isinstance(value['wire_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', value['wire_sha256']),
            'RELAY_CHECKPOINT_TRANSPORT')
    for field in ('x_request_id', 'x_cache'):
        require(value[field] is None or isinstance(value[field], str)
                and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value[field]), 'RELAY_CHECKPOINT_TRANSPORT')


def _failure(value):
    if value is None:
        return
    required = {'classification', 'http_status', 'retry_after_state', 'retry_after_seconds'}
    allowed = required | {'business_error', 'error_body_state', 'received_at', 'x_request_id', 'x_cache'}
    require(isinstance(value, dict) and required <= set(value) <= allowed
            and isinstance(value['classification'], str) and value['classification'] in FAILURE_CLASSES,
            'RELAY_CHECKPOINT_FAILURE')
    require(value['http_status'] is None or type(value['http_status']) is int
            and 100 <= value['http_status'] <= 599, 'RELAY_CHECKPOINT_FAILURE')
    require(value['retry_after_state'] in ('valid', 'missing', 'invalid', 'withheld'), 'RELAY_CHECKPOINT_FAILURE')
    wait = value['retry_after_seconds']
    require(wait is None if value['retry_after_state'] != 'valid' else
            type(wait) is int and 0 <= wait < 10**12, 'RELAY_CHECKPOINT_FAILURE')
    require(value.get('business_error') is None or isinstance(value['business_error'], str)
            and value['business_error'] in KNOWN_ERRORS,
            'RELAY_CHECKPOINT_FAILURE')
    require(value.get('error_body_state') is None or value['error_body_state'] in
            ('empty', 'oversized', 'withheld', 'parsed', 'invalid-json', 'unreadable', 'unsupported-encoding'),
            'RELAY_CHECKPOINT_FAILURE')
    if 'received_at' in value:
        checkpoint_time(value['received_at'])
    for field in ('x_request_id', 'x_cache'):
        require(value.get(field) is None or isinstance(value[field], str)
                and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', value[field]), 'RELAY_CHECKPOINT_FAILURE')


def validate_attempts(attempts, *, complete=False):
    """Check original execution records, including allocated cross-session waits."""
    from decision_kernel.infrastructure.relay_retry import business_retry_delay
    require(isinstance(attempts, list) and len(attempts) <= 4 and len(encoded(attempts)) <= 65536,
            'RELAY_CHECKPOINT_ATTEMPTS')
    common = {'attempt', 'requested_at', 'received_at', 'error', 'headers', 'retry_decision',
              'completed_wait_seconds', 'checkpoint_wait_seconds', 'checkpoint_not_before'}
    budget, previous = 0, None
    for index, item in enumerate(attempts, 1):
        require(isinstance(item, dict) and type(item.get('attempt')) is int and item['attempt'] == index,
                'RELAY_CHECKPOINT_ATTEMPTS')
        success = item.get('error') is None
        expected = common | ({'transport'} if success else {'failure', 'wait_before_next_attempt'})
        require(set(item) == expected, 'RELAY_CHECKPOINT_ATTEMPTS')
        started, received = checkpoint_time(item['requested_at']), checkpoint_time(item['received_at'])
        require(started <= received and (previous is None or previous <= started), 'RELAY_CHECKPOINT_CLOCK')
        previous = received
        _headers(item['headers']); _decision(item['retry_decision'])
        used, slept = item['checkpoint_wait_seconds'], item['completed_wait_seconds']
        _number(used, 180); _number(slept, used)
        if success:
            _transport(item['transport'])
            require(index == len(attempts) and item['retry_decision']['reason'] == 'response'
                    and used == slept == 0 and item['checkpoint_not_before'] is None,
                    'RELAY_CHECKPOINT_ATTEMPTS')
        else:
            require(isinstance(item['error'], str) and ERROR.fullmatch(item['error']), 'RELAY_CHECKPOINT_FAILURE')
            _failure(item['failure'])
            delay = business_retry_delay(item['failure'])
            reason = item['retry_decision']['reason']
            require(item['retry_decision']['required_wait_seconds'] == delay, 'RELAY_CHECKPOINT_DECISION')
            if delay is None:
                expected_reason = ('retry-after-unusable' if isinstance(item['failure'], dict)
                    and item['failure']['classification'] in {
                        'temporary-upstream', 'temporary-gateway', 'timeout', 'connection',
                        'dns-temporary', 'incomplete-read', 'rate-limit'} else 'not-retryable')
                require(reason == expected_reason, 'RELAY_CHECKPOINT_DECISION')
            elif index == 4:
                require(reason == 'attempt-limit', 'RELAY_CHECKPOINT_DECISION')
            elif budget + delay > 180:
                require(reason == 'wait-budget', 'RELAY_CHECKPOINT_DECISION')
            else:
                require(reason in ('retry', 'run-deadline'), 'RELAY_CHECKPOINT_DECISION')
            charged = delay if reason in ('retry', 'run-deadline') else 0
            require(used == charged and item['wait_before_next_attempt'] == (delay if reason == 'retry' else None),
                    'RELAY_CHECKPOINT_WAIT')
            require(item['checkpoint_not_before'] == (not_before(item['received_at'], used) if used else None),
                    'RELAY_CHECKPOINT_WAIT')
            if index < len(attempts):
                require(used >= 30 and reason in ('retry', 'run-deadline'), 'RELAY_CHECKPOINT_ATTEMPTS')
                if slept < used:
                    require(checkpoint_time(attempts[index]['requested_at']) >= checkpoint_time(item['checkpoint_not_before']),
                            'RELAY_CHECKPOINT_RETRY_AFTER')
        budget += used
        require(budget <= 180, 'RELAY_CHECKPOINT_BUDGET')
    require(not complete or bool(attempts) and attempts[-1]['error'] is None, 'RELAY_CHECKPOINT_ATTEMPTS')
    return budget


def new_retry_state():
    return {'phase': 'ready', 'attempts': [], 'reserved_attempt': None, 'wait_budget_seconds': 0,
            'pending_wait_seconds': None, 'not_before': None, 'decision': None}


def validate_retry_state(value):
    require(isinstance(value, dict) and set(value) == set(new_retry_state())
            and isinstance(value['phase'], str) and value['phase'] in PHASES,
            'RELAY_CHECKPOINT_RETRY_STATE')
    budget = validate_attempts(value['attempts'])
    require(type(value['wait_budget_seconds']) is int and value['wait_budget_seconds'] == budget,
            'RELAY_CHECKPOINT_BUDGET')
    phase, attempts = value['phase'], value['attempts']
    if value['decision'] is not None:
        _decision(value['decision'])
    if phase == 'in-flight':
        require(type(value['reserved_attempt']) is int and value['reserved_attempt'] == len(attempts) + 1
                and value['reserved_attempt'] <= 4, 'RELAY_CHECKPOINT_RESERVATION')
    else:
        require(value['reserved_attempt'] is None, 'RELAY_CHECKPOINT_RESERVATION')
    pending = value['pending_wait_seconds']
    if pending is not None:
        require(phase in ('waiting', 'paused') and attempts and attempts[-1]['error'] is not None
                and type(pending) is int and pending == attempts[-1]['checkpoint_wait_seconds'] >= 30
                and value['not_before'] == attempts[-1]['checkpoint_not_before']
                and attempts[-1]['completed_wait_seconds'] == 0, 'RELAY_CHECKPOINT_WAIT')
    else:
        require(value['not_before'] is None and phase != 'waiting', 'RELAY_CHECKPOINT_WAIT')
    if phase == 'response':
        require(attempts and attempts[-1]['error'] is None
                and value['decision'] == attempts[-1]['retry_decision'], 'RELAY_CHECKPOINT_RETRY_STATE')
    elif phase == 'failed':
        require(attempts and attempts[-1]['error'] is not None and value['decision'] == attempts[-1]['retry_decision']
                and value['decision']['reason'] not in ('retry', 'run-deadline'), 'RELAY_CHECKPOINT_RETRY_STATE')
    elif phase == 'paused':
        require(value['decision'] is not None and value['decision']['reason'] in ('run-deadline', 'work-unit-complete'),
                'RELAY_CHECKPOINT_RETRY_STATE')
        require(not attempts or len(attempts) < 4 and attempts[-1]['error'] is not None
                and attempts[-1]['retry_decision']['reason'] in ('retry', 'run-deadline'),
                'RELAY_CHECKPOINT_RETRY_STATE')
    else:
        require(not attempts or attempts[-1]['error'] is not None
                and attempts[-1]['retry_decision']['reason'] in ('retry', 'run-deadline')
                and len(attempts) < 4, 'RELAY_CHECKPOINT_RETRY_STATE')
    return deepcopy(value)


def _check_capture_checkpoint(value, request, registry, *, max_pages, policy=None):
    """Check the complete state without copying tables for a discarded return value."""
    from decision_kernel.application.research_sources import _inspect_result
    require(policy is None or vars(policy) == POLICY, 'RELAY_CHECKPOINT_POLICY_CHANGED')
    require(isinstance(value, dict) and set(value) == {'schema', 'capture', 'control'}
            and value['schema'] == CHECKPOINT_SCHEMA, 'RELAY_CHECKPOINT_SCHEMA')
    control, capture = value['control'], value['capture']
    require(isinstance(control, dict) and set(control) == {'request', 'max_pages', 'policy', 'state',
                'page_number', 'retry', 'pause_reason'} and len(encoded(control)) <= 131072,
            'RELAY_CHECKPOINT_CONTROL')
    require(control['request'] == request and control['policy'] == POLICY
            and isinstance(control['policy'], dict) and set(control['policy']) == set(POLICY)
            and all(type(v) is int for v in control['policy'].values())
            and type(max_pages) is int and 1 <= max_pages <= 100
            and type(control['max_pages']) is int and control['max_pages'] == max_pages,
            'RELAY_CHECKPOINT_SCOPE_CHANGED')
    require(control['state'] in ('ready', 'executing', 'paused', 'terminal', 'complete')
            and control['pause_reason'] in (None, 'WORK_UNIT_COMPLETE', 'STORAGE_BUDGET', 'RUN_DEADLINE', 'WAIT_INTERRUPTED'),
            'RELAY_CHECKPOINT_CONTROL')
    required = {'schema', 'api', 'parameters', 'query_key', 'pages', 'complete', 'error',
                'snapshot', 'source_identity', 'coverage'}
    require(isinstance(capture, dict) and required <= set(capture) <= required | {
        'request_execution', 'failure', 'pending_retry', 'attempts', 'next_parameters'}, 'RELAY_CHECKPOINT_CAPTURE')
    require(capture['snapshot'] == registry.snapshot, 'RELAY_CHECKPOINT_REGISTRY_CHANGED')
    require(capture['source_identity'] == 'THIRD_PARTY_RELAY_NOT_ISSUER'
            and capture['coverage'] == 'REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS',
            'RELAY_CHECKPOINT_CAPTURE')
    if 'failure' in capture:
        _failure(capture['failure'])
    if capture.get('pending_retry') is not None:
        pending = capture['pending_retry']
        require(isinstance(pending, dict) and set(pending) == {'reason', 'retry_after_seconds'}
                and pending['reason'] in ('run-deadline', 'attempt-or-wait-bound', 'work-unit-complete'),
                'RELAY_CHECKPOINT_DECISION')
        require(pending['retry_after_seconds'] is None or type(pending['retry_after_seconds']) is int
                and 0 <= pending['retry_after_seconds'] < 10**12, 'RELAY_CHECKPOINT_DECISION')
    if 'request_execution' in capture:
        execution = capture['request_execution']
        require(isinstance(execution, dict) and set(execution) == {'page_number', 'retry_decision'}
                and type(execution['page_number']) is int and 1 <= execution['page_number'] <= max_pages,
                'RELAY_CHECKPOINT_PAGE')
        if execution['retry_decision'] is not None:
            _decision(execution['retry_decision'])
    view = _inspect_result({'case_id': 'relay-capture-checkpoint', 'use': request, 'capture': capture}, registry)
    pages = capture['pages']
    require(len(pages) <= max_pages and sum(len(encoded(p['table'])) for p in pages) <= 8 * 1024 * 1024,
            'RELAY_CHECKPOINT_CAPTURE_BOUND')
    for page in pages:
        validate_attempts(page['attempts'], complete=True)
        # This execution owner validates the original attempts before binding
        # the finite projection. Ordinary source readers discard diagnostics.
        transport = {k: v for k, v in page['transport'].items() if k != 'request_clock'}
        last = page['attempts'][-1]
        require(last['transport'] == transport, 'RELAY_CHECKPOINT_TRANSPORT')
        if 'request_clock' in page['transport']:
            require({k: last[k] for k in ('requested_at', 'received_at')}
                    == page['transport']['request_clock'], 'SOURCE_REQUEST_CLOCK_INVALID')
    number = control['page_number']
    require(type(number) is int and 1 <= number <= max_pages + 1, 'RELAY_CHECKPOINT_PAGE')
    retry = validate_retry_state(control['retry']) if control['retry'] is not None else None
    state = control['state']
    if state == 'complete':
        require(view['complete'] and number == len(pages) and retry is not None and retry['phase'] == 'response'
                and retry['attempts'] == pages[-1]['attempts'] and control['pause_reason'] is None,
                'RELAY_CHECKPOINT_COMPLETENESS')
    else:
        require(not view['complete'], 'RELAY_CHECKPOINT_COMPLETENESS')
        if state in ('ready', 'executing', 'paused'):
            require(number == len(pages) + 1 and number <= max_pages
                    and all(len(p['table']['items']) == int(request['parameters']['limit']) for p in pages),
                    'RELAY_CHECKPOINT_PAGE')
        if state == 'ready':
            require(capture['error'] is None and retry is None and control['pause_reason'] is None,
                    'RELAY_CHECKPOINT_CONTROL')
        elif state == 'executing':
            require(retry is not None and retry['phase'] in ('in-flight', 'waiting', 'ready', 'response')
                    and control['pause_reason'] is None, 'RELAY_CHECKPOINT_CONTROL')
        elif state == 'paused':
            require(control['pause_reason'] is not None and (retry is None or retry['phase'] in ('paused', 'waiting')),
                    'RELAY_CHECKPOINT_CONTROL')
        else:
            require(capture['error'] is not None and number in (max(1, len(pages)), len(pages) + 1)
                    and control['pause_reason'] is None, 'RELAY_CHECKPOINT_CONTROL')
    if 'attempts' in capture:
        require(retry is not None and capture['attempts'] == retry['attempts'], 'RELAY_CHECKPOINT_ATTEMPTS_CHANGED')
    if 'next_parameters' in capture:
        offset = int(request['parameters'].get('offset', 0)) + sum(len(p['table']['items']) for p in pages)
        if state == 'terminal' and number == len(pages) and pages:
            offset = pages[-1]['offset']
        expected = {**request['parameters'], 'offset': str(offset)} if offset else dict(request['parameters'])
        require(capture['next_parameters'] == expected, 'RELAY_CHECKPOINT_OFFSET_CHANGED')
    return value


def validate_capture_checkpoint(value, request, registry, *, max_pages, policy=None):
    """Validate all supplied state and return a detached copy, not authentication."""
    return deepcopy(_check_capture_checkpoint(value, request, registry, max_pages=max_pages, policy=policy))


def validate_capture_transition(previous, current, request, registry, *, max_pages, policy=None):
    """A retained predecessor cannot lose attempts, reservations or accepted pages.

    Callers authenticate the predecessor reference. This checks the actual
    next state, rather than accepting an independently plausible fresh budget.
    """
    old = _check_capture_checkpoint(previous, request, registry, max_pages=max_pages, policy=policy)
    new = _check_capture_checkpoint(current, request, registry, max_pages=max_pages, policy=policy)
    return deepcopy(_check_capture_transition(old, new))


def _check_capture_transition(old, new):
    """Check an edge between states already fully checked by the same caller.

    This private, read-only check neither copies its inputs nor grants validity
    to a standalone state. Public transition validation always checks both states.
    """
    if encoded(old) == encoded(new):
        return new
    a, b = old['control'], new['control']
    require(a['state'] not in ('complete', 'terminal'), 'RELAY_CHECKPOINT_TERMINAL_RESTART')
    old_pages, new_pages = old['capture']['pages'], new['capture']['pages']
    count = len(old_pages)
    require(count <= len(new_pages) <= count + 1 and new_pages[:count] == old_pages,
            'RELAY_CHECKPOINT_PAGES_CHANGED')
    before, after = a['retry'], b['retry']
    if len(new_pages) == count + 1:
        require(before is not None and before['phase'] == 'in-flight', 'RELAY_CHECKPOINT_RESERVATION_REQUIRED')
        attempts = new_pages[-1]['attempts']
        require(len(attempts) == len(before['attempts']) + 1
                and attempts[:-1] == before['attempts'] and attempts[-1]['attempt'] == before['reserved_attempt']
                and attempts[-1]['error'] is None, 'RELAY_CHECKPOINT_ATTEMPTS_CHANGED')
        require(b['state'] in ('ready', 'complete', 'terminal'), 'RELAY_CHECKPOINT_TRANSITION')
        return new
    require(a['page_number'] == b['page_number'], 'RELAY_CHECKPOINT_PAGE')
    if before is None:
        require(after is None and b['state'] == 'paused' or after is not None and not after['attempts']
                and (after['phase'] == 'in-flight' and b['state'] == 'executing'
                     or after['phase'] == 'paused' and b['state'] == 'paused'), 'RELAY_CHECKPOINT_TRANSITION')
        return new
    require(after is not None, 'RELAY_CHECKPOINT_BUDGET_RESET')
    if before['phase'] == 'in-flight':
        require(after['phase'] in ('waiting', 'paused', 'failed', 'response')
                and len(after['attempts']) == len(before['attempts']) + 1
                and after['attempts'][:-1] == before['attempts']
                and after['attempts'][-1]['attempt'] == before['reserved_attempt'],
                'RELAY_CHECKPOINT_RESERVATION_UNSETTLED')
        return new
    require(before['phase'] in ('ready', 'waiting', 'paused'), 'RELAY_CHECKPOINT_NOT_RESUMABLE')
    require(len(after['attempts']) == len(before['attempts'])
            and after['wait_budget_seconds'] == before['wait_budget_seconds'], 'RELAY_CHECKPOINT_BUDGET_RESET')
    if before['attempts']:
        require(after['attempts'][:-1] == before['attempts'][:-1], 'RELAY_CHECKPOINT_ATTEMPTS_CHANGED')
        original, later = before['attempts'][-1], after['attempts'][-1]
        require({k: v for k, v in original.items() if k != 'completed_wait_seconds'} ==
                {k: v for k, v in later.items() if k != 'completed_wait_seconds'}
                and later['completed_wait_seconds'] >= original['completed_wait_seconds'],
                'RELAY_CHECKPOINT_ATTEMPTS_CHANGED')
    same_attempts = after['attempts'] == before['attempts']
    if after['phase'] == 'in-flight':
        require(before['pending_wait_seconds'] is None and same_attempts, 'RELAY_CHECKPOINT_WAIT')
    elif after['phase'] == 'paused':
        require(same_attempts and after['pending_wait_seconds'] == before['pending_wait_seconds']
                and after['not_before'] == before['not_before'], 'RELAY_CHECKPOINT_WAIT')
    elif after['phase'] == 'waiting':
        require(before['pending_wait_seconds'] is not None and same_attempts
                and after['pending_wait_seconds'] == before['pending_wait_seconds']
                and after['not_before'] == before['not_before'], 'RELAY_CHECKPOINT_WAIT')
    else:
        require(after['phase'] == 'ready' and before['pending_wait_seconds'] is not None
                and after['pending_wait_seconds'] is None, 'RELAY_CHECKPOINT_TRANSITION')
    return new


class CaptureCheckpoint:
    """Pure control plus one injected synchronous persistence function."""

    def __init__(self, request, registry, session, capture, *, max_pages, value=None, writer=None):
        require(vars(session.policy) == POLICY and type(max_pages) is int and 1 <= max_pages <= 100,
                'RELAY_CHECKPOINT_POLICY_CHANGED')
        require(writer is None or callable(writer), 'RELAY_CHECKPOINT_WRITER_REQUIRED')
        self.request, self.registry, self.session = request, registry, session
        self.max_pages, self.writer = max_pages, writer
        self.control = {'request': deepcopy(request), 'max_pages': max_pages, 'policy': dict(POLICY),
            'state': 'ready', 'page_number': 1, 'retry': None, 'pause_reason': None}
        self.capture = capture
        self.previous = None
        if value is not None:
            checked = validate_capture_checkpoint(value, request, registry, max_pages=max_pages, policy=session.policy)
            self.previous = deepcopy(checked)
            self.capture, self.control = checked['capture'], checked['control']
            retry = self.control['retry']
            require(self.control['state'] in ('complete', 'terminal') or not retry
                    or retry['phase'] not in ('in-flight', 'response'), 'RELAY_CHECKPOINT_INFLIGHT')
            if self.control['state'] == 'complete':
                # A saved successful response is committed to its exact last page.
                pass
            elif self.control['state'] != 'terminal':
                require(self.control['state'] in ('ready', 'paused') or retry and retry['phase'] in ('waiting', 'ready'),
                        'RELAY_CHECKPOINT_NOT_RESUMABLE')
                require(not retry or not retry['attempts'] or
                        (retry['attempts'][-1].get('failure') or {}).get('classification') not in STOP_CLASSES,
                        'RELAY_CHECKPOINT_GLOBAL_STOP')
                previous = ([self.capture['pages'][-1]['captured_at']] if self.capture['pages'] else [])
                previous += [retry['attempts'][-1]['received_at']] if retry and retry['attempts'] else []
                require(not previous or checkpoint_time(session.clock()) >= max(checkpoint_time(p) for p in previous),
                        'RELAY_CHECKPOINT_CLOCK')
                if retry and retry['attempts'] and retry['pending_wait_seconds'] is None:
                    last = retry['attempts'][-1]
                    require(last['completed_wait_seconds'] >= last['checkpoint_wait_seconds']
                            or checkpoint_time(session.clock()) >= checkpoint_time(last['checkpoint_not_before']),
                            'RELAY_CHECKPOINT_RETRY_AFTER')
                for key in ('error', 'failure', 'pending_retry', 'attempts', 'next_parameters'):
                    if key == 'error':
                        self.capture[key] = None
                    else:
                        self.capture.pop(key, None)
        require(self.control['state'] in ('complete', 'terminal') or callable(writer), 'RELAY_CHECKPOINT_WRITER_REQUIRED')

    def persist(self):
        value = {'schema': CHECKPOINT_SCHEMA, 'capture': deepcopy(self.capture), 'control': deepcopy(self.control)}
        try:
            if self.previous is None:
                validate_capture_checkpoint(value, self.request, self.registry,
                                            max_pages=self.max_pages, policy=self.session.policy)
            else:
                validate_capture_transition(self.previous, value, self.request, self.registry,
                                            max_pages=self.max_pages, policy=self.session.policy)
        except Exception:
            raise CheckpointStateError('RELAY_CHECKPOINT_STATE_INVALID') from None
        try:
            self.writer(deepcopy(value))
        except Exception:
            raise CheckpointWriteError('RELAY_CHECKPOINT_WRITE_UNSETTLED') from None
        self.previous = deepcopy(value)

    def retry_progress(self, value):
        self.control.update(state='executing', retry=deepcopy(value), pause_reason=None)
        # A success is durably committed together with its projected page. Until
        # then the previous in-flight reservation remains the safe restart state.
        if value['phase'] in ('response', 'failed'):
            return
        if value['phase'] == 'paused':
            # The capture owner attaches the exact stop error/next offset before
            # persisting this known zero-new-HTTP stop.
            return
        self.persist()

    def accepted(self, *, complete):
        self.control.update(state='complete' if complete else 'ready',
            page_number=len(self.capture['pages']) if complete else len(self.capture['pages']) + 1,
            pause_reason=None)
        if not complete:
            self.control['retry'] = None
        self.persist()

    def failed(self):
        retry = self.control['retry']
        error = self.capture['error']
        decision = (retry or {}).get('decision') or {}
        reason = ('STORAGE_BUDGET' if error == 'RELAY_STORAGE_BUDGET' else
                  'WORK_UNIT_COMPLETE' if error == 'RELAY_WORK_UNIT_COMPLETE' else
                  'RUN_DEADLINE' if decision.get('reason') == 'run-deadline' else
                  'WAIT_INTERRUPTED' if retry and retry['phase'] == 'waiting' else None)
        self.control.update(state='paused' if reason else 'terminal', pause_reason=reason)
        self.persist()

    def pause(self, reason, page_parameters):
        self.capture.update(error='RELAY_' + reason, next_parameters=deepcopy(page_parameters))
        self.control.update(state='paused', pause_reason=reason)
        self.persist()
