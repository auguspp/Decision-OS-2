"""Owns news capture receipts and native-artifact -> existing source custody seams."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError
import zipfile

import pytest

from decision_kernel.domain.records import RecordError, digest, encoded
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.infrastructure.news_capture import capture_newsnow, capture_relay_news, fetch_newsnow
from decision_kernel.interfaces.news_custody import source_from_archive, verify_native, render_news
from decision_kernel.observations.news_windows import newsnow_window, combine, verify_reading
from test_news_windows import NOW, newsnow, relay


def fixture_archive(reading=None):
    reading = reading or combine([newsnow_window(newsnow(), 'cls', observed_at=NOW)])
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('news-window.json', encoded(reading))
    raw = stream.getvalue()
    selection = {'source_run': '123', 'artifact_id': '456', 'artifact_sha256': digest(raw)}
    run = {'id': 123, 'repository': {'id': 1420041744}, 'head_repository': {'id': 1420041744},
           'path': '.github/workflows/news-intake.yml', 'head_branch': 'main',
           'event': 'workflow_dispatch', 'run_attempt': 1, 'status': 'completed',
           'conclusion': 'failure', 'head_sha': 'a' * 40}
    artifact = {'id': 456, 'name': 'dos2-news-123-1', 'digest': 'sha256:' + digest(raw),
                'expired': False, 'size_in_bytes': len(raw),
                'workflow_run': {'id': 123, 'head_sha': 'a' * 40,
                                 'repository_id': 1420041744, 'head_repository_id': 1420041744}}
    return raw, run, artifact, selection, reading


def test_selected_failure_recovery_retains_receipts_and_does_not_refetch_peers():
    calls = []
    clock = iter(('2026-10-16T00:01:00+00:00', '2026-10-16T00:01:03+00:00'))
    def one(source):
        calls.append(source)
        return newsnow()
    view = capture_newsnow(sources=('cls',), fetcher=one, clock=lambda: next(clock))
    assert calls == ['cls']
    assert view['observations'][0]['observed_at'] == '2026-10-16T00:01:03+00:00'
    proof = view['source_outcomes'][0]['capture']
    assert proof['requested_at'] < proof['received_at']
    assert proof['response_sha256'] == digest(newsnow()) and proof['source_body_retained'] is False
    assert verify_reading(view) == view


@pytest.mark.parametrize('selected', [(), ('cls', 'cls'), ('evil',), 'cls'])
def test_bad_source_selection_makes_no_calls(selected):
    with pytest.raises(RecordError):
        capture_newsnow(sources=selected, fetcher=lambda _: pytest.fail('must not call'))


def test_local_rejection_preserves_safe_reason_and_shape_not_unavailable_only():
    obj = json.loads(newsnow()); obj['items'] *= 31
    raw = encoded(obj)
    view = capture_newsnow(sources=('cls',), observed_at=NOW, fetcher=lambda _: raw)
    proof = view['source_outcomes'][0]['capture']
    assert proof['error_code'] == 'NEWSNOW_WINDOW_LIMIT'
    assert proof['safe_structure']['item_count'] == 31
    assert proof['response_sha256'] == digest(raw)
    assert view['observations'] == [] and view['source_outcomes'][0]['observations'] is None
    assert '合成新闻观察' not in json.dumps(view, ensure_ascii=False)


def test_failed_relay_source_has_receipt_and_not_a_green_empty_window():
    class Transport:
        calls = 0
        def _get_json(self, *a, **k):
            self.calls += 1
            raise RecordError('RELAY_HTTP_403')
    t = Transport()
    view = capture_relay_news('news', source='cls', start='2026-10-15 09:00:00',
                             end='2026-10-15 10:00:00', observed_at=NOW, transport=t)
    assert t.calls == 1 and view['source_outcomes'][0]['status'] == 'unavailable'
    receipt = view['source_outcomes'][0]['capture']
    assert receipt['error_code'] == 'RELAY_HTTP_403'
    assert receipt['request']['parameters']['src'] == 'cls'
    assert view['observations'] == []


def test_http_error_body_closed_never_printed():
    body = BytesIO(b'sensitive response')
    class Opener:
        def open(self, *a, **k):
            raise HTTPError('http://127.0.0.1', 503, 'sensitive', {}, body)
    with pytest.raises(RecordError, match='NEWSNOW_HTTP_503'):
        fetch_newsnow('cls', opener=Opener())
    assert body.closed


def test_existing_source_store_preserves_exact_partial_metadata_and_new_reader(tmp_path):
    raw, run, artifact, selection, view = fixture_archive()
    source = source_from_archive(raw, run, artifact, selection)
    assert source.kind == 'content' and source.acquired_at is None
    assert json.loads(source.text) == view
    assert 'NOT_ORIGINAL_NEWS_BODY' in source.limitations[0]
    backend = GitStore.init_synthetic(tmp_path / 'news.git')
    ref = VersionedStore(backend).put_source('news-window-123', source)
    # Another store reader, exact immutable source ref, not a current-pointer guess.
    fresh = VersionedStore(GitStore(tmp_path / 'news.git')).read_source(ref)
    assert fresh['data'] == source.text and fresh['original_verified'] is False
    assert fresh['availability'] == 'stored-representation'
    assert verify_reading(json.loads(fresh['data'])) == view
    assert VersionedStore(backend).put_source('news-window-123', source) == ref
    page = render_news(view, origin=source.origin)
    assert '# 已保存新闻观察' in page and '合成新闻观察' in page


@pytest.mark.parametrize('field,value', [('id',124),('path','.github/workflows/ci.yml'),
    ('event','pull_request'),('run_attempt',2),('status','in_progress'),('conclusion','cancelled'),
    ('head_repository',{'id':1}),('repository',{'id':1}),('head_branch','evil')])
def test_wrong_native_run_rejected_before_reading_archive(field, value):
    raw, run, artifact, selection, _ = fixture_archive()
    run[field] = value
    with pytest.raises(RecordError):
        source_from_archive(raw, run, artifact, selection)


@pytest.mark.parametrize('field,value', [('id',457),('name','other-123-1'),('expired',True),
    ('digest','sha256:'+'0'*64),('size_in_bytes',9000000),('workflow_run',{'id':1})])
def test_wrong_native_artifact_cannot_be_published(field, value):
    raw, run, artifact, selection, _ = fixture_archive()
    artifact[field] = value
    with pytest.raises(RecordError):
        source_from_archive(raw, run, artifact, selection)


@pytest.mark.parametrize('member', ['../news-window.json','raw.html','other/news-window.json'])
def test_wrong_zip_member_refused(member):
    _, run, artifact, selection, view = fixture_archive()
    b = BytesIO()
    with zipfile.ZipFile(b,'w') as z: z.writestr(member, encoded(view))
    raw = b.getvalue(); artifact['size_in_bytes'] = len(raw)
    selection['artifact_sha256'] = digest(raw); artifact['digest'] = 'sha256:' + digest(raw)
    with pytest.raises(RecordError): source_from_archive(raw,run,artifact,selection)


def test_native_digest_and_reading_digest_both_checked():
    raw, run, artifact, selection, view = fixture_archive()
    with pytest.raises(RecordError): source_from_archive(raw[:-1]+b'!',run,artifact,selection)
    view['observations'][0]['title']='another title'
    raw, run, artifact, selection, _ = fixture_archive(view)
    with pytest.raises(RecordError): source_from_archive(raw,run,artifact,selection)


def test_workflow_has_separate_explicit_custody_not_capture_write_permission():
    import yaml
    root=Path(__file__).resolve().parents[1]
    flow=yaml.load((root/'.github/workflows/news-custody.yml').read_text(),Loader=yaml.BaseLoader)
    assert set(flow['on']) == {'workflow_dispatch'} and flow['permissions'] == {}
    job=flow['jobs']['custody']
    assert job['permissions']=={'contents':'write','actions':'read'}
    assert 'public_metadata_custody_confirmed' in job['if']
    assert all('TUSHARE_PROXY_API_KEY' not in s.get('env',{}) for s in job['steps'])
    assert any('preflight' in s.get('run','') for s in job['steps'])
    assert any('publish' in s.get('run','') for s in job['steps'])
