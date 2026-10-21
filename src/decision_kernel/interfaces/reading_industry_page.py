"""Literal, offline presentation of the original industry reader result."""

CSS = r"""
.industry-scroll{max-width:100%;overflow:auto}.industry-table{width:100%;border-collapse:collapse;font-size:14px}.industry-table th,.industry-table td{padding:8px;border-bottom:1px solid #dce4e8;text-align:left;vertical-align:top;overflow-wrap:anywhere}.industry-controls{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.industry-scope{font-size:14px}.industry-series{border-top:1px solid #dce4e8;padding-top:12px;margin-top:16px}@media(max-width:720px){.industry-controls{grid-template-columns:1fr}.industry-table{min-width:520px}}
.industry-table button{min-width:10em}
.industry-use{border:1px solid #dce4e8;border-radius:10px;padding:16px;margin-top:20px}.industry-use-blocked{border-left:5px solid #a66b14}.industry-use-missing{background:#fff8e9;padding:12px;border-radius:6px}
"""

JS = r"""
function renderIndustryUses(pane,part){
 const v=part.view,ip=part.plan,val=x=>x===null||x===undefined?'未知':String(x);
 const name=s=>`${s.name||s.entity_id} (${s.entity_id})`;
 const terms={'read':'原输入可读','partial':'部分恢复','source-blocked':'来源资格受阻','unavailable':'不可用或不可比','producer':'生产 / 销售端','purchaser':'采购端','historical':'历史适用','not-revalidated':'未重新核验'};
 const term=x=>terms[x]||val(x);
 const sourceButton=ref=>button('导出此来源读取请求',()=>request('industry-use-source.json',{operation:'read-source',payload:ref}));
 pane.append(el('h3','六项产业用途与未补齐证据'),
  el('p',`包内截止 ${v.as_of} · 准备 ${v.prepared_at} · ${v.scope.read_uses} 项原输入可读，${v.scope.partial_uses} 项部分恢复，${v.scope.source_blocked_uses} 项来源资格受阻`,'banner'),
  el('p',v.coverage_note),
  el('p','每项固定保留原证据、缺项和来源受阻条件。可读是保存版本的读取结果；观察变化不代表公司净受益。历史角色、报告期与当前合同分别保留，未知不是零。'),
  el('p','组合用途没有 Brief 采用签名；即使同页提供简报，也不据此推断研究采用、Brief 采用或人工接受。'),
  details('整包范围、原输入范围及失败（筛选不会缩小这些范围）',{scope:v.scope,input_outcomes:v.input_outcomes,query:v.query,reference:v.reference,view_version:v.view_version,limitations:v.limitations,source_requests:v.source_requests,writes:v.writes}),
  button('下载完整六项用途读取结果 JSON',()=>save('industry-uses-result.json',JSON.stringify(v,null,2),'application/json')));
 const allSeries=v.uses.flatMap(u=>u.series),allRoles=v.uses.flatMap(u=>u.relations);
 const controls=el('div',undefined,'industry-controls');
 function select(id,title,options){const wrap=el('div'),l=el('label',title),s=el('select');l.htmlFor=id;s.id=id;s.append(el('option','全部'));s.firstChild.value='';
  for(const [value,text] of options){const o=el('option',text);o.value=value;s.append(o);}wrap.append(l,s);controls.append(wrap);return s;}
 const subject=select('industry-use-subject','本次返回的商品 / 主体',[...new Map(allSeries.map(s=>[s.subject.entity_id,name(s.subject)])).entries()]);
 const company=select('industry-use-company','有原证据的公司角色',[...new Map(allRoles.map(r=>[r.company.entity_id,name(r.company)])).entries()]);
 const summary=el('p'),body=el('div');pane.append(controls,summary,
  button('导出当前筛选的原读取请求',()=>request('industry-uses-query.json',{operation:'industry-read',payload:{...ip,
   subject_id:subject.value||(company.value?null:ip.subject_id),company_id:company.value||(subject.value?null:ip.company_id)}})),body);
 function seriesCard(s){const card=el('div',undefined,'industry-series');card.append(el('h4',`${name(s.subject)} · ${s.metric||s.series_id}`));
  if(s.channel_meaning)card.append(el('p',s.channel_meaning));
  const points=s.observations||s.points||[],scroll=el('div',undefined,'industry-scroll'),table=el('table',undefined,'industry-table'),thead=el('thead'),head=el('tr');
  for(const title of ['原期间','原数值','单位','口径 / 发布阶段','读取状态'])head.append(el('th',title));thead.append(head);table.append(thead);const tbody=el('tbody');
  for(const p of points){const row=el('tr');for(const text of [p.period_start&&p.period_end?`${p.period_start} — ${p.period_end}`:'期间未恢复',val(p.value),val(p.unit),`${val(p.period_kind)} / ${val(p.assessment)}`,p.reason_code||term(p.status)])row.append(el('td',text));tbody.append(row);}
  table.append(tbody);scroll.append(table);card.append(scroll);
  for(const c of s.changes||[])card.append(el('p',`${term(c.status)} · 变化量 ${val(c.delta)} · 百分比变化 ${val(c.percent_change)}${c.reason_code?' · '+c.reason_code:''}`));
  card.append(details('原读取器比较与选择身份',{input_id:s.input_id,input_view:s.input_view,packet_reference:s.packet_reference,series_id:s.series_id,changes:s.changes}));
  const sources=el('details');sources.append(el('summary','原规格、地区、来源日期及精确行'));
  for(const p of points){sources.append(details('原观察及读取限制',p));if(p.reference)sources.append(sourceButton(p.reference));}card.append(sources);return card;
 }
 function render(){body.replaceChildren();let shownCount=0;
  for(const use of v.uses){let roles=use.relations,series=use.series;
   if(company.value){roles=roles.filter(r=>r.company.entity_id===company.value);const keys=new Set(roles.map(r=>JSON.stringify([r.input_id,r.subject_id])));series=series.filter(s=>keys.has(JSON.stringify([s.input_id,s.subject.entity_id])));}
   else if(subject.value){roles=roles.filter(r=>r.subject_id===subject.value);series=series.filter(s=>s.subject.entity_id===subject.value);}
   shownCount+=series.length;
   const blocked=use.disposition==='source-blocked',card=el('section',undefined,`industry-use${blocked?' industry-use-blocked':''}`);
   card.append(el('h3',`${use.title} · ${term(use.status)}`),el('p',use.question),el('p',use.scope_note));
   const missing=el('div',undefined,'industry-use-missing');missing.append(el('h4','尚缺证据与用途限制'));const list=el('ul');for(const text of use.missing)list.append(el('li',text));missing.append(list);
   if(use.reopen_when)missing.append(el('p',`恢复条件：${use.reopen_when}`));card.append(missing);
   for(const e of use.evidence){card.append(el('p',`保存的来源资格证据：${term(e.status)}${e.reason_code?' · '+e.reason_code:''}`),el('p',e.note),details('精确引文、保管证据与验证边界',e),sourceButton(e.reference));}
   for(const s of series)card.append(seriesCard(s));
   if(!series.length)card.append(el('p',blocked?'本项未提供可读价格，不以其他代际、规格或产品价格补位。':'本次查询没有匹配系列；原整包范围、未恢复选择和缺项仍保留。这不等于市场没有观察或公司零暴露。'));
   for(const r of roles){const role=el('div',undefined,'question');role.append(el('h4',`${name(r.company)} · ${term(r.role)} · ${r.subject_id}`),
    el('p',`报告期 ${r.report_period}；适用 ${r.applicable_start} — ${r.applicable_end}；${term(r.applicability_at_cutoff)}；当前经营 ${term(r.current_business_freshness)}`),
    el('p',r.business_scope),el('p',`业务份额 ${val(r.business_share)}；弹性 ${val(r.elasticity)}；传导率 ${val(r.pass_through)}；时滞 ${val(r.lag)}；净受益 ${val(r.net_benefit)}`),
    details('原公司证据、收入 / 成本 / 利润 / 现金流假说及反证',r),sourceButton(r.evidence.reference));card.append(role);}
   card.append(details('本用途声明、选择结果和原整包范围',{use_id:use.use_id,family:use.family,disposition:use.disposition,scope:use.scope,selections:use.selections,selection_outcomes:use.selection_outcomes,input_outcomes:v.input_outcomes.filter(i=>use.selections.some(s=>s.input_id===i.input_id))}));body.append(card);
  }
  summary.textContent=`固定显示 ${v.uses.length} 项用途；本地显示 ${shownCount} / 本次返回 ${allSeries.length} 个系列。来源受阻、缺项及整包失败不随查询隐藏。`;
 }
 subject.addEventListener('change',()=>{company.value='';render();});company.addEventListener('change',()=>{subject.value='';render();});render();
}
function renderIndustryGoods(pane,part){
 const v=part.view,ip=part.plan,val=x=>x===null||x===undefined?'未知':String(x);
 const terms={'fresh-qualified':'本批合格且在时效内','qualified-not-fresh':'本批合格，当前已过期','qualified-freshness-unknown':'本批合格，时效未知','unresolved':'资格待核','excluded':'明确排除','source-blocked':'来源受阻'};
 const sourceButton=ref=>button('导出此来源读取请求',()=>request('industry-goods-source.json',{operation:'read-source',payload:ref}));
 pane.append(el('h3','独立商品资格'),el('p',`评估时刻 ${v.evaluation_at} · 原包截止 ${v.as_of}`,'banner'),
  el('p',`本批 ${v.scope.candidate_records} 条候选记录，${v.scope.comparable_series} 个可比系列，${v.scope.distinct_qualified_goods} 个去重合格商品；确认 ${v.scope.fresh_goods} 个在规则时效内，${v.scope.freshness_unknown_goods} 个时效未知。`),
  el('p','全范围商品数仍未知，500 目标尚未成立。名称、地区、系列、统计通道、修订和重读不增加商品；资料保管与权利声明不认证第三方权利。'),
  el('p',v.coverage_note),
  details('整批资格集合、全部处置与原输入范围（筛选不会缩小分母）',{scope:v.scope,sets:v.sets,outcomes:v.outcomes,input_outcomes:v.input_outcomes,source_qualifications:v.source_qualifications,quantity_milestone:v.quantity_milestone,reference:v.reference,view_version:v.view_version,limitations:v.limitations}),
  button('下载本次商品资格结果 JSON',()=>save('industry-goods-result.json',JSON.stringify(v,null,2),'application/json')));
 const controls=el('div',undefined,'industry-controls'),wrap=el('div'),search=el('input'),searchLabel=el('label','在本次返回范围搜索商品、系列或缺项');
 search.id='industry-goods-search';search.type='search';searchLabel.htmlFor=search.id;wrap.append(searchLabel,search);controls.append(wrap);
 function select(id,title,options){const box=el('div'),label=el('label',title),s=el('select');label.htmlFor=id;s.id=id;s.append(el('option','全部'));s.firstChild.value='';
  for(const [value,title] of options){const o=el('option',title);o.value=value;s.append(o);}box.append(label,s);controls.append(box);return s;}
 const subject=select('industry-goods-subject','原主体 / 归并商品',[...new Map(v.entries.filter(e=>e.subject_id).map(e=>[e.commodity_id||e.subject_id,e.title])).entries()]);
 const companies=v.entries.flatMap(e=>e.relations),company=select('industry-goods-company','有原证据的公司角色',[...new Map(companies.map(r=>[r.company.entity_id,r.company.name])).entries()]);
 const state=select('industry-goods-status','资格处置',Object.entries(terms)),summary=el('p'),body=el('div');
 pane.append(controls,summary,button('导出当前筛选的原商品读取请求',()=>request('industry-goods-query.json',{operation:'industry-read',payload:{...ip,
  subject_id:subject.value||(company.value?null:ip.subject_id),company_id:company.value||(subject.value?null:ip.company_id)}})),
  el('p','搜索和资格处置筛选只改变本地显示；导出的读取请求保留原评估时刻及主体 / 公司查询。'),body);
 function card(e){const box=el('section',undefined,'industry-use');box.append(el('h3',`${e.title} · ${terms[e.status]||e.status}`),
  el('p',`原主体 ${val(e.subject_id)} · 归并商品 ${val(e.commodity_id)}`),
  details('资格处置、未决项和重开条件',{candidate_id:e.candidate_id,disposition:e.disposition,identity_resolution:e.identity_resolution,reasons:e.reasons,missing:e.missing,reopen_when:e.reopen_when}));
  for(const proof of e.identity_evidence){box.append(details('原身份说明与核验边界',proof),sourceButton(proof.reference));}
  for(const s of e.selections){box.append(el('h4',s.series_id),details('本次资格比较、原输入与时效',{input_id:s.input_id,packet_reference:s.packet_reference,series_key:s.series_key,comparison:s.comparison,reasons:s.reasons,freshness:s.freshness,source_qualification:s.source_qualification}));
   if(!s.series){box.append(el('p','原系列未恢复；不会从目录补值。'));continue;}
   const scroll=el('div',undefined,'industry-scroll'),table=el('table',undefined,'industry-table'),head=el('tr');
   for(const title of ['原期间','原数值','单位','读取状态'])head.append(el('th',title));table.append(head);
   for(const p of s.series.observations){const row=el('tr');for(const text of [p.period_start&&p.period_end?`${p.period_start} — ${p.period_end}`:'期间未恢复',val(p.value),val(p.unit),p.reason_code||p.status])row.append(el('td',text));table.append(row);}
   scroll.append(table);box.append(scroll,details('原系列比较（资格计算不覆盖原结果）',s.series.changes));
   for(const p of s.series.observations){box.append(details('原观察、口径与来源限制',p));if(p.reference)box.append(sourceButton(p.reference));}
  }
  for(const role of e.relations){box.append(details('原公司角色与历史适用期（不推导净暴露）',role),sourceButton(role.evidence.reference));}
  return box;
 }
 function render(){const needle=search.value.trim().toLocaleLowerCase();let shown=v.entries;
  if(subject.value)shown=shown.filter(e=>[e.subject_id,e.commodity_id].includes(subject.value));
  if(company.value)shown=shown.map(e=>{const relations=e.relations.filter(r=>r.company.entity_id===company.value),inputs=new Set(relations.map(r=>r.input_id));
   return {...e,relations,selections:e.selections.filter(s=>inputs.has(s.input_id))};}).filter(e=>e.relations.length);
  if(state.value)shown=shown.filter(e=>e.status===state.value);
  if(needle)shown=shown.filter(e=>JSON.stringify({title:e.title,subject_id:e.subject_id,commodity_id:e.commodity_id,series:e.selections.map(s=>s.series_id),reasons:e.reasons,missing:e.missing}).toLocaleLowerCase().includes(needle));
  summary.textContent=`本地显示 ${shown.length} / 本次返回 ${v.entries.length} 条候选；整批 ${v.scope.candidate_records} 条候选的资格和失败仍见上方。`;
  body.replaceChildren(...shown.map(card));if(!shown.length)body.append(el('p','当前返回范围没有匹配；全范围覆盖和公司暴露不能据此记零。'));
 }
 for(const proof of v.source_qualifications.flatMap(s=>s.evidence)){pane.append(details('保存的来源资格说明（不是新的公开输出授权）',proof),sourceButton(proof.reference));}
 search.addEventListener('input',render);state.addEventListener('change',render);
 subject.addEventListener('change',()=>{company.value='';render();});company.addEventListener('change',()=>{subject.value='';render();});render();
}
function renderIndustryGoodsCoverage(pane,part){
 const v=part.view,ip=part.plan,s=v.scope,val=x=>x===null||x===undefined?'未知':String(x);
 const terms={'fresh-qualified':'合格且在时效内','qualified-not-fresh':'合格，已过期','qualified-freshness-unknown':'合格，时效未知','unresolved':'资格待核','excluded':'明确排除','source-blocked':'来源受阻'};
 const batches=new Map(v.batches.map(b=>[b.batch_id,b]));
 pane.append(el('h3','商品覆盖与跨批去重'),el('p',`共同评估时刻 ${v.evaluation_at} · 范围截止 ${v.as_of}`,'banner'),
  el('p',`固定 ${s.declared_batches} 批，已恢复 ${s.read_batches} 批；${s.candidate_records} 条候选，${s.comparable_series} 个可比系列，去重后 ${s.distinct_qualified_goods} 个合格商品，其中 ${s.fresh_goods} 个在规则时效内、${s.freshness_unknown_goods} 个时效未知。`),
  el('p',v.quantity_milestone.status==='established-in-declared-scope'?'本次固定范围已达到 500 个合格且在时效内的不同商品；来源的新期间、持续运行和人工验收仍须分别核实。':'本次固定范围尚未满足 500 个合格且在时效内的不同商品。'),
  el('p',v.coverage_note),el('p','各批原数值和来源证据仍从原批次打开。地区、规格、转载与重复批次不增加商品；新整理和重读不会刷新原观察。'),
  details('完整范围、集合、冲突与失败（筛选不改变计数）',{scope:v.scope,sets:v.sets,outcomes:v.outcomes,batches:v.batches,aliases:v.aliases,conflicts:v.conflicts,quantity_milestone:v.quantity_milestone,reference:v.reference,view_version:v.view_version,limitations:v.limitations}),
  button('下载完整商品覆盖结果 JSON',()=>save('industry-goods-coverage-result.json',JSON.stringify(v,null,2),'application/json')));
 for(const batch of v.batches){const box=el('div',undefined,'industry-use');
  box.append(el('h4',batch.batch_id),details('原批次范围与读取状态',batch));
  box.append(button('导出本批价格与证据读取请求',()=>request('industry-goods-batch.json',{operation:'industry-read',payload:{reference:batch.reference,view:'industry-goods',evaluation_at:v.evaluation_at}})));pane.append(box);}
 const controls=el('div',undefined,'industry-controls'),search=el('input'),label=el('label','在本次返回范围搜索商品或缺项');
 search.id='industry-coverage-search';search.type='search';label.htmlFor=search.id;const wrap=el('div');wrap.append(label,search);controls.append(wrap);
 const state=el('select'),stateLabel=el('label','资格处置');state.id='industry-coverage-state';stateLabel.htmlFor=state.id;
 const all=el('option','全部');all.value='';state.append(all);for(const [value,title] of Object.entries(terms)){const option=el('option',title);option.value=value;state.append(option);}
 const stateWrap=el('div');stateWrap.append(stateLabel,state);controls.append(stateWrap);const summary=el('p'),body=el('div');
 pane.append(controls,summary,button('导出原范围读取请求',()=>request('industry-goods-coverage-query.json',{operation:'industry-read',payload:{...ip}})),body);
 function render(){const needle=search.value.trim().toLocaleLowerCase();let shown=v.entries;
  if(state.value)shown=shown.filter(e=>e.status===state.value);
  if(needle)shown=shown.filter(e=>JSON.stringify(e).toLocaleLowerCase().includes(needle));
  summary.textContent=`本地显示 ${shown.length} / 本次返回 ${v.entries.length} 条；完整声明范围和失败仍保留在上方。`;
  body.replaceChildren();const scroll=el('div',undefined,'industry-scroll'),table=el('table',undefined,'industry-table'),head=el('tr');
  for(const text of ['商品','原批次','归并身份','资格','缺项与原因','原始价格与证据'])head.append(el('th',text));table.append(head);
  for(const e of shown){const row=el('tr');for(const text of [e.title,e.batch_id,val(e.canonical_id),terms[e.status]||e.status,[...(e.reasons||[]),...(e.missing||[])].join('；')])row.append(el('td',text));
   const cell=el('td'),batch=batches.get(e.batch_id);if(batch)cell.append(button('导出本批读取请求',()=>request('industry-goods-batch.json',{operation:'industry-read',payload:{reference:batch.reference,view:'industry-goods',evaluation_at:v.evaluation_at}})));row.append(cell);table.append(row);}
  scroll.append(table);body.append(scroll);if(!shown.length)body.append(el('p','本次返回范围没有匹配；这不表示全市场没有商品，也不表示公司零暴露。'));
 }
 search.addEventListener('input',render);state.addEventListener('change',render);render();
}
function renderIndustry(){
 const pane=$('industry-content'),part=D.industry;
 if(!part){pane.append(el('p','未指定产业阅读包；不会按公司名自动匹配来源、研究或简报。'));return;}
 pane.append(button('导出原产业读取请求',()=>request('industry-read.json',part.open_request)));
 if(part.status!=='readable'){
  pane.append(el('p',`产业包${label(part.status)}：${part.error}。未自动替换来源；无法判断不等于没有变化或公司联系。`,'banner'));return;
 }
 const v=part.view,ip=part.plan,retained=v.retained||v,mode=ip.view;
 if(mode==='industry-uses'){renderIndustryUses(pane,part);return;}
 if(mode==='industry-goods'){renderIndustryGoods(pane,part);return;}
 if(mode==='industry-goods-coverage'){renderIndustryGoodsCoverage(pane,part);return;}
 const adoption=part.brief_adoption,adopted=new Map((adoption?.rows||[]).map(r=>[r.series_id,r]));
 const adoptionTerms={'included':'指定简报已采用此选择','considered-not-included':'列入范围但本期未采用','not-considered-in-this-brief':'不在指定简报产业范围','industry-not-tracked-in-this-brief':'该简报未跟踪产业','brief-capacity':'本期容量省略（不是人工否决）','previously-included-same-selection':'前版已采用同一选择，本期省略','unreadable-or-outside-cutoff':'交付时不可读、超限或晚于截止','same-displayed-series-and-roles':'本页系列与可见角色和交付时一致','differs-from-delivery':'本页内容与交付时不同，不能套用原采用结论','unknown':'无法核对，不等于未采用'};
 const adoptionLabel=x=>adoptionTerms[x]||x;
 pane.append(el('p',adoption?.status==='related'?'下方逐系列关联指定已存简报；不代表研究采用、人工接受或当前净受益。':adoption?.status==='not-supplied'?'未指定已存简报；产业阅读不推断简报采用。':'简报采用关系未知或未展示；未读、权限及容量不当作未采用。','industry-adoption-notice'));
 if(adoption?.status==='related')pane.append(button('查看指定已存简报',()=>{location.hash='brief';}),details('采用核对范围与原正文摘要',{reference:adoption.brief_reference,brief_body_sha256:adoption.brief_body_sha256,scope:adoption.scope}));

 const val=x=>x===null||x===undefined?'未知':String(x);
 const subjectName=s=>`${s.name||s.entity_id} (${s.entity_id})`;
 const terms={'read':'可读','changed':'观察变化','unchanged':'同值','unavailable':'不可用或不可比','historical':'历史适用','within-declared-period':'在声明适用期内','not-revalidated':'未重新核验','producer':'生产 / 销售端','purchaser':'采购端','processor':'加工端','transporter':'运输端','substitute':'替代品角色','inventory':'库存角色','price-level':'价格水平','index-level':'指数水平','stock':'期末余额','period-flow':'本期流量','ytd-flow':'年初至今累计','monthly-release':'月报','preliminary-scan':'初步扫描','revised-release':'修订发布','period-average':'期间均值'};
 const term=x=>terms[x]||val(x);
 const caption={industry:'产业与商品','operating-cycle':'工业经营周期','vehicle-flows':'汽车产批零出口','livestock-weekly':'畜禽周度采集价'}[mode];
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
 else if(mode==='vehicle-flows'||mode==='livestock-weekly')series=v.entries;
 else for(const entry of v.entries)for(const [metric,m] of Object.entries(entry.metrics))series.push({...m,metric,subject:entry.subject});
 if(mode==='livestock-weekly')pane.append(el('p','采集日价格不等于全周均价；只展示原畜禽读取器的公布比较、舍入核对和实际间隔天数。非相邻采集日不冒充七天环比。'));
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
 function sourceButton(ref){return button('导出此来源读取请求',()=>request('industry-source.json',{operation:'read-source',payload:ref}));}
 function showSeries(s){
  const box=el('div',undefined,'industry-series');box.append(el('h3',`${subjectName(s.subject)} · ${s.metric||s.series_id}`));
  const ar=adopted.get(s.series_id);
  if(ar){const text=[adoptionLabel(ar.status),ar.omission_reason?adoptionLabel(ar.omission_reason):ar.classification,ar.displayed_content?adoptionLabel(ar.displayed_content.status):null].filter(Boolean).join(' · ');
   box.append(el('p',text,'industry-adoption'),details('确切选择、采用与当前显示核对（不代签经济质量）',ar));
  }
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
