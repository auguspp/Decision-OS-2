"""Literal offline presentation of the complete retained auction observation."""

CSS = r"""
.auction-table-wrap{overflow-x:auto}.auction-table{width:100%;border-collapse:collapse;font-size:14px}.auction-table th,.auction-table td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}.auction-entry{border-top:1px solid #dce4e8;padding:12px 0}.auction-columns{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}@media(max-width:900px){.auction-columns{grid-template-columns:1fr}}
"""

JS = r"""
function renderAuction(){
 const pane=$('auction-content'),part=D.auction;if(!pane||!part)return;
 const val=x=>x===null||x===undefined?'未知':String(x);
 const names={MATCHED:'原条件匹配',NOT_MATCHED:'竞价可比但未匹配',AUCTION_UNRESOLVED:'静态条件内、竞价未完成',STATIC_EXCLUDED:'静态条件排除',STATIC_UNRESOLVED:'静态条件未能判断'};
 const states={SHADOW_PROFILE_MATCH:'匹配固定观察条件',OUTSIDE_AUCTION_GAP_PROFILE:'竞价涨幅不在固定范围',AUCTION_INPUT_UNAVAILABLE:'竞价输入未取得',AUCTION_ROW_UNAVAILABLE:'该证券竞价未返回',AUCTION_VALUE_UNAVAILABLE_OR_CONFLICT:'竞价值缺失或冲突',EXCLUDED_BOARD:'代码板块排除',EXCLUDED_PRIOR_ST_NAME:'昨日名称含 ST',OUTSIDE_PRIOR_TURNOVER_PROFILE:'昨日换手不在固定范围',PRIOR_NAME_UNAVAILABLE:'昨日名称未能判断',PRIOR_TURNOVER_UNAVAILABLE:'昨日换手未能判断',PRIOR_IDENTITY_CONFLICT:'昨日身份值冲突',PRIOR_SESSION_MISMATCH:'昨日原行日期不符',PRIOR_SECURITY_IDENTITY_UNAVAILABLE:'原 U 行证券身份未明'};
 const table=(head,rows)=>{const wrap=el('div',undefined,'auction-table-wrap'),t=el('table',undefined,'auction-table'),h=el('tr');for(const title of head)h.append(el('th',title));t.append(h);for(const values of rows){const r=el('tr');for(const value of values)r.append(el('td',val(value)));t.append(r);}wrap.append(t);return wrap;};
 pane.append(el('p','昨日换手 10%–30%、原主板代码条件且昨日名称不含 ST、竞价相对同一行昨收高开 3%–9%，均含端点。价格观察不证明弱转强、可成交或策略效果。','banner'),
  button('下载完整竞价观察 JSON',()=>save('opening-auction-model.json',JSON.stringify(part,null,2),'application/json')),
  details('精确原根与读取范围',{plan:part.plan,status:part.status,error:part.error}));
 if(part.status==='not-included'){pane.append(el('p',`本组未展示：${part.error}。完整根仍保留，未截取一个好子集代替全范围。`));return;}
 const plan=p=>request('auction-room.json',roomRequest({...D.plan,auction:p})),controls=el('div',undefined,'actions');
 controls.append(button('导出竞价原根读取请求',()=>request('auction-root.json',part.open_request)),
  button('导出竞价第一页请求',()=>plan({...part.plan,offset:0,entity_id:null})));
 const next=button('导出竞价下一页请求',()=>plan(part.next_plan));next.disabled=!part.next_plan;controls.append(next);pane.append(controls);
 const input=el('input');input.id='auction-entity';input.value=part.plan.entity_id||'';input.placeholder='例如 600001.SH；留空为原 U 全范围';
 const caption=el('label','精确证券筛选，保留所有原分母与缺口');caption.htmlFor=input.id;
 pane.append(caption,input,button('导出竞价筛选请求',()=>plan({...part.plan,offset:0,entity_id:input.value.trim()||null})));
 if(!part.view){pane.append(el('p',`竞价${label(part.status)}：${part.error||'原件未恢复'}。没有改用其他根或日期。`));return;}
 const restored=part.view,v=restored.view;
 pane.append(details('保管恢复与原查询引用',{status:restored.status,unavailable_queries:restored.unavailable_queries,sources:restored.sources,qualification:restored.qualification}));
 for(const source of restored.sources)pane.append(button(`导出竞价来源 ${source.path} 读取请求`,()=>request('auction-query.json',{operation:'read-source',payload:source.reference})));
 if(!v){pane.append(el('p','原输入未恢复，观察未知。'));return;}
 const pool=v.prior_temperature,page=part.page;
 pane.append(el('p',`目标日 ${v.market_session} · 前一开市日 ${val(v.previous_session)} · 固定原版本 ${v.view_version}`,'muted'),
  el('p',`原池返回 ${val(pool?.returned_pool_rows)} 行；可识别 U/D/Z 各 ${val(pool?.unique_recognized.U)} / ${val(pool?.unique_recognized.D)} / ${val(pool?.unique_recognized.Z)} 项。独立 U 条件分母 ${val(v.cohort_denominator)}，静态内 ${val(v.static_eligible)}，竞价可比 ${val(v.auction_comparable)}，匹配 ${val(v.matched)}。`),
  el('p',v.coverage_gaps?'完整范围有缺项或覆盖证据未知；当前筛选不能消除这些缺口。':'本次明确返回范围的观察可读；不认证全市场穷尽。'),
  el('p',`取得时点：${({NOT_ESTABLISHED:'原请求／接收时钟不足，盘前时效未建立',RECEIVED_BEFORE_CONTINUOUS_OPEN:'原请求与接收均在目标日 09:26 至 09:30 前',HISTORICAL_OR_LATE_NOT_PREOPEN_DISCOVERY:'历史或迟到取得，不作为盘前发现'})[v.timeliness]||v.timeliness}。本页阅读时间不改变源时钟。`),
  el('h3','竞价到同日 raw close：保留原五组'),
  el('p',`收盘完成资格：${({NOT_ESTABLISHED:'原成功请求／接收时钟缺失，完成时点未知',REQUESTED_BEFORE_SESSION_COMPLETION:'原请求早于目标日 15:30，不计最终收盘',RECEIVED_AFTER_SESSION_COMPLETION:'原请求与接收均不早于目标日 15:30'})[v.same_session.completion_clock]||v.same_session.completion_clock}。可读的原数值与合格最终收盘分别列示。`),
  table(['原分组','分母','同日原值可读','完成收盘可用','两端可比','高于／低于／等于竞价','中位比值变化'],Object.entries(v.groups).map(([k,g])=>[names[k],g.denominator,g.close_readable,g.close_available,g.comparable,`${val(g.above_auction)} / ${val(g.below_auction)} / ${val(g.equal_to_auction)}`,g.median_change])),
  el('p','价格单位 CNY；换手与 gap 为百分数，同日变化为 close / auction_price − 1 的小数比值。零可比时中位数未知；比较方向使用未舍入原价。09:40、日内路径和下一开市日另有责任。'),
  details('全部 U/D/Z 原行、位置及背景资格',{pool_rows:v.pool_rows,prior_temperature:v.prior_temperature}),
  details('完整范围资格、源时钟与版本',{coverage:v.coverage,queries:v.queries,source_lineage:v.source_lineage,acquisition_clocks:v.acquisition_clocks,same_session:v.same_session,cohort_version:v.cohort_version,view_version:v.view_version,limits:v.limitations}),
  el('p',`当前筛选 ${page.matched_count} 项，本页 ${page.returned} 项；完整 U 分母仍为 ${val(v.cohort_denominator)}。`,'muted'));
 for(const row of page.entries){const box=el('article',undefined,'auction-entry');box.append(el('h3',`${row.entity_id||row.cohort_id} · ${row.name||'昨日名称未取得'}`),
  el('p',`${names[row.group]}：${states[row.status]||row.status}`),
  table(['昨日换手 %','竞价价','同一行昨收','gap %','同日原值可读','完成 raw close','同日比值变化'],[[row.prior_turnover_pct,row.auction_price,row.auction_pre_close,row.gap_pct,row.readable_close,row.same_session_close,row.auction_to_close_change]]),
  details('原位置、独立字段资格与接续状态',row));pane.append(box);}
 if(!page.entries.length)pane.append(el('p','当前页没有匹配身份；完整原池与未知范围仍保留。'));
}
renderAuction();
"""
