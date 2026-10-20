"""Literal retained condition results in the existing research agenda panel."""

JS = r"""
function watchConditions(parent,card){
 const rows=card.watch_evidence||[];
 if(!rows.length)return;
 const states={'first-trigger':'本次首次满足','continued-trigger':'仍满足','not-met':'未满足','cannot-determine':'资料不足，无法判断','review-required':'需要复核','ended':'检查已结束','not-due':'尚未到检查时点','not-enabled':'未启用'};
 const metrics={price:'价格',research_version:'研究版本',evidence:'经营证据',value_to_price_distance:'价值与价格距离',undiscounted_holding_period_return:'名义累计回报',expected_holding_period_return:'概率加权累计回报',positive_return_scenario_probability:'正收益情景概率'};
 parent.append(el('h3','已存条件检查'));
 for(const row of rows){
  const box=el('div',undefined,'question'),r=row.result;
  box.append(el('p',`${row.watch_id||'指定条件'} · ${label(row.status)}${row.recorded_at?' · 原检查时点 '+row.recorded_at:''}`));
  if(row.selection?.question_id)box.append(el('p',`对应原问题：${row.selection.question_id}`,'muted'));
  if(r){
   box.append(el('p',`组合状态：${states[r.status]||r.status}${r.combination==='all'?'（全部条件）':r.combination==='any'?'（任一条件）':''}`));
   const conditions=r.condition_results||[{condition_id:'原单条件',status:r.status,value:r.observation?.value,reason:r.observation?.reason}];
   for(const c of conditions){
    const title=c.condition?.condition_text||c.condition_id;
    box.append(el('p',`${title}：${states[c.status]||c.status}${c.value!==null&&c.value!==undefined?' · 原值 '+c.value:''}`));
    if(c.evidence?.reason)box.append(el('p',c.evidence.reason));
    if(c.reason)box.append(el('small',c.reason));
    if(c.trigger_continuity==='intervening-checks-unknown')box.append(el('p','两次明确结果之间有未观察时段，不能确认期间一直成立。','muted'));
   }
   for(const b of r.next_boundaries||[]){
    const v=edge=>edge?edge.value:'没有已声明边界';
    box.append(el('p',`${metrics[b.metric]||b.metric}${b.world_id?' / '+b.world_id:''}：当前 ${b.value??'未知'}；向下 ${v(b.below)}；向上 ${v(b.above)}${b.at?'；正处于边界 '+b.at.value:''}`));
   }
   box.append(el('p','经营条件保留提交者的判读与已读来源；程序重演不代表经济判断获得独立确认。','muted'));
  }else if(row.activity)box.append(el('p',`活动状态：${row.activity.state}；此事件没有执行新的条件检查。`));
  else box.append(el('p',row.error||'未取得该检查的可读结果。'));
  if(row.open_request)box.append(button('导出原条件检查读取请求',()=>request('watch-check-read.json',row.open_request)));
  box.append(details('原条件、证据引用、区间端点与检查范围',row));
  parent.append(box);
 }
}
"""
