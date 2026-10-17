"""Original Relay decisions and source identity, using synthetic HTTP only."""
from copy import deepcopy
from email.message import Message
from urllib.error import URLError

import pytest

from decision_kernel.application.market_retry_diagnostics import retry_diagnostics
from decision_kernel.application.research_sources import capture_registered, capture_plan, inspect_result, source_inputs, validate_plan
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_failure import rate_limit_headers
from decision_kernel.infrastructure.relay_http import RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryPolicy, RetryingRelay
from test_research_sources import Reply, body, busy, plan, registry, request, STAMP

KEY = 'synthetic-key-123'


class HeadersOpener:
    def __init__(self, *responses):
        self.responses, self.calls = iter(responses), []

    def open(self, req, timeout):
        assert timeout == 30
        self.calls.append(req)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        value, headers = response
        return Reply(value, req.full_url, headers)


def captured(*responses, policy=None, deadline=None, sleep=None, monotonic=lambda: 0, use=None):
    opener = HeadersOpener(*responses)
    client = RelayHTTP(opener=opener, credential=KEY)
    waits = []
    session = RetryingRelay(client, policy, sleep=sleep or waits.append, deadline=deadline,
                            monotonic=monotonic, clock=lambda: STAMP)
    checked = validate_plan(plan(use or request()), registry())['requests'][0]
    result = capture_registered(checked, registry(), session)
    return result, client, opener, waits


@pytest.mark.parametrize('raw,state,value', [
    (None, 'missing', None), ('0', 'valid', 0), (' 2000 ', 'valid', 2000),
    ('999999999999', 'valid', 999999999999), ('-1', 'invalid', None),
    ('1.5', 'invalid', None), ('1,2', 'invalid', None), ('１', 'invalid', None),
    ('\n3', 'invalid', None), (True, 'invalid', None), (3, 'invalid', None),
    ('9' * 13, 'invalid', None), (KEY, 'withheld', None),
])
def test_remaining_is_a_bounded_snapshot_not_a_default_or_coerced_quota(raw, state, value):
    out = rate_limit_headers({'X-RateLimit-Remaining': raw, 'X-RateLimit-IP-Remaining': '7'}, KEY)
    assert out == {'key_remaining': {'state': state, 'remaining': value},
                   'ip_remaining': {'state': 'valid', 'remaining': 7}}


def test_duplicate_and_numeric_credential_headers_are_not_retained():
    headers = Message()
    headers.add_header('X-RateLimit-IP-Remaining', '2')
    headers.add_header('X-RateLimit-IP-Remaining', '8')
    headers.add_header('X-RateLimit-Remaining', '001234567899')
    assert rate_limit_headers(headers, '12345678') == {
        'key_remaining': {'state': 'withheld', 'remaining': None},
        'ip_remaining': {'state': 'invalid', 'remaining': None}}


def test_http_side_channel_resets_without_changing_success_transport():
    op = HeadersOpener(busy(429, 'rate_limited', {'Retry-After': '45', 'X-RateLimit-IP-Remaining': '0'}),
                       (body(), {'X-RateLimit-Remaining': '6'}), URLError(TimeoutError('private')))
    client = RelayHTTP(opener=op, credential=KEY)
    args = registry(), 'daily', request()['parameters']
    with pytest.raises(RecordError, match='RELAY_HTTP_429'):
        client.request_registered(*args)
    first = deepcopy(client.last_header_diagnostics)
    client.request_registered(*args)
    assert set(client.last_response_meta) == {'http_status', 'x_request_id', 'x_cache', 'wire_sha256'}
    assert client.last_header_diagnostics['retry_after'] == {'state': 'missing', 'seconds': None}
    assert client.last_header_diagnostics['ip_remaining'] == {'state': 'missing', 'remaining': None}
    with pytest.raises(RecordError, match='RELAY_HTTP_UNAVAILABLE'):
        client.request_registered(*args)
    assert client.last_header_diagnostics is None
    assert first['retry_after'] == {'state': 'valid', 'seconds': 45}
    assert first['ip_remaining']['remaining'] == 0


def test_transport_failure_has_no_observed_headers_from_the_previous_429():
    c, _, _, waits = captured(busy(429, 'rate_limited', {'Retry-After': '30'}),
                             URLError(TimeoutError('private')), (body(), {}))
    d = retry_diagnostics(c)
    assert c['complete'] and waits == [30, 30] and d['completed_wait_seconds'] == 60
    assert d['last_failure']['attempt'] == 2 and d['last_failure']['request_recovered']
    assert d['last_failure']['retry_after'] == {'state': 'not-recorded', 'seconds': None}
    assert d['last_response']['attempt'] == 3 and d['last_response']['retry_after']['state'] == 'missing'


@pytest.mark.parametrize('status,code,header,policy,deadline,reason,wait', [
    (429, 'rate_limited', None, None, None, 'retry-after-unusable', None),
    (429, 'rate_limited', 'bad', None, None, 'retry-after-unusable', None),
    (429, 'rate_limited', KEY, None, None, 'retry-after-unusable', None),
    (429, 'rate_limited', '181', None, None, 'wait-budget', 181),
    (429, 'rate_limited', '86400', None, None, 'wait-budget', 86400),
    (429, 'rate_limited', '0', RetryPolicy(1, 180), None, 'attempt-limit', 30),
    (429, 'rate_limited', '30', None, 40, 'run-deadline', 30),
    (503, 'unknown', None, None, None, 'not-retryable', None),
    (403, 'forbidden', '60', None, None, 'not-retryable', None),
])
def test_exact_original_blocking_condition_never_sends_a_late_or_shortened_retry(status, code, header, policy, deadline, reason, wait):
    c, client, op, waits = captured(busy(status, code, {'Retry-After': header}), policy=policy, deadline=deadline)
    d = retry_diagnostics(c)
    assert client.requests == len(op.calls) == 1 and waits == [] and not c['complete']
    assert d['last_request']['retry_decision'] == {'reason': reason, 'required_wait_seconds': wait}
    assert d['completed_wait_seconds'] == 0 and not d['last_failure']['request_recovered']


@pytest.mark.parametrize('envelope', [False, True])
def test_valid_429_or_http200_business_limit_recovers_the_exact_request(envelope):
    headers = {'Retry-After': '45', 'X-RateLimit-Remaining': '0', 'X-RateLimit-IP-Remaining': '1'}
    first = ({'error': 'ip_rate_limited'}, headers) if envelope else busy(429, 'rate_limited', headers)
    c, client, op, waits = captured(first, (body(), {'X-RateLimit-Remaining': '10'}))
    d = retry_diagnostics(c)
    assert c['complete'] and waits == [45] and client.requests == 2
    assert op.calls[0].full_url == op.calls[1].full_url
    assert d['last_failure']['request_recovered'] and d['last_failure']['attempt'] == 1
    assert d['last_failure']['key_remaining']['remaining'] == 0
    assert d['last_response']['attempt'] == 2 and d['last_response']['key_remaining']['remaining'] == 10
    assert d['completed_wait_seconds'] == 45
    assert d['last_request']['retry_decision']['reason'] == 'response'


@pytest.mark.parametrize('delays,reason,completed', [([0, 0, 0, 0], 'attempt-limit', 90),
                                                   ([90, 91], 'wait-budget', 90)])
def test_per_request_attempt_and_cumulative_wait_bounds_keep_original_pending(delays, reason, completed):
    c, client, _, waits = captured(*(busy(429, 'rate_limited', {'Retry-After': str(v)}) for v in delays))
    d = retry_diagnostics(c)
    assert client.requests == len(delays) and sum(waits) == completed == d['completed_wait_seconds']
    assert d['last_request']['retry_decision']['reason'] == reason
    assert c['pending_retry'] == {'reason': 'attempt-or-wait-bound', 'retry_after_seconds': max(30, delays[-1])}


def test_interrupted_wait_is_planned_but_not_counted_as_completed():
    def interrupted(seconds):
        assert seconds == 45
        raise RecordError('SYNTHETIC_WAIT_INTERRUPTED')
    c, client, _, _ = captured(busy(429, 'rate_limited', {'Retry-After': '45'}), sleep=interrupted)
    assert client.requests == 1 and c['error'] == 'SYNTHETIC_WAIT_INTERRUPTED'
    assert c['attempts'][0]['wait_before_next_attempt'] == 45
    assert retry_diagnostics(c)['completed_wait_seconds'] == 0
    assert retry_diagnostics(c)['last_failure']['retry_decision']['reason'] == 'retry'


def test_preflight_deadline_has_zero_attempts_and_cannot_promote_old_client_headers():
    client = RelayHTTP(opener=HeadersOpener(), credential=KEY)
    client.last_error_meta = {'classification': 'rate-limit', 'retry_after_state': 'valid', 'retry_after_seconds': 45}
    client.last_header_diagnostics = {'retry_after': {'state': 'valid', 'seconds': 45}}
    session = RetryingRelay(client, monotonic=lambda: 0, deadline=29)
    use = validate_plan(plan(), registry())['requests'][0]
    c = capture_registered(use, registry(), session)
    d = retry_diagnostics(c)
    assert c['error'] == 'RELAY_RUN_DEADLINE' and client.requests == d['recorded_attempts'] == 0
    assert d['last_response'] is d['last_failure'] is None and d['completed_wait_seconds'] == 0
    assert d['last_request'] == {'page_number': 1, 'retry_decision': {'reason': 'run-deadline', 'required_wait_seconds': None}}


def test_next_page_deadline_keeps_the_previous_response_at_its_actual_page():
    ticks = iter([0, 0, 0, 600])
    c, client, _, waits = captured(busy(429, 'rate_limited', {'Retry-After': '45'}),
        (body(), {'X-RateLimit-Remaining': '7'}), deadline=600, monotonic=lambda: next(ticks), use=request(limit=1))
    d = retry_diagnostics(c)
    assert not c['complete'] and c['error'] == 'RELAY_RUN_DEADLINE' and client.requests == 2 and waits == [45]
    assert d['last_request']['page_number'] == 2 and d['last_request']['retry_decision']['reason'] == 'run-deadline'
    assert d['last_response']['page_number'] == d['last_failure']['page_number'] == 1
    assert d['last_failure']['request_recovered'] and d['recorded_attempts'] == 2 and d['completed_wait_seconds'] == 45


def test_recovered_response_with_count_rejection_does_not_double_count_attempts_or_qualify_capture():
    c, _, _, _ = captured(busy(429, 'rate_limited', {'Retry-After': '30'}), ({**body(), 'count': 2}, {}))
    assert not c['complete'] and c['error'] == 'SOURCE_COUNT_SUGGESTS_TRUNCATION'
    assert c['attempts'] == c['pages'][0]['attempts']
    d = retry_diagnostics(c)
    assert d['recorded_attempts'] == 2 and d['completed_wait_seconds'] == 30
    assert d['last_failure']['request_recovered'] and d['last_request']['retry_decision']['reason'] == 'response'


def test_success_with_zero_remaining_does_not_invent_a_reset_or_extra_wait():
    op = HeadersOpener((body(), {'X-RateLimit-Remaining': '0', 'Retry-After': '120'}),
                       ({**body(), 'api_name': 'daily_basic'}, {}))
    client = RelayHTTP(opener=op, credential=KEY); waits = []
    uses = plan(request(), request(api='daily_basic'))
    results = list(capture_plan(uses, registry(), client=client, public_rights_confirmed=True,
                                clock=lambda: STAMP, sleep=waits.append))
    assert all(x['capture']['complete'] for x in results) and len(op.calls) == 2 and waits == []


def test_new_diagnostics_do_not_change_original_source_exports_or_inspected_identity():
    op = HeadersOpener(busy(429, 'rate_limited', {'Retry-After': '30'}), (body(), {'X-RateLimit-Remaining': '7'}))
    item = list(capture_plan(plan(), registry(), client=RelayHTTP(opener=op, credential=KEY),
        public_rights_confirmed=True, sleep=lambda _: None, clock=lambda: STAMP))[0]
    legacy = deepcopy(item)
    legacy['capture'].pop('request_execution')
    for page in legacy['capture']['pages']:
        for attempt in page['attempts']:
            for key in ('headers', 'retry_decision', 'completed_wait_seconds'):
                attempt.pop(key)
    assert source_inputs(item, registry()) == source_inputs(legacy, registry())
    assert inspect_result(item, registry()) == inspect_result(legacy, registry())
    old = retry_diagnostics(legacy['capture'])
    assert old['completed_wait_seconds'] is None and old['last_request']['retry_decision']['reason'] == 'not-recorded'
    assert old['last_failure']['retry_after'] == {'state': 'valid', 'seconds': 30}
