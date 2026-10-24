"""Finite same-request business waits. No scheduler, alternate host, key or write retry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time

from decision_kernel.domain.records import RecordError, require
from decision_kernel.infrastructure.relay_http import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS, validate_request_timeout_seconds,
)

RETRYABLE_CLASSES = frozenset({'temporary-upstream', 'temporary-gateway', 'timeout',
                              'connection', 'dns-temporary', 'incomplete-read', 'rate-limit'})


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    max_wait_seconds: int = 180

    def __post_init__(self):
        require(type(self.max_attempts) is int and 1 <= self.max_attempts <= 20,
                'RELAY_RETRY_ATTEMPTS_INVALID')
        require(type(self.max_wait_seconds) is int and 0 <= self.max_wait_seconds <= 3600,
                'RELAY_RETRY_WAIT_INVALID')


def business_retry_delay(failure):
    """30s seller queue guidance (user supplied); never shorten a valid Retry-After."""
    if not isinstance(failure, dict):
        return None
    category = failure.get('classification')
    state, wait = failure.get('retry_after_state'), failure.get('retry_after_seconds')
    if category not in RETRYABLE_CLASSES:
        return None
    if state in {'invalid', 'withheld'} or category == 'rate-limit' and state != 'valid':
        return None
    if state == 'valid' and (type(wait) is not int or wait < 0):
        return None
    return max(30, wait or 0)


class RetryingRelay:
    """Explicit opt-in wrapper; the underlying RelayHTTP still owns each single attempt."""
    def __init__(self, client, policy=None, *, sleep=time.sleep, monotonic=time.monotonic,
                 clock=None, deadline=None):
        self.client = client
        self._request_timeout_seconds = validate_request_timeout_seconds(
            getattr(client, 'request_timeout_seconds', DEFAULT_REQUEST_TIMEOUT_SECONDS))
        self.policy = RetryPolicy() if policy is None else policy
        require(isinstance(self.policy, RetryPolicy), 'RELAY_RETRY_POLICY_INVALID')
        self.sleep, self.monotonic = sleep, monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.deadline = deadline
        self.last_attempts = []
        self.pending_retry = None
        self.last_retry_decision = None

    @property
    def request_timeout_seconds(self):
        return self._request_timeout_seconds

    def __getattr__(self, key):
        return getattr(self.client, key)

    def request(self, api, parameters, **kwargs):
        return self.run(lambda: self.client.request(api, parameters, **kwargs))

    def run(self, call, *, continuation=None, checkpoint=None, pause_requested=None):
        if continuation is not None or checkpoint is not None or pause_requested is not None:
            return self._run_checkpointed(call, continuation=continuation, checkpoint=checkpoint,
                                          pause_requested=pause_requested)
        self.last_attempts, self.pending_retry = [], None
        self.last_retry_decision = None
        waited = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.deadline is not None and self.monotonic() + self.request_timeout_seconds > self.deadline:
                self.last_retry_decision = {'reason': 'run-deadline', 'required_wait_seconds': None}
                self.pending_retry = {'reason': 'run-deadline', 'retry_after_seconds': None}
                raise RecordError('RELAY_RUN_DEADLINE')
            started = self.clock()
            try:
                body = self._checked_response(call)
            except RecordError as exc:
                failure = deepcopy(getattr(self.client, 'last_error_meta', None))
                delay, reason = self._decision(failure, attempt, waited)
                retry = reason == 'retry'
                self.last_retry_decision = {'reason': reason, 'required_wait_seconds': delay}
                recorded = {'attempt': attempt, 'requested_at': started,
                    'received_at': self.clock(), 'error': str(exc), 'failure': failure,
                    'wait_before_next_attempt': delay if retry else None,
                    'headers': deepcopy(getattr(self.client, 'last_header_diagnostics', None)),
                    'retry_decision': deepcopy(self.last_retry_decision), 'completed_wait_seconds': 0}
                self.last_attempts.append(recorded)
                if not retry:
                    if delay is not None:
                        self.pending_retry = {'reason': 'attempt-or-wait-bound', 'retry_after_seconds': delay}
                    raise
                self.sleep(delay)
                # A planned or interrupted wait is not a completed wait. This is
                # the requested duration whose sleep returned, not elapsed wall time.
                recorded['completed_wait_seconds'] = delay
                waited += delay
            else:
                self.last_retry_decision = {'reason': 'response', 'required_wait_seconds': None}
                self.last_attempts.append({'attempt': attempt, 'requested_at': started,
                    'received_at': self.clock(), 'error': None,
                    'headers': deepcopy(getattr(self.client, 'last_header_diagnostics', None)),
                    'retry_decision': deepcopy(self.last_retry_decision), 'completed_wait_seconds': 0,
                    'transport': deepcopy(getattr(self.client, 'last_response_meta', None))})
                return body

    def _checked_response(self, call):
        body = call()
        # HTTP200 can carry a real service refusal; preserve the original classifier.
        code = body.get('error') if isinstance(body, dict) else None
        categories = {'upstream_pool_exhausted': 'temporary-upstream',
                      'upstream_timeout': 'temporary-upstream', 'citydata_timeout': 'temporary-upstream',
                      'minute_data_pending': 'temporary-upstream',
                      'rate_limited': 'rate-limit', 'ip_rate_limited': 'rate-limit',
                      'unauthorized': 'auth-or-entitlement', 'forbidden': 'auth-or-entitlement',
                      'data_source_unavailable': 'source-unavailable'}
        if isinstance(code, str) and code in categories:
            state, wait = getattr(self.client, 'last_retry_after', ('missing', None))
            self.client.last_error_meta = {'classification': categories[code], 'business_error': code,
                'http_status': 200, 'retry_after_state': state, 'retry_after_seconds': wait}
            raise RecordError('RELAY_AUTH_OR_ENTITLEMENT' if categories[code] == 'auth-or-entitlement'
                              else 'RELAY_RATE_LIMIT' if categories[code] == 'rate-limit'
                              else 'RELAY_BUSINESS_REFUSAL')
        return body

    def _decision(self, failure, attempt, waited):
        delay = business_retry_delay(failure)
        # One decision owner for legacy and checkpointed execution, in the same order.
        if delay is None:
            reason = ('retry-after-unusable' if isinstance(failure, dict)
                      and failure.get('classification') in RETRYABLE_CLASSES else 'not-retryable')
        elif attempt >= self.policy.max_attempts:
            reason = 'attempt-limit'
        elif waited + delay > self.policy.max_wait_seconds:
            reason = 'wait-budget'
        elif self.deadline is not None and not self.monotonic() + delay + self.request_timeout_seconds <= self.deadline:
            reason = 'run-deadline'
        else:
            reason = 'retry'
        return delay, reason

    def _run_checkpointed(self, call, *, continuation, checkpoint, pause_requested):
        from decision_kernel.application.relay_capture_checkpoint import (
            POLICY, STOP_CLASSES, UNKNOWN_ATTEMPT, CheckpointStateError, checkpoint_time, new_retry_state, not_before,
            pause_reason, validate_retry_state,
        )
        require(vars(self.policy) == POLICY and callable(checkpoint), 'RELAY_CHECKPOINT_POLICY_CHANGED')
        saved = validate_retry_state(continuation if continuation is not None else new_retry_state())
        require(saved['phase'] in ('ready', 'paused', 'waiting'), 'RELAY_CHECKPOINT_NOT_RESUMABLE')
        last_known = next((a for a in reversed(saved['attempts'])
            if a.get('outcome') != UNKNOWN_ATTEMPT), {})
        require((last_known.get('failure') or {}).get('classification') not in STOP_CLASSES,
                'RELAY_CHECKPOINT_GLOBAL_STOP')
        self.last_attempts, self.pending_retry = deepcopy(saved['attempts']), None
        self.last_retry_decision = deepcopy(saved['decision'])
        waited = saved['wait_budget_seconds']
        pending, earliest = saved['pending_wait_seconds'], saved['not_before']
        if self.last_attempts:
            last = self.last_attempts[-1]
            previous_clock = last['reconciled_at'] if last.get('outcome') == UNKNOWN_ATTEMPT else last['received_at']
            require(checkpoint_time(self.clock()) >= checkpoint_time(previous_clock), 'RELAY_CHECKPOINT_CLOCK')
        if self.last_attempts and pending is None:
            last = self.last_attempts[-1]
            require(last['completed_wait_seconds'] >= last['checkpoint_wait_seconds']
                    or checkpoint_time(self.clock()) >= checkpoint_time(last['checkpoint_not_before']),
                    'RELAY_CHECKPOINT_RETRY_AFTER')

        def emit(phase, reserved=None):
            checkpoint({'phase': phase, 'attempts': deepcopy(self.last_attempts),
                'reserved_attempt': reserved, 'wait_budget_seconds': waited,
                'pending_wait_seconds': pending, 'not_before': earliest,
                'decision': deepcopy(self.last_retry_decision)})

        def admission(wait=0):
            reason = pause_reason(pause_requested)
            if reason:
                self.last_retry_decision = {'reason': 'work-unit-complete', 'required_wait_seconds': None}
                self.pending_retry = {'reason': 'work-unit-complete', 'retry_after_seconds': pending}
                emit('paused')
                raise RecordError('RELAY_' + reason)
            if self.deadline is not None and self.monotonic() + wait + self.request_timeout_seconds > self.deadline:
                self.last_retry_decision = {'reason': 'run-deadline', 'required_wait_seconds': pending}
                self.pending_retry = {'reason': 'run-deadline', 'retry_after_seconds': pending}
                emit('paused')
                raise RecordError('RELAY_RUN_DEADLINE')

        if pending is not None:
            now = checkpoint_time(self.clock())
            require(self.last_attempts and now >= checkpoint_time(self.last_attempts[-1]['received_at']),
                    'RELAY_CHECKPOINT_CLOCK')
            remaining = max(0, math.ceil((checkpoint_time(earliest) - now).total_seconds()))
            require(remaining <= pending, 'RELAY_CHECKPOINT_RETRY_AFTER')
            admission(remaining)
            emit('waiting')
            if remaining:
                self.sleep(remaining)
                self.last_attempts[-1]['completed_wait_seconds'] += remaining
            pending, earliest = None, None
            emit('ready')

        for attempt in range(len(self.last_attempts) + 1, self.policy.max_attempts + 1):
            admission()
            started = self.clock()
            # Persistence of this reservation MUST finish before the HTTP call.
            emit('in-flight', attempt)
            if self.deadline is not None and self.monotonic() + self.request_timeout_seconds > self.deadline:
                # The writer can consume the remaining admission window. Do not
                # issue a late HTTP request or erase its durable reservation.
                raise CheckpointStateError('RELAY_CHECKPOINT_ADMISSION_EXPIRED')
            try:
                body = self._checked_response(call)
            except RecordError as exc:
                failure = deepcopy(getattr(self.client, 'last_error_meta', None))
                delay, reason = self._decision(failure, attempt, waited)
                self.last_retry_decision = {'reason': reason, 'required_wait_seconds': delay}
                received = self.clock()
                charged = delay if reason in ('retry', 'run-deadline') else 0
                recorded = {'attempt': attempt, 'requested_at': started, 'received_at': received,
                    'error': str(exc), 'failure': failure,
                    'wait_before_next_attempt': delay if reason == 'retry' else None,
                    'headers': deepcopy(getattr(self.client, 'last_header_diagnostics', None)),
                    'retry_decision': deepcopy(self.last_retry_decision), 'completed_wait_seconds': 0,
                    'checkpoint_wait_seconds': charged,
                    'checkpoint_not_before': not_before(received, charged) if charged else None}
                self.last_attempts.append(recorded)
                waited += charged
                pending = charged or None
                earliest = recorded['checkpoint_not_before']
                if reason != 'retry':
                    if delay is not None:
                        self.pending_retry = {'reason': 'attempt-or-wait-bound', 'retry_after_seconds': delay}
                    emit('paused' if reason == 'run-deadline' else 'failed')
                    raise
                emit('waiting')
                self.sleep(delay)
                recorded['completed_wait_seconds'] = delay
                pending, earliest = None, None
                emit('ready')
            else:
                self.last_retry_decision = {'reason': 'response', 'required_wait_seconds': None}
                self.last_attempts.append({'attempt': attempt, 'requested_at': started,
                    'received_at': self.clock(), 'error': None,
                    'headers': deepcopy(getattr(self.client, 'last_header_diagnostics', None)),
                    'retry_decision': deepcopy(self.last_retry_decision), 'completed_wait_seconds': 0,
                    'transport': deepcopy(getattr(self.client, 'last_response_meta', None)),
                    'checkpoint_wait_seconds': 0, 'checkpoint_not_before': None})
                emit('response')
                return body
