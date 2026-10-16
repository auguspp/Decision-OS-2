"""Finite same-request business waits. No scheduler, alternate host, key or write retry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import time

from decision_kernel.domain.records import RecordError, require


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
    if category not in {'temporary-upstream', 'temporary-gateway', 'timeout',
                        'connection', 'dns-temporary', 'incomplete-read', 'rate-limit'}:
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

    def __getattr__(self, key):
        return getattr(self.client, key)

    def request(self, api, parameters, **kwargs):
        return self.run(lambda: self.client.request(api, parameters, **kwargs))

    def run(self, call):
        self.last_attempts, self.pending_retry = [], None
        waited = 0
        for attempt in range(1, self.policy.max_attempts + 1):
            if self.deadline is not None and self.monotonic() + 30 > self.deadline:
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
                retry = (delay is not None and attempt < self.policy.max_attempts
                         and waited + delay <= self.policy.max_wait_seconds
                         and (self.deadline is None or self.monotonic() + delay + 30 <= self.deadline))
                self.last_attempts.append({'attempt': attempt, 'requested_at': started,
                    'received_at': self.clock(), 'error': str(exc), 'failure': failure,
                    'wait_before_next_attempt': delay if retry else None})
                if not retry:
                    if delay is not None:
                        self.pending_retry = {'reason': 'attempt-or-wait-bound', 'retry_after_seconds': delay}
                    raise
                self.sleep(delay)
                waited += delay
            else:
                self.last_attempts.append({'attempt': attempt, 'requested_at': started,
                    'received_at': self.clock(), 'error': None,
                    'transport': deepcopy(getattr(self.client, 'last_response_meta', None))})
                return body
