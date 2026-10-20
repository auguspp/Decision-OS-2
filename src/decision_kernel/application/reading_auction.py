"""One exact auction root in the ordinary read-only Room and store CLI."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from decision_kernel.application.auction_inputs import AUCTION_READING_SCHEMA, AUCTION_VIEW_SCHEMA
from decision_kernel.application.market_custody import READING_SCHEMA, _read_manifest, market_read_scope, read_stored_market
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, Selection, SourceRef, encoded, require
from decision_kernel.observations.market_daily import MarketInputError
from decision_kernel.observations.opening_auction import CODE

MAX_AUCTION_BYTES = 2 * 1024 * 1024


def auction_plan(value):
    require(isinstance(value, dict) and 'reference' in value
            and set(value) <= {'reference', 'offset', 'limit', 'entity_id'}, 'ROOM_AUCTION_PLAN_INVALID')
    ref = SourceRef.from_dict(value['reference'])
    require(ref.selection == Selection(), 'MARKET_CUSTODY_WHOLE_SOURCE_REQUIRED')
    offset, limit, entity = value.get('offset', 0), value.get('limit', 20), value.get('entity_id')
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
            'ROOM_AUCTION_PAGE_INVALID')
    require(entity is None or isinstance(entity, str) and CODE.fullmatch(entity), 'ROOM_AUCTION_ENTITY_INVALID')
    return {'reference': json.loads(encoded(asdict(ref))), 'offset': offset, 'limit': limit, 'entity_id': entity}


def open_auction(store, value, *, registry=None):
    # Shared by the existing global consumer after its original facade is moved
    # unchanged. It owns no transport, budget, source collection or new scope.
    from decision_kernel.application.market_reading_scope import SourceReads, operation_failure
    from decision_kernel.application.reading_room import _failure

    plan = auction_plan(value)
    registry = registry if registry is not None else RelayRegistry(Path('docs/sources/relay-registry.json'))
    with market_read_scope(store) as scoped:
        backend = SourceReads(scoped.backend)
        try:
            reader = VersionedStore(backend)
            result = {'schema': 'reading-auction-v1', 'plan': plan, 'reference': plan['reference'],
                'open_request': {'operation': 'read-source', 'payload': deepcopy(plan['reference'])},
                'source_requests': 0, 'writes': 0, 'next_plan': None}
            try:
                ref = SourceRef.from_dict(plan['reference'])
                _, root = _read_manifest(reader, ref, READING_SCHEMA)
                require(isinstance(root.get('reading'), dict)
                        and root['reading'].get('schema') == AUCTION_READING_SCHEMA, 'ROOM_AUCTION_ROOT_REQUIRED')
                restored = read_stored_market(ref, registry, reader, offset=0, limit=100)
                backend.check()
                view = restored['view']
                result.update(status='readable', view=restored, page=None)
                if view is not None:
                    require(view.get('schema') == AUCTION_VIEW_SCHEMA
                            and (view['cohort_denominator'] is None and not view['cohort_rows']
                                 or view['cohort_denominator'] == len(view['cohort_rows'])),
                            'ROOM_AUCTION_FULL_SCOPE_REQUIRED')
                    rows = [e for e in view['cohort_rows'] if plan['entity_id'] is None
                            or e['entity_id'] == plan['entity_id']]
                    page = rows[plan['offset']:plan['offset'] + plan['limit']]
                    next_offset = plan['offset'] + len(page) if plan['offset'] + len(page) < len(rows) else None
                    result['page'] = {'entries': deepcopy(page), 'matched_count': len(rows),
                        'offset': plan['offset'], 'limit': plan['limit'], 'entity_id': plan['entity_id'],
                        'returned': len(page), 'next_offset': next_offset}
                    if next_offset is not None:
                        result['next_plan'] = {**deepcopy(plan), 'offset': next_offset}
                partial = (restored['status'] != 'restored' or view is None or view['coverage_gaps']
                           or view['queries']['close']['status'] == 'rejected'
                           or view['same_session']['status'] == 'CLOSE_COMPLETION_NOT_ESTABLISHED')
                result['status'] = 'partial' if partial else 'readable'
            except (RecordError, MarketInputError, KeyError, TypeError) as error:
                if operation_failure(error):
                    raise
                result.update(_failure(error))
            backend.check()
            if len(encoded(result)) > MAX_AUCTION_BYTES:
                result = {'schema': 'reading-auction-v1', 'plan': plan, 'status': 'not-included',
                          'error': 'ROOM_AUCTION_DISPLAY_BOUND'}
        finally:
            backend.active = False
    return result
