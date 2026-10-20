"""Normal Room reader for the exact original-cohort next-session root."""
from copy import deepcopy
from pathlib import Path

from decision_kernel.application.auction_next_session_custody import read, paginate
from decision_kernel.application.auction_next_session_inputs import reference
from decision_kernel.application.market_reading_scope import operation_failure
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import MAX_BYTES, RecordError, encoded, require
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.opening_auction import CODE


def next_session_plan(value):
    require(isinstance(value, dict) and 'reference' in value
            and set(value) <= {'reference', 'offset', 'limit', 'entity_id'}, 'ROOM_AUCTION_NEXT_PLAN_INVALID')
    ref = reference(value['reference'])
    offset, limit, entity = value.get('offset', 0), value.get('limit', 20), value.get('entity_id')
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
            'ROOM_AUCTION_NEXT_PAGE_INVALID')
    require(entity is None or isinstance(entity, str) and CODE.fullmatch(entity), 'ROOM_AUCTION_NEXT_ENTITY_INVALID')
    return {'reference': ref, 'offset': offset, 'limit': limit, 'entity_id': entity}


def open_next_session(store, value, *, registry=None):
    from decision_kernel.application.reading_room import _failure
    plan = next_session_plan(value)
    registry = registry if registry is not None else RelayRegistry(Path('docs/sources/relay-registry.json'))
    result = {'schema': 'reading-auction-next-session-v1', 'plan': plan, 'reference': plan['reference'],
              'open_request': {'operation': 'read-source', 'payload': deepcopy(plan['reference'])},
              'source_requests': 0, 'writes': 0, 'next_plan': None}
    try:
        restored = read(plan['reference'], registry, store, limit=100)
        view = restored['view']
        result.update(view=restored, page=None, status='partial')
        if view is not None:
            page = paginate(view, offset=plan['offset'], limit=plan['limit'], entity_id=plan['entity_id'])
            result['page'] = {'entries': page['entries'], **page['pagination']}
            if page['pagination']['next_offset'] is not None:
                result['next_plan'] = {**deepcopy(plan), 'offset': page['pagination']['next_offset']}
            result['status'] = view['status'] if restored['status'] == 'restored' else 'partial'
    except (RecordError, MarketInputError, KeyError, TypeError) as error:
        if operation_failure(error):
            raise
        result.update(_failure(error))
    # The source scope and final native check already completed before this
    # display projection. An oversized panel never hides a terminal exception.
    if len(encoded(result)) > MAX_BYTES:
        return {'schema': 'reading-auction-next-session-v1', 'plan': plan, 'status': 'not-included',
                'error': 'ROOM_AUCTION_NEXT_DISPLAY_BOUND'}
    return result
