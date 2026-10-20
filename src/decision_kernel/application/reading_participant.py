"""Bounded family pages over the exact participant result, with whole-scope counts."""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application.participant_reading import read_participants
from decision_kernel.domain.records import Selection, SourceRef, encoded, identifier, require
from decision_kernel.observations.participant_observations import FAMILIES

MAX_PARTICIPANT_PANEL_BYTES = 768 * 1024


def participant_plan(value):
    require(isinstance(value, dict) and {'reference'} <= set(value)
            <= {'reference', 'company_id', 'participant_id', 'family', 'offset', 'limit'},
            'ROOM_PARTICIPANT_PLAN')
    ref = SourceRef.from_dict(value['reference'])
    require(ref.selection == Selection(), 'PARTICIPANT_WHOLE_SOURCE_REQUIRED')
    company, participant = value.get('company_id'), value.get('participant_id')
    require(company is None or participant is None, 'PARTICIPANT_ONE_QUERY_DIRECTION')
    for selector in (company, participant):
        if selector is not None:
            identifier(selector)
    family, offset, limit = value.get('family', FAMILIES[0]), value.get('offset', 0), value.get('limit', 20)
    require(isinstance(family, str) and family in FAMILIES, 'ROOM_PARTICIPANT_FAMILY')
    require(type(offset) is int and offset >= 0 and type(limit) is int and 1 <= limit <= 50,
            'ROOM_PARTICIPANT_PAGE')
    return {'reference': asdict(ref), 'company_id': company, 'participant_id': participant,
            'family': family, 'offset': offset, 'limit': limit}


def open_participants(store, value, *, registry=None):
    """Scope/failures and all family cards remain; only displayed rows are paged."""
    plan = participant_plan(value)
    result = read_participants(plan['reference'], store, company_id=plan['company_id'],
                               participant_id=plan['participant_id'], registry=registry)
    require(result['reference'] == plan['reference'], 'ROOM_PARTICIPANT_REFERENCE_MISMATCH')
    family = next(f for f in result['families'] if f['family'] == plan['family'])
    rows = family['rows'][plan['offset']:plan['offset'] + plan['limit']]
    history_keys = {(r['company_id'], r['participant']['participant_id']) for r in rows if r['participant']}
    view = {key: deepcopy(value) for key, value in result.items()
            if key not in ('families', 'history', 'estimate_changes')}
    view['families'] = [{key: deepcopy(value) for key, value in family.items() if key != 'rows'}
                       | {'query_rows': len(family['rows'])} for family in result['families']]
    view['family'] = plan['family']
    view['rows'] = deepcopy(rows)
    view['history'] = [deepcopy(h) for h in result['history'] if h['family'] == plan['family']
                      and (h['company_id'], h['participant_id']) in history_keys]
    view['estimate_changes'] = [deepcopy(c) for c in result['estimate_changes']
                               if plan['family'] == 'estimate-revisions'
                               and (c['company_id'], c['participant_id']) in history_keys]
    next_offset = plan['offset'] + plan['limit'] if plan['offset'] + plan['limit'] < len(family['rows']) else None
    view['page'] = {'offset': plan['offset'], 'limit': plan['limit'], 'returned': len(rows),
                    'query_rows': len(family['rows']), 'next_offset': next_offset}
    view['source_requests'] = view['writes'] = 0
    require(len(encoded(view)) <= MAX_PARTICIPANT_PANEL_BYTES, 'ROOM_PARTICIPANT_DISPLAY_BOUND')
    return {'reference': deepcopy(plan['reference']), 'plan': plan, 'status': 'readable', 'view': view,
            'next_plan': {**deepcopy(plan), 'offset': next_offset} if next_offset is not None else None,
            'partial': result['status'] == 'partial'}
