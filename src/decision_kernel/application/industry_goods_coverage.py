"""Bounded cross-batch qualification over exact, freshly read goods packets.

This is a projection, not a new source registry or qualification writer. Every
leaf runs the existing reader at one explicit clock. Shared immutable reads and
the original scope/API budgets apply to the entire operation, including aliases.
"""
from copy import deepcopy
from dataclasses import asdict

from decision_kernel.application import industry_goods as goods
from decision_kernel.application.industry_operations import industry_source_scope
from decision_kernel.application.industry_reading import _clock, _document
from decision_kernel.domain.records import (
    RecordError, SourceRef, StoreUnavailable, checked_text, digest,
    encoded, identifier, require,
)

SCHEMA = 'industry-goods-coverage-v1'
MAX_BATCHES = 32
MAX_ALIASES = 128
MAX_READ_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024


def _shape(value, fields):
    require(isinstance(value, dict) and set(value) == set(fields), 'INDUSTRY_COVERAGE_SHAPE')


def _list(value, maximum, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum, 'INDUSTRY_COVERAGE_BOUND')
    return value


def packet_input(value):
    """Validate the declaration before opening any of its dependencies."""
    _shape(value, ('schema', 'prepared_at', 'as_of', 'coverage_note', 'batches', 'aliases'))
    require(value['schema'] == SCHEMA, 'INDUSTRY_COVERAGE_SCHEMA')
    require(_clock(value['prepared_at']) <= _clock(value['as_of']), 'INDUSTRY_PREPARATION_AFTER_CUTOFF')
    checked_text(value['coverage_note'], limit=4096)
    ids, contents = set(), set()
    for batch in _list(value['batches'], MAX_BATCHES, 1):
        _shape(batch, ('batch_id', 'reference'))
        key, ref = identifier(batch['batch_id']), goods._whole(batch['reference'])
        content = encoded([ref.manifest.repository, ref.manifest.path, ref.manifest.sha256])
        require(key not in ids and content not in contents, 'INDUSTRY_COVERAGE_DUPLICATE_BATCH')
        ids.add(key)
        contents.add(content)
    aliases = set()
    for alias in _list(value['aliases'], MAX_ALIASES):
        _shape(alias, ('commodity_id', 'canonical_id', 'evidence'))
        source, target = identifier(alias['commodity_id']), identifier(alias['canonical_id'])
        require(source != target and source not in aliases, 'INDUSTRY_COVERAGE_ALIAS_ID')
        aliases.add(source)
        goods._proofs(alias['evidence'])
        require(all('record_id' not in p and SourceRef.from_dict(p['reference']).selection.kind == 'lines'
                    for p in alias['evidence']), 'INDUSTRY_COVERAGE_ALIAS_EVIDENCE_LINES')
    return deepcopy(value)


def _entity(versions, entity):
    if entity is not None:
        versions.setdefault(entity['entity_id'], set()).add(encoded(entity))


def _proof_entities(proofs, entities):
    for proof in proofs:
        if proof['status'] == 'read':
            for entity in proof['source']['entities']:
                _entity(entities, entity)


def _candidate(batch_id, entry, sources, entities, bound):
    _proof_entities(entry['identity_evidence'], entities)
    resolution = entry['identity_resolution']
    for name in ('original_subject', 'canonical_subject'):
        entity = resolution[name]
        _entity(entities, entity)
        if entity is not None and entity['kind'] == 'commodity' and resolution['status'] == 'evidenced-curation':
            bound.add(entity['entity_id'])
    facts, source_keys, series_keys = set(), set(), set()
    selections = []
    for selection in entry['selections']:
        facts.update(goods._fact_keys(selection, sources))
        if selection['comparable']:
            series_keys.add(selection['series_key'])
        selections.append({k: deepcopy(selection[k]) for k in
                           ('input_id', 'series_id', 'series_key', 'status', 'qualified_series', 'fresh', 'reasons')})
        series = selection['series']
        if series is None:
            continue
        entity = series['subject']
        _entity(entities, entity)
        for point in series['observations']:
            if point['status'] != 'read':
                continue
            if entity['kind'] == 'commodity':
                bound.add(entity['entity_id'])
            source_keys.add((point['source']['source_id'], point['source']['origin']))
            for retained in point['source']['entities']:
                _entity(entities, retained)
    compact = {k: deepcopy(entry[k]) for k in
               ('candidate_id', 'title', 'subject_id', 'commodity_id', 'disposition',
                'status', 'qualified', 'fresh', 'reasons', 'missing', 'reopen_when')}
    compact.update(batch_id=batch_id, candidate_key=[batch_id, entry['candidate_id']],
                   canonical_id=entry['commodity_id'], leaf_status=entry['status'],
                   series_keys=sorted(series_keys), selections=selections)
    return {'entry': compact, 'facts': facts, 'source_keys': source_keys, 'coverage_reasons': set()}


def _aliases(declarations, reader, prepared, entities, bound, candidates):
    """Only direct, evidenced aliases to actual retained commodity roots apply."""
    named = {a['commodity_id']: a for a in declarations}
    leaf_roots = {item['entry']['commodity_id'] for item in candidates}
    chained = set()
    for source, alias in named.items():
        if alias['canonical_id'] in named:
            # Both links of A -> B -> C require a corrected direct-root
            # declaration; this reader neither follows nor repairs chains.
            chained.update((source, alias['canonical_id']))
    retained = [goods._proof_results(a['evidence'], reader, prepared) for a in declarations]
    for proofs in retained:
        _proof_entities(proofs, entities)
    results, valid, invalid = [], {}, set()
    for alias, proofs in zip(declarations, retained):
        source, target = alias['commodity_id'], alias['canonical_id']
        reasons = []
        if source not in leaf_roots:
            reasons.append('INDUSTRY_COVERAGE_ALIAS_SOURCE_NOT_LEAF_ROOT')
        if source in chained:
            reasons.append('INDUSTRY_COVERAGE_ALIAS_NOT_DIRECT_ROOT')
        if not {source, target} <= bound:
            reasons.append('INDUSTRY_COVERAGE_ALIAS_NOT_RETAINED_BOUND')
        if any(len(entities.get(key, ())) != 1 for key in (source, target)):
            reasons.append('INDUSTRY_COVERAGE_ALIAS_ENTITY_CONFLICT')
        if any(p['status'] != 'read' for p in proofs):
            reasons.append('INDUSTRY_COVERAGE_ALIAS_EVIDENCE_UNAVAILABLE')
        proof_ids = {e['entity_id'] for p in proofs if p['status'] == 'read'
                     for e in p['source']['entities'] if e['kind'] == 'commodity'}
        if not {source, target} <= proof_ids:
            reasons.append('INDUSTRY_COVERAGE_ALIAS_EVIDENCE_BINDING')
        compact_proofs = [{k: deepcopy(p[k]) for k in
                          ('reference', 'quote', 'note', 'status', 'reason_code') if k in p}
                         for p in proofs]
        results.append({'commodity_id': source, 'canonical_id': target,
                        'status': 'unavailable' if reasons else 'read',
                        'reasons': reasons, 'evidence': compact_proofs})
        if reasons:
            invalid.add(source)
        else:
            valid[source] = target
    # A target can also be non-root through a leaf's own identity declaration,
    # even when it is not named as the source of another coverage alias.
    subjects = {}
    for item in candidates:
        entry = item['entry']
        if entry['commodity_id'] is not None:
            canonical = valid.get(entry['commodity_id'], entry['commodity_id'])
            subjects.setdefault(entry['subject_id'], set()).add(canonical)
    for alias in results:
        source, target = alias['commodity_id'], alias['canonical_id']
        if alias['status'] == 'read' and subjects.get(target, {target}) != {target}:
            alias['status'] = 'unavailable'
            alias['reasons'].append('INDUSTRY_COVERAGE_ALIAS_NOT_DIRECT_ROOT')
            invalid.add(source)
            valid.pop(source)
    return results, valid, invalid


def _cross_batch(candidates, aliases, invalid_aliases, entities, source_declarations):
    conflicts, subject_owners, fact_owners = [], {}, {}
    # A rejected alias can name an original subject already merged by its leaf.
    # Preserve that uncertainty for every candidate sharing the affected root;
    # otherwise the unconsumed declaration could silently leave two good counts.
    affected_roots = {aliases.get(e['commodity_id'], e['commodity_id'])
                      for item in candidates for e in (item['entry'],)
                      if e['commodity_id'] is not None and
                      (e['subject_id'] in invalid_aliases or e['commodity_id'] in invalid_aliases)}
    bad_entities = {key for key, variants in entities.items() if len(variants) > 1}
    bad_sources = set()
    families = {}
    for key, variants in source_declarations.items():
        identities = {encoded([v['family_id'], v['rights'], v['freshness']]) for v in variants}
        if len(identities) > 1:
            bad_sources.add(key)
        for variant in variants:
            families.setdefault(variant['family_id'], set()).add(encoded(variant['freshness']))
    bad_families = {key for key, variants in families.items() if len(variants) > 1}
    for key, variants in source_declarations.items():
        if any(v['family_id'] in bad_families for v in variants):
            bad_sources.add(key)
    for item in candidates:
        entry = item['entry']
        original = entry['commodity_id']
        entry['canonical_id'] = aliases.get(original, original)
        canonical, subject = entry['canonical_id'], entry['subject_id']
        if original in invalid_aliases or subject in invalid_aliases or canonical in affected_roots:
            item['coverage_reasons'].add('INDUSTRY_COVERAGE_ALIAS_UNAVAILABLE')
        if {subject, original, canonical} & bad_entities:
            item['coverage_reasons'].add('INDUSTRY_COVERAGE_ENTITY_CONFLICT')
        if item['source_keys'] & bad_sources:
            item['coverage_reasons'].add('INDUSTRY_COVERAGE_SOURCE_CONFLICT')
        if canonical is not None:
            subject_owners.setdefault(subject, {}).setdefault(canonical, []).append(item)
            for fact in item['facts']:
                fact_owners.setdefault(fact, {}).setdefault(canonical, []).append(item)
    for kind, owners in (('subject', subject_owners), ('fact', fact_owners)):
        for key, targets in owners.items():
            if len(targets) < 2:
                continue
            affected = [item for values in targets.values() for item in values]
            reason = 'INDUSTRY_COVERAGE_SUBJECT_CONFLICT' if kind == 'subject' else 'INDUSTRY_COVERAGE_FACT_CONFLICT'
            for item in affected:
                item['coverage_reasons'].add(reason)
            conflicts.append({'kind': kind,
                'subject_id' if kind == 'subject' else 'fact_sha256': key if kind == 'subject' else digest(key),
                'canonical_ids': sorted(targets),
                'candidate_keys': [list(k) for k in sorted({tuple(x['entry']['candidate_key']) for x in affected})]})
    nonroots = {}
    for item in candidates:
        canonical = item['entry']['canonical_id']
        targets = subject_owners.get(canonical, {canonical: []})
        if canonical is not None and set(targets) != {canonical}:
            item['coverage_reasons'].add('INDUSTRY_COVERAGE_CANONICAL_NOT_ROOT')
            nonroots.setdefault(canonical, []).append(item['entry']['candidate_key'])
    for canonical, keys in sorted(nonroots.items()):
        conflicts.append({'kind': 'canonical-root', 'commodity_id': canonical,
                          'canonical_ids': sorted(subject_owners[canonical]), 'candidate_keys': keys})
    for entity in sorted(bad_entities):
        conflicts.append({'kind': 'entity', 'entity_id': entity})
    for key in sorted(bad_sources):
        conflicts.append({'kind': 'source', 'source_id': key[0], 'origin': key[1]})
    for family in sorted(bad_families):
        conflicts.append({'kind': 'family-clock', 'family_id': family})
    for alias in sorted(invalid_aliases):
        conflicts.append({'kind': 'alias', 'commodity_id': alias})
    return conflicts


def _outcomes(candidates):
    results = []
    for item in candidates:
        entry, reasons = item['entry'], item['coverage_reasons']
        entry['qualified'] = entry['qualified'] and not reasons
        entry['fresh'] = entry['fresh'] if entry['qualified'] else False
        entry['reasons'] = sorted(set(entry['reasons']) | reasons)
        entry['status'] = ('fresh-qualified' if entry['fresh'] is True
                           else 'qualified-freshness-unknown' if entry['qualified'] and entry['fresh'] is None
                           else 'qualified-not-fresh' if entry['qualified']
                           else 'source-blocked' if entry['disposition'] == 'source-blocked'
                           else 'unresolved' if entry['disposition'] in ('physical-good', 'identity-unresolved')
                           else 'excluded')
        results.append(entry)
    return results


def read_industry_goods_coverage(reference, store, *, evaluation_at, subject_id=None, company_id=None):
    """Read and compare every declared batch before filtering or returning output."""
    require(company_id is None, 'INDUSTRY_COVERAGE_COMPANY_QUERY_USE_BATCH')
    if subject_id is not None:
        identifier(subject_id)
    now, ref = _clock(evaluation_at), goods._whole(reference)
    with industry_source_scope(store) as scoped:
        total, cache, fatal = 0, {}, None
        class Reader:
            # Children retain their normal reader code but do not open another
            # backend scope, API session, or independent commit/request budget.
            backend = object()
            def read_source(self, source_ref):
                nonlocal total, fatal
                if fatal is not None:
                    raise fatal
                key = encoded(asdict(source_ref))
                if key not in cache:
                    try:
                        value = scoped.read_source(source_ref)
                    except RecordError as error:
                        fatal = goods._terminal(error)
                        raise fatal or error
                    total += len(encoded(value))
                    if total > MAX_READ_BYTES:
                        fatal = StoreUnavailable('INDUSTRY_COVERAGE_READING_BOUND')
                        raise fatal
                    cache[key] = value
                return deepcopy(cache[key])
        reader = Reader()
        root = reader.read_source(ref)
        require(root['manifest']['input']['kind'] == 'content' and isinstance(root['data'], str),
                'INDUSTRY_COVERAGE_PACKET_REQUIRED')
        body = packet_input(_document(root['data']))
        prepared, cutoff = _clock(body['prepared_at']), _clock(body['as_of'])
        require(cutoff <= now, 'INDUSTRY_GOODS_EVALUATION_BEFORE_CUTOFF')
        batches, candidates, entities, bound, source_declarations = [], [], {}, set(), {}
        for batch in body['batches']:
            selected = goods._whole(batch['reference'])
            summary = {**deepcopy(batch), 'open_request': {'operation': 'industry-read',
                'payload': {'reference': asdict(selected), 'view': 'industry-goods', 'evaluation_at': evaluation_at}}}
            try:
                leaf_root = reader.read_source(selected)
                require(leaf_root['manifest']['input']['kind'] == 'content' and isinstance(leaf_root['data'], str),
                        'INDUSTRY_GOODS_PACKET_REQUIRED')
                declaration = goods.packet_input(_document(leaf_root['data']))
                require(_clock(declaration['prepared_at']) <= prepared and _clock(declaration['as_of']) <= cutoff,
                        'INDUSTRY_COVERAGE_BATCH_AFTER_CUTOFF')
                leaf = goods.read_industry_goods(selected, reader, evaluation_at=evaluation_at)
            except RecordError as error:
                terminal = fatal or goods._terminal(error)
                if terminal is not None:
                    raise terminal
                batches.append({**summary, 'status': 'unavailable', 'reason_code': str(error), 'scope': None})
                continue
            if fatal is not None:
                raise fatal
            summary.update(status=leaf['status'], scope=deepcopy(leaf['scope']),
                           prepared_at=leaf['prepared_at'], as_of=leaf['as_of'], view_version=leaf['view_version'])
            batches.append(summary)
            sources = {(s['source_id'], s['origin']): s for s in leaf['source_qualifications']}
            for key, source in sources.items():
                _proof_entities(source['evidence'], entities)
                source_declarations.setdefault(key, []).append({k: deepcopy(source[k]) for k in
                    ('source_id', 'origin', 'family_id', 'rights', 'freshness', 'status', 'reopen_when')})
            candidates.extend(_candidate(batch['batch_id'], e, sources, entities, bound) for e in leaf['entries'])
        aliases, valid_aliases, invalid_aliases = _aliases(
            body['aliases'], reader, prepared, entities, bound, candidates)
        if fatal is not None:
            raise fatal
        conflicts = _cross_batch(candidates, valid_aliases, invalid_aliases, entities, source_declarations)
        entries = _outcomes(candidates)
        comparable = sorted({key for e in entries for key in e['series_keys']})
        qualified = sorted({e['canonical_id'] for e in entries if e['qualified']})
        fresh = sorted({e['canonical_id'] for e in entries if e['fresh'] is True})
        unknown = sorted({e['canonical_id'] for e in entries if e['qualified'] and e['fresh'] is None} - set(fresh))
        missing = sum(b['status'] == 'unavailable' for b in batches)
        missing_inputs = sum(b['scope']['unavailable_input_packets'] for b in batches if b['scope'] is not None)
        unresolved = sum(e['status'] == 'unresolved' for e in entries)
        reasons = []
        if missing:
            reasons.append('DECLARED_BATCHES_UNAVAILABLE')
        if missing_inputs:
            reasons.append('DECLARED_BATCH_INPUTS_UNAVAILABLE')
        if conflicts:
            reasons.append('CROSS_BATCH_IDENTITY_OR_SOURCE_CONFLICT')
        if len(fresh) < 500:
            reasons.append('FEWER_THAN_500_FRESH_QUALIFIED_GOODS')
        scope = {'declared_batches': len(batches), 'read_batches': len(batches) - missing,
                 'unavailable_batches': missing, 'candidate_records': len(entries),
                 'unavailable_input_packets': missing_inputs,
                 'comparable_series': len(comparable), 'distinct_qualified_goods': len(qualified),
                 'fresh_goods': len(fresh), 'freshness_unknown_goods': len(unknown),
                 'unresolved_candidates': unresolved,
                 'excluded_candidates': sum(e['status'] == 'excluded' for e in entries),
                 'source_blocked_candidates': sum(e['status'] == 'source-blocked' for e in entries),
                 'conflicts': len(conflicts), 'global_qualified_goods': None, 'global_fresh_goods': None}
        partial = missing or conflicts or any(b['status'] != 'read' for b in batches)
        result = {'schema': 'industry-goods-coverage-result-v1', 'reference': asdict(ref),
            'prepared_at': body['prepared_at'], 'as_of': body['as_of'], 'evaluation_at': evaluation_at,
            'coverage_note': body['coverage_note'], 'status': 'partial' if partial else 'read', 'scope': scope,
            'sets': {'candidate_ids': [e['candidate_key'] for e in entries], 'comparable_series': comparable,
                     'qualified_goods': qualified, 'fresh_goods': fresh, 'freshness_unknown_goods': unknown},
            'batches': batches, 'aliases': aliases, 'conflicts': conflicts,
            'source_qualifications': [{'source_id': key[0], 'origin': key[1],
                'declarations': [json_value for _, json_value in sorted({encoded(v): v for v in variants}.items())]}
                for key, variants in sorted(source_declarations.items())],
            'outcomes': entries, 'entries': entries, 'query': {'subject_id': None, 'company_id': None},
            'quantity_milestone': {'target': 500,
                'status': 'not-established' if reasons else 'established-in-declared-scope',
                'reason': ';'.join(reasons) if reasons else '500-fresh-qualified-goods-in-complete-declared-batches',
                'reasons': reasons, 'scope': 'exact-declared-batches-only'},
            'qualification': 'evidenced-curation-and-retained-comparison-not-independent-authentication',
            'limitations': ['DECLARED_COHORT_IS_NOT_GLOBAL_MARKET_COVERAGE',
                'IDENTITY_ALIAS_AND_RIGHTS_SEMANTICS_REQUIRE_CURATOR_REVIEW',
                'RETAINED_USE_EVIDENCE_DOES_NOT_GRANT_PUBLIC_OUTPUT_PERMISSION',
                'READING_OR_REPACKAGING_DOES_NOT_REFRESH_OBSERVATION_PERIODS',
                'DETAIL_VALUES_AND_ORIGINAL_FAILURES_REMAIN_IN_EXACT_LEAF_READERS',
                'QUANTITY_RESULT_IS_NOT_NATURAL_NEW_PERIOD_RUN_OR_ISSUE_CLOSEOUT',
                'NO_SOURCE_RETRY_RESEARCH_BRIEF_ADOPTION_OR_HUMAN_ACCEPTANCE'],
            'source_requests': 0, 'writes': 0, 'research_executed': False}
        result['view_version'] = digest(encoded(result))
        if len(encoded(result)) > MAX_RESULT_BYTES:
            raise StoreUnavailable('INDUSTRY_COVERAGE_RESULT_BOUND')
        if subject_id is not None:
            result['entries'] = [e for e in entries if subject_id in
                                 (e['subject_id'], e['commodity_id'], e['canonical_id'])]
        result['query'] = {'subject_id': subject_id, 'company_id': None}
        if len(encoded(result)) > MAX_RESULT_BYTES:
            raise StoreUnavailable('INDUSTRY_COVERAGE_RESULT_BOUND')
    return result
