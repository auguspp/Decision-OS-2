"""Same-snapshot Odds Book in the normal Room; synthetic real Git only."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from decision_kernel.application import odds_book, reading_room
from decision_kernel.application.conditional_odds import ConditionalOdds, RESULT_RECORD
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.domain.records import BlobRef, RecordError, RevisionInput, SourceRef, encoded
from decision_kernel.interfaces import odds_commands, reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_conditional_odds import authorized, context, spec_payload
from test_odds_book import saved_pair

AT = "2026-10-20T08:00:00Z"


def request(query=None, **changes):
    return {"as_of": AT, "odds_book": {} if query is None else query, **changes}


def html_model(page):
    return json.loads(page.split('<script id="data" type="application/json">')[1].split('</script>')[0])


def test_normal_room_uses_one_original_page_and_keeps_versions_prices_and_book(context, monkeypatch):
    store, research, spec, alias, first, second = saved_pair(context)
    newer = store.submit_revision("room-new", RevisionInput("case", "r2", "full", "Later synthetic Full.",
        "Same mandate", "synthetic", predecessor=research, unresolved=("Synthetic only",)))
    store.submit_revision("room-other", RevisionInput("other", "r1", "quick", "Other original.",
        "Another commission", "synthetic", unresolved=("Not assessed",)))
    before = store.backend.head()
    query = {"limit": 8, "scan_limit": 32}
    expected = odds_book.read_book(store, query)
    original, calls = odds_book.read_book, []
    def one(reader, value, registry=None):
        calls.append(deepcopy(value));return original(reader, value, registry=registry)
    monkeypatch.setattr(odds_book, "read_book", one)
    monkeypatch.setattr(ConditionalOdds, "compute", lambda *a, **k: pytest.fail("Room must not calculate"))
    result = reading_commands.perform(store, {"plan": request(query, reference=asdict(research))}, public=False)
    assert len(calls) == 1 and calls[0]["snapshot"] == result["catalogue"]["snapshot"] == before
    assert result["odds_book"]["view"] == expected
    assert result["research_book"]["catalogue"]["total_cases"] == result["catalogue"]["total_cases"] == 2
    assert result["research_book"]["explicit_versions"] == 1
    rows = result["odds_book"]["view"]["items"]
    prices = [r for r in rows if r["kind"] == RESULT_RECORD]
    assert {r["recorded"]["price"]["price"] for r in prices} == {"80", "100"}
    assert all(r["research"]["at_snapshot"] == {"status": "historical", "reference": asdict(newer)} for r in prices)
    assert any(r["reference"] == asdict(research) for r in result["readings"])
    assert html_model(render_page(result)) == result and store.backend.head() == before
    assert result["odds_book"]["open_request"] == {"operation": "read-odds-book", "payload": calls[0]}


def test_page_navigation_pins_both_denominators_refresh_releases_both(context):
    store, research, *_ = saved_pair(context)
    first = reading_commands.perform(store, {"plan": request({"limit": 1, "scan_limit": 2})}, public=False)
    page = first["odds_book"]["view"]
    assert page["next_cursor"] is not None
    store.submit_revision("after-room", RevisionInput("after", "r1", "quick", "After first page.", "Synthetic", "synthetic",
                                                     unresolved=("Synthetic only",)))
    plan = deepcopy(first["plan"])
    plan["odds_book"] = {"cursor": page["next_cursor"], "limit": 1, "scan_limit": 2}
    following = reading_commands.perform(store, {"plan": plan}, public=False)
    assert following["catalogue"] == first["catalogue"]
    assert following["odds_book"]["view"]["snapshot"] == page["snapshot"]
    assert following["odds_book"]["view"]["scan"]["start"] == page["scan"]["end"]
    fresh = reading_commands.perform(store, {"plan": following["refresh_plan"]}, public=False)
    assert fresh["catalogue"]["snapshot"] == fresh["odds_book"]["view"]["snapshot"] == store.backend.head()
    assert fresh["catalogue"]["total_cases"] == 2 and first["catalogue"]["total_cases"] == 1
    assert "cursor" not in following["refresh_plan"]["catalogue"]
    assert "snapshot" not in following["refresh_plan"]["odds_book"]


def test_conflicting_snapshot_rejected_without_calling_odds_reader(context, monkeypatch):
    store, _ = context
    old = store.backend.head()
    store.submit_revision("changed", RevisionInput("other", "r1", "quick", "Synthetic.", "Synthetic", "synthetic",
                                                  unresolved=("Synthetic only",)))
    monkeypatch.setattr(odds_book, "read_book", lambda *a, **k: pytest.fail("mismatched snapshot was read"))
    with pytest.raises(RecordError, match="ROOM_ODDS_BOOK_SNAPSHOT_MISMATCH"):
        reading_commands.perform(store, {"plan": request({"snapshot": old})}, public=False)


def test_filtered_broken_candidate_and_tail_cursor_do_not_hide_unknown(context):
    store, research, *_ = saved_pair(context)
    store.backend.commit({"research/artifacts/odds-result-orphan/" + "0" * 64 + "/content.bin": b"Synthetic orphan"},
                         expected=store.backend.head())
    first = ReadingRoom(store).build(request({"research_ref": asdict(research), "limit": 8, "scan_limit": 32}))
    page = first["odds_book"]["view"]
    bad = next(r for r in page["items"] if r["status"] != "readable")
    assert bad["filter_match"] == "unknown" and first["odds_book"]["status"] == "partial"
    cursor = {"schema": "odds-book-cursor-v1", "repository": page["repository"], "snapshot": page["snapshot"],
              "offset": page["scan"]["total_candidates"], "research_ref": asdict(research)}
    tail = ReadingRoom(store).build({**first["plan"], "odds_book": {"cursor": cursor}})["odds_book"]["view"]
    assert tail["items"] == [] and tail["next_cursor"] is None and tail["coverage"] == "this-page-only"
    assert tail["status"] == "no-match-in-this-page"


def test_absent_and_null_query_do_not_read_and_empty_query_is_explicit(context, monkeypatch):
    store, _ = context
    original, calls = odds_book.read_book, []
    def count(*args, **kwargs):
        calls.append(1);return original(*args, **kwargs)
    monkeypatch.setattr(odds_book, "read_book", count)
    empty = ReadingRoom(store).build({"as_of": AT})
    null = ReadingRoom(store).build({"as_of": AT, "odds_book": None})
    assert empty == null and calls == [] and "odds_book" not in empty
    selected = ReadingRoom(store).build(request())
    assert calls == [1] and selected["plan"]["odds_book"]["limit"] == 4
    assert selected["plan"]["odds_book"]["scan_limit"] == 16


def test_actual_escaped_html_capacity_omits_only_odds_and_preserves_original_book(context):
    store, old = context
    source = SourceRef.from_dict(store.read_revision(old)["record"]["input"]["sources"][0])
    large = "<" * 580000
    current = store.submit_revision("large-case", RevisionInput("case", "r2", "full", large,
        "Synthetic capacity case", "synthetic", (source,), predecessor=old))
    store.submit_revision("large-other", RevisionInput("other", "r1", "full", large,
        "Synthetic capacity other", "synthetic", (source,)))
    for index in range(4):
        payload = spec_payload(asdict(current), spec_id="capacity-" + str(index), limitations=["<" * 2000] * 32)
        ConditionalOdds(store).freeze(authorized("spec", payload), submission_id="capacity-spec-" + str(index))
    before = store.backend.head()
    base = reading_commands.perform(store, {"plan": {"as_of": AT}}, public=False)
    assert len(render_page(base).encode()) < 8 * 1024 * 1024
    result = reading_commands.perform(store, {"plan": request({"limit": 4, "scan_limit": 16})}, public=False)
    assert result["odds_book"]["status"] == "not-included"
    assert result["odds_book"]["error"] == "ROOM_ODDS_BOOK_HTML_BOUND_USE_ORIGINAL"
    assert result["readings"] == base["readings"] and result["research_book"] == base["research_book"]
    page = render_page(result)
    assert html_model(page) == result and len(page.encode()) <= 8 * 1024 * 1024
    assert len(encoded(result)) <= 6 * 1024 * 1024 and store.backend.head() == before


def test_panel_and_model_display_bounds_retain_exact_original_query(context, monkeypatch):
    store, *_ = saved_pair(context)
    first = ReadingRoom(store).build(request())
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 1000)
    limited = ReadingRoom(store).build(first["plan"])
    assert limited["odds_book"]["status"] == "not-included"
    assert limited["odds_book"]["query"] == first["plan"]["odds_book"]
    assert limited["odds_book"]["scan"] == first["odds_book"]["view"]["scan"]
    assert limited["readings"] == first["readings"]
    assert "read-odds-book" in render_page(limited)


def test_main_cli_emits_matching_json_and_offline_html(context, tmp_path):
    store, *_ = saved_pair(context)
    source, output = tmp_path / "room.json", tmp_path / "room.html"
    source.write_bytes(encoded(request({"limit": 2, "scan_limit": 8})))
    before = store.backend.head()
    run = subprocess.run([sys.executable, "-m", "decision_kernel", "store", "--path", str(tmp_path / "odds.git"),
        "research-room", "--input", str(source), "--out", str(output)], capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")})
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert html_model(output.read_text()) == result and result["odds_book"]["view"]["items"]
    assert store.backend.head() == before


def rendered_javascript(page, clicks=()):
    node = shutil.which("node")
    if node is None:
        pytest.skip("complete offline script execution uses the existing optional Node runtime")
    runner = r'''
const assert=require('assert'),vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8')),page=input.page;
const marker='<script id="data" type="application/json">',markup=page.split(marker)[0],model=page.split(marker)[1].split('</script>')[0];
const script=page.slice(page.lastIndexOf('<script>')+8).split('</script>')[0],created=[],ids=new Map(),blobs=new Map(),downloads=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this.textContent='';this.listeners={};created.push(this);}
 append(...v){this.children.push(...v);}prepend(...v){this.children.unshift(...v);}replaceChildren(...v){this.children=v;}
 get firstChild(){return this.children[0];}setAttribute(k,v){this[k]=v;}addEventListener(k,v){this.listeners[k]=v;}
 click(){if(this.disabled)return;if(this.tag==='a')downloads.push({name:this.download,blob:blobs.get(this.href)});else this.listeners.click?.call(this);}remove(){}
}
for(const m of markup.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g))ids.set(m[2],new Element(m[1]));
const data=new Element('script');data.textContent=model;ids.set('data',data);
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,createElement:t=>new Element(t),createTextNode:t=>{const e=new Element('#text');e.textContent=t;return e;},querySelector:()=>null};
const context=vm.createContext({document,Blob,location:{hash:''},setTimeout:f=>f(),URL:{createObjectURL:b=>{const id='blob:'+blobs.size;blobs.set(id,b);return id;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
for(const text of input.clicks){
 const b=created.find(e=>e.tag==='button'&&e.textContent===text);assert(b,text);b.click();
}
assert.equal(vm.runInContext('JSON.stringify(D)',context),original);
if(input.clicks.length)assert(ids.get('notice').textContent.includes('尚未执行'));
(async()=>{const values=[];for(const d of downloads)values.push({name:d.name,value:JSON.parse(await d.blob.text())});process.stdout.write(JSON.stringify({exports:values,text:created.filter(e=>e.tag!=='script').map(e=>e.textContent).join('\n'),pre:created.filter(e=>e.tag==='pre').map(e=>e.textContent)}));})();
'''
    executed = subprocess.run([node, "-e", runner], input=json.dumps({"page": page, "clicks": clicks}),
                              capture_output=True, text=True, timeout=20)
    assert executed.returncode == 0, executed.stderr
    return json.loads(executed.stdout)


def test_complete_shipped_javascript_exports_reenter_original_readers(context):
    store, *_ = saved_pair(context)
    result = reading_commands.perform(store, {"plan": request({"limit": 2, "scan_limit": 8})}, public=False)
    page, before = render_page(result), store.backend.head()
    view = rendered_javascript(page, ["导出本页原 Odds Book 请求", "导出 Odds 下一页请求", "导出原件读取请求",
                                     "导出此确切研究的 Odds 页请求", "导出此版本 Research Book 请求"])
    assert "保管 verified" in view["text"] and "原价格时点" in view["text"]
    exports = {row["name"]: row["value"] for row in view["exports"]}
    exact = exports["read-odds.json"]
    reopened = odds_commands.perform(store, exact["operation"], exact["payload"])
    assert reopened["recalculation"] == "matches"
    raw = exports["read-odds-book.json"]
    assert odds_commands.perform(store, raw["operation"], raw["payload"]) == result["odds_book"]["view"]
    for name in ("odds-book-next.json", "odds-book-research.json", "odds-book-research-version.json"):
        exported = exports[name]
        following = reading_commands.perform(store, exported["payload"], public=False)
        assert following["catalogue"] == result["catalogue"]
        assert following["odds_book"]["view"]["snapshot"] == result["catalogue"]["snapshot"]
    assert store.backend.head() == before


def test_stored_probability_displays_original_nested_clock_and_aggregate(tmp_path, monkeypatch):
    from decision_kernel.application.probability_odds import ProbabilityOdds
    from decision_kernel.application.versioned_store import VersionedStore
    from decision_kernel.infrastructure.git_store import GitStore
    from test_probability_odds import EVALUATED, build_context, calculation
    monkeypatch.setattr("decision_kernel.application.versioned_store._now", lambda: "2026-01-03T02:30:00+00:00")
    backend = GitStore.init_synthetic(tmp_path / "probability-room.git")
    store = VersionedStore(backend)
    context = build_context(store, lambda: VersionedStore(GitStore(backend.root)))
    saved = context[1].save(authorized("calculation", calculation(context)), submission_id="saved")
    monkeypatch.setattr(ProbabilityOdds, "compute", lambda *a, **k: pytest.fail("Room must not recalculate"))
    before = backend.head()
    result = reading_commands.perform(store, {"plan": request({"limit": 8, "scan_limit": 32})}, public=False)
    row = next(row for row in result["odds_book"]["view"]["items"] if row["reference"] == saved["reference"])
    original = row["recorded"]
    assert original["calculation_input"]["evaluated_at"] == EVALUATED
    assert original["aggregate"]["weighted_total_payoff_per_share"] == "95"
    page = render_page(result)
    view = rendered_javascript(page)
    assert "原计算时点 " + EVALUATED in view["text"]
    summary = next(json.loads(text) for text in view["pre"]
                   if '"aggregate"' in text and '"schema"' not in text)
    assert summary["aggregate"] == original["aggregate"] and "weighted_aggregate" not in summary
    assert "probability_status" in summary and "investment_authority" not in summary
    assert html_model(page) == result and backend.head() == before
