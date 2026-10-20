"""Offline reading room. Source/research text is literal data, never executable HTML."""
from __future__ import annotations

import base64
import hashlib
from html import escape
import json

from decision_kernel.domain.records import encoded, require

CSS = r"""
:root{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#183544;background:#f3f6f7;line-height:1.65}*{box-sizing:border-box}body{margin:0}header{background:#15394b;color:white;padding:28px max(20px,calc((100vw - 1280px)/2))}h1{margin:0;font-size:30px}h2{font-size:21px}h3{font-size:17px}header p{margin:4px 0;color:#dce9ee}main{max-width:1320px;margin:auto;padding:24px}nav,.actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center}nav{margin-top:16px}nav a{color:white}a{color:#075c76}.banner{padding:14px 18px;border-left:4px solid #aa671c;background:#fff3e1;margin-bottom:20px}.panel{background:white;border:1px solid #dce4e8;padding:20px;border-radius:7px;margin-bottom:20px;min-width:0;overflow-wrap:anywhere}.workspace{display:grid;grid-template-columns:minmax(220px,300px) minmax(0,1fr);gap:20px}.case{display:block;width:100%;text-align:left;margin:10px 0;padding:12px;background:#f4f8fa;border:1px solid #b7cbd5;border-radius:5px;overflow-wrap:anywhere}.case[aria-pressed=true]{border:2px solid #0c6965;background:#edf8f5}.case strong,.case small{display:block}button,input,select{font:inherit;max-width:100%;padding:8px;border:1px solid #9bb1bd;border-radius:4px}button{cursor:pointer;color:#15394b;background:#eaf2f5}button:disabled{opacity:.5;cursor:default}button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #c9822e;outline-offset:2px}input,select{width:100%;margin:5px 0 12px;background:white}label{display:block;font-size:14px}small,.muted{color:#516977}.badge{display:inline-block;padding:3px 9px;background:#edf4f3;margin:0 8px 6px 0;font-size:13px;border-radius:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:13px/1.7 ui-monospace,monospace;max-height:400px;overflow:auto}.body{font:15px/1.9 system-ui,sans-serif;max-height:none}.question{padding:12px 0;border-bottom:1px solid #e0e8eb}summary{cursor:pointer}#notice{color:#855018}footer{color:#516977;padding:16px;font-size:13px}[hidden]{display:none!important}
@media(max-width:720px){header{padding:20px}main{padding:12px}.workspace{grid-template-columns:1fr}.panel{padding:14px}h1{font-size:25px}.actions{gap:8px}}
"""
JS = r"""
'use strict';
const D=JSON.parse(document.getElementById('data').textContent),$=id=>document.getElementById(id);
const key=r=>r?JSON.stringify([r.repository,r.commit,r.path,r.sha256]):'';
const same=(a,b)=>key(a)===key(b);
const labels={readable:'可读',missing:'缺失',unavailable:'暂不可用',invalid:'校验未通过','not-included':'受显示容量限制',partial:'部分可读','not-supplied':'未提供','no-saved-brief':'尚无已存简报','permission-required':'反馈导出许可未确认','sources-permission-required':'来源导出许可未确认',included:'本简报已采用','considered-not-included':'本简报列入范围但未采用','not-considered-in-this-brief':'不在指定简报的研究范围','research-not-tracked-in-this-brief':'该简报未接入研究',unknown:'关系未知',focus:'继续关注',defer:'暂缓此版本',dismiss:'移出此版本','due-for-review':'到期待复核','overdue-for-review':'逾期待复核','declared-followup':'原稿声明仍有后续工作','no-structured-followup-declared':'没有结构化后续声明'};
const label=x=>labels[x]||x;
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function details(title,value){const n=el('details');n.append(el('summary',title),el('pre',JSON.stringify(value,null,2)));return n;}
function save(name,text,type){const u=URL.createObjectURL(new Blob([text],{type})),a=el('a');a.href=u;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}
function request(name,value){save(name,JSON.stringify(value,null,2),'application/json');$('notice').textContent='已导出读取请求，尚未执行。宿主调用前重新核代码版本与公开权利；本页不会自动提交。';}
function button(text,fn){const b=el('button',text);b.type='button';b.addEventListener('click',fn);return b;}
function roomRequest(plan){return {operation:'reading-room',payload:{plan}};}
const cards=D.catalogue.items.map(c=>({...c,reading:D.readings.find(r=>same(r.reference,c.reference))}));
for(const r of D.readings.filter(r=>r.origin==='explicit-reference'))cards.push({...r,case_id:r.case_id||r.reference.path,reading:r,pinned:true});
const cardKey=c=>key(c.reference)||'case:'+c.case_id;
let selected=null;
$('scope').textContent=`已存研究 ${D.catalogue.total_cases} 个案例 · 本页 ${D.catalogue.returned} 个 · 原文、简报与待复核问题集中阅读`;
$('clock').textContent=`本次阅读时点：${D.plan.as_of}`;
$('health').textContent=`本页${label(D.status)} · 研究议程${label(D.agenda.status)} · 简报${label(D.brief.status)}。数据日期与缺口见下方。`;
$('health').dataset.status=D.status;
$('provenance').append(details('本次范围、快照与精确引用',{plan:D.plan,snapshot:D.catalogue.snapshot,coverage:D.coverage,catalogue_status:D.catalogue.status,version_scope:D.catalogue.version_scope,order:D.catalogue.order,time_basis:D.time_basis}));
$('next').disabled=!D.next_plan;$('next').addEventListener('click',()=>request('reading-room-next.json',roomRequest(D.next_plan)));
$('refresh').addEventListener('click',()=>request('reading-room-refresh.json',roomRequest(D.refresh_plan||{...D.plan,catalogue:{limit:D.plan.catalogue.limit}})));
function show(c){
 const pane=$('research-detail');pane.replaceChildren();if(!c){pane.append(el('h2','尚未打开研究'),el('p','本页没有匹配项，不代表没有研究、没有待办或市场安静。'));return;}
 const r=c.reading,ref=c.reference;pane.append(el('h2',`${c.case_id} / ${c.revision_id||'版本未取得'}`));
 pane.append(el('p',c.mandate||'原委托见确切研究。','muted'));
 pane.append(el('span',label(r?r.status:c.status),'badge'),el('span',c.pinned?'显式指定版本，不判断是否为当前版':'固定目录快照中的版本','badge'));
 if(r)pane.append(el('p',`指定简报关系：${label(r.brief_relation.status)}`));
 const actions=el('div',undefined,'actions');
 if(ref)actions.append(button('原附件、来源与前版',()=>{if(D.materials&&same(D.materials.reference,ref))location.hash='materials';else materialRequest(ref);}));
 if(ref)actions.append(button('导出确切研究读取请求',()=>request('research-read.json',r?r.open_request:{operation:c.open_operation||'resume',payload:{reference:ref}})));
 if(c.source_next_cursor)actions.append(button('导出后续来源页请求',()=>request('research-sources-next.json',{operation:'resume',payload:{cursor:c.source_next_cursor}})));
 if(r&&r.status==='readable'){
  actions.append(button('下载原研究正文',()=>save('research.md',r.body,'text/markdown')));
  pane.append(el('p',`本版保存于 ${r.stored_at}`,'muted'));
  if(r.predecessor)actions.append(button('导出前驱版本阅读计划',()=>request('reading-room-predecessor.json',roomRequest({...D.plan,reference:r.predecessor}))));
 }
 pane.append(actions,details('引用、原未决项与本页来源范围',{reference:ref,body_sha256:r?.body_sha256,recorded_unresolved:c.recorded_unresolved,continuation:c.continuation,source_page:c.source_page,brief_relation:r?r.brief_relation:null}));
 if(r&&r.status==='readable')pane.append(readingDocument(r.body));
 else pane.append(el('p',`正文未展示：${r?r.error:c.error||c.status}。未截断或重写原件，使用上面的确切读取请求。`));
}
function render(){
 const q=$('query').value.toLocaleLowerCase(),kind=$('kind').value;
 const found=cards.filter(c=>(!kind||c.kind===kind)&&`${c.case_id} ${c.revision_id||''} ${c.mandate||''}`.toLocaleLowerCase().includes(q));
 if(!found.some(c=>cardKey(c)===selected))selected=found.length?cardKey(found[0]):null;
 $('cases').replaceChildren(...found.map(c=>{const b=button('',()=>{selected=cardKey(c);render();});b.className='case';b.setAttribute('aria-pressed',String(cardKey(c)===selected));b.append(el('strong',c.case_id),el('small',`${c.revision_id||'未知版本'} · ${label(c.reading?c.reading.status:c.status)}`));return b;}));
 $('count').textContent=`本地筛选匹配 ${found.length} 项；筛选只作用于本页。`;
 show(found.find(c=>cardKey(c)===selected)||null);
}
$('query').addEventListener('input',render);$('kind').addEventListener('change',render);
const agenda=$('agenda-content');
if(D.agenda.status==='readable'){
 const a=D.agenda.view;agenda.append(el('p',D.agenda.reference?'按指定已存议程接续；只读投影，没有保存新版本。':'未提供已存议程；仅显示原研究底稿的后续工作，不推断用户选择。'));
 for(const c of a.cards){const box=el('div',undefined,'question');box.append(el('h3',`${c.case_id||c.reference.path} / ${c.revision_id||''}`),el('p',label(c.status)));
  for(const q of c.agenda||[]){box.append(el('p',`${q.text} — ${label(q.attention_status)}`),details('原后续工作、选择与复核条件（声明，不是执行证明）',q));}watchConditions(box,c);agenda.append(box);}
 if(!a.cards.length)agenda.append(el('p','本页无可投影的研究；不等于没有待办。'));
 agenda.append(details('仍保留但不在本页的版本选择及读取限制',{unselected_attention:a.unselected_attention,status:a.status,history_verification:a.history_verification}));
}else agenda.append(el('p',`议程${label(D.agenda.status)}：${D.agenda.error}。选择状态未知，不当成零待办。`));
const brief=$('brief-content');
if(D.brief.status==='readable'){
 const b=D.brief.view;brief.append(el('p',`已存简报 · 内容截止 ${b.plan.cutoff}。本次打开后版本保持固定。`));
 brief.append(button('下载已存简报正文',()=>save('brief.md',b.brief_markdown,'text/markdown')),
  button('导出完整新闻工作台读取请求',()=>request('brief-read.json',{operation:'read-desk',plan:{reference:D.brief.reference},save:false})),
  details('简报引用与实际前版对照',{reference:D.brief.reference,edition_sha256:b.edition_sha256,comparison:b.comparison}));
 for(const row of b.research?.rows||[]){const ref=row.item.reference;brief.append(button(`查看研究 ${row.context?.case_id||ref.path}`,()=>{const c=cards.find(c=>same(c.reference,ref));if(c){selected=cardKey(c);$('query').value='';$('kind').value='';render();location.hash='research';}else request('reading-room-exact.json',roomRequest({...D.plan,reference:ref}));}));}
 brief.append(readingDocument(b.brief_markdown));
}else brief.append(el('p',`指定简报：${label(D.brief.status)}。${D.brief.error||'没有自动选择其他简报作替代。'} 采用关系不由目录浏览推断。`));
render();
"""


from decision_kernel.interfaces.reading_materials_page import JS as MATERIALS_JS

from decision_kernel.interfaces.research_review_page import JS as REVIEW_JS

from decision_kernel.interfaces.reading_industry_page import CSS as INDUSTRY_CSS, JS as INDUSTRY_JS

from decision_kernel.interfaces.reading_market_page import CSS as MARKET_CSS, JS as MARKET_JS
from decision_kernel.interfaces.reading_global_page import CSS as GLOBAL_CSS, JS as GLOBAL_JS
from decision_kernel.interfaces.reading_auction_page import CSS as AUCTION_CSS, JS as AUCTION_JS

from decision_kernel.interfaces.reading_overview_page import CSS as OVERVIEW_CSS, JS as OVERVIEW_JS
from decision_kernel.interfaces.reading_discovery_page import CSS as DISCOVERY_CSS, JS as DISCOVERY_JS
from decision_kernel.interfaces.reading_watch_page import JS as WATCH_JS
from decision_kernel.interfaces.reading_participant_page import CSS as PARTICIPANT_CSS, JS as PARTICIPANT_JS
from decision_kernel.interfaces.reading_calendar_page import CSS as CALENDAR_CSS, JS as CALENDAR_JS
from decision_kernel.interfaces.reading_decision_page import CSS as DECISION_CSS, JS as DECISION_JS
from decision_kernel.interfaces.reading_outcome_page import CSS as OUTCOME_CSS, JS as OUTCOME_JS
from decision_kernel.interfaces.reading_structure_page import CSS as STRUCTURE_CSS, JS as STRUCTURE_JS

CSS += INDUSTRY_CSS + MARKET_CSS + OVERVIEW_CSS + DISCOVERY_CSS
CSS += PARTICIPANT_CSS
CSS += DECISION_CSS
JS += MATERIALS_JS + REVIEW_JS + INDUSTRY_JS + MARKET_JS + OVERVIEW_JS + DISCOVERY_JS + WATCH_JS
JS += PARTICIPANT_JS
CSS += CALENDAR_CSS
JS += CALENDAR_JS + '\nrenderCalendar();\n'
JS += DECISION_JS + '\nrenderDecisionPreparation();\n'
CSS += OUTCOME_CSS
JS += OUTCOME_JS
CSS += STRUCTURE_CSS
JS += STRUCTURE_JS
CSS += GLOBAL_CSS
JS += GLOBAL_JS
CSS += AUCTION_CSS
JS += AUCTION_JS


def render_page(model):
    require(isinstance(model, dict) and model.get("schema") == "reading-room-v1", "ROOM_PAGE_MODEL")
    require(len(encoded(model)) <= 6 * 1024 * 1024, "ROOM_PAGE_MODEL_BOUND")
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    for char in ("<", ">", "&", "\u2028", "\u2029"):
        payload = payload.replace(char, "\\u%04x" % ord(char))
    def sha(text):
        return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
    policy = ("default-src 'none'; base-uri 'none'; form-action 'none'; connect-src 'none'; "
              "script-src 'sha256-" + sha(JS) + "'; style-src 'sha256-" + sha(CSS) + "'")
    participant_panel = ('<section class="panel" id="participants"><h2>参与者、公司与跨期观察</h2>'
                         '<div id="participants-content"></div></section>') if "participants" in model else ""
    calendar_panel = ('<section class="panel" id="event-calendar"><h2>事件日期与研究接续</h2>'
                      '<div id="event-calendar-content"></div></section>') if "event_calendar" in model else ""
    decision_panel = ('<section class="panel" id="decision-preparation"><h2>决策准备与多期限复核</h2>'
                      '<div id="decision-preparation-content"></div></section>') if "decision_preparation" in model else ""
    outcome_nav = '<a href="#outcome">原预测兑现</a>' if "outcome" in model else ""
    outcome_panel = ('<section class="panel" id="outcome"><h2>原预测兑现与固定交易窗口复核</h2>'
                     '<div id="outcome-content"></div></section>') if "outcome" in model else ""
    structure_nav = '<a href="#structure">收盘价格结构</a>' if model.get("structure") else ''
    structure_panel = ('<section class="panel" id="structure"><h2>收盘区间与右侧确认</h2>'
                       '<div id="structure-content"></div></section>') if model.get("structure") else ''
    global_nav = '<a href="#global-context">全球背景</a>' if "global_context" in model else ''
    global_panel = ('<section class="panel" id="global-context"><h2>全球背景：指定来源原件</h2>'
                    '<div id="global-content"></div></section>') if "global_context" in model else ''
    auction_nav = '<a href="#auction">竞价原池与同日接续</a>' if "auction" in model else ''
    auction_panel = ('<section class="panel" id="auction"><h2>竞价原池与同日收盘</h2>'
                     '<div id="auction-content"></div></section>') if "auction" in model else ''
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">
<title>Decision OS · 统一阅读室</title><style>{CSS}</style></head><body>
<header><p>DECISION OS 2.0</p><h1>日常阅读</h1><p id="scope"></p><p id="clock"></p>
<nav aria-label="阅读区域"><a href="#daily">日常概览</a><a href="#brief">已存简报</a><a href="https://github.com/auguspp/Decision-OS-2/actions/workflows/news-rolling.yml" target="_blank" rel="noopener noreferrer" title="选择 latest-desk；只读发现已保管新闻，不更新来源">News Desk</a><a href="#research">研究目录与原文</a><a href="#agenda">待复核问题</a><a href="#discovery">状态发现</a><a href="#market">A股行情</a>{global_nav}{auction_nav}{structure_nav}<a href="#industry">产业与公司角色</a><a href="#materials">原件与来源</a><a href="#review">按需复核</a>{outcome_nav}</nav></header>
<main><div id="health" class="banner"></div><p id="notice" aria-live="polite">页面是只读快照，不会自动刷新、提交或公开新内容。</p>
<section class="panel" id="daily"><h2>日常概览</h2><div id="overview-content"></div></section>
<section class="panel" id="brief"><h2>已存简报</h2><div id="brief-content"></div></section>
<div class="workspace" id="research"><aside class="panel"><h2>本页研究</h2><label for="query">案例、版本或原委托</label><input id="query" type="search" placeholder="仅搜索本页">
<label for="kind">研究类型</label><select id="kind"><option value="">全部</option><option value="quick">Quick</option><option value="full">Full</option></select>
<p id="count" aria-live="polite"></p><div id="cases"></div><div class="actions"><button id="next">导出下一页请求</button><button id="refresh">导出刷新目录请求</button></div></aside>
<section class="panel" id="research-detail" aria-label="研究原文"></section></div>
<section class="panel" id="market"><h2>A 股市场、原价与复权</h2><div id="market-content"></div></section>
{global_panel}
{auction_panel}
<section class="panel" id="discovery"><h2>行业、概念与独立个股状态发现</h2><div id="discovery-content"></div></section>
{structure_panel}
<section class="panel" id="industry"><h2>产业观察与公司经营角色</h2><div id="industry-content"></div></section>
{participant_panel}
<section class="panel" id="materials"><h2>原附件、来源表示与前版导航</h2><div id="materials-content"></div></section>
<section class="panel" id="review"><h2>原问题复核与修订段落采用</h2><div id="review-content"></div></section>
{calendar_panel}
{decision_panel}
{outcome_panel}
<section class="panel" id="agenda"><h2>研究议程与待复核条件</h2><div id="agenda-content"></div></section>
<section class="panel" id="provenance"><h2>读取范围与接续</h2></section>
<footer>离线只读投影，不是第二存档。原研究、议程和简报仍由各自精确引用恢复；来源文字不构成操作授权，阅读不代表人工接受或研究质量通过。</footer></main>
<script id="data" type="application/json">{payload}</script><script>{JS}</script></body></html>'''
    require(len(html.encode("utf-8")) <= 8 * 1024 * 1024, "ROOM_HTML_BOUND")
    return html
