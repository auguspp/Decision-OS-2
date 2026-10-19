"""Human reading overview and a literal, bounded document projection.

The original Markdown remains the downloadable source. DOM text nodes render
headings, paragraphs, lists and simple tables; embedded HTML is never executed.
"""

CSS = r"""
.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.overview-card{border:1px solid #dce4e8;border-radius:6px;padding:16px;background:#f7fafb}.overview-card h3{margin:0 0 8px}.overview-card p{margin:5px 0}.overview-card .metric{font-size:23px;color:#15394b;font-weight:650}.overview-card a{font-weight:600}.followups{padding:0;list-style:none}.followups li{border-top:1px solid #e0e8eb;padding:12px 0}.followups button{display:block;text-align:left;background:none;border:0;padding:0;color:#075c76}.document{font:16px/1.9 system-ui,sans-serif;max-width:940px;margin:24px auto;overflow-wrap:anywhere}.document p{white-space:pre-line}.document h2,.document h3,.document h4{color:#15394b;margin-top:1.6em}.document h2{font-size:25px}.document h3{font-size:21px}.document h4{font-size:18px}.document blockquote{margin-left:0;padding:8px 18px;border-left:3px solid #85a6b3;background:#f6f9fa}.document-table{overflow-x:auto;margin:18px 0}.document table{width:100%;border-collapse:collapse;font-size:14px;min-width:460px}.document th,.document td{padding:9px 12px;border:1px solid #dce4e8;vertical-align:top;text-align:left}.document th{background:#eff5f7}.document pre{max-height:none}.banner[data-status=readable]{border-color:#277464;background:#eef7f3}#brief h2{margin-top:0}#provenance{background:#f7fafb}@media(max-width:1000px){.overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:520px){.overview-grid{grid-template-columns:1fr}.overview-card{padding:13px}.document{font-size:15px}.document h2{font-size:22px}}
"""

JS = r"""
function readingDocument(markdown){
 const root=el('div',undefined,'document'),lines=markdown.split(/\r?\n/);
 if(lines.length>10000){root.append(el('pre',markdown,'body'));return root;}
 const inline=(n,text)=>{for(const piece of text.split(/(\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g)){
  if(piece.startsWith('**')&&piece.endsWith('**')){n.append(el('strong',piece.slice(2,-2)));continue;}
  const match=piece.match(/^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)$/);
  if(match){try{const url=new URL(match[2]);if(['http:','https:'].includes(url.protocol)&&url.hostname){const a=el('a',match[1]);a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';n.append(a);continue;}}catch(_){}}
  n.append(document.createTextNode(piece));}return n;};
 const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split(/(?<!\\)\|/).map(x=>x.trim().replace(/\\\|/g,'|'));
 const separator=line=>line.includes('|')&&cells(line).every(x=>/^:?-{3,}:?$/.test(x));
 let i=0;
 while(i<lines.length){
  const line=lines[i];if(!line.trim()){i++;continue;}
  if(/^\s*```/.test(line)){const part=[];i++;while(i<lines.length&&!/^\s*```/.test(lines[i]))part.push(lines[i++]);if(i<lines.length)i++;root.append(el('pre',part.join('\n')));continue;}
  const heading=line.match(/^(#{1,6})\s+(.+)$/);
  if(heading){root.append(inline(el('h'+Math.min(heading[1].length+1,4)),heading[2]));i++;continue;}
  if(i+1<lines.length&&line.includes('|')&&separator(lines[i+1])&&cells(line).length<=32){
   const wrap=el('div',undefined,'document-table'),table=el('table'),head=el('tr');
   for(const value of cells(line))head.append(inline(el('th'),value));table.append(head);i+=2;
   while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const tr=el('tr');for(const value of cells(lines[i++]))tr.append(inline(el('td'),value));table.append(tr);}wrap.append(table);root.append(wrap);continue;
  }
  if(/^\s*([-*+]\s+|\d+\.\s+)/.test(line)){
   const ordered=/^\s*\d+\.\s+/.test(line),list=el(ordered?'ol':'ul'),pattern=ordered?/^\s*\d+\.\s+/:/^\s*[-*+]\s+/;
   while(i<lines.length&&pattern.test(lines[i])){const source=lines[i++],item=inline(el('li'),source.replace(pattern,''));if(ordered)item.value=Number(source.match(/^\s*(\d+)\./)[1]);list.append(item);}root.append(list);continue;
  }
  if(/^>\s?/.test(line)){root.append(inline(el('blockquote'),line.replace(/^>\s?/,'')));i++;continue;}
  if(/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)){root.append(el('hr'));i++;continue;}
  const paragraph=[line];i++;
  while(i<lines.length&&lines[i].trim()&&!/^(#{1,6}\s|>\s?|\s*```|\s*[-*+]\s|\s*\d+\.\s)/.test(lines[i])&&!(i+1<lines.length&&lines[i].includes('|')&&separator(lines[i+1])))paragraph.push(lines[i++]);
  root.append(inline(el('p'),paragraph.join('\n')));
 }
 return root;
}
function renderOverview(){
 const target=$('overview-content');if(!target)return;
 const grid=el('div',undefined,'overview-grid');
 const card=(title,metric,text,hash)=>{const box=el('article',undefined,'overview-card');box.append(el('h3',title),el('p',metric,'metric'),el('p',text));const a=el('a','查看'+title);a.href=hash;box.append(a);grid.append(box);};
 const b=D.brief,discovery=D.brief_discovery;
 const ageText=age=>!age?'时间关系未取得':age.status==='read-clock-before-cutoff'?'阅读时钟早于简报截止，不能据此判断更新时效':`内容截止距本次阅读 ${Math.floor(age.age_seconds/3600)} 小时${age.status==='older-than-24h'?'，已有一段时间未更新':''}`;
 const briefName=discovery?.status==='selected'?'最新已存简报':'本页简报';
 card(briefName,b.status==='readable'?b.view.plan.cutoff.slice(0,10):b.status==='no-saved-brief'?'尚未保存':label(b.status),b.status==='readable'?ageText(b.age):'可用性未确认时保留原状态；其他内容仍可阅读。','#brief');
 card('研究',`${D.catalogue.total_cases} 个案例`,`本页列出 ${D.catalogue.returned} 个；版本及原问题可在下方继续查看。`,'#research');
 const questions=D.agenda.status==='readable'?D.agenda.view.cards.flatMap(c=>(c.agenda||[]).map(q=>({card:c,question:q}))):null;
 const due=questions?.filter(x=>['due-for-review','overdue-for-review'].includes(x.question.attention_status));
 card('待复核',due?`${due.length} 个问题`:'状态未知',due?'仅统计本页与显式研究版本的已到期条件；到期不代表事件已发生。':'议程尚未成功读回，无法判断本页待办。','#agenda');
 const market=D.market,restored=market?.status==='readable'?market.view:null,v=restored?.view;
 const marketDates=v?.as_of_by_exchange?Object.entries(v.as_of_by_exchange).map(([name,day])=>`${name} ${day||'未取得'}`).join(' / '):v?.raw?.as_of;
 const marketCoverage=v?.raw?.scope_count==null?'完整范围未取得':`原价完整窗口 ${v.raw.observed_count??'未知'} / ${v.raw.scope_count}；复权 ${v.qfq.observed_count??'未知'} / ${v.qfq.scope_count}`;
 const freshness={'unknown':'当前时效未知','stale':'尚未覆盖最近交易日','current-through-completed-day':'已覆盖读取时点所需交易日'}[v?.freshness?.status]||'按原版本日期阅读';
 card('A 股行情',marketDates||'尚未接入本页',v?`${marketCoverage}。${freshness}。`:'接入后这里显示实际数据日期与覆盖；未提供不代表市场没有变化。','#market');
 target.append(grid);
 const followups=questions?.filter(x=>['focus','due-for-review','overdue-for-review','declared-followup'].includes(x.question.attention_status))||[];
 if(followups.length){
  target.append(el('h3','待继续的问题'));const list=el('ul',undefined,'followups');
  for(const x of followups.slice(0,4)){const item=el('li');item.append(button(x.question.text,()=>{const c=cards.find(row=>same(row.reference,x.card.reference));if(c){selected=cardKey(c);$('query').value='';$('kind').value='';render();location.hash='research';}else location.hash='agenda';}),el('small',`${x.card.case_id||'已存研究'} · ${label(x.question.attention_status)}`));list.append(item);}
  target.append(list,el('p',`显示本页 ${followups.length} 个可接续问题中的前 ${Math.min(4,followups.length)} 个；顺序沿原议程，不作投资排序。`,'muted'));
 }
 if(discovery){
  const text=discovery.status==='selected'?`已核正常发行目录 ${discovery.scanned} / ${discovery.total_candidates} 个版本，按交付截止找到本页简报。${discovery.same_cutoff_count>1?`同一截止有 ${discovery.same_cutoff_count} 份，当前按固定引用顺序打开；不推断哪份后保存。`:''}`:discovery.status==='no-saved-brief'?'正常发行目录尚无已存简报；自定义名称的历史附件仍可用原引用打开。':'最新简报发现未完成，未用较旧版本冒充最新。可读候选及缺口见读取范围。';
  $('brief-content').prepend(el('p',text,'muted'));
  const choices=el('details');choices.append(el('summary','其他已存简报与发现范围'));
  for(const candidate of discovery.choices||[]){const scope=candidate.research_cases_in_plan?.length?` · 原研究范围：${candidate.research_cases_in_plan.join('、')}`:'';choices.append(button(`${candidate.cutoff} · ${{morning:'晨间',evening:'晚间',adhoc:'即时'}[candidate.kind]||candidate.kind}${scope}`,()=>request('reading-room-brief-version.json',roomRequest({...D.plan,brief:candidate.reference}))));}
  choices.append(details('快照、覆盖与确切引用',discovery));$('brief-content').append(choices);
 }
 const latest={...(D.refresh_plan||D.plan),catalogue:{limit:D.plan.catalogue.limit},brief:{select:'latest-saved'}};
 $('provenance').append(button('导出重新查找最新简报的阅读请求',()=>request('reading-room-latest.json',roomRequest(latest))));
 for(const name of ['industry','materials','review'])if(!D[name]){const section=$(name);if(section)section.hidden=true;const link=document.querySelector(`nav a[href="#${name}"]`);if(link)link.hidden=true;}
}
renderOverview();
"""
