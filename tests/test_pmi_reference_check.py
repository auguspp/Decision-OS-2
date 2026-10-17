"""Original Git/HTTP/PMI paths with synthetic values; no live source or research acceptance."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from decision_kernel.application.pmi_reference_check import (
    SCHEMA, REPOSITORY, checked_plan, check_reference,
)
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import pmi_check_commands, relay_source_runtime
from decision_kernel.observations.manufacturing_pmi import FIELDS
from test_github_store import GitAPI
from test_industry_pmi import row, SCOPE
from test_research_sources import http, busy, registry

STAMP = '2026-10-17T01:00:00+00:00'
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def prepared(tmp_path):
    api = GitAPI(tmp_path / 'bound.git')
    api.binding = replace(api.binding, repository=REPOSITORY, visibility='public')
    anchor = api.git.commit({'store.json': api.binding.marker()}, expected=api.git.head())
    api.binding = replace(api.binding, anchor=anchor)
    api.meta.update(full_name=REPOSITORY, private=False)
    store = VersionedStore(GitHubStore(api.binding, api))
    doc = {'unit': 'index-percent', 'comparison_unit': 'percentage-point',
        'representation': 'official-web-table-manual-transcription-not-original-html-or-relay',
        'scope': {**SCOPE, 'population': 'china-national-manufacturing'},
        'rows': [row('202601'), row('202602'), row('202603')],
        'unused': 'REFERENCE_PROSE_MUST_NOT_LEAVE_CHECK'}
    source = SourceInput('synthetic-pmi-reference', 'content', 'Synthetic reference, not actual NBS data',
        'https://www.stats.gov.cn/synthetic-not-a-real-page', encoded(doc).decode(), custody='excerpt',
        published_at='2026-04-01', acquired_at='2026-10-16', limitations=('Synthetic test only',))
    ref = store.put_source('synthetic-pmi', source)
    read_store = VersionedStore(GitHubStore(replace(api.binding, write_enabled=False), api))
    plan = {'schema': SCHEMA, **SCOPE, 'reference': asdict(ref)}
    return api, store, read_store, plan, source, doc


def reply(rows=None, fields=FIELDS):
    rows = [row('202601'), row('202602'), row('202603')] if rows is None else rows
    return {'code': 0, 'api_name': 'cn_pmi', 'count': len(rows),
            'data': {'fields': list(fields), 'items': [[r[f] for f in fields] for r in rows]}}


def check(prepared, *bodies, **kwargs):
    api, _, store, plan, _, _ = prepared
    client, opener = http(*(bodies or [reply()]))
    before, writes = api.git.head(), api.writes
    output = check_reference(plan, registry(), store, client=client, access_confirmed=True,
                             clock=lambda: STAMP, **kwargs)
    assert api.git.head() == before and api.writes == writes
    assert client.last_response_bytes is None
    return output, opener


def test_exact_same_reference_three_months_and_point_changes(prepared):
    out, opener = check(prepared)
    assert len(opener.calls) == 1 and out['status'] == 'matched'
    assert out['source']['rows'] == 3 and out['source']['complete']
    assert out['comparison']['cells'] == {'matched': 15, 'different': 0, 'unavailable': 0}
    assert out['comparison']['adjacent_changes'] == {'matched': 10, 'different': 0, 'unavailable': 0}
    assert out['reference_checked'] and out['comparison']['all_cells_match']
    text = encoded(out).decode()
    for forbidden in ('REFERENCE_PROSE', '49.2', '48.1', 'wire_sha256', 'x_request_id', 'projection_sha256', '"items"'):
        assert forbidden not in text
    assert not out['records_written'] and not out['supplier_rows_retained'] and not out['research_executed']


def test_different_value_is_not_rounded_into_agreement_or_exported(prepared):
    rows = [row('202601'), row('202602', pmi010000=49.200001), row('202603')]
    out, _ = check(prepared, reply(rows))
    assert out['status'] == 'different' and out['comparison']['cells']['different'] == 1
    assert out['comparison']['adjacent_changes']['different'] == 2
    assert out['comparison']['nonmatching_cells'] == [{'month': '202602', 'field': 'pmi010000', 'status': 'different'}]
    assert '49.200001' not in encoded(out).decode() and not out['comparison']['all_cells_match']


@pytest.mark.parametrize('rows,unavailable', [([], 15), ([row('202601'), row('202603')], 5),
    ([row('202601'), row('202602', pmi010000=None), row('202603')], 1),
    ([row('202601'), row('202602', pmi010000='49.2'), row('202603')], 1),
    ([row('202601'), row('202602', pmi010000=True), row('202603')], 1),
    ([row('202601'), row('202602', pmi010000=-1), row('202603')], 1)])
def test_empty_missing_and_invalid_never_zero_or_bridge(prepared, rows, unavailable):
    out, _ = check(prepared, reply(rows))
    assert out['status'] == 'incomplete' and out['source']['complete']
    assert out['comparison']['cells']['unavailable'] == unavailable
    assert out['comparison']['adjacent_changes']['unavailable'] >= 2
    assert not out['comparison']['all_cells_match']


@pytest.mark.parametrize('rows', [[row('202601'), row('202601')], [row('202604')], [row('bad')]])
def test_duplicate_or_wrong_month_rejects_whole_source(prepared, rows):
    out, _ = check(prepared, reply(rows))
    assert out['status'] == 'source-rejected' and out['comparison'] is None


def test_unrequested_private_prose_does_not_enter_any_receipt(prepared):
    rows = [row(m) | {'private_prose': 'DO_NOT_EXPORT_123'} for m in ('202601', '202602', '202603')]
    out, _ = check(prepared, reply(rows, (*FIELDS, 'private_prose')))
    assert out['status'] == 'matched' and 'DO_NOT_EXPORT_123' not in encoded(out).decode()


@pytest.mark.parametrize('status,error', [(401, 'unauthorized'), (403, 'forbidden'), (400, 'invalid_params'),
                                        (503, 'data_source_unavailable'), (503, 'unknown_vendor_code')])
def test_non_retryable_refusal_not_empty_and_no_prose(prepared, status, error):
    waits = []
    out, opener = check(prepared, busy(status, error), sleep=waits.append)
    assert out['status'] == 'source-incomplete' and out['comparison'] is None
    assert len(opener.calls) == 1 and not waits and not out['source']['complete']
    assert 'never persist this' not in encoded(out).decode()


def test_original_30_second_queue_retry_preserved(prepared):
    waits = []
    out, opener = check(prepared, busy(), reply(), sleep=waits.append)
    assert out['status'] == 'matched' and waits == [30] and len(opener.calls) == 2
    assert opener.calls[0].full_url == opener.calls[1].full_url
    assert len(out['source']['attempts']) == 2
    assert out['source']['attempts'][0]['business_error'] == 'upstream_pool_exhausted'
    assert out['source']['attempts'][0]['wait_before_next_attempt'] == 30


def test_long_retry_after_never_shortened_or_repeated(prepared):
    waits = []
    out, opener = check(prepared, busy(headers={'Retry-After': '600'}), sleep=waits.append)
    assert not waits and len(opener.calls) == 1 and out['status'] == 'source-incomplete'
    assert out['source']['pending_retry']['retry_after_seconds'] == 600
    assert out['source']['attempts'][0]['retry_after_seconds'] == 600


@pytest.mark.parametrize('change', ['unit', 'scope', 'origin', 'missing', 'duplicate', 'date', 'foreign', 'digest'])
def test_invalid_reference_rejected_before_relay(prepared, change):
    api, writer, reader, plan, source, doc = prepared
    if change == 'unit': doc['unit'] = 'CNY'
    if change == 'scope': doc['scope']['start_month'] = '202512'
    if change == 'missing': doc['rows'].pop(1)
    if change == 'duplicate': doc['rows'][1] = doc['rows'][0]
    source = replace(source, text=encoded(doc).decode())
    if change == 'origin': source = replace(source, origin='https://elsewhere.invalid')
    if change == 'date': source = replace(source, published_at='2026-03-01')
    if change not in ('foreign', 'digest'):
        plan['reference'] = asdict(writer.put_source('different-reference', source))
    else:
        plan['reference']['manifest']['repository' if change == 'foreign' else 'sha256'] = (
            'elsewhere/repository' if change == 'foreign' else 'f' * 64)
    client, opener = http(reply())
    if change == 'foreign':
        with pytest.raises(RecordError):
            check_reference(plan, registry(), reader, client=client, access_confirmed=True, clock=lambda: STAMP)
    else:
        out = check_reference(plan, registry(), reader, client=client, access_confirmed=True, clock=lambda: STAMP)
        assert out['status'] == 'reference-unavailable' and out['comparison'] is None
    assert not opener.calls


@pytest.mark.parametrize('value', [False, None, 1, 'true', []])
def test_library_access_is_explicit_before_store_or_source(prepared, value):
    with pytest.raises(RecordError, match='ACCESS_REQUIRED'):
        check_reference(prepared[3], registry(), object(), access_confirmed=value)


def test_revoked_access_on_exit_discards_comparison(prepared):
    api, _, reader, plan, _, _ = prepared
    client, opener = http(reply())
    original = client.request_registered
    def request(*args, **kwargs):
        value = original(*args, **kwargs)
        api.meta['private'] = True
        return value
    client.request_registered = request
    out = check_reference(plan, registry(), reader, access_confirmed=True, client=client, clock=lambda: STAMP)
    assert len(opener.calls) == 1 and out['status'] == 'reference-unavailable'
    assert out['comparison'] is None and not out['reference_checked']


def host_event(plan, **changes):
    inputs = {'operation': 'check', 'family': 'industry-pmi', 'source_plan': encoded(plan).decode(),
              'code_sha': 'a' * 40, 'access_confirmed': True, 'public_rights_confirmed': False, **changes}
    return {'inputs': inputs}


def host_env(tmp_path):
    return {'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REF': 'refs/heads/main',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40, 'RUNNER_TEMP': str(tmp_path)}


def test_same_host_no_secret_plan_then_counts_only_real_consumer(prepared, tmp_path, monkeypatch):
    event, env = host_event(prepared[3]), host_env(tmp_path)
    monkeypatch.chdir(ROOT)
    out = tmp_path / 'host'
    assert relay_source_runtime.execute('prepare', event, env, out) == 0
    def live(plan, reg, progress):
        client, _ = http(reply())
        return check_reference(plan, reg, prepared[2], access_confirmed=True, client=client,
                               clock=lambda: STAMP, progress=progress)
    monkeypatch.setattr(pmi_check_commands, '_live', live)
    assert relay_source_runtime.execute('check', event, env, out) == 0
    assert set(p.name for p in out.iterdir()) == {'request.json', 'plan.json', 'result.json'}
    result = json.loads((out / 'result.json').read_bytes())
    assert result['status'] == 'matched' and result['code_sha'] == 'a' * 40
    assert '49.2' not in (out / 'result.json').read_text()
    with pytest.raises(RecordError, match='ALREADY_STARTED'):
        relay_source_runtime.execute('check', event, env, out)


@pytest.mark.parametrize('change', [{'access_confirmed': False}, {'access_confirmed': 1},
    {'operation': 'capture', 'public_rights_confirmed': True}, {'public_rights_confirmed': True},
    {'codes': '002714.SZ'}, {'periods': '20260630'}, {'dates': '20261016'}, {'apis': 'cn_pmi'},
    {'page_size': '1'}, {'max_pages': '4'}, {'code_sha': 'b' * 40}, {'token': 'SECRET_SENTINEL'}])
def test_new_mode_invalid_before_artifact_or_credentials(prepared, tmp_path, change):
    with pytest.raises(ValueError):
        relay_source_runtime.execute('prepare', host_event(prepared[3], **change), host_env(tmp_path), tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('family', ['financial', 'participants', 'research'])
def test_check_cannot_enable_other_api_families(prepared, tmp_path, family):
    with pytest.raises(ValueError, match='UNUSED_CHECK_MODE'):
        relay_source_runtime.settings(host_event(prepared[3], family=family), host_env(tmp_path))


def test_prepared_plan_change_and_host_error_do_not_publish_prose(prepared, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    event, env, out = host_event(prepared[3]), host_env(tmp_path), tmp_path / 'host'
    relay_source_runtime.execute('prepare', event, env, out)
    (out / 'plan.json').write_text('{}')
    with pytest.raises(RecordError, match='PLAN_CHANGED'):
        relay_source_runtime.execute('check', event, env, out)
    out = tmp_path / 'second'
    relay_source_runtime.execute('prepare', event, env, out)
    def fail(*args): raise OSError('PRIVATE_SOURCE_ERROR_PROSE')
    monkeypatch.setattr(pmi_check_commands, '_live', fail)
    assert relay_source_runtime.execute('check', event, env, out) == 2
    assert 'PRIVATE_SOURCE_ERROR_PROSE' not in (out / 'result.json').read_text()


@pytest.mark.parametrize('change', [
    {'schema': 'other'}, {'reference': {}}, {'start_month': '202600'}, {'url': 'https://untrusted.invalid'},
])
def test_plan_shape_rejected_without_read_or_source(prepared, change):
    with pytest.raises(RecordError):
        check_reference({**prepared[3], **change}, registry(), object(), access_confirmed=True, clock=lambda: STAMP)


def test_current_month_is_not_assumed_published(prepared):
    plan = {**prepared[3], 'end_month': '202610'}
    with pytest.raises(RecordError, match='CLOSED_MONTHS'):
        check_reference(plan, registry(), object(), access_confirmed=True, clock=lambda: STAMP)


def test_plan_is_real_subprocess_offline_and_has_no_store_key(prepared, tmp_path):
    import os
    import subprocess
    event = host_event(prepared[3], operation='plan', access_confirmed=False)
    event_file = tmp_path / 'event.json'
    event_file.write_bytes(encoded(event))
    env = {k: v for k, v in os.environ.items() if k not in ('TUSHARE_PROXY_API_KEY', 'DECISION_OS_STORE_TOKEN')}
    env.update(host_env(tmp_path), PYTHONPATH=str(ROOT / 'src'), GITHUB_EVENT_PATH=str(event_file))
    process = subprocess.run(['python', '-m', 'decision_kernel.interfaces.relay_source_runtime',
        'prepare', '--out', str(tmp_path / 'subprocess')], cwd=ROOT, env=env,
        capture_output=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads((tmp_path / 'subprocess/plan.json').read_text())['requests'][0]['api'] == 'cn_pmi'
    assert json.loads((tmp_path / 'subprocess/result.json').read_text())['status'] == 'planned-not-captured'
