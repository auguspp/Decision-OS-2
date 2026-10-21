"""Literal cross-company Odds views over one verified sequence of Book pages."""

CSS = r"""
.odds-overview-scope{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}.odds-overview-scope p{margin:0;padding:10px;background:#f4f8fa;border:1px solid #dce4e8;border-radius:4px}.odds-overview-filters{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 12px;margin:18px 0}.odds-overview-boundary-filters{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0 12px}.odds-overview-table-wrap{max-width:100%;overflow:auto}.odds-overview-table{width:100%;border-collapse:collapse;font-size:14px;table-layout:fixed}.odds-overview-table th,.odds-overview-table td{padding:12px 10px;text-align:left;vertical-align:top;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}.odds-overview-table th{background:#eef4f6}.odds-overview-table td p{margin:0 0 8px}.odds-overview-table td:nth-child(1){width:20%}.odds-overview-table .actions{gap:6px}.odds-overview-price{margin:8px 0;padding:10px;border-left:3px solid #648b98;background:#f4f8fa;overflow-wrap:anywhere}.odds-overview-price p{margin:3px 0}.odds-overview-price .amount{font-weight:650;font-size:17px}.odds-overview-detail{margin:20px 0;padding:16px;border:1px solid #b8cfd7;border-radius:5px;overflow-wrap:anywhere}.odds-overview-detail:focus{outline:2px solid #2e748e;outline-offset:3px}.odds-overview-result-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.odds-overview-result{border:1px solid #dce4e8;padding:12px;border-radius:4px;min-width:0}.odds-overview-result h5{font-size:15px;margin:0 0 6px}.odds-overview-failures{padding:14px;border:1px solid #dfbc86;background:#fff8ed;margin-top:18px;overflow-wrap:anywhere}.odds-overview-failure{border-top:1px solid #e5cfac;padding:10px 0}.odds-overview-tags{display:flex;flex-wrap:wrap;gap:5px}.odds-overview-tags .badge{margin:0}.odds-overview-original{font-size:13px;color:#516977;overflow-wrap:anywhere}.odds-overview-spec{padding:14px 0;border-top:1px solid #dce4e8}
@media(max-width:900px){.odds-overview-filters{grid-template-columns:repeat(2,minmax(0,1fr))}.odds-overview-boundary-filters{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:720px){.odds-overview-scope,.odds-overview-filters,.odds-overview-boundary-filters,.odds-overview-result-grid{grid-template-columns:1fr}.odds-overview-table thead{display:none}.odds-overview-table,.odds-overview-table tbody,.odds-overview-table tr,.odds-overview-table td{display:block;width:100%}.odds-overview-table tr{border:1px solid #dce4e8;border-radius:5px;margin:0 0 14px}.odds-overview-table td{border:0;padding:9px 12px}.odds-overview-table td:nth-child(1){width:100%}.odds-overview-table td:before{content:attr(data-label);display:block;font-weight:650;margin-bottom:5px;color:#385665}.odds-overview-detail{padding:12px}.odds-overview-table-wrap{overflow:visible}}
"""

DISCOVERY_JS = r"""
function renderOddsBookOverviewDiscovery(){
 const box=$('odds-book-content');if(!box)return;
 box.append(button('导出同快照 Odds 总览请求（最多 4 页）',()=>{
  const plan=researchBookPlan({odds_book_view:{query:{snapshot:D.catalogue.snapshot},page_limit:4,detail:null}});
  delete plan.odds_book;
  request('odds-book-overview.json',roomRequest(plan));
 }));
}
renderOddsBookOverviewDiscovery();
"""

ENTRY_JS = r"""
function oddsOverviewPlan(query,detail=null,changes={}){
 const panel=D.odds_book_view,selectedView=D.plan.odds_book_view||{};
 const current=query||panel?.query||selectedView.query||{};
 const plan=researchBookPlan({...changes,odds_book_view:{query:{...current,snapshot:D.catalogue.snapshot},
  page_limit:panel?.page_limit||selectedView.page_limit||1,detail}});
 delete plan.odds_book;
 return plan;
}
function renderOddsBookOverviewEntry(){
 const box=$('odds-book-content'),panel=D.odds_book_view;if(!box)return;
 if(panel?.view){renderOddsBookOverview(box,panel);return;}
 box.append(el('p','Odds Book 总览本次未显示：'+(panel?.error||'显示容量不足或未包含投影')+'。原研究正文和确切读取入口保留。'));
 box.append(button('导出本次总览读取请求',()=>request('odds-book-overview.json',roomRequest(oddsOverviewPlan()))));
 if(panel?.open_request)box.append(button('导出原单页读取请求',()=>request('odds-book-original-page.json',panel.open_request)));
 if(panel)box.append(details('实际范围与未显示原因',panel));
}
renderOddsBookOverviewEntry();
"""

JS = r"""
function ovText(value){return value===null||value===undefined?'未记录':typeof value==='object'?JSON.stringify(value):String(value);}
function ovLabel(value){return ({current:'快照内当前研究',historical:'历史研究版本','pointer-absent-unknown':'当前研究指针未知',
 'declared-reference':'声明参考价（性质见原注）','relay-observation':'Relay 原历史观察','retained-ftshare-raw-close':'已保管 FTShare 原始收盘',
 'retained-relay-raw-close':'已保管 Relay 原始收盘','not-established':'未建立','stored-declarations-only':'仅原保存声明',
 'not-certified':'未认证','not-requested':'未请求',verified:'已核保管',full:'Full',quick:'Quick',
 'first-trigger':'原检查首次满足','continued-trigger':'原检查继续满足','not-met':'原检查未满足','cannot-determine':'原检查无法判断',
 'review-required':'原检查要求复核','not-due':'原检查尚未到期',ended:'原检查已结束',active:'active',paused:'paused',retired:'retired',
 'not-found-in-complete-scan-of-known-layouts':'完整扫描未找到已存 Odds 结果',
 'not-observed-in-this-read':'本次读取范围未见 Odds 结果',
 'present-in-this-read-not-current-certification':'本次已读到保存结果，未认证当前适用性'})[value]||ovText(value);}
function ovRows(view){return (view.pages||[]).flatMap(page=>page.items||[]);}
function ovGroupRows(rows,group){return rows.filter(row=>row.status==='readable'&&row.research_key===group.research_key);}
function ovResults(rows){return rows.filter(row=>row.recorded?.price);}
function ovChecks(rows){return rows.map(row=>({row,check:row.recorded?.recorded_result})).filter(item=>item.check&&typeof item.check==='object');}
function ovStates(rows){return [...new Set([...ovChecks(rows).flatMap(({check})=>[check.status,...(check.condition_results||[]).map(item=>item.status)]),
 ...rows.map(row=>row.recorded?.stored_state)].filter(Boolean))];}
function ovBoundaryRows(rows){
 const result=[];
 for(const {row,check} of ovChecks(rows))for(const boundary of check.next_boundaries||[]){
  let found=false;
  for(const side of ['below','at','above'])if(boundary[side]){
   found=true;result.push({row,check,boundary,side,edge:boundary[side]});
  }
  if(!found)result.push({row,check,boundary,side:'unknown',edge:null});
 }
 return result;
}
function ovSide(side){return ({below:'向下',at:'处于边界',above:'向上',unknown:'无法判断'})[side]||side;}
function ovOriginal(box,operation,payload,title){
 if(operation&&payload)box.append(button(title||'导出原件读取请求',()=>request('odds-book-'+operation+'.json',{operation,payload})));
}
function ovDetailRequest(box,row){
 if(row.status!=='readable'||!row.open_operation||!row.open_payload)return;
 box.append(button('导出此对象的总览详情请求',()=>request('odds-book-detail.json',
  roomRequest(oddsOverviewPlan(undefined,{operation:row.open_operation,payload:row.open_payload})))));
}
function ovPrice(box,result,compact=false){
 const price=result.price;if(!price)return;
 const area=el('div',undefined,'odds-overview-price');
 area.append(el('p',ovText(price.price)+' '+ovText(price.currency),'amount'),
  el('p','价格性质：'+ovLabel(price.kind)+' · 原字段 '+ovText(price.kind)),
  el('p','原说明：'+ovText(price.note)),
  el('p','原价格时点：'+ovText(price.as_of)),
  el('p','原评估时点：'+ovText(result.calculation_input?.evaluated_at)),
  el('p','原市场资格：'+ovLabel(result.market_qualification)+' · 原字段 '+ovText(result.market_qualification)),
  el('p','股数资格：'+ovText(price.share_count_qualification||result.price_check?.share_count_qualification||result.price_check?.currency_share_class_and_capital)),
  el('p','本次仅恢复原记录；当前新价及今日适用性未认证。','odds-overview-original'));
 if(!compact)area.append(el('p','证券 / 股类：'+ovText(price.security?.code)+' / '+ovText(price.security?.share_class)),
  el('p','价格时钟含义：'+ovText(price.timestamp_semantics)),
  details('原价格、市场政策与资格全文',{price,market_qualification:result.market_qualification,market_policy:result.market_policy,market:result.market,price_check:result.price_check}));
 box.append(area);
}
function ovBoundaryText(item){
 const b=item.boundary,e=item.edge;
 return '原指标 '+ovText(b.metric)+' / 情景 '+ovText(b.world_id)+' · '+ovSide(item.side)+
  ' · 原当前值 '+ovText(b.value)+' · 原边界 '+ovText(e?.value)+' · 原距离 '+ovText(e?.distance)+
  ' · 原检查 '+ovText(item.check.request?.checked_at)+' · '+ovText(b.status);
}
function ovResearchActions(box,research){
 if(!research?.reference)return;
 const ref=research.reference,loaded=cards.find(card=>same(card.reference,ref));
 if(loaded)box.append(button('打开本页确切研究与反证',()=>{
  selected=cardKey(loaded);$('query').value='';$('kind').value='';render();location.hash='research-detail';
 }));
 else box.append(button('导出确切研究与反证阅读请求',()=>request('odds-book-research-context.json',
  roomRequest(oddsOverviewPlan(undefined,null,{reference:ref})))));
 box.append(button('导出此研究的总览请求',()=>{
  const old=D.odds_book_view?.query||D.plan.odds_book_view.query;
  request('odds-book-research-overview.json',roomRequest(oddsOverviewPlan({
   limit:old.limit,scan_limit:old.scan_limit,research_ref:ref,snapshot:D.catalogue.snapshot})));
 }));
 box.append(button('导出本版原材料阅读请求',()=>request('odds-book-materials.json',
  roomRequest(oddsOverviewPlan(undefined,null,{reference:ref,materials:{reference:ref,source_limit:4,history_limit:2}})))));
 const agendaCard=D.agenda.view?.cards?.find(card=>same(card.reference,ref));
 if(agendaCard)box.append(button('查看本版已读议程',()=>{location.hash='agenda';}));
 if(D.brief.status==='readable'&&D.brief.reference)box.append(button('查看本页具名 Brief',()=>{location.hash='brief';}),
  button('导出具名 Brief 原读取请求',()=>request('odds-book-brief.json',{operation:'read-desk',plan:{reference:D.brief.reference},save:false})));
}
function ovResult(box,row){
 const r=row.recorded;box.append(el('h5',r.schema==='probability-odds-v1'?'原概率 Odds 结果':'原条件 Odds 结果'));
 ovPrice(box,r);
 box.append(el('p','原估值日期 '+ovText(r.valuation_date)+'；原目标日期 '+ovText(r.target_date)),
  el('p','原概率状态：'+ovText(r.probability_status)));
 for(const world of r.world_results||[]){
  box.append(el('p','原情景 '+ovText(world.world_id||world.id)+' · '+ovText(world.world_name)));
  if(world.value_to_price_distance!==undefined)box.append(el('p','原价值 / 价格距离：'+ovText(world.value_to_price_distance)+'（不是未来收益）'));
  if(world.undiscounted_holding_period_return!==undefined)box.append(el('p','原未折现持有期回报：'+ovText(world.undiscounted_holding_period_return)+'（未新增年化）'));
 }
 box.append(el('p',r.schema==='probability-odds-v1'?'使用原研究声明的概率与政策；本页未核验经验概率。':'条件结果未建立概率时，不生成等权概率或加权结论。'),
  details('原情景、聚合结果与限制',{world_results:r.world_results,aggregate:r.aggregate,weighted_aggregate:r.weighted_aggregate,
   participation_zone:r.participation_zone,limitations:r.limitations}));
 ovDetailRequest(box,row);ovOriginal(box,row.open_operation,row.open_payload);
 box.append(details('结果确切原引用',row.reference));
}
function ovRenderGroupDetail(box,group,rows){
 box.replaceChildren();if(!group){box.append(el('p','本次筛选没有匹配研究；已扫描范围、未读范围和坏项仍见总览。'));return;}
 const research=group.research,owned=ovGroupRows(rows,group),checks=ovChecks(owned);
 box.append(el('h3',research.case_id+' / '+research.revision_id+' · '+ovLabel(research.kind)),
  el('p','原委托：'+ovText(research.mandate)),
  el('p','研究状态：'+ovLabel(research.at_snapshot?.status)+'；本版保存于 '+ovText(research.stored_at)+'（保存时间不是事实截止）。'),
  el('p','已存结果关系：'+ovLabel(group.saved_odds)),
  el('p','本次总览未执行研究或批量重算；价格变化与原研究证据分别保留。','banner'));
 const actions=el('div',undefined,'actions');ovResearchActions(actions,research);
 actions.append(button('返回 Odds 总表',()=>{location.hash='odds-overview-table';}));box.append(actions);
 box.append(el('h4','原未决项与反证接续'));
 for(const item of research.recorded_unresolved||[])box.append(el('p',ovText(item)));
 if(!(research.recorded_unresolved||[]).length)box.append(el('p','本研究未记录未决项；这不认证研究充分性或不存在反证。'));
 const book=bookEntry(research.reference),agendaCard=D.agenda.view?.cards?.find(card=>same(card.reference,research.reference));
 box.append(el('p','本版已读议程关系：'+(agendaCard?ovText(agendaCard.status):'本页未建立；不补造选择或复核日期。')),
  el('p','本页具名 Brief 对本版的采用关系：'+ovText(book?.brief_relation?.status||'unknown')+'；页面同现不等于正文采用。'));
 for(const specGroup of group.spec_groups||[]){
  const area=el('section',undefined,'odds-overview-spec'),members=owned.filter(row=>row.spec_key===specGroup.spec_key);
  const specRow=members.find(row=>row.recorded?.schema==='valuation-spec-v1'),spec=specRow?.recorded;
  area.append(el('h4','同一原规格的已存结果'));
  if(spec)area.append(el('p',ovText(spec.security?.code)+' / '+ovText(spec.security?.share_class)+' · '+ovText(spec.currency)+
   ' · 原股数 '+ovText(spec.share_count)),el('p','原价值类型 '+ovText(spec.value_kind)+'；方法 '+ovText(spec.method)),
   el('p','估值日 '+ovText(spec.valuation_date)+'；目标日 '+ovText(spec.target_date)+'；资本结构日 '+ovText(spec.capital_structure?.as_of)),
   el('p','原资本结构假设：'+ovText(spec.capital_structure?.assumption)),el('p','原现金口径：'+ovText(spec.cash_policy)));
  else area.append(el('p','本次未读到该规格的独立卡片；关联来自已核原引用，不补造规格字段。'));
  area.append(el('p','下列结果共用确切 spec_key；按原存储顺序并列，不选择“最新价格”，不重估世界、股数、概率或期限。'));
  if(specRow)ovDetailRequest(area,specRow);
  for(const ref of specGroup.original_references||[])ovOriginal(area,'read-valuation',{reference:ref},'导出此规格原读取请求');
  const grid=el('div',undefined,'odds-overview-result-grid'),results=ovResults(members);
  for(const row of results){const result=el('article',undefined,'odds-overview-result');ovResult(result,row);grid.append(result);}
  if(!results.length)grid.append(el('p','本次读取范围没有该规格的可读结果；仍按总览完整性与缺项解释。'));
  area.append(grid,details('原规格关系与精确引用',specGroup));box.append(area);
 }
 if(!(group.spec_groups||[]).length)box.append(el('p','本次范围没有此研究的可关联估值规格。'+ovLabel(group.saved_odds)));
 box.append(el('h4','Watch 与已记录复核'));
 for(const row of owned.filter(item=>item.recorded?.watch_id||item.watch_ref||item.activity_selection)){
  const r=row.recorded,area=el('div',undefined,'question');
  area.append(el('p','原对象类型 '+row.kind+'；原活动状态 '+ovText(r.stored_state)+'；原事件时钟 '+ovText(r.at)),
   el('p','原活动选择 '+ovText(row.activity_selection?.status)+'；历史验证 '+ovText(r.history_validation||row.activity_selection?.history_validation)));
  if(r.recorded_result)area.append(el('p','原检查状态 '+ovLabel(r.recorded_result.status)+'；原检查时点 '+ovText(r.recorded_result.request?.checked_at)));
  else if(r.action)area.append(el('p','此原管理事件未包含新条件检查。'));
  ovDetailRequest(area,row);ovOriginal(area,row.open_operation,row.open_payload);
  if(row.watch_ref)ovOriginal(area,'read-odds-watch',{reference:row.watch_ref},'导出关联 Watch 原请求');
  area.append(details('原 Watch、检查或管理事件字段',r));box.append(area);
 }
 for(const {row,check} of checks){
  const area=el('div',undefined,'question');area.append(el('p','原检查 '+ovText(check.request?.checked_at)+' · '+ovLabel(check.status)));
  for(const condition of check.condition_results||[])area.append(el('p','原条件 '+ovText(condition.condition_id)+
   ' / '+ovText(condition.metric)+' / '+ovText(condition.world_id)+' · '+ovLabel(condition.status)+' · 原值 '+ovText(condition.value)+' · 原因 '+ovText(condition.reason)));
  for(const reason of check.request?.review_reasons||[])area.append(el('p','原复核理由：'+ovText(reason)));
  for(const item of ovBoundaryRows([row]))area.append(el('p',ovBoundaryText(item)));
  area.append(details('原检查、条件、证据和边界',check));box.append(area);
 }
 if(!checks.length)box.append(el('p','本次范围没有可读的原检查；不能推断没有 Watch、无需复核或今日条件成立。'));
 box.append(el('p','检查可被多个原对象引用；展示行数不代表新增检查次数。','odds-overview-original'),
  details('原研究、版本关系与精确引用',research));
}
function ovRenderExact(box,detail){
 if(!detail)return;
 const area=el('section',undefined,'odds-overview-detail');area.id='odds-overview-exact';
 area.append(el('h3','本次指定的确切原对象读取'),
  el('p','实际操作 '+ovText(detail.operation)+' · '+label(detail.status)),
  el('p','以下仅代表这一个显式原 reader 的实际返回；未把总表全部对象重新计算。'));
 if(detail.status==='readable'){
  const result=detail.result;area.append(el('p','该确切请求已执行并返回可读结果。'));
  if(result?.result?.price)ovPrice(area,result.result);
  if(result?.recalculation!==undefined)area.append(el('p','原 reader 重演结果：'+ovText(result.recalculation)));
  area.append(details('确切原 reader 的完整返回',result));
 }else area.append(el('p','该对象没有可读详情：'+ovText(detail.error||detail.status)+'。原总览保管声明不替代此次详情结果。'));
 area.append(details('本次确切选择', {operation:detail.operation,payload:detail.payload}),
  button('返回 Odds 总表',()=>{location.hash='odds-overview-table';}));box.append(area);
}
function ovSelect(parent,id,title,values){
 const labelNode=el('label',title),select=el('select');select.id=id;
 const all=el('option','全部已读范围');all.value='';select.append(all);
 for(const [value,text] of values){const option=el('option',text);option.value=value;select.append(option);}
 labelNode.append(select);parent.append(labelNode);return select;
}
function renderOddsBookOverview(box,panel){
 const view=panel.view,rows=ovRows(view),groups=view.research_relations||[],scan=view.scan,counts=view.counts;
 const boundaries=ovBoundaryRows(rows),unique=values=>[...new Set(values)];
 box.append(el('p','固定快照 '+view.snapshot+'；按原存储顺序读取，未作投资排序。'));
 const scope=el('div',undefined,'odds-overview-scope');scope.append(
  el('p','本次实际连续读取 '+counts.pages+' 页；扫描 ['+scan.start+', '+scan.end+') / '+scan.total_candidates+' 个候选。'),
  el('p','候选覆盖：'+(scan.complete_from_start?'已从0扫描至末尾':'尚未完整扫描')+'；未读前缀 '+scan.unread_prefix+'，未读尾部 '+scan.unread_tail+'。'),
  el('p','已扫描内容：'+(view.content_complete?'未发现内容缺项':'存在内容缺项')+'；失败 '+counts.failed_items+'，容量省略 '+counts.omitted_items+'。'));
 box.append(scope,el('p','当前适用性未认证；保存时的价格、资格、估值和检查时钟均保留。筛选只选择本次已读内容，坏项与未读范围始终保留。','banner'));
 const actions=el('div',undefined,'actions');actions.append(button('导出本次总览读取请求',()=>request('odds-book-overview.json',roomRequest(oddsOverviewPlan()))));
 if(panel.open_request)actions.append(button('导出原单页读取请求',()=>request('odds-book-original-page.json',panel.open_request)));
 if(view.next_cursor)actions.append(button('导出后续总览读取请求',()=>request('odds-book-overview-next.json',
  roomRequest(oddsOverviewPlan({...panel.query,cursor:view.next_cursor})))));
 if(panel.query.research_ref)actions.append(button('导出同快照未过滤总览首页',()=>request('odds-book-overview-unfiltered.json',
  roomRequest(oddsOverviewPlan({snapshot:view.snapshot,limit:panel.query.limit,scan_limit:panel.query.scan_limit})))));
 box.append(actions,details('实际页链、覆盖与读取边界',{snapshot:view.snapshot,scan,counts,content_complete:view.content_complete,
  coverage:view.coverage,query:panel.query,page_limit:panel.page_limit,semantic_replay:view.semantic_replay,current_validity:view.current_validity,
  human_acceptance:view.human_acceptance,investment_authority:view.investment_authority,persistence:view.persistence,source_acquisition:view.source_acquisition}));
 const filters=el('div',undefined,'odds-overview-filters'),searchLabel=el('label','研究对象、版本或原证券代码');
 const search=el('input');search.id='odds-overview-query';search.type='search';search.placeholder='只搜索本次已读内容';searchLabel.append(search);filters.append(searchLabel);
 const researchState=ovSelect(filters,'odds-overview-research-state','研究版本状态',unique(groups.map(g=>g.research.at_snapshot?.status).filter(Boolean)).map(v=>[v,ovLabel(v)]));
 const oddsState=ovSelect(filters,'odds-overview-state','已存 Odds 层次',[
  ['conditional','有原条件结果'],['probability','有原概率结果'],['spec-only','有规格，本次未见结果'],['no-result','本次未见可读结果']]);
 const reviewState=ovSelect(filters,'odds-overview-review','原复核、检查与活动状态',[
  ['unresolved','研究记录有未决项'],...unique(ovStates(rows)).map(v=>[v,ovLabel(v)])]);
 const priceKind=ovSelect(filters,'odds-overview-price-kind','原价格性质',unique(ovResults(rows).map(r=>r.recorded.price.kind)).map(v=>[v,ovLabel(v)]));
 box.append(filters);
 const boundaryFilters=el('div',undefined,'odds-overview-boundary-filters');
 const metric=ovSelect(boundaryFilters,'odds-overview-metric','原边界指标',unique(boundaries.map(b=>b.boundary.metric)).map(v=>[v,ovText(v)]));
 const world=ovSelect(boundaryFilters,'odds-overview-world','原边界情景',unique(boundaries.map(b=>ovText(b.boundary.world_id))).map(v=>[v,v]));
 const direction=ovSelect(boundaryFilters,'odds-overview-direction','原边界方向',unique(boundaries.map(b=>b.side)).map(v=>[v,ovSide(v)]));
 const distance=ovSelect(boundaryFilters,'odds-overview-distance','原记录的边界距离',unique(boundaries.map(b=>ovText(b.edge?.distance))).map(v=>[v,v]));
 box.append(el('p','边界筛选保留原指标、情景、方向与十进制距离；没有跨指标的“接近”评分，也不会创建新触发条件。','odds-overview-original'),boundaryFilters);
 const count=el('p');count.id='odds-overview-count';count.setAttribute('aria-live','polite');box.append(count);
 const wrap=el('div',undefined,'odds-overview-table-wrap'),table=el('table',undefined,'odds-overview-table');
 table.id='odds-overview-table';table.setAttribute('aria-label','同一快照的跨研究版本 Odds 总表');
 const head=el('thead'),header=el('tr'),titles=['研究与版本','已存结果层次','原价格与资格','原复核与边界','读取接续'];
 for(const title of titles){const th=el('th',title);th.setAttribute('scope','col');header.append(th);}
 head.append(header);const body=el('tbody');table.append(head,body);wrap.append(table);box.append(wrap);
 const selectedDetail=el('section',undefined,'odds-overview-detail');selectedDetail.id='odds-overview-selected';selectedDetail.setAttribute('tabindex','-1');box.append(selectedDetail);
 const fail=el('section',undefined,'odds-overview-failures');fail.id='odds-overview-failures';
 fail.append(el('h3','全部已读坏项与容量省略（不参与隐藏筛选）'),
  el('p','原失败 '+counts.failed_items+'，原容量省略 '+counts.omitted_items+'；未读前缀 '+scan.unread_prefix+'，未读尾部 '+scan.unread_tail+'。'));
 for(const row of rows.filter(item=>item.status!=='readable')){
  const area=el('article',undefined,'odds-overview-failure');area.append(el('p',row.kind+' · '+label(row.status)),
   el('p',row.path),el('p','原缺口 '+ovText(row.error)+'；原因 '+ovText(row.reason)+'；过滤归属 '+ovText(row.filter_match)));
  ovOriginal(area,row.open_operation,row.open_payload,'导出原候选读取请求');area.append(details('原坏项与精确引用',row));fail.append(area);
 }
 if(!rows.some(row=>row.status!=='readable'))fail.append(el('p','本次已读对象没有坏项或容量省略；不据此认证未读范围。'));
 box.append(fail);ovRenderExact(box,panel.detail);
 let chosen=null;
 function update(){
  const text=search.value.toLocaleLowerCase();
  const found=groups.filter(group=>{
   const owned=ovGroupRows(rows,group),results=ovResults(owned),r=group.research;
   const searchable=[r.case_id,r.revision_id,r.mandate,...owned.flatMap(row=>[row.recorded?.security?.code,row.recorded?.price?.security?.code])].filter(Boolean).join(' ').toLocaleLowerCase();
   if(text&&!searchable.includes(text))return false;
   if(researchState.value&&r.at_snapshot?.status!==researchState.value)return false;
   if(oddsState.value==='conditional'&&!results.some(row=>row.recorded.schema!=='probability-odds-v1'))return false;
   if(oddsState.value==='probability'&&!results.some(row=>row.recorded.schema==='probability-odds-v1'))return false;
   if(oddsState.value==='spec-only'&&(!(group.spec_groups||[]).length||results.length))return false;
   if(oddsState.value==='no-result'&&results.length)return false;
   if(reviewState.value==='unresolved'&&!(r.recorded_unresolved||[]).length)return false;
   if(reviewState.value&&reviewState.value!=='unresolved'&&!ovStates(owned).includes(reviewState.value))return false;
   if(priceKind.value&&!results.some(row=>row.recorded.price.kind===priceKind.value))return false;
   if(metric.value||world.value||direction.value||distance.value){
    if(!ovBoundaryRows(owned).some(item=>(!metric.value||item.boundary.metric===metric.value)&&
     (!world.value||ovText(item.boundary.world_id)===world.value)&&(!direction.value||item.side===direction.value)&&
     (!distance.value||ovText(item.edge?.distance)===distance.value)))return false;
   }
   return true;
  });
  count.textContent='本地筛选匹配 '+found.length+' / '+groups.length+' 个已读研究版本；原候选分母 '+scan.total_candidates+'，失败和省略未隐藏。';
  body.replaceChildren();
  for(const group of found){
   const owned=ovGroupRows(rows,group),r=group.research,tr=el('tr'),cells=titles.map(title=>{
    const td=el('td');td.setAttribute('data-label',title);return td;});
   cells[0].append(el('strong',r.case_id+' / '+r.revision_id),el('p',ovLabel(r.kind)+' · '+ovLabel(r.at_snapshot?.status)),el('p',r.mandate));
   cells[1].append(el('p',ovLabel(group.saved_odds)),
    el('p','同版规格 '+(group.spec_groups||[]).length+'；本次可读结果 '+ovResults(owned).length+'。'));
   const results=ovResults(owned);
   for(const row of results)ovPrice(cells[2],row.recorded,true);
   if(!results.length)cells[2].append(el('p','本次没有可读价格结果；当前合格新价未认证。'));
   const states=ovStates(owned),watches=owned.filter(row=>row.recorded?.watch_id);
   cells[3].append(el('p','原未决项 '+(r.recorded_unresolved||[]).length+'；原记录状态 '+(states.length?states.map(ovLabel).join('、'):'本次未读到')),
    el('p','本次已读 Watch '+watches.length+'；原活动状态未作当前认证。'));
   for(const watch of watches)cells[3].append(el('p','原 Watch '+ovText(watch.recorded.watch_id)+
    '；原活动选择 '+ovText(watch.activity_selection?.status)+'；历史验证 '+ovText(watch.activity_selection?.history_validation)));
   const selectedBoundaries=ovBoundaryRows(owned);
   for(const item of selectedBoundaries)cells[3].append(el('p',ovBoundaryText(item)));
   if(!selectedBoundaries.length)cells[3].append(el('p','本次未读到原边界记录；不判断条件接近程度。'));
   cells[4].append(button('查看本版已读详情',()=>{
    chosen=group.research_key;ovRenderGroupDetail(selectedDetail,group,rows);location.hash='odds-overview-selected';selectedDetail.focus?.();
   }));ovResearchActions(cells[4],r);
   tr.append(...cells);body.append(tr);
  }
  if(!found.some(group=>group.research_key===chosen))chosen=found[0]?.research_key||null;
  ovRenderGroupDetail(selectedDetail,found.find(group=>group.research_key===chosen)||null,rows);
 }
 for(const control of [search,researchState,oddsState,reviewState,priceKind,metric,world,direction,distance])control.addEventListener(control===search?'input':'change',update);
 filters.append(button('清除总览筛选',()=>{
  for(const control of [search,researchState,oddsState,reviewState,priceKind,metric,world,direction,distance])control.value='';
  update();
 }));
 update();
}
"""
