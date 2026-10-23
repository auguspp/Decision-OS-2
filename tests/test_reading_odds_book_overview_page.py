"""The shipped offline overview script, with real stored fixtures and DOM events."""
from dataclasses import asdict
import json
import shutil
import subprocess

import pytest

from decision_kernel.application import odds_book, reading_room
from decision_kernel.application.conditional_odds import ConditionalOdds
from decision_kernel.application.reading_room import ReadingRoom
from decision_kernel.application.watch_activity import WatchActivity
from decision_kernel.domain.records import RevisionInput
from decision_kernel.interfaces import odds_commands, reading_commands
from decision_kernel.interfaces.reading_page import render_page
from test_conditional_odds import authorized, context, price_payload
from test_odds_book import saved_pair
from test_odds_watch import case, request as watch_request, save as save_check
from test_probability_odds import ctx, calculation
from test_reading_odds_book import AT, html_model
from test_watch_conditions import freeze, interval, numeric, v2_input
from test_watch_activity import apply, change


def plan(query=None, *, page_limit=4, detail=None, **changes):
    return {"as_of": AT, "odds_book_view": {
        "query": {"limit": 2} if query is None else query,
        "page_limit": page_limit, "detail": detail}, **changes}


def exercise(page, actions=()):
    """Exercise full render_page output; this does not replace browser visual QA."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("offline script interaction uses the existing optional Node runtime")
    runner = r'''
const assert=require('assert'),vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const marker='<script id="data" type="application/json">',markup=input.page.split(marker)[0];
const model=input.page.split(marker)[1].split('</script>')[0],script=input.page.slice(input.page.lastIndexOf('<script>')+8).split('</script>')[0];
const ids=new Map(),created=[],blobs=new Map(),downloads=[];
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.value='';this._text='';this.listeners={};created.push(this);}
 set id(value){this._id=value;ids.set(value,this);}get id(){return this._id;}
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return [this._text,...this.children.map(child=>child.textContent)].filter(Boolean).join('\n');}
 append(...values){this.children.push(...values);}prepend(...values){this.children.unshift(...values);}
 replaceChildren(...values){this._text='';this.children=values;}
 get firstChild(){return this.children[0];}setAttribute(key,value){this[key]=value;}
 addEventListener(kind,callback){this.listeners[kind]=callback;}
 focus(){this.focused=true;}remove(){}
 click(){if(this.disabled)return;if(this.tag==='a')downloads.push({name:this.download,blob:blobs.get(this.href)});else this.listeners.click?.call(this);}
}
for(const match of markup.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)){const node=new Element(match[1]);node.id=match[2];}
const data=new Element('script');data.id='data';data.textContent=model;
const document={body:new Element('body'),getElementById:id=>ids.get(id)||null,createElement:tag=>new Element(tag),
 createTextNode:text=>{const node=new Element('#text');node.textContent=text;return node;},querySelector:()=>null};
const location={hash:''},context=vm.createContext({document,Blob,location,setTimeout:fn=>fn(),
 URL:{createObjectURL:blob=>{const id='blob:'+blobs.size;blobs.set(id,blob);return id;},revokeObjectURL:()=>{}}});
vm.runInContext(script,context,{timeout:5000});
const original=vm.runInContext('JSON.stringify(D)',context);
const walk=node=>node?[node,...node.children.flatMap(walk)]:[];
const plain=node=>!node||node.tag==='details'?'':[node._text,...node.children.map(plain)].filter(Boolean).join('\n');
const text=id=>ids.get(id)?.textContent||'';
function state(){
 const table=ids.get('odds-overview-table'),body=table?.children.find(node=>node.tag==='tbody');
 return {count:text('odds-overview-count'),table:plain(table),rows:body?.children.map(plain)||[],
  failures:text('odds-overview-failures'),detail:plain(ids.get('odds-overview-selected')),
  exact:plain(ids.get('odds-overview-exact')),overview:plain(ids.get('odds-book-content')),
  research:plain(ids.get('research-detail')),hash:location.hash,
  results:walk(ids.get('odds-overview-selected')).filter(node=>node.className==='odds-overview-result').map(plain)};
}
const states=[state()];
for(const action of input.actions){
 if(action.set){const node=ids.get(action.set);assert(node,action.set);node.value=action.value;node.listeners[action.event||'change']?.call(node);}
 if(action.click){
  const scope=ids.get(action.scope||'odds-book-content'),buttons=walk(scope).filter(node=>node.tag==='button'&&node.textContent===action.click);
  const button=buttons[action.index||0];assert(button,'button '+action.click+' in '+(action.scope||'odds-book-content'));button.click();
 }
 states.push(state());
}
assert.equal(vm.runInContext('JSON.stringify(D)',context),original,'local interaction mutated the fixed receipt');
assert(!created.some(node=>node.tag==='img'||node.tag==='iframe'),'source text became active content');
(async()=>{const exports=[];for(const item of downloads){const text=await item.blob.text();exports.push({name:item.name,value:item.name.endsWith('.json')?JSON.parse(text):text});}
 process.stdout.write(JSON.stringify({states,exports,notice:text('notice')}));})();
'''
    run = subprocess.run([node, "-e", runner], input=json.dumps({"page": page, "actions": actions}),
                         capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def add_versions(store, research):
    newer = store.submit_revision("ui-newer", RevisionInput("case", "r2", "full",
        "Later synthetic research body.", "Same commission", "synthetic", predecessor=research,
        unresolved=("New unresolved evidence",)))
    other = store.submit_revision("ui-other", RevisionInput("other", "r1", "quick",
        "Separate synthetic body.", "Separate commission", "synthetic",
        unresolved=("No saved valuation or sourced conclusion",)))
    return newer, other


def test_default_room_discovers_overview_and_legacy_page_clears_single_page_selector(context):
    store, *_ = saved_pair(context)
    before = store.backend.head()
    for changes in ({}, {"odds_book": {"limit": 1}}):
        model = ReadingRoom(store).build({"as_of": AT, **changes})
        assert "odds_book_view" not in model
        result = exercise(render_page(model), [{"click": "导出同快照 Odds 总览请求（最多 4 页）"}])
        exported = result["exports"][0]["value"]
        assert exported["operation"] == "reading-room" and "尚未执行" in result["notice"]
        selection = exported["payload"]["plan"]
        assert "odds_book" not in selection
        assert selection["catalogue"]["cursor"]["snapshot"] == model["catalogue"]["snapshot"]
        assert selection["odds_book_view"] == {
            "query": {"snapshot": model["catalogue"]["snapshot"]}, "page_limit": 4, "detail": None}
        opened = reading_commands.perform(store, exported["payload"], public=False)
        assert opened["odds_book_view"]["view"]["schema"] == "odds-book-overview-v1"
        assert opened["odds_book_view"]["view"]["snapshot"] == before
        assert len(exercise(render_page(opened))["states"][0]["results"]) == 2
    assert store.backend.head() == before


def test_visible_prices_and_same_spec_results_keep_exact_strings_clocks_and_versions(context):
    store, research, spec, alias, first, second = saved_pair(context)
    exact = "100.12345678901234567890123456789012"
    note = 'Synthetic hypothetical price <img src=x onerror="throw 1">; not a market observation.'
    ConditionalOdds(store, allow_declared_only=True).save(authorized("calculation", {"spec_ref": spec,
        "price": price_payload(price_id="precise-ui", price=exact, note=note)}), submission_id="precise-ui")
    add_versions(store, research)
    before = store.backend.head()
    model = ReadingRoom(store).build(plan(reference=asdict(research)))
    page = render_page(model)
    assert html_model(page) == model and '<img src=x' not in page
    view = exercise(page)["states"][0]
    assert len(view["rows"]) == 3 and len(view["results"]) == 3
    assert exact + " CNY" in view["table"] and note in view["table"]
    assert "价格性质：声明参考价（性质见原注） · 原字段 declared-reference" in view["table"]
    assert "原价格时点：2026-01-02T16:00:00+08:00" in view["table"]
    assert "原评估时点：未记录" in view["table"]
    assert "原市场资格：未建立 · 原字段 not-established" in view["table"]
    assert "历史研究版本" in view["rows"][0] and "快照内当前研究" in view["table"]
    assert all("未建立概率" in value and "原未折现持有期回报" in value for value in view["results"])
    assert all(value["commit"] in page for value in (spec, alias, first, second))
    assert store.backend.head() == before


def test_probability_market_qualification_and_assessment_clock_are_primary(ctx):
    store, service, *_ = ctx
    result = service.save(authorized("calculation", calculation(ctx)), submission_id="ui-probability")
    saved = service.read(result["reference"])["result"]
    model = ReadingRoom(store).build(plan({"limit": 8}))
    view = exercise(render_page(model), [
        {"set": "odds-overview-state", "value": "probability"},
        {"set": "odds-overview-price-kind", "value": saved["price"]["kind"]},
    ])["states"][-1]
    assert len(view["rows"]) == 1
    assert "原评估时点：2026-01-03T04:00:00+00:00" in view["table"]
    assert saved["price"]["as_of"] in view["table"]
    assert saved["market_qualification"] in view["table"]
    assert saved["price"]["kind"] in view["table"]
    assert "原概率 Odds 结果" in view["detail"]
    assert saved["probability_status"] in view["detail"]
    assert "当前新价及今日适用性未认证" in view["table"]


def test_local_filters_never_hide_failed_candidates_or_unread_scope(context):
    store, research, *_ = saved_pair(context)
    add_versions(store, research)
    bad_path = "research/artifacts/odds-result-orphan/" + "0" * 64 + "/content.bin"
    store.backend.commit({bad_path: b"Synthetic orphan"}, expected=store.backend.head())
    before = store.backend.head()
    model = ReadingRoom(store).build(plan({"limit": 8}))
    view = model["odds_book_view"]["view"]
    assert view["counts"]["failed_items"] == 1 and not view["content_complete"]
    result = exercise(render_page(model), [
        {"set": "odds-overview-query", "value": "TEST.SZ", "event": "input"},
        {"set": "odds-overview-state", "value": "no-result"},
        {"click": "清除总览筛选"},
        {"set": "odds-overview-research-state", "value": "historical"},
    ])
    states = result["states"]
    assert len(states[1]["rows"]) == 1 and len(states[2]["rows"]) == 0
    assert len(states[3]["rows"]) == 3 and len(states[4]["rows"]) == 1
    for state in states:
        assert state["failures"] == states[0]["failures"]
        assert bad_path.rsplit("/", 1)[0] in state["failures"]
        assert "原候选分母 7" in state["count"] and "失败和省略未隐藏" in state["count"]
        assert "本次读取范围未见 Odds 结果" in states[0]["table"]
        assert "完整扫描未找到已存 Odds 结果" not in state["table"]
    assert result["exports"] == [] and store.backend.head() == before


def test_boundaries_match_one_original_metric_world_direction_and_exact_distance(case):
    conditions = [numeric("low", "90"), numeric("high"), interval("band"),
                  numeric("return", "0.1", "gte", "undiscounted_holding_period_return", "one")]
    store, service, watch = freeze(case, conditions)
    saved, check = save_check(service, watch_request(store, watch,
        price=v2_input(price_payload(price="97"))), "ui-boundaries")
    before = store.backend.head()
    model = ReadingRoom(store).build(plan({"limit": 8}))
    holding = next(item for item in check["next_boundaries"]
                   if item["metric"] == "undiscounted_holding_period_return")
    distance = holding["below"]["distance"]
    result = exercise(render_page(model), [
        {"set": "odds-overview-metric", "value": "price"},
        {"set": "odds-overview-direction", "value": "above"},
        {"set": "odds-overview-distance", "value": "2"},
        {"set": "odds-overview-distance", "value": "3"},
        {"set": "odds-overview-world", "value": "one"},
        {"click": "清除总览筛选"},
        {"set": "odds-overview-metric", "value": "undiscounted_holding_period_return"},
        {"set": "odds-overview-world", "value": "one"},
        {"set": "odds-overview-direction", "value": "below"},
        {"set": "odds-overview-distance", "value": distance},
        {"set": "odds-overview-review", "value": "first-trigger"},
    ])
    states = result["states"]
    assert len(states[2]["rows"]) == 1 and len(states[3]["rows"]) == 0
    assert len(states[4]["rows"]) == 1 and len(states[5]["rows"]) == 0
    assert len(states[-1]["rows"]) == 1
    assert distance in states[-1]["table"] and check["request"]["checked_at"] in states[-1]["table"]
    assert "原条件 return" in states[-1]["detail"]
    assert saved["path"] in render_page(model) and store.backend.head() == before


def test_frozen_watch_and_recorded_lifecycle_state_are_visible_without_a_new_check(case):
    store, _, _, _, watch = case
    model = ReadingRoom(store).build(plan({"limit": 8}))
    state = exercise(render_page(model))["states"][0]
    assert "本次已读 Watch 1" in state["table"]
    assert "pointer-absent-unknown" in state["table"]
    activity = WatchActivity(store)
    registered = apply(activity, change(watch), "ui-register")
    apply(activity, change(watch, registered, "retire", "2026-01-03T04:01:00Z"), "ui-retire")
    before = store.backend.head()
    model = ReadingRoom(store).build(plan({"limit": 8}))
    states = exercise(render_page(model), [
        {"set": "odds-overview-review", "value": "retired"},
    ])["states"]
    assert len(states[-1]["rows"]) == 1 and "retired" in states[-1]["table"]
    assert "pointer-present-not-replayed" in states[-1]["table"]
    assert "原活动状态未作当前认证" in states[-1]["table"]
    assert "此原管理事件未包含新条件检查" in states[-1]["detail"]
    assert store.backend.head() == before


def test_exported_next_detail_and_material_requests_reenter_with_exact_fixed_context(context):
    store, research, *_ = saved_pair(context)
    add_versions(store, research)
    before = store.backend.head()
    # One bounded range leaves a tail but includes original result requests.
    model = ReadingRoom(store).build(plan({"limit": 2}, page_limit=1))
    panel = model["odds_book_view"]
    actual = [row for page in panel["view"]["pages"] for row in page["items"]]
    export = exercise(render_page(model), [
        {"click": "导出本次总览读取请求"},
        {"click": "导出后续总览读取请求"},
        {"click": "导出此对象的总览详情请求"},
        {"click": "导出原件读取请求"},
        {"click": "导出确切研究与反证阅读请求"},
        {"click": "导出本版原材料阅读请求"},
    ])
    values = {item["name"]: item["value"] for item in export["exports"]}
    assert "尚未执行" in export["notice"]
    for value in values.values():
        if value["operation"] != "reading-room":
            continue
        exported = value["payload"]["plan"]
        assert "odds_book" not in exported
        assert exported["catalogue"]["cursor"]["snapshot"] == before
        assert exported["odds_book_view"]["query"]["snapshot"] == before
        assert exported["odds_book_view"]["page_limit"] == 1
    same = values["odds-book-overview.json"]["payload"]["plan"]
    assert same["odds_book_view"]["query"] == panel["query"]
    following = reading_commands.perform(store, values["odds-book-overview-next.json"]["payload"], public=False)
    assert following["odds_book_view"]["view"]["scan"]["start"] == panel["view"]["scan"]["end"]
    assert following["odds_book_view"]["view"]["scan"]["unread_prefix"] == panel["view"]["scan"]["end"]
    detail = values["odds-book-detail.json"]["payload"]["plan"]["odds_book_view"]["detail"]
    assert any(detail == {"operation": row["open_operation"], "payload": row["open_payload"]} for row in actual)
    opened = reading_commands.perform(store, values["odds-book-detail.json"]["payload"], public=False)
    assert opened["odds_book_view"]["detail"]["result"]["recalculation"] == "matches"
    raw = values["odds-book-read-odds.json"]
    assert odds_commands.perform(store, raw["operation"], raw["payload"])["recalculation"] == "matches"
    for name in ("odds-book-research-context.json", "odds-book-materials.json"):
        exported = values[name]["payload"]["plan"]
        assert exported["reference"] == asdict(research)
        assert exported["odds_book_view"]["query"] == panel["query"]
    materials = reading_commands.perform(store, values["odds-book-materials.json"]["payload"], public=False)
    assert materials["materials"]["reference"] == asdict(research)
    assert materials["odds_book_view"]["view"] == panel["view"]
    assert store.backend.head() == before


def test_loaded_exact_research_and_original_reader_details_remain_distinct(context):
    store, research, _, _, first, _ = saved_pair(context)
    add_versions(store, research)
    selection = {"operation": "read-odds", "payload": {"reference": first}}
    model = ReadingRoom(store).build(plan(reference=asdict(research), detail=selection))
    view = exercise(render_page(model), [
        {"set": "query", "value": "no-match", "event": "input"},
        {"click": "打开本页确切研究与反证"},
    ])["states"][-1]
    assert view["hash"] == "research-detail" and "case / r1" in view["research"]
    assert "Synthetic partial research" in view["research"]
    assert "原 reader 重演结果：matches" in view["exact"]
    assert "该确切请求已执行并返回可读结果" in view["exact"]
    assert "未把总表全部对象重新计算" in view["exact"]


def test_detail_outside_actual_range_has_no_readable_fallback(context):
    store, _, spec, *_ = saved_pair(context)
    model = ReadingRoom(store).build(plan({"limit": 1}, page_limit=1,
        detail={"operation": "read-valuation", "payload": {"reference": spec}}))
    exact = exercise(render_page(model))["states"][0]["exact"]
    assert "ROOM_ODDS_BOOK_DETAIL_NOT_IN_VIEW" in exact
    assert "该对象没有可读详情" in exact
    assert "该确切请求已执行并返回可读结果" not in exact


def test_capacity_omission_preserves_visible_counts_and_exact_original_reentry(context, monkeypatch):
    store, *_ = saved_pair(context)
    monkeypatch.setattr(odds_book, "MAX_CARD_BYTES", 20)
    model = ReadingRoom(store).build(plan({"limit": 1}))
    state = exercise(render_page(model), [
        {"set": "odds-overview-query", "value": "missing", "event": "input"},
    ])["states"][-1]
    assert state["rows"] == [] and "原容量省略 4" in state["failures"]
    assert "候选覆盖：已从0扫描至末尾" in state["overview"]
    assert "已扫描内容：存在内容缺项" in state["overview"]
    monkeypatch.setattr(reading_room, "MAX_PANEL_BYTES", 1000)
    omitted = ReadingRoom(store).build(plan({"limit": 1}))
    assert omitted["odds_book_view"]["status"] == "not-included"
    result = exercise(render_page(omitted), [{"click": "导出本次总览读取请求"}])
    assert "总览本次未显示" in result["states"][0]["overview"]
    assert result["exports"][0]["value"]["payload"]["plan"]["odds_book_view"]["query"] == omitted["odds_book_view"]["query"]
