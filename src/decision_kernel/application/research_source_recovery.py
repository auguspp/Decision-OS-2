"""Read-only reconciliation of authenticated source attempts; no retry or new ledger."""
from datetime import datetime

from decision_kernel.domain.records import RecordError, checked_text, encoded, require


def attempt_key(row):
    return (row['run_id'], row['run_attempt'], row['query_index'])


def order(row):
    stamp = datetime.fromisoformat(row['started_at'].replace('Z', '+00:00'))
    require(stamp.tzinfo is not None, 'SOURCE_RECOVERY_TIME_REQUIRED')
    return (stamp, *attempt_key(row))


def _selector(value):
    require(isinstance(value, dict) and set(value) == {'run_id', 'run_attempt', 'query_index'}
            and all(type(value[k]) is int and value[k] >= (0 if k == 'query_index' else 1) for k in value),
            'SOURCE_RECOVERY_SELECTOR_INVALID')
    return attempt_key(value)


def _scope(row):
    parameters = {k:v for k,v in row['parameters'].items() if k not in ('fields', 'limit')}
    parameters.setdefault('offset', '0')
    return encoded({'case_id':row['case_id'], 'api':row['api'], 'parameters':parameters})


def relay_recovery(attempts, corrections):
    """Exact retries recover automatically; field changes require explicit explanation.

    Corrections verify scope/returned fields, NOT the economic meaning of a field
    rename. Missing old fields stay visible, and any rename remains review-required.
    A later failure cannot be erased by pointing to an earlier successful capture.
    """
    require(isinstance(corrections, list) and len(corrections) <= 64, 'SOURCE_RECOVERY_LIMIT')
    by_attempt = {attempt_key(row): row for row in attempts}
    require(len(by_attempt) == len(attempts), 'SOURCE_RECOVERY_ATTEMPT_AMBIGUOUS')
    groups = {}
    for row in attempts:
        groups.setdefault((row['case_id'], row['query_key']), []).append(row)
    current = {}
    for key, group in groups.items():
        actual = [row for row in group if row['state'] != 'planned-not-captured']
        selected = max(actual or group, key=order)
        current[key] = {'case_id':key[0], 'query_key':key[1], 'api':selected['api'],
            'state':selected['state'], 'selected_attempt':dict(zip(('run_id','run_attempt','query_index'),attempt_key(selected))),
            'history_count':len(group), 'historical_incomplete_count':sum(row['state'] not in ('complete','planned-not-captured') for row in group),
            'recovered_by':None, 'missing_fields':[], 'field_mapping_review_required':False}
        if selected['state'] == 'complete' and current[key]['historical_incomplete_count']:
            current[key]['recovered_by'] = current[key]['selected_attempt']
    applied, rejected, used = [], [], set()
    for index, item in enumerate(corrections):
        try:
            require(isinstance(item, dict) and {'from','to','reason'} <= set(item)
                    <= {'from','to','reason','field_map'}, 'SOURCE_RECOVERY_INPUT_INVALID')
            checked_text(item['reason'], limit=4096)
            a, b = _selector(item['from']), _selector(item['to'])
            require(a in by_attempt and b in by_attempt and a != b, 'SOURCE_RECOVERY_RECEIPT_NOT_FOUND')
            old, new = by_attempt[a], by_attempt[b]
            key = (old['case_id'],old['query_key'])
            require(key not in used, 'SOURCE_RECOVERY_DUPLICATE')
            require(old['state'] not in ('complete','planned-not-captured') and new['state']=='complete',
                    'SOURCE_RECOVERY_NOT_FAILED_TO_COMPLETE')
            require(order(new) > order(old) and order(new) >= order(by_attempt[attempt_key(current[key]['selected_attempt'])]),
                    'SOURCE_RECOVERY_NOT_LATER')
            require(_scope(old) == _scope(new), 'SOURCE_RECOVERY_SCOPE_MISMATCH')
            old_fields, new_fields = set(old['parameters']['fields'].split(',')), set(new['parameters']['fields'].split(','))
            mapping = item.get('field_map', {})
            require(isinstance(mapping, dict) and all(isinstance(k,str) and isinstance(v,str) for k,v in mapping.items())
                    and set(mapping) <= old_fields - new_fields and set(mapping.values()) <= new_fields
                    and len(set(mapping.values())) == len(mapping), 'SOURCE_RECOVERY_FIELD_MAP_INVALID')
            missing = sorted(old_fields - new_fields - set(mapping))
            review = bool(mapping)
            state = 'partial-recovery' if missing else 'complete-corrected-fields' if review else 'complete'
            current[key].update(state=state, recovered_by=item['to'], missing_fields=missing,
                                field_mapping_review_required=review)
            used.add(key)
            applied.append({'from':item['from'], 'to':item['to'], 'reason':item['reason'],
                            'field_map':mapping, 'missing_fields':missing, 'state':state,
                            'field_semantics':'requires-review' if review else 'same-named-fields-only'})
        except (RecordError, KeyError, TypeError, ValueError):
            # Keep the raw attempts and refuse to use an unverified replacement.
            rejected.append({'index':index, 'error':'SOURCE_RECOVERY_NOT_VERIFIED'})
    rows = list(current.values())
    complete = bool(rows) and all(row['state'] in ('complete','complete-corrected-fields') for row in rows)
    review = any(row['field_mapping_review_required'] for row in rows)
    state = ('captured-with-corrections' if review else 'captured') if complete else (
        'capture-incomplete' if any(row['state']!='planned-not-captured' for row in rows)
        else 'planned-not-captured' if rows else 'not-executed')
    return {'state':state, 'current_queries':rows, 'corrections':applied, 'rejected_corrections':rejected,
            'historical_attempt_count':len(attempts), 'field_mapping_review_required':review,
            'meaning':'current-declared-query-coverage-not-economic-or-freshness-approval'}
