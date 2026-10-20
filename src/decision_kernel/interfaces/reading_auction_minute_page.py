"""Literal offline projection of one exact minute root and its original cohort."""

CSS = r"""
.minute-table-wrap{overflow-x:auto}.minute-table{width:100%;border-collapse:collapse;font-size:14px}.minute-table th,.minute-table td{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}.minute-entry{border-top:1px solid #dce4e8;padding:12px 0}
"""

JS = r"""
function renderAuctionMinute(){
 const pane=$('auction-minute-content'),part=D.auction_minute;if(!pane||!part)return;
 const val=x=>x===null||x===undefined?'未知':String(x);
 const groups={MATCHED:'原条件匹配',NOT_MATCHED:'竞价可比但未匹配',AUCTION_UNRESOLVED:'竞价未能判断',STATIC_EXCLUDED:'静态排除',STATIC_UNRESOLVED:'静态未能判断'};
 const states={EXACT_0940_CLOSE_OBSERVED:'唯一精确 09:40 close 可读',COHORT_IDENTITY_UNRESOLVED:'原名单身份未明',REQUEST_BUDGET_NOT_REQUESTED:'原 16 批范围外，未请求',NOT_REQUESTED_AFTER_SOURCE_STOP:'来源停止后未尝试',ORIGINAL_UNAVAILABLE:'确切原件未恢复',SYMBOL_AMBIGUOUS:'同证券原行重复，不能择一',SYMBOL_NOT_RETURNED:'原响应未返回该证券',TARGET_MINUTE_AMBIGUOUS:'目标分钟重复，不能择一',TARGET_MINUTE_NOT_RETURNED:'没有精确 09:40 点',TARGET_MINUTE_TIME_UNQUALIFIED:'09:39 开始边界不合格',TARGET_CLOSE_INVALID:'close 缺失或不合格',MINUTE_ITEMS_UNAVAILABLE:'分钟列表不合格',ENTITLEMENT_DENIED:'原回执：来源拒绝',RATE_LIMITED:'原回执：来源限流',AUTHENTICATION_FAILED:'原回执：来源认证失败',PARSE_FAILURE:'原响应无法按本合同解释'};
 const table=(head,rows)=>{const wrap=el('div',undefined,'minute-table-wrap'),t=el('table',undefined,'minute-table'),h=el('tr');for(const title of head)h.append(el('th',title));t.append(h);for(const values of rows){const r=el('tr');for(const value of values)r.append(el('td',val(value)));t.append(r);}wrap.append(t);return wrap;};
 pane.append(el('p','同一原竞价名单的 09:39–09:40 供应商 close。历史或延后取得，不代表 09:40 当时发现；单点不建立日内路径、成交性或交易效果。','banner'),
  button('下载完整分钟观察 JSON',()=>save('auction-minute-model.json',JSON.stringify(part,null,2),'application/json')),
  details('精确分钟根与读取范围',{plan:part.plan,status:part.status,error:part.error}));
 if(part.status==='not-included'){pane.append(el('p',`整组未展示：${part.error}。完整范围未被筛成一个小样本。`));return;}
 const plan=p=>request('auction-minute-room.json',roomRequest({...D.plan,auction_minute:p})),controls=el('div',undefined,'actions');
 controls.append(button('导出分钟原根读取请求',()=>request('auction-minute-root.json',part.open_request)),
  button('导出分钟第一页请求',()=>plan({...part.plan,offset:0,entity_id:null})));
 const next=button('导出分钟下一页请求',()=>plan(part.next_plan));next.disabled=!part.next_plan;controls.append(next);pane.append(controls);
 const input=el('input');input.id='auction-minute-entity';input.value=part.plan.entity_id||'';input.placeholder='例如 600001.SH 或原 BJ 身份；留空为全名单';
 const caption=el('label','按确切证券筛选；所有原组分母和未尝试成员继续保留');caption.htmlFor=input.id;
 pane.append(caption,input,button('导出分钟筛选请求',()=>plan({...part.plan,offset:0,entity_id:input.value.trim()||null})));
 if(!part.view){pane.append(el('p',`分钟观察${label(part.status)}：${part.error||'确切原件未恢复'}。`));return;}
 const restored=part.view,v=restored.view;
 pane.append(details('原件恢复与来源资格',{status:restored.status,sources:restored.sources,qualification:restored.qualification,reading_error:restored.reading_error}));
 for(const source of restored.sources)pane.append(button(`导出分钟来源 ${source.path} 读取请求`,()=>request('auction-minute-source.json',{operation:'read-source',payload:source.reference})));
 if(!v){pane.append(el('p','原竞价名单无法独立恢复，分母未知；没有改用缓存名单或其他日期。'));return;}
 pane.append(el('p',`市场日 ${v.market_session} · 原名单 ${v.cohort_denominator} 项 · 准确 09:40 close 可读 ${v.observed_0940} 项。`),
  el('p',`目标 ${v.target_open_time} 至 ${v.target_close_time}；原取得 ${v.acquisition.started_at} 至 ${v.acquisition.finished_at}。本次阅读不会重写这些时钟。`),
  el('p',`原回执 ${v.acquisition.provided_attempt_count} 个尝试，停止原因 ${v.acquisition.source_stop||'无'}；本次来源请求为 0。`),
  table(['原分组','完整分母','精确点可读'],Object.entries(v.groups).map(([key,g])=>[groups[key],g.denominator,g.observed_0940])),
  el('p','Relay 原竞价价与 FTShare 分钟 close 分栏；跨来源收益为 NOT_COMPUTED。分钟价沿供应商原字段，不补币种、复权或单位转换证明。'),
  el('p',v.coverage_gaps?'完整范围仍有缺项或原池覆盖未知；当前分页不能消除缺口。':'完整原返回名单的点可读；原池未返回范围和独立市场穷尽仍未证明。'),
  details('原请求、给定回执与未请求范围',{plan:restored.capture_plan,receipt:restored.provided_receipt,unrequested_symbols:v.unrequested_symbols,unexpected_rows:v.unexpected_rows}),
  details('原名单背景、时点和限制',{seed:v.seed_context,cohort_version:v.cohort_version,view_version:v.view_version,limits:v.limitations}),
  el('p',`当前筛选 ${part.page.matched_count} 项，本页 ${part.page.returned} 项；完整分母仍为 ${v.cohort_denominator}。`,'muted'));
 for(const row of part.page.entries){const box=el('article',undefined,'minute-entry');box.append(el('h3',`${row.entity_id||row.cohort_id} · ${row.name||'原名称未取得'}`),
  el('p',`${groups[row.group]}；${states[row.minute.status]||row.minute.status}`),
  table(['原 Relay 竞价价','FTShare 09:40 close','跨来源收益'],[[row.auction_price,row.minute.close_0940,v.cross_provider_return]]),
  details('原成员、字段位置与逐项资格',row));pane.append(box);}
 if(!part.page.entries.length)pane.append(el('p','当前页没有该身份；完整原名单及其未知成员继续保留。'));
}
renderAuctionMinute();
"""
