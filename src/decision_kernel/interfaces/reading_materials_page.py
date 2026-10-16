"""Material section for the one offline reading room; no network or active previews."""

JS = r"""
Object.assign(labels,{'retained':'已保管','verified':'已核验','failed':'未通过','not-retained':'未保管','not-requested':'未请求','unsupported':'通路暂不支持','rights-excluded':'权利排除','not-applicable':'不适用','not-declared':'未声明','verified-declared-inventory':'声明清单已核验','stored-representation':'已保管表示','external-only':'仅外部定位','report-original':'原报告','calculation-file':'计算文件','source-evidence':'来源证据','supporting-file':'辅助文件',snapshot:'完整叙事声明',delta:'增量稿声明'});
function materialPlan(ref, changes={}){
 return {...D.plan,reference:ref,materials:{reference:ref,source_limit:4,history_limit:2,...changes}};
}
function materialRequest(ref, changes={}){
 request('reading-room-materials.json',roomRequest(materialPlan(ref,changes)));
}
function renderMaterials(){
 const pane=$('materials-content'),panel=D.materials;
 pane.replaceChildren();
 pane.append(el('p','材料只按指定研究版本读取。原文件不在此执行；下载不代表已审阅、研究完成或更正被接受。来源不同表示不自动算独立证据。','muted'));
 if(!panel){pane.append(el('p','本页未请求材料。可从研究卡片导出材料阅读计划；执行前须单独确认材料公开权利。'));return;}
 if(panel.status!=='readable'){
  pane.append(el('p',`材料${label(panel.status)}：${panel.error}。其他研究仍可独立阅读。`),
   button('导出原交付读取请求',()=>request('delivery-read.json',{operation:'open-delivery',payload:{reference:panel.reference}})));return;
 }
 const v=panel.view,ref=v.research.reference,inv=v.inventory;
 pane.append(el('h3',`${v.research.case_id} / ${v.research.revision_id}`),
  el('p',`材料状态：${label(v.status)}；清单核验：${label(inv.status)}；正文模式：${label(inv.body_mode)||'未声明'}。核验只说明声明清单，不认证来源真实性。`),
  details('确切研究与原清单的核验范围',v.research));
 pane.append(el('h3','原附件及声明缺项'));
 if(!inv.items)pane.append(el('p','此研究没有交付清单；不推断没有原文件，也不补造原件。'));
 for(const item of inv.items||[]){
  const box=el('div',undefined,'question'),m=item.metadata;
  box.append(el('strong',m?m.name:item.id),el('p',`${label(item.role)} · 声明 ${label(item.status)} · 本次读取 ${label(item.readback)}`),el('p',item.reason||''));
  if(m)box.append(el('p',`原大小 ${m.size} 字节；SHA256 ${m.sha256}`,'muted'));
  if(item.open_request)box.append(button('导出该原文件读取请求',()=>request('artifact-read.json',item.open_request)),
   button('导出含该原文件的阅读计划',()=>materialRequest(ref,{...v.plan,attachment_id:item.id})));
  if(item.error)box.append(el('p',item.error));
  box.append(details('该项精确引用与限制',item));pane.append(box);
 }
 const a=v.attachment;
 if(a.status==='readable'){
  const m=a.metadata;
  pane.append(el('h3',`已取回原附件：${m.name}`),el('p',`${m.size} 字节，原字节已核验；仅下载，不在页面预览或执行。`));
  pane.append(button('下载已核验原附件',()=>{
   if(!/^[a-f0-9]{64}$/.test(m.sha256))return;
   const raw=atob(a.data_base64),bytes=Uint8Array.from(raw,c=>c.charCodeAt(0));
   const ext={'text/html':'.html','application/pdf':'.pdf','text/plain':'.txt','text/markdown':'.md','application/json':'.json','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'.xlsx'}[m.media_type]||'.bin';
   save('artifact-'+m.sha256.slice(0,16)+ext,bytes,'application/octet-stream');
  }));
 }else pane.append(el('p',`原附件下载：${label(a.status)}${a.error?' · '+a.error:''}。不会自动选另一文件替代。`));
 const s=v.source_page;
 pane.append(el('h3','来源表示与原版本分页'),el('p',`原研究共 ${s.total} 个来源引用；本页从 ${s.offset} 开始，返回 ${s.returned}，剩余 ${s.remaining}。未显示其他页不等于未取得。`));
 for(const row of s.items){
  const box=el('div',undefined,'question'),w=row.view;
  box.append(el('strong',`来源下标 ${row.index} · ${label(row.status)}`));
  if(w){
   const m=w.manifest.input;
   box.append(el('p',`${m.title} · 保管表示 ${m.custody}`),el('p',`取得：${m.acquired_at||'未标'}；披露：${m.published_at||'未标'}；期间：${m.report_period||'未标'}`),
    el('p',(w.limitations||[]).join('；')),details('来源版本、行列选择与外部定位',{reference:row.reference,origin:m.origin,original_verified:w.original_verified}));
   if(w.data!==null){box.append(details('本页实际来源表示（字面显示，不执行）',w.data),
    button('下载此来源表示 JSON',()=>save('source-'+row.index+'.json',JSON.stringify(w,null,2),'application/json')));}
   else box.append(el('p','仅外部定位，没有可恢复的来源正文；不会访问该网址补抓。'));
  }else box.append(el('p',row.display_error||row.error||'本页没有可展示的来源表示。'));
  box.append(button('导出确切来源读取请求',()=>request('source-read.json',row.open_request)));pane.append(box);
 }
 if(v.next_source_cursor)pane.append(button('导出下一来源页的阅读计划',()=>materialRequest(ref,{...v.plan,source_cursor:v.next_source_cursor})));
 pane.append(el('h3','前版、增量基稿与修订导航'),el('p','以下仅是确切前驱关系，不把每次修订都认作已接受的更正。历史判断不自动适用于现在。'));
 if(inv.base_revision)pane.append(button('查看声明的增量基稿',()=>materialRequest(inv.base_revision)));
 for(const row of v.history){
  const box=el('div',undefined,'question');
  box.append(el('strong',row.revision_id||row.reference.path),el('p',row.status));
  if(row.status==='read')box.append(el('p',`原正文 ${row.body_size} 字节 · ${row.body_sha256}`),
   button('导出此前版的阅读与材料计划',()=>materialRequest(row.reference)),details('前驱原记录身份',row));
  else box.append(el('p',row.error));pane.append(box);
 }
 if(v.next_history_reference)pane.append(button('导出更早前版的阅读计划',()=>materialRequest(v.next_history_reference)));
 if(!v.history.length&&!v.next_history_reference)pane.append(el('p','没有声明前驱；这不证明是完整研究或已完成更正。'));
 if(inv.errors?.length)pane.append(details('未通过的清单核验',inv.errors));
}
renderMaterials();
"""
