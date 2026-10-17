"""Literal, offline presentation of the original industry reader result."""

CSS = r"""
.industry-scroll{max-width:100%;overflow:auto}.industry-table{width:100%;border-collapse:collapse;font-size:14px}.industry-table th,.industry-table td{padding:8px;border-bottom:1px solid #dce4e8;text-align:left;vertical-align:top;overflow-wrap:anywhere}.industry-controls{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.industry-scope{font-size:14px}.industry-series{border-top:1px solid #dce4e8;padding-top:12px;margin-top:16px}@media(max-width:720px){.industry-controls{grid-template-columns:1fr}.industry-table{min-width:520px}}
"""

JS = r"""
function renderIndustry(){
 const pane=$('industry-content'),part=D.industry;
 if(!part){pane.append(el('p','未指定产业阅读包；不会按公司名自动匹配来源、研究或简报。'));return;}
 pane.append(button('导出原产业读取请求',()=>request('industry-read.json',part.open_request)));
 if(part.status!=='readable'){
  pane.append(el('p',`产业包${label(part.status)}：${part.error}。未自动替换来源；无法判断不等于没有变化或公司联系。`,'banner'));return;
 }
 const v=part.view,ip=part.plan,retained=v.retained||v,mode=ip.view;
 const val=x=>x===null||x===undefined?'未知':String(x);
 const subjectName=s=>`${s.name||s.entity_id} (${s.entity_id})`;
 const terms={'read':'可读','changed':'观察变化','unchanged':'同值','unavailable':'不可用或不可比','historical':'历史适用','within-declared-period':'在声明适用期内','not-revalidated':'未重新核验','producer':'生产 / 销售端','purchaser':'采购端','processor':'加工端','transporter':'运输端','substitute':'替代品角色','inventory':'库存角色','price-level':'价格水平','index-level':'指数水平','stock':'期末余额','period-flow':'本期流量','ytd-flow':'年初至今累计','monthly-release':'月报','preliminary-scan':'初步扫描','revised-release':'修订发布','period-average':'期间均值'};
 const term=x=>terms[x]||val(x);
 const caption={industry:'产业与商品','operating-cycle':'工业经营周期','vehicle-flows':'汽车产批零出口'}[mode];
 pane.append(el('p',`${caption} · 包内截止 ${v.as_of} · ${v.status==='partial'?'部分恢复，缺项仍在':'原读取器可读'}`,'banner'),
  el('p','这是指定已存版本，不自动宣称最新。观察变化不是公司净受益；累计、余额、同比与单月不能互换。历史角色不是当前合同，未知不是零。'),
  el('p',retained.coverage_note||'范围见原读取结果。'),el('p',`本包用途：${retained.use||'见原结果'}；准备时刻：${v.prepared_at||retained.prepared_at||'未提供'}。`),
  details('整包范围、原查询及永久引用（本地筛选不改分母）',{scope:v.scope,retained_scope:retained.scope,query:v.query,reference:v.reference,view_version:v.view_version||null}),
  button('下载完整读取结果 JSON',()=>save('industry-reader-result.json',JSON.stringify(v,null,2),'application/json')));
 const diagnostics={relation_outcomes:retained.relation_outcomes,unclassified_series:v.unclassified_series,
  unclassified_relations:v.unclassified_relations,coverage:v.coverage,limitations:v.limitations,
  source_authenticity:retained.source_authenticity,qualification:retained.qualification,
  source_requests:v.source_requests,writes:v.writes,company_benefit:v.company_benefit};
 const failures=(retained.relation_outcomes||[]).filter(x=>x.status==='unavailable');
 pane.append(el('p',`整包未恢复输入：${val(retained.scope?.unavailable_inputs)}；关系失败：${failures.length}。这些缺口不随筛选隐藏。`,'industry-scope'));
 const ds=details('原范围缺口、拒绝原因与证明边界',diagnostics);ds.open=v.status==='partial';pane.append(ds);
 let series=[];
 if(mode==='industry')series=v.series.map(s=>({...s,points:s.observations}));
 else if(mode==='vehicle-flows')series=v.entries;
 else for(const entry of v.entries)for(const [metric,m] of Object.entries(entry.metrics))series.push({...m,metric,subject:entry.subject});
 const relations=mode==='operating-cycle'?[]:v.relations;
 const controls=el('div',undefined,'industry-controls');
 function select(id,text,options){const wrap=el('div'),l=el('label',text),s=el('select');l.htmlFor=id;s.id=id;s.append(el('option','全部'));
  s.firstChild.value='';for(const [value,text] of options){const o=el('option',text);o.value=value;s.append(o);}wrap.append(l,s);controls.append(wrap);return s;}
 const subjects=[...new Map(series.map(s=>[s.subject.entity_id,subjectName(s.subject)])).entries()];
 const companies=[...new Map(relations.map(r=>[r.company.entity_id,subjectName(r.company)])).entries()];
 const subject=select('industry-subject','本包返回的主体或商品',subjects),company=select('industry-company','有证据的公司角色',companies);
 company.disabled=mode==='operating-cycle';pane.append(controls);
 if(company.disabled)pane.append(el('p','经营周期视图不支持公司映射；宏观总量不能自动变成个股暴露。'));
 const summary=el('p'),body=el('div'),relationBody=el('div'),calculationBody=el('div');
 pane.append(summary,button('导出当前筛选的原读取请求',()=>request('industry-query.json',{operation:'industry-read',payload:{...ip,
  subject_id:subject.value||(company.value?null:ip.subject_id),company_id:company.value||(subject.value?null:ip.company_id)}})),body,relationBody,calculationBody);
 function sourceButton(ref){return button('导出此来源读取请求',()=>request('industry-source.json',{operation:'read-source',payload:{reference:ref}}));}
 function showSeries(s){
  const box=el('div',undefined,'industry-series');box.append(el('h3',`${subjectName(s.subject)} · ${s.metric||s.series_id}`));
  if(s.channel_meaning)box.append(el('p',s.channel_meaning));
  const scroll=el('div',undefined,'industry-scroll'),table=el('table',undefined,'industry-table'),thead=el('thead'),head=el('tr');
  for(const name of ['期间','原数值','单位','期间口径 / 发布阶段','读取状态'])head.append(el('th',name));thead.append(head);table.append(thead);
  const tbody=el('tbody');
  for(const p of s.points){const tr=el('tr');for(const text of [p.period_start&&p.period_end?`${p.period_start} — ${p.period_end}`:'期间未恢复',val(p.value),val(p.unit),`${term(p.period_kind)} / ${term(p.assessment)}`,p.reason_code||term(p.status)])tr.append(el('td',text));tbody.append(tr);}
  table.append(tbody);scroll.append(table);box.append(scroll);
  for(const c of s.changes||[])box.append(el('p',`${term(c.status)} · 变化量 ${val(c.delta)} · 百分比变化 ${val(c.percent_change)}${c.reason_code?' · '+c.reason_code:''}`));
  box.append(details('原比较规则、单位与全部转换限制',s.changes));
  const sources=el('details');sources.append(el('summary','规格、地区、来源日期、精确行与限制'));
  for(const p of s.points){sources.append(details('原观察及来源表示',p));if(p.reference)sources.append(sourceButton(p.reference));}box.append(sources);return box;
 }
 function render(){
  let shownRelations=relations,ids=null;
  if(company.value){shownRelations=relations.filter(r=>r.company.entity_id===company.value);ids=new Set(shownRelations.map(r=>r.subject_id));}
  else if(subject.value){shownRelations=relations.filter(r=>r.subject_id===subject.value);ids=new Set([subject.value]);}
  const shown=series.filter(s=>!ids||ids.has(s.subject.entity_id));
  summary.textContent=`本地显示 ${shown.length} / 本次读取返回 ${series.length} 个系列；${shownRelations.length} / 返回 ${relations.length} 条角色。整包范围见上方，不按筛选重算。`;
  body.replaceChildren(...shown.map(showSeries));relationBody.replaceChildren();calculationBody.replaceChildren();
  if(!shown.length)body.append(el('p','当前返回范围没有匹配系列；不代表市场没有变化或公司没有暴露。'));
  if(mode!=='operating-cycle')relationBody.append(el('h3','公司角色、适用期与未知暴露'));
  for(const r of shownRelations){const box=el('div',undefined,'question');
   box.append(el('h3',`${subjectName(r.company)} · ${term(r.role)} · ${r.subject_id}`),
    el('p',`适用 ${r.applicable_start} — ${r.applicable_end}；包内截止状态 ${term(r.applicability_at_cutoff)}；当前经营 ${term(r.current_business_freshness)}`),
    el('p',`业务：${r.business_scope}；报告期：${r.report_period}`),
    el('p',`业务份额 ${val(r.business_share)}；弹性 ${val(r.elasticity)}；传导率 ${val(r.pass_through)}；时滞 ${val(r.lag)}；净受益 ${val(r.net_benefit)}`),
    details('发行人依据、原文限制、传导假说及反证',r),sourceButton(r.evidence.reference));relationBody.append(box);
  }
  if(mode!=='operating-cycle'&&!shownRelations.length)relationBody.append(el('p','当前返回范围没有合格角色；不能推定没有业务关系或零暴露。'));
  if(mode==='operating-cycle')for(const entry of v.entries.filter(e=>!ids||ids.has(e.subject.entity_id))){
   const box=el('div',undefined,'question');box.append(el('h3',`${subjectName(entry.subject)} · 原读取器派生指标`),
    details('未提供的指标（不是零）',entry.not_supplied_metrics));
   for(const snapshot of entry.snapshots){box.append(el('p',`原期间 ${snapshot.period_ends.join(' / ')}`));
    for(const [name,c] of Object.entries(snapshot.calculations))box.append(el('p',`${name}：${val(c.value)} ${val(c.unit)} · ${c.reason_code||c.status}`));
    box.append(details('原计算及操作数精确引用、未执行的研究问题',snapshot));}calculationBody.append(box);
  }
 }
 subject.addEventListener('change',()=>{company.value='';render();});company.addEventListener('change',()=>{subject.value='';render();});render();
}
renderIndustry();
"""
