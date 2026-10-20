"""Normal Room failures and source buttons over synthetic real Git/native wire.

The whole exported page executes before its actual market button is clicked.
No production source, credential, network or records are used by these tests.
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

from decision_kernel.application import reading_industry, reading_market, reading_participant
from decision_kernel.application.versioned_store import VersionedStore
from decision_kernel.domain.records import RecordError, SourceInput, SourceRef, StoreUnavailable, encoded
from decision_kernel.infrastructure.github_store import API_ROOT, MAX_REQUESTS, GitHubStore
from decision_kernel.interfaces import research_runtime
from test_industry_reading import prepare as prepare_industry
from test_market_room import plan as market_plan, retain
from test_news_read_budget import transport
from test_participant_reading import packet as save_packet
from test_participant_room_integration import plan as participant_plan, prepare
from test_research_runtime import CODE, host, inputs


@pytest.fixture
def private_participants(host):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    reference, packet = prepare(writer)
    manifest = packet["inputs"][0]["references"][0]["manifest"]
    blob = host.git._run("rev-parse", manifest["commit"] + ":" + manifest["path"]).stdout.decode().strip()
    return host, reference, packet, "/repos/" + host.binding.repository + "/git/blobs/" + blob


def participant_fault(host, path, failure, monkeypatch):
    """Fail the first child's real manifest GET once, then allow normal responses."""
    api = transport(host)
    opener, original_request = api._opener, api.request
    actual = reading_participant.open_participants
    state = {"inside": False, "injected": False, "escaped": None, "transport_error": None}
    trace = []

    class OneResponse:
        def open(self, request, timeout):
            if state["inside"] and not state["injected"] and request.method == "GET" and request.full_url == API_ROOT + path:
                state["injected"] = True
                raise HTTPError(request.full_url, failure, "Synthetic one-shot response", {},
                                io.BytesIO(b'{"message":"synthetic fixture only"}'))
            return opener.open(request, timeout)

    def request(method, requested_path, payload=None):
        event = {"method": method, "path": requested_path, "inside_participant": state["inside"]}
        trace.append(event)
        target = state["inside"] and not state["injected"] and method == "GET" and requested_path == path
        if target:
            state["failure_index"] = len(trace) - 1
            if failure == "request-budget":
                # The original 512-request budget is already spent. Do not change
                # the production limit or create a fresh client to finish the read.
                api._requests = MAX_REQUESTS
                state["injected"] = True
        try:
            return original_request(method, requested_path, payload)
        except RecordError as error:
            event["error"] = str(error)
            if target:
                state["transport_error"] = error
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

    if failure != "request-budget":
        api._opener = OneResponse()
    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(reading_participant, "open_participants", entered)
    return api, state, trace


@pytest.mark.parametrize("failure,reason", [
    (403, "REMOTE_NOT_AUTHORIZED"), (409, "REMOTE_HTTP_FAILURE"),
    ("request-budget", "REMOTE_REQUEST_LIMIT"),
])
def test_participant_terminal_native_failure_stops_room_before_any_fresh_read_or_private_export(
        private_participants, tmp_path, monkeypatch, failure, reason):
    host, reference, _, path = private_participants
    api, state, trace = participant_fault(host, path, failure, monkeypatch)
    before, writes = host.git.head(), host.writes
    output = tmp_path / "terminal"
    error = None
    try:
        research_runtime.run_command(host.binding, api=api,
            inputs=inputs("reading-room", {"plan": participant_plan(reference, family="holders-funds")}),
            code_sha=CODE, output=output)
    except RecordError as caught:
        error = caught
    report = json.loads((output / "report.json").read_bytes())
    (tmp_path / "transport-trace.json").write_bytes(encoded({
        "failure": failure, "trace": trace, "failure_index": state.get("failure_index"),
        "api_requests": api._requests, "runtime_error": str(error) if error else None,
        "report_status": report["status"], "inputs_released": (output / "inputs.json").exists(),
        "html_released": (output / "reading-room.html").exists(),
    }))
    assert state["injected"] and state["failure_index"] > 0
    assert isinstance(error, StoreUnavailable) and str(error) == reason
    assert error is state["escaped"] is state["transport_error"]
    assert trace[state["failure_index"] + 1:] == []
    assert report["status"] == "failed" and report["error"] == reason and "result" not in report
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert host.git.head() == before and host.writes == writes


def test_participant_ordinary_missing_child_remains_partial_with_original_scope(
        private_participants, tmp_path):
    host, _, original, _ = private_participants
    packet = deepcopy(original)
    manifest = packet["inputs"][0]["references"][0]["manifest"]
    manifest["path"] = "source-views/synthetic-missing/" + manifest["path"].split("/", 2)[2]
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    reference = save_packet(writer, packet)
    api = transport(host)
    before, writes = host.git.head(), host.writes
    output = tmp_path / "ordinary-gap"
    report = research_runtime.run_command(host.binding, api=api,
        inputs=inputs("reading-room", {"plan": participant_plan(reference, family="holders-funds", limit=1)}),
        code_sha=CODE, output=output)
    part = report["result"]["participants"]
    view = part["view"]
    assert report["status"] == "completed" and report["result"]["status"] == "partial"
    assert part["status"] == "readable" and part["partial"]
    assert view["scope"]["declared_inputs"] == len(packet["inputs"]) == 8
    assert view["scope"]["declared_families"] == 8 and view["scope"]["unavailable_inputs"] == 1
    assert view["input_outcomes"][0]["status"] == "unavailable"
    assert view["input_outcomes"][0]["references"] == json.loads(encoded(packet["inputs"][0]["references"]))
    assert all(item["status"] == "read" for item in view["input_outcomes"][1:])
    assert view["source_requests"] == view["writes"] == 0
    assert len(view["rows"]) == 1 and part["next_plan"] is not None
    assert (output / "inputs.json").exists() and (output / "reading-room.html").exists()
    assert host.git.head() == before and host.writes == writes


def test_market_terminal_native_failure_stops_room_before_fresh_read_or_private_export(
        host, tmp_path, monkeypatch):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    reference = retain(writer, lambda: VersionedStore(GitHubStore(host.binding, host)))
    manifest = reference["manifest"]
    blob = host.git._run("rev-parse", manifest["commit"] + ":" + manifest["path"]).stdout.decode().strip()
    path = "/repos/" + host.binding.repository + "/git/blobs/" + blob
    api = transport(host)
    opener, original_request, actual = api._opener, api.request, reading_market.open_market
    state = {"inside": False, "injected": False, "escaped": None, "transport_error": None}
    trace = []

    class OneResponse:
        def open(self, request, timeout):
            if state["inside"] and not state["injected"] and request.method == "GET" and request.full_url == API_ROOT + path:
                state["injected"] = True
                raise HTTPError(request.full_url, 403, "Synthetic one-shot response", {},
                                io.BytesIO(b'{"message":"synthetic fixture only"}'))
            return opener.open(request, timeout)

    def request(method, requested_path, payload=None):
        trace.append({"method": method, "path": requested_path, "inside_market": state["inside"]})
        target = state["inside"] and not state["injected"] and method == "GET" and requested_path == path
        if target:
            state["failure_index"] = len(trace) - 1
        try:
            return original_request(method, requested_path, payload)
        except RecordError as error:
            trace[-1]["error"] = str(error)
            if target:
                state["transport_error"] = error
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
    monkeypatch.setattr(reading_market, "open_market", entered)
    before, writes = host.git.head(), host.writes
    output, error = tmp_path / "market-terminal", None
    try:
        research_runtime.run_command(host.binding, api=api,
            inputs=inputs("reading-room", {"plan": market_plan(reference)}), code_sha=CODE, output=output)
    except RecordError as caught:
        error = caught
    report = json.loads((output / "report.json").read_bytes())
    (tmp_path / "transport-trace.json").write_bytes(encoded({
        "trace": trace, "failure_index": state.get("failure_index"), "api_requests": api._requests,
        "runtime_error": str(error) if error else None, "report_status": report["status"],
        "same_exception_api_to_open_market": state["transport_error"] is state["escaped"],
        "same_exception_api_to_runtime": state["transport_error"] is error,
        "inputs_released": (output / "inputs.json").exists(),
        "html_released": (output / "reading-room.html").exists(),
    }))
    assert state["injected"] and state["failure_index"] > 0
    assert isinstance(error, StoreUnavailable) and str(error) == "REMOTE_NOT_AUTHORIZED"
    assert error is state["escaped"] is state["transport_error"]
    assert trace[state["failure_index"] + 1:] == []
    assert api._requests == len(trace) < MAX_REQUESTS == 512
    assert report["status"] == "failed" and report["error"] == "REMOTE_NOT_AUTHORIZED" and "result" not in report
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert host.git.head() == before and host.writes == writes


@pytest.fixture
def private_industry(host):
    host.binding = replace(host.binding, private_upgrade_allowed=True)
    host.meta["private"] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    reference, _ = prepare_industry(writer)
    return host, reference


def industry_room_plan(reference):
    return {"as_of": "2026-10-17T09:30:00+08:00",
            "industry": {"reference": asdict(reference), "view": "industry"}}


@pytest.mark.parametrize("failure,reason", [
    ("source-403", "REMOTE_NOT_AUTHORIZED"),
    ("scope-exit-binding", "PUBLIC_BINDING_MISMATCH"),
])
def test_industry_terminal_failure_stops_room_before_fresh_read_or_private_export(
        private_industry, tmp_path, monkeypatch, failure, reason):
    host, reference = private_industry
    manifest = reference.manifest
    blob = host.git._run("rev-parse", manifest.commit + ":" + manifest.path).stdout.decode().strip()
    repository_path = "/repos/" + host.binding.repository
    fault_path = repository_path + "/git/blobs/" + blob
    api = transport(host)
    opener, original_request = api._opener, api.request
    original_scope = reading_industry.industry_source_scope
    original_open, original_read = reading_industry.open_industry, reading_industry.read_industry
    state = {"inside": False, "body_read": False, "injected": False, "failure_index": None,
             "origin_error": None, "escaped": None}
    trace = []

    def target(method, path):
        return (failure == "source-403" and state["inside"] and not state["injected"]
                and method == "GET" and path == fault_path)

    class OneResponse:
        def open(self, request, timeout):
            if target(request.method, request.full_url.removeprefix(API_ROOT)):
                state["injected"] = True
                trace[-1]["http_status"] = 403
                raise HTTPError(request.full_url, 403, "Synthetic one-shot response", {},
                                io.BytesIO(b'{"message":"synthetic fixture only"}'))
            response = opener.open(request, timeout)
            trace[-1]["http_status"] = response.status
            return response

    def request(method, path, payload=None):
        trace.append({"method": method, "path": path, "inside_industry": state["inside"],
                      "body_read": state["body_read"]})
        is_target = target(method, path)
        if is_target:
            state["failure_index"] = len(trace) - 1
        try:
            return original_request(method, path, payload)
        except RecordError as error:
            trace[-1]["error"] = str(error)
            if is_target:
                state["origin_error"] = error
                state["request_count_at_failure"] = len(trace)
            raise

    @contextmanager
    def scope(store):
        with original_scope(store) as reader:
            yield reader
        if failure == "scope-exit-binding":
            # The Room's shared backend owns its native exit checks. Inject a
            # declared source-scope failure here without replacing its reader.
            state["injected"] = True
            state["request_count_at_failure"] = len(trace)
            state["origin_error"] = RecordError("PUBLIC_BINDING_MISMATCH")
            raise state["origin_error"]

    def read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        state["body_read"] = True
        return result

    def entered(*args, **kwargs):
        state["inside"] = True
        try:
            return original_open(*args, **kwargs)
        except RecordError as error:
            state["escaped"] = error
            raise
        finally:
            state["inside"] = False

    api._opener = OneResponse()
    monkeypatch.setattr(api, "request", request)
    monkeypatch.setattr(reading_industry, "industry_source_scope", scope)
    monkeypatch.setattr(reading_industry, "read_industry", read)
    monkeypatch.setattr(reading_industry, "open_industry", entered)
    before, writes = host.git.head(), host.writes
    output, error = tmp_path / "industry-terminal", None
    try:
        research_runtime.run_command(host.binding, api=api,
            inputs=inputs("reading-room", {"plan": industry_room_plan(reference)}), code_sha=CODE, output=output)
    except RecordError as caught:
        error = caught
    report = json.loads((output / "report.json").read_bytes())
    (tmp_path / "transport-trace.json").write_bytes(encoded({
        "failure": failure, "trace": trace, "failure_index": state.get("failure_index"),
        "request_count_at_failure": state.get("request_count_at_failure"),
        "api_requests": api._requests, "runtime_error": str(error) if error else None,
        "origin_error_type": type(state["origin_error"]).__name__, "body_read": state["body_read"],
        "same_exception_origin_to_reader": state["origin_error"] is state["escaped"],
        "same_exception_origin_to_runtime": state["origin_error"] is error,
        "report_status": report["status"], "inputs_released": (output / "inputs.json").exists(),
        "html_released": (output / "reading-room.html").exists(),
    }))
    assert state["injected"] and state["request_count_at_failure"] > 0
    assert state["body_read"] is (failure == "scope-exit-binding")
    assert type(error) is (StoreUnavailable if failure == "source-403" else RecordError)
    assert str(error) == reason and error is state["escaped"] is state["origin_error"]
    assert trace[state["request_count_at_failure"]:] == []
    assert api._requests == len(trace) < MAX_REQUESTS == 512
    assert report["status"] == "failed" and report["error"] == reason and "result" not in report
    assert not (output / "inputs.json").exists() and not (output / "reading-room.html").exists()
    assert host.git.head() == before and host.writes == writes


def test_industry_ordinary_missing_packet_remains_local_in_private_room(private_industry, tmp_path):
    host, original = private_industry
    missing = replace(original, manifest=replace(original.manifest,
        path="source-views/synthetic-industry-missing/" + original.manifest.path.split("/", 2)[2]))
    before, writes = host.git.head(), host.writes
    output = tmp_path / "industry-missing"
    report = research_runtime.run_command(host.binding, api=transport(host),
        inputs=inputs("reading-room", {"plan": industry_room_plan(missing)}), code_sha=CODE, output=output)
    panel = report["result"]["industry"]
    assert report["status"] == "completed" and report["result"]["status"] == "partial"
    assert panel["status"] == "missing" and panel["error"] == "ROOM_OBJECT_MISSING"
    assert panel["reference"] == json.loads(encoded(asdict(missing))) and "view" not in panel
    assert report["repository_visibility"] == "private" and report["write"] == "not-requested"
    assert (output / "inputs.json").exists() and (output / "reading-room.html").exists()
    assert host.git.head() == before and host.writes == writes


def market_button_request(page):
    """Run the complete native HTML script with DOM/blob sinks, without replacing request/save."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("full offline page execution requires the existing optional Node runtime")
    runner = r'''
const assert=require('assert'),vm=require('vm');
const page=JSON.parse(require('fs').readFileSync(0,'utf8'));
const marker='<script id="data" type="application/json">';
const markup=page.split(marker)[0],model=page.split(marker)[1].split('</script>')[0];
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
 createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element('#text');n.textContent=text;return n;},
 querySelector:()=>null};
const sandbox={document,Blob,location:{hash:''},setTimeout:fn=>fn(),URL:{
 createObjectURL:blob=>{const key='blob:synthetic-'+blobs.size;blobs.set(key,blob);return key;},revokeObjectURL:()=>{}}};
vm.runInNewContext(script,sandbox,{timeout:5000});
const buttons=created.filter(n=>n.tag==='button'&&n.textContent==='导出原市场根读取请求'&&!n.disabled);
assert.equal(buttons.length,1);buttons[0].click();assert.equal(downloads.length,1);
(async()=>{const exports=[];for(const item of downloads)exports.push({name:item.name,...JSON.parse(await item.blob.text())});
 process.stdout.write(JSON.stringify({exports,full_script_bytes:Buffer.byteLength(script)}));})();
'''
    run = subprocess.run([node, "-e", runner], input=json.dumps(page), text=True,
                         capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    value = json.loads(run.stdout)
    script = page.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    assert value["full_script_bytes"] == len(script.encode()) and len(value["exports"]) == 1
    return value["exports"][0]


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("kind,status", [("retained", "readable"), ("invalid-original", "invalid")])
def test_complete_market_page_source_buttons_reopen_exact_native_root_and_invalid_original(
        host, tmp_path, private, kind, status):
    if private:
        host.binding = replace(host.binding, private_upgrade_allowed=True)
        host.meta["private"] = True
    writer = VersionedStore(GitHubStore(replace(host.binding, write_enabled=True), host))
    factory = lambda: VersionedStore(GitHubStore(host.binding, host))
    if kind == "retained":
        reference = retain(writer, factory)
    else:
        invalid = writer.put_source("market-invalid-original", SourceInput("market-invalid-original", "content",
            "Synthetic non-market original", "synthetic:market-consumer", '{"schema":"not-a-market-root"}\n'))
        reference = json.loads(encoded(asdict(invalid)))
    before, writes = host.git.head(), host.writes
    if not private:
        calls = len(host.requests)
        with pytest.raises(RecordError, match="PUBLIC_MARKET"):
            research_runtime.run_command(host.binding, api=transport(host),
                inputs=inputs("reading-room", {"plan": market_plan(reference),
                    "public_research_custody_confirmed": True, "public_sources_custody_confirmed": False}),
                code_sha=CODE, output=tmp_path / "denied")
        assert len(host.requests) == calls and not (tmp_path / "denied").exists()
    destination = tmp_path / kind
    payload = {"plan": market_plan(reference)}
    if not private:
        payload.update(public_research_custody_confirmed=True, public_sources_custody_confirmed=True)
    room = research_runtime.run_command(host.binding, api=transport(host),
        inputs=inputs("reading-room", payload), code_sha=CODE, output=destination)
    assert room["status"] == "completed" and room["write"] == "not-requested"
    assert room["repository_visibility"] == ("private" if private else "public")
    assert room["result"]["market"]["status"] == status
    page = (destination / "reading-room.html").read_text()
    embedded = page.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(embedded) == room["result"]
    exported = market_button_request(page)
    assert exported["name"] == "market-root.json" and exported["operation"] == "read-source"
    # Use exactly the actual button payload in the normal host, without a
    # repair wrapper or permission fields inserted by the test or page.
    read = research_runtime.run_command(host.binding, api=transport(host),
        inputs=inputs(exported["operation"], exported["payload"]), code_sha=CODE,
        output=destination / "source-read")
    assert exported["payload"] == reference and set(exported["payload"]) == {"manifest", "selection"}
    assert read["status"] == "completed" and read["write"] == "not-requested"
    restored = VersionedStore(GitHubStore(host.binding, transport(host))).read_source(SourceRef.from_dict(reference))
    assert encoded(read["result"]) == encoded(restored)
    assert encoded(json.loads((destination / "source-read/report.json").read_bytes())["result"]) == encoded(read["result"])
    if status == "invalid":
        assert read["result"]["data"] == '{"schema":"not-a-market-root"}\n'
    assert host.git.head() == before and host.writes == writes
