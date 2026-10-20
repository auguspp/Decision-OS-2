"""Original GitHubAPI/ModelWire, native host and full offline global page script."""
from copy import deepcopy
from dataclasses import asdict, replace
import io
import json
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from decision_kernel.application import reading_global
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, RevisionInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure import runtime_publication as policy
from decision_kernel.infrastructure.github_store import API_ROOT, GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import research_runtime
from decision_kernel.interfaces.reading_page import render_page
from test_global_room import retain_global, room_plan, source_query
from test_news_read_budget import transport
from test_research_runtime import CODE, host, inputs


def seed(host, *, private=False):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = private
    # Use the original native custody writer and fresh reader even while the
    # wire model stores bytes in real local synthetic Git.
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    fresh = lambda: VersionedStore(GitHubStore(host.binding, host))
    refs = retain_global(writer, fresh)
    writer.submit_revision("global-host-research", RevisionInput("global-host-research", "r1", "quick",
        "SYNTHETIC_GLOBAL_PRIVATE_BODY\n", "Synthetic consumer boundary", "synthetic",
        unresolved=("Not real research or source acceptance.",)))
    host.requests.clear()
    return refs


def public_grant(host, tmp_path, monkeypatch):
    """Bind the real fixed-file policy to this test's own immutable synthetic store."""
    host.binding = replace(host.binding, repository=policy.REPOSITORY, repository_id=policy.REPOSITORY_ID,
        branch="records", store_id=policy.STORE_ID, visibility="public", private_upgrade_allowed=True)
    anchor = host.git.commit({"store.json": host.binding.marker()}, expected=host.git.head())
    host.binding = replace(host.binding, anchor=anchor)
    host.meta.update(id=policy.REPOSITORY_ID, full_name=policy.REPOSITORY, private=False)
    monkeypatch.setattr(policy, "ANCHOR", anchor)
    doc = {"schema": "public-runtime-authorization-v1", "enabled": True,
        "repository": policy.REPOSITORY, "repository_id": policy.REPOSITORY_ID, "branch": "records",
        "anchor": anchor, "store_id": policy.STORE_ID,
        "authorization_ref": "https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674",
        "scopes": sorted(policy.SCOPES)}
    path = tmp_path / "synthetic-public-runtime.json"; path.write_bytes(encoded(doc))
    monkeypatch.setattr(policy, "AUTHORIZATION_FILE", path)
    return doc, path


def page_exports(page):
    """Execute the entire shipped script with DOM/blob sinks and real handlers."""
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
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,
 createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},querySelector:()=>null};
const sandbox={document,Blob,location:{hash:''},setTimeout:fn=>fn(),URL:{
 createObjectURL:blob=>{const key='blob:synthetic-'+blobs.size;blobs.set(key,blob);return key;},revokeObjectURL:()=>{}}};
const context=vm.createContext(sandbox);vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
const click=text=>{const found=created.filter(n=>n.tag==='button'&&n.textContent===text);assert.equal(found.length,1,text);assert(!found[0].disabled,text);found[0].click();};
click('下载完整全球背景模型 JSON');
for(const n of created.filter(n=>n.tag==='button'&&/^导出(美股指数|Shibor 利率)(原根|来源 )/.test(n.textContent)))n.click();
for(const [name,label,entity] of [['us_indices','美股指数','SPX'],['shibor','Shibor 利率','3m']]){
 click(`导出${label}下一页请求`);
 const select=created.find(n=>n.id===`global-${name}-entity`);assert(select);select.value=entity;
 click(`导出${label}筛选请求`);click(`下载${label}完整原读取结果 JSON`);
}
click('下载完整全球背景模型 JSON');
assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,value:JSON.parse(await item.blob.text())});
 process.stdout.write(JSON.stringify({exports,text:created.filter(n=>n.tag!=='script').map(n=>n.textContent).join('\n'),full_script_bytes:Buffer.byteLength(script)}));})();
'''
    result = subprocess.run([node, "-e", runner], input=json.dumps(page), text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["full_script_bytes"] == len(page.rsplit("<script>", 1)[1].split("</script>", 1)[0].encode())
    return value


@pytest.mark.parametrize("private", [False, True], ids=["trusted-public", "native-private"])
def test_native_global_page_source_and_navigation_requests_reopen_original_models(host, tmp_path, monkeypatch, private):
    grant = public_grant(host, tmp_path, monkeypatch) if not private else None
    refs = seed(host, private=private)
    plan = room_plan(refs, us_indices={"limit": 1}, shibor={"limit": 1})
    before, writes = host.git.head(), host.writes
    output = tmp_path / "global-page"; api = transport(host)
    original_inputs = inputs("reading-room", {"plan": plan})
    report = research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["repository_visibility"] == ("private" if private else "public")
    assert 0 < api._requests < MAX_REQUESTS == 512
    if grant:
        assert report["publication_authorization"] == grant[0]["authorization_ref"]
        assert "public_sources_custody_confirmed" not in original_inputs["payload"]
    else:
        assert "publication_authorization" not in report
    page = (output / "reading-room.html").read_text()
    assert page == render_page(report["result"])
    assert 'href="#global-context"' in page and 'id="global-content"' in page
    executed = page_exports(page)
    assert "指数点位（index-points）" in executed["text"] and "年百分率" in executed["text"] and "基点差 bp" in executed["text"]
    exports = executed["exports"]
    complete = [e["value"] for e in exports if e["name"] == "global-context-model.json"]
    assert complete == [report["result"]["global_context"]] * 2
    source_requests = [e["value"] for e in exports if e["value"].get("operation") == "read-source"]
    assert len(source_requests) == 8  # Two roots, five US queries, one Shibor query.
    for number, request in enumerate(source_requests):
        assert set(request["payload"]) == {"manifest", "selection"}
        restored = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request["operation"], request["payload"]), code_sha=CODE,
            output=output / ("source-" + str(number)))
        assert restored["status"] == "completed"
        original = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(SourceRef.from_dict(request["payload"]))
        assert encoded(restored["result"]) == encoded(original)
    navigations = [e["value"] for e in exports if e["value"].get("operation") == "reading-room"]
    assert len(navigations) == 4
    for number, request in enumerate(navigations):
        assert set(request["payload"]) == {"plan"}
        assert {name: part["reference"] for name, part in request["payload"]["plan"]["global_context"].items()} == refs
        reopened = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request["operation"], request["payload"]), code_sha=CODE,
            output=output / ("navigation-" + str(number)))
        for name, part in report["result"]["global_context"]["families"].items():
            assert reopened["result"]["global_context"]["families"][name]["view"] == part["view"]
    for name in refs:
        full = next(e["value"] for e in exports if e["name"] == "global-" + name + "-reader.json")
        assert full == report["result"]["global_context"]["families"][name]["view"]
    assert all(method == "GET" for method, _, _ in host.requests)
    assert host.git.head() == before and host.writes == writes
    if grant:
        doc, path = grant; doc["enabled"] = False; path.write_bytes(encoded(doc))
        denied = tmp_path / "revoked-public"
        with pytest.raises(RecordError, match="ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED"):
            research_runtime.run_command(host.binding, api=transport(host), inputs=original_inputs, code_sha=CODE, output=denied)
        assert not denied.exists()


@pytest.mark.parametrize("private", [True, False], ids=["private-output-withheld", "authorized-public-input-retained"])
def test_single_native_403_after_entering_global_stops_all_later_reads_and_body_output(host, tmp_path, monkeypatch, private):
    if not private:
        public_grant(host, tmp_path, monkeypatch)
    refs = seed(host, private=private)
    reader = VersionedStore(GitHubStore(host.binding, host))
    _, query = source_query(reader, refs["us_indices"], code="DJI")
    manifest = query["pages"][0]["reference"]["manifest"]
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
    before, writes, output = host.git.head(), host.writes, tmp_path / "global-terminal"
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
    assert "SYNTHETIC_GLOBAL_PRIVATE_BODY" not in (output / "report.json").read_text()
    assert host.git.head() == before and host.writes == writes
    (tmp_path / "global-403-evidence.json").write_bytes(encoded({"private": private,
        "same_error_origin_reader_runtime": caught.value is state["origin"] is state["escaped"],
        "requests": len(trace), "failure_index": state["failure_index"],
        "requests_after_error": len(trace[state["failure_index"] + 1:]),
        "inputs_released": (output / "inputs.json").exists(),
        "html_released": (output / "reading-room.html").exists(), "trace": trace}))


def test_private_visibility_reversal_after_complete_global_model_cannot_release(host, tmp_path, monkeypatch):
    refs = seed(host, private=True)
    original, complete = reading_global.open_global, []
    def reverse(*args, **kwargs):
        result = original(*args, **kwargs)
        complete.append(result["status"])
        host.meta["private"] = False
        return result
    monkeypatch.setattr(reading_global, "open_global", reverse)
    output = tmp_path / "global-reversed"
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED"):
        research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs("reading-room", {"plan": room_plan(refs)}), code_sha=CODE, output=output)
    assert complete == ["readable"]
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert "SYNTHETIC_GLOBAL_PRIVATE_BODY" not in (output / "report.json").read_text()


def test_literal_global_text_and_existing_room_capacity_do_not_create_executable_markup(tmp_path):
    from test_global_room import global_inputs
    from decision_kernel.application.reading_room import ReadingRoom
    from decision_kernel.infrastructure.git_store import GitStore
    from test_market_relay_inputs import REGISTRY
    store = VersionedStore(GitStore.init_synthetic(tmp_path / "literal.git"))
    fresh = lambda: VersionedStore(GitStore(tmp_path / "literal.git"))
    refs = retain_global(store, fresh, global_inputs())
    room = ReadingRoom(store, market_registry=REGISTRY).build(room_plan(refs, us_indices={"limit": 1}, shibor={"limit": 1}))
    marker = '</script><img src=x onerror=alert(1)>'
    room["global_context"]["families"]["us_indices"]["view"]["view"]["qualification_limits"].append(marker)
    page = render_page(room)
    assert marker not in page and "\\u003c/script\\u003e" in page
    executed = page_exports(page)
    assert marker in executed["text"]
    assert "innerHTML" not in page and "localStorage" not in page
    from html import unescape
    assert "connect-src 'none'" in unescape(page) and "max-width:1000px" in page
    assert len(page.encode()) < 8 * 1024 * 1024
