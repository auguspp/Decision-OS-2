"""Real capture/HTTP with synthetic responses; no source service is contacted."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from decision_kernel.application.relay_capture_checkpoint import (
    CaptureCheckpoint, CheckpointStateError, CheckpointWriteError, RELEASE_DECISION,
    release_in_flight, validate_capture_checkpoint, validate_capture_transition,
)
from decision_kernel.application.research_sources import (
    capture_registered, inspect_result, source_inputs, source_page_inputs, validate_plan,
)
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_retry import RetryingRelay
from test_research_sources import STAMP, body, busy, http, plan, registry, request


class Clock:
    def __init__(self, seconds=0):
        self.seconds, self.waits = seconds, []

    def now(self):
        return (datetime.fromisoformat(STAMP) + timedelta(seconds=self.seconds)).isoformat()

    def monotonic(self):
        return self.seconds

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.seconds += seconds


def run(*responses, saved=None, use=None, max_pages=4, seconds=0, budget=600,
        pause=None, writer=None, sleep=None):
    use = use or validate_plan(plan(), registry())['requests'][0]
    client, opener = http(*responses)
    clock, writes = Clock(seconds), []
    session = RetryingRelay(client, sleep=sleep or clock.sleep, monotonic=clock.monotonic,
        clock=clock.now, deadline=seconds + budget)
    sink = writer or (lambda value: writes.append(deepcopy(value)))
    capture = capture_registered(use, registry(), session, max_pages=max_pages, checkpoint=saved,
        checkpoint_writer=sink, pause_requested=(lambda: pause(opener)) if pause else None)
    return capture, writes, client, opener, clock


def result(capture, use):
    return {'case_id': 'synthetic-lifecycle', 'use': use, 'capture': capture}


def empty():
    value = body()
    value['data']['items'] = []
    return value


def test_completed_checkpoint_reuses_exact_query_without_a_credential_or_http():
    use = validate_plan(plan(), registry())['requests'][0]
    capture, writes, client, _, _ = run(body(), use=use)
    assert capture['complete'] and client.requests == 1
    assert [w['control']['state'] for w in writes] == ['executing', 'complete']
    assert writes[0]['control']['retry']['phase'] == 'in-flight'
    assert writes[0]['control']['retry']['reserved_attempt'] == 1
    assert writes[0]['capture']['pages'] == []
    checkpoint = writes[-1]
    validate_capture_checkpoint(checkpoint, use, registry(), max_pages=4)
    client._credential = ''
    session = RetryingRelay(client, clock=lambda: STAMP)
    again = capture_registered(use, registry(), session, max_pages=4, checkpoint=checkpoint)
    assert again == capture and client.requests == 1
    assert source_inputs(result(capture, use), registry()) == source_page_inputs(result(capture, use), registry())


@pytest.mark.parametrize('kind', ['checkpoint', 'transition', 'unchanged-transition'])
def test_public_validation_returns_nested_copies_without_mutating_inputs(kind):
    _, saved, _, _, _ = run(body())
    previous, current = saved[-2:]
    use = current['control']['request']
    original = deepcopy((previous, current))

    def check():
        if kind == 'checkpoint':
            return validate_capture_checkpoint(current, use, registry(), max_pages=4)
        return validate_capture_transition(current if kind == 'unchanged-transition' else previous,
            current, use, registry(), max_pages=4)

    checked = check()
    assert checked == current and (previous, current) == original
    checked['capture']['pages'][0]['table']['items'][0][-1] = 'changed returned row'
    checked['capture']['pages'][0]['attempts'][0]['transport']['wire_sha256'] = 'changed'
    checked['control']['request']['parameters']['trade_date'] = '20260101'
    assert (previous, current) == original

    later = check()
    expected = deepcopy(later)
    current['capture']['pages'][0]['table']['items'][0][-1] = 'changed supplied row'
    current['control']['retry']['attempts'].clear()
    assert later == expected


@pytest.mark.parametrize('side', ['previous', 'current', 'both'])
def test_transition_rechecks_both_supplied_states_after_their_prior_validation(side):
    _, saved, _, _, _ = run(body())
    previous, current = deepcopy(saved[-1]), deepcopy(saved[-1])
    use = deepcopy(current['control']['request'])
    assert validate_capture_transition(previous, current, use, registry(), max_pages=4) == current
    for label, state in (('previous', previous), ('current', current)):
        if side in (label, 'both'):
            state['capture']['pages'][0]['table']['items'][0][1] = '20260929'
    with pytest.raises(RecordError, match='SOURCE_RETURNED_SCOPE_MISMATCH'):
        validate_capture_transition(previous, current, use, registry(), max_pages=4)


def test_writer_mutation_cannot_change_capture_control_or_the_next_predecessor():
    _, saved, client, _, _ = run(body())
    current = saved[-1]
    use = current['control']['request']
    writes = []

    def writer(value):
        writes.append(value)
        value['capture']['pages'][0]['table']['items'][0][-1] = 'writer mutation'
        value['control']['retry']['attempts'].clear()
        value['control']['request']['parameters']['limit'] = '1'

    controller = CaptureCheckpoint(use, registry(), RetryingRelay(client, clock=lambda: STAMP),
        current['capture'], max_pages=4, value=current, writer=writer)
    expected = deepcopy(controller.previous)
    controller.persist()
    assert len(writes) == 1 and writes[0] != expected
    assert controller.previous == expected
    assert controller.capture == expected['capture'] and controller.control == expected['control']
    assert current == expected
    controller.capture['pages'][0]['table']['items'][0][-1] = 'later capture change'
    assert controller.previous == expected and current == expected
    assert client.requests == 1


def test_failed_mutating_writer_does_not_advance_the_previous_checkpoint():
    _, saved, client, _, _ = run(body())
    use = saved[0]['control']['request']
    failure, writes = [], []

    def writer(value):
        writes.append(value)
        if failure:
            value['control']['request']['parameters']['trade_date'] = '20260101'
            value['capture']['pages'].append({'changed': True})
            raise RuntimeError('synthetic unknown write')

    controller = CaptureCheckpoint(use, registry(), RetryingRelay(client, clock=lambda: STAMP),
        deepcopy(saved[0]['capture']), max_pages=4, writer=writer)
    controller.persist()
    previous = deepcopy(controller.previous)
    controller.control.update(state='paused', pause_reason='STORAGE_BUDGET')
    controller.capture.update(error='RELAY_STORAGE_BUDGET', next_parameters=deepcopy(use['parameters']))
    candidate = deepcopy({'schema': previous['schema'], 'capture': controller.capture, 'control': controller.control})
    failure.append(True)
    with pytest.raises(CheckpointWriteError, match='WRITE_UNSETTLED'):
        controller.persist()
    assert controller.previous == previous
    assert controller.capture == candidate['capture'] and controller.control == candidate['control']
    assert len(writes) == 2 and writes[-1] != candidate
    assert client.requests == 1


def test_page_pause_preserves_original_pages_and_resumes_only_exact_next_offset():
    use = validate_plan(plan(request(limit=1)), registry())['requests'][0]
    first, saved, client, _, _ = run(body(), use=use, pause=lambda op: len(op.calls) == 1)
    assert client.requests == 1 and first['error'] == 'RELAY_WORK_UNIT_COMPLETE'
    assert saved[-1]['control']['state'] == 'paused' and saved[-1]['control']['retry'] is None
    assert first['next_parameters'] == {**use['parameters'], 'offset': '1'}
    original = deepcopy(saved[-1])
    output, resumed, client, opener, _ = run(empty(), use=use, saved=original, seconds=1)
    assert output['complete'] and client.requests == len(opener.calls) == 1
    assert 'offset=1' in opener.calls[0].full_url
    assert output['pages'][0] == original['capture']['pages'][0]
    assert original == saved[-1]
    assert inspect_result(result(output, use), registry())['page_count'] == 2
    assert resumed[-1]['control']['state'] == 'complete'


def test_complete_queries_survive_a_later_zero_http_deadline_as_in_g24():
    use_a = validate_plan(plan(), registry())['requests'][0]
    use_b = validate_plan(plan(request(trade_date='20261001')), registry())['requests'][0]
    first, saved_a, client, _, _ = run(body(), use=use_a)
    stopped, saved_b, later, _, _ = run(use=use_b, seconds=600, budget=0)
    assert first['complete'] and client.requests == 1 and later.requests == 0
    assert stopped['error'] == 'RELAY_RUN_DEADLINE'
    assert saved_b[-1]['control']['retry']['attempts'] == []
    reused, _, reader, _, _ = run(use=use_a, saved=saved_a[-1], seconds=700)
    next_body = body(); next_body['data']['items'][0][1] = '20261001'
    completed, _, fresh, _, _ = run(next_body, use=use_b, saved=saved_b[-1], seconds=700)
    assert reused == first and reader.requests == 0
    assert completed['complete'] and fresh.requests == 1


def test_retry_cross_session_honors_not_before_without_recharging_or_resetting_attempts():
    first, saves, client, _, clock = run(busy(503, 'upstream_timeout', {'Retry-After': '30'}), budget=40)
    saved = deepcopy(saves[-1])
    assert client.requests == 1 and clock.waits == [] and not first['complete']
    assert saved['control']['state'] == 'paused'
    assert saved['control']['retry']['wait_budget_seconds'] == 30
    second, states, later, _, now = run(body(), saved=saved, seconds=10)
    assert second['complete'] and later.requests == 1 and now.waits == [20]
    attempts = second['pages'][0]['attempts']
    assert [a['attempt'] for a in attempts] == [1, 2]
    assert attempts[0]['retry_decision']['reason'] == 'run-deadline'
    assert attempts[0]['completed_wait_seconds'] == 20
    assert attempts[0]['checkpoint_wait_seconds'] == 30
    assert states[-1]['control']['retry']['wait_budget_seconds'] == 30
    assert saved == saves[-1]


def test_elapsed_retry_after_is_not_reported_as_a_completed_sleep():
    _, saved, _, _, _ = run(busy(504, 'upstream_timeout', {'Retry-After': '60'}), budget=40)
    capture, _, client, _, clock = run(body(), saved=saved[-1], seconds=100)
    assert capture['complete'] and client.requests == 1 and clock.waits == []
    old = capture['pages'][0]['attempts'][0]
    assert old['completed_wait_seconds'] == 0 and old['checkpoint_wait_seconds'] == 60


def test_exhausted_attempts_stay_four_across_sessions_and_cannot_restart():
    _, saves, first, _, clock = run(*(busy(504) for _ in range(3)), budget=100)
    assert first.requests == 3 and clock.waits == [30, 30]
    output, saved, last, _, waits = run(busy(504), saved=saves[-1], seconds=90)
    assert last.requests == 1 and waits.waits == [] and not output['complete']
    assert saved[-1]['control']['state'] == 'terminal'
    assert saved[-1]['control']['retry']['decision']['reason'] == 'attempt-limit'
    assert [a['attempt'] for a in output['attempts']] == [1, 2, 3, 4]
    final, _, client, _, _ = run(saved=saved[-1], seconds=1000)
    assert final == output and client.requests == 0


def test_wait_budget_failure_is_terminal_even_after_the_wall_clock_has_passed():
    out, saved, client, _, clock = run(busy(504, headers={'Retry-After': '120'}),
                                      busy(504, headers={'Retry-After': '61'}))
    assert client.requests == 2 and clock.waits == [120]
    assert saved[-1]['control']['retry']['decision']['reason'] == 'wait-budget'
    assert saved[-1]['control']['retry']['wait_budget_seconds'] == 120
    again, _, later, _, _ = run(saved=saved[-1], seconds=86400)
    assert again == out and later.requests == 0


@pytest.mark.parametrize('status,error', [(429, 'rate_limited'), (403, 'forbidden')])
def test_global_stops_do_not_gain_cross_session_retry_permission(status, error):
    _, saved, _, _, _ = run(busy(status, error, {'Retry-After': '30'}), budget=40)
    if saved[-1]['control']['state'] == 'terminal':
        _, _, client, _, _ = run(saved=saved[-1], seconds=100)
        assert client.requests == 0
    else:
        with pytest.raises(RecordError, match='GLOBAL_STOP'):
            run(body(), saved=saved[-1], seconds=100)


def test_unknown_inflight_http_cannot_be_reissued_from_last_reservation():
    writes = []
    with pytest.raises(RuntimeError, match='process stopped'):
        run(RuntimeError('process stopped'), writer=lambda value: writes.append(deepcopy(value)))
    assert writes[-1]['control']['retry']['phase'] == 'in-flight'
    with pytest.raises(RecordError, match='INFLIGHT'):
        run(body(), saved=writes[-1], seconds=1)


def test_reservation_persistence_failure_prevents_http_and_is_not_source_failure():
    client, opener = http(body())
    use = validate_plan(plan(), registry())['requests'][0]
    def reject(_):
        raise RecordError('SYNTHETIC_STORE_UNSETTLED')
    with pytest.raises(CheckpointWriteError, match='WRITE_UNSETTLED'):
        capture_registered(use, registry(), RetryingRelay(client, clock=lambda: STAMP),
                           max_pages=4, checkpoint_writer=reject)
    assert client.requests == len(opener.calls) == 0


def test_page_persistence_failure_retains_inflight_and_stops_before_next_page():
    use = validate_plan(plan(request(limit=1)), registry())['requests'][0]
    writes = []
    def writer(value):
        if value['capture']['pages']:
            raise RecordError('SYNTHETIC_STORE_UNSETTLED')
        writes.append(deepcopy(value))
    client, opener = http(body(), empty())
    with pytest.raises(CheckpointWriteError):
        capture_registered(use, registry(), RetryingRelay(client, clock=lambda: STAMP),
                           max_pages=4, checkpoint_writer=writer)
    assert client.requests == len(opener.calls) == 1
    assert writes[-1]['control']['retry']['phase'] == 'in-flight'


def test_known_interrupted_wait_resumes_original_budget_and_not_before():
    def interrupted(_):
        raise RecordError('SYNTHETIC_WAIT_INTERRUPTED')
    _, saved, client, _, _ = run(busy(504, headers={'Retry-After': '45'}), sleep=interrupted)
    assert client.requests == 1 and saved[-1]['control']['pause_reason'] == 'WAIT_INTERRUPTED'
    out, _, later, _, clock = run(body(), saved=saved[-1], seconds=10)
    assert out['complete'] and later.requests == 1 and clock.waits == [35]
    assert out['pages'][0]['attempts'][0]['completed_wait_seconds'] == 35
    assert out['pages'][0]['attempts'][0]['checkpoint_wait_seconds'] == 45


def test_page_bound_is_cumulative_and_cannot_be_enlarged_by_resume():
    use = validate_plan(plan(request(limit=1)), registry())['requests'][0]
    out, saved, client, _, _ = run(body(), use=use, max_pages=1)
    assert not out['complete'] and out['error'] == 'SOURCE_PAGE_BOUND_REACHED' and client.requests == 1
    same, _, later, _, _ = run(use=use, max_pages=1, saved=saved[-1])
    assert same == out and later.requests == 0
    with pytest.raises(RecordError, match='SCOPE_CHANGED'):
        run(empty(), use=use, max_pages=2, saved=saved[-1])


def test_short_page_with_larger_declared_count_is_retained_but_never_completed():
    value = body(); value['count'] = 5
    capture, saved, client, _, _ = run(value)
    assert client.requests == 1 and capture['error'] == 'SOURCE_COUNT_SUGGESTS_TRUNCATION'
    assert saved[-1]['control']['state'] == 'terminal' and len(capture['pages']) == 1
    assert capture['attempts'] == capture['pages'][0]['attempts']
    with pytest.raises(RecordError, match='COMPLETE_CAPTURE'):
        source_inputs(result(capture, saved[-1]['control']['request']), registry())
    assert len(source_page_inputs(result(capture, saved[-1]['control']['request']), registry())) == 1


@pytest.mark.parametrize('mutate', [
    lambda s: s['control'].__setitem__('max_pages', 5),
    lambda s: s['capture']['pages'][0].__setitem__('offset', 1),
    lambda s: s['capture']['pages'][0].__setitem__('attempts', []),
    lambda s: s['control']['retry'].__setitem__('wait_budget_seconds', 1),
    lambda s: s['control']['retry']['attempts'][0].__setitem__('attempt', 0),
    lambda s: s['capture']['pages'][0]['attempts'][0].__setitem__('completed_wait_seconds', 30),
    lambda s: s['capture'].__setitem__('snapshot', {}),
    lambda s: s['capture']['pages'][0]['table']['items'][0].__setitem__(1, '20260929'),
])
def test_changed_control_or_reading_envelopes_do_not_supply_a_retry_budget(mutate):
    _, saved, _, _, _ = run(body())
    value = deepcopy(saved[-1]); mutate(value)
    with pytest.raises(RecordError):
        run(saved=value, seconds=100)


def test_storage_pause_keeps_query_identity_and_issues_zero_http():
    use = validate_plan(plan(), registry())['requests'][0]
    output, saved, client, _, _ = run(use=use, pause=lambda _: 'STORAGE_BUDGET')
    assert client.requests == 0 and output['error'] == 'RELAY_STORAGE_BUDGET'
    assert saved[-1]['control']['pause_reason'] == 'STORAGE_BUDGET'
    assert saved[-1]['control']['retry'] is None
    result_, _, later, _, _ = run(body(), use=use, saved=saved[-1])
    assert result_['complete'] and later.requests == 1


def test_repeated_page_is_checked_against_prefix_from_previous_session():
    use = validate_plan(plan(request(limit=1)), registry())['requests'][0]
    _, saved, _, _, _ = run(body(), use=use, pause=lambda op: bool(op.calls))
    out, records, client, _, _ = run(body(), use=use, saved=saved[-1], seconds=1)
    assert client.requests == 1 and not out['complete'] and out['error'] == 'SOURCE_PAGINATION_STALLED'
    assert len(out['pages']) == 1 and records[-1]['control']['state'] == 'terminal'


def test_capture_byte_bound_includes_pages_retained_by_prior_session():
    use = validate_plan(plan(request(limit=500)), registry())['requests'][0]
    def large(letter):
        value = body()
        value['data']['items'] = [['600519.SH', '20260930', letter * 6000] for _ in range(500)]
        value['count'] = None
        return value
    _, saved, first, _, _ = run(large('a'), large('b'), use=use, pause=lambda op: len(op.calls) == 2)
    assert first.requests == 2 and len(saved[-1]['capture']['pages']) == 2
    out, final, later, _, _ = run(large('c'), use=use, saved=saved[-1], seconds=1)
    assert later.requests == 1 and out['error'] == 'SOURCE_CAPTURE_MEMORY_LIMIT'
    assert len(out['pages']) == 2 and final[-1]['control']['state'] == 'terminal'


def test_legacy_capture_does_not_add_execution_checkpoint_fields():
    use = validate_plan(plan(), registry())['requests'][0]
    client, _ = http(body())
    old = capture_registered(use, registry(), RetryingRelay(client, clock=lambda: STAMP), max_pages=4)
    current, _, _, _, _ = run(body(), use=use)
    assert 'checkpoint_wait_seconds' not in old['pages'][0]['attempts'][0]
    assert inspect_result(result(old, use), registry()) == inspect_result(result(current, use), registry())
    assert source_inputs(result(old, use), registry()) == source_inputs(result(current, use), registry())


def fresh_looking_state(previous):
    value = deepcopy(previous)
    value['control'].update(state='ready', retry=None, pause_reason=None)
    for key in ('failure', 'attempts', 'pending_retry', 'next_parameters', 'request_execution'):
        value['capture'].pop(key, None)
    value['capture']['error'] = None
    return value


def test_valid_standalone_state_cannot_reset_three_failed_attempts_in_the_same_chain():
    _, saves, _, _, _ = run(*(busy(504) for _ in range(3)), budget=100)
    previous = saves[-1]
    fake = fresh_looking_state(previous)
    use = previous['control']['request']
    # A plausible new query is not a valid continuation of this original query.
    validate_capture_checkpoint(fake, use, registry(), max_pages=4)
    with pytest.raises(RecordError, match='BUDGET_RESET'):
        validate_capture_transition(previous, fake, use, registry(), max_pages=4)


def test_transition_cannot_clear_an_unknown_reserved_attempt():
    saved = []
    with pytest.raises(RuntimeError):
        run(RuntimeError('source response lost'), writer=lambda s: saved.append(deepcopy(s)))
    old = saved[-1]
    new = fresh_looking_state(old)
    new['control'].update(state='paused', pause_reason='WORK_UNIT_COMPLETE')
    new['capture'].update(error='RELAY_WORK_UNIT_COMPLETE', next_parameters=new['control']['request']['parameters'])
    validate_capture_checkpoint(new, new['control']['request'], registry(), max_pages=4)
    with pytest.raises(RecordError, match='BUDGET_RESET'):
        validate_capture_transition(old, new, new['control']['request'], registry(), max_pages=4)


def test_terminal_cannot_be_published_as_a_fresh_budget_under_its_predecessor():
    _, saves, _, _, _ = run(busy(504, headers={'Retry-After': '7200'}))
    old = saves[-1]
    new = fresh_looking_state(old)
    use = old['control']['request']
    validate_capture_checkpoint(new, use, registry(), max_pages=4)
    with pytest.raises(RecordError, match='TERMINAL_RESTART'):
        validate_capture_transition(old, new, use, registry(), max_pages=4)


def _unconfirmed():
    """A real in-flight reservation: the second request was sent, no answer came back."""
    saved = []
    with pytest.raises(RuntimeError):
        run(busy(504), RuntimeError('source response lost'), writer=lambda s: saved.append(deepcopy(s)))
    assert saved[-1]['control']['retry']['phase'] == 'in-flight'
    return saved[-1]


def test_release_names_the_unknown_request_and_leaves_the_venue_resumable():
    old = _unconfirmed()
    use = old['control']['request']
    released = release_in_flight(old)
    # Nothing recorded is dropped: the unknown request stays named in history.
    assert released['control']['retry']['attempts'] == old['control']['retry']['attempts']
    assert released['control']['retry']['wait_budget_seconds'] == old['control']['retry']['wait_budget_seconds']
    assert released['control']['retry']['reserved_attempt'] is None
    assert released['control']['retry']['decision'] == RELEASE_DECISION
    # The control phase is deliberately unchanged: only the reservation is gone.
    assert old['control']['state'] == released['control']['state'] == 'executing'
    assert released['control']['page_number'] == old['control']['page_number']
    validate_capture_checkpoint(released, use, registry(), max_pages=4)
    validate_capture_transition(old, released, use, registry(), max_pages=4)


def test_released_checkpoint_resumes_on_the_next_invocation():
    released = release_in_flight(_unconfirmed())
    capture, _, _, _, _ = run(body(), saved=released)
    assert capture['complete'] is True


def test_release_is_refused_when_nothing_is_unconfirmed():
    _, saves, _, _, _ = run(body())
    with pytest.raises(RecordError, match='NOT_INFLIGHT'):
        release_in_flight(saves[-1])
    with pytest.raises(RecordError, match='NOT_INFLIGHT'):
        release_in_flight(release_in_flight(_unconfirmed()))


def test_release_decision_cannot_smuggle_a_dropped_attempt():
    old = _unconfirmed()
    use = old['control']['request']
    released = release_in_flight(old)
    released['control']['retry']['attempts'] = released['control']['retry']['attempts'][:-1]
    released['control']['retry']['wait_budget_seconds'] = 0
    validate_capture_checkpoint(released, use, registry(), max_pages=4)
    with pytest.raises(RecordError, match='UNSETTLED'):
        validate_capture_transition(old, released, use, registry(), max_pages=4)


def test_transition_requires_new_success_to_settle_the_reserved_attempt_number():
    saved = []
    with pytest.raises(RuntimeError):
        run(busy(504), RuntimeError('source response lost'), writer=lambda s: saved.append(deepcopy(s)))
    assert saved[-1]['control']['retry']['reserved_attempt'] == 2
    _, unrelated, _, _, _ = run(body())
    use = saved[-1]['control']['request']
    validate_capture_checkpoint(unrelated[-1], use, registry(), max_pages=4)
    with pytest.raises(RecordError, match='ATTEMPTS_CHANGED'):
        validate_capture_transition(saved[-1], unrelated[-1], use, registry(), max_pages=4)


def test_removing_pending_wait_cannot_send_http_before_original_not_before():
    _, saved, _, _, _ = run(busy(504, headers={'Retry-After': '60'}), budget=40)
    fake = deepcopy(saved[-1])
    fake['control']['retry'].update(pending_wait_seconds=None, not_before=None)
    use = fake['control']['request']
    validate_capture_checkpoint(fake, use, registry(), max_pages=4)
    client, opener = http(body())
    clock = Clock(10)
    with pytest.raises(RecordError, match='RETRY_AFTER'):
        capture_registered(use, registry(), RetryingRelay(client, clock=clock.now), max_pages=4,
                           checkpoint=fake, checkpoint_writer=lambda _: None)
    assert client.requests == len(opener.calls) == 0


def test_state_validation_failure_does_not_attempt_a_write_or_http(monkeypatch):
    import decision_kernel.application.relay_capture_checkpoint as checkpoints
    def invalid(*args, **kwargs):
        raise RecordError('RELAY_CHECKPOINT_CONTROL')
    monkeypatch.setattr(checkpoints, 'validate_capture_checkpoint', invalid)
    use = validate_plan(plan(), registry())['requests'][0]
    client, opener = http(body()); writes = []
    with pytest.raises(CheckpointStateError, match='STATE_INVALID'):
        capture_registered(use, registry(), RetryingRelay(client, clock=lambda: STAMP),
                           max_pages=4, checkpoint_writer=writes.append)
    assert not writes and client.requests == len(opener.calls) == 0


def test_slow_reservation_save_cannot_allow_http_after_the_shared_deadline():
    use = validate_plan(plan(), registry())['requests'][0]
    client, opener = http(body()); clock = Clock(); writes = []
    def writer(value):
        writes.append(deepcopy(value))
        clock.seconds = 600
    session = RetryingRelay(client, clock=clock.now, monotonic=clock.monotonic, deadline=600)
    with pytest.raises(CheckpointStateError, match='ADMISSION_EXPIRED'):
        capture_registered(use, registry(), session, max_pages=4, checkpoint_writer=writer)
    assert client.requests == len(opener.calls) == 0
    assert len(writes) == 1 and writes[0]['control']['retry']['phase'] == 'in-flight'
    with pytest.raises(RecordError, match='INFLIGHT'):
        run(saved=writes[0], seconds=700)
