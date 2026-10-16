"""Self-contained offline news desk; remote text is data, never HTML or JavaScript."""
from __future__ import annotations

import base64
import hashlib
from html import escape
import json

from decision_kernel.domain.records import encoded, require

CSS = r"""
:root{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;color:#142c3d;background:#f3f5f6;line-height:1.65}
*{box-sizing:border-box}body{margin:0}header{background:#12354a;color:white;padding:30px max(24px,calc((100vw - 1200px)/2))}header p{color:#d8e8ed;margin:5px 0}h1{font-size:30px;margin:0}h2{font-size:20px;margin:0 0 12px}h3{font-size:17px;margin:0 0 10px}main{max-width:1250px;margin:auto;padding:24px}.banner{border-left:4px solid #ac671d;background:#fff5e6;padding:12px 18px;margin-bottom:18px}.bar,.stats,.pager{display:flex;gap:12px;flex-wrap:wrap;align-items:center}.stats{margin:18px 0}.stat{background:white;padding:14px 22px;border:1px solid #dce4e8;border-radius:6px;flex:1;min-width:150px}.stat strong{font-size:28px;display:block}.muted,small{color:#516775}.panel{background:white;border:1px solid #dce4e8;padding:20px;margin-bottom:18px;border-radius:7px}input,select,textarea,button{font:inherit;border:1px solid #9caeb9;border-radius:4px;padding:7px 10px;max-width:100%}input,select,textarea{background:white;color:#163548}button{background:#eaf1f5;color:#173c50;cursor:pointer}button.primary{background:#0c6262;color:white}button:disabled{opacity:.5;cursor:default}button:focus-visible,input:focus,select:focus,textarea:focus{outline:3px solid #d89736;outline-offset:2px}label{display:block;font-size:13px}.filters{display:grid;grid-template-columns:2fr 1fr 1fr;gap:14px}.filters input,.filters select{width:100%}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin:18px 0}.card{background:white;border:1px solid #dce4e8;border-radius:6px;padding:18px;overflow-wrap:anywhere}.card .meta{font-size:12px;color:#526c7a;margin-bottom:8px}.tag{font-size:12px;padding:3px 8px;background:#eaf3f2;border-radius:3px;display:inline-block;margin:0 6px 6px 0}.actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}a{color:#075b80}textarea{width:100%;min-height:60px;margin:5px 0 10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.65 ui-monospace,monospace;max-height:450px;overflow:auto}#brief-body{font:15px/1.9 system-ui,sans-serif;max-height:none}summary{cursor:pointer}#notice{color:#855018;font-weight:600}footer{padding:24px 0;font-size:12px;color:#526c7a}p{margin:8px 0}.pager{justify-content:space-between}[hidden]{display:none!important}.wide{grid-column:1/-1}
@media(max-width:720px){header{padding:22px 18px}main{padding:14px}.grid,.filters{grid-template-columns:1fr}h1{font-size:25px}.panel{padding:15px}.stat{padding:10px 14px;min-width:110px}.stat strong{font-size:22px}}
"""

JS = r"""
'use strict';
const DATA=JSON.parse(document.getElementById('data').textContent), E=DATA.edition;
const $=id=>document.getElementById(id), rows=DATA.articles, drafts=new Map();
const state=new Map(E.attention_state.map(c=>[c.entry_id,c]));
const included=new Set(E.included.map(c=>c.entry_id));
const labels={focus:'继续关注',defer:'暂缓此版本',dismiss:'移出此版本',reset:'未选择'};
let offset=0, briefOnly=false; const PAGE=40;
const fmt=s=>s?new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Singapore',dateStyle:'short',timeStyle:'medium',hour12:false}).format(new Date(s)):'未取得';
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function choice(id){return drafts.get(id)||state.get(id)||{entry_id:id,action:'reset',reason:'',question:'',review_at:null};}
function notice(){ $('notice').textContent=drafts.size?`有 ${drafts.size} 个本地选择变更。尚未存档；请导出计划后显式提交。`:'没有新的选择。浏览、搜索和打开原文不会自动记成已读或人工接受。';}
function update(id,patch){drafts.set(id,{...choice(id),...patch});notice();}
const sources=[...new Set([...rows.map(r=>r.source),...E.window.source_outcomes.map(r=>r.outcome.source)])].sort();
for(const s of sources){const n=el('option',s);n.value=s;$('source').append(n);}
$('window').textContent=`${fmt(E.window.window_start)} → ${fmt(E.window.as_of)} · 新加坡时间 · 24小时观察窗`;
$('cutoff').textContent=`简报截止 ${fmt(E.plan.cutoff)}；页面是冻结快照，不会自动更新。`;
$('n-all').textContent=rows.length;$('n-brief').textContent=E.included.length;
$('n-fail').textContent=E.window.coverage.failed_source_windows;
$('health-note').textContent=`整窗有 ${E.window.coverage.failed_source_windows} 个历史失败窗口、${E.window.coverage.unlocated_source_windows} 个未定位时钟。筛选不会隐藏缺口；失败不等于没有新闻。尚未启用连续采集或通知。`;
$('health').textContent=JSON.stringify({coverage:E.window.coverage,source_outcomes:E.window.source_outcomes,losses:E.window.losses,rejected_references:E.window.rejected_references},null,2);
$('brief-title').textContent=({morning:'晨间新闻简报',evening:'晚间新闻简报',adhoc:'即时新闻简报'})[E.plan.kind];
$('brief-body').textContent=E.brief_markdown;
$('provenance').textContent=JSON.stringify({window_reference:E.plan.window,edition_reference:DATA.saved_reference,edition_sha256:E.edition_sha256,baseline:E.plan.baseline,comparison:E.comparison},null,2);
$('saved').textContent=DATA.saved_reference?'此版本已保管，可按精确引用重建。':'此页尚无存档回执；文件存在不代表已保存到 records。';
function filtered(){
 const q=$('query').value.toLocaleLowerCase(),source=$('source').value,status=$('status').value;
 const from=$('from').value?Date.parse($('from').value+':00+08:00'):-Infinity;
 const to=$('to').value?Date.parse($('to').value+':00+08:00'):Infinity;
 if(from>to){$('filter-error').textContent='起点不能晚于终点。';return [];}
 $('filter-error').textContent='';
 return rows.filter(r=>(!source||r.source===source)&&r.title.toLocaleLowerCase().includes(q)&&
  (!status||choice(r.entry_id).action===status)&&(!briefOnly||included.has(r.entry_id))&&
  Date.parse(r.last_observed)>=from&&Date.parse(r.last_observed)<=to);
}
function card(r){
 const n=el('article',undefined,'card'); n.dataset.entryId=r.entry_id;
 n.append(el('div',`${r.source} · 最后观察 ${fmt(r.last_observed)} · 窗口内 ${r.observations} 次`, 'meta'),el('h3',r.title));
 n.append(el('span',included.has(r.entry_id)?'本期简报采用':'仍可阅读，未入本期简报','tag'),el('span','观察待解释 · 研究未启动','tag'));
 const claim=r.published_claim.raw;n.append(el('p',`媒体时间声明：${claim==null?'缺失':claim}（未核实）`,'muted'));
 const line=el('div',undefined,'actions');
 if(r.url){const a=el('a','打开来源原链接');a.href=r.url;a.target='_blank';a.rel='noopener noreferrer';line.append(a);}else line.append(el('span','未取得单篇原链接','muted'));
 const select=el('select');select.setAttribute('aria-label',`选择：${r.title}`);
 for(const [v,label] of Object.entries(labels)){const op=el('option',label);op.value=v;select.append(op);} select.value=choice(r.entry_id).action;
 line.append(select);n.append(line);
 const edit=el('div');edit.hidden=select.value==='reset';
 const reason=el('textarea');reason.maxLength=600;reason.value=choice(r.entry_id).reason;reason.setAttribute('aria-label','选择理由');
 const question=el('textarea');question.maxLength=400;question.value=choice(r.entry_id).question;question.setAttribute('aria-label','待研究问题');
 const due=el('input');due.type='datetime-local';due.setAttribute('aria-label','复核时间（新加坡）');
 const oldDue=choice(r.entry_id).review_at;if(oldDue)due.value=new Date(Date.parse(oldDue)+8*3600000).toISOString().slice(0,16);
 edit.append(el('label','理由（必填；不要填私人或无权公开的内容）'),reason,el('label','待研究问题（仅“继续关注”可填；不自动启动 Quick）'),question,el('label','暂缓复核时间（可选，新加坡时间）'),due);n.append(edit);
 const apply=()=>{update(r.entry_id,{action:select.value,reason:select.value==='reset'?'恢复未选择状态':reason.value,question:select.value==='focus'?question.value:'',review_at:select.value==='defer'&&due.value?new Date(due.value+':00+08:00').toISOString():null});edit.hidden=select.value==='reset';};
 select.addEventListener('change',apply);reason.addEventListener('input',apply);question.addEventListener('input',apply);due.addEventListener('change',apply);
 const d=el('details');d.append(el('summary','精确来源版本'),el('pre',JSON.stringify(r.source_reference,null,2)));n.append(d);return n;
}
function render(){const found=filtered();if(offset>=found.length)offset=0;$('cards').replaceChildren(...found.slice(offset,offset+PAGE).map(card));
 $('empty').hidden=found.length!==0;$('count').textContent=`匹配 ${found.length} 条 · 第 ${found.length?Math.floor(offset/PAGE)+1:0} / ${Math.ceil(found.length/PAGE)} 页`;
 $('prev').disabled=offset===0;$('next').disabled=offset+PAGE>=found.length;notice();}
for(const id of ['query','source','status','from','to'])$(id).addEventListener('input',()=>{offset=0;render();});
$('prev').addEventListener('click',()=>{offset=Math.max(0,offset-PAGE);render();});$('next').addEventListener('click',()=>{offset+=PAGE;render();});
$('all').addEventListener('click',()=>{briefOnly=false;offset=0;render();});$('brief-only').addEventListener('click',()=>{briefOnly=true;offset=0;render();});
function save(name,text,type){const url=URL.createObjectURL(new Blob([text],{type}));const a=el('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function validChanges(){const list=[...drafts.values()];if(list.some(c=>!c.reason.trim()))throw Error('每个选择需要理由。');if(list.some(c=>c.review_at&&Date.parse(c.review_at)<=Date.now()))throw Error('新暂缓复核时间必须在未来。');return list;}
$('export-plan').addEventListener('click',()=>{try{const changes=validChanges();const merged=new Map((DATA.saved_reference?[]:E.plan.attention).map(c=>[c.entry_id,c]));for(const c of changes)merged.set(c.entry_id,c);
 const plan={...E.plan,cutoff:new Date().toISOString(),baseline:DATA.saved_reference||E.plan.baseline,attention:[...merged.values()]};save('news-selection-plan.json',JSON.stringify(plan,null,2),'application/json');$('notice').textContent='已导出本地计划；尚未提交或公开。以 desk 操作显式提交，并单独确认反馈公开权利。';}catch(e){$('notice').textContent=e.message;}});
$('export-quick').addEventListener('click',()=>{try{validChanges();const selected=rows.filter(r=>choice(r.entry_id).action==='focus'&&choice(r.entry_id).question.trim());
 const result={schema:'dos2-news-quick-candidates-v1',status:'candidate-drafts-not-research',research_started:false,window_reference:E.plan.window,candidates:selected.map(r=>({entry_id:r.entry_id,question:choice(r.entry_id).question,reason:choice(r.entry_id).reason,source_reference:r.source_reference}))};
 save('news-quick-candidates.json',JSON.stringify(result,null,2),'application/json');}catch(e){$('notice').textContent=e.message;}});
$('download-brief').addEventListener('click',()=>save('news-brief.md',E.brief_markdown,'text/markdown'));
window.addEventListener('beforeunload',e=>{if(drafts.size){e.preventDefault();e.returnValue='';}});render();
"""


def render_page(model):
    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":"))
    for char in ("<", ">", "&", "\u2028", "\u2029"):
        payload = payload.replace(char, "\\u%04x" % ord(char))
    def csp_hash(text):
        return base64.b64encode(hashlib.sha256(text.encode()).digest()).decode()
    policy = ("default-src 'none'; base-uri 'none'; form-action 'none'; connect-src 'none'; "
              "script-src 'sha256-" + csp_hash(JS) + "'; style-src 'sha256-" + csp_hash(CSS) + "'")
    title = "Decision OS · 新闻工作台"
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{escape(policy, quote=True)}">
<title>{title}</title><style>{CSS}</style></head><body>
<header><p>DECISION OS 2.0 / NEWS DESK</p><h1>新闻工作台</h1><p id="window"></p><p id="cutoff"></p></header>
<main><div class="banner" id="health-note"></div><div class="stats"><div class="stat"><strong id="n-all"></strong>可读观察版本</div><div class="stat"><strong id="n-brief"></strong>本期简报采用</div><div class="stat"><strong id="n-fail"></strong>历史失败窗口（非来源数）</div></div>
<section class="panel"><h2>阅读与显式选择</h2><div class="filters">
<label>标题搜索<input id="query" type="search" placeholder="搜索已保管的标题"></label><label>来源<select id="source"><option value="">全部来源</option></select></label>
<label>选择状态<select id="status"><option value="">全部状态</option><option value="reset">未选择</option><option value="focus">继续关注</option><option value="defer">暂缓</option><option value="dismiss">移出</option></select></label>
<label>最后观察自（新加坡时间）<input id="from" type="datetime-local"></label><label>最后观察至（新加坡时间）<input id="to" type="datetime-local"></label></div>
<p id="filter-error" role="alert"></p><div class="bar"><button id="all">全部可读</button><button id="brief-only">仅本期简报</button><button id="export-plan" class="primary">导出选择计划</button><button id="export-quick">导出 Quick 候选</button></div><p id="notice" role="status" aria-live="polite"></p>
<p class="muted">选择只绑定该版本，不屏蔽同一公司或新闻的新版本。变更仅在本页内存，不会联网或自动归档。</p></section>
<div class="pager"><span id="count" role="status"></span><div class="bar"><button id="prev">上一页</button><button id="next">下一页</button></div></div>
<p id="empty" class="banner" hidden>当前筛选没有匹配条目。整窗来源缺口仍在；这不是“市场没有新闻”。</p><section class="grid" id="cards" aria-label="新闻条目"></section>
<section class="panel"><div class="bar"><h2 id="brief-title"></h2><button id="download-brief">导出原简报正文</button></div><p class="muted" id="saved"></p><pre id="brief-body"></pre></section>
<section class="panel"><details><summary>整窗来源健康与历史失败（不随筛选改变）</summary><pre id="health"></pre></details><details><summary>窗口、简报和比较基线的精确引用</summary><pre id="provenance"></pre></details></section>
<noscript><p class="banner">此离线阅读页需要 JavaScript。可直接阅读同包 news-brief.md；没有自动发送或后台任务。</p></noscript>
<footer>来源观察 ≠ 已验证事实 ≠ 研究结论 ≠ 人工决定。该文件是冻结阅读入口，不是已部署的全天候网站。</footer></main>
<script id="data" type="application/json">{payload}</script><script>{JS}</script></body></html>'''
    require(len(html.encode()) <= 8 * 1024 * 1024, "NEWS_DESK_PAGE_BOUND")
    return html
