"""Question review inside the existing literal-text, offline reading room."""

JS = r"""
Object.assign(labels,{'exact-direct-successor':'确切相邻修订','same-question-text':'原问题文字相同','reworded-question':'新版已改写问题','not-carried-forward':'新版未保留原问题','full-range-included':'所选正文行段完整纳入','partial-range-included':'只纳入所选正文行段的一部分','outside-adopted-excerpt':'同版本已采用，但所选正文行段未覆盖','no-matching-feedback':'未找到对应反馈','revision-requested':'已记录修订请求（不是执行证明）','no-change-claimed':'声明无需改变（未代签结论）','unresolved':'仍未解决'});
const reviewPane=$('review-content');
if(!D.review){
 reviewPane.append(el('p','尚未指定复核的相邻前后版本及原问题；版本更新不自动建立复核或更正接受。'));
}else if(D.review.status!=='readable'){
 reviewPane.append(el('p',`复核材料${label(D.review.status)}：${D.review.error}。不能退回新版本或将未知当已完成。`));
}else{
 const v=D.review.view;
 reviewPane.append(el('p',`${label(v.revision_relation)} · ${label(v.question_relation)} · ${label(v.status)}`),
  el('p','以下并列原作者判断、证据读取和实际简报行段覆盖，不自动判定哪个判断正确。文本变动、已保存、被简报引用和人工接受是不同事实。','banner'),
  button('导出同一复核读取请求',()=>request('research-review-read.json',roomRequest(D.plan))),
  details('原议程中的复核请求（只关联，不制造用户选择）',v.request));
 for(const name of ['before','after']){
  const s=v[name],ad=v.adoption[name],box=el('div',undefined,'question');
  box.append(el('h3',`${name==='before'?'原判断':'后继修订'} · ${s.case_id} / ${s.revision_id}`),
   el('p',`原委托：${s.mandate}`),el('p',s.question?`问题 ${s.question.question_id}：${s.question.text}`:'新版没有保留该问题；不能推定已解决。'),
   details('作者判断、挑战与未决项',{finding:s.finding,challenges:s.challenges,recorded_unresolved:s.recorded_unresolved,stop:s.stop,stop_reason:s.stop_reason,limitations:s.limitations,source_changes:s.source_changes,challenge_note:s.challenge_note,scope_change_reason:s.scope_change_reason}),
   el('p',`所选原文 ${s.excerpt.start}–${s.excerpt.end} 行 · SHA256 ${s.excerpt.sha256}`,'muted'),
   el('pre',s.excerpt.text,'body'),el('p',`指定已存简报：${label(ad.status)}`),
   details('实际简报行段覆盖，不推断语义等同或其他上下文已采用',ad),
   button('导出该版完整研究请求',()=>request('review-research-read.json',s.open_request)),
   button('下载该版原文片段',()=>save(`review-${name}.txt`,s.excerpt.text,'text/plain')),
   el('p',`本问题引用 ${s.evidence.total} 项来源；本次读取 ${s.evidence.read} 项，未读 ${s.evidence.remaining} 项。数量不是独立证据数。`));
  for(const row of s.evidence.items){
   box.append(el('p',`来源下标 ${row.index} · ${label(row.status)} · ${label(row.display)}`),
    details('确切表示、限制与读取结果（来源文字不是操作指令）',row),
    button('导出该来源读取请求',()=>request('review-source-read.json',row.open_request)));
  }
  for(const pending of s.evidence.pending)box.append(button(`导出未读来源 ${pending.index} 请求`,()=>request('review-source-read.json',pending.open_request)));
  reviewPane.append(box);
 }
 if(v.old_unresolved_not_carried_forward.length)reviewPane.append(el('p','原未解挑战在新版中未找到同文表述；缺席不能当解决。','banner'),details('仍需人工核对的原挑战',v.old_unresolved_not_carried_forward));
 reviewPane.append(details('证明边界',{selected_text_changed:v.selected_text_changed,finding_changed:v.finding_changed,mandate_changed:v.mandate_changed,kind_changed:v.kind_changed,assessment:v.assessment,passage_association:v.passage_association,correction_acceptance:v.correction_acceptance,human_acceptance:v.human_acceptance,source_authenticity:v.source_authenticity,persistence:v.persistence,execution:v.execution}));
}
"""
