"""Finite next-open-session observations over an exact original auction cohort.

Pure values only. Calendar selection, completed-source qualification and raw
field availability remain different facts. No price splice or return is made.
"""
from copy import deepcopy
from datetime import datetime, time, timedelta, timezone

from decision_kernel.domain.records import digest, encoded, source_time
from decision_kernel.observations.market_daily import MarketInputError, trade_day
from decision_kernel.observations.opening_auction import CODE, GROUPS, PROFILE, group

MAX_COHORTS = 16
MAX_CALENDAR_DAYS = 512
SOURCE_FIELDS = ('cohort_id', 'entity_id', 'name', 'prior_turnover_pct', 'status',
                 'auction_price', 'auction_pre_close', 'gap_pct', 'matched_profile',
                 'prior_rows', 'prior_row_indices')


def require(ok, code):
    if not ok:
        raise MarketInputError('AUCTION_NEXT_' + code)


def clock(value):
    require(isinstance(value, str), 'CLOCK_INVALID')
    source_time(value)
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.utcoffset() is not None, 'CLOCK_INVALID')
    return result


def seed(view, reference, checked_at):
    require(view.get('schema') == 'relay-auction-context-v1' and view.get('profile') == PROFILE,
            'ORIGINAL_PROFILE')
    trade_day(view.get('market_session'))
    rows = view.get('cohort_rows')
    require(isinstance(rows, list), 'ORIGINAL_ROWS')
    if view.get('cohort_denominator') is None:
        require(not rows, 'ORIGINAL_DENOMINATOR')
        return None
    require(type(view['cohort_denominator']) is int and len(rows) == view['cohort_denominator'],
            'ORIGINAL_DENOMINATOR')
    members = [{**{k: deepcopy(row[k]) for k in SOURCE_FIELDS}, 'group': group(row)} for row in rows]
    ids = [r['cohort_id'] for r in members]
    codes = [r['entity_id'] for r in members if r['entity_id'] is not None]
    require(len(set(ids)) == len(ids) and len(set(codes)) == len(codes)
            and all(isinstance(c, str) and CODE.fullmatch(c) for c in codes), 'ORIGINAL_MEMBERS')
    groups = {g: sum(r['group'] == g for r in members) for g in GROUPS}
    require(groups == {g: view['groups'][g]['denominator'] for g in GROUPS}
            and groups['MATCHED'] == view['matched']
            and sum(groups[g] for g in ('MATCHED', 'NOT_MATCHED', 'AUCTION_UNRESOLVED'))
            == view['static_eligible'], 'ORIGINAL_GROUPS')
    for lineage in view['source_lineage'].values():
        for page in lineage['pages']:
            require(clock(page['captured_at']) <= clock(checked_at), 'SOURCE_IN_FUTURE')
    identity = {'profile': PROFILE, 'market_session': view['market_session'],
                'cohort_version': view['cohort_version']}
    return {'id': digest(encoded(identity)), **identity, 'auction_reference': deepcopy(reference),
            'cohort_denominator': len(members), 'members': members,
            'members_hash': digest(encoded(members)), 'original_groups': groups,
            'prior_temperature': deepcopy(view['prior_temperature']),
            'original_coverage': deepcopy(view['coverage']), 'original_coverage_gaps': view['coverage_gaps'],
            'auction_timeliness': view['timeliness'], 'first_recorded_at': checked_at}


def target_session(cohort, calendar, through, previous=None):
    """Fix the first explicit open, independently of a quote or completion clock."""
    old = (previous or {}).get('calendar_prefix', [])
    for row in old:
        require(calendar.get(row['session']) is row['is_open'], 'CALENDAR_REVISED')
    start = trade_day(cohort['market_session'])
    try:
        day = start + timedelta(days=1)
    except OverflowError:
        raise MarketInputError('AUCTION_NEXT_CALENDAR_WINDOW_INVALID') from None
    end = trade_day(through) if through is not None else start
    prefix, target, gap = [], None, None
    while day <= end:
        require(len(prefix) < MAX_CALENDAR_DAYS, 'CALENDAR_BOUND')
        text = day.isoformat()
        if text not in calendar:
            gap = text
            break
        require(type(calendar[text]) is bool, 'CALENDAR_VALUE')
        prefix.append({'session': text, 'is_open': calendar[text]})
        if calendar[text]:
            target = text
            break
        if day == end:
            break
        try:
            day += timedelta(days=1)
        except OverflowError:
            raise MarketInputError('AUCTION_NEXT_CALENDAR_WINDOW_INVALID') from None
    require((previous or {}).get('target_session') in (None, target), 'TARGET_CHANGED')
    return target, prefix, gap


def dated_result(cohort, target, gap, quote, checked_at):
    completion = quote['completion'] if quote is not None else 'NOT_ESTABLISHED'
    completed = completion == 'RECEIVED_AFTER_SESSION_COMPLETION'
    rows = []
    for member in cohort['members']:
        code = member['entity_id']
        row = {k: member[k] for k in ('cohort_id', 'entity_id', 'group')}
        default = ('COHORT_IDENTITY_UNRESOLVED' if code is None else 'CALENDAR_GAP' if gap else
                   'TARGET_NOT_ESTABLISHED' if target is None else 'QUOTE_NOT_RETAINED')
        item = quote['entries'].get(code) if quote is not None and code is not None else None
        for field in ('open', 'close'):
            value, state = (None, default) if quote is None or code is None else (
                (None, 'ROW_NOT_RETURNED') if item is None else
                (item[field]['value'], item[field]['status']))
            row['readable_' + field] = value
            row['next_' + field] = value if completed else None
            row[field + '_status'] = state
        rows.append(row)
    groups = {g: {'denominator': sum(r['group'] == g for r in rows),
                 **{field + '_readable': sum(r['group'] == g and r['readable_' + field] is not None for r in rows)
                    for field in ('open', 'close')},
                 **{field + '_observed': sum(r['group'] == g and r['next_' + field] is not None for r in rows)
                    for field in ('open', 'close')}} for g in GROUPS}
    status = ('CALENDAR_GAP' if gap else 'WAITING_FOR_TARGET_SESSION' if target is None else
              'TARGET_COMPLETION_NOT_ESTABLISHED' if not completed else
              'NEXT_SESSION_OBSERVED' if all(r['next_open'] is not None and r['next_close'] is not None for r in rows)
              else 'NEXT_SESSION_OBSERVED_WITH_GAPS')
    received = quote['received_through'] if quote is not None else None
    timing = 'NOT_ESTABLISHED'
    if received and completed:
        zone = timezone(timedelta(hours=8))
        end = datetime.combine(trade_day(target), time.max, tzinfo=zone)
        timing = ('SAME_SESSION_AFTER_CLOSE' if clock(received) <= end else
                  'LATER_ACQUISITION_NOT_HISTORICAL_KNOWLEDGE')
    result = {'target_session': target, 'status': status, 'rows': rows, 'groups': groups,
              'completion_clock': completion, 'completed_session_cutoff': target + 'T15:30:00+08:00' if target else None,
              'quote_key': quote['key'] if quote is not None else None,
              'quote_lineage': deepcopy(quote['lineage']) if quote is not None else None,
              'row_coverage': deepcopy(quote['coverage']) if quote is not None else None,
              'acquisition_timing': timing,
              'price_basis': 'RAW_OPEN_CLOSE_ONLY_NOT_CROSS_SESSION_ADJUSTED_RETURN'}
    result['result_hash'] = digest(encoded({'target_session': target, 'rows': rows}))
    result['first_recorded_at'] = checked_at
    return result


def project(cohort, calendar, through, quotes, checked_at, previous=None):
    require(cohort['members_hash'] == digest(encoded(cohort['members'])), 'MEMBERS_CHANGED')
    target, prefix, gap = target_session(cohort, calendar, through, previous)
    current = dated_result(cohort, target, gap, quotes.get(target), checked_at)
    prior = (previous or {}).get('result')
    lost = []
    if prior is not None:
        require([(r['cohort_id'], r['entity_id'], r['group']) for r in prior['rows']]
                == [(r['cohort_id'], r['entity_id'], r['group']) for r in current['rows']], 'PREVIOUS_MEMBERS_CHANGED')
        if prior['target_session'] == target:
            lost = [{'cohort_id': old['cohort_id'], 'entity_id': old['entity_id'], 'field': field,
                     'status': new[field + '_status'] if new['readable_' + field] is None
                     else current['completion_clock']}
                    for old, new in zip(prior['rows'], current['rows']) for field in ('open', 'close')
                    if (old['readable_' + field] is not None and new['readable_' + field] is None)
                    or (old['next_' + field] is not None and new['next_' + field] is None)]
    result = deepcopy(prior) if lost else current
    unchanged = bool(prior and result['result_hash'] == prior['result_hash'])
    if unchanged:
        result['first_recorded_at'] = prior['first_recorded_at']
    attempt = {'quote_key': current['quote_key'], 'completion_clock': current['completion_clock'],
               'quote_lineage': current['quote_lineage'], 'row_coverage': current['row_coverage'],
               'unavailable_previously_observed_fields': lost}
    return {**deepcopy(cohort), 'target_session': target, 'calendar_prefix': prefix, 'calendar_gap': gap,
            'result': result, 'current_attempt': attempt,
            'observation': 'PREVIOUS_DATED_OBSERVATIONS_RETAINED' if lost else
                           'UNCHANGED_DATED_OBSERVATIONS' if unchanged else 'DATED_OBSERVATIONS_UPDATED'}


def retain_cohorts(prior, current, previous_reference):
    cohorts, archived = {}, []
    for old in (prior or {}).get('cohorts', []):
        terminal = old['result']['completion_clock'] == 'RECEIVED_AFTER_SESSION_COMPLETION'
        if terminal and current and old['id'] != current['id'] and current['market_session'] > old['target_session']:
            archived.append({'id': old['id'], 'market_session': old['market_session'],
                             'target_session': old['target_session'], 'result_hash': old['result']['result_hash'],
                             'reference': deepcopy(previous_reference)})
        else:
            cohorts[old['id']] = deepcopy(old)
    if current:
        old = cohorts.get(current['id'])
        require(old is None or old['members_hash'] == current['members_hash'], 'ORIGINAL_COHORT_CHANGED')
        cohorts.setdefault(current['id'], current)
    require(len(cohorts) <= MAX_COHORTS, 'ACTIVE_COHORT_BOUND')
    return list(cohorts.values()), archived
