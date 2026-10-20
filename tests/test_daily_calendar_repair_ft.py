"""Integrate the narrow calendar repair with the original complete FT test host."""
from copy import deepcopy

from decision_kernel.application import daily_market as daily
from decision_kernel.application.daily_calendar_repair import PROOF_SCHEMA
from decision_kernel.application.ftshare_collection import restore_ftshare_collection


def test_repair_uses_real_existing_ft_owner_and_next_day_keeps_normal_budget():
    from test_daily_market import Host
    from test_market_relay_inputs import REGISTRY
    host = Host()
    host.calendar_error = TimeoutError('synthetic transport timeout; not a real provider')
    first = host.run()
    assert first['receipt']['status'] == 'blocked'
    assert first['receipt']['calendar_source_requests'] == 1
    assert first['receipt']['price_source_requests'] == 0
    failed = daily.latest_cycle(host.fixture['fresh']())
    original = deepcopy(failed)
    grant = {'schema': PROOF_SCHEMA, 'original_reference': failed['reference'], 'venue': 'SSE',
        'additional_requests': 1,
        'authorization_ref': 'https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6284820778'}
    host.calendar_error = None
    repaired = host.run(calendar_repair=grant)
    receipt = repaired['receipt']
    assert receipt['status'] == 'complete', receipt
    assert (receipt['calendar_source_requests'], receipt['price_source_requests']) == (2, 4)
    assert receipt['calendar_reserved_requests'] == 3
    cycle = daily.latest_cycle(host.fixture['fresh']())['cycle']
    assert cycle['calendar_repair']['original_reference'] == original['reference']
    price = restore_ftshare_collection(cycle['market_reference'], REGISTRY, host.fixture['fresh']())['collection']
    assert price['reserved_requests'] == price['plan']['source_budget'] == 4
    assert all(unit['status'] == 'complete' for unit in price['units'].values())
    before = sum(client.requests for client in host.calendar_clients + host.price_clients)
    assert host.run(calendar_repair=grant)['receipt']['status'] == 'already-complete'
    assert sum(client.requests for client in host.calendar_clients + host.price_clients) == before
    status = host.run(operation='status')['receipt']
    assert status['calendar_source_budget'] == 3 and status['calendar_reserved_requests'] == 3
    assert status['source_requests'] == 0
    host.now = '2026-10-20T12:10:00+00:00'
    next_day = host.run()['receipt']
    assert next_day['status'] == 'complete', next_day
    assert next_day['calendar_source_requests'] == next_day['calendar_source_budget'] == 2
    assert next_day['price_source_requests'] == 4
    assert daily.latest_cycle(host.fixture['fresh']())['cycle']['schema'] == daily.SCHEMA
