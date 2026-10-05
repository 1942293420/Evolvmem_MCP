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
