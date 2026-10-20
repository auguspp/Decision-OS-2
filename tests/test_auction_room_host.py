"""Original GitHubAPI/real-Git host and the complete shipped offline page.

Only wire replies and input values are synthetic; no source access or second
runtime/budget owner is used. This is an engineering consumer, not market proof.
"""
from dataclasses import replace
from html import unescape
import io
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application import reading_auction
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT, GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_auction_inputs import REGISTRY, auction_payload
from test_auction_room import room_plan
from test_global_room import source_query
from test_global_room_host import public_grant
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs


LITERAL = '</script><img src=x onerror=alert(1)>'


def seed(host, *, private=False):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = private
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
    payload = auction_payload()
    # Two identical consumed name values retain duplicate semantics, while the
    # source text must remain literal through the actual whole page script.
    for index in (0, 8):
        payload['pool_result']['capture']['pages'][0]['table']['items'][index][2] = LITERAL
    ref = retain_market_reading(payload, REGISTRY, writer, fresh_reader=fresh,
                               public_rights_confirmed=True)['reference']
    writer.submit_revision('auction-host-research', RevisionInput('auction-host-research', 'r1', 'quick',
        'SYNTHETIC_AUCTION_PRIVATE_BODY\n', 'Synthetic consumer boundary', 'synthetic',
        unresolved=('Not real research or source acceptance.',)))
    host.requests.clear()
    return json.loads(encoded(ref))


def page_exports(page):
    """Run the full shipped script and actual download/navigation handlers."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('full offline page execution requires the existing optional Node runtime')
    runner = r'''
const assert=require('assert'),vm=require('vm');
const page=JSON.parse(require('fs').readFileSync(0,'utf8'));
const marker='<script id="data" type="application/json">',markup=page.split(marker)[0];
const model=page.split(marker)[1].split('</script>')[0];
const script=page.slice(page.lastIndexOf('<script>')+8).split('</script>')[0];
const created=[],ids=new Map(),blobs=new Map(),downloads=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.listeners={};created.push(this);}
 append(...items){this.children.push(...items);}prepend(...items){this.children.unshift(...items);}
 replaceChildren(...items){this.children=items;}get firstChild(){return this.children[0];}
 setAttribute(name,value){this[name]=value;}addEventListener(name,fn){this.listeners[name]=fn;}
 click(){if(this.disabled)return;if(this.tag==='a')downloads.push({name:this.download,blob:blobs.get(this.href)});else this.listeners.click?.call(this);}
 remove(){}
}
for(const match of markup.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g))ids.set(match[2],new Element(match[1]));
const data=new Element('script');data.textContent=model;ids.set('data',data);
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,
 createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelector:()=>null};
const context=vm.createContext({document,Blob,location:{hash:''},setTimeout:fn=>fn(),URL:{
 createObjectURL:blob=>{const key='blob:synthetic-'+blobs.size;blobs.set(key,blob);return key;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
const click=text=>{const found=created.filter(n=>n.tag==='button'&&n.textContent===text);assert.equal(found.length,1,text);assert(!found[0].disabled,text);found[0].click();};
click('下载完整竞价观察 JSON');click('导出竞价原根读取请求');
for(const n of created.filter(n=>n.tag==='button'&&/^导出竞价来源 /.test(n.textContent)))n.click();
click('导出竞价第一页请求');click('导出竞价下一页请求');
const field=created.find(n=>n.id==='auction-entity');assert(field);field.value='600001.SH';
click('导出竞价筛选请求');click('下载完整竞价观察 JSON');
assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,value:JSON.parse(await item.blob.text())});
 process.stdout.write(JSON.stringify({exports,text:created.filter(n=>n.tag!=='script').map(n=>n.textContent).join('\n'),full_script_bytes:Buffer.byteLength(script)}));})();
'''
    result = subprocess.run([node, '-e', runner], input=json.dumps(page), text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['full_script_bytes'] == len(page.rsplit('<script>', 1)[1].split('</script>', 1)[0].encode())
    return value


@pytest.mark.parametrize('private', [False, True], ids=['trusted-public', 'native-private'])
def test_native_auction_page_requests_reopen_exact_original_models(host, tmp_path, monkeypatch, private):
    grant = public_grant(host, tmp_path, monkeypatch) if not private else None
    ref = seed(host, private=private)
    before, writes, api = host.git.head(), host.writes, transport(host)
    output = tmp_path / 'auction-page'
    original_inputs = inputs('reading-room', {'plan': room_plan(ref, limit=1)})
    report = research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    assert report['status'] == 'completed' and report['write'] == 'not-requested'
    assert report['repository_visibility'] == ('private' if private else 'public')
    assert 0 < api._requests < MAX_REQUESTS == 512
    if grant:
        assert report['publication_authorization'] == grant[0]['authorization_ref']
        assert 'public_sources_custody_confirmed' not in json.loads(original_inputs['payload'])
    page = (output / 'reading-room.html').read_text()
    assert page == render_page(report['result']) and 'id="auction-content"' in page
    assert LITERAL not in page and '\\u003c/script\\u003e' in page
    assert 'innerHTML' not in page and 'localStorage' not in page and "connect-src 'none'" in unescape(page)
    executed = page_exports(page)
    assert LITERAL in executed['text'] and '完成收盘可用' in executed['text'] and '15:30' in executed['text']
    part = report['result']['auction']
    assert part['view']['view']['cohort_denominator'] == 8 and part['view']['view']['matched'] == 2
    full = [e['value'] for e in executed['exports'] if e['name'] == 'opening-auction-model.json']
    assert full == [part, part]
    source_requests = [e['value'] for e in executed['exports'] if e['value'].get('operation') == 'read-source']
    assert len(source_requests) == 5
    for number, request in enumerate(source_requests):
        assert set(request['payload']) == {'manifest', 'selection'}
        restored = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request['operation'], request['payload']), code_sha=CODE,
            output=output / ('source-' + str(number)))
        original = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(SourceRef.from_dict(request['payload']))
        assert restored['status'] == 'completed' and encoded(restored['result']) == encoded(original)
    requests = [e['value'] for e in executed['exports'] if e['value'].get('operation') == 'reading-room']
    assert len(requests) == 3
    for number, request in enumerate(requests):
        assert set(request['payload']) == {'plan'}
        assert request['payload']['plan']['auction']['reference'] == ref
        restored = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request['operation'], request['payload']), code_sha=CODE,
            output=output / ('navigation-' + str(number)))
        assert restored['result']['auction']['view'] == part['view']
    assert all(method == 'GET' for method, _, _ in host.requests)
    assert host.git.head() == before and host.writes == writes
    if grant:
        doc, path = grant; doc['enabled'] = False; path.write_bytes(encoded(doc))
        denied = tmp_path / 'revoked-public'
        with pytest.raises(RecordError, match='ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED'):
            research_runtime.run_command(host.binding, api=transport(host), inputs=original_inputs, code_sha=CODE, output=denied)
        assert not denied.exists()


@pytest.mark.parametrize('private,kind', [(True, '403'), (False, '403'), (True, 'budget')],
                         ids=['private-403', 'public-403', 'original-512-budget'])
def test_native_terminal_failure_keeps_original_object_and_zero_later_reads(host, tmp_path, monkeypatch, private, kind):
    if not private:
        public_grant(host, tmp_path, monkeypatch)
    ref = seed(host, private=private)
    reader = VersionedStore(GitHubStore(host.binding, host))
    _, query = source_query(reader, ref, api='limit_list_d')
    manifest = query['pages'][0]['reference']['manifest']
    blob = host.git._run('rev-parse', manifest['commit'] + ':' + manifest['path']).stdout.decode().strip()
    path = '/repos/' + host.binding.repository + '/git/blobs/' + blob
    api, state, trace = transport(host), {'inside': False, 'injected': False}, []
    opener, original_request, actual = api._opener, api.request, reading_auction.open_auction
    class OneResponse:
        def open(self, request, timeout):
            if state['inside'] and not state['injected'] and kind == '403' and request.full_url == API_ROOT + path:
                state['injected'] = True
                raise HTTPError(request.full_url, 403, 'Synthetic one-shot response', {}, io.BytesIO(b'{"message":"synthetic only"}'))
            return opener.open(request, timeout)
    def request(method, requested_path, payload=None):
        target = state['inside'] and not state['injected'] and requested_path == path
        if target and kind == 'budget':
            state['injected'] = True
            # Consume the real original shared operation allowance; neither
            # reset its counter nor lower the production limit for this test.
            while api._requests < MAX_REQUESTS:
                request('GET', '/repos/' + host.binding.repository)
        trace.append({'method': method, 'path': requested_path, 'inside_auction': state['inside']})
        try:
            return original_request(method, requested_path, payload)
        except RecordError as error:
            trace[-1]['error'] = str(error)
            if target:
                state['origin'], state['failure_index'] = error, len(trace) - 1
            raise
    def entered(*args, **kwargs):
        state['inside'] = True
        try:
            return actual(*args, **kwargs)
        except RecordError as error:
            state['escaped'] = error
            raise
        finally:
            state['inside'] = False
    api._opener = OneResponse()
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(reading_auction, 'open_auction', entered)
    before, writes, output = host.git.head(), host.writes, tmp_path / 'auction-terminal'
    original_inputs = inputs('reading-room', {'plan': room_plan(ref)})
    with pytest.raises(StoreUnavailable, match='REMOTE_NOT_AUTHORIZED' if kind == '403' else 'REMOTE_REQUEST_LIMIT') as caught:
        research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    report = json.loads((output / 'report.json').read_bytes())
    assert state['injected'] and caught.value is state['origin'] is state['escaped']
    assert trace[state['failure_index'] + 1:] == []
    assert api._requests == len(trace)
    assert api._requests == MAX_REQUESTS + 1 if kind == 'budget' else 0 < api._requests < MAX_REQUESTS
    assert report['status'] == 'failed' and 'result' not in report
    assert not (output / 'reading-room.html').exists()
    assert (output / 'inputs.json').exists() is (not private)
    if not private:
        assert json.loads((output / 'inputs.json').read_bytes()) == original_inputs
    assert 'SYNTHETIC_AUCTION_PRIVATE_BODY' not in (output / 'report.json').read_text()
    assert host.git.head() == before and host.writes == writes
    (tmp_path / 'auction-terminal-evidence.json').write_bytes(encoded({'private': private, 'kind': kind,
        'same_error_origin_reader_runtime': caught.value is state['origin'] is state['escaped'],
        'requests': len(trace), 'failure_index': state['failure_index'],
        'requests_after_error': len(trace[state['failure_index'] + 1:]),
        'inputs_released': (output / 'inputs.json').exists(),
        'html_released': (output / 'reading-room.html').exists(), 'trace': trace}))


def test_private_visibility_reversal_after_complete_auction_model_withholds_final_output(host, tmp_path, monkeypatch):
    ref = seed(host, private=True)
    original, complete = reading_auction.open_auction, []
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs)
        complete.append(result['view']['view']['cohort_denominator'])
        host.meta['private'] = False
        return result
    monkeypatch.setattr(reading_auction, 'open_auction', reverse)
    output = tmp_path / 'auction-reversed'
    with pytest.raises(RecordError, match='PRIVATE_UPGRADE_REVERSED'):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs('reading-room', {'plan': room_plan(ref)}), code_sha=CODE, output=output)
    assert complete == [8]
    assert not (output / 'inputs.json').exists() and not (output / 'reading-room.html').exists()
    assert 'SYNTHETIC_AUCTION_PRIVATE_BODY' not in (output / 'report.json').read_text()
