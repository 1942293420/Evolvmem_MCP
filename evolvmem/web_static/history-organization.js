/* Project history owns the classification Skill and its review queue. */
window.EvolvHistoryOrganization={async render({mount,api,esc,toast,projects,onDirty,onBusy}){
 const [initial,queue]=await Promise.all([api('skills/ownership'),api('history/organization')]);
 let skill=initial,page=1,rows=queue.items,total=queue.total,busy=false,rulesDirty=false;
 const drafts=new Map(),suggestions=new Map(),errors=new Map(),selected=new Set();
 const $=s=>mount.querySelector(s);
 const name=p=>projects.find(x=>x.project===p)?.display_name||p;
 const options=value=>'<option value="">请选择项目</option>'+projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}" ${p.project===value?'selected':''}>${esc((p.display_name||p.project)+(projects.some(x=>x.project!==p.project&&x.display_name===p.display_name)?' · '+p.project:''))}</option>`).join('');
 const errorText=e=>({cleaning_confirmation_required:'原文已变化，请返回数据清洗重新核对。',cleaning_source_referenced:'来源已有知识引用，请先处理引用关系。',cleaning_source_deleted:'资料已被永久删除。',classification_no_longer_unassigned:'这条记录已经确认归属，请刷新列表。',classification_source_changed:'会话已有新版本，请刷新后核对。',classification_source_conflict:'关联资料已有其他项目归属，请先核对来源。',classification_bad_response:'AI 返回格式无法读取，可重试或手动分类。',revision_conflict:'记录或规则已变化，请刷新并重新核对。'})[e]||e;
 const dirty=()=>onDirty(rulesDirty||drafts.size>0);
 mount.innerHTML=`<details class="panel history-rules"><summary>项目归属规则</summary><div class="section-head"><div><h2>项目归属规则</h2></div><div class="actions"><span id="history-rule-state">已保存</span><button class="primary write" data-history-action="rules-save">保存规则</button></div></div>
 <div class="field"><label for="history-instructions">项目归属规则</label><textarea id="history-instructions" class="stage-instructions">${esc(skill.instructions)}</textarea></div>
 <details><summary>高级匹配设置</summary><label class="extraction-choice"><input id="history-alias-matching" type="checkbox" ${skill.settings.project_alias_matching?'checked':''}>按项目名称与别名匹配</label><div class="field"><label for="history-ambiguous">不作为项目名称线索的通用词 · 每行一个</label><textarea id="history-ambiguous">${esc(skill.settings.ambiguous_project_names.join('\n'))}</textarea></div><button data-history-action="download">下载已保存 Skill</button></details></details>
 <section class="panel history-review"><div class="section-head"><div><h2>待归类资料</h2></div><div class="actions"><a href="#knowledge/cleaning" data-view="cleaning">前往数据清洗 ↗</a><a href="#knowledge/projects" data-view="projects">管理项目名录 ↗</a></div></div>
 <div class="actions history-batch"><button class="write" data-history-action="preview">AI 预览所选</button><select id="history-batch-project" aria-label="批量选择项目">${options('')}</select><button class="write" data-history-action="stage">应用到所选</button><button class="primary write" data-history-action="save-all">批量保存分类</button><button class="write danger" data-history-action="delete">永久删除所选</button><button data-history-action="refresh">刷新列表</button></div>
 <p id="history-notice" role="status"></p><div id="history-rows"></div><div class="pager history-pager"><button data-history-action="previous">上一页</button><span id="history-page"></span><button data-history-action="next">下一页</button></div></section>`;
 function draw(){
  $('#history-rows').innerHTML=rows.length?`<div class="table-wrap"><table class="history-classification"><thead><tr><th><input type="checkbox" id="history-select-all" aria-label="选择本页全部资料" ${rows.every(r=>selected.has(r.key))?'checked':''}></th><th>会话 / 资料</th><th>AI 建议与判断依据</th><th>最终项目归属</th></tr></thead><tbody>${rows.map(row=>{const draft=drafts.get(row.key),suggestion=suggestions.get(row.key);return `<tr data-history-row="${esc(row.key)}"><td><input type="checkbox" data-history-select="${esc(row.key)}" aria-label="选择 ${esc(row.title)}" ${selected.has(row.key)?'checked':''}></td><td><strong>${esc(row.title)}</strong><small>${esc(row.kind==='archive'?'已清洗会话':'来源资料')} · ${esc(row.created_at)}</small><p>${esc(row.body.slice(0,180)||'正文暂不可读取')}</p><button data-history-action="detail" data-key="${esc(row.key)}">查看正文</button></td><td>${suggestion?`<strong>${esc(suggestion.project?name(suggestion.project):'待手动判断')}</strong><p>${esc(suggestion.reason)}</p>${suggestion.truncated?'<small>长会话片段预览，请核对正文</small>':''}`:'<span class="hint">尚未预览，也可直接手动选择</span>'}</td><td><select data-history-project="${esc(row.key)}" aria-label="最终项目归属 ${esc(row.key)}">${options(draft?.project||'')}</select>${draft?`<small>${draft.source==='manual'?'手动选择':'采用 AI 建议'} · 待保存</small><button class="write" data-history-action="save-row" data-key="${esc(row.key)}">保存分类</button>`:''}${errors.has(row.key)?`<p class="error" role="alert">${esc(errors.get(row.key))}</p>`:''}</td></tr>`;}).join('')}</tbody></table></div>`:'<div class="empty">没有待归类资料</div>';
  $('#history-page').textContent=`第 ${page} / ${Math.max(1,Math.ceil(total/20))} 页 · 共 ${total} 条 · ${drafts.size} 条分类待保存`;
  $('[data-history-action="previous"]').disabled=busy||page<=1;
  $('[data-history-action="next"]').disabled=busy||page*20>=total;
  $('[data-history-action="preview"]').disabled=busy||rulesDirty||!selected.size;
  $('[data-history-action="save-all"]').disabled=busy||rulesDirty||!drafts.size;
  $('#history-select-all')?.setAttribute('aria-label','选择本页全部资料');
  if($('#history-select-all'))$('#history-select-all').indeterminate=selected.size>0&&!rows.every(r=>selected.has(r.key));
  dirty();
 }
 function notice(text){$('#history-notice').textContent=text;}
 async function load(){let q=await api('history/organization?page='+page);if(page>1&&!q.items.length){page=Math.max(1,Math.ceil(q.total/20));q=await api('history/organization?page='+page);}rows=q.items;total=q.total;selected.clear();draw();}
 function stage(row,project,source='manual'){if(project)drafts.set(row.key,{key:row.key,expected_revision:row.expected_revision,project,source});else drafts.delete(row.key);errors.delete(row.key);dirty();}
 function ruleChanged(){rulesDirty=true;$('#history-rule-state').textContent='有未保存修改';dirty();draw();}
 mount.oninput=e=>{if(['history-instructions','history-ambiguous'].includes(e.target.id))ruleChanged();};
 mount.onchange=e=>{const el=e.target;if(el.id==='history-alias-matching')ruleChanged();else if(el.dataset.historySelect){el.checked?selected.add(el.dataset.historySelect):selected.delete(el.dataset.historySelect);draw();}else if(el.id==='history-select-all'){selected.clear();if(el.checked)rows.forEach(r=>selected.add(r.key));draw();}else if(el.dataset.historyProject){stage(rows.find(r=>r.key===el.dataset.historyProject),el.value);draw();}};
 async function save(targets){
  if(rulesDirty)throw Error('请先保存规则，再确认分类。');
  let succeeded=0,failed=0;
  for(let offset=0;offset<targets.length;offset+=100){const result=await api('history/organization/save',{items:targets.slice(offset,offset+100)});for(const r of result.items){if(r.ok){drafts.delete(r.key);suggestions.delete(r.key);errors.delete(r.key);}else errors.set(r.key,errorText(r.error));}succeeded+=result.succeeded;failed+=result.failed;}
  notice(`分类保存：成功 ${succeeded} 条，失败 ${failed} 条。${failed?'失败项的修改已保留；原文有变化的资料需返回数据清洗重新核对。':'可到“已整理”查看对应项目。'}`);
  await load();
 }
 mount.onclick=async e=>{const button=e.target.closest('[data-history-action]');if(!button||busy)return;e.preventDefault();const action=button.dataset.historyAction;busy=true;onBusy(true);mount.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=true);
  try{
   if(action==='delete'){const targets=rows.filter(r=>selected.has(r.key));if(!targets.length)throw Error('请先勾选资料。');if(!confirm(`永久删除以下 ${targets.length} 条资料？\n${targets.map(r=>'• '+r.title).join('\n')}\n会删除清洗稿、正文和本机原始会话文件（含历史快照），无法撤销。已有引用的来源会保留。`))return;const result=await api('cleaning/delete',{items:targets.map(r=>({key:r.key,expected_revision:r.expected_revision})),confirm_permanent:true});for(const r of result.items){if(r.ok){drafts.delete(r.key);suggestions.delete(r.key);errors.delete(r.key);}else errors.set(r.key,errorText(r.error));}notice(`永久删除：成功 ${result.succeeded} 条，失败 ${result.failed} 条。`);await load();}
   else if(action==='rules-save'){skill=await api('skills/ownership',{expected_revision:skill.revision,instructions:$('#history-instructions').value,settings:{project_alias_matching:$('#history-alias-matching').checked,ambiguous_project_names:$('#history-ambiguous').value.split('\n').map(v=>v.trim()).filter(Boolean)}});rulesDirty=false;$('#history-rule-state').textContent='已保存';suggestions.clear();for(const [key,draft] of drafts)if(draft.source==='ai')drafts.delete(key);notice('规则已保存。请按新规则预览；手动选择的分类已保留。');}
   else if(action==='download'){const url=URL.createObjectURL(new Blob([skill.skill],{type:'text/markdown;charset=utf-8'})),a=document.createElement('a');a.href=url;a.download='SKILL.md';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
   else if(action==='preview'){if(rulesDirty)throw Error('请先保存规则。');const targets=rows.filter(r=>selected.has(r.key)&&!drafts.has(r.key));if(!targets.length)throw Error('所选资料已有待保存分类；先保存，或清空项目选择后重新预览。');notice('AI 正在按已保存规则预览分类…');const result=await api('history/organization/preview',{items:targets.map(r=>({key:r.key,expected_revision:r.expected_revision})),rule_revision:skill.revision});for(const r of result.items){suggestions.set(r.key,r);if(r.project)stage(r,r.project,'ai');}notice(`已预览 ${result.items.length} 条，尚未保存分类。请核对建议；不正确的直接修改项目。`);}
   else if(action==='stage'){const project=$('#history-batch-project').value;if(!project)throw Error('请选择批量归属的项目。');rows.filter(r=>selected.has(r.key)).forEach(r=>stage(r,project));}
   else if(action==='save-all')await save([...drafts.values()]);
   else if(action==='save-row')await save([drafts.get(button.dataset.key)]);
   else if(action==='detail'){const row=await api('history/organization/detail?'+new URLSearchParams({key:button.dataset.key}));const dialog=document.createElement('dialog');dialog.className='history-source-dialog';dialog.innerHTML=`<div class="section-head"><h2>核对正文</h2><button autofocus>关闭</button></div><pre class="project-memory-text">${esc(row.body||'暂无可读取正文')}</pre>`;mount.append(dialog);dialog.querySelector('button').onclick=()=>dialog.close();dialog.onclose=()=>dialog.remove();dialog.showModal();}
   else {if(action==='previous')page--;if(action==='next')page++;await load();}
  }catch(error){notice(errorText(error.message));toast(errorText(error.message));}
  finally{busy=false;onBusy(false);mount.querySelectorAll('button,input,select,textarea').forEach(el=>el.disabled=false);draw();}
 };draw();
}};
