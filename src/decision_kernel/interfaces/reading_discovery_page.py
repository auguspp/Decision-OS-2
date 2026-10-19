"""Human-readable state, changes and original evidence; literal DOM only."""
CSS = r"""
.discovery-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.discovery-scope{padding:12px;background:#edf4f3;border-radius:5px}.discovery-event{padding:12px 0;border-top:1px solid #dce4e8}@media(max-width:900px){.discovery-grid{grid-template-columns:1fr}}
"""
JS = r"""
function renderDiscovery(){
 const pane=$('discovery-content'),part=D.discovery;
 if(!part){pane.append(el('p','未指定状态发现快照。行业、概念与独立个股分别读取；缺少一类资料不表示其他方向没有变化。'));return;}
 const names={sector:'行业',concept:'概念',stock:'独立个股'},terms={'persistent':'持续','accelerating':'加速','weakening':'弱化','exited':'退出','not-entered':'未进入','unknown':'无法判断','not-supplied':'未提供','unavailable':'暂不可用','readable':'可读','baseline':'首次观察基线','entry':'进入','exit':'退出','acceleration':'加速','weakening':'弱化','continuing':'接续观察','unchanged-session':'同一交易日','acceleration-entered':'转为加速','weakened':'转为弱化','baseline-observation':'首次观察基线','entered':'进入','continued':'持续观察','unchanged':'状态未变','rebaseline':'比较基准已改变','not-written':'本次只读计算，未保存','saved-exact-version':'已保存的确切版本'};
 const term=x=>terms[x]||x||'未提供',val=x=>x===null||x===undefined?'未知':String(x);
 const readPlan=p=>request('discovery-room-read.json',roomRequest({...D.plan,discovery:p}));
 pane.append(el('p','状态发现 · 价格路径产生待研究观察。规则、来源日期和原事件分别保留。','banner'),details('本次确切输入与读取计划',part.plan));
 const controls=el('div',undefined,'actions');
 for(const kind of ['sector','concept','stock'])controls.append(button('读取'+names[kind],()=>readPlan({...part.plan,kind,offset:0,entity_id:null})));
 const next=button('读取下一页状态',()=>readPlan(part.next_plan));next.disabled=!part.next_plan;controls.append(next);pane.append(controls);
 if(part.status!=='readable'){pane.append(el('p',`状态读取${label(part.status)}：${part.error||'确切来源尚未通过'}。没有改读另一个旧快照。`,'banner'));return;}
 const v=part.view,grid=el('div',undefined,'discovery-grid');
 for(const kind of ['sector','concept','stock']){const s=v.surfaces[kind],box=el('div',undefined,'discovery-scope');box.append(el('h3',names[kind]),el('p',`${term(s.status)} · 可判断 ${val(s.qualified_count)} / 声明范围 ${val(s.scope_count)}`),el('p',`实际数据截止 ${val(s.as_of)} · 无法判断 ${val(s.unknown_count)}`),details('全范围状态、来源资格和限制',s));grid.append(box);}pane.append(grid);
 pane.append(el('p',`规则：${v.policy}；判断时点：${v.as_of}；${term(v.persistence)}。`),el('p','首次观察基线和历史重算不证明当时系统已实时发出提醒。持续或加速不是盈利预测；缺数据不判断为弱化或退出。'));
 pane.append(el('h3',names[v.kind]+` · 本页 ${v.page.returned} / 查询结果 ${v.page.selected_count}`));
 const query=el('input');query.id='discovery-entity';query.maxLength=128;query.placeholder='精确实体 ID，见状态标题或来源缺口';query.value=part.plan.entity_id||'';
 const ql=el('label','按精确实体读取');ql.htmlFor=query.id;pane.append(ql,query,button('导出实体读取请求',()=>readPlan({...part.plan,entity_id:query.value.trim()||null,offset:0})));
 for(const row of v.states){const box=el('article',undefined,'discovery-event');box.append(el('h3',`${row.name||row.entity_id} · ${row.entity_id}`),el('span',term(row.state),'badge'),el('span',term(row.transition),'badge'),el('p',`资料截止 ${val(row.as_of)}${row.reason_code?' · '+row.reason_code:''}`));
  const m=row.metrics||{};box.append(el('p',`近 5 日涨跌 ${val(m.change_5_pct)}% · 近 20 日涨跌 ${val(m.change_20_pct)}% · 20 日涨幅的 5 日变化 ${val(m.acceleration_5_of_20_pp)} 个百分点`),el('p',`20 日涨幅为正已连续 ${val(m.positive_20_sessions)} 个交易日${m.persistence_left_censored?'（更早起点超出已存窗口）':''}${m.persistence_interrupted_by_gap?'；历史缺口中断持续性核实':''}。`),details('判断数值与完整状态依据',m));
  for(const [field,title] of [['origin_event','原始进入或基线事件'],['last_event','本次接续事件']]){const id=row[field];if(id&&v.events[id]&&!(field==='last_event'&&id===row.origin_event)){const event=v.events[id];box.append(el('h3',title),el('p',`${term(event.type)} · ${val(event.at)}`),details('原事件与确切来源',{event_id:id,event,current_proof:v.proofs[event.current_proof],previous_proof:v.proofs[event.previous_proof]}));}}
  pane.append(box);
 }
 if(!v.states.length)pane.append(el('p','当前查询没有可展示的状态。请同时检查资料缺口；这不代表没有候选或市场安静。'));
 pane.append(details(`资料缺口：展示 ${v.gap_display_count} / 查询缺口 ${v.gap_query_count}`,v.gaps),button('下载本页状态与原事件依据',()=>save('market-discovery-reading.json',JSON.stringify(v,null,2),'application/json')));
 if(part.reference)pane.append(button('导出完整已存快照读取请求',()=>request('market-discovery-artifact.json',{operation:'read-artifact',payload:part.reference})));
}
renderDiscovery();
"""
