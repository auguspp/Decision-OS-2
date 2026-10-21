"""Declared coverage through the existing host and shipped offline page.

All observations, identity statements and permissions here are synthetic. These
tests exercise no provider, real coverage assertion or independent acceptance.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
import shutil
import subprocess

import pytest

from decision_kernel.application import industry_goods_coverage as coverage
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, digest, encoded
from decision_kernel.infrastructure.github_store import GitHubStore
from decision_kernel.interfaces import industry_commands, research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_industry_goods import EVALUATION, prepare, packet as goods_packet, store
from test_industry_room import plan, root_for
from test_industry_runtime import DUMMY
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs, invoke


def packet(store, batches):
    body = {'schema': coverage.SCHEMA, 'prepared_at': EVALUATION, 'as_of': EVALUATION,
            'coverage_note': 'Synthetic fixed collection; no actual product claim.',
            'batches': [{'batch_id': key, 'reference': asdict(ref)} for key, ref in batches],
            'aliases': []}
    source = SourceInput('synthetic-coverage-room', 'content', 'Synthetic coverage',
        'synthetic:coverage-room', encoded(body).decode(), acquired_at=EVALUATION, published_at=EVALUATION)
    return store.put_source('coverage-room-' + digest(encoded(asdict(source))), source)


@pytest.mark.parametrize('operation', ['industry-read', 'reading-room'])
@pytest.mark.parametrize('change', [{'evaluation_at': None}, {'evaluation_at': ''},
    {'evaluation_at': '2026-10-17T09:30:00'}, {'company_id': 'synthetic-company'}])
def test_coverage_clock_and_company_scope_fail_before_io(host, tmp_path, operation, change):
    selected = {'reference': deepcopy(DUMMY), 'view': 'industry-goods-coverage',
                'evaluation_at': EVALUATION, **change}
    if selected['evaluation_at'] is None:
        del selected['evaluation_at']
    value = ({**selected, 'public_sources_custody_confirmed': True} if operation == 'industry-read'
             else {'plan': {'as_of': EVALUATION, 'industry': selected},
                   'public_sources_custody_confirmed': True, 'public_research_custody_confirmed': True})
    with pytest.raises(RecordError):
        invoke(host, tmp_path / 'out', operation, value)
    assert not host.requests and host.writes == 0 and not (tmp_path / 'out').exists()


def test_native_coverage_keeps_stale_goods_and_exact_child_requests(host, tmp_path):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root = root_for(writer, 'industry-goods-coverage')
    head, writes = host.git.head(), host.writes
    value = {'reference': asdict(root), 'view': 'industry-goods-coverage',
             'evaluation_at': '2026-10-21T09:30:00+08:00', 'public_sources_custody_confirmed': True}
    report = invoke(host, tmp_path / 'coverage', 'industry-read', value)
    result = report['result']
    assert report['status'] == 'industry-reading-partial' and report['write'] == 'not-requested'
    assert result['scope']['read_batches'] == result['scope']['distinct_qualified_goods'] == 1
    assert result['scope']['fresh_goods'] == 0 and result['scope']['global_fresh_goods'] is None
    assert result['entries'][0]['status'] == 'qualified-not-fresh'
    assert result['quantity_milestone']['status'] == 'not-established'
    child, = result['batches']
    request = child['open_request']
    assert set(request['payload']) == {'reference', 'view', 'evaluation_at'}
    assert request['payload']['evaluation_at'] == value['evaluation_at']
    reopened = invoke(host, tmp_path / 'leaf', 'industry-read',
        {**request['payload'], 'public_sources_custody_confirmed': True})
    assert reopened['result']['scope'] == child['scope']
    assert reopened['result']['entries'][0]['selections'][0]['series']['observations']
    assert host.git.head() == head and host.writes == writes


def controls(page):
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
const rows=()=>walk(pane).filter(e=>e.tag==='tr'&&e.children.some(c=>c.tag==='td')).map(e=>e.children.filter(c=>c.tag==='td').map(c=>c.textContent));
'''
    suffix = r'''
const initial=rows();
act('industry-coverage-search','blocked','input');const search=rows();
act('industry-coverage-search','absent synthetic commodity','input');const empty=rows();
act('industry-coverage-search','','input');act('industry-coverage-state','fresh-qualified','change');const state=rows();
for(const b of walk(pane).filter(e=>e.textContent==='导出本批价格与证据读取请求'))b.run();
walk(pane).find(e=>e.textContent==='导出原范围读取请求').run();
walk(pane).find(e=>e.textContent==='下载完整商品覆盖结果 JSON').run();
process.stdout.write(JSON.stringify({initial,search,empty,state,exports,saved,unchanged:JSON.stringify(D)===original}));
'''
    run = subprocess.run([node, '-e', prefix + component + suffix], input=payload,
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_page_filters_preserve_full_counts_and_reopen_each_original_batch(store):
    first, body, _ = prepare(store)
    blocked = deepcopy(body['candidates'][0])
    blocked.update(candidate_id='blocked-memory', title='Synthetic blocked memory',
        subject_id=None, commodity_id=None, disposition='source-blocked', selections=[],
        reopen_when='Original restriction must be removed; no retry is authorized.')
    body['candidates'].append(blocked)
    second = goods_packet(store, body)
    root = packet(store, [('first', first), ('second', second)])
    model = ReadingRoom(store).build(plan(root, 'industry-goods-coverage'))
    view = model['industry']['view']
    observed = controls(render_page(model))
    assert len(observed['initial']) == 3 and len(observed['search']) == 1
    assert observed['empty'] == [] and len(observed['state']) == 2
    assert view['scope']['candidate_records'] == 3 and view['scope']['source_blocked_candidates'] == 1
    assert view['scope']['distinct_qualified_goods'] == view['scope']['fresh_goods'] == 1
    assert observed['unchanged'] and observed['saved'][0]['value'] == json.loads(encoded(view))
    expected = {encoded(b['reference']): b for b in view['batches']}
    leaf_exports = [e for e in observed['exports'] if e['payload']['view'] == 'industry-goods']
    assert len(leaf_exports) == 2
    for request in leaf_exports:
        assert request['operation'] == 'industry-read'
        assert set(request['payload']) == {'reference', 'view', 'evaluation_at'}
        leaf = industry_commands.perform(store, {**request['payload'], 'public_sources_custody_confirmed': True})
        assert leaf['scope'] == expected[encoded(request['payload']['reference'])]['scope']
    query, = [e for e in observed['exports'] if e['payload']['view'] == 'industry-goods-coverage']
    assert query['payload'] == model['industry']['plan']
    assert 'public_sources_custody_confirmed' not in query['payload']
    assert query['payload']['evaluation_at'] == EVALUATION


def test_native_private_coverage_budget_failure_exports_no_partial_view(host, tmp_path, monkeypatch):
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    root = root_for(writer, 'industry-goods-coverage')
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta['private'] = True
    monkeypatch.setattr(coverage, 'MAX_READ_BYTES', 1)
    out = tmp_path / 'denied'
    with pytest.raises(RecordError, match='INDUSTRY_COVERAGE_READING_BOUND'):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs('reading-room', {'plan': plan(root, 'industry-goods-coverage')}),
            code_sha=CODE, output=out)
    assert not (out / 'reading-room.html').exists()
    assert not (out / 'reading-result.json').exists()
