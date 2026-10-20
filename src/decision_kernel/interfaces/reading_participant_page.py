"""Literal, offline participant facts, coverage and original-reference navigation."""
CSS = r"""
.participant-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.participant-family{background:#eef4f3;border-radius:6px;padding:12px}.participant-row{border-top:1px solid #dce4e8;padding:14px 0}.participant-selectors{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.participant-meta{font-size:14px;overflow-wrap:anywhere}@media(max-width:960px){.participant-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:640px){.participant-grid,.participant-selectors{grid-template-columns:1fr}}
"""
JS = r"""
function renderParticipants(){
 const pane=$('participants-content'),part=D.participants;
 if(!pane)return;
 if(!part){pane.append(el('p','未指定参与者阅读包。八类观察按真实来源范围保留，未提供资料不等于没有行为。'));return;}
 const names={'public-seats':'游资 / 公开席位','institution-seats':'机构席位','northbound':'北向公开事实','holders-funds':'股东 / 基金','visits':'机构调研','estimate-revisions':'研报 / 预测修订','holder-actions':'股东变动 / 回购','locked-capital':'锁定资本 / 发行'};
 const terms={'read':'原输入可读','partial':'部分可用','incomplete':'原查询未完成','not-retained':'未取得保管原件','unavailable':'不可用','comparable':'同口径可比'};
 const term=x=>terms[x]||x||'未知',val=x=>x===null||x===undefined?'未知':String(x);
 const readPlan=p=>request('participant-room-read.json',roomRequest({...D.plan,participants:p}));
 pane.append(el('p','公开参与者 · 保存的事实、报告期与来源标签','banner'),
  button('导出当前参与者读取请求',()=>readPlan(part.plan)),details('确切读取计划',part.plan));
 if(part.status!=='readable'){pane.append(el('p',`参与者读取${label(part.status)}：${part.error||'原件未通过核验'}。缺失保持可见，未重抓来源。`,'banner'));return;}
 const v=part.view,grid=el('div',undefined,'participant-grid');
 for(const f of v.families){const card=el('section',undefined,'participant-family');
  card.append(el('h3',names[f.family]),el('p',`${term(f.status)} · 原范围 ${f.scope.rows} 行 · 当前查询 ${f.query_rows} 行`),el('p',f.scope_note),
   button('读取本面',()=>readPlan({...part.plan,family:f.family,offset:0})),details('缺项与恢复条件',{missing:f.missing,reopen_when:f.reopen_when,scope:f.scope}));grid.append(card);}
 pane.append(grid,el('p',`资料包截止 ${v.as_of} · 准备 ${v.prepared_at}`),el('p',v.coverage_note),
  el('p','供应商标签不认证真实自然人或基金全仓。调研行不是活动数；季度持股不变成每日净买；计划与实施不互换，累计不相加。'),
  details('整包范围、失败、原请求与精确来源（筛选不缩小）',{scope:v.scope,input_outcomes:v.input_outcomes,reference:v.reference,view_version:v.view_version,limitations:v.limitations}));
 const selectors=el('div',undefined,'participant-selectors');
 function selector(id,title,initial){const box=el('div'),label=el('label',title),input=el('input');label.htmlFor=id;input.id=id;input.maxLength=96;input.value=initial||'';box.append(label,input);selectors.append(box);return input;}
 const company=selector('participant-company','公司证券 ID（如 security-002384-sz）',part.plan.company_id),participant=selector('participant-identity','来源参与者 ID（见本页观察）',part.plan.participant_id);
 company.addEventListener('input',()=>{if(company.value)participant.value='';});participant.addEventListener('input',()=>{if(participant.value)company.value='';});
 pane.append(selectors,button('导出公司或参与者查询',()=>readPlan({...part.plan,company_id:company.value.trim()||null,participant_id:participant.value.trim()||null,offset:0})),
  el('h3',`${names[v.family]} · 本页 ${v.page.returned} / 当前查询 ${v.page.query_rows} 行`));
 const previous=button('读取上一页',()=>readPlan({...part.plan,offset:Math.max(0,part.plan.offset-part.plan.limit)})),next=button('读取下一页',()=>readPlan(part.next_plan));
 previous.disabled=part.plan.offset===0;next.disabled=!part.next_plan;pane.append(previous,next);
 const sources=new Map(v.input_outcomes.map(i=>[i.input_id,i]));
 for(const row of v.rows){const box=el('article',undefined,'participant-row'),p=row.participant;
  const participantName=p?p.labels.map(l=>l.value).join(' / '):row.anonymous_participant?'匿名来源标签，不能跨期归并':'原资料未给出具名参与者';
  box.append(el('h3',`${row.ts_code} · ${participantName}`),el('p',`原报告 / 观察日 ${val(row.reported_day)} · 报告期 ${val(row.report_period)} · 披露日 ${val(row.publication_date)}`,'participant-meta'),
   el('p',`原接口 ${row.api} · 原行 ${row.source_row} · 单位资格 ${row.unit_qualification}`,'participant-meta'),
   el('p',row.interpretation),button('查询此公司',()=>readPlan({...part.plan,company_id:row.company_id,participant_id:null,offset:0})),
   details('原数值、阶段、日期及行身份',{raw:row.raw,cells:row.cells,source_dates:row.source_dates,temporal_issues:row.temporal_issues,source_row:row.source_row,row_sha256:row.row_sha256,input_id:row.input_id,page_index:row.page_index}));
  if(p){box.append(el('p',`来源参与者 ID：${p.participant_id}`,'participant-meta'),button('查询此来源标签的公司 / 历史',()=>readPlan({...part.plan,company_id:null,participant_id:p.participant_id,offset:0})));}
  for(const ref of sources.get(row.input_id)?.references||[])box.append(button('导出精确原件读取请求',()=>request('participant-source-read.json',{operation:'read-source',payload:ref})));
  pane.append(box);
 }
 if(!v.rows.length)pane.append(el('p','当前页没有合格观察行。请查看全包缺项、失败与页范围；不据此判断无参与、清仓或市场安静。'));
 pane.append(details('本页来源标签的原观察历史（不相加、不推断清仓）',v.history));
 if(v.family==='estimate-revisions')pane.append(details('同机构标签 / 同公司 / 同目标期的原指标比较',v.estimate_changes));
 pane.append(button('下载本页参与者结果 JSON',()=>save('participant-reading.json',JSON.stringify(v,null,2),'application/json')),
  el('p','读取没有执行研究、简报采用、主动通知或人工投资决定。'));
}
renderParticipants();
"""
