(() => {
  'use strict';
  window.EvolvKnowledge = {create(mount, hooks = {}) {
  mount.classList.add('knowledge-workspace');
  mount.innerHTML=`<header class="knowledge-heading"><div><span class="knowledge-eyebrow">PROJECT KNOWLEDGE</span><h1 id="knowledge-title">项目历史</h1><p id="knowledge-description">每个项目一份总记忆，关联历次会话的摘要与清洗正文。</p></div><div class="actions"><span id="readonly" class="hint" hidden>当前为只读访问</span><button id="new-item" class="primary write">＋ 新增资料</button></div></header>
  <nav class="knowledge-tabs" aria-label="整理与规则"><div><a href="#knowledge/intake" data-view="intake">资料待确认 <b id="pending-count"></b></a><a href="#knowledge/library" data-view="library">来源资料</a><a href="#knowledge/learning" data-view="learning">协作学习成果</a><a href="#knowledge/rules" data-view="rules">高级入库设置</a><a href="#knowledge/skill" data-view="skill">处理 Skill</a></div></nav>
  <div class="knowledge-flow" aria-label="记忆处理流程"><ol><li><b>1 · 同步与归属</b></li><li><b>2 · 清洗提炼</b></li><li><b>3 · 分别入库</b></li><li><b>4 · 参考与学习</b></li></ol></div><div id="content" aria-live="polite"></div><div id="toast" role="status" hidden></div><dialog id="dialog" aria-labelledby="knowledge-dialog-title"><div id="dialog-content"></div></dialog>`;
  const $ = s => mount.querySelector(s);
  const esc = v => String(v ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const types = {reference:'参考资料',fact:'事实 / 业务规则',decision:'决策',experience:'经验',playbook:'操作方法',session_summary:'会话摘要',project_summary:'项目摘要',workstream_checkpoint:'任务断点',workflow_policy:'流程规范',constraint:'约束',preference:'偏好',user_profile:'用户资料'};
  const statuses = {active:'已入库',candidate:'待确认',archived:'已归档',deleted:'已删除',superseded:'历史版本'};
  const errors = {revision_conflict:'资料已被其他操作更新，请重新打开后再保存。',project_required:'请选择所属项目，或明确选择全局知识。',project_not_found:'项目未登记，请先在项目知识库添加。',confirm_project_first:'请先确认资料归属，再确认入库。',qa_source_changed:'来源已变化，请到经验问答重新核对和保存答案。',invalid_qa_question:'请填写 4–160 字的明确问题。',invalid_qa_answer:'请填写 5–400 字的简洁答案。',qa_conflict_requires_confirmation:'同一问题和条件已有不同答案。请核对来源后，勾选替换旧答案或保存待确认。',invalid_content:'请填写标题和正文；正文最多 100,000 字。',workstream_move_confirmation_required:'任务断点需要整组迁移，请打开详情操作。',workstream_has_related_tasks:'此任务有关联父任务或子任务，暂不能单独迁移。请先在任务管理中核对关联关系。',workstream_lifecycle_managed_by_task:'任务断点的状态由任务管理维护，请到顶部的项目进展查看任务。',managed_content_create_correction:'此内容由任务或经验生成，请新增补充资料。',invalid_rule_settings:'入库条件格式不正确，请检查 JSON 设置。',invalid_skill_format:'Skill 格式不完整，请保留文件头和 JSON 入库条件。',invalid_skill_frontmatter:'请保留 Skill 的 name 和 description。',identity_conflict:'目标项目已存在同一资料，请先核对重复内容。',alias_conflict:'别名已属于其他项目，请换一个别名。'};
  const state = {view:'projects',project:'__all__',q:'',status:'',type:'',category:'',page:1,queue:'all',rows:[],selected:new Set(),projects:[],rules:null,total:0,document:null,memoryLane:'overview',projectSort:'recent'};
  let toastTimer, renderSequence=0, rulesDirty=false, actionBusy=false;
  function toast(message) { $('#toast').textContent=message; $('#toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('#toast').hidden=true,6500); }
  async function api(route, body) {
    const response=await EvolvAuth.fetch('/api/knowledge/'+route,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok || result.ok===false) throw new Error(errors[result.error] || result.error || '操作失败，请重试。');
    if(body!==undefined&&route!=='rules'&&route!=='preview'&&route!=='extraction/preview'&&(route!=='organize'||body.apply))hooks.changed?.();
    return result;
  }
  function projectName(id) { return state.projects.find(p=>p.project===id)?.display_name || id || '未归属'; }
  function projectOptions(value='__all__', filters=false) {
    return (filters?'<option value="__all__">所有项目</option>':'<option value="">未归属 · 待确认</option>')+`<option value="__global__" ${value==='__global__'?'selected':''}>全局知识</option>`+(filters?`<option value="__none__" ${value==='__none__'?'selected':''}>未归属</option>`:'')+state.projects.filter(p=>p.status==='active'||p.status==='unregistered').map(p=>`<option value="${esc(p.project)}" ${p.project===value?'selected':''}>${esc(p.display_name||p.project)}</option>`).join('');
  }
  const categories={habit:'长期习惯',project_convention:'项目约定',task_requirement:'任务要求',environment:'环境事实',decision:'决策依据',experience:'技术经验',reference:'参考资料'};
  function categoryOptions(value='',all=true){return (all?'<option value="">全部用途</option>':'')+Object.entries(categories).map(([k,v])=>`<option value="${k}" ${k===value?'selected':''}>${v}</option>`).join('');}
  function typeOptions(value='', all=true) { return (all?'<option value="">全部类型</option>':'')+Object.entries(types).map(([k,v])=>`<option value="${k}" ${k===value?'selected':''}>${v}</option>`).join(''); }
  function badge(row) {if(row.status==='active'&&row.ownership?.excluded)return '<span class="badge amber">归属待确认</span>';return `<span class="badge ${row.status==='active'?'green':row.status==='candidate'?'amber':''}">${statuses[row.status]||esc(row.status)}</span>`;}
  function reason(row) {if(row.ingestion_reason)return row.ingestion_reason; if(row.ownership.excluded)return ({unresolved:'缺少明确归属',conflict:'归属线索冲突',unreviewed:'旧资料尚未核对归属',pending:'归属待确认',rejected:'归属已拒绝'})[row.ownership.state]||'归属需要核对'; return row.status==='candidate'?'等待核对资料与来源':'';}
  function route(view,project) { hooks.navigate(view,project); }
  function modal(title,body,footer='') {$('#dialog-content').innerHTML=`<div class="dialog-header"><h2 id="knowledge-dialog-title">${esc(title)}</h2><button class="quiet" data-action="close" aria-label="关闭">✕</button></div><div class="dialog-body">${body}</div>${footer?`<div class="dialog-footer">${footer}</div>`:''}`;if(!$('#dialog').open)$('#dialog').showModal();}
  function close(){$('#dialog').close();}
  async function refreshProjects(){const result=await api('projects');state.projects=result.projects;$('#pending-count').textContent=result.pending||'';return result;}
  async function render(){
    const seq=++renderSequence; $('#content').inert=true;
    $('#new-item').hidden=!['intake','library'].includes(state.view)||(state.view==='library'&&!state.project.startsWith('__'));
    const management=['intake','rules','skill','learning'].includes(state.view)||(state.view==='library'&&state.project==='__all__');
    $('.knowledge-heading').hidden=state.view==='library'&&!state.project.startsWith('__');
    $('.knowledge-tabs').hidden=!management;$('.knowledge-flow').hidden=!management||['skill','learning'].includes(state.view);
    const titles={projects:'项目历史',library:state.project==='__all__'?'来源资料':'项目历史',qa:'经验知识',intake:'资料待确认',rules:'清洗与入库规则',skill:'处理 Skill',learning:'协作学习'};
    $('#knowledge-title').textContent=titles[state.view];$('#knowledge-description').textContent=state.view==='skill'?'修改 AI 的归属判断、资料清洗、提炼与学习方式，并用样例检查效果。':state.view==='qa'?'按分类、项目和条件整理可复用知识；每条问答都有来源。':management?'在同一工作台完成核对、入库、规则调整与学习。':'每个项目一份总记忆，关联历次会话的摘要与清洗正文。';
    try {
    mount.querySelectorAll('[data-view]').forEach(a=>{const active=a.dataset.view===state.view;a.classList.toggle('active',active);a.setAttribute('aria-current',active?'page':'false');});
    const info=await refreshProjects();if(seq!==renderSequence)return;
    $('#content').onclick=null;$('#content').oninput=null;$('#content').onchange=null;
    if(state.view==='learning'){await EvolvLearning.render({mount:$('#content'),api,esc,toast,detail,projects:state.projects,onDirty:v=>rulesDirty=v,onBusy:v=>{actionBusy=v;mount.setAttribute('aria-busy',String(v));}});return;}
    if(state.view==='projects'){renderProjects(info);return;}
    if(state.view==='qa'){$('#content').innerHTML='<div class="memory-manager"></div>';await EvolvMemory.render({mount:$('.memory-manager'),doc:{project:state.project,name:'所有项目',source_ids:[],sessions:[],qa_count:0},projects:state.projects,api,esc,toast,lane:'qa',standalone:true,onLane:()=>{},onBusy:v=>{actionBusy=v;mount.setAttribute('aria-busy',String(v));},onChanged:render,onSource:detail,onDirty:v=>rulesDirty=v});return;}
    if(state.view==='skill'){await EvolvPipeline.render({mount:$('#content'),api,esc,toast,onDirty:v=>rulesDirty=v,onBusy:v=>{actionBusy=v;mount.setAttribute('aria-busy',String(v));}});return;}
    if(['rules'].includes(state.view)){state.rules=await api('rules');if(seq!==renderSequence)return;renderRules();return;}
    const params=new URLSearchParams({project:state.project,q:state.q,status:state.status,content_type:state.type,category:state.category,page:state.page});
    if(state.view==='intake')params.set('queue',state.queue);
    const result=await api('items?'+params);if(seq!==renderSequence)return;
    state.document=null;
    if(state.view==='library'&&state.projects.some(p=>p.project===state.project&&p.status==='active')){
      state.document=await api('project-memory?'+new URLSearchParams({project:state.project}));if(seq!==renderSequence)return;
    }
    if(state.view==='library'&&state.project==='__global__')state.document={project:'',name:'全局知识',summary:'跨项目通用的经验与习惯，在经验问答中维护。',body:'',source_ids:[],sessions:[],qa_count:(await api('qa?project=__global__&state=active')).total};
    state.rows=result.items;state.total=result.total;state.selected.clear();await renderLibrary(result);
    } finally {if(seq===renderSequence)$('#content').inert=false;}
  }
  let projectInfo=null;
  const projectSorts={recent:'最近更新',name:'项目名称 A–Z',materials:'资料最多',pending:'待确认最多'};
  const projectCollator=new Intl.Collator('zh-CN',{numeric:true,sensitivity:'base'});
  function renderProjects(info){
    projectInfo=info;
    const active=info.projects.filter(p=>p.status==='active');
    const other=info.projects.filter(p=>p.status!=='active');
    $('#content').innerHTML=`<section class="project-collection" aria-label="项目历史目录">
      <div class="project-collection-top"><div class="project-collection-summary"><span><strong>${active.length}</strong> 个项目</span><i></i><span><strong>${active.reduce((n,p)=>n+p.total,0).toLocaleString('zh-CN')}</strong> 份项目资料</span><i></i><span><strong>${active.reduce((n,p)=>n+p.pending,0).toLocaleString('zh-CN')}</strong> 份待确认</span></div><button class="primary write" data-action="project-new">＋ 添加项目</button></div>
      <div class="project-searchbar"><div class="project-search-field"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/></svg><input id="project-search" type="search" maxlength="200" placeholder="搜索项目名称、标识或别名…" aria-label="搜索项目" value="${esc(state.q)}"><button class="quiet" data-action="project-search-clear" aria-label="清除项目搜索" ${state.q?'':'hidden'}>清除</button></div><label class="project-sort-label" for="project-sort">排序<select id="project-sort">${Object.entries(projectSorts).map(([key,label])=>`<option value="${key}" ${state.projectSort===key?'selected':''}>${label}</option>`).join('')}</select></label></div>
      <div class="project-results-heading"><h2 id="project-results-title">全部项目</h2><span id="project-result-count" role="status" aria-live="polite"></span></div><div id="project-results"></div>
      <div class="project-utilities"><a href="#knowledge/library?project=__global__" data-action="open-project" data-project="__global__"><span class="project-utility-icon">◎</span><span><strong>全局知识</strong><small>跨项目的习惯、约定与通用资料</small></span><span aria-hidden="true">↗</span></a><a href="#knowledge/intake?project=__none__" data-action="project-queue" data-project="__none__"><span class="project-utility-icon">?</span><span><strong>未归属资料</strong><small>核对来源，再放回对应项目</small></span><span aria-hidden="true">↗</span></a></div>
      ${other.length?`<details class="project-other-names"><summary>待整理名称与归档项目 <span>${other.length}</span></summary><p class="hint">这些名称单独保留，不重复显示成正式项目。可核对别名和来源后整理归属。</p>${other.map(p=>{const canonical=active.find(x=>x.aliases.includes(p.project));return `<div class="conversation-row"><strong>${esc(p.display_name||p.project)}</strong><span class="badge">${p.status==='archived'?'已归档项目':canonical?'已登记别名 · '+esc(canonical.display_name||canonical.project):'项目名待确认'}</span><button data-action="open-project" data-project="${esc(p.project)}">核对 ${p.total} 份资料</button></div>`;}).join('')}</details>`:''}
    </section>`;
    renderProjectCards();
  }
  function renderProjectCards(){
    const active=projectInfo.projects.filter(p=>p.status==='active');
    const terms=state.q.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const rows=active.filter(p=>{const text=[p.display_name,p.project,...p.aliases].join(' ').toLocaleLowerCase();return terms.every(t=>text.includes(t));});
    rows.sort((a,b)=>{
      const byName=()=>projectCollator.compare(a.display_name||a.project,b.display_name||b.project)||a.project.localeCompare(b.project);
      if(state.projectSort==='name')return byName();
      if(state.projectSort==='materials')return b.total-a.total||byName();
      if(state.projectSort==='pending')return b.pending-a.pending||byName();
      return String(b.updated_at||'').localeCompare(String(a.updated_at||''))||byName();
    });
    $('#project-results-title').textContent=terms.length?'搜索结果':'全部项目';
    $('#project-result-count').textContent=terms.length?`${rows.length} / ${active.length} 个项目`:`${active.length} 个项目 · ${projectSorts[state.projectSort]}`;
    $('[data-action="project-search-clear"]').hidden=!state.q;
    $('#project-results').innerHTML=rows.length?`<div class="project-grid">${rows.map(p=>{
      const name=p.display_name||p.project;
      const tone=[...p.project].reduce((n,c)=>n+c.codePointAt(0),0)%4;
      const date=p.updated_at?new Date(p.updated_at.replace(' ','T')+'Z'):null;
      const updated=date&&!Number.isNaN(date.valueOf())?new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}).format(date).replaceAll('/','.'):'暂无更新';
      const sameName=active.some(x=>x.project!==p.project&&(x.display_name||x.project)===name);
      return `<article class="project-card history-project-card tone-${tone}" data-project-card="${esc(p.project)}">
        <div class="project-card-top"><span class="project-cover-icon" aria-hidden="true"><svg viewBox="0 0 32 32" fill="none"><path d="M6 7.5h8l3 3H26v15H6z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M11 16h10M11 20h7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span><span class="project-card-state ${p.pending?'needs-review':''}">${p.pending?'有资料待确认':p.total?'已整理':'等待第一份记忆'}</span><button class="project-settings quiet write" data-action="project-edit" data-project="${esc(p.project)}" aria-label="编辑 ${esc(name)}" title="项目设置">···</button></div>
        <h3><a href="#knowledge/library?project=${encodeURIComponent(p.project)}" data-action="open-project" data-project="${esc(p.project)}" title="${esc(name)}">${esc(name)}</a></h3><p class="project-card-id" title="项目标识：${esc(p.project)}">${esc(p.project)}</p>
        <div class="project-aliases" title="${esc(p.aliases.join(' · '))}">${p.aliases.length?p.aliases.slice(0,2).map(alias=>`<span>${esc(alias)}</span>`).join('')+(p.aliases.length>2?`<span>+${p.aliases.length-2}</span>`:''):'<span class="alias-empty">项目摘要 · 历次对话 · 来源资料</span>'}</div>
        ${sameName?`<p class="project-name-notice">同名项目，请按上方标识区分</p>`:''}
        <div class="project-card-metrics"><div><strong>${p.total.toLocaleString('zh-CN')}</strong><span>份资料</span></div><div class="${p.pending?'metric-pending':''}"><strong>${p.pending.toLocaleString('zh-CN')}</strong><span>份待确认</span></div>${p.pending?`<a href="#knowledge/intake?project=${encodeURIComponent(p.project)}" data-action="project-queue" data-project="${esc(p.project)}" aria-label="核对 ${esc(name)} 的待确认资料">去核对 ↗</a>`:''}</div>
        <div class="project-card-footer"><time title="${esc(p.updated_at||'尚无更新时间')}" ${date&&!Number.isNaN(date.valueOf())?`datetime="${date.toISOString()}"`:''}>${date?'更新于 ':''}${updated}</time><span>查看历史 <span class="project-open-arrow" aria-hidden="true">→</span></span></div>
      </article>`;
    }).join('')}</div>`:`<div class="project-empty"><span aria-hidden="true">⌕</span><h3>${terms.length?'没有找到匹配的项目':'从第一个项目开始'}</h3><p>${terms.length?'试试项目名称、标识或业务别名。':'添加项目后，历次对话与知识资料会在这里积累。'}</p>${terms.length?'<button data-action="project-search-clear">清除搜索</button>':'<button class="primary write" data-action="project-new">＋ 添加项目</button>'}</div>`;
  }
  function filterProjects(){renderProjectCards();history.replaceState(null,'',hash());}
  async function renderLibrary(result){
    const intake=state.view==='intake';
    $('#content').innerHTML=`${intake?'':'<p class="intro">在一个地方查看和管理所有项目的资料。支持搜索全文、编辑内容、调整归属和入库状态。</p>'}<div class="toolbar"><input id="search" type="search" placeholder="搜索标题、正文或关键词…" aria-label="搜索资料" value="${esc(state.q)}"><select id="filter-project" aria-label="筛选项目">${projectOptions(state.project,true)}</select><select id="filter-type" aria-label="筛选资料类型">${typeOptions(state.type)}</select><select id="filter-category" aria-label="筛选知识用途">${categoryOptions(state.category)}</select>${intake?`<select id="filter-queue" aria-label="待确认类别"><option value="all">全部待确认</option><option value="candidate" ${state.queue==='candidate'?'selected':''}>内容待确认</option><option value="ownership" ${state.queue==='ownership'?'selected':''}>归属待确认</option></select>`:`<select id="filter-status" aria-label="筛选资料状态"><option value="">当前资料</option>${Object.entries(statuses).map(([k,v])=>`<option value="${k}" ${k===state.status?'selected':''}>${v}</option>`).join('')}</select>`}<button data-action="search">搜索</button></div><div class="section-head ${intake?'intake-actionbar':''}"><h2>${state.project==='__all__'?(intake?'待确认资料':'全部资料'):state.project==='__none__'?'未归属资料':state.project==='__global__'?'全局知识':esc(projectName(state.project))} <span class="hint">${result.total} 份</span></h2>${intake?'<div class="actions"><button data-view="skill" class="intake-skill-button">编辑归属与清洗 Skill</button><button class="primary write" data-action="organize-page"><span aria-hidden="true">✦</span> AI 整理</button></div>':''}</div><div id="selection" class="selection" hidden></div>${result.items.length?`<div class="table-wrap"><table><thead><tr><th class="check"><input id="select-all" type="checkbox" aria-label="选择本页全部资料"></th><th>资料 / 来源线索</th><th>所属项目</th><th>类型</th><th>状态</th></tr></thead><tbody>${result.items.map(row=>`<tr><td class="check"><input type="checkbox" data-select="${row.id}" aria-label="选择资料 ${row.id}"></td><td><button class="item-link" data-action="detail" data-id="${row.id}">${esc(row.title)}</button><div class="excerpt">${esc(row.body)}</div>${intake&&reason(row)?`<div class="reason">${esc(reason(row))}</div>`:''}</td><td>${esc(row.project?projectName(row.project):row.scope==='global'?'全局知识':'未归属')}${row.ownership.excluded?'<div class="reason">归属待确认</div>':''}</td><td><span class="badge">${types[row.content_type]||esc(row.content_type)}</span></td><td>${badge(row)}<div class="hint" style="margin-top:8px">${esc(row.updated_at?.slice(0,10))}</div></td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">这里暂时没有资料<br>可以调整筛选条件，或新增一份资料。</div>'}<div class="pagination"><span>共 ${result.total} 份 · 第 ${state.page} / ${Math.max(1,Math.ceil(result.total/result.page_size))} 页</span><div class="actions"><button data-action="prev" ${state.page===1?'disabled':''}>上一页</button><button data-action="next" ${state.page*result.page_size>=result.total?'disabled':''}>下一页</button></div></div>`;
    if(state.document){
      const fragments=document.createElement('details');fragments.className='project-fragments';
      fragments.innerHTML='<summary>来源片段 · 查看、纠正与更改归属</summary>';
      while($('#content').firstChild)fragments.append($('#content').firstChild);
      $('#content').innerHTML='<div class="memory-manager"></div>';
      await EvolvMemory.render({mount:$('.memory-manager'),doc:state.document,projects:state.projects,api,esc,toast,lane:state.memoryLane,onLane:v=>{state.memoryLane=v;history.replaceState(null,'',hash());},onBusy:v=>{actionBusy=v;mount.setAttribute('aria-busy',String(v));},onChanged:render,onSource:detail,onDirty:v=>rulesDirty=v});
      $('#content').append(fragments);
    }

  }

  function selection(){const box=$('#selection');box.hidden=!state.selected.size;box.innerHTML=`<span>已选 ${state.selected.size} 份资料</span><button class="write" data-action="batch-assign">更改归属</button><button class="write" data-action="batch-publish">确认入库</button><button class="write" data-action="organize-selected">AI 整理所选</button><button class="write" data-action="batch-archive">归档</button>`;}
  function projectEditor(project){const p=state.projects.find(x=>x.project===project);modal(p?'项目设置':'添加项目',`<div class="field"><label for="project-id">项目标识</label><input id="project-id" value="${esc(p?.project)}" ${p?'readonly':''} placeholder="例如 eva、evolvmem"><small>项目标识不必与本地文件夹同名。Windows 按“目录 → 项目标识”明确映射；多个目录可属于同一项目。</small></div><div class="field"><label for="project-name">显示名称</label><input id="project-name" value="${esc(p?.display_name)}" placeholder="例如 EVA 智能客服"></div><div class="field"><label for="project-aliases">业务别名</label><textarea id="project-aliases" placeholder="每行一个，例如：智能客服">${esc(p?.aliases.join('\n'))}</textarea><small>名称与别名会参与资料归属判断，请避免含糊的通用词。</small></div>`,`<button data-action="close">取消</button><button class="primary write" data-action="project-save">保存项目</button>`);}
  let editing=null, proposalIds=[];
  function newItem(seed={}){editing=null;const project=seed.project??(state.project.startsWith('__')?'':state.project);modal('新增知识资料',`${seed.source?`<div class="rule-note">${esc(seed.source)}</div>`:''}<div class="field"><label for="item-title">资料标题</label><input id="item-title" maxlength="200" value="${esc(seed.title)}" placeholder="给资料一个便于查找的名称"></div><div class="form-grid"><div class="field"><label for="item-project">所属项目</label><select id="item-project">${projectOptions(project)}</select></div><div class="field"><label for="item-type">资料类型</label><select id="item-type">${typeOptions('reference',false).replace(/<option value="(workstream_checkpoint|project_summary)".*?<\/option>/g,'')}</select></div></div><div class="field"><label for="item-body">资料正文</label><textarea class="body" id="item-body" placeholder="粘贴文本或 Markdown，保留业务规则、依据与适用条件。">${esc(seed.body)}</textarea><div class="actions"><label class="hint" for="import-file">或导入文本 / Markdown</label><input id="import-file" type="file" accept=".txt,.md,.markdown,text/plain,text/markdown"></div></div><div class="field"><label for="item-source">来源说明</label><input id="item-source" value="${esc(seed.source||'用户录入')}" placeholder="会话、文件名称或原文链接"></div><div class="hint">按规则处理会立即判断：明确资料入库，疑难资料进入待确认。</div>`,`<button data-action="create-draft">保存到待确认</button><button class="primary write" data-action="create-auto">按规则处理</button>`);}
  async function detail(id){editing=await api('items/'+id);const r=editing;modal('资料详情',`<div class="meta-line">${badge(r)}<span class="badge">${types[r.content_type]}</span><span class="hint">编号 ${r.id} · ${esc(r.updated_at?.slice(0,10))}</span></div><div class="field"><label for="item-title">资料标题</label><input id="item-title" value="${esc(r.title)}" maxlength="200"></div><div class="form-grid"><div class="field"><label for="item-project">项目归属</label><select id="item-project">${projectOptions(r.project|| (r.scope==='global'?'__global__':''))}</select></div><div class="field"><label>归属操作</label><button class="write" data-action="assign">确认 / 更改归属</button><small>${r.workstream_id?'变更将整组迁移任务和历史断点。':'人工确认会优先于自动判断。'}</small></div></div>${reason(r)?`<div class="rule-note">${esc(reason(r))}</div>`:''}<div class="field"><label for="item-body">正文</label><textarea class="body" id="item-body" ${r.managed_content?'readonly':''}>${esc(r.body)}</textarea>${r.managed_content?'<small>这是系统生成的任务、摘要或结构化经验。内容随来源更新；可新增补充资料记录更正。</small><button class="write" data-action="correction">新增补充 / 更正资料</button>':''}</div><div class="form-grid"><div class="field"><label for="item-category">知识用途</label><select id="item-category">${categoryOptions(r.learning.category,false)}</select><small>${esc(r.learning.basis==='unreviewed'?'按旧类型建议，尚未核对':r.learning.basis==='explicit'?'来自用户明确表达':r.learning.basis==='manual'?'用户已修正':'AI 推断，需结合来源判断')}</small></div><div class="field"><label for="item-trigger">适用时机</label><input id="item-trigger" value="${esc(r.learning.trigger)}"></div></div><button class="write" data-action="classification-save">保存用途与适用时机</button>${r.learning.evidence.map(e=>`<blockquote>${esc(e.quote)}</blockquote><p class="hint">${esc(e.session)} · 消息 ${e.message_index+1}</p>`).join('')}${EvolvExtraction.process(r.learning,esc)}<div class="field"><label for="item-tags">标签</label><input id="item-tags" value="${esc(r.tags.join('，'))}" placeholder="用逗号分隔"></div><div class="section-head"><h3>原始来源</h3><span class="hint">${r.sources.length} 条关联</span></div>${r.sources.map(s=>`<div class="source">${esc(s.source_ref||s.source_kind)}<small>${esc(s.adapter||s.source_kind)} · ${esc(s.created_at)}${s.source_project?' · '+esc(s.source_project):''}</small></div>`).join('')||'<p class="hint">旧记录没有关联原始来源，确认前请核对正文。</p>'}${r.rule_revision?`<p class="version">处理规则版本 ${esc(r.rule_revision.slice(0,12))}</p>`:''}`,`<button class="danger write" data-action="item-delete">删除</button>${r.status==='active'?'<button class="write" data-action="item-archive">归档</button>':r.status==='candidate'?'<button class="write" data-action="item-reject">暂不入库</button><button class="write" data-action="item-publish">确认入库</button>':'<button class="write" data-action="item-restore">恢复入库</button>'}<button class="primary write" data-action="item-save">保存内容</button>`);}
  function renderRules(){rulesDirty=false;const r=state.rules,s=r.settings;
    if(state.view==='skill'){$('#content').innerHTML=`<p class="intro">这是 AI 读取的知识库管理 Skill。说明文字与可执行入库条件保存在同一份文件中，保存后用于后续提炼和整理。</p><section class="panel"><div class="section-head" style="margin-top:0"><div><h2>SKILL.md</h2><p class="version">当前版本 ${r.revision.slice(0,12)}</p></div><div class="actions"><button data-action="skill-download">下载文件</button><button class="write" data-action="rule-reset">恢复默认</button><button class="primary write" data-action="skill-save">保存 Skill</button></div></div><textarea id="skill-text" class="code skill-editor" aria-label="管理 Skill 内容" spellcheck="false">${esc(r.skill)}</textarea><p>请保留文件头与 JSON 条件。修改自然语言说明会影响 AI 的提炼；明确的阈值、忽略词与项目覆盖由入库程序执行。</p></section>`;return;}
    $('#content').innerHTML=`<div class="rules-toolbar"><div><h2>入库规则</h2><p>明确资料自动入库，疑难资料留待确认。</p></div><div class="actions"><span id="rules-save-state" class="hint" role="status">已保存</span><button class="primary write" data-action="rules-save">保存更改</button></div></div>
    <div class="rules-stages" role="group" aria-label="入库规则操作步骤"><button data-rule-stage="settings" aria-pressed="true">1 · 调整规则</button><button data-rule-stage="trial" aria-pressed="false">2 · 试运行效果</button><span>3 · 保存更改后生效</span></div><div class="rules-layout">
      <section class="panel rules-editor" data-rule-stage-pane="settings" aria-labelledby="rules-editor-title">
        <header class="rules-panel-heading"><h3 id="rules-editor-title">规则设置</h3><p>修改后统一保存，作用于后续入库。</p></header>
        <div class="form-grid"><div class="field"><label for="min-confidence">自动入库置信度</label><input id="min-confidence" type="number" min="0" max="1" step="0.05" value="${s.auto_min_confidence}"><small>0–1，数值越高，自动入库越谨慎。</small></div><div class="field"><label for="require-source">来源要求</label><select id="require-source"><option value="true" ${s.require_source?'selected':''}>需要可核对来源</option><option value="false" ${!s.require_source?'selected':''}>不强制要求来源</option></select></div></div>
        <div id="extraction-settings"></div>
        <details class="rules-disclosure"><summary>内容筛选与项目例外<span>字数、忽略词、项目条件</span></summary><div class="rules-disclosure-body">
          <div class="form-grid"><div class="field"><label for="min-chars">正文最少字数</label><input id="min-chars" type="number" min="1" value="${s.min_chars}"></div><div class="field"><label for="max-chars">正文最多字数</label><input id="max-chars" type="number" min="1" max="100000" value="${s.max_chars}"></div></div>
          <div class="field"><label for="ignore-keywords">忽略关键词</label><textarea id="ignore-keywords" placeholder="每行一个；命中的资料不进入正式知识库">${esc(s.ignore_keywords.join('\n'))}</textarea></div>
          <div class="field"><label for="ambiguous-names">不作为项目线索的通用词</label><textarea id="ambiguous-names" placeholder="每行一个，例如：设计、测试、平台">${esc((s.ambiguous_project_names||[]).join('\n'))}</textarea><small>避免把常见业务词误认成项目。</small></div>
          <div class="field"><label for="project-overrides">项目专属条件（JSON）</label><textarea class="code" id="project-overrides" spellcheck="false">${esc(JSON.stringify(s.project_overrides,null,2))}</textarea><small>例：{"eva": {"auto_min_confidence": 0.95}}。其余项目沿用默认条件。</small></div>
        </div></details>
        <details class="rules-disclosure"><summary>AI 整理说明<span>修改归属判断与保留范围</span></summary><div class="rules-disclosure-body"><div class="field"><label for="rule-instructions">整理说明</label><textarea id="rule-instructions" class="rules-long-text">${esc(r.instructions)}</textarea><small>写明来源优先级、保留范围和例外，供 AI 整理时参考。</small></div></div></details>
        <footer class="rules-meta"><span id="rules-version" class="version">版本 ${r.revision.slice(0,12)}</span><a href="#knowledge/skill" data-view="skill">编辑完整 Skill ↗</a></footer>
      </section>
      <section class="panel rules-playground" data-rule-stage-pane="trial" hidden aria-labelledby="rules-playground-title">
        <header class="rules-panel-heading"><h3 id="rules-playground-title">试运行</h3><p>先看效果，再决定是否保存规则。</p></header>
        <div class="rules-modes" role="group" aria-label="选择试运行方式"><button type="button" data-rule-mode="conversation" aria-pressed="true">对话提炼</button><button type="button" data-rule-mode="material" aria-pressed="false">资料判断</button></div>
        <div id="extraction-preview" data-rule-pane="conversation"></div>
        <div data-rule-pane="material" hidden>
          <p class="hint">检查资料归属与入库结果，使用已保存的规则。</p>
          <div class="field"><label for="sample-title">资料标题</label><input id="sample-title" placeholder="例如：EVA 客服退款规则"></div>
          <div class="field"><label for="sample-body">正文</label><textarea id="sample-body" placeholder="输入包含项目线索的资料内容…"></textarea></div>
          <div class="form-grid"><div class="field"><label for="sample-confidence">资料置信度</label><input id="sample-confidence" type="number" min="0" max="1" step="0.05" value="0.9"></div><div class="field"><label for="sample-source">来源</label><input id="sample-source" value="规则预览样例"></div></div>
          <button data-action="rule-preview">判断入库结果</button><div id="preview-result" hidden class="preview-result" role="status"></div>
          <p class="hint rules-preview-note">仅检查，不写入知识库。</p>
        </div>
      </section>
    </div><div id="extraction-result" class="rules-result" role="status" hidden data-rule-pane="conversation"></div>`;
    EvolvExtraction.mount({editor:$('#extraction-settings'),preview:$('#extraction-preview'),output:$('#extraction-result'),api,esc,projects:state.projects,policy:r,getDraft:extractionDraft,onDirty:markRulesDirty,onBusy:v=>{actionBusy=v;mount.setAttribute('aria-busy',String(v));}});
    $('#content').querySelectorAll('[data-rule-stage]').forEach(button=>button.onclick=()=>{
      if(actionBusy)return;
      const stage=button.dataset.ruleStage;
      $('#content').querySelectorAll('[data-rule-stage]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
      $('#content').querySelectorAll('[data-rule-stage-pane]').forEach(p=>p.hidden=p.dataset.ruleStagePane!==stage);
      $('#extraction-result').hidden=stage!=='trial'||$('[data-rule-mode="material"]').getAttribute('aria-pressed')==='true';
    });
    $('#content').querySelectorAll('[data-rule-mode]').forEach(button=>button.onclick=()=>{
      if(actionBusy)return;
      const mode=button.dataset.ruleMode;
      $('#content').querySelectorAll('[data-rule-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
      $('#content').querySelectorAll('[data-rule-pane]').forEach(p=>p.hidden=p.dataset.rulePane!==mode);
    });
  }
  function markRulesDirty(){rulesDirty=true;const status=$('#rules-save-state');if(status){status.textContent='有未保存的更改';status.classList.add('unsaved');}}

  function extractionDraft(){
    const settings=EvolvExtraction.settings(mount,{...state.rules.settings,
      ambiguous_project_names:$('#ambiguous-names').value.split('\n').map(s=>s.trim()).filter(Boolean),
      auto_min_confidence:Number($('#min-confidence').value),require_source:$('#require-source').value==='true',
      min_chars:Number($('#min-chars').value),max_chars:Number($('#max-chars').value),
      ignore_keywords:$('#ignore-keywords').value.split('\n').map(s=>s.trim()).filter(Boolean),
      project_overrides:JSON.parse($('#project-overrides').value)});
    return {expected_revision:state.rules.revision,settings,instructions:$('#rule-instructions').value};
  }
  function selectedItems(){return state.rows.filter(r=>state.selected.has(r.id)).map(r=>({id:r.id,expected_revision:r.revision}));}
  async function batch(action,extra={}){const result=await api('batch',{action,items:selectedItems(),...extra});close();await render();toast(`已处理 ${result.succeeded} 份${result.failed?'；'+result.failed+' 份未处理：'+result.items.filter(r=>!r.ok).map(r=>errors[r.error]||r.error).join('；'):''}`);}
  async function organize(ids,apply=false){proposalIds=ids;const result=await api('organize',{ids,apply});modal(apply?'整理完成':'整理结果预览',`<div class="rule-note">${apply?`已自动入库 ${result.applied} 份，忽略归档 ${result.ignored} 份，${result.pending} 份仍需确认。`:'仅处理项目明确且符合当前规则的资料。人工决定和系统生成资料会保留，疑难资料继续待确认。'}</div><div class="proposals">${result.items.map(p=>`<div class="proposal"><strong>${esc(p.title)}</strong><p><span class="badge ${p.action==='auto'?'green':'amber'}">${p.applied?(p.action==='ignore'?'已归档':'已入库'):p.action==='auto'?'可自动入库':p.action==='ignore'?'不入库':'待确认'}</span> ${esc(p.project?projectName(p.project):'未确定项目')}</p><p>${esc(p.reason)}</p></div>`).join('')}</div>`,apply?'<button class="primary" data-action="close">完成</button>':`<button data-action="close">取消</button><button class="primary write" data-action="organize-apply" ${!result.items.some(p=>['auto','ignore'].includes(p.action))?'disabled':''}>按当前规则整理明确资料</button>`);if(apply)await render();}
  const actions={
    retry:()=>render(),
    'project-search-clear':()=>{state.q='';$('#project-search').value='';filterProjects();$('#project-search').focus();},
    conversation:async b=>{const r=await api('conversations/'+b.dataset.id+'?'+new URLSearchParams({project:b.dataset.project}));modal('整理后的对话',`<p class="hint">已去除工具记录、系统注入和内部推理；保留需求、回复、纠正与决定，敏感值已脱敏。</p><pre class="project-memory-text">${esc(r.text||'没有可展示的对话内容。')}</pre>`,'<button data-action="close">关闭</button>');},
    close, 'project-new':()=>projectEditor(), 'project-edit':b=>projectEditor(b.dataset.project),
    'open-project':b=>route('library',b.dataset.project), 'project-queue':b=>route('intake',b.dataset.project),
    search:()=>{state.q=$('#search').value;state.page=1;return render();},prev:()=>{state.page--;return render();},next:()=>{state.page++;return render();},
    'project-save':async()=>{await api('projects',{project:$('#project-id').value.trim(),display_name:$('#project-name').value.trim(),aliases:$('#project-aliases').value.split('\n').map(s=>s.trim()).filter(Boolean)});close();await render();toast('项目与别名已保存');},
    detail:b=>detail(Number(b.dataset.id)),
    'create-auto':()=>create('auto'),'create-draft':()=>create('draft'),
    'item-save':async()=>{const r=await api(`items/${editing.id}/update`,{expected_revision:editing.revision,title:$('#item-title').value,body:$('#item-body').value,tags:$('#item-tags').value.split(/[,，]/).map(s=>s.trim()).filter(Boolean)});await render();await detail(r.id);toast('资料内容已保存');},
    assign:async()=>{const p=$('#item-project').value;if(!p)throw new Error('请选择项目或全局知识。');if(editing.workstream_id&&!confirm('将迁移整个任务及其全部历史断点到所选项目。确认继续？'))return;await api(`items/${editing.id}/assign`,{expected_revision:editing.revision,project:p==='__global__'?'':p,move_workstream:!!editing.workstream_id});await render();await detail(editing.id);toast('项目归属已确认');},
    'classification-save':async()=>{const r=await api('learning/memories/'+editing.id,{expected_revision:editing.revision,category:$('#item-category').value,trigger:$('#item-trigger').value});await render();await detail(r.id);toast('分类已修正，受影响的协作规则需要重新确认');},
    correction:()=>newItem({project:editing.project,title:'补充：'+editing.title,source:`对知识资料 #${editing.id} 的补充 / 更正`}),
    'batch-assign':()=>modal('批量更改归属',`<p class="intro" style="margin-top:20px">已选择 ${state.selected.size} 份资料。任务断点请在详情中整组迁移。</p><div class="field"><label for="batch-project">目标项目</label><select id="batch-project">${projectOptions('')}</select></div>`,'<button data-action="close">取消</button><button class="primary write" data-action="batch-assign-save">确认更改</button>'),
    'batch-assign-save':()=>{const p=$('#batch-project').value;if(!p)throw new Error('请选择目标项目。');return batch('assign',{project:p==='__global__'?'':p});},
    'batch-publish':()=>batch('publish'),'batch-archive':()=>batch('archive'),
    'organize-selected':()=>organize([...state.selected]),'organize-page':()=>organize(state.rows.map(r=>r.id)), 'organize-apply':()=>organize(proposalIds,true),
    'rules-save':async()=>{state.rules=await api('rules',extractionDraft());rulesDirty=false;$('#rules-save-state').textContent='已保存';$('#rules-save-state').classList.remove('unsaved');$('#rules-version').textContent='版本 '+state.rules.revision.slice(0,12);$('#extraction-result').replaceChildren();$('#preview-result').hidden=true;toast('提炼与入库规则已保存，后续处理会读取同一版本');},
    'skill-save':async()=>{state.rules=await api('rules',{expected_revision:state.rules.revision,skill:$('#skill-text').value});renderRules();toast('管理 Skill 已保存，与入库规则共用同一来源');},
    'rule-reset':async()=>{if(!confirm('将完整 Skill 和入库条件恢复为默认内容，确认继续？'))return;state.rules=await api('rules',{expected_revision:state.rules.revision,reset:true});renderRules();toast('已恢复默认规则');},
    'skill-download':()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([$('#skill-text').value],{type:'text/markdown;charset=utf-8'}));a.download='SKILL.md';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);},
    'rule-preview':async()=>{const r=await api('preview',{title:$('#sample-title').value,body:$('#sample-body').value,confidence:Number($('#sample-confidence').value),source:$('#sample-source').value});$('#preview-result').hidden=false;$('#preview-result').textContent=`${({auto:'自动入库',review:'待确认',ignore:'不入库'})[r.action]}\n项目：${r.project?projectName(r.project):r.scope==='global'?'全局知识':'未确定'}\n原因：${r.reason}\n规则版本：${r.rule_revision.slice(0,12)}`;}
  };
  async function create(action){const p=$('#item-project').value;const row=await api('items',{title:$('#item-title').value,body:$('#item-body').value,source:$('#item-source').value,content_type:$('#item-type').value,project:p==='__global__'?'':p,scope:p==='__global__'?'global':'project',action});close();await render();toast(row.status==='active'?'资料已入库':row.status==='archived'?'资料未入库，已归档：'+row.ingestion_reason:'资料已保存到待确认');}
  ['archive','restore','publish','reject','delete'].forEach(action=>actions['item-'+action]=async()=>{if(action==='delete'&&!confirm('删除这份资料？删除后可在“已删除”筛选中找回。'))return;await api(`items/${editing.id}/transition`,{expected_revision:editing.revision,action});close();await render();toast('资料状态已更新');});
  mount.addEventListener('click',async event=>{const tab=event.target.closest('[data-view]');if(tab){event.preventDefault();route(tab.dataset.view,['library','intake'].includes(tab.dataset.view)?'__all__':undefined);return;}const b=event.target.closest('[data-action]');if(!b)return;event.preventDefault();const action=actions[b.dataset.action];if(!action||actionBusy)return;const locks=['classification-save','rules-save','skill-save','rule-reset','rule-preview','conversation','detail','item-save','assign','create-auto','create-draft','project-save','batch-assign-save','batch-publish','batch-archive','organize-selected','organize-page','organize-apply'].includes(b.dataset.action)||b.dataset.action.startsWith('item-');actionBusy=locks;mount.setAttribute('aria-busy',String(locks));b.disabled=true;try{await action(b);}catch(error){toast(error.message);}finally{actionBusy=false;mount.setAttribute('aria-busy','false');b.disabled=false;}});
  mount.addEventListener('change',async event=>{const e=event.target;try{if(e.id==='project-sort'){state.projectSort=e.value;filterProjects();}else if(e.dataset.select){e.checked?state.selected.add(Number(e.dataset.select)):state.selected.delete(Number(e.dataset.select));selection();}else if(e.id==='select-all'){state.selected=e.checked?new Set(state.rows.map(r=>r.id)):new Set();mount.querySelectorAll('[data-select]').forEach(x=>x.checked=e.checked);selection();}else if(e.id.startsWith('filter-')){state[({'filter-project':'project','filter-type':'type','filter-category':'category','filter-status':'status','filter-queue':'queue'})[e.id]]=e.value;state.page=1;await render();}else if(e.id==='import-file'&&e.files[0]){if(e.files[0].size>400000)throw new Error('文本文件过大，请使用不超过 100,000 字的资料。');$('#item-body').value=await e.files[0].text();if(!$('#item-title').value)$('#item-title').value=e.files[0].name.replace(/\.(txt|md|markdown)$/i,'');$('#item-source').value=e.files[0].name;}}catch(error){toast(error.message);}});
  mount.addEventListener('input',e=>{if(e.target.id==='project-search'){state.q=e.target.value;filterProjects();}if(e.target.id==='search')state.q=e.target.value;if(['skill-text','min-confidence','require-source','min-chars','max-chars','ignore-keywords','ambiguous-names','project-overrides','rule-instructions'].includes(e.target.id))markRulesDirty();});
  mount.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.id==='search')actions.search().catch(x=>toast(x.message));});
  $('#new-item').onclick=()=>newItem();
  mount.classList.toggle('readonly',!EvolvAuth.canWrite);$('#readonly').hidden=EvolvAuth.canWrite;
  window.addEventListener('beforeunload',e=>{if(rulesDirty){e.preventDefault();e.returnValue='';}});
  function hash(){const p=new URLSearchParams();if(state.project!=='__all__')p.set('project',state.project);if(state.q)p.set('q',state.q);if(state.view==='projects'&&state.projectSort!=='recent')p.set('sort',state.projectSort);if(state.memoryLane!=='overview'&&state.view==='library')p.set('lane',state.memoryLane);return `#knowledge/${state.view}${p.size?'?'+p:''}`;}
  return {
    kind:'memories', integrated:true,
    get project(){return state.project;}, set project(value){state.project=value||'__all__';state.q='';state.type='';state.category='';state.status='';state.queue='all';state.view='library';state.memoryLane='overview';},
    get query(){return state.q;}, set query(value){state.q=value;state.view='library';state.memoryLane='overview';},
    set view(value){if(['projects','library','intake','rules','skill','learning','qa'].includes(value))state.view=value;},
    hash,
    set projectSort(value){state.projectSort=Object.hasOwn(projectSorts,value)?value:'recent';},
    set lane(value){state.memoryLane=['overview','history','qa'].includes(value)?value:'overview';},
    canLeave(){if(actionBusy){toast('正在处理，请稍候再切换。');return false;}if(rulesDirty&&!confirm('当前内容有未保存的修改。放弃修改并离开？'))return false;rulesDirty=false;return true;},
    draw(){},
    load(){state.page=1;state.selected.clear();return render().catch(e=>{$('#content').innerHTML=`<div class="empty error">${esc(e.message)}<br><button data-action="retry">重新加载</button></div>`;});},
  };
  }};
})();
