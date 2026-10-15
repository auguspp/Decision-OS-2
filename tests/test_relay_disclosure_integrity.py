"""Regression protection for the schedule source, its transport and save receipt."""
from dataclasses import asdict
from http.client import IncompleteRead
import io
import json
from urllib.error import HTTPError

import pytest

from decision_kernel.application.relay_disclosure_date import save_schedule
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, UnknownWrite
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.infrastructure.relay_disclosure_date import RelayDisclosureTransport
from decision_kernel.observations.relay_disclosure_date import normalize_disclosure_dates
from test_relay_disclosure_date import CAP, TICK, PERIOD, response, norm
from test_relay_disclosure_date_transport import Reply, Opener, url, response as wire


def test_revision_record_is_not_assumed_to_be_one_date():
    raw=response(items=[[TICK,'20260924',PERIOD,'20261029',None,'20261020 -> 20261029']])
    value=norm(raw).rows[0].modify_date
    assert value=={'presence':'value','value':'20261020 -> 20261029',
                   'interpretation':'unparsed-revision-record'}


def test_future_actual_date_is_retained_but_not_qualified_as_occurred():
    snap=norm(response(items=[[TICK,'20261020',PERIOD,'20261029','20261102',None]]))
    assert snap.rows[0].actual_date['value']=='2026-11-02'
    assert set(snap.rows[0].temporal_issues)=={'ANNOUNCEMENT_AFTER_CAPTURE','ACTUAL_DISCLOSURE_AFTER_CAPTURE'}


def test_report_period_must_be_a_quarter_end():
    with pytest.raises(RecordError,match='RELAY_REPORT_PERIOD_INVALID'):
        normalize_disclosure_dates(ts_code=TICK,end_date='20260929',raw=response(),
                                   captured_at=CAP,source_mode='offline-unverified')


@pytest.mark.parametrize('value',[None,True,123,[]])
def test_bad_transport_digest_is_a_bounded_error(value):
    with pytest.raises(RecordError,match='RELAY_RESPONSE_RECEIPT_INVALID'):
        normalize_disclosure_dates(ts_code=TICK,end_date=PERIOD,raw=response(),
           captured_at=CAP,source_mode='live-relay',response_transport={
            'http_status':200,'x_request_id':None,'x_cache':None,'wire_sha256':value})


def test_offline_response_cannot_assert_an_http_receipt():
    with pytest.raises(RecordError,match='RELAY_RESPONSE_RECEIPT_INVALID'):
        normalize_disclosure_dates(ts_code=TICK,end_date=PERIOD,raw=response(),
           captured_at=CAP,source_mode='offline-unverified',response_transport={
            'http_status':200,'x_request_id':None,'x_cache':None,'wire_sha256':'a'*64})


@pytest.mark.parametrize('length',['999999999','n/a','1'])
def test_content_length_enforced_before_qualified_response(length):
    own=Opener(Reply(wire(),url(),headers={'Content-Length':length}))
    with pytest.raises(RecordError,match='RELAY_RESPONSE_BYTES_INVALID'):
        RelayDisclosureTransport(opener=own,credential='synthetic-key')(TICK,PERIOD,100)
    assert own.reply.closed


def test_interrupted_stream_is_closed_and_not_retried():
    class Broken(Reply):
        def read(self,*args):raise IncompleteRead(b'{}',100)
    own=Opener(Broken(b'',url()))
    with pytest.raises(RecordError,match='RELAY_HTTP_UNAVAILABLE'):
        RelayDisclosureTransport(opener=own,credential='synthetic-key')(TICK,PERIOD,100)
    assert own.reply.closed and len(own.requests)==1


def test_denial_response_handle_is_closed():
    stream=io.BytesIO(b'private')
    own=Opener(HTTPError(url(),403,'private',{},stream))
    with pytest.raises(RecordError):
        RelayDisclosureTransport(opener=own,credential='synthetic-key')(TICK,PERIOD,100)
    assert stream.closed


def test_escaped_credential_and_reflecting_header_never_escape():
    key='synthetic-key'
    raw=b'{"msg":"synthetic-\\u006bey"}'
    own=Opener(Reply(raw,url()))
    t=RelayDisclosureTransport(opener=own,credential=key)
    with pytest.raises(RecordError,match='RELAY_SECRET_REFLECTED'):t(TICK,PERIOD,100)
    assert t.last_response_meta is None
    own=Opener(Reply(wire(),url(),headers={'X-Request-ID':key,'X-Cache':'HIT'}))
    t=RelayDisclosureTransport(opener=own,credential=key)
    t(TICK,PERIOD,100)
    assert key not in json.dumps(t.last_response_meta)


def test_known_source_reference_survives_additional_readback_failure(tmp_path,monkeypatch):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/'s.git'))
    real_put=store.put_source
    known=[]
    def put(*args):
        ref=real_put(*args);known.append(ref)
        monkeypatch.setattr(store,'read_source',lambda *_:(_ for _ in ()).throw(RecordError('RECEIPT_READ_UNAVAILABLE')))
        return ref
    monkeypatch.setattr(store,'put_source',put)
    result=save_schedule(store,norm(),'original-intent',public_rights_confirmed=True)
    assert not result['complete'] and result['status']=='saved-reference-unverified'
    assert result['reference']==asdict(known[0]) and result['submission_id']=='original-intent'
    assert len(known)==1


def test_unknown_submission_returns_original_intent_without_retry(tmp_path,monkeypatch):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/'s.git'))
    calls=[]
    def put(*args):calls.append(args);raise UnknownWrite('CONTROLLED_UNKNOWN')
    monkeypatch.setattr(store,'put_source',put)
    result=save_schedule(store,norm(),'original-intent',public_rights_confirmed=True)
    assert result['status']=='unknown-write' and not result['complete']
    assert result['reference'] is None and result['submission_id']=='original-intent'
    assert len(calls)==1


def test_cli_incomplete_save_is_not_success(tmp_path, monkeypatch, capsys):
    from decision_kernel.interfaces import relay_disclosure_date_cli as cli
    from test_relay_disclosure_date_cli import previous_capture, args
    path=previous_capture(tmp_path)
    target=tmp_path/'s.git';GitStore.init_synthetic(target)
    monkeypatch.setattr(cli,'save_schedule',lambda *a,**k:{
        'complete':False,'status':'unknown-write','submission_id':'original-intent','reference':None})
    assert cli.main(args(path)+['--save','--batch-id','original-intent',
        '--public-rights-confirmed','--local-synthetic-store',str(target)])==2
    assert json.loads(capsys.readouterr().out)['storage']['status']=='unknown-write'


def test_cli_invalid_writer_does_not_consume_live_request(tmp_path,monkeypatch,capsys):
    from decision_kernel.interfaces import relay_disclosure_date_cli as cli
    monkeypatch.setattr(cli,'fetch_disclosure_dates',lambda **_:(_ for _ in ()).throw(AssertionError('network')))
    assert cli.main(['--ts-code',TICK,'--end-date',PERIOD,'--live','--save',
        '--batch-id','bad intent','--public-rights-confirmed',
        '--local-synthetic-store',str(tmp_path/'not-existing.git')])==2
    assert 'INVALID_ID' in capsys.readouterr().err
