"""Compatibility does not turn unknown envelopes into public body archives."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef
from decision_kernel.infrastructure.git_store import GitStore
from decision_kernel.interfaces.relay_participant_cli import main
from test_relay_participants import query, body, sources, fetch, save, capture, TIME

class WireClient:
    last_error_meta = None
    requests = 0
    def __init__(self, value): self.value=value; self.last_response_meta=None
    def validate_credential(self): pass
    def request(self, api, params):
        self.requests += 1
        self.last_response_meta={"http_status":200,"x_cache":"HIT","x_request_id":"synthetic",
                                 "wire_sha256":sha256(json.dumps(self.value).encode()).hexdigest()}
        return self.value

def projected(q=None,payload=None):
    q=q or query()
    return fetch(q,transport=WireClient(payload or body(q)),clock=lambda:TIME,response_filter=sources.metadata_envelope)

@pytest.mark.parametrize('api',tuple(sources.SPECS))
def test_extra_root_metadata_is_omitted_not_wrongly_an_interface_failure(api):
    q=query(api);p=body(q);p.update(extra_metadata={'secret':'other-unreviewed-secret'},trace_notes='private-notes')
    c=projected(q,p);v=sources.inspect_capture(c)
    assert v['request_complete'] and v['row_count']==1
    assert 'extra_metadata' not in json.dumps(c) and 'private-notes' not in json.dumps(c)
    assert 'other-unreviewed-secret' not in json.dumps(c)
    note=v['pages'][0]['response_projection']
    assert note=={'schema':'relay-metadata-envelope-projection-v1','omitted_root_fields':2,'original_body_retained':False}
    assert v['pages'][0]['transport']['wire_sha256']==sha256(json.dumps(p).encode()).hexdigest()
    assert c['pages'][0]['body']['data']==p['data']

@pytest.mark.parametrize('changes', [{'code':False},{'code':1},{'ok':False},{'success':False},
    {'error':'data_source_unavailable'},{'api_name':'daily'},{'count':-1}])
def test_projection_never_drops_denials_wrong_api_or_failure_flags(changes):
    p=body();p.update(changes);p['extra']='ignore'
    c=projected(payload=p)
    assert c['error'] and not c['pages']
    assert c['failure']['classification']=='response-contract'
    assert c['failure']['http_status']==200

@pytest.mark.parametrize('code',['rate_limited','ip_rate_limited','unauthorized','forbidden'])
def test_shared_business_denial_remains_prior_to_projection(code):
    p=body();p['error']=code;p['extra']='ignore'
    c=projected(payload=p)
    assert c['error'] in {'RELAY_RATE_LIMIT','RELAY_AUTH_OR_ENTITLEMENT'}
    assert not c['pages']

@pytest.mark.parametrize('api,field',[('report_rc','author_name'),('research_report','abstr'),('stk_surv','fund_visitors')])
def test_projection_cannot_hide_unrequested_data_columns(api,field):
    q=query(api);p=body(q);p['data']['fields'].append(field);p['data']['items'][0].append('private-content')
    c=projected(q,p)
    assert c['error']=='PARTICIPANT_FIELDS_INVALID' and not c['pages']
    fail=c['failure'];assert fail['field_count']==len(p['data']['fields']) and fail['row_count']==1
    assert 'private-content' not in json.dumps(c)
    assert fail['transport']['wire_sha256']

@pytest.mark.parametrize('field,value',[('ts_code','600519.SH'),('report_date','20261014')])
def test_projection_keeps_real_scope_checks(field,value):
    p=body();p['data']['items'][0][p['data']['fields'].index(field)]=value
    c=projected(payload=p)
    assert c['error']=='PARTICIPANT_SCOPE_MISMATCH' and not c['pages']


def test_freeform_prose_and_invalid_metadata_omitted_without_coercion():
    p=body();p.update(msg='upstream prose private',request_id={'nested':'private'},provider=None,source='sample')
    safe,note=sources.metadata_envelope(p)
    assert note['omitted_root_fields']==2 and 'msg' not in safe and 'request_id' not in safe
    assert safe['provider'] is None and safe['source']=='sample'


def test_projection_survives_existing_store_and_independent_read(tmp_path):
    store=VersionedStore(GitStore.init_synthetic(tmp_path/'s.git'))
    p=body();p['ignored']='not-retained'
    c=projected(payload=p)
    result=save(c,lambda:store,public_rights_confirmed=True)
    assert result['complete']
    ref=SourceRef.from_dict(result['saved'][0]['reference'])
    v=VersionedStore(GitStore(tmp_path/'s.git')).read_source(ref)
    excerpt=json.loads(v['data'])
    assert excerpt['page']['response_projection']['omitted_root_fields']==1
    assert 'not-retained' not in v['data'] and not v['original_verified']
    assert save(c,lambda:store,public_rights_confirmed=True)==result


@pytest.mark.parametrize('change',[{'omitted_root_fields':True},{'omitted_root_fields':-1},
    {'original_body_retained':True},{'schema':'fake'},{'unknown':'value'}])
def test_invalid_projection_declaration_refused_on_read(change):
    c=projected();c['pages'][0]['response_projection'].update(change)
    with pytest.raises(RecordError,match='PROJECTION'):sources.inspect_capture(c)


def test_legacy_capture_without_projection_stays_readable():
    assert sources.inspect_capture(capture())['request_complete']


def test_real_cli_uses_projection_but_not_unreviewed_values(tmp_path,monkeypatch,capsys):
    from decision_kernel.interfaces import relay_financial_cli as cli
    plan=tmp_path/'plan';plan.write_text(json.dumps(asdict(query()))+'\n')
    p=body();p['extra']={'credential':'not-public'}
    monkeypatch.setattr(cli,'RelayHTTP',lambda:WireClient(p))
    out=tmp_path/'capture'
    assert main(['capture','--live','--plan',str(plan),'--output',str(out)])==0
    c=json.loads(out.read_text())['capture']
    assert 'not-public' not in out.read_text()
    assert sources.inspect_capture(c)['request_complete']


def test_financial_default_capture_does_not_acquire_projection():
    from test_relay_financial_batch import query as fq,body as fb,Client
    from decision_kernel.infrastructure.relay_financial import capture_query
    p=fb();p['extra']='historically-retained'
    c=capture_query(fq(),transport=Client([p]),clock=lambda:TIME)
    assert c['error'] is None and 'response_projection' not in c['pages'][0]
    assert c['pages'][0]['body']['extra']=='historically-retained'
