"""Normal native/local consumers and complete shipped HTML; all data synthetic."""
from dataclasses import asdict, replace
import base64
import hashlib
from html import unescape
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import HTTPError

import pytest

from decision_kernel.application import brief_delivery, research_book
from decision_kernel.application.research_artifacts import ResearchArtifacts
from decision_kernel.application.research_workflow import ResearchWorkflow
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import BlobRef, RecordError, StoreUnavailable, encoded
from decision_kernel.domain.research_work import QuestionFinding, ResearchProgress, ResearchQuestion, ResearchRequest
from decision_kernel.infrastructure.github_store import GitHubStore, MAX_REQUESTS
from decision_kernel.interfaces import research_runtime, news_rolling_cli
from decision_kernel.interfaces.reading_page import render_page
from test_global_room_host import public_grant
from test_news_delivery import index_ref, plan as brief_plan, retain, store
from test_news_read_budget import setup, transport
from test_research_book import AT, LITERAL, book_versions, entry
from test_research_runtime import CODE, host, inputs

ROOT = Path(__file__).parents[1]


def seed(host, monkeypatch, *, private=False, brief=False):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = private
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    old, current, source = book_versions(writer, monkeypatch)
    plan = {"as_of": AT, "reference": old}
    document = None
    if brief:
        document, _ = brief_delivery.build_edition(writer.read_source, ResearchArtifacts(writer),
            brief_plan(index_ref(writer), research={"items": [{"reference": old, "start": 1, "end": 1}]}))
        plan["brief"] = asdict(retain(writer, document, "research-book-brief"))
    host.requests.clear()
    return writer, old, current, source, plan, document


def page_exports(page):
    node = shutil.which("node")
    if node is None:
        pytest.skip("complete offline script execution uses the existing optional Node runtime")
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
 createObjectURL:blob=>{const id='blob:synthetic-'+blobs.size;blobs.set(id,blob);return id;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
const click=text=>{const found=created.filter(n=>n.tag==='button'&&n.textContent===text);assert.equal(found.length,1,text);assert(!found[0].disabled,text);found[0].click();};
click('导出确切研究读取请求');click('导出前驱版本阅读计划');click('导出 Book 材料阅读请求');
click('导出原来源 0 读取请求');click('导出所选问题 Review 请求');click('下载本版 Book 投影 JSON');
if(created.some(n=>n.tag==='button'&&n.textContent==='导出具名 Brief 读取请求'))click('导出具名 Brief 读取请求');
const count=downloads.length,field=created.find(n=>n.id==='research-book-review-before-end');field.value='999999';
click('导出所选问题 Review 请求');assert.equal(downloads.length,count);assert(ids.get('notice').textContent.includes('未导出'));
assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,value:JSON.parse(await item.blob.text())});
 process.stdout.write(JSON.stringify({exports,text:created.filter(n=>n.tag!=='script').map(n=>n.textContent).join('\n'),full_script_bytes:Buffer.byteLength(script)}));})();
'''
    run = subprocess.run([node, "-e", runner], input=json.dumps(page), text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@pytest.mark.parametrize("private", [False, True], ids=["trusted-public", "native-private"])
def test_original_host_complete_page_exports_reenter_exact_consumers(host, monkeypatch, tmp_path, private):
    grant = public_grant(host, tmp_path, monkeypatch) if not private else None
    _, old, current, source, plan, document = seed(host, monkeypatch, private=private, brief=True)
    before, writes, api = host.git.head(), host.writes, transport(host)
    output = tmp_path / "book-native"
    original_inputs = inputs("reading-room", {"plan": plan})
    report = research_runtime.run_command(host.binding, api=api, inputs=original_inputs, code_sha=CODE, output=output)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    assert report["repository_visibility"] == ("private" if private else "public")
    result = report["result"]
    assert entry(result, current)["brief_relation"]["status"] == "not-considered-in-this-brief"
    assert entry(result, old)["brief_relation"]["status"] == "included"
    page = (output / "reading-room.html").read_text()
    assert page == render_page(result) and LITERAL not in page and "\\u003c/script\\u003e" in page
    assert "innerHTML" not in page and "localStorage" not in page and "connect-src 'none'" in unescape(page)
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() in unescape(page)
    assert json.loads(page.split('<script id="data" type="application/json">')[1].split('</script>')[0]) == result
    executed = page_exports(page)
    assert LITERAL in executed["text"] and "研究事实截止：未建立" in executed["text"]
    assert executed["full_script_bytes"] == len(script.encode())
    assert 0 < api._requests < MAX_REQUESTS == 512
    exports = {item["name"]: item["value"] for item in executed["exports"]}
    assert exports["research-book-entry.json"] == entry(result, current)
    assert exports["research-book-source-0.json"] == {"operation": "read-source", "payload": source}
    assert exports["research-book-review.json"]["payload"]["plan"]["review"]["before"]["reference"] == old
    for name, request in exports.items():
        if request.get("operation") not in ("reading-room", "resume", "read-source"):
            continue
        if request["operation"] == "reading-room":
            cursor = request["payload"]["plan"]["catalogue"]["cursor"]
            assert cursor["snapshot"] == result["catalogue"]["snapshot"] and cursor["offset"] == result["catalogue"]["offset"]
        reopened = research_runtime.run_command(host.binding, api=transport(host),
            inputs=inputs(request["operation"], request["payload"]), code_sha=CODE, output=output / name.removesuffix(".json"))
        assert reopened["status"] == "completed" and reopened["write"] == "not-requested"
        if name == "research-book-review.json":
            assert reopened["result"]["review"]["view"]["revision_relation"] == "exact-direct-successor"
        if name == "reading-room-predecessor.json":
            assert entry(reopened["result"], old)["reference"] == old
    if not private:
        # Re-enter the original news host for the named Brief action, retaining
        # its own explicit publication checks and read-only job semantics.
        exported = exports["research-book-brief.json"]
        _, call = setup(monkeypatch, tmp_path, host, exported["plan"], saving=False, operation=exported["operation"])
        call.update(public_research_custody_confirmed="true", public_feedback_custody_confirmed="true")
        (tmp_path / "event.json").write_bytes(encoded({"inputs": call}))
        assert news_rolling_cli.main(["build", "--out", str(output / "named-brief")]) == 0
        assert json.loads((output / "named-brief/edition.json").read_bytes()) == document
    assert host.git.head() == before and host.writes == writes
    if grant:
        authorization, path = grant;authorization["enabled"] = False;path.write_bytes(encoded(authorization))
        denied = tmp_path / "book-revoked"
        with pytest.raises(RecordError, match="ROOM_PUBLIC_RESEARCH_NOT_CONFIRMED"):
            research_runtime.run_command(host.binding, api=transport(host), inputs=original_inputs, code_sha=CODE, output=denied)
        assert not denied.exists()


@pytest.mark.parametrize("failure", ["visibility", "http-403"])
def test_final_native_failure_after_book_projection_withholds_private_inputs_and_html(host, monkeypatch, tmp_path, failure):
    _, _, _, _, plan, _ = seed(host, monkeypatch, private=True)
    api = transport(host)
    original_attach, opener = research_book.attach_book, api._opener
    state = {"projected": False, "failed": False, "after": 0}
    class Once:
        def open(self, request, timeout):
            if state["failed"]:
                state["after"] += 1
            if state["projected"] and not state["failed"] and failure == "http-403":
                state["failed"] = True
                raise HTTPError(request.full_url, 403, "Synthetic final rejection", {}, io.BytesIO(b'{}'))
            return opener.open(request, timeout)
    def attach(*args, **kwargs):
        original_attach(*args, **kwargs)
        state["projected"] = True
        if failure == "visibility":
            host.meta["private"] = False
    api._opener = Once()
    monkeypatch.setattr(research_book, "attach_book", attach)
    output, before, writes = tmp_path / "book-rejected", host.git.head(), host.writes
    with pytest.raises(RecordError, match="PRIVATE_UPGRADE_REVERSED" if failure == "visibility" else "REMOTE_NOT_AUTHORIZED"):
        research_runtime.run_command(host.binding, api=api, inputs=inputs("reading-room", {"plan": plan}), code_sha=CODE, output=output)
    assert state["projected"] and state["after"] == 0
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    report = json.loads((output / "report.json").read_bytes())
    assert report["status"] == "failed" and "result" not in report and "合成经营委托" not in encoded(report).decode()
    assert host.git.head() == before and host.writes == writes


def test_main_store_cli_reopens_book_without_extra_input_or_a_second_install(store, monkeypatch, tmp_path):
    old, current, _ = book_versions(store, monkeypatch)
    source = tmp_path / "book-plan.json";source.write_bytes(encoded({"as_of": AT, "reference": old}))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    before, values = store.backend.head(), []
    for name in ("first", "reopen"):
        run = subprocess.run([sys.executable, "-m", "decision_kernel", "store", "--path", str(tmp_path / "news.git"),
            "research-room", "--input", str(source), "--out", str(tmp_path / (name + ".html"))],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        values.append(json.loads(run.stdout))
    assert values[0] == values[1] and entry(values[0], current)["workpaper_status"] == "validated"
    assert (tmp_path / "first.html").read_bytes() == (tmp_path / "reopen.html").read_bytes()
    assert store.backend.head() == before


def test_escaped_book_cannot_overflow_a_readable_original_page(host, monkeypatch, tmp_path):
    """The original 8 MiB HTML bound matters even below both JSON limits."""
    public_grant(host, tmp_path, monkeypatch)
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    originals = []
    for index in range(2):
        request = ResearchRequest(f"book-capacity-{index}", "quick", "Synthetic finite capacity comparison",
                                  (ResearchQuestion("q1", "Synthetic recorded question"),))
        progress = ResearchProgress("Synthetic narrative\n" + "<" * 150000,
            (QuestionFinding("q1", "answered", "<" * 150000),), "continue",
            "Synthetic only; no actual research",
            limitations=("Synthetic capacity input; no real business content.",))
        ref = ResearchWorkflow(writer).checkpoint(request, progress,
            submission_id=f"book-capacity-{index}", revision_id="r1")["reference"]
        originals.append(writer.read_revision(BlobRef(**ref)))
    before, writes, api = host.git.head(), host.writes, transport(host)
    output = tmp_path / "book-html-capacity"
    report = research_runtime.run_command(host.binding, api=api,
        inputs=inputs("reading-room", {"plan": {"as_of": AT}}), code_sha=CODE, output=output)
    assert report["status"] == "completed" and report["write"] == "not-requested"
    model = report["result"]
    base = {key: value for key, value in model.items() if key != "research_book"}
    assert len(render_page(base).encode()) < 8 * 1024 * 1024
    projections = [research_book.project_research(original, max_bytes=768 * 1024) for original in originals]
    full = research_book.book_panel(model["catalogue"], model["readings"], projections,
        model["agenda"], model["brief"], max_bytes=768 * 1024)
    assert full["status"] == "readable" and len(encoded(full)) < 768 * 1024
    unsafe = {**base, "research_book": full}
    assert len(encoded(unsafe)) < 6 * 1024 * 1024
    with pytest.raises(RecordError, match="^ROOM_HTML_BOUND$"):
        render_page(unsafe)
    assert model["research_book"] == {"schema": "research-book-v1", "status": "not-included",
        "error": "RESEARCH_BOOK_HTML_BOUND_USE_ORIGINAL"}
    assert model["catalogue"]["total_cases"] == model["catalogue"]["returned"] == 2
    for reading, original in zip(model["readings"], originals, strict=True):
        assert reading["status"] == "readable"
        assert reading["body"] == original["body"] and reading["reference"] == original["reference"]
        assert reading["open_request"]["payload"]["reference"] == original["reference"]
    page = (output / "reading-room.html").read_text()
    assert len(page.encode()) <= 8 * 1024 * 1024 and page == render_page(model)
    assert json.loads(page.split('<script id="data" type="application/json">')[1].split('</script>')[0]) == model
    assert json.loads((output / "report.json").read_bytes())["result"] == model
    assert host.git.head() == before and host.writes == writes and 0 < api._requests < MAX_REQUESTS
