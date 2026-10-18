(() => {
  'use strict';
  window.EvolvProjectReview={create({project,projects,api,esc,onDetail,onChanged}){
    const node=document.createElement('section');node.className='project-review';
    let data=null,loading=null,busy=false,stopped=false,loaded=false,target=project,selected=new Set(),pages=new Map(),message='',failures=[];
    const name=id=>projects.find(p=>p.project===id)?.display_name||id;
    const all=()=>data?.groups.flatMap(g=>g.items)||[];
    const errors={revision_conflict:'资料已变化，请刷新后处理',individual_review_required:'需要单独判断',confirmed_project_unchanged:'已有人工归属，未更改项目',identity_conflict:'存在相同资料，请查看详情',qa_conflict_requires_confirmation:'问答存在分歧，需要核对',qa_source_changed:'问答来源已变化'};
    function group(g,exception=false){
      const page=Math.min(pages.get(g.id)||0,Math.max(0,Math.ceil(g.count/12)-1)),items=g.items.slice(page*12,page*12+12);
      pages.set(g.id,page);
      const n=g.items.filter(i=>selected.has(i.id)).length;
      return `<section class="panel" data-pr-section="${g.id}"><header class="section-head"><h3>${exception?'':`<input type="checkbox" data-pr-group="${g.id}" aria-label="选择${esc(g.title)}" ${n===g.count?'checked':''} ${busy?'disabled':''}> `}${esc(g.title)} · ${g.count} 份</h3>${exception?'':`<span>已选 ${n} 份</span>`}</header>
        ${exception?`<p>${({verification:'这些经验需要实际验证结果，归入项目不会使它们变成已验证经验。',managed:'这些记录由任务和来源维护，不作为普通知识批量入库。',conflict:'存在归属冲突、曾被否决或涉及多个项目。',answer:'答案与原文还需核对，未加入批量归档。'})[g.id]}</p>`:''}
        <details data-pr-details="${g.id}"><summary>查看本组资料</summary><div class="pr-items">${items.map(i=>`<article class="pr-item">${exception?'':`<input type="checkbox" data-pr-item="${i.id}" aria-label="选择${esc(i.title)}" ${selected.has(i.id)?'checked':''} ${busy?'disabled':''}>`}<div><button class="quiet" data-pr-detail="${i.id}">${esc(i.title)}</button><p>${esc(i.preview)}</p>${i.confirmed?'<span class="badge">沿用已确认项目</span>':''}</div></article>`).join('')}</div><div class="actions"><button data-pr-page="${g.id}" data-step="-1" ${page===0?'disabled':''}>上一页</button><span>${page+1} / ${Math.max(1,Math.ceil(g.count/12))}</span><button data-pr-page="${g.id}" data-step="1" ${(page+1)*12>=g.count?'disabled':''}>下一页</button></div></details></section>`;
    }
    function draw(){
      if(!data){node.innerHTML='<p role="status">正在汇总项目资料…</p>';return;}
      const opened=new Set([...node.querySelectorAll('details[open]')].map(e=>e.dataset.prDetails));
      const count=all().filter(i=>selected.has(i.id)).length;
      node.innerHTML=`<header class="section-head"><h2>${esc(name(project))} · 集中归档</h2><button data-pr-refresh ${busy?'disabled':''}>刷新资料</button></header>
        <div class="pr-summary"><strong>${all().length.toLocaleString('zh-CN')} 份可集中处理</strong><span>${data.exceptions.reduce((n,g)=>n+g.count,0).toLocaleString('zh-CN')} 份另行处理</span></div>
        <div class="pr-status" role="status" ${message?'':'hidden'}>${esc(message)}</div>
        ${data.groups.map(g=>group(g)).join('')||'<section class="panel"><p>本项目没有需要集中归档的资料。</p></section>'}
        ${data.groups.length?`<section class="panel pr-save"><label>归入项目 <select data-pr-target aria-label="归入项目" ${busy?'disabled':''}>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}" ${p.project===target?'selected':''}>${esc(name(p.project))}</option>`).join('')}</select></label><strong>已选 ${count.toLocaleString('zh-CN')} 份</strong><div class="actions">${busy?'<button data-pr-stop>完成当前批后停止</button>':`<button class="write" data-pr-save="defer" ${count?'':'disabled'}>所选暂不归档</button><button class="primary write" data-pr-save="archive" ${count?'':'disabled'}>所选归入「${esc(name(target))}」</button>`}</div></section>`:''}
        ${failures.length?`<section class="panel"><h3>${failures.length} 份未处理</h3>${failures.slice(0,20).map(f=>`<p><button data-pr-detail="${f.id}">查看资料</button> ${esc(errors[f.error]||'处理未完成，请查看资料后重试')}</p>`).join('')}</section>`:''}
        ${data.exceptions.length?'<h2>另行处理</h2>'+data.exceptions.map(g=>group(g,true)).join(''):''}`;
      for(const d of node.querySelectorAll('details'))d.open=opened.has(d.dataset.prDetails);
      for(const box of node.querySelectorAll('[data-pr-group]')){
        const g=data.groups.find(g=>g.id===box.dataset.prGroup),n=g.items.filter(i=>selected.has(i.id)).length;
        box.indeterminate=n>0&&n<g.count;
      }
    }
    async function load(force=false){
      if(busy||(!force&&data))return;
      if(loading)return loading;
      loading=(async()=>{
        const next=await api('project-review?'+new URLSearchParams({project}));
        data=next;selected=new Set(all().filter(i=>!loaded||selected.has(i.id)).map(i=>i.id));loaded=true;draw();
      })();
      try{await loading;}finally{loading=null;}
    }
    async function save(action){
      if(busy)return;
      const entries=all().filter(i=>selected.has(i.id));if(!entries.length)return;
      busy=true;stopped=false;failures=[];let done=0,succeeded=0;
      message=`正在${action==='archive'?'归入项目':'暂不归档'}：0 / ${entries.length} 份`;draw();
      try{
        for(let offset=0;offset<entries.length&&!stopped;offset+=20){
          const result=await api('project-review/apply',{source_project:project,project:target,action,items:entries.slice(offset,offset+20).map(({id,expected_revision})=>({id,expected_revision}))});
          const saved=new Set(result.items.filter(i=>i.ok).map(i=>i.id));
          for(const g of data.groups){g.items=g.items.filter(i=>!saved.has(i.id));g.count=g.items.length;}
          data.groups=data.groups.filter(g=>g.count);for(const id of saved)selected.delete(id);
          failures.push(...result.items.filter(i=>!i.ok));done+=result.items.length;succeeded+=result.succeeded;
          message=`已处理 ${done} / ${entries.length} 份，已保存 ${succeeded} 份`;draw();
        }
        message=`${stopped?'已停止。':''}已保存 ${succeeded} 份${failures.length?`，${failures.length} 份需重新查看`:''}。`;
        if(succeeded)onChanged?.();
      }catch(error){message=`已保存 ${succeeded} 份。${error.message} 未确认结果的批次请先刷新资料。`;}
      finally{busy=false;draw();}
    }
    node.addEventListener('change',e=>{
      if(e.target.matches('[data-pr-target]'))target=e.target.value;
      if(e.target.dataset.prItem){const id=Number(e.target.dataset.prItem);e.target.checked?selected.add(id):selected.delete(id);}
      if(e.target.dataset.prGroup){const g=data.groups.find(g=>g.id===e.target.dataset.prGroup);for(const i of g.items)e.target.checked?selected.add(i.id):selected.delete(i.id);}
      draw();
    });
    node.addEventListener('click',async e=>{
      const b=e.target.closest('button');if(!b)return;
      try{
        if(b.hasAttribute('data-pr-refresh')){message='';failures=[];await load(true);}
        if(b.dataset.prSave)await save(b.dataset.prSave);
        if(b.hasAttribute('data-pr-stop')){stopped=true;b.disabled=true;}
        if(b.dataset.prDetail)await onDetail(Number(b.dataset.prDetail));
        if(b.dataset.prPage){const id=b.dataset.prPage;pages.set(id,(pages.get(id)||0)+Number(b.dataset.step));draw();}
      }catch(error){message=error.message;draw();}
    });
    draw();return {node,load,get busy(){return busy;},invalidate(){if(!busy)data=null;}};
  }};
})();
