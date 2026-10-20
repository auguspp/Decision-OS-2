"""Offline literal presentation of full T+1 cohorts and their exact references."""

CSS = r"""
.auction-next-wrap{overflow-x:auto}.auction-next-table{width:100%;border-collapse:collapse;font-size:14px}.auction-next-table th,.auction-next-table td{text-align:left;padding:8px;vertical-align:top;border-bottom:1px solid #dce4e8;overflow-wrap:anywhere}.auction-next-cohort{padding:14px 0;border-top:1px solid #dce4e8}
"""

JS = r"""
function renderAuctionNextSession(){
 const pane=$('auction-next-content'),part=D.auction_next_session;if(!pane||!part)return;
 const val=x=>x===null||x===undefined?'未知':String(x);
 const names={MATCHED:'原条件匹配',NOT_MATCHED:'竞价可比但未匹配',AUCTION_UNRESOLVED:'静态内竞价未决',STATIC_EXCLUDED:'静态排除',STATIC_UNRESOLVED:'静态未决'};
 const table=(headers,rows)=>{const wrap=el('div',undefined,'auction-next-wrap'),t=el('table',undefined,'auction-next-table'),h=el('tr');for(const s of headers)h.append(el('th',s));t.append(h);for(const values of rows){const r=el('tr');for(const v of values)r.append(el('td',val(v)));t.append(r);}wrap.append(t);return wrap;};
 pane.append(el('p','保留原竞价 U 名单和五组，按明确 SSE 日历固定其后第一个开市日。报价缺失不会顺延目标；原 raw open 与 close 分别资格化，不生成跨日收益或交易效果。','banner'),
  button('下载完整下一开市日观察 JSON',()=>save('auction-next-session-model.json',JSON.stringify(part,null,2),'application/json')),
  details('本次精确读取范围',{plan:part.plan,status:part.status,error:part.error}));
 if(part.status==='not-included'){pane.append(el('p','本组超过有限展示范围，完整根仍保留。'));return;}
 const navigate=p=>request('auction-next-room.json',roomRequest({...D.plan,auction_next_session:p}));
 pane.append(button('导出下一开市日原根读取请求',()=>request('auction-next-root.json',part.open_request)),
  button('导出下一开市日第一页请求',()=>navigate({...part.plan,offset:0,entity_id:null})));
 const next=button('导出下一开市日下一页请求',()=>navigate(part.next_plan));next.disabled=!part.next_plan;pane.append(next);
 const input=el('input');input.id='auction-next-entity';input.value=part.plan.entity_id||'';input.placeholder='例如 600001.SH；留空保留全部成员';
 const caption=el('label','精确证券筛选；所有原分母、未决与接续仍保留');caption.htmlFor=input.id;
 pane.append(caption,input,button('导出下一开市日筛选请求',()=>navigate({...part.plan,offset:0,entity_id:input.value.trim()||null})));
 if(!part.view||!part.view.view){pane.append(el('p',`原件未完整恢复：${part.error||part.view?.reading_error||part.status}。没有改用其他日期或猜值。`));return;}
 const restored=part.view,v=restored.view;
 pane.append(el('p',`记录时钟 ${v.checked_at}；记录时间不能补签行情当时可知。完整版本 ${v.view_version}`,'muted'),details('全部查询原引用与恢复状态',restored.sources));
 for(const source of restored.sources)pane.append(button(`导出下一开市日来源 ${source.query} 读取请求`,()=>request('auction-next-source.json',{operation:'read-source',payload:source.reference})));
 if(v.previous_reference)pane.append(button('导出下一开市日前版原件请求',()=>request('auction-next-previous.json',{operation:'read-source',payload:v.previous_reference})),button('导出下一开市日前版阅读请求',()=>navigate({...part.plan,reference:v.previous_reference,offset:0,entity_id:null})));
 if(v.current_auction&&v.current_auction.cohort_denominator===null)pane.append(el('p','当前原 auction 名单尚未建立，分母未知；不能视为零。此前 pending 观察保留。'));
 for(const c of v.cohorts){const box=el('article',undefined,'auction-next-cohort'),r=c.result;
  box.append(el('h3',`原竞价 ${c.market_session} → ${c.target_session||'目标未建立'}`),
   el('p',`原独立 U 分母 ${val(c.cohort_denominator)}；原 U/D/Z 返回 ${val(c.prior_temperature?.returned_pool_rows)} 行。名单版本 ${c.cohort_version}`),
   el('p',`完成时点资格：${r.completion_clock}；限定目标日 15:30 后的原请求及接收时钟。可读值和已完成日观察分开。`),
   el('p',`当前 ${r.status}；取得时点 ${r.acquisition_timing}。首次记录 ${r.first_recorded_at}；结果摘要 ${r.result_hash}`),
   table(['原分组','全分母','开盘可读','收盘可读','完成日开盘','完成日收盘'],Object.entries(r.groups).map(([g,n])=>[names[g],n.denominator,n.open_readable,n.close_readable,n.open_observed,n.close_observed])),
   button(`导出原竞价 ${c.market_session} 原件请求`,()=>request('auction-next-seed.json',{operation:'read-source',payload:c.auction_reference})),
   details('完整原成员、来源与目标日证据',{members:c.members,original_groups:c.original_groups,prior_temperature:c.prior_temperature,original_coverage:c.original_coverage,calendar_prefix:c.calendar_prefix,calendar_gap:c.calendar_gap,quote_lineage:r.quote_lineage,row_coverage:r.row_coverage}));
  if(c.observation==='PREVIOUS_DATED_OBSERVATIONS_RETAINED')box.append(el('p','本次包丢失已有字段或完成资格，以下保留整份旧结果及其原来源；不是本次重新取得，也没有跨包拼接。','banner'));
  box.append(details('本次尝试与旧结果保留缺口',{observation:c.observation,current_attempt:c.current_attempt}));pane.append(box);
 }
 for(const a of v.archived_cohorts)pane.append(el('p',`已归档原竞价 ${a.market_session} → ${a.target_session}，准确结果 ${a.result_hash}`),button(`导出归档 ${a.market_session} 原件请求`,()=>request('auction-next-archive.json',{operation:'read-source',payload:a.reference})),button(`导出归档 ${a.market_session} 阅读请求`,()=>navigate({...part.plan,reference:a.reference,offset:0,entity_id:null})));
 pane.append(el('p',`当前筛选 ${part.page.matched_count} 项，本页 ${part.page.returned} 项；上方完整 cohort 分母保持。`,'muted'));
 for(const row of part.page.entries)pane.append(el('h3',`${row.entity_id||row.member_id} · ${names[row.group]} · ${row.target_session||'目标未知'}`),table(['原开盘可读','原收盘可读','完成日 raw open','完成日 raw close','开盘字段状态','收盘字段状态'],[[row.readable_open,row.readable_close,row.next_open,row.next_close,row.open_status,row.close_status]]));
 if(!part.page.entries.length)pane.append(el('p','本页没有匹配身份；原分母与全部缺口仍在完整模型中。'));
}
renderAuctionNextSession();
"""
