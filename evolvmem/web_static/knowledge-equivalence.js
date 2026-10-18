/* A person confirms two expressions; suggestions alone never save a project. */
window.EvolvKnowledgeEquivalence={
  create({mount,api,esc,projects,onConfirm}){
    let unit=null,sequence=0;const cache=new Map(),views=new Map();
    const label=x=>EvolvProjects.scopeLabel(x,projects);
    const errors={revision_conflict:'资料已变化，请刷新资料后重新核对。',equivalence_scope_conflict:'两条资料已确认的归属不同，请先核对项目归属。',equivalence_choose_original:'请选择这组同义表达中的原知识。',equivalence_already_confirmed:'这条表述已确认过，请刷新资料。',equivalence_target_unavailable:'原知识已变化，请重新选择。'};
    mount.addEventListener('click',e=>e.stopPropagation());
    mount.addEventListener('input',e=>e.stopPropagation());
    mount.addEventListener('change',e=>e.stopPropagation());
    function draw(){
      let selected=null,searching=false;const key=unit.task_id+':'+unit.digest;
      mount.innerHTML=`<details data-ke-open><summary>与已有知识相同</summary><div class="organization-settings"><label>查找已归档知识<input type="search" data-ke-query placeholder="知识原文或标题"></label><button type="button" data-ke-search>查找</button></div><p role="status" data-ke-status></p><div data-ke-list></div><div data-ke-compare></div></details>`;
      const $=s=>mount.querySelector(s);
      let loaded=false;
      async function search(){
        searching=true;const token=++sequence,params=new URLSearchParams({task_id:unit.task_id,digest:unit.digest,q:$('[data-ke-query]').value}),id=params.toString();
        $('[data-ke-status]').textContent='正在查找已归档知识…';
        try{
          const result=cache.get(id)||await api('equivalences/candidates?'+params);cache.set(id,result);
          if(token!==sequence||key!==unit.task_id+':'+unit.digest)return;
          searching=false;selected=null;$('[data-ke-compare]').replaceChildren();
          $('[data-ke-status]').textContent=result.items.length?'选择原知识，核对两种表述后再确认。':'没有找到匹配的已归档知识。';
          $('[data-ke-list]').innerHTML=result.items.map((x,i)=>`<button type="button" class="ke-candidate" data-ke-candidate="${i}"><strong>${esc(x.title)}</strong><span>${esc(label(x))}</span></button>`).join('');
          $('[data-ke-list]').onclick=e=>{
            const b=e.target.closest('[data-ke-candidate]');if(!b)return;
            selected=result.items[Number(b.dataset.keCandidate)];
            $('[data-ke-compare]').innerHTML=`<div class="ke-compare"><section><h4>本次表述</h4><pre class="review-original">${esc(unit.text)}</pre></section><section><h4>已有知识</h4><pre class="review-original">${esc(selected.text)}</pre><p>已归入「${esc(label(selected))}」</p></section></div><button type="button" class="primary write" data-ke-confirm>确认相同，沿用归属</button>`;
            $('[data-ke-confirm]').onclick=async()=>{
              const button=$('[data-ke-confirm]');button.disabled=true;
              try{await onConfirm({task_id:unit.task_id,digest:unit.digest,expected_revision:unit.revision,target_knowledge_id:selected.knowledge_id,target_revision:selected.revision});}
              catch(error){$('[data-ke-status]').textContent=errors[error.message]||error.message;}
              finally{button.disabled=false;}
            };
          };
        }catch(error){if(token===sequence)$('[data-ke-status]').textContent=errors[error.message]||error.message;}
      }
      $('[data-ke-open]').ontoggle=()=>{if($('[data-ke-open]').open&&!loaded){loaded=true;search();}};
      $('[data-ke-search]').onclick=search;
      $('[data-ke-query]').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();search();}};
      views.set(key+':'+unit.revision,{node:mount.firstElementChild,resume:()=>{if(searching)search();}});
    }
    return {detach(){++sequence;mount.replaceChildren();},show(value){++sequence;unit=value;const old=views.get(unit.task_id+':'+unit.digest+':'+unit.revision);if(old){mount.replaceChildren(old.node);old.resume();}else draw();},invalidate(){++sequence;cache.clear();views.clear();}};
  },
  records({mount,api,esc,projects,onSaved}){
    let sequence=0;
    async function load(){
      const token=++sequence;
      mount.innerHTML='<h2>已确认的同义表达</h2><p role="status">正在读取…</p>';
      try{
        const result=await api('equivalences');if(token!==sequence)return;
        mount.innerHTML=`<div class="section-head"><h2>已确认的同义表达</h2><button data-ke-refresh>刷新</button></div><p role="status" data-ke-status>${result.items.length?'撤销关联后，两份资料分别展示，已保存的项目归属不变。':'还没有确认过同义表达。'}</p>${result.items.map((x,i)=>`<article class="organization-task"><h3>${esc(x.target.title)}</h3><div class="ke-compare"><section><h4>原知识</h4><pre class="review-original">${esc(x.target.text)}</pre></section><section><h4>已确认的另一种表述</h4><pre class="review-original">${esc(x.source.text)}</pre></section></div><p>归入「${esc(EvolvProjects.scopeLabel(x.source,projects))}」</p><button type="button" class="write" data-ke-revoke="${i}">撤销同义关联</button></article>`).join('')}`;
        mount.querySelector('[data-ke-refresh]').onclick=load;
        mount.querySelectorAll('[data-ke-revoke]').forEach(b=>b.onclick=async()=>{
          b.disabled=true;const x=result.items[Number(b.dataset.keRevoke)];
          try{await api('equivalences/revoke',{knowledge_id:x.source_knowledge_id,expected_revision:x.revision});await onSaved();await load();}
          catch(error){mount.querySelector('[data-ke-status]').textContent=error.message==='revision_conflict'?'关联已变化，请刷新后重试。':error.message;b.disabled=false;}
        });
      }catch(error){if(token===sequence)mount.querySelector('[role=status]').textContent=error.message;}
    }
    return {load};
  }
};
