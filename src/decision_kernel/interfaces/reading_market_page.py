"""Literal A-share dual-view presentation in the existing offline reading room."""

CSS = r"""
.market-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.market-entry{border-top:1px solid #dce4e8;padding:12px 0}.market-table{width:100%;border-collapse:collapse;font-size:14px}.market-table th,.market-table td{text-align:left;vertical-align:top;padding:7px;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}@media(max-width:900px){.market-columns{grid-template-columns:1fr}}
"""

JS = r"""
function renderMarket(){
 const pane=$('market-content'),part=D.market;
 if(!part){pane.append(el('p','未指定已保管的 A 股市场根。计数检查回执不是行情源，不能据此生成收益；未提供不等于市场没有变化。'));return;}
 const term=x=>({'observed':'所声明窗口可读','data-gaps':'存在数据缺口','partial-history':'历史不完整','not-supplied':'未提供','invalid-data':'数据未通过','restored':'所需来源表示已恢复','partial-restoration':'部分来源表示未恢复','benchmark-not-provided':'未提供基准','empty-declared-universe':'声明范围为空'}[x]||x||'未知');
 const val=x=>x===null||x===undefined?'未取得':String(x);
 const readPlan=p=>request('market-room-read.json',roomRequest({...D.plan,market:p}));
 pane.append(el('p','A 股原价 / 复权 · 指定已存版本，不自动换到最新。只读，不启动采集、研究或保存。','banner'),
  button('导出原市场根读取请求',()=>request('market-root.json',part.open_request)),details('确切市场根与本次分页',part.plan));
 const controls=el('div',undefined,'actions');
 controls.append(button('导出此根第一页请求',()=>readPlan({...part.plan,offset:0,entity_id:null})));
 const next=button('导出此根下一页请求',()=>readPlan(part.next_plan));next.disabled=!part.next_plan;controls.append(next);
 pane.append(controls);
 const query=el('input');query.id='market-security';query.placeholder='例如 600000.SH，仅导出新读取请求';query.maxLength=9;query.value=part.plan.entity_id||'';
 const queryLabel=el('label','精确证券代码（不是公司名称匹配）');queryLabel.htmlFor=query.id;
 pane.append(queryLabel,query,button('导出指定证券读取请求',()=>{
  const code=query.value.trim();if(code&&!/^[0-9]{6}\.(SH|SZ)$/.test(code)){$('notice').textContent='请输入六位代码及 .SH 或 .SZ；当前页面和存档没有改变。';return;}
  readPlan({...part.plan,offset:0,entity_id:code||null});
 }));
 if(part.status!=='readable'){
  pane.append(el('p',`市场面板${label(part.status)}：${part.error}。原根/来源/权限或容量条件未通过，没有改读其他市场数据。`,'banner'));return;
 }
 const restored=part.view,v=restored.view;
 pane.append(el('p',`保管恢复：${term(restored.status)}；查询 ${restored.query_count}，未恢复 ${restored.unavailable_queries}。这不是行情完整性或实时健康认证。`),
  button('下载本页完整市场读取结果 JSON',()=>save('market-reader-result.json',JSON.stringify(restored,null,2),'application/json')),
  details('查询来源、局部失败和保管限制',{sources:restored.sources,reading_error:restored.reading_error,qualification:restored.qualification,original_http_restored:restored.original_http_restored,execution_diagnostics:restored.execution_diagnostics}));
 if(!v){pane.append(el('p','共享输入未恢复，不能生成双视图。其他研究面板仍可用；无结果不是零收益。'));return;}
 pane.append(el('p',`市场 ${v.raw.market} · 截止 ${v.raw.as_of} · 币种 ${v.raw.currency} · 声明范围 ${v.scope_count} 个证券。证券代码不是永久公司身份。`),
  el('p','原价与复权分别判断。价格比率不是总回报；复权窗口可读不等于全历史因子完整，停复牌解释不填补缺价。没有基准不显示相对收益。'),
  details('完整主表分母、资格、隔离与原解释',{master:v.master,qualification_counts:v.qualification_counts,normalization:v.normalization,qualification_limits:v.qualification_limits}));
 const columns=el('div',undefined,'market-columns');
 for(const kind of ['raw','qfq']){
  const s=v[kind],box=el('section');box.append(el('h3',kind==='raw'?'原价（raw）':'复权（qfq，原锚点口径）'),
   el('p',`${term(s.status)}；本页 ${s.entries.length} / 查询范围 ${s.page.selected_count} / 完整声明 ${s.scope_count}。未读页未计作本页覆盖。`),
   details('完整范围与窗口计数（分页不改分母）',{page:s.page,coverage:s.coverage,scope_count:s.scope_count,received_count:s.received_count,observed_count:s.observed_count,missing_count:s.missing_count,incomplete_count:s.incomplete_count,horizon_counts:s.horizon_counts,calendar_version:s.calendar_version,source_version:s.source_version,universe_version:s.universe_version,discovery_claim:s.discovery_claim}));
  if(!s.entries.length)box.append(el('p','本次查询/页没有返回证券，不代表原声明范围为空或没有机会。'));
  for(const e of s.entries){
   const row=el('article',undefined,'market-entry');row.append(el('h3',`${e.security_reference?.name||'名称未取得'} · ${e.entity_id}`),el('p',`${term(e.status)}；输入：${val(e.input_status)}。`));
   const table=el('table',undefined,'market-table'),head=el('tr');
   for(const title of ['交易日窗','基准日 → 截止日','原状态','变动 %'])head.append(el('th',title));table.append(head);
   for(const w of e.horizons){const tr=el('tr');for(const x of [w.sessions,`${val(w.baseline_date)} → ${val(w.end_date)}`,term(w.status),val(w.change_pct)])tr.append(el('td',val(x)));table.append(tr);}
   row.append(table,details('原证券资格、因子缺口、日历与来源明细',e));box.append(row);
  }columns.append(box);
 }pane.append(columns,el('p','本页 JSON 是原读取器结果，不是供应商原始响应文件。浏览、下载和导出请求均不表示人工接受，也未采用进 Brief。'));
}
renderMarket();
"""
