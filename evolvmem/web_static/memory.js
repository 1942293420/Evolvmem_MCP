/* Project history and concise Q&A share one workbench and source links. */
window.EvolvMemory = {async render({mount, doc, projects, api, esc, toast, lane='overview', onLane, onBusy, onChanged, onSource, onDirty=()=>{}, standalone=false}) {
  const labels={habit:'长期习惯',project_convention:'项目约定',task_requirement:'任务要求',environment:'环境配置',decision:'决策依据',experience:'技术经验',reference:'参考知识'};
  const state={project:doc.project||'__global__',lane,category:'',status:'',q:'',page:1,editing:null,busy:false,dirty:false};
  const $=s=>mount.querySelector(s);
  const params=()=>new URLSearchParams({project:state.project,category:state.category,state:state.status,q:state.q,page:state.page});
  const options=(entries,value)=>Object.entries(entries).map(([k,v])=>`<option value="${esc(k)}" ${k===value?'selected':''}>${esc(v)}</option>`).join('');
  mount.innerHTML=`<header class="memory-heading"><div><a class="hint" href="#knowledge/projects" data-view="projects">← 所有项目</a><h2>${esc(doc.name)}</h2><p class="hint">历史回答“以前说过什么”，经验问答回答“类似事情怎么做”。</p></div></header>
    <nav class="memory-lanes" aria-label="项目记忆类型"><button data-memory-lane="overview">项目总览</button>${doc.project?'<button data-memory-lane="history">历史记录</button>':''}<button data-memory-lane="qa">经验问答</button></nav><div class="memory-pane"></div>
    <dialog class="memory-dialog" aria-label="记忆详情"><div class="memory-dialog-content"></div></dialog>`;
  if(standalone){$('.memory-heading').hidden=true;$('.memory-lanes').hidden=true;}
  const modal=(title,body,footer='')=>{$('.memory-dialog-content').innerHTML=`<div class="dialog-header"><h2>${title}</h2><button data-memory-action="close" aria-label="关闭记忆详情">✕</button></div><div class="dialog-body">${body}</div><div class="dialog-footer">${footer||'<button data-memory-action="close">关闭</button>'}</div>`;if(!$('.memory-dialog').open)$('.memory-dialog').showModal();};
  function overview(){
    $('.memory-pane').innerHTML=`<section class="panel project-document"><h3>项目摘要</h3><p class="hint">按已入库的会话摘要和任务记录持续更新；经验问答单独分类。这里的摘录是历史参考。</p><pre class="project-memory-text">${esc(doc.summary||'尚无已入库的历史摘要。可以先查看同步的历史记录，或整理经验问答。')}</pre>${doc.project_summary?`<details class="project-fulltext"><summary>AI 项目摘要 · ${esc(({ready:"已生成",failed:"更新失败，保留上次结果",pending:"等待生成",vector_dirty:"待同步"})[doc.project_summary.status]||doc.project_summary.status)}</summary><p class="hint">历史汇总，来源截至 ${esc(doc.project_summary.covered_through||"尚未记录")}；最新要求以当前资料为准。</p><pre class="project-memory-text">${esc(doc.project_summary.l1||"尚未生成")}</pre><details><summary>详细汇总及来源</summary><pre class="project-memory-text">${esc(doc.project_summary.l2)}</pre>${doc.project_summary.sources.map(s=>`<p class="hint">${esc(s.summary||s.source_ref)}</p>`).join("")}</details></details>`:""}<details class="project-fulltext"><summary>总记忆正文 · ${doc.source_ids.length} 份来源</summary><pre class="project-memory-text">${esc(doc.body||'暂无历史正文摘要')}</pre></details><div class="memory-shortcuts">${doc.project?`<button data-memory-lane="history">历史记录 · ${doc.sessions.length} 次</button>`:''}<button data-memory-lane="qa">可用经验问答 · ${doc.qa_count}</button></div></section>`;
  }
  function history(){
    const pending=doc.sessions.filter(a=>a.storage!=='database'&&a.state==='available').length;
    $('.memory-pane').innerHTML=`<section class="panel"><div class="section-head"><div><h3>历史记录 · 第 1–3 步</h3><p class="hint">正文已去除工具记录、系统注入和内部推理；会话摘要关联对应正文。新对话自动清洗入库。</p></div>${pending?`<button class="write" data-memory-action="migrate">清洗旧对话入库 · ${pending} 次</button>`:''}</div><div class="project-conversations">${doc.sessions.map(a=>`<article class="conversation-row"><div><strong>${esc(a.adapter)} · ${esc(a.created_at)}</strong><span class="badge">${esc(a.stage)}</span> <span class="badge">${a.storage==='database'?'清洗正文已入库':a.state==='available'?'旧对话待迁移':'来源已过期'}</span></div><p>${esc(a.summary||'该次对话尚无已入库摘要。')}</p><button data-memory-action="conversation" data-id="${a.id}" data-project="${esc(doc.project)}">查看整理后的对话</button></article>`).join('')||'<div class="empty">尚无关联的历史对话。</div>'}</div></section>`;
  }
  async function qa(){
    const r=await api('qa?'+params());
    $('.memory-pane').innerHTML=`<section class="panel"><div class="section-head"><div><h3>经验问答 · 第 3 步成果</h3><p class="hint">一个问题、一条短答案；分类与适用条件分别保存。第 4 步的协作学习参考这些有效知识。</p></div><div class="actions"><a href="#experiences">经验案例与验证记录 →</a><button class="primary write" data-memory-action="new">＋ 新增问答</button></div></div>
      <div class="toolbar qa-filters"><input id="qa-search" type="search" aria-label="搜索问答" placeholder="搜索问题、答案或来源" value="${esc(state.q)}">${standalone?`<select id="qa-project-filter" aria-label="问答项目">${options({'__all__':'所有项目','__global__':'全局知识',...Object.fromEntries(projects.filter(p=>p.status==='active').map(p=>[p.project,p.display_name||p.project]))},state.project)}</select>`:''}<select id="qa-category-filter" aria-label="问答分类">${options({'':'全部分类',...labels},state.category)}</select><select id="qa-state-filter" aria-label="问答状态">${options({'':'全部状态',active:'可用问答',pending:'待确认 / 待整理',unformatted:'旧资料待整理'},state.status)}</select><button data-memory-action="search">搜索</button></div>
      <p class="hint">${r.total} 组 · ${r.record_count} 份来源资料；完全相同的问答或旧资料已收拢。</p><div class="qa-cards">${r.items.map(row=>`<article class="qa-card"><div class="meta-line"><span class="badge">${esc(row.project||'全局')}</span><span class="badge">${esc(labels[row.category])}</span><span class="badge ${row.effective?'green':'amber'}">${row.effective?'可用问答':row.status==='unformatted'?'旧资料待整理':'待确认'}</span>${row.source_ids.length>1?`<span class="badge">同文 ${row.source_ids.length} 份</span>`:''}</div><h4>${esc(row.question?'问：'+row.question:row.source_title)}</h4><p>${esc(row.answer?'答：'+row.answer:row.source_body.slice(0,160))}</p>${row.trigger?`<p class="hint">适用：${esc(row.trigger)}</p>`:''}${!row.effective?`<p class="reason">${esc(row.reason)}</p>`:''}<button data-memory-action="edit" data-id="${row.id}">${row.question?'查看 / 编辑问答':row.managed_content?'提炼为独立问答':'整理成简洁问答'}</button>${row.source_ids.length>1?`<details><summary>查看重复来源</summary>${row.source_ids.map(id=>`<button data-memory-action="source" data-id="${id}">资料 #${id}</button>`).join('')}</details>`:''}</article>`).join('')||'<div class="empty">没有符合筛选条件的问答。旧资料可在“待确认 / 待整理”中整理。</div>'}</div>
      <div class="pagination"><span>第 ${r.page} / ${Math.max(1,Math.ceil(r.total/r.page_size))} 页</span><div><button data-memory-action="prev" ${r.page<=1?'disabled':''}>上一页</button><button data-memory-action="next" ${r.page*r.page_size>=r.total?'disabled':''}>下一页</button></div></div></section>`;
  }
  async function draw(){
    mount.querySelectorAll('.memory-lanes button').forEach(b=>{b.classList.toggle('active',b.dataset.memoryLane===state.lane);b.setAttribute('aria-pressed',String(b.dataset.memoryLane===state.lane));});
    if(state.lane==='history'&&doc.project)history();else if(state.lane==='qa')await qa();else overview();
  }
  const dirty=value=>{state.dirty=value;onDirty(value);};
  const discard=()=>{if(state.dirty&&!confirm('问答有未保存的修改，放弃修改？'))return false;dirty(false);return true;};
  $('.memory-dialog').addEventListener('cancel',e=>{if(state.busy||!discard())e.preventDefault();});
  mount.oninput=e=>{if(e.target.closest('.memory-dialog'))dirty(true);};
  async function edit(id){
    dirty(false);
    const r=id?await api('qa/'+id):null;state.editing=r;
    const project=r?.project??(state.project.startsWith('__')?'':state.project);
    modal(r?'整理 / 编辑经验问答':'新增经验问答',`<p class="hint">${r?.managed_content?'将新建独立问答，原方法案例及验证记录保留。':''}保留关键适用条件。确认入库表示这条知识可供参考，不等于方法已经验证成功。</p>
      <div class="field"><label for="qa-question">问题 · 最多 160 字</label><input id="qa-question" maxlength="160" value="${esc(r?.question||'')}" placeholder="遇到什么情况，需要知道什么？"></div>
      <div class="field"><label for="qa-answer">简洁答案 · 最多 400 字</label><textarea id="qa-answer" maxlength="400" placeholder="一句话说明做法，保留必要条件">${esc(r?.answer||(r?.source_body.length<=400?r.source_body:''))}</textarea></div>
      <div class="form-grid"><div class="field"><label for="qa-project">适用项目 <button type="button" class="write quiet" data-memory-action="project-new">＋ 新增项目</button></label><select id="qa-project"><option value="" ${project===''?'selected':''}>全局 · 跨项目通用</option>${projects.filter(p=>p.status==='active').map(p=>`<option value="${esc(p.project)}" ${project===p.project?'selected':''}>${esc(p.display_name||p.project)}</option>`).join('')}</select></div><div class="field"><label for="qa-category">知识分类</label><select id="qa-category">${options(labels,r?.category||'reference')}</select></div></div>
      <div class="field"><label for="qa-trigger">适用条件</label><input id="qa-trigger" maxlength="500" value="${esc(r?.trigger||'')}" placeholder="例如：修改业务行为时；仅用于本次任务"></div>
      <label class="extraction-choice"><input type="checkbox" id="qa-replace">确认入库时，归档同项目、同分类、同条件下该问题的旧答案</label>
      ${r?EvolvExtraction.process(r.learning,esc):''}${r?`<details><summary>核对原资料与来源</summary><pre class="project-memory-text">${esc(r.source_body)}</pre><button data-memory-action="source" data-id="${r.id}">查看资料 #${r.id}</button>${r.sources.filter(s=>s.archive_id&&s.source_project).map(s=>`<button data-memory-action="conversation" data-id="${s.archive_id}" data-project="${esc(s.source_project)}">整理后的对话 #${s.archive_id}</button>`).join('')}</details>`:''}`,
      `${r?'<button class="write" data-memory-action="archive">归档资料</button>':''}<button data-memory-action="close">取消</button><button class="write" data-memory-action="draft">保存待确认</button><button class="primary write" data-memory-action="publish">确认入库</button>`);
  }
  async function act(action,b){
    if(action==='close'){if(discard())$('.memory-dialog').close();return;}
    if(action==='conversation'){if(!discard())return;const r=await api('conversations/'+b.dataset.id+'?'+new URLSearchParams({project:b.dataset.project}));modal('整理后的历史对话',`<p class="hint">${r.storage==='database'?'正文保存在数据库中。':'旧来源读取时清洗；可在历史记录页迁移入库。'}需求、回复、纠正与决定按顺序保留。</p><pre class="project-memory-text">${esc(r.text)}</pre>`);return;}
    if(action==='source'){if(!discard())return;$('.memory-dialog').close();await onSource(Number(b.dataset.id));return;}
    if(action==='project-new'){await EvolvProjects.create({api,esc,onSaved:async project=>{projects=(await api('projects')).projects;const select=$('#qa-project');select.add(new Option(projects.find(p=>p.project===project)?.display_name||project,project));select.value=project;dirty(true);}});return;}
    if(action==='new'){await edit();return;}
    if(action==='edit'){await edit(Number(b.dataset.id));return;}
    if(action==='search'){state.q=$('#qa-search').value;state.page=1;await qa();return;}
    if(action==='prev'||action==='next'){state.page+=action==='prev'?-1:1;await qa();return;}
    if(action==='migrate'){
      let after=0,total=0,missing=0,r;
      do{r=await api('history/migrate',{project:doc.project,after_id:after,limit:50});after=r.after_id;total+=r.migrated;missing+=r.unavailable.length;}while(r.has_more);
      toast(`已清洗入库 ${total} 次对话${missing?`；${missing} 份来源不可恢复`:''}。全程本机处理。`);await onChanged();return;
    }
    if(action==='publish'||action==='draft'){
      const r=state.editing;await api(r?'qa/'+r.id:'qa',{expected_revision:r?.revision,question:$('#qa-question').value,answer:$('#qa-answer').value,project:$('#qa-project').value,category:$('#qa-category').value,trigger:$('#qa-trigger').value,action:action==='publish'?'publish':'draft',replace_conflicts:$('#qa-replace').checked});
      dirty(false);$('.memory-dialog').close();toast(action==='publish'?'问答已确认入库，后续经验检索可使用。':'问答已保存到待确认。');await onChanged();return;
    }
    if(action==='archive'){if(!discard())return;const r=await api('items/'+state.editing.id);await api('items/'+r.id+'/transition',{expected_revision:r.revision,action:'archive'});$('.memory-dialog').close();toast('资料已归档，对应问答停止使用。');await onChanged();}
  }
  mount.onclick=async e=>{
    const b=e.target.closest('[data-memory-action],[data-memory-lane]');if(!b||state.busy)return;
    e.preventDefault();state.busy=true;b.disabled=true;onBusy(true);
    try{if(b.dataset.memoryLane){state.lane=b.dataset.memoryLane;onLane(state.lane);await draw();}else await act(b.dataset.memoryAction,b);}catch(error){toast(error.message);}finally{state.busy=false;b.disabled=false;onBusy(false);}
  };
  mount.onchange=async e=>{if(e.target.closest('.memory-dialog'))dirty(true);if(['qa-category-filter','qa-state-filter','qa-project-filter'].includes(e.target.id)){state[e.target.id==='qa-project-filter'?'project':e.target.id==='qa-category-filter'?'category':'status']=e.target.value;state.page=1;try{await qa();}catch(error){toast(error.message);}}};
  mount.onkeydown=e=>{if(e.key==='Enter'&&e.target.id==='qa-search'){e.preventDefault();act('search').catch(error=>toast(error.message));}};
  await draw();
}};
