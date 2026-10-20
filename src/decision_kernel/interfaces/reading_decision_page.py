"""Offline human-readable preparation; literal text and no confirmation controls."""

CSS = r"""
.decision-horizon,.decision-alternative{border:1px solid #dce4e8;border-radius:10px;padding:16px;margin:14px 0}.decision-review-due{border-left:5px solid #a66b14}.decision-history{overflow-wrap:anywhere}
"""

JS = r"""
function renderDecisionPreparation(){
 const pane=$('decision-preparation-content'),part=D.decision_preparation;
 if(!pane)return;
 if(!part){pane.append(el('p','未选择决策准备包。'));return;}
 if(part.open_request)pane.append(button('导出原准备包读取请求',()=>request('decision-preparation-read.json',part.open_request)));
 if(part.status!=='readable'){pane.append(el('p',`准备包未恢复：${part.error||part.status}。`,'banner'));return;}
 const v=part.view,d=v.document,i=d.input;
 const terms={'researching':'继续研究','awaiting-evidence':'等待证据','candidate-for-human-review':'供人审阅的候选判断','not-pursued':'作者暂不推进',
  'not-started':'尚未进入窗口','in-window':'在观察窗口内','window-ended':'窗口已经结束','review-due':'已到复核时点','not-due':'尚未到期',
  'undated':'没有确定日期','date-reached-needs-evidence':'已到预计日期，仍需证据','partial':'资料或研究仍有缺项','readable':'保存版本可读',
  'participate':'参与','wait':'等待','decline':'放弃','continue-research':'继续研究',
  'valuation-not-established':'估值尚未建立','odds-not-established':'Odds 尚未建立','price-context-not-established':'价格上下文尚未建立',
  'qualified-market-not-established':'行情资格尚未建立','target-security-not-established':'目标证券尚未建立'};
 const term=x=>terms[x]||x;
 pane.append(el('h3',`决策准备 · ${i.preparation_id}`),el('p',`${term(i.stance)} · ${term(d.status)}`,'banner'),
  el('p',i.rationale),el('p',`作者声明准备时间 ${i.prepared_at}；本次复核时刻 ${v.projection.at}。`),
  el('p','本页保留 AI 判断与原研究依据。该准备包没有创建正式决定、取得人工确认或执行交易；预计日期到达只提示复核，不能证明催化兑现或机制失效。'),
  el('p',`研究 ${d.research.case_id} / ${d.research.revision_id}；${v.current_use?.status==='historical-research-not-current'?'这是历史研究版本。':'原确切版本见引用。'}`),
  details('原问题、判断、反证与尚未解决内容',d.research),details('采用的原来源及读取范围',d.evidence),
  button('下载完整准备包及判断历史',()=>save('decision-preparation.json',JSON.stringify(v,null,2),'application/json')));
 const list=(title,values)=>{const ul=el('ul');for(const x of values)ul.append(el('li',term(x)));pane.append(el('h4',title),ul);};
 const target=i.target.security;pane.append(el('h4','目标证券与表达'),
  el('p',target?`${target.code} · ${target.share_class}`:'目标证券尚未建立'),el('p',i.target.expression),
  el('p','目标与备选行动是准备包作者的声明。没有账户、总资产或持仓上下文，本页不作组合适配或仓位评估。'));
 list('主要假设',i.major_assumptions);list('最强反证',i.strongest_counterarguments);
 pane.append(el('h4','合理替代选项，交由用户裁定'));
 for(const a of i.alternatives){const card=el('section',undefined,'decision-alternative');
  card.append(el('h4',term(a.choice)),el('p',a.description),el('p',`支持理由：${a.case_for}`),el('p',`不利理由：${a.case_against}`));
  if(a.unknowns.length)card.append(details('此选项仍未知的部分',a.unknowns));pane.append(card);
 }
 list('仍需用户裁定的事项',i.human_questions);
 const analysis=d.analysis_context;pane.append(el('h4','原有估值、Odds 与价格上下文'));
 if(analysis.valuation){const spec=analysis.valuation.view.spec;
  pane.append(el('p',`估值时点 ${spec.valuation_date}；目标日期 ${spec.target_date||'不适用'}；${spec.currency}；${spec.method}。`),
   details('原估值条件与确切引用',analysis.valuation));
 }else pane.append(el('p','估值尚未建立；本次准备没有生成估值。'));
 if(analysis.odds)pane.append(details(analysis.odds.kind==='probability'?'原有概率 Odds 及确切引用':'原有条件结果及确切引用',analysis.odds));
 else pane.append(el('p','Odds 尚未建立；未补造概率或加权结果。'));
 if(analysis.price_context){const p=analysis.price_context.price;pane.append(
  el('p',`原价格 ${p.price} ${p.currency}；行情时点 ${p.as_of}。`),
  el('p','这是原结果的有日期上下文。本次准备未刷新价格，未认定它在准备时点仍可用，也不是当前可执行报价。'),
  details('原行情与概率资格、引用和限制',analysis.price_context));
 }else pane.append(el('p','价格上下文尚未建立。'));
 if(analysis.probability_assessment)pane.append(details('原概率判断、覆盖假设和风险政策',analysis.probability_assessment));
 if(d.data_gaps.length)list('尚未建立或仍缺少的部分',d.data_gaps);
 list('本次判断限制',i.limitations);
 for(const h of v.projection.horizons){const card=el('section',undefined,`decision-horizon${h.review_state==='review-due'?' decision-review-due':''}`);
  card.append(el('h4',h.label),el('p',`窗口 ${h.window_start} — ${h.window_end} · ${term(h.window_state)}`),
   el('p',`复核 ${h.review_at} · ${term(h.review_state)}`),el('p',`经济机制：${h.mechanism}`),el('p',`预期变化：${h.expected_change}`));
  for(const c of h.catalysts)card.append(el('p',`催化：${c.description}；${c.expected_at||'日期未知'}；${term(c.timing)}。`));
  for(const c of h.invalidation)card.append(el('p',`失效条件：${c.description}；尚未评价是否发生。`));
  if(h.limitations.length)card.append(details('该期限的限制',h.limitations));pane.append(card);
 }
 const history=el('div',undefined,'decision-history');history.append(el('h4','按确切前驱恢复的判断历史'));
 for(const row of v.history){const old=row.document.input;history.append(details(`${old.prepared_at} · ${term(old.stance)}`,{
  reference:row.reference,revision_note:old.revision_note,rationale:old.rationale,research_ref:old.research_ref,
  changes:row.document.changes,limitations:old.limitations,original_horizons:old.horizons,
  target:old.target,alternatives:old.alternatives,human_questions:old.human_questions,
  major_assumptions:old.major_assumptions,strongest_counterarguments:old.strongest_counterarguments,
  data_gaps:row.document.data_gaps,analysis_context:row.document.analysis_context}));}
 history.append(el('p','这里只显示选中准备包的完整前驱链，不宣称已发现所有准备包或最新全局判断。后稿移除的期限或问题保留在历史中，移除不代表已经解决。'));pane.append(history);
}
"""
