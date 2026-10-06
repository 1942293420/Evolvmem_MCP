/* Selected raw material -> reviewed cleaning draft -> project classification. */
window.EvolvCleaning={async render({mount,api,esc,toast,onDirty,onBusy}){
 let [skill,queue]=await Promise.all([api('skills/cleaning'),api('cleaning')]);
 let page=1,rows=queue.items,total=queue.total,busy=false,ruleDirty=false;
 const selected=new Set(),drafts=new Map(),errors=new Map();
 const $=s=>mount.querySelector(s),categories={habit:'长期习惯',project_convention:'项目约定',task_requirement:'任务要求',environment:'环境事实',decision:'决策依据',experience:'技术经验',reference:'参考资料'};
 const errorText=e=>({cleaning_delete_decision_required:'请先决定保留还是删除这条资料。',revision_conflict:'资料或规则已变化，请刷新、核对后重试。',cleaning_source_deleted:'资料已被永久删除。',cleaning_source_referenced:'这份资料已有知识、经验或摘要引用，请先核对引用关系。',cleaning_delete_failed:'部分文件未能删除，请刷新核对后重试。',cleaning_source_unavailable:'原文暂不可读取，可手动补充清洗稿。',cleaning_preview_timeout:'本次预览时间已用完，请减少选择后重试。',cleaning_model_failed:'模型暂时不可用，原文和现有修改已保留。',cleaning_bad_response:'这条资料的 AI 清洗结果不完整，请重试或手动整理。',classification_no_longer_unassigned:'资料已被归入项目，请刷新列表。'})[e]||e;
 const pendingDelete=d=>d?.recommended_action==='delete';
 const selectedDrafts=()=>rows.filter(r=>selected.has(r.key)).map(r=>drafts.get(r.key)).filter(Boolean);
 const saveable=()=>[...drafts.values()].filter(d=>!pendingDelete(d));
 const dirty=()=>onDirty(ruleDirty||drafts.size>0);
 mount.innerHTML=`<section class="panel history-rules"><div class="section-head"><div><h2>数据清洗 Skill</h2><p class="hint">先保存规则，再预览所选资料。原文保留供核对，清洗稿单独保存。</p></div><div class="actions"><span id="clean-rule-state">已保存</span><button class="primary write" data-clean-action="rules-save">保存规则</button></div></div><div class="field"><label for="clean-instructions">清洗规则</label><textarea id="clean-instructions" class="stage-instructions">${esc(skill.instructions)}</textarea></div><details><summary>去噪条件与 Skill 文件</summary><div class="field"><label for="clean-drop-lines">去除的固定提示行 · 每行一个</label><textarea id="clean-drop-lines">${esc(skill.settings.cleaning_drop_lines.join('\n'))}</textarea></div><label class="extraction-choice"><input id="clean-collapse" type="checkbox" ${skill.settings.cleaning_collapse_duplicates?'checked':''}>合并相邻重复消息</label><button data-clean-action="download">下载已保存 Skill</button></details></section>
 <section class="panel cleaning-review"><div class="section-head"><div><h2>待清洗资料</h2><p class="hint">勾选资料 → AI 预览 → 核对清洗稿；弃用项决定保留或删除 → 批量保存</p></div><a href="#knowledge/unassigned" data-view="unassigned">查看清洗后待入库项目 ↗</a></div><p class="hint">每次最多预览本页 20 条，长文分段处理。预览和修改暂存在本标签页，切页后可返回继续，刷新或关闭标签页会丢失。批量操作仅处理勾选项：保留项保存分类，AI 建议弃用项确认后删除。</p><div class="actions history-batch"><button class="write" data-clean-action="preview">AI 预览所选</button><button class="primary write" data-clean-action="organize">开始自动整理（后台）</button><button class="write" data-clean-action="save-all">批量保存 / 删除所选</button><button class="write danger" data-clean-action="delete">永久删除所选</button><button data-clean-action="refresh">刷新列表</button></div><p id="clean-notice" role="status"></p><div id="clean-rows"></div><div class="history-pager"><button data-clean-action="previous">上一页</button><span id="clean-page"></span><button data-clean-action="next">下一页</button></div></section>`;
 function draw(){
  $('#clean-rows').innerHTML=rows.length?`<div class="cleaning-list"><label class="cleaning-select-all"><input id="clean-select-all" type="checkbox" ${rows.every(r=>selected.has(r.key))?'checked':''}>选择本页全部资料</label>${rows.map(row=>{const d=drafts.get(row.key),discard=pendingDelete(d);return `<article class="cleaning-card ${discard?'cleaning-delete-pending':''}" data-clean-row="${esc(row.key)}"><div class="section-head"><label><input type="checkbox" data-clean-select="${esc(row.key)}" ${selected.has(row.key)?'checked':''}> <strong>${esc(row.title)}</strong></label><small>${esc(row.kind==='archive'?'会话':'资料')} · ${esc(row.created_at)}</small></div>${discard?`<div class="cleaning-delete-question" role="alert"><strong>AI 建议弃用：是否永久删除这条资料？</strong><p>${esc(d.delete_reason||d.reason)}</p><p class="hint">尚未删除。勾选后可随批量操作统一确认删除；也可选择保留后继续整理。</p></div>`:''}<div class="cleaning-comparison"><section><h3>原文 · 核对依据</h3><p class="hint history-source">${esc(row.source)}</p><pre class="project-memory-text cleaning-original">${esc(row.body||'原文暂不可读取')}</pre><div class="actions"><button data-clean-action="detail" data-key="${esc(row.key)}">查看完整原文</button><button class="write" data-clean-action="manual" data-key="${esc(row.key)}">手动整理</button><button class="write danger ${discard?'delete-highlight':''}" data-clean-action="delete-row" data-key="${esc(row.key)}">${discard?'确认永久删除此条':'永久删除此条'}</button>${discard?`<button class="write" data-clean-action="keep-row" data-key="${esc(row.key)}">保留并继续整理</button>`:''}</div></section><section><h3>清洗稿 · 可直接修改</h3><textarea class="clean-result" data-clean-text="${esc(row.key)}" aria-label="清洗稿 ${esc(row.key)}" placeholder="勾选后 AI 预览，或手动填写清洗稿">${esc(d?.cleaned_text||'')}</textarea><label class="clean-category">资料类别<select data-clean-category="${esc(row.key)}" aria-label="资料类别 ${esc(row.key)}">${Object.entries(categories).map(([k,v])=>`<option value="${k}" ${k===(d?.category||'reference')?'selected':''}>${v}</option>`).join('')}</select></label>${d?`<p class="hint">${esc(d.reason||'手动整理')} · ${d.source==='manual'?'人工修改':'AI 建议'} · ${discard?'待决定删除':'待保存'}</p><button class="write" data-clean-action="save-row" data-key="${esc(row.key)}" ${discard?'disabled':''}>保存清洗与分类</button>`:'<p class="hint">保存后送到待入库项目确认归属。</p>'}${errors.has(row.key)?`<p class="error" role="alert">${esc(errors.get(row.key))}</p>`:''}</section></div></article>`;}).join('')}</div>`:'<div class="empty">没有待清洗资料。已保存的清洗稿可在“项目历史 → 待入库项目”继续归类。</div>';
  controls();dirty();
 }
 function controls(){
  $('#clean-page').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/20))} 页 · 共 ${total} 条 · ${saveable().length} 条可保存 · ${[...drafts.values()].filter(pendingDelete).length} 条待决定删除`;
  $('[data-clean-action="previous"]').disabled=busy||page<=1;$('[data-clean-action="next"]').disabled=busy||page*20>=total;
  $('[data-clean-action="preview"]').disabled=busy||ruleDirty||!selected.size;
  const batch=selectedDrafts(),deletions=batch.filter(pendingDelete).length;
  $('[data-clean-action="save-all"]').disabled=busy||ruleDirty||!batch.length;
  const organize=$('[data-clean-action="organize"]');
  if(organize){organize.disabled=busy||ruleDirty||!selected.size||batch.some(pendingDelete);
   organize.textContent=selected.size?`开始自动整理 ${selected.size} 条（后台）`:'开始自动整理（后台）';}
  $('[data-clean-action="save-all"]').textContent=batch.length?`批量保存 ${batch.length-deletions} 条 / 删除 ${deletions} 条`:'批量保存 / 删除所选';
  $('[data-clean-action="delete"]').disabled=busy||!selected.size;
  mount.querySelectorAll('[data-clean-action="save-row"]').forEach(b=>b.disabled=busy||ruleDirty||pendingDelete(drafts.get(b.dataset.key)));
 }
 function notice(text){$('#clean-notice').textContent=text;}
 function ruleChanged(){ruleDirty=true;$('#clean-rule-state').textContent='有未保存修改';controls();dirty();}
 function edit(key,field,value){const row=rows.find(r=>r.key===key),old=drafts.get(key);drafts.set(key,{key,expected_revision:old?.expected_revision||row.expected_revision,cleaned_text:old?.cleaned_text||'',category:old?.category||'reference',...old,[field]:value,source:'manual'});errors.delete(key);controls();dirty();}
 mount.oninput=e=>{if(['clean-instructions','clean-drop-lines'].includes(e.target.id))ruleChanged();else if(e.target.dataset.cleanText)edit(e.target.dataset.cleanText,'cleaned_text',e.target.value);};
 mount.onchange=e=>{const el=e.target;if(el.id==='clean-collapse')ruleChanged();else if(el.dataset.cleanSelect){el.checked?selected.add(el.dataset.cleanSelect):selected.delete(el.dataset.cleanSelect);controls();}else if(el.id==='clean-select-all'){selected.clear();if(el.checked)rows.forEach(r=>selected.add(r.key));draw();}else if(el.dataset.cleanCategory){edit(el.dataset.cleanCategory,'category',el.value);draw();}};
 async function load(){queue=await api('cleaning?page='+page);if(page>1&&!queue.items.length){page=Math.max(1,Math.ceil(queue.total/20));queue=await api('cleaning?page='+page);}rows=queue.items;total=queue.total;selected.clear();}
 async function save(targets){
  if(ruleDirty)throw Error('请先保存规则。');if(!targets.length)return;let succeeded=0,failed=0;
  for(let offset=0;offset<targets.length;offset+=100){const result=await api('cleaning/save',{items:targets.slice(offset,offset+100),rule_revision:skill.revision});succeeded+=result.succeeded;failed+=result.failed;for(const r of result.items){if(r.ok){drafts.delete(r.key);errors.delete(r.key);}else errors.set(r.key,errorText(r.error));}}
  notice(`清洗保存：成功 ${succeeded} 条，失败 ${failed} 条。${failed?'失败项的修改已保留。':'已送到“项目历史 → 待入库项目”。'}`);await load();
 }
 async function saveBatch(targets){
  const keep=targets.filter(d=>!pendingDelete(d)),discard=targets.filter(pendingDelete);
  let saved=0,deleted=0,failed=0,interrupted='';
  const untouched=selected.size-targets.length;
  for(const [route,items] of [['save',keep],['delete',discard]]){
   if(!items.length)continue;
   try{
    const result=await api('cleaning/'+route,{items,rule_revision:skill.revision,...(route==='delete'?{confirm_permanent:true}:{})});
    if(route==='save')saved+=result.succeeded;else deleted+=result.succeeded;
    failed+=result.failed;
    for(const r of result.items){if(r.ok){drafts.delete(r.key);errors.delete(r.key);selected.delete(r.key);}else errors.set(r.key,errorText(r.error));}
   }catch(error){interrupted=`${route==='save'?'保存':'删除'}请求未能确认结果：${errorText(error.message)}。剩余条目未继续处理，请核对后重试。`;break;}
  }
  notice(`批量处理：保存成功 ${saved} 条，删除成功 ${deleted} 条，失败 ${failed} 条。${untouched?`${untouched} 条尚无清洗稿，未处理。`:''}${interrupted}`);
  await load();
 }
 mount.onclick=async e=>{const button=e.target.closest('[data-clean-action]');if(!button||busy)return;e.preventDefault();const action=button.dataset.cleanAction;
  const batchTargets=action==='save-all'?selectedDrafts():[];
  if(action==='save-all'){if(ruleDirty||!batchTargets.length)return;const discard=batchTargets.filter(pendingDelete);if(discard.length&&!confirm(`本次将保存 ${batchTargets.length-discard.length} 条清洗与分类，并永久删除 ${discard.length} 条 AI 建议弃用资料。\n待删除：\n${discard.map(d=>'• '+rows.find(r=>r.key===d.key).title).join('\n')}\n删除正文、清洗稿和本机原始会话文件（含历史快照），无法撤销。未勾选条目不处理。确认统一执行？`))return;}
  const deleteTargets=action==='delete-row'?rows.filter(r=>r.key===button.dataset.key):rows.filter(r=>selected.has(r.key));
  if(action==='delete'||action==='delete-row'){const targets=deleteTargets;if(!targets.length)return;if(!confirm(`永久删除以下 ${targets.length} 条资料？\n${targets.map(r=>'• '+r.title).join('\n')}\n将删除正文、清洗稿和本机保存的原始会话文件（含历史快照），无法撤销。已有引用的来源会保留并提示。`))return;}
  busy=true;onBusy(true,action==='preview');mount.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=true);
  try{
   if(action==='rules-save'){skill=await api('skills/cleaning',{expected_revision:skill.revision,instructions:$('#clean-instructions').value,settings:{cleaning_drop_lines:$('#clean-drop-lines').value.split('\n').map(x=>x.trim()).filter(Boolean),cleaning_collapse_duplicates:$('#clean-collapse').checked}});ruleDirty=false;$('#clean-rule-state').textContent='已保存';for(const [key,d] of drafts)if(d.source==='ai')drafts.delete(key);notice('清洗规则已保存。旧 AI 预览已清除，人工修改已保留。');}
   else if(action==='preview'){if(ruleDirty)throw Error('请先保存规则。');const targets=rows.filter(r=>selected.has(r.key)&&!drafts.has(r.key));if(!targets.length)throw Error('所选资料已有待保存清洗稿，请先核对和保存。');notice('AI 正在按已保存规则清洗所选资料…');const result=await api('cleaning/preview',{items:targets.map(r=>({key:r.key,expected_revision:r.expected_revision})),rule_revision:skill.revision});let count=0,deletions=0;for(const r of result.items){if(r.ok){drafts.set(r.key,{...r,source:'ai'});errors.delete(r.key);count++;if(pendingDelete(r))deletions++;}else errors.set(r.key,errorText(r.error));}notice(`已预览 ${count} 条，${result.items.length-count} 条需重试或手动处理。尚未保存，请核对清洗稿与类别。${deletions?`其中 ${deletions} 条建议弃用：是否删除？请在高亮条目中逐条决定。`:''}`);}
   else if(action==='save-all')await saveBatch(batchTargets);
   else if(action==='save-row')await save([drafts.get(button.dataset.key)]);
   else if(action==='keep-row'){const d=drafts.get(button.dataset.key);if(d)drafts.set(button.dataset.key,{...d,recommended_action:'keep',delete_reason:'',source:'manual',reason:'已选择保留，请核对清洗稿后保存。'});notice('已选择保留这条资料，核对或修改后可保存。');}
   else if(action==='manual'){const row=await api('cleaning/detail?'+new URLSearchParams({key:button.dataset.key}));drafts.set(row.key,{...drafts.get(row.key),key:row.key,expected_revision:row.expected_revision,cleaned_text:drafts.get(row.key)?.cleaned_text||row.body,category:drafts.get(row.key)?.category||'reference',source:'manual'});errors.delete(row.key);}
   else if(action==='detail'){const row=await api('cleaning/detail?'+new URLSearchParams({key:button.dataset.key}));const dialog=document.createElement('dialog');dialog.className='history-source-dialog';dialog.innerHTML=`<div class="section-head"><h2>完整原文</h2><button autofocus>关闭</button></div><pre class="project-memory-text">${esc(row.body||'原文暂不可读取')}</pre>`;mount.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();dialog.showModal();}
   else if(action==='delete'||action==='delete-row'){const result=await api('cleaning/delete',{items:deleteTargets.map(r=>({key:r.key,expected_revision:r.expected_revision})),confirm_permanent:true});for(const r of result.items){if(r.ok){drafts.delete(r.key);errors.delete(r.key);}else errors.set(r.key,errorText(r.error));}notice(`永久删除：成功 ${result.succeeded} 条，失败 ${result.failed} 条。${result.failed?'未删除的条目已保留，请核对原因。':''}`);await load();}
   else if(action==='download'){const url=URL.createObjectURL(new Blob([skill.skill],{type:'text/markdown;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='SKILL.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
   else{if(action==='previous')page--;if(action==='next')page++;await load();}
  }catch(error){notice(errorText(error.message));toast(errorText(error.message));}
  finally{busy=false;onBusy(false);mount.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=false);draw();}
 };draw();
}};
