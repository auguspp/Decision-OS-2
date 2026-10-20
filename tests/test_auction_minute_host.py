"""Original GitHubAPI/real temporary Git and the actual shipped page handlers."""
from dataclasses import replace
from html import unescape
import io
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application import reading_auction_minute
from decision_kernel.application.market_custody import retain_market_reading
from decision_kernel.application.market_reading import read_market_payload
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT, GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_auction_minute_inputs import REGISTRY, seed_payload, minute_payload
from test_auction_minute_room import room_plan
from test_auction_minute_custody import root_document
from test_global_room_host import public_grant
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs

LITERAL = "</script><img src=x onerror=alert(1)>"


def seed(host, *, private=False):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = private
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
    payload = seed_payload()
    for index in (0, 8):
        payload["pool_result"]["capture"]["pages"][0]["table"]["items"][index][2] = LITERAL
    auction = retain_market_reading(payload, REGISTRY, writer, fresh_reader=fresh, public_rights_confirmed=True)
    value = minute_payload(read_market_payload(payload, REGISTRY), auction["reference"])
    saved = retain_market_reading(value, REGISTRY, writer, fresh_reader=fresh, public_rights_confirmed=True)
    writer.submit_revision("minute-host-research", RevisionInput("minute-host-research", "r1", "quick",
        "SYNTHETIC_MINUTE_PRIVATE_BODY\n", "Synthetic consumer boundary", "synthetic",
        unresolved=("Not real minute/source acceptance.",)))
    host.requests.clear()
    return json.loads(encoded(saved["reference"]))


def page_exports(page):
    node = shutil.which("node")
    if node is None:
        pytest.skip("full offline page execution requires the existing optional Node runtime")
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
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,createElement:tag=>new Element(tag),
 createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelector:()=>null};
const context=vm.createContext({document,Blob,location:{hash:''},setTimeout:fn=>fn(),URL:{
 createObjectURL:blob=>{const key='blob:synthetic-'+blobs.size;blobs.set(key,blob);return key;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
const click=text=>{const found=created.filter(n=>n.tag==='button'&&n.textContent===text);assert.equal(found.length,1,text);assert(!found[0].disabled,text);found[0].click();};
click('下载完整分钟观察 JSON');click('导出分钟原根读取请求');
for(const n of created.filter(n=>n.tag==='button'&&/^导出分钟来源 /.test(n.textContent)))n.click();
click('导出分钟第一页请求');click('导出分钟下一页请求');
const field=created.find(n=>n.id==='auction-minute-entity');assert(field);field.value='830001.BJ';
click('导出分钟筛选请求');click('下载完整分钟观察 JSON');
assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,value:JSON.parse(await item.blob.text())});
 process.stdout.write(JSON.stringify({exports,text:created.filter(n=>n.tag!=='script').map(n=>n.textContent).join('\n'),full_script_bytes:Buffer.byteLength(script)}));})();
'''
    run = subprocess.run([node, "-e", runner], input=json.dumps(page), text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    value = json.loads(run.stdout)
    assert value["full_script_bytes"] == len(page.rsplit("<script>", 1)[1].split("</script>", 1)[0].encode())
    return value


@pytest.mark.parametrize("private", [False, True], ids=["trusted-public", "native-private"])
def test_normal_host_full_page_exports_reopen_exact_root_sources_pages_and_bj(host, tmp_path, monkeypatch, private):
    grant = public_grant(host, tmp_path, monkeypatch) if not private else None
    reference = seed(host, private=private)
    before, writes, api = host.git.head(), host.writes, transport(host)
    output = tmp_path / "minute-page"
    # The literal name is on 600001.SH, after the sorted 300001.SZ member.
    # Select its actual page; the full cohort is intentionally not re-ordered.
    original_inputs = inputs("reading-room", {"plan": room_plan(reference, limit=1, offset=1)})
    report = research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["repository_visibility"] == ("private" if private else "public")
    assert 0 < api._requests < MAX_REQUESTS == 512
    if grant:
        assert report["publication_authorization"] == grant[0]["authorization_ref"]
        assert "public_sources_custody_confirmed" not in json.loads(original_inputs["payload"])
    page = (output / "reading-room.html").read_text()
    assert page == render_page(report["result"]) and 'id="auction-minute-content"' in page
    assert LITERAL not in page and "\\u003c/script\\u003e" in page
    assert "innerHTML" not in page and "localStorage" not in page and "connect-src 'none'" in unescape(page)
    executed = page_exports(page)
    assert LITERAL in executed["text"] and "NOT_COMPUTED" in executed["text"] and "09:39" in executed["text"]
    part = report["result"]["auction_minute"]
    full = [e["value"] for e in executed["exports"] if e["name"] == "auction-minute-model.json"]
    assert full == [part, part] and part["view"]["view"]["cohort_denominator"] == 9
    sources = [e["value"] for e in executed["exports"] if e["value"].get("operation") == "read-source"]
    assert len(sources) == 3
    for number, request in enumerate(sources):
        assert set(request["payload"]) == {"manifest", "selection"}
        out = research_runtime.run_command(host.binding, api=transport(host), inputs=inputs(request["operation"], request["payload"]),
            code_sha=CODE, output=output / ("source-" + str(number)))
        original = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(SourceRef.from_dict(request["payload"]))
        assert out["status"] == "completed" and encoded(out["result"]) == encoded(original)
    requests = [e["value"] for e in executed["exports"] if e["value"].get("operation") == "reading-room"]
    assert len(requests) == 3
    for number, request in enumerate(requests):
        assert set(request["payload"]) == {"plan"} and request["payload"]["plan"]["auction_minute"]["reference"] == reference
        out = research_runtime.run_command(host.binding, api=transport(host), inputs=inputs(request["operation"], request["payload"]),
            code_sha=CODE, output=output / ("navigation-" + str(number)))
        assert out["result"]["auction_minute"]["view"] == part["view"]
        if number == 2:
            assert out["result"]["auction_minute"]["page"]["entries"][0]["entity_id"] == "830001.BJ"
    assert all(method == "GET" for method, _, _ in host.requests)
    assert host.git.head() == before and host.writes == writes
    if grant:
        document, path = grant; document["enabled"] = False; path.write_bytes(encoded(document))
        denied = tmp_path / "revoked"
        with pytest.raises(RecordError, match="ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED"):
            research_runtime.run_command(host.binding, api=transport(host), inputs=original_inputs, code_sha=CODE, output=denied)
        assert not denied.exists()
    (tmp_path / "minute-page-evidence.json").write_bytes(encoded({"private": private,
        "normal_requests": api._requests, "source_requests_exported_and_read": len(sources),
        "navigation_requests_exported_and_read": len(requests), "full_script_bytes": executed["full_script_bytes"],
        "whole_cohort_denominator": part["view"]["view"]["cohort_denominator"], "writes": 0}))


@pytest.mark.parametrize("private,kind", [(True, "403"), (False, "403"), (True, "budget")],
                         ids=["private-one-403", "public-one-403", "original-512-budget"])
def test_actual_store_failure_same_object_and_zero_further_reads_or_html(host, tmp_path, monkeypatch, private, kind):
    if not private:
        public_grant(host, tmp_path, monkeypatch)
    reference = seed(host, private=private)
    reader = VersionedStore(GitHubStore(host.binding, host))
    root = root_document(reader, reference)
    original_ref = SourceRef.from_dict(root["sources"][0]["reference"])
    original = reader.read_source(original_ref)
    body_path = original["manifest"]["representation"]["path"]
    blob = host.git._run("rev-parse", original_ref.manifest.commit + ":" + body_path).stdout.decode().strip()
    path = "/repos/" + host.binding.repository + "/git/blobs/" + blob
    api, state, trace = transport(host), {"inside": False, "injected": False}, []
    opener, request_original, actual = api._opener, api.request, reading_auction_minute.open_auction_minute
    class Once:
        def open(self, request, timeout):
            if state["inside"] and not state["injected"] and kind == "403" and request.full_url == API_ROOT + path:
                state["injected"] = True
                raise HTTPError(request.full_url, 403, "Synthetic one-shot refusal", {}, io.BytesIO(b'{"message":"synthetic only"}'))
            return opener.open(request, timeout)
    def request(method, requested_path, payload=None):
        target = state["inside"] and not state["injected"] and requested_path == path
        if target and kind == "budget":
            state["injected"] = True
            while api._requests < MAX_REQUESTS:
                request("GET", "/repos/" + host.binding.repository)
        trace.append({"method": method, "path": requested_path, "inside_minute": state["inside"]})
        try:
            return request_original(method, requested_path, payload)
        except RecordError as error:
            trace[-1]["error"] = str(error)
            if target:
                state["origin"], state["failure_index"] = error, len(trace) - 1
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
    api._opener = Once()
    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(reading_auction_minute, "open_auction_minute", entered)
    before, writes, output = host.git.head(), host.writes, tmp_path / "minute-terminal"
    original_inputs = inputs("reading-room", {"plan": room_plan(reference)})
    with pytest.raises(StoreUnavailable, match="REMOTE_NOT_AUTHORIZED" if kind == "403" else "REMOTE_REQUEST_LIMIT") as caught:
        research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    report = json.loads((output / "report.json").read_bytes())
    assert state["injected"] and caught.value is state["origin"] is state["escaped"]
    assert trace[state["failure_index"] + 1:] == [] and api._requests == len(trace)
    assert api._requests == MAX_REQUESTS + 1 if kind == "budget" else 0 < api._requests < MAX_REQUESTS
    assert report["status"] == "failed" and "result" not in report and not (output / "reading-room.html").exists()
    assert (output / "inputs.json").exists() is (not private)
    if not private:
        assert json.loads((output / "inputs.json").read_bytes()) == original_inputs
    assert "SYNTHETIC_MINUTE_PRIVATE_BODY" not in (output / "report.json").read_text()
    assert host.git.head() == before and host.writes == writes
    (tmp_path / "minute-terminal-evidence.json").write_bytes(encoded({"private": private, "kind": kind,
        "same_error_origin_reader_runtime": caught.value is state["origin"] is state["escaped"],
        "requests": len(trace), "failure_index": state["failure_index"],
        "requests_after_error": len(trace[state["failure_index"] + 1:]),
        "inputs_released": (output / "inputs.json").exists(), "html_released": False, "trace": trace}))


def test_private_reversal_after_complete_minute_model_withholds_inputs_and_html(host, tmp_path, monkeypatch):
    reference = seed(host, private=True)
    actual, completed = reading_auction_minute.open_auction_minute, []
    def reverse(*args, **kwargs):
        result = actual(*args, **kwargs)
        completed.append(result["view"]["view"]["cohort_denominator"])
        host.meta["private"] = False
        return result
    monkeypatch.setattr(reading_auction_minute, "open_auction_minute", reverse)
    output = tmp_path / "minute-reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(host.binding, api=transport(host), inputs=inputs("reading-room", {"plan": room_plan(reference)}),
                                    code_sha=CODE, output=output)
    assert completed == [9] and not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert "SYNTHETIC_MINUTE_PRIVATE_BODY" not in (output / "report.json").read_text()
