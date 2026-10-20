"""Literal global-context presentation on the existing offline reading page."""

CSS = r"""
.global-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.global-family{min-width:0}.global-entry{border-top:1px solid #dce4e8;padding:12px 0}.global-table{width:100%;border-collapse:collapse;font-size:14px}.global-table th,.global-table td{text-align:left;vertical-align:top;padding:7px;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}.global-table-wrap{overflow-x:auto}@media(max-width:1000px){.global-columns{grid-template-columns:1fr}}
"""

JS = r"""
function renderGlobal(){
 const pane=$('global-content'),context=D.global_context;if(!pane||!context)return;
 const names={us_indices:'美股指数',shibor:'Shibor 利率',treasury:'美国国债期限利率',fx:'ECB 外汇参考价',commodities:'黄金与原油期货日线',crypto:'Coinbase 加密资产日线'},ids={us_indices:['DJI','IXIC','RUT','SPX'],shibor:['on','1w','2w','1m','3m','6m','9m','1y'],treasury:['UST:1MONTH','UST:3MONTH','UST:6MONTH','UST:1YEAR','UST:2YEAR','UST:5YEAR','UST:10YEAR','UST:30YEAR'],fx:['EUR/USD','EUR/CNY','EUR/JPY','EUR/GBP'],commodities:['GC=F','CL=F','BZ=F'],crypto:['BTC-USD','ETH-USD']};
 const publicNames=['treasury','fx','commodities','crypto'];
 const val=x=>x===null||x===undefined?'未取得':String(x);
 const term=x=>({'observed':'所需观察可读','partial-history':'部分窗口可读','no-observation-at-date':'截止日观察缺失','no-usable-window':'没有可用窗口','missing-market-sessions':'窗口内交易日缺失','insufficient-calendar-history':'交易日历不足','checked':'来源表示已校验','checked-empty':'完整返回为空','rejected':'来源未通过','not-supplied':'未提供','restored':'原查询已恢复','partial-restoration':'原查询部分恢复','missing-value':'该格缺值','invalid-value':'该格无效','date-not-returned':'指定日期未返回','missing-quote':'所需利率缺失','invalid-quote':'所需利率无效','arithmetic-unavailable':'无法精确计算'}[x]||label(x)||'未知');
 const table=(head,rows)=>{const wrap=el('div',undefined,'global-table-wrap'),t=el('table',undefined,'global-table'),h=el('tr');for(const title of head)h.append(el('th',title));t.append(h);for(const values of rows){const row=el('tr');for(const value of values)row.append(el('td',val(value)));t.append(row);}wrap.append(t);return wrap;};
 pane.append(el('p','分别读取本次指定的全球背景保管根。每族的源日期、单位和完整范围独立保留；未提供的族不会自动取数或替换。','banner'),
  el('p','原件可恢复不等于当前最新、持续更新或六类真实来源已经齐备。给定原件及摘要也不认证来源请求、公开权利或投资资格。'),
  button('下载完整全球背景模型 JSON',()=>save('global-context-model.json',JSON.stringify(context,null,2),'application/json')),
  details('本次精确根、覆盖与读取边界',{plan:context.plan,coverage:context.coverage,freshness:context.freshness,status:context.status,error:context.error}));
 if(context.status==='not-included'){pane.append(el('p',`全球背景未展示：${context.error}。原根仍保留，可明确选择单个族后重新读取。`));return;}
 const columns=el('div',undefined,'global-columns');
 for(const name of Object.keys(names)){
  const part=context.families[name],box=el('section',undefined,'global-family');box.append(el('h3',names[name]));
  if(!part){box.append(el('p','本次未提供此族的已保管根，不推断数据为空或已完成更新。'));columns.append(box);continue;}
  const readPlan=p=>request(`global-${name}-room.json`,roomRequest({...D.plan,global_context:{...context.plan,[name]:p}}));
  const controls=el('div',undefined,'actions');
  controls.append(button(`导出${names[name]}原根读取请求`,()=>request(`global-${name}-root.json`,part.open_request)),
   button(`导出${names[name]}第一页请求`,()=>readPlan({...part.plan,offset:0,entity_id:null})));
  const next=button(`导出${names[name]}下一页请求`,()=>readPlan(part.next_plan));next.disabled=!part.next_plan;controls.append(next);box.append(controls);
  const select=el('select');select.id=`global-${name}-entity`;const all=el('option','完整声明范围');all.value='';select.append(all);
  const full=part.view?.view,choices=full?(publicNames.includes(name)?full.declared_entities:name==='us_indices'?full.declared_indices:full.declared_tenors):ids[name];
  for(const id of choices){const option=el('option',id);option.value=id;select.append(option);}select.value=part.plan.entity_id||'';
  const caption=el('label',name==='us_indices'?'精确指数（不按表现排名）':name==='shibor'?'精确期限（不推断其他利率）':'精确来源序列（保留完整范围缺口）');caption.htmlFor=select.id;
  box.append(caption,select,button(`导出${names[name]}筛选请求`,()=>readPlan({...part.plan,offset:0,entity_id:select.value||null})),
   details('原根及本次导航',part.plan));
  if(part.status!=='readable'){box.append(el('p',`${names[name]}${label(part.status)}：${part.error}。未换用其他根或发起取数。`,'banner'));columns.append(box);continue;}
  const restored=part.view,v=restored.view;
  box.append(el('p',`保管恢复：${term(restored.status)}；完整来源 ${restored.query_count}，未恢复 ${restored.unavailable_queries}。观察缺口另列。`),
   button(`下载${names[name]}完整原读取结果 JSON`,()=>save(`global-${name}-reader.json`,JSON.stringify(restored,null,2),'application/json')),
   details('查询原引用、失败与保管限制',{sources:restored.sources,reading_error:restored.reading_error,qualification:restored.qualification,original_http_restored:restored.original_http_restored}));
  for(const source of restored.sources)box.append(button(`导出${names[name]}来源 ${source.path} 读取请求`,()=>request(`global-${name}-query.json`,{operation:'read-source',payload:source.reference})));
  if(!v){box.append(el('p','本族共享输入未恢复，原观察未知；没有用零值或另一日期代替。'));columns.append(box);continue;}
  const page=part.page;
  box.append(el('p',`源区间 ${v.start} → ${v.as_of} · 完整声明 ${v.scope_count} 项 · 当前筛选 ${page.matched_count} 项 · 本页 ${page.returned} 项。`, 'muted'),
   el('p',part.partial?'完整范围仍有来源或观察缺口；当前筛选页不能消除这些缺口。':'完整声明范围的所需观察可读；不认证当前最新或全球覆盖。'),
   details('完整范围、版本及分页',{scope_count:v.scope_count,view_version:v.view_version,declared_indices:v.declared_indices,declared_tenors:v.declared_tenors,declared_entities:v.declared_entities,available_values:v.available_values,missing_values:v.missing_values,source_qualification_counts:v.source_qualification_counts,observed_count:v.observed_count,end_quote_counts:v.end_quote_counts,comparison_counts:v.comparison_counts,received_cell_counts:v.received_cell_counts,pagination:{...page,entries:undefined},qualification_limits:v.qualification_limits}));
  if(!page.entries.length)box.append(el('p','当前导航页没有项目，完整声明范围和缺口仍保留。'));
  if(publicNames.includes(name)){
   const explanation={treasury:'美国国债 par yield 为年百分率（ANNUAL_PERCENT）；两次实际返回日期的差为 bp，不是债券价格回报。',fx:'ECB 信息参考价以 EUR 为基币，单位分别为 USD/CNY/JPY/GBP_PER_EUR；不是交易报价。',commodities:'GC=F、CL=F、BZ=F 是供应商期货日线；单位分别为美元/金衡盎司、美元/桶。现货、结算价及换月连续性未认证，不计算区间收益。',crypto:'BTC-USD 与 ETH-USD 仅代表 Coinbase 场所的完成 UTC 日桶，按精确请求产品识别；不补缺失日桶，不代表全市场。'};
   box.append(el('p',explanation[name]),el('p',`原捕获 ${v.captured_from} → ${v.captured_through}；日期合同 ${v.capture_version}。完整范围有值 ${v.available_values}/${v.scope_count}，缺值 ${v.missing_values}。`),
    details('原捕获日期资格与来源状态',{date_policy:v.date_policy,execution_complete:v.execution_complete,capture_sha256:v.capture_sha256,outcomes:v.outcomes,source_qualification:v.source_qualification,vintage:v.vintage,authority:v.authority}));
   for(const entry of page.entries){const article=el('article',undefined,'global-entry');article.append(el('h3',entry.name),
     table(['精确序列','源日期','数值','单位','上一实际返回日期','上一数值','变化','变化单位'],[[entry.entity_id,entry.source_date,entry.value,entry.unit,entry.previous_date,entry.previous_value,entry.change,entry.change_unit]]),
     el('p',entry.value===null?'该序列最新返回格缺值或原件未恢复；未回填上一非空值。':'数值仅归属于所列源日期；不推断市场开闭或原发布时间。'),details('原序列、日期与单位限制',entry));box.append(article);}
  }else if(name==='us_indices'){
   box.append(el('p','单位：指数点位（index-points），币种不适用。原 US 市场日期与交易日窗口保持；点位比率不是美元价格或认证总回报。'));
   for(const entry of page.entries){const article=el('article',undefined,'global-entry'),obs=entry.observation;article.append(el('h3',entry.entity_id),el('p',`来源：${term(entry.source_qualification.status)}；${obs?term(obs.status):'原观察未取得'}。`));
    if(obs)article.append(table(['交易日间隔','基准日 → 截止日','点位：基准 → 截止','窗口状态','点位变动 %'],obs.horizons.map(w=>[w.sessions,`${val(w.baseline_date)} → ${val(w.end_date)}`,`${val(w.baseline_level)} → ${val(w.end_level)}`,term(w.status),val(w.change_pct)])));
    article.append(details('原窗口、缺日与查询沿革',entry));box.append(article);
   }
  }else{
   box.append(el('p','CNY Shibor · 年百分率（%）与基点差（bp）；1 个百分点 = 100 bp。按明确源日期比较，不转换成价格收益或美股交易日窗口。'));
   for(const entry of page.entries){const article=el('article',undefined,'global-entry');article.append(el('h3',entry.entity_id),el('p',`${entry.end_quote.date} 年百分率：${val(entry.end_quote.rate_pct)} % · ${term(entry.end_quote.status)}`),
     table(['明确日期对','基准年百分率 %','状态','百分点差','基点差 bp'],entry.comparisons.map(c=>[`${c.baseline.date} → ${c.end_date}`,val(c.baseline.rate_pct),term(c.status),val(c.change_pp),val(c.change_bp)])),details('原期限观察与全接收范围缺格',entry));box.append(article);}
   box.append(el('h3','同日期限差'),table(['源日期','期限对','状态','百分点差','基点差 bp'],v.curve_spreads.map(s=>[s.date,`${s.long_tenor} − ${s.short_tenor}`,term(s.status),val(s.spread_pp),val(s.spread_bp)])),
    details('原接收日期和来源沿革',{source_timezone:v.source_timezone,received_dates:v.received_dates,source_qualification:v.source_qualification,source_lineage:v.source_lineage}));
  }
  columns.append(box);
 }
 pane.append(columns,el('p','下载保留原读取模型，分页和筛选仅导出新读取请求；页面不自动执行、保存或采用进研究／Brief。'));
}
renderGlobal();
"""
