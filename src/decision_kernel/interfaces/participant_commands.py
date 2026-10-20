"""Finite participant-reader payloads; this module does not register a host action."""
from dataclasses import asdict

from decision_kernel.application.participant_reading import read_participants
from decision_kernel.domain.records import Selection, SourceRef, identifier, require


def checked_payload(value):
    require(isinstance(value, dict) and {'reference'} <= set(value)
            <= {'reference', 'company_id', 'participant_id'}, 'PARTICIPANT_COMMAND_PAYLOAD')
    reference = SourceRef.from_dict(value['reference'])
    require(reference.selection == Selection(), 'PARTICIPANT_WHOLE_SOURCE_REQUIRED')
    company, participant = value.get('company_id'), value.get('participant_id')
    require(company is None or participant is None, 'PARTICIPANT_ONE_QUERY_DIRECTION')
    for selector in (company, participant):
        if selector is not None:
            identifier(selector)
    return {'reference': asdict(reference), 'company_id': company, 'participant_id': participant}


def perform(store, payload, *, registry=None):
    """The enclosing host owns output custody checks and the trusted registry."""
    plan = checked_payload(payload)
    return read_participants(plan['reference'], store, company_id=plan['company_id'],
                             participant_id=plan['participant_id'], registry=registry)
