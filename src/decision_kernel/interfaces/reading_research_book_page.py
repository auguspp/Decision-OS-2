"""Research Book in the original offline Room, using its literal DOM helpers."""

CSS = r"""
.research-book{margin:20px 0;padding:16px;border:1px solid #c3d5da;border-radius:6px;background:#f7fafb}.book-finding{padding:10px 0;border-bottom:1px solid #dce4e8}.book-review-ranges{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.book-review-ranges input{margin-bottom:0}@media(max-width:720px){.book-review-ranges{grid-template-columns:1fr}}
"""

JS = r"""
function researchBookPlan(changes={}){
 const plan={...D.plan,...changes},c=D.catalogue;
 if(c.repository&&c.snapshot&&Number.isInteger(c.offset))plan.catalogue={limit:D.plan.catalogue.limit,cursor:{
  schema:'research-catalogue-cursor-v1',repository:c.repository,snapshot:c.snapshot,offset:c.offset}};
 return plan;
}
function bookLabel(value){return ({answered:'已有回答',reframed:'原问题已重述',open:'仍未回答',continue:'继续',blocked:'受阻',concluded:'原作者声明收口',
 'acquired-unanalysed':'资料已得但尚待分析','disclosed-unverified':'已披露但尚待核实','currently-unidentifiable':'目前无法识别','future-event':'待未来事件',interrupted:'执行中断',
 changed:'因挑战改变判断','retained-with-reason':'说明理由后保留判断',unresolved:'挑战尚未解决'})[value]||value;}
function bookEntry(reference){return D.research_book?.entries?.find(e=>same(e.reference,reference));}
function bookEvidence(box,entry,indices){
 box.append(el('p',indices.length?`原证据下标：${indices.join('、')}。引用数量不代表独立证据数量。`:'本项未记录证据下标。','muted'));
 for(const index of indices){const ref=entry.sources[index];if(ref)box.append(button(`导出证据来源 ${index} 请求`,()=>request('research-book-source-'+index+'.json',{operation:'read-source',payload:ref})));}
}
function bookReview(box,entry){
 const before=bookEntry(entry.predecessor),old=D.readings.find(r=>same(r.reference,entry.predecessor)),current=D.readings.find(r=>same(r.reference,entry.reference));
 if(!entry.predecessor){box.append(el('p','未声明前驱；不推断已完成研究或更正。','muted'));return;}
 if(!before?.workpaper||!old?.body||!current?.body||!entry.workpaper){
  box.append(el('p','逐题 Review 需要本页已打开的直接前驱与当前版工作底稿；此页不自动追读历史。','muted'));return;
 }
 if(before.case_id!==entry.case_id||before.review_lines.initial_line===null||entry.review_lines.initial_line===null){
  box.append(el('p','当前两版没有可直接导出的原问题与合规原文行段，保留原文入口。'));return;
 }
 const area=el('details'),questions=el('select');questions.id='research-book-review-question';
 for(const q of before.workpaper.questions){const option=el('option',`${q.question_id}：${q.text}`);option.value=q.question_id;questions.append(option);}
 questions.value=before.workpaper.questions[0].question_id;
 const qlabel=el('label','前驱原问题');qlabel.append(questions);
 area.append(el('summary','对照本页已打开的直接前驱'),el('p','选择原问题和两版原文行段后导出。预填各版第一个合规非空行，未替你判断段落重要性或语义对应。每版最多80行，原宿主会再次核原文及材料权限。'),qlabel);
 const grid=el('div',undefined,'book-review-ranges'),fields={};
 for(const [side,title,detail] of [['before','前驱',before],['after','当前版',entry]])for(const [edge,word] of [['start','起行'],['end','止行']]){
  const input=el('input');input.type='number';input.min='1';input.max=String(detail.review_lines.total);input.step='1';input.value=String(detail.review_lines.initial_line);
  input.id='research-book-review-'+side+'-'+edge;fields[side+'_'+edge]=input;
  const l=el('label',`${title}${word}（原正文共${detail.review_lines.total}行）`);l.append(input);grid.append(l);
 }
 area.append(grid,button('导出所选问题 Review 请求',()=>{
  const items={};
  for(const [side,detail] of [['before',before],['after',entry]]){
   const start=Number(fields[side+'_start'].value),end=Number(fields[side+'_end'].value);
   if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start<1||end<start||end-start>=80||end>detail.review_lines.total){$('notice').textContent='行段超出原文或80行界限；未导出请求。';return;}
   items[side]={reference:detail.reference,start,end};
  }
  request('research-book-review.json',roomRequest(researchBookPlan({reference:entry.reference,review:{...items,question_id:questions.value,evidence_limit:2}})));
 }));box.append(area);
}
function renderResearchBook(pane,card){
 const panel=D.research_book,entry=bookEntry(card.reference),box=el('section',undefined,'research-book');box.id='research-book-detail';
 box.append(el('h3','Research Book · 作者判断与接续'));
 if(!panel||panel.status==='not-included'||!entry){
  box.append(el('p',`Book 投影未包含（not-included）：${panel?.error||'本模型没有可用的 Book 投影'}。原正文及确切读取入口继续保留。`));pane.append(box);return;
 }
 box.append(el('p',entry.origin==='explicit-reference'?'显式确切版本；不计当前目录案例分母，也不判断是否仍为当前版。':'来自本页固定目录快照；页面搜索只筛本页。','muted'));
 if(entry.status!=='readable'&&entry.status!=='partial'){
  box.append(el('p',`Book ${label(entry.status)}：${entry.error||'原研究不可读'}。未重写或截断正文。`));pane.append(box);return;
 }
 box.append(el('p',`原委托：${entry.mandate}`),el('p',`${entry.kind==='full'?'Full':entry.kind==='quick'?'Quick':entry.kind} · ${entry.case_id} / ${entry.revision_id}`),
  el('p',`本版保存于 ${entry.stored_at}；保存时间不代替研究事实截止。`,'muted'),
  el('p','标准公司标识：未建立。研究事实截止：未建立。“最强反证”排序：未建立；挑战沿作者原顺序。','muted'),
  el('p','以下是原作者已保存的判断与停止声明；页面未执行研究、判定质量或记录人工接受。','banner'));
 const actions=el('div',undefined,'actions');
 actions.append(button('导出 Book 材料阅读请求',()=>request('research-book-materials.json',roomRequest(researchBookPlan({reference:entry.reference,materials:{reference:entry.reference,source_limit:4,history_limit:2}})))),
  button('查看原议程',()=>{location.hash='agenda';}),button('下载本版 Book 投影 JSON',()=>save('research-book-entry.json',JSON.stringify(entry,null,2),'application/json')));
 if(D.brief.status==='readable')actions.append(button('导出具名 Brief 读取请求',()=>request('research-book-brief.json',{operation:'read-desk',plan:{reference:D.brief.reference},save:false})));
 box.append(actions,el('p',`仅对指定具名 Brief 的版本采用关系：${label(entry.brief_relation?.status||'unknown')}。不代表所有 Brief 的采用历史。`));
 const agenda=entry.agenda_card_index===null?null:D.agenda.view?.cards?.[entry.agenda_card_index];
 box.append(el('p',agenda?`原议程本版状态：${label(agenda.status)}；具体问题选择与复核日期见“原议程”。`:`本版议程关系：${label(D.agenda.status)}；未补造用户选择。`));
 const w=entry.workpaper;
 if(!w){box.append(el('p',entry.workpaper_status==='invalid'?'工作底稿校验未通过；结构化判断未展示，原正文仍可读。':'旧正文未记录规范工作底稿；问题、findings、挑战、next_action、remaining_kind、stop及来源变化字段均未建立。'),details('原记录的未决项',entry.recorded_unresolved));}
 else{
  box.append(el('h4','原问题'));
  for(const q of w.questions)box.append(el('p',`${q.question_id} · ${q.text}`));
  box.append(el('h4','作者 findings（原顺序）'));
  for(const f of w.findings){const line=el('div',undefined,'book-finding');line.append(el('strong',`${f.question_id} · ${bookLabel(f.status)}`),el('p',f.finding));
   if(f.status==='open')line.append(el('p',`剩余类型：${bookLabel(f.remaining_kind)}；原重要性声明：${f.material?'重要':'非重要'}`));
   if(f.next_action)line.append(el('p',`原下一步：${f.next_action}`));bookEvidence(line,entry,f.evidence);box.append(line);}
  box.append(el('h4',`作者挑战（原顺序，共${w.challenges.length}项）`));
  for(const challenge of w.challenges){const line=el('div',undefined,'book-finding');line.append(el('strong',`${challenge.question_id} · ${bookLabel(challenge.disposition)}`),el('p',challenge.argument),el('p',`原回应：${challenge.response}`));bookEvidence(line,entry,challenge.evidence);box.append(line);}
  if(!w.challenges.length)box.append(el('p','没有记录结构化挑战条目；不推断没有反证。'));
  box.append(el('h4','原停止声明与资料变化'),el('p',`${bookLabel(w.stop)} · ${w.stop_reason}`),
   el('p',`来源变化：${w.source_changes||'原字段为空，未推断无变化'}`),el('p',`挑战说明：${w.challenge_note||'原字段为空'}`),
   el('p',`范围变化理由：${w.scope_change_reason||'原字段为空'}`),details('原限制及未决项',{limitations:w.limitations,recorded_unresolved:entry.recorded_unresolved}));
 }
 const sources=el('details');sources.append(el('summary',`原来源引用（${entry.sources.length}项；Book未额外读取正文）`));
 for(const [index,ref] of entry.sources.entries()){
  const existing=card.source_page?.items?.find(row=>row.index===index);
  sources.append(el('p',`来源 ${index} · ${existing?label(existing.status):'本目录页未提供该来源的读取状态'}`),
   button(`导出原来源 ${index} 读取请求`,()=>request('research-book-source-'+index+'.json',{operation:'read-source',payload:ref})),details('原 SourceRef '+index,ref));
 }
 box.append(sources);bookReview(box,entry);pane.append(box);
}
"""
