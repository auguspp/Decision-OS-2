"""Whole-research source use, using the existing catalogue and HTTP; not an AI or store."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
import time

from decision_kernel.application.relay_registry import RelayRegistry, PUBLIC_READ_GROUPS
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded, identifier, require, source_time
from decision_kernel.infrastructure.relay_http import ROOT, RelayHTTP
from decision_kernel.infrastructure.relay_retry import RetryingRelay, RetryPolicy

STAGES = ('baseline', 'business', 'financial', 'valuation', 'challenge', 'revision', 'delivery')
# Names are defined by the existing registry; unclassified/private scopes never gain admission.
PUBLIC_GROUPS = PUBLIC_READ_GROUPS
SCHEMA = 'research-source-plan-v1'
CAPTURE = 'registered-relay-capture-v1'
NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
SENSITIVE = re.compile(r'(?:token|secret|password|authorization|api_key|cookie|email|phone|mobile|'
                       r'contact|passport|id_card|full_text|content|abstr|summary|participants|recep_person|fund_visitors|author)', re.I)
COMMON_PARAMETERS = frozenset({'ts_code', 'ann_date', 'f_ann_date', 'end_date', 'start_date', 'trade_date',
    'report_date', 'surv_date', 'period', 'month', 'start_m', 'end_m', 'start_q', 'end_q', 'date',
    'exchange', 'exchange_id', 'index_code', 'index_symbol', 'industry_code', 'market', 'list_status',
    'fields', 'limit', 'offset', 'report_type', 'comp_type', 'type', 'src', 'freq', 'asset', 'adj',
    'inst_csname', 'name', 'symbol', 'industry', 'level', 'is_new', 'is_open', 'nav_date', 'cal_date',
    'start_time', 'end_time', 'status', 'fund_type', 'is_hs', 'curr_type'})
FORBIDDEN_PARAMS = {'url', 'host', 'endpoint', 'proxy', 'callback', 'redirect', 'method', '__probe'}
AUCTION_OPTIONAL_FIELDS = {
    'limit_list_d': frozenset({'name', 'close', 'turnover_ratio', 'limit_times'}),
    'stk_auction': frozenset({'price', 'pre_close'}),
}
# Daily retains the same finite clock for the auction's completed-session use;
# no existing daily reader gains a new required field or timing qualification.
REQUEST_CLOCK_APIS = frozenset(AUCTION_OPTIONAL_FIELDS) | {'daily'}
PAGE_COUNTS = ('count', 'total')
PAGE_FLAGS = ('has_more', 'has_next', 'truncated', 'next', 'next_page', 'next_cursor')


def _page_evidence(body, rows):
    """Finite response metadata, without retaining cursor text or diagnostics."""
    out = {'schema': 'relay-page-coverage-v1', 'body': {}, 'data': {}}
    for name, obj in (('body', body), ('data', body['data'])):
        for key in PAGE_COUNTS:
            if key in obj:
                require(type(obj[key]) is int and obj[key] >= rows, 'SOURCE_COUNT_INVALID')
                out[name][key] = obj[key]
        for key in PAGE_FLAGS:
            if key not in obj:
                continue
            value = obj[key]
            if key in ('has_more', 'has_next', 'truncated'):
                require(value is None or type(value) is bool, 'SOURCE_PAGE_METADATA_INVALID')
            else:
                require(value is None or type(value) is bool or type(value) is int and value >= 0
                        or isinstance(value, str) and len(value.encode()) <= 128
                        and not any(ord(c) < 32 or ord(c) == 127 for c in value),
                        'SOURCE_PAGE_METADATA_INVALID')
            out[name][key] = value not in (None, False, '')
    for key in PAGE_COUNTS:
        values = [obj[key] for obj in (out['body'], out['data']) if key in obj]
        require(len(set(values)) <= 1, 'SOURCE_PAGE_COUNT_CONFLICT')
    counts = [obj['count'] for obj in (out['body'], out['data']) if 'count' in obj]
    totals = [obj['total'] for obj in (out['body'], out['data']) if 'total' in obj]
    require(not counts or not totals or max(counts) <= min(totals), 'SOURCE_PAGE_COUNT_CONFLICT')
    return out


def _declared_rows(table):
    evidence = table.get('coverage_evidence') or {}
    return max([table['reported_count'] or 0] + [obj[key]
        for name in ('body', 'data') for obj in [evidence.get(name, {})]
        for key in PAGE_COUNTS if key in obj])


def _consistent_total(tables):
    # Only these finite evidence-bearing pages declare total. A per-page count
    # may shrink on the tail; an explicit total cannot change or be overrun.
    totals = {obj['total'] for table in tables for name in ('body', 'data')
              for obj in [table.get('coverage_evidence', {}).get(name, {})] if 'total' in obj}
    require(len(totals) <= 1, 'SOURCE_PAGE_COUNT_CONFLICT')
    return next(iter(totals)) if totals else None


def _more_rows(table):
    evidence = table.get('coverage_evidence') or {}
    return any(evidence.get(name, {}).get(key, False)
               for name in ('body', 'data') for key in PAGE_FLAGS)


def _request_clock(value, captured_at):
    require(isinstance(value, dict) and set(value) == {'requested_at', 'received_at'},
            'SOURCE_REQUEST_CLOCK_INVALID')
    stamps = []
    for item in (value['requested_at'], value['received_at'], captured_at):
        source_time(item)
        stamp = datetime.fromisoformat(item.replace('Z', '+00:00'))
        require(stamp.utcoffset() is not None, 'SOURCE_REQUEST_CLOCK_INVALID')
        stamps.append(stamp)
    require(stamps[0] <= stamps[1] <= stamps[2], 'SOURCE_REQUEST_CLOCK_INVALID')
    return deepcopy(value)


def _field_name(api, value):
    """Keep general names strict; only Shibor admits its seven documented tenor columns."""
    return isinstance(value, str) and (NAME.fullmatch(value) is not None or
        api == "shibor" and value in {"1w", "2w", "1m", "3m", "6m", "9m", "1y"})


def source_use_guidance(kind):
    """Returned at initial prepare AND resume, independent of open findings/gaps."""
    return {'policy': 'sources-throughout-research-v1', 'kind': kind,
        'available_from_start': True, 'gap_required': False, 'stages': list(STAGES),
        'instruction': '从研究开局起把接口与公告/原件并列使用；分析、估值、挑战及修订均可选用相关接口。'
                       '先读已有资料并判断时效，必要时取新数据；不强制逐阶段重复抓取，不以接口替代附注或研究判断。',
        'registry': 'docs/sources/relay-registry.json',
        'guide': 'docs/research/source-use.md',
        'source_workflow': 'relay-source-runtime.yml', 'family': 'research',
        'cli': 'python -m decision_kernel.interfaces.relay_research_cli',
        'execution_authority': 'existing-user-mandate-and-host-permission-not-granted-by-preparation',
        'acceptance': 'actual-source-use-in-research-not-call-count-or-automatic-economic-approval'}


def admitted(registry, api):
    require(isinstance(registry, RelayRegistry), 'SOURCE_TRUSTED_REGISTRY_REQUIRED')
    item = registry.lookup(api)
    require(item['registered_reader_candidate'] is True,
            'SOURCE_PUBLIC_GET_NOT_ADMITTED')
    return item


def parameters_for(registry, api, parameters):
    item = admitted(registry, api)
    require(isinstance(parameters, dict) and parameters, 'SOURCE_PARAMETERS_REQUIRED')
    result = {}
    allowed = COMMON_PARAMETERS | set(item['required'] or []) | {k for g in (item['required_any'] or []) for k in g}
    if api == 'stk_auction':
        allowed |= {'ts_type'}
    for key, value in parameters.items():
        require(isinstance(key, str) and NAME.fullmatch(key) and key in allowed and key not in FORBIDDEN_PARAMS
                and not SENSITIVE.search(key), 'SOURCE_PARAMETER_NOT_ALLOWED')
        require(type(value) in (str, int) and str(value).strip()
                and len(str(value)) <= 8192 and not any(ord(c) < 32 or ord(c) == 127 for c in str(value)),
                'SOURCE_PARAMETER_VALUE_INVALID')
        result[key] = str(value)
    if api == 'stk_auction':
        require(result.get('ts_type') == 'STK', 'SOURCE_AUCTION_STOCK_TYPE_REQUIRED')
    fields = result.get('fields', '').split(',')
    require(1 <= len(fields) <= 128 and len(set(fields)) == len(fields)
            and all(_field_name(api, f) and not SENSITIVE.search(f) for f in fields),
            'SOURCE_EXPLICIT_SAFE_FIELDS_REQUIRED')
    require(set(result) - {'fields', 'limit', 'offset'}, 'SOURCE_BUSINESS_SCOPE_REQUIRED')
    require(result.get('limit', '').isascii() and result['limit'].isdigit()
            and 1 <= int(result['limit']) <= 5000, 'SOURCE_LIMIT_REQUIRED')
    if 'offset' in result:
        require(result['offset'].isascii() and result['offset'].isdigit(), 'SOURCE_OFFSET_INVALID')
    check = registry.check_parameters(api, result)
    require(check['gateway_requirements_satisfied'], 'SOURCE_GATEWAY_PARAMETERS_INVALID')
    return result, item


def validate_plan(plan, registry):
    require(isinstance(plan, dict) and set(plan) == {'schema', 'case_id', 'requests'}
            and plan['schema'] == SCHEMA, 'SOURCE_PLAN_INVALID')
    identifier(plan['case_id'])
    require(isinstance(plan['requests'], list) and plan['requests'], 'SOURCE_REQUESTS_REQUIRED')
    normalized = []
    for request in plan['requests']:
        require(isinstance(request, dict) and set(request) == {'stage', 'purpose', 'api', 'parameters'},
                'SOURCE_REQUEST_INVALID')
        require(request['stage'] in STAGES and isinstance(request['purpose'], str)
                and 0 < len(request['purpose'].strip()) <= 1024, 'SOURCE_USE_REQUIRED')
        parameters, item = parameters_for(registry, request['api'], request['parameters'])
        normalized.append({**request, 'parameters': parameters,
            'query_key': digest(encoded({'api': request['api'], 'parameters': parameters})),
            'group': item['group']})
    return {'schema': SCHEMA, 'case_id': plan['case_id'], 'requests': normalized,
            'snapshot': deepcopy(registry.snapshot), 'network_requests': 0,
            'authority': 'caller-mandate-required', 'economic_assessment': 'not-performed'}


def _table(api, params, body, *, preserve_coverage=True):
    require(isinstance(body, dict) and type(body.get('code')) is int and body['code'] == 0
            and body.get('ok', True) is True and body.get('success', True) is True
            and body.get('error') in (None, '') and body.get('api_name') in (None, api), 'SOURCE_BUSINESS_FAILED')
    data = body.get('data')
    require(isinstance(data, dict), 'SOURCE_TABLE_INVALID')
    fields, rows = data.get('fields'), data.get('items')
    require(isinstance(fields, list) and len(fields) <= 512
            and all(_field_name(api, f) for f in fields) and len(set(fields)) == len(fields), 'SOURCE_FIELDS_INVALID')
    require(isinstance(rows, list) and len(rows) <= int(params['limit']), 'SOURCE_ROWS_INVALID')
    requested = params['fields'].split(',')
    required = set(requested) - AUCTION_OPTIONAL_FIELDS.get(api, set())
    require(not rows or required <= set(fields), 'SOURCE_REQUESTED_FIELDS_MISSING')
    selected = [f for f in fields if f in requested]
    indices = [fields.index(f) for f in selected]
    projected = []
    for row in rows:
        require(isinstance(row, list) and len(row) == len(fields), 'SOURCE_ROW_WIDTH_INVALID')
        values = [row[n] for n in indices]
        require(all(v is None or type(v) in (str, int, float, bool) for v in values), 'SOURCE_CELL_INVALID')
        require(all(not isinstance(v, str) or len(v.encode()) <= 8192 for v in values), 'SOURCE_TEXT_TOO_LARGE')
        for field, value in zip(selected, values):
            if value not in (None, '') and (field in {'url', 'link'} or field.endswith('_url')):
                from decision_kernel.observations.relay_participants import _locator
                require(isinstance(value, str), 'SOURCE_LOCATOR_INVALID')
                _locator(value)  # Reuse reviewed public locator checks; never follow it.
        item = dict(zip(fields, row))
        for parameter, field in (('ts_code', 'ts_code'), ('period', 'end_date'),
                                 ('trade_date', 'trade_date'), ('report_date', 'report_date')):
            if parameter in params and field in item:
                require(str(item[field]) in params[parameter].split(','), 'SOURCE_RETURNED_SCOPE_MISMATCH')
        projected.append(values)
    count = body.get('count')
    require(count is None or type(count) is int and count >= len(rows), 'SOURCE_COUNT_INVALID')
    value = {'fields': selected, 'items': projected, 'reported_count': count,
             'missing_requested_fields': [f for f in requested if f not in fields],
             'omitted_unrequested_columns': len(fields) - len(selected),
             'representation': 'requested-column-projection-not-original-http',
             'units': 'NOT_ESTABLISHED_BY_GENERIC_READER', 'business_semantics': 'REQUIRES_SOURCE_SPECIFIC_CHECK'}
    if api in AUCTION_OPTIONAL_FIELDS and preserve_coverage:
        value['coverage_evidence'] = _page_evidence(body, len(rows))
    encoded(value)  # Reject non-finite numeric cells without converting or rounding them.
    return value


def capture_registered(request, registry, session, *, max_pages=100, checkpoint=None,
                       checkpoint_writer=None, pause_requested=None):
    params, item = parameters_for(registry, request['api'], request['parameters'])
    require(type(max_pages) is int and max_pages > 0, 'SOURCE_PAGE_BOUND_INVALID')
    result = {'schema': CAPTURE, 'api': request['api'], 'parameters': params,
        'query_key': request['query_key'], 'pages': [], 'complete': False, 'error': None,
        'snapshot': deepcopy(registry.snapshot), 'source_identity': 'THIRD_PARTY_RELAY_NOT_ISSUER',
        'coverage': 'REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS'}
    controller = None
    if checkpoint is not None or checkpoint_writer is not None or pause_requested is not None:
        from decision_kernel.application.relay_capture_checkpoint import CaptureCheckpoint, pause_reason
        controller = CaptureCheckpoint(request, registry, session, result, max_pages=max_pages,
                                       value=checkpoint, writer=checkpoint_writer)
        result = controller.capture
        if controller.control['state'] in ('complete', 'terminal'):
            return result  # Exact retained completion/failure: no credential or source request.
    offset, seen, total, declared = int(params.get('offset', 0)), set(), 0, 0
    for page in result['pages']:
        table = page['table']
        seen.add(digest(encoded({'fields': table['fields'], 'items': table['items']})))
        offset += len(table['items'])
        total += len(encoded(table))
        declared = max(declared, _declared_rows(table))
    for page_number in range(len(result['pages']) + 1, max_pages + 1):
        page_params = {**params, 'offset': str(offset)} if offset else dict(params)
        if controller is not None and controller.control['retry'] is None:
            requested_pause = pause_reason(pause_requested)
            if requested_pause:
                controller.pause(requested_pause, page_params)
                return result
        try:
            call = lambda: session.client.request_registered(registry, request['api'], page_params)
            body = (session.run(call) if controller is None else session.run(call,
                continuation=controller.control['retry'], checkpoint=controller.retry_progress,
                pause_requested=pause_requested))
            result['request_execution'] = {'page_number': page_number,
                'retry_decision': deepcopy(session.last_retry_decision)}
            table = _table(request['api'], page_params, body)
            fingerprint = digest(encoded({'fields': table['fields'], 'items': table['items']}))
            require(not table['items'] or fingerprint not in seen, 'SOURCE_PAGINATION_STALLED')
            seen.add(fingerprint)
            total += len(encoded(table))
            require(total <= 8 * 1024 * 1024, 'SOURCE_CAPTURE_MEMORY_LIMIT')
            captured_at = session.clock()
            transport = deepcopy(session.client.last_response_meta)
            if request['api'] in REQUEST_CLOCK_APIS and session.last_attempts:
                last = session.last_attempts[-1]
                require(last.get('error') is None and last.get('transport') == transport,
                        'SOURCE_REQUEST_CLOCK_INVALID')
                transport['request_clock'] = _request_clock(
                    {k: last.get(k) for k in ('requested_at', 'received_at')}, captured_at)
            expected_total = _consistent_total([p['table'] for p in result['pages']] + [table])
            require(expected_total is None or offset + len(table['items']) <= expected_total,
                    'SOURCE_PAGE_COUNT_CONFLICT')
            result['pages'].append({'offset': offset, 'captured_at': captured_at, 'table': table,
                'transport': transport, 'attempts': deepcopy(session.last_attempts)})
            offset += len(table['items'])
            declared = max(declared, _declared_rows(table))
            evidence = table.get('coverage_evidence') or {}
            require(not any(evidence.get(name, {}).get('truncated', False) for name in ('body', 'data')),
                    'SOURCE_EXPLICIT_TRUNCATION')
            if len(table['items']) < int(params['limit']):
                require(declared <= offset and not _more_rows(table), 'SOURCE_COUNT_SUGGESTS_TRUNCATION')
                result['complete'] = True
                if controller is not None:
                    controller.accepted(complete=True)
                return result
            if controller is not None and page_number < max_pages:
                controller.accepted(complete=False)
        except RecordError as exc:
            result['request_execution'] = {'page_number': page_number,
                'retry_decision': deepcopy(session.last_retry_decision)}
            result.update(error=str(exc), failure=deepcopy(session.last_error_meta),
                          pending_retry=deepcopy(session.pending_retry), attempts=deepcopy(session.last_attempts),
                          next_parameters=page_params)
            if controller is not None:
                controller.failed()
            return result
    result.update(error='SOURCE_PAGE_BOUND_REACHED', next_parameters={**params, 'offset': str(offset)})
    if controller is not None:
        controller.control['page_number'] = max_pages + 1
        controller.failed()
    return result


def capture_plan(plan, registry, *, client=None, public_rights_confirmed=False, policy=None,
                 max_pages=100, run_seconds=900, sleep=time.sleep, monotonic=time.monotonic, clock=None):
    require(public_rights_confirmed is True, 'SOURCE_PUBLIC_RIGHTS_REQUIRED')
    checked = validate_plan(plan, registry)  # Entire batch first, before obtaining credentials or any source traffic.
    require(type(run_seconds) is int and 30 <= run_seconds <= 3600, 'SOURCE_RUN_BOUND_INVALID')
    client = client or RelayHTTP()
    client.validate_credential()
    session = RetryingRelay(client, policy, sleep=sleep, monotonic=monotonic, clock=clock,
                            deadline=monotonic() + run_seconds)
    obtained, stopped = {}, None
    for request in checked['requests']:
        key = request['query_key']
        if key in obtained:
            yield {'case_id': plan['case_id'], 'use': request, 'status': 'reused-in-this-batch' if obtained[key]['complete'] else 'prior-incomplete-in-this-batch',
                   'capture': deepcopy(obtained[key])}
            continue
        if stopped:
            yield {'case_id': plan['case_id'], 'use': request, 'status': 'not-attempted', 'error': stopped, 'capture': None}
            continue
        capture = capture_registered(request, registry, session, max_pages=max_pages)
        obtained[key] = capture  # Same failed query is not silently retried again under another stage.
        category = (capture.get('failure') or {}).get('classification')
        if category in {'auth-or-entitlement', 'rate-limit'} or capture['error'] in {'RELAY_RUN_DEADLINE', 'RELAY_SECRET_REFLECTED'}:
            stopped = capture['error']
        yield {'case_id': plan['case_id'], 'use': request,
               'status': 'captured' if capture['complete'] else 'capture-incomplete', 'capture': capture}


def _inspect_result(result, registry):
    """Check every supplied page without creating an unused export copy.

    The private result borrows the input pages. Callers may inspect its metadata
    synchronously; only ``inspect_result`` exposes a detached page projection.
    This remains local validation, not publisher or economic authentication.
    """
    require(isinstance(result, dict) and isinstance(result.get('use'), dict), 'SOURCE_RESULT_INVALID')
    use = result['use']
    checked = validate_plan({'schema': SCHEMA, 'case_id': result.get('case_id'),
        'requests': [{k: use.get(k) for k in ('stage', 'purpose', 'api', 'parameters')}]}, registry)['requests'][0]
    require(use == checked, 'SOURCE_USE_CHANGED')
    capture = result.get('capture')
    require(isinstance(capture, dict) and capture.get('schema') == CAPTURE
            and type(capture.get('complete')) is bool, 'SOURCE_CAPTURE_INVALID')
    require(capture.get('api') == checked['api'] and capture.get('parameters') == checked['parameters']
            and capture.get('query_key') == checked['query_key'], 'SOURCE_CAPTURE_SCOPE_MISMATCH')
    require(capture.get('error') is None or isinstance(capture['error'], str)
            and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', capture['error']), 'SOURCE_CAPTURE_ERROR_INVALID')
    pages = capture.get('pages')
    require(isinstance(pages, list) and len(encoded(pages)) <= 16 * 1024 * 1024, 'SOURCE_CAPTURE_PAGES_INVALID')
    params = checked['parameters']
    offset, count, declared, seen, previous = int(params.get('offset', 0)), 0, 0, set(), None
    for index, page in enumerate(pages):
        require(isinstance(page, dict) and set(page) == {'offset', 'captured_at', 'table', 'transport', 'attempts'}
                and type(page['offset']) is int and page['offset'] == offset, 'SOURCE_PAGE_SEQUENCE_INVALID')
        source_time(page['captured_at'])
        stamp = datetime.fromisoformat(page['captured_at'].replace('Z', '+00:00'))
        require(stamp.utcoffset() is not None and (previous is None or stamp >= previous), 'SOURCE_PAGE_CLOCK_INVALID')
        previous = stamp
        table = page['table']
        table_keys = {'fields', 'items', 'reported_count', 'missing_requested_fields',
                      'omitted_unrequested_columns', 'representation', 'units', 'business_semantics'}
        require(isinstance(table, dict) and table_keys <= set(table)
                <= table_keys | ({'coverage_evidence'} if checked['api'] in AUCTION_OPTIONAL_FIELDS else set()),
            'SOURCE_PROJECTED_TABLE_INVALID')
        require(type(table['omitted_unrequested_columns']) is int and 0 <= table['omitted_unrequested_columns'] <= 512,
                'SOURCE_PROJECTION_COUNT_INVALID')
        body = {'code': 0, 'data': {'fields': table['fields'], 'items': table['items']}}
        evidence = table.get('coverage_evidence')
        if evidence is not None:
            require(isinstance(evidence, dict) and set(evidence) == {'schema', 'body', 'data'}
                    and evidence['schema'] == 'relay-page-coverage-v1'
                    and all(isinstance(evidence[k], dict) and set(evidence[k]) <= set(PAGE_COUNTS + PAGE_FLAGS)
                            for k in ('body', 'data')), 'SOURCE_PAGE_METADATA_INVALID')
            body.update(evidence['body'])
            body['data'].update(evidence['data'])
        else:
            body['count'] = table['reported_count']
        rebuilt = _table(checked['api'], params, body, preserve_coverage=evidence is not None)
        require(all(table[k] == rebuilt[k] for k in table if k != 'omitted_unrequested_columns'),
                'SOURCE_PROJECTION_CHANGED')
        meta = page['transport']
        meta_keys = {'http_status','x_request_id','x_cache','wire_sha256'}
        require(isinstance(meta, dict) and meta_keys <= set(meta)
                <= meta_keys | ({'request_clock'} if checked['api'] in REQUEST_CLOCK_APIS else set())
                and type(meta['http_status']) is int and meta['http_status'] == 200
                and isinstance(meta['wire_sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', meta['wire_sha256']),
                'SOURCE_TRANSPORT_RECEIPT_INVALID')
        for name in ('x_request_id', 'x_cache'):
            require(meta[name] is None or isinstance(meta[name], str) and re.fullmatch(r'[A-Za-z0-9._-]{1,128}', meta[name]),
                    'SOURCE_TRANSPORT_RECEIPT_INVALID')
        if 'request_clock' in meta:
            _request_clock(meta['request_clock'], page['captured_at'])
            # Source inspection consumes only the finite saved projection.
            # Execution diagnostics are discarded, not an alternate clock or
            # an admission condition. Capture/checkpoint owners bind attempts.
        fingerprint = digest(encoded({'fields': table['fields'], 'items': table['items']}))
        require(not table['items'] or fingerprint not in seen, 'SOURCE_PAGINATION_STALLED')
        seen.add(fingerprint)
        require(index == len(pages)-1 or len(table['items']) == int(params['limit']), 'SOURCE_PAGE_AFTER_TERMINAL')
        offset += len(table['items']); count += len(table['items'])
        declared = max(declared, _declared_rows(table))
    expected_total = _consistent_total([page['table'] for page in pages])
    require(expected_total is None or offset <= expected_total, 'SOURCE_PAGE_COUNT_CONFLICT')
    complete = bool(pages) and capture['error'] is None and len(pages[-1]['table']['items']) < int(params['limit']) and declared <= offset
    if checked['api'] in AUCTION_OPTIONAL_FIELDS and pages:
        complete = complete and not _more_rows(pages[-1]['table']) and not any(
            page['table'].get('coverage_evidence', {}).get(name, {}).get('truncated', False)
            for page in pages for name in ('body', 'data'))
    require(capture['complete'] == complete, 'SOURCE_COMPLETENESS_MISMATCH')
    return {'query_key': checked['query_key'], 'stage': checked['stage'], 'purpose': checked['purpose'],
        'api': checked['api'], 'parameters': params, 'complete': complete, 'row_count': count,
        'page_count': len(pages), 'pages': pages, 'error': capture['error'],
        'coverage': 'REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS',
        'units': 'NOT_ESTABLISHED_BY_GENERIC_READER', 'economic_assessment': 'not-performed'}


def inspect_result(result, registry):
    """Recheck captured bytes and return an independent source-page projection."""
    checked = _inspect_result(result, registry)
    # Execution diagnostics stay in the capture. Do not turn arbitrary edited
    # diagnostic keys/prose into a source excerpt because capture JSON supplied them.
    return {**checked, 'pages': [
        {key: deepcopy(page[key]) for key in ('offset', 'captured_at', 'table', 'transport')}
        for page in checked['pages']]}


def source_inputs(result, registry):
    """Existing deliver/put-source accepts these; this function itself performs no write."""
    view = inspect_result(result, registry)
    require(view['complete'], 'SOURCE_COMPLETE_CAPTURE_REQUIRED')
    return _page_inputs(view)


def source_page_inputs(result, registry):
    """Export inspected received pages for an explicit execution checkpoint.

    This does not qualify an incomplete query for normal reading. The checkpoint
    writer retains completeness/control separately and validates them on restore.
    Complete-query source_inputs retains its original admission requirement.
    """
    return _page_inputs(inspect_result(result, registry))


def _page_inputs(view):
    output = []
    for page in view['pages']:
        source = SourceInput(source_id='relay-query-' + view['query_key'][:24] + '-' + str(page['offset']),
            kind='content', title='Relay '+view['api']+' '+view['parameters'].get('ts_code', 'selected-scope'),
            origin=ROOT+'/'+view['api'], text=encoded({'schema': CAPTURE, 'api': view['api'],
                'parameters': view['parameters'], 'page': page}).decode(), custody='excerpt',
            acquired_at=page['captured_at'], limitations=(
                'THIRD_PARTY_RELAY_REQUESTED_FIELD_PROJECTION_NOT_ORIGINAL_HTTP_OR_PDF',
                'UNITS_AND_BUSINESS_SEMANTICS_REQUIRE_SOURCE_SPECIFIC_CHECK; NOT_HISTORICAL_PIT_CERTIFICATION',
                'REQUESTED_PAGE_TERMINATION_NOT_HISTORY_COMPLETENESS; NO_ECONOMIC_APPROVAL',
                'CAPTURE_JSON_NOT_SOURCE_AUTHENTICATION; PUBLICATION_RIGHTS_MUST_BE_CONFIRMED_BY_WRITER'))
        output.append(source)
    return output
