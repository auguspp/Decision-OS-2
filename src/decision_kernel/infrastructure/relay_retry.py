"""Finite same-request business waits. No scheduler, alternate host, key or write retry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from decision_kernel.domain.records import RecordError, require

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
        self.policy = RetryPolicy() if policy is None else policy
        require(isinstance(self.policy, RetryPolicy), 'RELAY_RETRY_POLICY_INVALID')
        self.sleep, self.monotonic = sleep, monotonic
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.deadline = deadline
        self.last_attempts = []
        self.pending_retry = None
        self.last_retry_decision = None

    def __getattr__(self, key):
        return getattr(self.client, key)

    def request(self, api, parameters, **kwargs):
        return self.run(lambda: self.client.request(api, parameters, **kwargs))

    def run(self, call):
        self.last_attempts, self.pending_retry = [], None
        self.last_retry_decision = None
        waited = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.deadline is not None and self.monotonic() + 30 > self.deadline:
                self.last_retry_decision = {'reason': 'run-deadline', 'required_wait_seconds': None}
                self.pending_retry = {'reason': 'run-deadline', 'retry_after_seconds': None}
                raise RecordError('RELAY_RUN_DEADLINE')
            started = self.clock()
            try:
                body = call()
                # HTTP200 can carry a real service refusal; never pass it as a successful table.
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
            except RecordError as exc:
                failure = deepcopy(getattr(self.client, 'last_error_meta', None))
                delay = business_retry_delay(failure)
                # Same short-circuit order and budgets as the original decision.
                # Record the first blocking condition here, not by guessing later.
                if delay is None:
                    reason = ('retry-after-unusable' if isinstance(failure, dict)
                              and failure.get('classification') in RETRYABLE_CLASSES else 'not-retryable')
                elif attempt >= self.policy.max_attempts:
                    reason = 'attempt-limit'
                elif waited + delay > self.policy.max_wait_seconds:
                    reason = 'wait-budget'
                elif self.deadline is not None and not self.monotonic() + delay + 30 <= self.deadline:
                    reason = 'run-deadline'
                else:
                    reason = 'retry'
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
