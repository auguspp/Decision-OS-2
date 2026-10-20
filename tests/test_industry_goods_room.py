"""Finite goods on the original native host and generated offline page.

All inputs and the one-shot transport fault are synthetic. No real source
request, public-rights assertion, or independent human acceptance is exercised.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application import industry_goods as goods, reading_industry
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT, MAX_REQUESTS, GitHubStore
from decision_kernel.interfaces import industry_commands, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_industry_goods import EVALUATION, prepare, packet, store
from test_industry_reading import BUYER, packet as original_packet
from test_industry_room import plan
from test_industry_source_buttons import source_buttons
from test_industry_runtime import DUMMY
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs, invoke


def references(view):
    refs = [e['reference'] for s in view['source_qualifications'] for e in s['evidence']]
    for entry in view['entries']:
        refs.extend(e['reference'] for e in entry['identity_evidence'])
        refs.extend(p['reference'] for s in entry['selections'] if s['series']
                    for p in s['series']['observations'])
        refs.extend(r['evidence']['reference'] for r in entry['relations'])
    return {encoded(ref) for ref in refs}


@pytest.mark.parametrize('mode,clock', [('industry-goods', None), ('industry-goods', ''),
    ('industry-goods', '2026-10-17T09:30:00'), ('industry', EVALUATION)])
@pytest.mark.parametrize('operation', ['industry-read', 'reading-room'])
def test_explicit_goods_clock_fails_preflight_before_io(host, tmp_path, mode, clock, operation):
    selected = {'reference': deepcopy(DUMMY), 'view': mode}
    if clock is not None:
        selected['evaluation_at'] = clock
    value = ({**selected, 'public_sources_custody_confirmed': True} if operation == 'industry-read'
             else {'plan': {'as_of': EVALUATION, 'industry': selected},
                   'public_sources_custody_confirmed': True, 'public_research_custody_confirmed': True})
    with pytest.raises(RecordError):
        invoke(host, tmp_path / 'out', operation, value)
    assert not host.requests and host.writes == 0 and not (tmp_path / 'out').exists()


def test_original_industry_host_retains_stale_qualification_as_partial(host, tmp_path):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root, _, _ = prepare(writer)
    before, writes = host.git.head(), host.writes
    value = {'reference': asdict(root), 'view': 'industry-goods',
             'evaluation_at': '2026-10-21T09:30:00+08:00', 'public_sources_custody_confirmed': True}
    report = invoke(host, tmp_path / 'read', 'industry-read', value)
    result = report['result']
    assert report['status'] == 'industry-reading-partial' and report['write'] == 'not-requested'
    assert result['scope']['distinct_qualified_goods'] == 1 and result['scope']['fresh_goods'] == 0
    assert result['entries'][0]['status'] == 'qualified-not-fresh'
    assert result['scope']['global_qualified_goods'] is None
    assert result['evaluation_at'] == value['evaluation_at']
    assert result['source_requests'] == result['writes'] == 0
    assert host.git.head() == before and host.writes == writes


def test_goods_source_buttons_reopen_bare_exact_refs_on_existing_native_host(host, tmp_path):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root, _, _ = prepare(writer)
    before, writes = host.git.head(), host.writes
    for private in (False, True):
        if private:
            host.binding = replace(host.binding, private_upgrade_allowed=True)
            host.meta['private'] = True
        value = {'plan': plan(root, 'industry-goods')}
        if not private:
            value.update(public_research_custody_confirmed=True, public_sources_custody_confirmed=True)
        destination = tmp_path / ('private' if private else 'public')
        api = transport(host)
        report = research_runtime.run_command(host.binding, api=api,
            inputs=inputs('reading-room', value), code_sha=CODE, output=destination)
        assert report['status'] == 'completed' and report['write'] == 'not-requested'
        view = report['result']['industry']['view']
        assert view['sets']['qualified_goods'] == view['sets']['fresh_goods'] == ['synthetic-copper']
        assert view['quantity_milestone']['status'] == 'not-established'
        page = (destination / 'reading-room.html').read_text()
        exports = source_buttons(page)
        assert len(exports) == 6
        assert {e['name'] for e in exports} == {'industry-goods-source.json'}
        assert {encoded(e['payload']) for e in exports} == references(view)
        selected, values = set(), set()
        for index, request in enumerate(exports):
            assert request['operation'] == 'read-source'
            assert set(request['payload']) == {'manifest', 'selection'}
            ref = SourceRef.from_dict(request['payload'])
            reopened = research_runtime.run_command(host.binding, api=transport(host),
                inputs=inputs('read-source', request['payload']), code_sha=CODE,
                output=destination / ('source-' + str(index)))
            expected = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(ref)
            assert reopened['write'] == 'not-requested'
            assert encoded(reopened['result']) == encoded(expected)
            selected.add(ref.selection.kind)
            if ref.selection.kind == 'rows':
                assert len(reopened['result']['data']) == 1
                values.add(reopened['result']['data'][0]['value'])
        assert selected == {'rows', 'lines'} and values == {'100', '110'}
        assert 0 < api._requests < MAX_REQUESTS == 512
    assert host.git.head() == before and host.writes == writes


def controls(page):
    """Run the shipped component; observe rendered DOM and literal exports."""
    node = shutil.which('node')
    if node is None:
        pytest.skip('offline interaction uses the existing optional Node runtime')
    payload = page.split('<script id="data" type="application/json">', 1)[1].split('</script>', 1)[0]
    script = page.rsplit('<script>', 1)[1].split('</script>', 1)[0]
    start = script.index('function renderIndustryUses(')
    component = script[start:script.index('renderIndustry();', start) + len('renderIndustry();')]
    prefix = r'''
const D=JSON.parse(require('fs').readFileSync(0,'utf8')),original=JSON.stringify(D),nodes=[],exports=[],saved=[];
class E{constructor(tag,text,cls){this.tag=tag;this.textContent=text===undefined?'':String(text);this.className=cls;this.value='';this.children=[];this.listeners={};nodes.push(this);}
append(...n){this.children.push(...n);}replaceChildren(...n){this.children=n;}get firstChild(){return this.children[0];}addEventListener(name,fn){this.listeners[name]=fn;}}
const pane=new E('div'),$=id=>id==='industry-content'?pane:null,el=(...args)=>new E(...args);
const details=(title,value)=>{const d=el('details',title);d.detail=value;return d;};
const button=(text,run)=>{const b=el('button',text);b.run=run;return b;};
const request=(name,value)=>exports.push({name,...value}),label=x=>x;
const save=(name,value,type)=>saved.push({name,value:JSON.parse(value),type});
const walk=e=>[e,...e.children.flatMap(walk)],find=id=>nodes.find(n=>n.id===id);
const act=(id,value,event)=>{const e=find(id);e.value=value;e.listeners[event]();};
const cards=()=>walk(pane).filter(e=>e.tag==='section').map(e=>({
 titles:walk(e).filter(n=>n.tag==='h3'||n.tag==='h4').map(n=>n.textContent),
 details:walk(e).filter(n=>n.detail!==undefined).map(n=>({title:n.textContent,value:n.detail}))}));
'''
    suffix = r'''
const initial=cards();
act('industry-goods-search','blocked','input');const search=cards();
act('industry-goods-search','no such synthetic item','input');const empty=cards();
act('industry-goods-search','','input');act('industry-goods-status','fresh-qualified','change');const state=cards();
act('industry-goods-company','synthetic-buyer','change');const company=cards();
walk(pane).find(e=>e.textContent==='导出当前筛选的原商品读取请求').run();
walk(pane).find(e=>e.textContent==='下载本次商品资格结果 JSON').run();
process.stdout.write(JSON.stringify({initial,search,empty,state,company,exports,saved,unchanged:JSON.stringify(D)===original}));
'''
    run = subprocess.run([node, '-e', prefix + component + suffix], input=payload,
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 0 and run.stderr == ''
    return json.loads(run.stdout)


def test_page_search_status_and_company_filter_keep_scope_and_exact_input_roles(store):
    _, body, raw = prepare(store)
    other = deepcopy(raw)
    other['relations'] = []
    other['series'][0]['series_id'] = 'other-packet-no-issuer-role'
    ref = original_packet(store, other)
    body['inputs'].append({'input_id': 'other', 'view': 'industry', 'reference': asdict(ref)})
    body['candidates'][0]['selections'].append({'input_id': 'other', 'series_id': other['series'][0]['series_id']})
    blocked = deepcopy(body['candidates'][0])
    blocked.update(candidate_id='blocked-memory', title='Synthetic blocked memory',
                   subject_id=None, commodity_id=None, disposition='source-blocked', selections=[],
                   reopen_when='Original restriction actually removed; no retries are authorized.')
    body['candidates'].append(blocked)
    root = packet(store, body)
    model = ReadingRoom(store).build(plan(root, 'industry-goods'))
    view = model['industry']['view']
    observed = controls(render_page(model))
    assert len(observed['initial']) == 2
    assert len(observed['search']) == 1 and 'blocked memory' in observed['search'][0]['titles'][0]
    assert observed['empty'] == [] and len(observed['state']) == len(observed['company']) == 1
    card = observed['company'][0]
    assert 'other-packet-no-issuer-role' not in card['titles']
    roles = [d['value'] for d in card['details'] if d['title'].startswith('原公司角色')]
    assert len(roles) == 1 and roles[0]['company']['entity_id'] == BUYER.entity_id
    assert roles[0]['input_id'] == 'original'
    assert observed['unchanged'] and observed['saved'][0]['value'] == json.loads(encoded(view))
    assert view['scope']['candidate_records'] == 2 and view['scope']['source_blocked_candidates'] == 1
    assert view['outcomes'][1]['reopen_when'] == blocked['reopen_when']
    query, = observed['exports']
    assert query['operation'] == 'industry-read'
    assert query['payload'] == {**model['industry']['plan'], 'subject_id': None, 'company_id': BUYER.entity_id}
    assert query['payload']['evaluation_at'] == EVALUATION
    assert 'public_sources_custody_confirmed' not in query['payload']
    reopened = industry_commands.perform(store, {**query['payload'], 'public_sources_custody_confirmed': True})
    assert reopened['scope'] == view['scope'] and reopened['sets'] == view['sets']
    assert all(s['input_id'] == 'original' for e in reopened['entries'] for s in e['selections'])


@pytest.mark.parametrize('failure,reason', [
    ('source-403', 'REMOTE_NOT_AUTHORIZED'),
    ('reading-scope-closed', 'READING_SCOPE_CLOSED'),
    ('source-scan-closed', 'SOURCE_SCAN_SCOPE_CLOSED_OR_CHANGED'),
    ('scope-exit', 'PUBLIC_BINDING_MISMATCH'),
    ('full-result-bound', 'INDUSTRY_GOODS_RESULT_BOUND'),
])
def test_goods_terminal_failure_stops_native_room_without_private_export(
        host, tmp_path, monkeypatch, failure, reason):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root, _, raw = prepare(writer)
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = True
    price = SourceRef.from_dict(raw['series'][0]['observations'][0])
    blob = host.git._run('rev-parse', price.manifest.commit + ':' + price.manifest.path).stdout.decode().strip()
    fault_path = '/repos/' + host.binding.repository + '/git/blobs/' + blob
    api = transport(host)
    opener, original_request = api._opener, api.request
    original_scope, original_read = goods.industry_source_scope, reading_industry.read_industry_goods
    trace, state = [], {'injected': False, 'origin': None, 'reader': None, 'count': None, 'body': False}

    class OneResponse:
        def open(self, request, timeout):
            if (failure == 'source-403' and not state['injected'] and request.method == 'GET'
                    and request.full_url.removeprefix(API_ROOT) == fault_path):
                state['injected'] = True
                raise HTTPError(request.full_url, 403, 'Synthetic one-shot response', {},
                                io.BytesIO(b'{"message":"synthetic fixture only"}'))
            return opener.open(request, timeout)

    def request(method, path, payload=None):
        trace.append((method, path))
        try:
            return original_request(method, path, payload)
        except RecordError as error:
            if state['injected'] and state['origin'] is None:
                state.update(origin=error, count=len(trace))
            raise

    @contextmanager
    def scope(store):
        with original_scope(store) as reader:
            class ClosedOnce:
                def read_source(self, ref):
                    if ref == price and not state['injected']:
                        state.update(injected=True, count=len(trace), origin=RecordError(reason))
                        raise state['origin']
                    return reader.read_source(ref)
            yield ClosedOnce() if failure in ('reading-scope-closed', 'source-scan-closed') else reader
        if failure == 'scope-exit':
            state.update(injected=True, body=True, count=len(trace), origin=RecordError(reason))
            raise state['origin']

    def read(*args, **kwargs):
        try:
            return original_read(*args, **kwargs)
        except RecordError as error:
            state['reader'] = error
            if failure == 'full-result-bound':
                state.update(injected=True, body=True, count=len(trace), origin=error)
            raise

    api._opener = OneResponse()
    monkeypatch.setattr(api, 'request', request)
    monkeypatch.setattr(goods, 'industry_source_scope', scope)
    monkeypatch.setattr(reading_industry, 'read_industry_goods', read)
    if failure == 'full-result-bound':
        monkeypatch.setattr(goods, 'MAX_RESULT_BYTES', 200)
    value = plan(root, 'industry-goods', subject_id='no-returned-entry')
    before, writes = host.git.head(), host.writes
    destination = tmp_path / failure
    with pytest.raises(RecordError, match=reason) as raised:
        research_runtime.run_command(host.binding, api=api,
            inputs=inputs('reading-room', {'plan': value}), code_sha=CODE, output=destination)
    report = json.loads((destination / 'report.json').read_bytes())
    assert state['injected'] and state['count'] > 0
    assert raised.value is state['origin'] is state['reader']
    assert type(raised.value) is (StoreUnavailable if failure in ('source-403', 'full-result-bound') else RecordError)
    assert trace[state['count']:] == [] and api._requests == len(trace) < MAX_REQUESTS == 512
    assert report['status'] == 'failed' and report['error'] == reason and 'result' not in report
    assert not (destination / 'inputs.json').exists() and not (destination / 'reading-room.html').exists()
    assert host.git.head() == before and host.writes == writes
