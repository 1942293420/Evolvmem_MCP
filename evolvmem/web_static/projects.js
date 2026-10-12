/* A project is registered once, from any place that needs it. */
window.EvolvProjects={create({api,esc,onSaved}) {
  if(!EvolvAuth.canWrite)return Promise.reject(Error('当前为只读访问'));
  return new Promise(resolve=>{
    const serverErrors={invalid_project_id:'项目标识需以小写字母或数字开头，只能包含小写字母、数字、点、下划线或连字符，最长 64 个字符。',alias_conflict:'别名已属于其他项目，请换一个别名。'};
    const dialog=document.createElement('dialog');dialog.className='memory-dialog project-create-dialog';dialog.setAttribute('aria-label','新增项目');
    dialog.innerHTML=`<form><div class="dialog-header"><h2>新增项目</h2><button type="button" data-project-cancel aria-label="关闭新增项目">✕</button></div><div class="dialog-body"><p class="hint">创建后可立即在当前位置使用，已有问答草稿会保留。</p><div class="field"><label for="new-project-id">项目标识</label><input id="new-project-id" required pattern="[a-z0-9][a-z0-9._\\-]{0,63}" placeholder="例如 customer-service"><small>使用小写字母、数字或连字符；不必与文件夹名称相同。</small></div><div class="field"><label for="new-project-name">显示名称</label><input id="new-project-name" maxlength="200" placeholder="例如 智能客服"></div><div class="field"><label for="new-project-aliases">业务别名 · 每行一个</label><textarea id="new-project-aliases"></textarea></div><p data-project-error role="alert" class="reason"></p></div><div class="dialog-footer"><button type="button" data-project-cancel>取消</button><button class="primary write" type="submit">创建并选中</button></div></form>`;
    document.body.append(dialog);let busy=false,created=null;
    dialog.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
    dialog.addEventListener('close',()=>{dialog.remove();resolve(created);},{once:true});
    dialog.querySelectorAll('[data-project-cancel]').forEach(b=>b.onclick=()=>{if(!busy)dialog.close();});
    dialog.querySelector('form').onsubmit=async e=>{e.preventDefault();if(busy)return;busy=true;dialog.setAttribute('aria-busy','true');const submit=dialog.querySelector('[type=submit]');submit.disabled=true;
      try{const project=dialog.querySelector('#new-project-id').value.trim();
        if(!project)throw Error('请填写项目标识。');
        if(!/^[a-z0-9][a-z0-9._-]{0,63}$/.test(project))throw Error('项目标识需以小写字母或数字开头，只能包含小写字母、数字、点、下划线或连字符，最长 64 个字符。');
        const list=await api('projects');if(list.projects.some(p=>p.project===project))throw Error('项目标识已存在，请选用已有项目或换一个标识。');
        await api('projects',{project,display_name:dialog.querySelector('#new-project-name').value.trim()||project,aliases:dialog.querySelector('#new-project-aliases').value.split('\n').map(s=>s.trim()).filter(Boolean)});
        created=project;await onSaved?.(project);dialog.close();
      }catch(error){dialog.querySelector('[data-project-error]').textContent=serverErrors[error.message]||error.message;}finally{busy=false;dialog.setAttribute('aria-busy','false');submit.disabled=false;}
    };dialog.showModal();
  });
}};

/* Applicability is explicit: personal general, or one shared item in selected projects. */
EvolvProjects.projectIds=row=>{const raw=typeof row.projects==='string'?JSON.parse(row.projects):row.projects||[];return raw.length?raw:[row.project].filter(Boolean);};
EvolvProjects.scopeLabel=(row,projects=[])=>{
  const names=EvolvProjects.projectIds(row);
  return (row.knowledge_scope||row.scope)==='global'?'个人通用':names.map(p=>projects.find(x=>x.project===p)?.display_name||p).join('、');
};
EvolvProjects.scopePicker=({mount,projects,esc,value={}})=>{
  mount.innerHTML=`<div class="knowledge-scope-picker"><label>适用范围<select name="knowledge_scope" aria-label="知识适用范围"><option value="project">指定项目（可多选）</option><option value="global">个人通用</option></select></label><div data-scope-projects><input type="search" data-scope-search aria-label="查找可选项目" placeholder="查找项目"><div class="scope-options" role="group" aria-label="选择适用项目">${projects.filter(p=>p.status==='active').map(p=>`<label><input type="checkbox" name="projects" value="${esc(p.project)}"><span>${esc(p.display_name||p.project)}</span></label>`).join('')}</div></div><p data-scope-selected role="status"></p></div>`;
  const box=mount.querySelector('.knowledge-scope-picker'), mode=box.querySelector('[name=knowledge_scope]');
  const inputs=()=>[...box.querySelectorAll('[name=projects]')];
  function read(){return {knowledge_scope:mode.value,projects:mode.value==='global'?[]:inputs().filter(x=>x.checked).map(x=>x.value)};}
  function draw(){const v=read();box.querySelector('[data-scope-projects]').hidden=mode.value==='global';box.querySelector('[data-scope-selected]').textContent=mode.value==='global'?'供各项目参考，仍按知识中的适用条件使用':v.projects.length?`已选 ${v.projects.length} 个项目：${EvolvProjects.scopeLabel(v,projects)}${v.projects.length>1?'；共用一条知识，修改同步生效':''}`:'尚未选择项目';}
  function set(v){const selected=EvolvProjects.projectIds(v);mode.value=v.knowledge_scope||v.scope||'project';inputs().forEach(x=>x.checked=selected.includes(x.value));draw();}
  box.addEventListener('change',draw);
  box.querySelector('[data-scope-search]').addEventListener('input',e=>{const q=e.target.value.toLocaleLowerCase();inputs().forEach(x=>x.closest('label').hidden=!(x.value+' '+x.closest('label').textContent).toLocaleLowerCase().includes(q));});
  set(value);return {read,set,draw};
};

EvolvProjects.businessEvidence=(analysis,projects,esc)=>{
  if(!analysis||!Object.keys(analysis.chain||{}).length)return '';
  const stages={need:'需求目标',analysis:'问题分析',change:'修改或设计对象',result:'验收或当前结果'};
  const roles={owner:'直接负责的项目',applicable:'知识适用项目',related:'关联场景或依赖',uncertain:'尚待确定'};
  const rows=Object.entries(stages).filter(([key])=>analysis.chain[key]).map(([key,label])=>`<div><strong>${label}${analysis.chain_sources?.[key]?` · ${esc(analysis.chain_sources[key])}`:''}</strong><blockquote>${esc(analysis.chain[key])}</blockquote></div>`).join('');
  const targets=(analysis.targets||[]).map(t=>`<p>${esc(projects.find(p=>p.project===t.project)?.display_name||t.project)}：${esc(roles[t.role]||'尚待确定')}</p>`).join('');
  return `<details class="business-evidence"><summary>查看归类依据</summary>${targets?`<div>${targets}</div>`:''}${rows}</details>`;
};
