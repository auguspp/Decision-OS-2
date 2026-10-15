"""Capability metadata only; synthetic transport and real local file roundtrip."""
import io
import json
from pathlib import Path
from hashlib import sha256
from urllib.error import HTTPError

import pytest
from decision_kernel.domain.records import RecordError
from decision_kernel.infrastructure.relay_http import CAPABILITIES, RelayHTTP
from decision_kernel.interfaces.relay_capabilities_cli import capture, public_metadata, main

SHA = 'a' * 40
KEY = 'synthetic-secret-123'

class Reply(io.BytesIO):
    def __init__(self, raw, url=CAPABILITIES):
        super().__init__(raw)
        self.status = 200
        self.url = url
        self.headers = {'Content-Type': 'application/json', 'Content-Length': str(len(raw))}
    def geturl(self):
        return self.url

class Opener:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
    def open(self, request, timeout):
        self.calls.append(request)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

def client(raw=b'{"apis":{"income":{"enabled":true,"methods":["GET"]}}}', url=CAPABILITIES):
    own = Opener(Reply(raw, url))
    return RelayHTTP(opener=own, credential=KEY), own


def test_exact_raw_roundtrip_and_receipt(tmp_path):
    raw = b'{ "apis": {"income": {"enabled":true}}, "version":"demo" }\n'
    c, o = client(raw)
    out = tmp_path/'saved'
    r = capture(out, code_sha=SHA, public_rights_confirmed=True, client=c, env={})
    assert r['status']=='captured' and r['business_requests']==0
    assert (out/'capabilities.raw.json').read_bytes()==raw
    assert json.loads((out/'receipt.json').read_bytes())==r
    assert r['sha256']==sha256(raw).hexdigest()
    assert len(o.calls)==1 and o.calls[0].get_method()=='GET'
    assert o.calls[0].full_url==CAPABILITIES
    assert o.calls[0].get_header('X-api-key')==KEY
    assert KEY not in json.dumps(r)


def test_single_metadata_entry_never_executes_named_business_api():
    c, o = client(b'{"name":"p_delete","methods":["DELETE"]}', CAPABILITIES+'/p_delete')
    assert c.capabilities('p_delete')['name']=='p_delete'
    assert o.calls[0].get_method()=='GET' and '/pro/' not in o.calls[0].full_url

@pytest.mark.parametrize('name',['../daily','income?a=b','https://evil.example','INCOME','',5])
def test_invalid_name_never_requests(name):
    c, o = client()
    with pytest.raises(RecordError): c.capabilities(name)
    assert not o.calls


def test_does_not_admit_business_apis():
    c,o=client()
    with pytest.raises(RecordError,match='RELAY_API_NOT_ALLOWED'):
        c.request('p_delete',{'portfolio_id':'demo'})
    assert not o.calls

@pytest.mark.parametrize('raw',[
    b'{"x":1,"x":2}',b'{"x":NaN}',
    b'{"x":"synthetic-secret-123"}',b'{"x":"synthetic-secret-12\\u0033"}',
])
def test_rejected_response_not_retained(tmp_path,raw):
    c,o=client(raw)
    result=capture(tmp_path/'out',code_sha=SHA,public_rights_confirmed=True,client=c,env={})
    assert result['status']=='failed' and c.last_response_bytes is None
    assert not (tmp_path/'out'/'capabilities.raw.json').exists()
    assert KEY not in json.dumps(result)

@pytest.mark.parametrize('status',[401,403,429,503])
def test_http_error_recorded_no_retry(tmp_path,status):
    o=Opener(HTTPError(CAPABILITIES,status,'private',{},io.BytesIO(b'private')))
    c=RelayHTTP(opener=o,credential=KEY)
    r=capture(tmp_path/'out',code_sha=SHA,public_rights_confirmed=True,client=c,env={})
    assert r['error']=='RELAY_HTTP_'+str(status) and len(o.calls)==1
    assert not (tmp_path/'out'/'capabilities.raw.json').exists()

@pytest.mark.parametrize('value',[False,1,'true',None])
def test_rights_require_explicit_boolean(tmp_path,value):
    c,o=client()
    with pytest.raises(RecordError):
        capture(tmp_path/'out',code_sha=SHA,public_rights_confirmed=value,client=c,env={})
    assert not o.calls and not (tmp_path/'out').exists()


def test_manual_main_host_and_existing_output_before_network(tmp_path):
    c,o=client()
    with pytest.raises(RecordError):
        capture(tmp_path/'new',code_sha=SHA,public_rights_confirmed=True,client=c,env={'GITHUB_ACTIONS':'true'})
    with pytest.raises(RecordError):
        capture(tmp_path,code_sha=SHA,public_rights_confirmed=True,client=c,env={})
    assert not o.calls


def test_sensitive_metadata_not_uploaded(tmp_path):
    c,o=client(b'{"config":{"password":"not-a-customer-key-but-secret"}}')
    r=capture(tmp_path/'out',code_sha=SHA,public_rights_confirmed=True,client=c,env={})
    assert r['error']=='CAPABILITIES_SENSITIVE_FIELD'
    assert not (tmp_path/'out'/'capabilities.raw.json').exists()
    public_metadata({'required':['api_key'],'api_key':'<YOUR_CLIENT_KEY>'})


def test_workflow_contract():
    text=(Path(__file__).resolve().parents[1]/'.github/workflows/relay-capabilities.yml').read_text()
    assert 'contents: read' in text and 'contents: write' not in text
    assert 'workflow_dispatch:' in text and '\n  schedule:' not in text
    assert text.count('secrets.TUSHARE_PROXY_API_KEY')==1
    assert 'refs/heads/main' in text and 'persist-credentials: false' in text
    assert '--public-rights-confirmed' in text
