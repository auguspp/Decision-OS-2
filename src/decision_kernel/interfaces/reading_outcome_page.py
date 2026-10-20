"""Offline outcome presentation; all quoted data uses the Room's literal DOM."""
CSS = r"""
.outcome-row{border-top:1px solid #dce4e8;padding:14px 0}.outcome-table{width:100%;border-collapse:collapse;font-size:14px}.outcome-table th,.outcome-table td{padding:8px;border-bottom:1px solid #dce4e8;text-align:left;vertical-align:top}.outcome-scroll{overflow:auto;max-width:100%}@media(max-width:720px){.outcome-table{min-width:620px}}
"""
JS = r"""
function renderOutcome(){
 const pane=$('outcome-content'),part=D.outcome;if(!pane)return;
 if(!part){pane.append(el('p','未指定原预测或已存状态版本；不会补写历史预测或重新选择结果较好的对象。'));return;}
 const val=x=>x===null||x===undefined?'未知':String(x);
 const terms={'compared-literal-basis':'原文口径已核对，可比较','insufficient-basis':'缺少口径原文，尚不可比较','actual-not-retained':'缺少确切实际材料','not-mature':'原期间尚未结束','not-published-at-cutoff':'截止时尚未披露','actual-unavailable':'实际材料不可用','incomparable':'口径或历史不一致','unavailable':'原件未恢复','observed':'固定交易日窗口可比较','not-mature-in-retained-window':'已存日历尚未覆盖期限','source-needed':'窗口资料待补','original-unqualified':'原范围内未合格','within':'区间内（含端点）','below':'低于下限','above':'高于上限'};
 const term=x=>terms[x]||val(x),readPlan=p=>request('outcome-room-read.json',roomRequest({...D.plan,outcome:p}));
 pane.append(button('导出相同原件的复核读取请求',()=>readPlan(part.plan)),
  el('p','保存原预测、未解问题与全范围缺口。数值差异不直接决定判断正确与否，也不代替人工接受。','banner'));
 if(part.status!=='readable'){pane.append(el('p',`兑现复核${label(part.status)}：${part.error||'原件未恢复'}。未知不会填成零。`));return;}
 const v=part.view;pane.append(el('p',`复核截止 ${v.as_of}`),
  details('确切输入、原声明分母与读取限制',{plan:v.plan,scope:v.economic.scope,limitations:v.limitations,view_sha256:v.view_sha256}),
  button('下载本次兑现复核 JSON',()=>save('outcome-review.json',JSON.stringify(v,null,2),'application/json')),
  el('h3',`经营预测与实际 · 原声明 ${v.economic.scope.declared_forecasts} 项`));
 for(const r of v.economic.rows){const box=el('article',undefined,'outcome-row'),f=r.forecast,a=r.actual,c=r.comparison;
  box.append(el('h3',`${r.forecast_id} · ${term(r.status)}`));
  if(r.reason_code)box.append(el('p',r.reason_code));
  if(f){box.append(el('p',`${f.basis.subject} · ${f.basis.metric} · ${f.basis.period} · ${f.basis.unit}`),
    el('p',`原文声明日期 ${f.issued_on}；本版实际保存 ${f.research_stored_at}。这两个时刻分别保留，原文日期不证明当时已保存或已对外发出预测。`),
    el('p',`原预测 ${f.range?`${f.lower} 至 ${f.upper}（中点 ${f.value}）`:f.value}；实际 ${val(a?.value)}`),
    el('pre',f.original_excerpt.text,'body'),details('原未解问题、所选行与完整口径',f));}
  if(c)box.append(el('p',`实际减预测 ${c.difference} ${c.unit}；绝对差 ${c.absolute_difference}；差额 / 原预测绝对值 ${val(c.relative_to_absolute_forecast)}。${c.relative_denominator_status==='zero-forecast-undefined'?'原预测为零，比例未定义。':''}${c.range_position?'区间位置：'+term(c.range_position)+'。':''}`));
  else box.append(el('p','本项尚未给出兑现差异；缺失、未成熟与口径材料不足分别保留。'));
  if(a){box.append(details('实际原来源、定位、原口径与限制',a),button('导出实际来源读取请求',()=>request('outcome-actual-source.json',{operation:'read-source',payload:a.reference})));}
  box.append(details('本项完整状态与知识边界',r));pane.append(box);
 }
 if(!v.economic.rows.length)pane.append(el('p','本次未声明经营预测选择；这不是全部研究没有预测的结论。'));
 const m=v.market;if(!m)return;
 pane.append(el('h3','原状态事件的后续 T+5 / T+20'),el('p','从原事件之后的完整交易日历数 5 / 20 个 session；缺日不移动目标日。这里是已存价格的机械变动，未经公司行动总回报认证，也不证明实时发现或选股效果。'),
  details('原全范围、失败与固定分母（翻页不改变）',m.scope));
 if(m.status==='unavailable'){pane.append(el('p',`状态原件不可用：${m.reason_code}；原分母未知，未当作零。`));return;}
 pane.append(el('p',`本页 ${m.page.returned} / 原范围 ${m.page.total} 项；原观察截止 ${m.original_observation_as_of}，后续观察截止 ${m.later_observation_as_of}。`));
 const next=button('导出下一页原范围读取请求',()=>readPlan(part.next_plan));next.disabled=!part.next_plan;pane.append(next);
 for(const r of m.rows){const box=el('article',undefined,'outcome-row');box.append(el('h3',`${r.original_state?.name||r.entity_id} · ${r.kind}`));
  const scroll=el('div',undefined,'outcome-scroll'),table=el('table',undefined,'outcome-table'),head=el('tr');
  for(const t of ['期限','原事件日','固定目标日','状态','价格变化 %','原缺日 / 原因'])head.append(el('th',t));const th=el('thead');th.append(head);table.append(th);const body=el('tbody');
  for(const h of r.horizons){const tr=el('tr');for(const text of [`T+${h.horizon_sessions}`,val(h.anchor_date),val(h.target_date),term(h.status),val(h.return_pct),h.missing_sessions?.join(', ')||h.reason_code||'无'])tr.append(el('td',text));body.append(tr);}table.append(body);scroll.append(table);
  box.append(scroll,details('原事件、原状态 / 缺口与两侧价格引用',r));pane.append(box);
 }
}
renderOutcome();
"""
