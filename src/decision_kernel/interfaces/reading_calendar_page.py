"""Literal event dates and retained followups in the existing offline room."""

CSS = r"""
.calendar-event{border-top:1px solid #dce4e8;padding:14px 0}.calendar-dates{display:flex;flex-wrap:wrap;gap:12px}
"""

JS = r"""
function renderCalendar(){
 const pane=$('event-calendar-content'),part=D.event_calendar;if(!pane||!part)return;
 const term=x=>({'upcoming':'计划窗口未到','window-open':'计划日期窗口内','due-no-actual-evidence':'到期，尚无实际发生证据','date-unknown':'日期未知','source-reported-cancelled':'来源报告取消','source-reported-occurred':'来源报告实际发生','temporal-uncertainty':'时间资格未确定','version-ordering-unknown':'版本先后未确定','schedule-changed':'来源计划日期变化','date-became-unknown':'后版日期未明','source-status-changed':'来源状态变化','ordering-unknown':'先后不可确定','unchanged':'所选字段未变'}[x]||label(x));
 const when=f=>!f||f.presence!=='value'?(f?.presence==='null'?'原字段为空':'来源未提供'):f.value;
 if(part.status!=='readable'){pane.append(el('p',`事件日历${label(part.status)}：${part.error}。不能据此认为没有事件。`,'banner'));return;}
 const v=part.view;
 pane.append(el('p',v.scope_note),el('p',`读取 ${v.scope.read_inputs}/${v.scope.declared_inputs} 个指定来源，${v.scope.distinct_read_events} 个已读事件。到期只提示查证，预约与实际发生分别保留。`,'banner'),details('完整范围、缺失输入和版本', {scope:v.scope,inputs:v.input_outcomes,version:v.view_version,time_basis:v.time_basis}));
 const query=el('input');query.id='calendar-subject';query.placeholder='精确主体标识';query.value=part.plan.subject_id||'';
 const lab=el('label','按主体重新读取');lab.htmlFor=query.id;
 pane.append(lab,query,button('导出主体读取请求',()=>{const id=query.value.trim();if(id&&!/^[a-z0-9][a-z0-9_-]{0,95}$/.test(id)){$('notice').textContent='请输入来源中的确切主体标识。';return;}request('event-calendar-room.json',roomRequest({...D.plan,event_calendar:{...part.plan,subject_id:id||null}}));}));
 for(const e of v.events){
  const box=el('article',undefined,'calendar-event');box.append(el('h3',`${e.subject_name||e.subject_id||''} · ${e.title||e.event_id}`),el('p',term(e.state),'badge'));
  if(e.current){const c=e.current;box.append(el('p',`计划：${when(c.scheduled_for)}；实际：${when(c.actual_at)}；来源公布：${when(c.announced_at)}；时区：${c.timezone}。`),el('p',`期间 ${c.period||'未声明'} · ${c.date_interpretation}`,'muted'));}
  if(e.state==='due-no-actual-evidence')box.append(el('p','计划窗口已到，需要取得实际披露或事件结果；不能从日期经过推断事件发生。'));
  if(e.state==='source-reported-occurred')box.append(el('p','日期来源报告已发生；本次没有据此认证公告原文已读或研究已经复核。'));
  for(const change of e.transitions||[])box.append(el('p',`版本变化：${term(change.status)}${change.previous_date?' · '+when(change.previous_date)+' → '+when(change.next_date):''}`));
  const originals=new Map();
  for(const version of e.versions||[])for(const original of [version,...(version.evidence||[])]){
   if(original.open_request)originals.set(JSON.stringify(original.open_request.payload),original.open_request);
  }
  for(const original of originals.values())box.append(button('导出精确日历原件读取请求',()=>request('calendar-source-read.json',original)));
  for(const f of e.followups||[]){box.append(el('h4',f.question||'对应的原研究'),el('p',f.quote));if(f.status==='readable')box.append(button('导出原研究接续请求',()=>request('calendar-research.json',f.open_request)));else box.append(el('p',`原研究关系${label(f.status)}：${f.error}`));}
  box.append(details('来源版本、原日期字段与明确缺口',e));pane.append(box);
 }
 if(!v.events.length)pane.append(el('p','本次过滤没有匹配的已读事件；完整输入范围和未读取项仍见上方。'));
 pane.append(details('后续研究读取结果',v.followup_outcomes));
}
"""
