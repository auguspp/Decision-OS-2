"""Literal Odds Book display and requests for the existing authenticated host."""

CSS = r"""
.odds-entry{padding:14px 0;border-bottom:1px solid #dce4e8}.odds-entry h3{margin:5px 0}.odds-relations{margin:16px 0;padding:12px;background:#f4f8fa}.odds-clocks{font-size:14px;color:#516977}
"""

# The small entry remains discoverable without implicitly reading the Book.
# The full renderer is included only when its optional projection is admitted.
ENTRY_JS = r"""
function oddsBookPlan(query){return researchBookPlan({odds_book:{...query,snapshot:D.catalogue.snapshot}});}
function renderOddsBookEntry(){
 const box=$('odds-book-content'),panel=D.odds_book;if(!box)return;
 if(panel?.view){renderOddsBook(box,panel);return;}
 const query=panel?.query||D.plan.odds_book;
 box.append(el('p',query?`Odds Book 投影未包含（not-included）：${panel?.error||'显示容量不足'}。原研究与 Research Book 保留；使用确切原入口。`:'本次未读取 Odds Book。可导出与本页研究目录同快照的首个有限页请求；页面不会自行读取。'));
 box.append(button(query?'导出本页 Odds Book 原读取请求':'导出同快照 Odds Book 首页请求',()=>request('odds-book-read.json',query?{operation:'read-odds-book',payload:query}:roomRequest(oddsBookPlan({})))));
 if(panel)box.append(details('实际范围与未展示原因',panel));
}
renderOddsBookEntry();
"""

JS = r"""
function oddsText(value){return value===null||value===undefined?'未记录':String(value);}
function oddsOriginal(box,operation,payload,title){
 if(operation&&payload)box.append(button(title||'导出原件读取请求',()=>request(operation+'.json',{operation,payload})));
}
function oddsResearchActions(box,research){
 if(!research?.reference)return;
 box.append(button('导出此确切研究的 Odds 页请求',()=>request('odds-book-research.json',roomRequest(oddsBookPlan({limit:D.plan.odds_book.limit,scan_limit:D.plan.odds_book.scan_limit,research_ref:research.reference})))));
 box.append(button('导出此版本 Research Book 请求',()=>request('odds-book-research-version.json',roomRequest(researchBookPlan({reference:research.reference})))));
}
function oddsRecorded(box,row){
 const r=row.recorded||{},spec=r.schema==='valuation-spec-v1'?r:null,check=r.recorded_result;
 if(spec){
  box.append(el('p',`${spec.security.code} / ${spec.security.share_class} · ${spec.currency} · 股数 ${spec.share_count}`),
   el('p',`价值类型 ${spec.value_kind}；方法 ${spec.method}`),
   el('p',`估值日期 ${oddsText(spec.valuation_date)}；目标日期 ${oddsText(spec.target_date)}；资本结构日期 ${oddsText(spec.capital_structure?.as_of)}`,'odds-clocks'),
   el('p',`原资本结构假设：${oddsText(spec.capital_structure?.assumption)}`),el('p',`原现金口径：${spec.cash_policy}；概率状态：${spec.probability_status}`));
  for(const world of spec.worlds||[])box.append(details(`原情景 ${world.id} · ${world.name}`,world));
 }
 if(r.price){
  box.append(el('p',`已存价格 ${oddsText(r.price.price)} ${oddsText(r.price.currency)} · ${oddsText(r.price.security?.code)} / ${oddsText(r.price.security?.share_class)}`),
   el('p',`原价格时点 ${oddsText(r.price.as_of)}；原计算时点 ${oddsText(r.calculation_input?.evaluated_at)}；估值日期 ${oddsText(r.valuation_date)}；目标日期 ${oddsText(r.target_date)}`,'odds-clocks'),
   el('p',r.schema==='probability-odds-v1'?'原研究概率及政策输出；未在本页作经验概率核验或重新计算。':'条件 Odds；未建立概率时不生成等权概率或加权结果。'));
  for(const world of r.world_results||[]){
   const line=el('div',undefined,'question');line.append(el('strong',`原情景 ${oddsText(world.world_id||world.id)}`));
   if(world.value_to_price_distance!==undefined)line.append(el('p',`价值 / 价格距离：${world.value_to_price_distance}（不是未来收益）`));
   if(world.undiscounted_holding_period_return!==undefined)line.append(el('p',`未折现持有期回报：${world.undiscounted_holding_period_return}（不新增年化）`));
   line.append(details('原情景结果与口径',world));box.append(line);
  }
  const aggregate=r.schema==='probability-odds-v1'?{aggregate:r.aggregate}:{weighted_aggregate:r.weighted_aggregate};
  box.append(details('原聚合结果、参与区间与资格（按原字段）',{...aggregate,participation_zone:r.participation_zone,market_qualification:r.market_qualification,probability_status:r.probability_status,limitations:r.limitations}));
 }
 if(check||r.at||r.checked_at)box.append(el('p',`原检查 / 事件时钟：${oddsText(check?.request?.checked_at||check?.request?.at||r.checked_at||r.at)}；原状态：${oddsText(r.stored_state||check?.status)}。未重放整段历史。`,'odds-clocks'));
 if(row.spec_ref)oddsOriginal(box,'read-valuation',{reference:row.spec_ref},'导出原估值规格请求');
 if(row.watch_ref)oddsOriginal(box,'read-odds-watch',{reference:row.watch_ref},'导出原 Watch 请求（另行校验历史）');
 box.append(details('完整原字段与限制',r));
}
function renderOddsBook(box,panel){
 const page=panel.view,scan=page.scan;
 box.append(el('p',`固定快照 ${page.snapshot}；候选总数 ${scan.total_candidates}，本次扫描 [${scan.start}, ${scan.end})，返回 ${page.items.length} 项。覆盖仅本页。`),
  el('p',`原状态：${page.status}；顺序是存储路径，不是优先级或新近程度。${scan.at_end?'已到候选末尾；不证明此前页面已读。':'仍有候选，需明确导出下一页请求。'}`),
  el('p','已验证保管不等于重新计算，未认证今日适用性；没有人工接受或投资执行授权。页面阅读时钟不刷新原估值、价格、概率或检查日期。','banner'));
 const actions=el('div',undefined,'actions');oddsOriginal(actions,panel.open_request.operation,panel.open_request.payload,'导出本页原 Odds Book 请求');
 if(page.next_cursor)actions.append(button('导出 Odds 下一页请求',()=>request('odds-book-next.json',roomRequest(oddsBookPlan({cursor:page.next_cursor,limit:panel.query.limit,scan_limit:panel.query.scan_limit})))));
 if(panel.query.research_ref)actions.append(button('导出同快照未过滤首页请求',()=>request('odds-book-unfiltered.json',roomRequest(oddsBookPlan({limit:panel.query.limit,scan_limit:panel.query.scan_limit})))));
 box.append(actions,details('原覆盖、身份与读取边界',{snapshot:page.snapshot,scan:page.scan,coverage:page.coverage,query:panel.query,semantic_replay:page.semantic_replay,current_validity:page.current_validity,human_acceptance:page.human_acceptance,investment_authority:page.investment_authority,persistence:page.persistence,source_acquisition:page.source_acquisition}));
 const relations=el('div',undefined,'odds-relations');relations.append(el('h3','本页原研究关系'));
 for(const group of page.research_relations){
  const r=group.research,line=el('div',undefined,'question');line.append(el('strong',`${r.case_id} / ${r.revision_id} · ${r.kind}`),el('p',`已存 Odds 关系：${group.saved_odds}；覆盖 ${group.coverage}。`));
  oddsResearchActions(line,r);
  for(const spec of group.spec_groups||[])line.append(details('同一原规格的本页多个已存结果；不选择“最新价格”',spec));
  relations.append(line);
 }
 if(!page.research_relations.length)relations.append(el('p','本页没有可建立的研究关系；不推断全库无 Odds。'));
 box.append(relations);
 for(const row of page.items){
  const card=el('article',undefined,'odds-entry'),r=row.research;card.append(el('h3',r?`${r.case_id} / ${r.revision_id} · ${r.kind}`:row.path),el('p',`${row.kind} · ${label(row.status)}`));
  if(r){card.append(el('p',`原委托：${r.mandate}`),el('p',`本版保存于 ${r.stored_at}；保存日期不是事实截止。快照内状态：${r.at_snapshot.status}。`,'odds-clocks'));oddsResearchActions(card,r);}
  const original=el('div',undefined,'actions');oddsOriginal(original,row.open_operation,row.open_payload);card.append(original);
  if(row.status==='readable'){
   card.append(el('p',`保管 ${row.custody}；语义重放 ${row.semantic_replay}；当前适用性 ${row.current_validity}；资格 ${row.qualification}。`));oddsRecorded(card,row);
   if(row.activity_selection){const a=row.activity_selection;card.append(el('p',`原活动指针状态：${a.status}；历史验证 ${a.history_validation}。`));oddsOriginal(card,a.open_operation,a.open_payload,'导出当前活动查询（执行时重新选择；不是本快照回放）');}
  }else card.append(el('p',`原缺口：${row.error||row.status}；原因 ${row.reason||'未提供'}；过滤匹配 ${row.filter_match||'未知'}。未隐藏此候选或构造成功引用。`));
  card.append(details('精确原引用与研究未决项',{reference:row.reference,identity:row.identity,research:row.research,open_operation:row.open_operation,open_payload:row.open_payload}));box.append(card);
 }
 if(!page.items.length)box.append(el('p',`本页没有返回项：${page.status}。沿原扫描范围解释，不把本页缺项升级为全库判断。`));
}
"""
