"""Literal close structure in the original offline room; no submission code."""
CSS = r"""
.structure-ranges{border-collapse:collapse;width:100%;margin:12px 0}.structure-ranges th,.structure-ranges td{padding:8px;text-align:left;border-bottom:1px solid #dce4e8}.structure-row{padding:16px 0;border-top:1px solid #dce4e8}.structure-table{overflow-x:auto}
"""
JS = r"""
function renderCloseStructure(){
 const part=D.structure;if(!part)return;const pane=$('structure-content');
 const names={sector:'行业',concept:'概念',stock:'独立个股'},terms={'above-range':'高于原区间','below-range':'低于原区间','at-high':'在原区间高点','at-low':'在原区间低点','at-flat-range':'与原平坦区间相等','inside-range':'在原区间内','observed':'窗口完整','partial-history':'部分窗口可读','no-usable-observation':'没有可用窗口','insufficient-calendar-history':'日历历史不足','missing-market-sessions':'缺少指定交易日收盘','source-unavailable':'来源资格未通过','eligible':'来源资格通过','unavailable':'来源暂不可用','baseline':'未指定前版','same-session':'同一交易日原版本比较','consecutive-session':'相邻交易日可比较','historical-prices-changed':'重叠历史价格已重述','calendar-changed-or-missing':'交易日历改变或缺失','contract-changed':'实体范围或价格口径改变','date-reversed':'数据日期倒退','unobserved-session-gap':'中间存在未观察交易日','unconfirmed-no-right-close':'尚无右侧收盘，未确认','no-strict-candidate':'末端相等，无严格候选','high':'局部高点','low':'局部低点'};
 const term=x=>terms[x]||x||'未知',val=x=>x===null||x===undefined?'未知':String(x);
 const read=p=>request('close-structure-room.json',roomRequest({...D.plan,structure:p}));
 pane.append(el('p','前 N 个已完成交易日的收盘区间，以及严格三点转折；转折须右侧相邻交易日收盘后才能确认。','banner'),details('确切来源和有限读取计划',part.plan));
 if(part.status!=='readable'){pane.append(el('p',`价格结构${label(part.status)}：${part.error||'原输入未通过校验'}。`));return;}
 const v=part.view,s=v.scope;
 pane.append(el('p',`${names[v.kind]} · 原目录 ${val(s.scope_count)} 项；来源合格 ${val(s.source_eligible_count)} 项；全部窗口完整 ${val(s.complete_count)} 项；仍有缺口 ${val(s.incomplete_count)} 项。`),el('p',`来源状态 ${term(s.status)}${s.reason_code?' · '+s.reason_code:''}${s.source_hold?' · source hold':''}。数据截止 ${val(s.as_of)} · 读取时点 ${v.as_of}。计数先覆盖整个原目录，再做筛选与分页，不认证今天的市场已被覆盖。`),details('全范围分母、来源 hold、窗口和版本比较缺口',s));
 if(v.previous)pane.append(details('指定前版范围及离开当前范围的实体',v.previous));
 const query=el('input');query.id='structure-entity';query.maxLength=128;query.value=part.plan.entity_id||'';query.placeholder='精确实体 ID；留空读取原目录';
 const ql=el('label','按精确实体继续读取');ql.htmlFor=query.id;
 const next=button('导出下一页读取请求',()=>read(part.next_plan));next.disabled=!part.next_plan;
 pane.append(ql,query,button('导出实体读取请求',()=>read({...part.plan,entity_id:query.value.trim()||null,offset:0})),next,el('p',`本页 ${v.page.returned} / 查询范围 ${val(v.page.selected_count)}；全范围状态不随当前页变化。`));
 for(const row of v.rows){
  const box=el('article',undefined,'structure-row');box.append(el('h3',`${row.name} · ${row.entity_id}`),el('span',term(row.status),'badge'),el('p',`来源：${term(row.source_qualification.status)}${row.source_qualification.reason_code?' · '+row.source_qualification.reason_code:''}；前版：${term(row.history.status)}。`));
  const o=row.observation;
  if(o){
   const wrap=el('div',undefined,'structure-table'),table=el('table',undefined,'structure-ranges'),head=el('tr');
   for(const text of ['前期交易日','原收盘区间','最后收盘','相对区间位置 / 缺口'])head.append(el('th',text));table.append(head);
   for(const r of o.ranges){const tr=el('tr');for(const text of [r.sessions, r.status==='observed'?`${r.prior_low} — ${r.prior_high}`:'未知',val(r.end_close),r.status==='observed'?term(r.position):term(r.status)+(r.missing_dates?'：'+r.missing_dates.join('、'):'')])tr.append(el('td',String(text)));table.append(tr);}wrap.append(table);box.append(wrap);
   const t=o.turns;box.append(el('p',`三点窗口：${t.observed_triples} / ${t.possible_triples} 组完整；缺口 ${t.missing_triples} 组；已取得 ${t.available_calendar_sessions} / 请求 ${t.requested_sessions} 个交易日。`));
   for(const turn of t.confirmed)box.append(el('p',`${term(turn.kind)}：发生于 ${turn.turn_session}，右侧确认于 ${turn.confirmed_session}；收盘 ${turn.turn_close}。`));
   if(!t.confirmed.length)box.append(el('p','本窗口没有已确认的严格转折；请同时检查日历长度与价格缺口。'));
   box.append(el('p',`末端 ${t.candidate.turn_session}：${term(t.candidate.status)}${t.candidate.kind?' · '+term(t.candidate.kind):''}。`),details('原区间、日期和三点价格',o));
  }
  box.append(details('确切来源资格与历史比较', {source_reference:row.source_reference,source_qualification:row.source_qualification,source_provenance:row.source_provenance,history:row.history}));
  if(row.source_reference)box.append(button('导出原价格来源读取请求',()=>request('close-structure-price-source.json',{operation:'read-source',payload:row.source_reference})));
  pane.append(box);
 }
 if(!v.rows.length)pane.append(el('p','本页没有实体行；原范围是否可恢复及其缺口以上方分母和来源状态为准。'));
 pane.append(el('p','历史重算不证明当时系统已观察或提醒；收盘局部高低点不代表走势反转、盈利判断或交易指令。本页未处理集合竞价、分钟 K 或缠论结构。'),button('下载本页价格结构',()=>save('close-structure-reading.json',JSON.stringify(v,null,2),'application/json')),button('导出原范围来源读取请求',()=>request('close-structure-root.json',part.open_request)));
}
renderCloseStructure();
"""
