"""Literal author Brief and exact adoption navigation in the existing Room."""

JS = r"""
function renderAiBrief(){
 const panel=D.ai_brief,target=$('ai-brief-content');if(!panel||!target)return;
 const statusName=status=>({'no-saved-handoff':'最近七天尚无已存 AI 晚报',adopted:'本 AI 晚报已采用','not-adopted-in-this-ai-brief':'本 AI 晚报未采用此确切版本','cannot-determine':'无法判断','review-required':'需要复核','first-trigger':'首次触发'}[status]||label(status));
 const exact=(reference)=>roomRequest({...D.plan,ai_brief:reference});
 const openResearch=reference=>{const card=cards.find(row=>same(row.reference,reference));if(card){selected=cardKey(card);$('query').value='';$('kind').value='';render();location.hash='research';}else request('reading-room-ai-research.json',roomRequest({...D.plan,reference}));};
 target.append(el('p','作者正文与已存资料简报分别保管。本页只读，不生成晚报、不运行 Watch，也不记录人工投资决定。','muted'));
 if(panel.status==='readable'){
  const view=panel.view,doc=view.document;
  target.append(el('p',`AI 晚报 · 执行日期 ${doc.execution_date} · 原内容截止 ${doc.cutoff}`));
  if(panel.age?.status==='read-clock-before-cutoff')target.append(el('p','阅读时钟早于原内容截止；这不是当时已知或当日更新的证明。','banner'));
  else if(panel.age?.status==='older-than-24h')target.append(el('p','原内容截止距本次阅读已超过 24 小时；最新已存不等于今天新生成。','muted'));
  target.append(el('p',doc.scope));
  const actions=el('div',undefined,'actions');
  actions.append(button('下载 AI 晚报原正文',()=>save('ai-brief.md',doc.body,'text/markdown')),
   button('导出 AI 晚报确切读取请求',()=>request('ai-brief-read.json',panel.open_request)));
  if(doc.baseline)actions.append(button('打开原 AI 晚报基线',()=>request('reading-room-ai-baseline.json',exact(doc.baseline))));
  target.append(actions,readingDocument(doc.body));
  if(doc.gaps.length){target.append(el('h3','原稿保留的资料与执行缺口'));const gaps=el('ul');for(const gap of doc.gaps)gaps.append(el('li',gap));target.append(gaps);}
  target.append(el('h3','实际采用的研究与 Watch'));
  if(!doc.adoptions.length&&!doc.watch_adoptions?.length)target.append(el('p','本份正文没有登记已存研究或 Watch 检查采用；不由同公司、同页展示推断采用。'));
  for(const adoption of doc.adoptions){
   const item=view.research.find(row=>same(row.reference,adoption.reference)),box=el('div',undefined,'question');
   box.append(button(`查看采用研究 ${item?.case_id||adoption.reference.path} / ${item?.revision_id||'确切版本'}`,()=>openResearch(adoption.reference)),el('p',adoption.used_change),details('原研究引用与正文摘要',adoption));target.append(box);
  }
  for(const adoption of doc.watch_adoptions||[]){
   const row=view.watch_evidence_adoption.find(item=>same(item.selection.check_ref,adoption.check_ref)&&same(item.selection.reference,adoption.reference)&&item.selection.question_id===adoption.question_id&&item.selection.kind===adoption.kind),box=el('div',undefined,'question');
   box.append(el('h4',adoption.question_id?`原 Full 问题：${adoption.question_id}`:'原 Full Odds 的价格条件'),el('p',adoption.used_change));
   if(row)box.append(el('p',`原检查 ${row.recorded_at} · ${statusName(row.result?.status||row.status)} · 已存结果重演 ${row.recalculation}`),button('导出原 Watch 检查读取请求',()=>request('ai-brief-watch-check.json',row.open_request)));
   box.append(button('查看 Watch 绑定的 Full',()=>openResearch(adoption.reference)),details('精确采用与原检查', {adoption,evidence:row}));target.append(box);
  }
  target.append(details('原文摘要、保管引用与阅读边界',{reference:panel.reference,body_sha256:view.body_sha256,baseline:doc.baseline,snapshot:panel.snapshot,human_acceptance:view.human_acceptance,economic_quality:view.economic_quality,persistence:panel.persistence}));
 }else{
  target.append(el('p',`AI 晚报：${statusName(panel.status)}。${panel.error||'缺少已存对象或完整发现证据，不能据此判断任务未运行。'}`));
  if(panel.open_request)target.append(button('导出 AI 晚报确切读取请求',()=>request('ai-brief-read.json',panel.open_request)));
 }
 if(panel.discovery){
  const choices=el('details');choices.append(el('summary','AI 晚报发现范围与确切候选'));
  if(panel.discovery.status==='partial')choices.append(el('p','发现存在缺口；以下候选不被标为无缺口的最新晚报。'));
  for(const item of panel.discovery.choices||[])choices.append(button(`读取已存 AI 晚报 ${item.execution_date} · ${item.cutoff}`,()=>request('reading-room-ai-version.json',exact(item.reference))));
  choices.append(details('七天范围、候选数量与失败',panel.discovery));target.append(choices);
 }
 const latest={...(D.refresh_plan||D.plan),catalogue:{limit:D.plan.catalogue.limit},ai_brief:{select:'latest-ai-brief'}};
 target.append(button('导出重新查找最新 AI 晚报的请求',()=>request('reading-room-ai-latest.json',roomRequest(latest))));
 const overview=$('overview-content'),grid=overview?.children[0];
 if(grid){const card=el('article',undefined,'overview-card'),link=el('a','查看 AI 晚报');link.href='#ai-brief';card.append(el('h3','AI 晚报'),el('p',panel.status==='readable'?panel.view.document.execution_date:statusName(panel.status),'metric'),el('p',panel.status==='readable'?`原内容截止 ${panel.view.document.cutoff}`:'已保存作者正文的状态独立于资料简报。'),link);grid.append(card);}
}
renderAiBrief();
"""
