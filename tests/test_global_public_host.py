"""Original GitHubAPI/ModelWire and complete offline page; synthetic records only."""
from dataclasses import replace
import io
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application import reading_global
from decision_kernel.application.global_public_custody import read_public_root
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT, GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_global_public_inputs import replace_body
from test_global_public_room import COUNTS, public_inputs, retain_public
from test_global_room import room_plan
from test_global_room_host import public_grant
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs

MARKER = '</script><img src=x onerror=alert(1)>'


def seed_public(host, *, private=False):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = private
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
    values = public_inputs()
    raw = json.loads(values["commodities"]["records"][0]["body"])
    raw["chart"]["result"][0]["meta"]["synthetic_note"] = MARKER
    replace_body(values["commodities"], 0, json.dumps(raw))
    refs = retain_public(writer, fresh, values)
    writer.submit_revision("global-public-host-research", RevisionInput("global-public-host-research", "r1", "quick",
        "SYNTHETIC_GLOBAL_PUBLIC_PRIVATE_BODY\n", "Synthetic consumer boundary", "synthetic",
        unresolved=("Not real research or source acceptance.",)))
    host.requests.clear()
    return refs


def page_exports(page):
    """Run the actual entire shipped script and actual download/navigation handlers."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("full offline page execution requires the existing optional Node runtime")
    runner = r'''
const assert=require('assert'),vm=require('vm'),page=JSON.parse(require('fs').readFileSync(0,'utf8'));
const marker='<script id="data" type="application/json">',markup=page.split(marker)[0],model=page.split(marker)[1].split('</script>')[0];
const script=page.slice(page.lastIndexOf('<script>')+8).split('</script>')[0],created=[],ids=new Map(),blobs=new Map(),downloads=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.listeners={};created.push(this);}
 append(...items){this.children.push(...items);}prepend(...items){this.children.unshift(...items);}replaceChildren(...items){this.children=items;}
 get firstChild(){return this.children[0];}setAttribute(name,value){this[name]=value;}addEventListener(name,fn){this.listeners[name]=fn;}
 click(){if(this.disabled)return;if(this.tag==='a')downloads.push({name:this.download,blob:blobs.get(this.href)});else this.listeners.click?.call(this);}remove(){}
}
for(const match of markup.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g))ids.set(match[2],new Element(match[1]));
const data=new Element('script');data.textContent=model;ids.set('data',data);
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,createElement:tag=>new Element(tag),
 createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelector:()=>null};
const context=vm.createContext({document,Blob,location:{hash:''},setTimeout:fn=>fn(),URL:{createObjectURL:blob=>{
 const key='blob:synthetic-'+blobs.size;blobs.set(key,blob);return key;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});const original=vm.runInContext('JSON.stringify(D)',context);
const click=text=>{const found=created.filter(n=>n.tag==='button'&&n.textContent===text);assert.equal(found.length,1,text);assert(!found[0].disabled,text);found[0].click();};
click('下载完整全球背景模型 JSON');
const names={treasury:'美国国债期限利率',fx:'ECB 外汇参考价',commodities:'黄金与原油期货日线',crypto:'Coinbase 加密资产日线'};
const entities={treasury:'UST:30YEAR',fx:'EUR/GBP',commodities:'BZ=F',crypto:'ETH-USD'};
for(const [name,label] of Object.entries(names)){
 for(const n of created.filter(n=>n.tag==='button'&&(n.textContent===`导出${label}原根读取请求`||n.textContent.startsWith(`导出${label}来源 `))))n.click();
 click(`导出${label}下一页请求`);const select=created.find(n=>n.id===`global-${name}-entity`);assert(select);select.value=entities[name];
 click(`导出${label}筛选请求`);click(`下载${label}完整原读取结果 JSON`);
}
click('下载完整全球背景模型 JSON');assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,value:JSON.parse(await item.blob.text())});
process.stdout.write(JSON.stringify({exports,text:created.filter(n=>n.tag!=='script').map(n=>n.textContent).join('\n'),full_script_bytes:Buffer.byteLength(script)}));})();
'''
    result = subprocess.run([node, "-e", runner], input=json.dumps(page), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["full_script_bytes"] == len(page.rsplit("<script>", 1)[1].split("</script>", 1)[0].encode())
    return value


def test_native_public_normal_page_and_all_bare_source_navigation_requests_reopen(host, tmp_path, monkeypatch):
    grant, _ = public_grant(host, tmp_path, monkeypatch)
    refs = seed_public(host)
    plan = room_plan(refs, **{name: {"limit": 1} for name in refs})
    before, writes = host.git.head(), host.writes
    api, output = transport(host), tmp_path / "global-public-page"
    original_inputs = inputs("reading-room", {"plan": plan})
    report = research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["publication_authorization"] == grant["authorization_ref"]
    assert "public_sources_custody_confirmed" not in original_inputs["payload"]
    assert 0 < api._requests < MAX_REQUESTS == 512
    page = (output / "reading-room.html").read_text()
    assert page == render_page(report["result"]) and MARKER not in page
    executed = page_exports(page)
    assert MARKER in executed["text"]
    assert all(text in executed["text"] for text in ("ANNUAL_PERCENT", "USD_PER_EUR", "USD_PER_TROY_OUNCE", "USD_PER_BTC", "不计算区间收益"))
    exports = executed["exports"]
    assert [e["value"] for e in exports if e["name"] == "global-context-model.json"] == [report["result"]["global_context"]] * 2
    source_requests = [e["value"] for e in exports if e["value"].get("operation") == "read-source"]
    assert len(source_requests) == 11  # Four roots and 1+1+3+2 originals.
    for number, request in enumerate(source_requests):
        assert set(request["payload"]) == {"manifest", "selection"}
        restored = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request["operation"], request["payload"]), code_sha=CODE, output=output / ("source-" + str(number)))
        assert restored["status"] == "completed"
        expected = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(SourceRef.from_dict(request["payload"]))
        assert encoded(restored["result"]) == encoded(expected)
    navigations = [e["value"] for e in exports if e["value"].get("operation") == "reading-room"]
    assert len(navigations) == 8
    for number, request in enumerate(navigations):
        assert set(request["payload"]) == {"plan"}
        assert {name: part["reference"] for name, part in request["payload"]["plan"]["global_context"].items()} == refs
        reopened = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request["operation"], request["payload"]), code_sha=CODE, output=output / ("navigation-" + str(number)))
        for name, part in report["result"]["global_context"]["families"].items():
            assert reopened["result"]["global_context"]["families"][name]["view"] == part["view"]
    for name, count in COUNTS.items():
        part = report["result"]["global_context"]["families"][name]
        assert part["view"]["view"]["scope_count"] == count
        assert next(e["value"] for e in exports if e["name"] == "global-" + name + "-reader.json") == part["view"]
    assert all(method == "GET" for method, _, _ in host.requests)
    assert host.git.head() == before and host.writes == writes


def test_all_six_families_share_original_operation_budget_and_keep_their_denominators(host, tmp_path, monkeypatch):
    from test_global_room import retain_global
    public_grant(host, tmp_path, monkeypatch)
    refs = seed_public(host)
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
    refs.update(retain_global(writer, fresh))
    host.requests.clear()
    api, before, writes = transport(host), host.git.head(), host.writes
    result = research_runtime.run_command(host.binding, api=api,
        inputs=inputs("reading-room", {"plan": room_plan(refs)}), code_sha=CODE, output=tmp_path / "six-families")
    families = result["result"]["global_context"]["families"]
    assert set(families) == {"us_indices", "shibor", *COUNTS}
    assert result["result"]["global_context"]["status"] == "readable"
    for name, count in {"us_indices": 4, "shibor": 8, **COUNTS}.items():
        assert families[name]["view"]["view"]["scope_count"] == count
        assert families[name]["reference"] == refs[name]
    assert 0 < api._requests < MAX_REQUESTS == 512
    assert host.git.head() == before and host.writes == writes
    assert all(method == "GET" for method, _, _ in host.requests)


@pytest.mark.parametrize("private", [True, False], ids=["private-withheld", "authorized-public-input-retained"])
def test_one_original_403_after_entering_global_is_same_error_and_zero_later_reads(host, tmp_path, monkeypatch, private):
    if not private:
        public_grant(host, tmp_path, monkeypatch)
    refs = seed_public(host, private=private)
    _, root = read_public_root(VersionedStore(GitHubStore(host.binding, host)), refs["treasury"])
    manifest = root["sources"][0]["reference"]["manifest"]
    blob = host.git._run("rev-parse", manifest["commit"] + ":" + manifest["path"]).stdout.decode().strip()
    path = "/repos/" + host.binding.repository + "/git/blobs/" + blob
    api, state, trace = transport(host), {"inside": False, "injected": False}, []
    opener, original_request, actual = api._opener, api.request, reading_global.open_global
    class OneResponse:
        def open(self, request, timeout):
            if state["inside"] and not state["injected"] and request.method == "GET" and request.full_url == API_ROOT + path:
                state["injected"] = True
                raise HTTPError(request.full_url, 403, "Synthetic one-shot response", {}, io.BytesIO(b'{"message":"synthetic only"}'))
            return opener.open(request, timeout)
    def request(method, requested_path, payload=None):
        trace.append({"method": method, "path": requested_path, "inside_global": state["inside"]})
        target = state["inside"] and not state["injected"] and requested_path == path
        if target:
            state["failure_index"] = len(trace) - 1
        try:
            return original_request(method, requested_path, payload)
        except RecordError as error:
            trace[-1]["error"] = str(error)
            if target:
                state["origin"] = error
            raise
    def entered(*args, **kwargs):
        state["inside"] = True
        try:
            return actual(*args, **kwargs)
        except RecordError as error:
            state["escaped"] = error
            raise
        finally:
            state["inside"] = False
    api._opener = OneResponse()
    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(reading_global, "open_global", entered)
    before, writes, output = host.git.head(), host.writes, tmp_path / "global-public-terminal"
    original_inputs = inputs("reading-room", {"plan": room_plan(refs)})
    with pytest.raises(StoreUnavailable, match="REMOTE_NOT_AUTHORIZED") as caught:
        research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    report = json.loads((output / "report.json").read_bytes())
    assert state["injected"] and caught.value is state["origin"] is state["escaped"]
    assert trace[state["failure_index"] + 1:] == []
    assert 0 < api._requests == len(trace) < MAX_REQUESTS == 512
    assert report["status"] == "failed" and "result" not in report
    assert not (output / "reading-room.html").exists()
    assert (output / "inputs.json").exists() is (not private)
    if not private:
        assert json.loads((output / "inputs.json").read_bytes()) == original_inputs
    assert "SYNTHETIC_GLOBAL_PUBLIC_PRIVATE_BODY" not in (output / "report.json").read_text()
    assert host.git.head() == before and host.writes == writes
    (tmp_path / "global-public-403-evidence.json").write_bytes(encoded({"private": private,
        "same_error_origin_reader_runtime": caught.value is state["origin"] is state["escaped"],
        "requests": len(trace), "failure_index": state["failure_index"], "requests_after_error": 0,
        "inputs_released": (output / "inputs.json").exists(), "html_released": False, "trace": trace}))


def test_final_native_visibility_failure_after_four_complete_families_withholds_private_output(host, tmp_path, monkeypatch):
    refs = seed_public(host, private=True)
    original, complete = reading_global.open_global, []
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs)
        complete.append(result["status"])
        host.meta["private"] = False
        return result
    monkeypatch.setattr(reading_global, "open_global", reverse)
    output = tmp_path / "global-public-reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs("reading-room", {"plan": room_plan(refs)}), code_sha=CODE, output=output)
    assert complete == ["readable"]
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert "SYNTHETIC_GLOBAL_PUBLIC_PRIVATE_BODY" not in (output / "report.json").read_text()
