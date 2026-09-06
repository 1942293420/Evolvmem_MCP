/* Shared context browsing; Signal adds explicit memory organization actions. */
(() => {
  'use strict';
  const $ = (q, root = document) => root.querySelector(q);
  const $$ = (q, root = document) => [...root.querySelectorAll(q)];
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
  const text = s => `<p>${esc(s || '尚未记录')}</p>`;
  const labels = {active:'有效',candidate:'待验证',archived:'已归档',superseded:'已被替代',
    pinned:'固定注入',normal:'常用记忆',reference:'参考资料',fact:'事实',decision:'决定',preference:'偏好',constraint:'约束',experience:'经验',playbook:'归纳方法',
    single_verified:'单次验证',repeated_verified:'重复验证',unverified:'尚未验证',contradicted:'存在反证',
    technical:'技术验证',business:'业务结果',user_confirmed:'用户确认',success:'成功',confirmed:'确认有效',failure:'适用条件内失败',inapplicable:'本场景不适用',unknown:'结果未知',used:'已采用，结果待验证',
    ready:'已生成摘要',failed:'最近更新失败',pending:'等待生成',vector_dirty:'摘要已生成，索引待同步',open:'进行中',paused:'已暂停',blocked:'有阻塞',completed:'已完成',cancelled:'已取消'};
  const label = s => labels[s] || s || '未记录';
  const state = {page:'home',projects:[],stats:null,insights:null,boot:0,dialog:0};
  const browsers = new Map();
  const memoryCache = new Map();
  const name = slug => state.projects.find(p=>p.project===slug)?.display_name || slug || '未归属项目';
  const date = value => {
    if (!value) return '尚未记录';
    const d = new Date(/[Z+]/.test(value) ? value : value.replace(' ','T')+'Z');
    return Number.isNaN(d.valueOf()) ? value : d.toLocaleString('zh-CN',{hour12:false});
  };
  const shortDate = value => date(value).split(' ')[0];
  const badge = (value, good=false) => `<span class="ui-badge ${good?'good':'neutral'}">${esc(value)}</span>`;
  const options = (items, selected) => items.map(([v,t])=>`<option value="${esc(v)}" ${v===selected?'selected':''}>${esc(t)}</option>`).join('');
  const projectOptions = selected => options([['','全部项目'],...state.projects.map(p=>[p.project,name(p.project)])],selected);
  const section = (title, content) => `<section class="ui-detail-section"><h3>${title}</h3>${content}</section>`;
  const list = (items, ordered=false) => items?.length ? `<${ordered?'ol':'ul'}>${items.map(v=>`<li>${esc(v)}</li>`).join('')}</${ordered?'ol':'ul'}>` : '<p class="ui-note">尚未记录</p>';
  const conditions = values => values && Object.keys(values).length ? `<dl class="ui-conditions">${Object.entries(values).map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : '<p class="ui-note">未记录明确条件，采用前仍需核对当前场景。</p>';
  function errorBox(message='暂时无法读取数据。') { return `<div class="ui-empty"><strong>${esc(message)}</strong><span>可以刷新重试，已保存的内容不会因此改变。</span><button class="ui-button" data-refresh>重新读取</button></div>`; }
  async function get(path) {
    const response=await fetch(path,{cache:'no-store'});
    if(!response.ok) throw new Error(response.status===404?'这条记录已不存在，请刷新列表。':'暂时无法读取数据，请稍后重试。');
    return response.json();
  }
  function valid(row) { return row.status==='active' && !row.expired && ['single_verified','repeated_verified'].includes(row.validation_level); }
  function caseBadge(row) { return badge(row.expired?'已过期':valid(row)?label(row.validation_level):label(row.status),valid(row)); }
  function caseCard(row) {
    return `<button class="ui-card" data-detail="experience" data-id="${row.id}"><span class="ui-card-head">${caseBadge(row)}<span>↗</span></span><h3 class="ui-card-title">${esc(row.problem)}</h3><p class="ui-card-copy">${esc(Object.entries(row.conditions||{}).map(([k,v])=>`${k}：${v}`).join(' · ')||'查看案例步骤与验证依据')}</p><span class="ui-card-foot"><span>${esc(name(row.project))}</span><span>${row.success_count} 次独立成功</span></span></button>`;
  }
  function recordLink(kind,row,primary,secondary='') {
    return `<button class="data-record" data-detail="${kind}" data-id="${esc(row.id??'')}" ${row.id==null?'disabled':''}><span class="data-primary">${esc(primary||'尚无内容')}</span>${secondary?`<span class="data-secondary">${esc(secondary)}</span>`:''}</button>`;
  }
  function recordStatus(row,kind) {
    const contradicted=kind==='experience'&&row.validation_level==='contradicted';
    const value=kind==='experience'?(row.expired?'已过期':contradicted?'存在反证':valid(row)?label(row.validation_level):label(row.status)):kind==='memory'&&row.status==='active'?'活跃':label(row.status);
    const attention=row.expired||contradicted||['failed','blocked','candidate'].includes(row.status);
    return `<span class="data-status ${attention?'attention':''}">${esc(value)}</span>`;
  }
  function recordsTable(rows,kind) {
    const col=(title,style='')=>`<th scope="col" class="${style}">${title}</th>`;
    const cell=(value,style='')=>`<td class="${style}">${value}</td>`;
    const project=row=>cell(esc(name(row.project)),'data-col-project');
    const status=row=>cell(recordStatus(row,kind),'data-col-status');
    const updated=value=>cell(`<time title="${esc(date(value))}">${esc(value?shortDate(value):'—')}</time>`,'data-col-date data-col-extra');
    let headings,body;
    if(kind==='memory') {
      headings=col('记忆内容 / 标识')+col('项目','data-col-project')+col('状态','data-col-status')+col('类型','data-col-type data-col-extra')+col('更新日期','data-col-date data-col-extra')+col('命中','data-col-number data-col-extra');
      body=rows.map(row=>`<tr>${cell(recordLink(kind,row,row.value,row.key))}${project(row)}${status(row)}${cell(esc(label(row.attribute)),'data-col-type data-col-extra')}${updated(row.updated_at)}${cell(esc(row.access_count),'data-col-number data-col-extra')}</tr>`).join('');
    } else if(kind==='experience') {
      headings=col('问题 / 适用条件')+col('项目','data-col-project')+col('验证状态','data-col-status')+col('成功','data-col-number data-col-extra')+col('失败','data-col-number data-col-extra')+col('更新日期','data-col-date data-col-extra');
      body=rows.map(row=>`<tr>${cell(recordLink(kind,row,row.problem,Object.entries(row.conditions||{}).map(([k,v])=>`${k}：${v}`).join(' · ')))}${project(row)}${status(row)}${cell(esc(row.success_count),'data-col-number data-col-extra')}${cell(esc(row.failure_count),'data-col-number data-col-extra')}${updated(row.updated_at)}</tr>`).join('');
    } else if(kind==='summary') {
      headings=col('项目摘要')+col('项目','data-col-project')+col('摘要状态','data-col-status')+col('内容更新','data-col-date data-col-extra')+col('来源截至','data-col-date data-col-extra');
      body=rows.map(row=>`<tr>${cell(recordLink(kind,row,row.summary||'暂无已生成内容',row.id==null?'尚无可展开的摘要':''))}${project(row)}${status(row)}${updated(row.content_updated_at)}${updated(row.covered_through)}</tr>`).join('');
    } else {
      headings=col('任务概况')+col('项目','data-col-project')+col('任务状态','data-col-status')+col('断点版本','data-col-number data-col-extra')+col('更新日期','data-col-date data-col-extra');
      body=rows.map(row=>`<tr>${cell(recordLink(kind,row,row.summary,row.is_focus?'当前续接任务':''))}${project(row)}${status(row)}${cell(esc(row.checkpoint_revision),'data-col-number data-col-extra')}${updated(row.updated_at)}</tr>`).join('');
    }
    const title={memory:'记忆记录',experience:'经验案例',summary:'项目摘要',workstream:'任务断点'}[kind];
    return `<div class="data-table-wrap"><table class="data-table"><caption class="data-sr-only">${title}，点击内容展开详情</caption><thead><tr>${headings}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  function progressCard(row, kind) {
    return `<button class="ui-card" data-detail="${kind}" data-id="${esc(row.id ?? '')}" ${row.id==null?'disabled':''}><span class="ui-card-head">${badge(label(row.status),['ready','completed'].includes(row.status))}<span>${esc(shortDate(row.updated_at))}</span></span><h3 class="ui-card-title">${esc(kind==='summary'?name(row.project):row.summary)}</h3><p class="ui-card-copy">${esc(kind==='summary'?row.summary:name(row.project))}</p><span class="ui-card-foot"><span>${kind==='summary'?'项目的最近记录':'保存的任务断点'}</span><span>查看详情 ↗</span></span></button>`;
  }
  function navigate(page, project, query) {
    if(!['home','memories','experiences','progress'].includes(page)) return;
    const current=[...browsers.values()].find(c=>c.kind===state.page);
    if(current?.canLeave&&!current.canLeave()) {
      if(location.hash!==`#${state.page}`)history.replaceState(null,'',`#${state.page}`);
      return;
    }
    state.page=page;
    document.body.dataset.activePage=page;
    $$('[data-page-panel]').forEach(el=>el.hidden=el.dataset.pagePanel!==page);
    $$('[data-page-link]').forEach(el=>{const current=el.dataset.pageLink===page;el.classList.toggle('active',current);el.setAttribute('aria-current',current?'page':'false');});
    history.replaceState(null,'',`#${page}`);
    const mount=$(`[data-slot="${{memories:'memory-browser',experiences:'experience-browser',progress:'progress-browser'}[page]}"]`);
    if(mount) {
      let controller=browsers.get(mount);
      if(!controller) {
        if(page==='memories'&&document.body.dataset.design==='signal'&&window.EvolvOrganizer) {
          controller=window.EvolvOrganizer.create(mount,{
            get,esc,label,projectName:name,projects:()=>state.projects,
            onRows:rows=>rows.forEach(row=>memoryCache.set(String(row.id),row)),
            openMemory:id=>openDetail('memory',id),onChanged:()=>boot(false),
          });
          controller.integrated=true;
        } else controller=makeBrowser(mount,page);
        browsers.set(mount,controller);
      }
      if(project!==undefined) {
        controller.project=project;
        if(page==='memories') {controller.query='';controller.status='active';}
      }
      if(query!==undefined) controller.query=query;
      controller.page=1;controller.draw();controller.load();
    }
    window.scrollTo({top:0,behavior:'instant'});
    if(page==='home') requestAnimationFrame(()=>window.dispatchEvent(new Event('resize')));
  }
  function countUp(el,value) {
    if(typeof value!=='number') {el.textContent='—';return;}
    el.textContent=value.toLocaleString('en-US');
    if(matchMedia('(prefers-reduced-motion:reduce)').matches) return;
    const start=performance.now();
    const tick=now=>{if(!el.isConnected)return;const t=Math.min(1,(now-start)/650);el.textContent=Math.round(value*(1-(1-t)**3)).toLocaleString('en-US');if(t<1)requestAnimationFrame(tick);};
    requestAnimationFrame(tick);
  }
  async function boot(refreshBrowser=true) {
    const token=++state.boot;
    $$('[data-data-status]').forEach(el=>el.textContent='正在连接记忆库…');
    const urls=['/api/stats','/api/insights','/api/projects','/api/memories?status=active&page_size=200&sort=access_count','/api/experiences?status=verified&page_size=3','/api/workstreams?status=unfinished&page_size=3','/api/memories?status=active&sort=created_at&order=desc&page_size=3'];
    const responses=await Promise.allSettled(urls.map(get));
    if(token!==state.boot) return;
    const [stats,insights,projects,memories,experiences,tasks,recent]=responses.map(r=>r.status==='fulfilled'?r.value:null);
    state.stats=stats;state.insights=insights;state.projects=projects?.projects || insights?.projects || [];
    memories?.rows.forEach(row=>memoryCache.set(String(row.id),row));
    recent?.rows.forEach(row=>memoryCache.set(String(row.id),row));
    const numbers={...stats,...insights,project_count:projects?projects.projects.filter(p=>p.status==='active').length:undefined};
    $$('[data-stat]').forEach(el=>countUp(el,numbers[el.dataset.stat]));
    $$('[data-slot=metrics]').forEach(el=>{
      el.innerHTML=[['total_active','活跃记忆'],['verified_experiences','已验证经验'],['ready_summaries','项目摘要'],['unfinished_workstreams','未完成任务']].map(([k,t])=>`<div class="ui-metric"><span class="metric-value" data-count="${k}">—</span><span class="metric-label">${t}</span></div>`).join('');
      $$('[data-count]',el).forEach(n=>countUp(n,numbers[n.dataset.count]));
    });
    $$('[data-slot=featured-experiences]').forEach(el=>{el.innerHTML=experiences?(experiences.rows.length?experiences.rows.map(caseCard).join(''):'<div class="ui-empty">验证过的经验会在这里积累。</div>'):errorBox();});
    $$('[data-slot=project-list]').forEach(el=>{el.innerHTML=projects?projects.projects.filter(p=>p.status==='active'&&p.active_items>0).sort((a,b)=>b.active_items-a.active_items).slice(0,5).map((p,i)=>`<button class="ui-row" data-project="${esc(p.project)}"><span><span class="ui-row-title">${esc(p.display_name||p.project)}</span><span class="ui-row-meta">${esc(p.project)}</span></span><span class="ui-row-meta">${p.active_items} 条 ↗</span></button>`).join(''):errorBox();if(projects&&!el.innerHTML)el.innerHTML='<div class="ui-empty">暂无已归属项目的记忆。</div>';});
    $$('[data-slot=recent-memories]').forEach(el=>{el.innerHTML=recent?recent.rows.map(row=>`<button class="ui-row" data-detail="memory" data-id="${row.id}"><span><span class="ui-row-title">${esc(row.value.slice(0,90))}</span><span class="ui-row-meta">${esc(name(row.project))} · ${esc(shortDate(row.created_at))}</span></span><span class="ui-row-arrow">↗</span></button>`).join(''):errorBox();if(recent&&!el.innerHTML)el.innerHTML='<div class="ui-empty">暂无活跃记忆。</div>';});
    $$('[data-slot=task-list]').forEach(el=>{el.innerHTML=tasks?(tasks.rows.length?tasks.rows.map(r=>progressCard(r,'workstream')).join(''):'<div class="ui-empty"><strong>当前没有未完成的任务</strong><span>新的任务断点会在与助手协作时保存。</span><button class="ui-button" data-page-link="progress">查看项目进展 ↗</button></div>'):errorBox();});
    $$('[data-galaxy]').forEach(el=>{
      $('.galaxy-error',el)?.remove();
      if(projects&&memories) window.EvolvGalaxy.mount(el,{projects:projects.projects,memories:memories.rows,total:stats?.total_active});
      else {const error=document.createElement('div');error.className='galaxy-error';error.innerHTML=errorBox('星图数据暂不可用。');el.append(error);}
    });
    const failures=responses.filter(r=>r.status==='rejected').length;
    $$('[data-data-status]').forEach(el=>{el.textContent=failures?'部分数据未能加载 · 可刷新重试':'实时数据已连接';el.classList.toggle('ui-status-error',!!failures);});
    if(refreshBrowser&&state.page!=='home') {const controller=[...browsers.values()].find(c=>c.kind===state.page);if(controller){controller.draw();controller.load();}}
  }

  // Browser controllers and the exact-record dialog are defined below.
  const dialog=document.createElement('dialog');dialog.className='ui-dialog data-dialog';dialog.setAttribute('aria-label','记忆详情');document.body.append(dialog);
  dialog.addEventListener('click',e=>{if(e.target===dialog){++state.dialog;dialog.close();}});
  dialog.addEventListener('close',()=>++state.dialog);

  function makeBrowser(mount,kind) {
    const c={kind,page:1,query:'',project:'',status:kind==='experiences'?'verified':'all',mode:'summary',request:0,timer:null};
    const statuses=()=>kind==='memories'?[['active','活跃记忆'],['archived','已归档'],['all','全部状态']]
      :kind==='experiences'?[['verified','已验证'],['candidate','待验证'],['inactive','已归档 / 已失效'],['all','全部经验']]
      :c.mode==='summary'?[['all','全部状态'],['ready','已生成'],['failed','更新失败'],['pending','等待生成'],['vector_dirty','索引待同步']]
      :[['all','全部任务'],['unfinished','未完成'],['open','进行中'],['blocked','有阻塞'],['paused','已暂停'],['completed','已完成'],['cancelled','已取消']];
    if(kind==='memories')c.status='active';
    c.draw=()=>{
      mount.innerHTML=`${kind==='progress'?`<nav class="ui-subnav" aria-label="进展类型"><button data-progress-mode="summary" aria-pressed="${c.mode==='summary'}">项目摘要</button><button data-progress-mode="workstream" aria-pressed="${c.mode==='workstream'}">任务断点</button></nav>`:''}
      <div class="ui-toolbar"><label class="ui-filter-label">搜索<input class="ui-search" data-filter="query" type="search" maxlength="500" placeholder="${kind==='experiences'?'搜索问题、步骤或条件':'搜索记录内容'}" value="${esc(c.query)}"></label><label class="ui-filter-label">项目<select class="ui-select" data-filter="project">${projectOptions(c.project)}</select></label><label class="ui-filter-label">状态<select class="ui-select" data-filter="status">${options(statuses(),c.status)}</select></label></div>
      <div class="ui-result-info"><span data-results-info role="status">正在读取…</span><span>${kind==='experiences'?'成功 / 失败为独立结果记录数':'点击内容查看详情'}</span></div><div data-results aria-busy="true"></div><div class="ui-pager" data-pager></div>`;
      $$('[data-filter]',mount).forEach(el=>{
        const run=()=>{c[el.dataset.filter]=el.value;c.page=1;c.load();};
        if(el.dataset.filter==='query')el.oninput=()=>{++c.request;clearTimeout(c.timer);c.timer=setTimeout(run,220);};else el.onchange=run;
      });
      $$('[data-progress-mode]',mount).forEach(el=>el.onclick=()=>{clearTimeout(c.timer);c.mode=el.dataset.progressMode;c.status='all';c.page=1;c.draw();c.load();});
    };
    c.load=async()=>{
      const token=++c.request;clearTimeout(c.timer);
      const area=$('[data-results]',mount);area.setAttribute('aria-busy','true');
      area.innerHTML='<div class="ui-empty">正在读取记录…</div>';$('[data-pager]',mount).innerHTML='';
      const endpoint=kind==='memories'?'memories':kind==='experiences'?'experiences':c.mode==='summary'?'project-summaries':'workstreams';
      const params=new URLSearchParams({q:c.query,status:c.status,page:c.page,page_size:18,[kind==='memories'?'attribution':'project']:c.project});
      if(kind==='memories'){params.set('sort','created_at');params.set('order','desc');}
      try {
        const data=await get(`/api/${endpoint}?${params}`);if(token!==c.request)return;
        if(kind==='memories')data.rows.forEach(row=>memoryCache.set(String(row.id),row));
        area.className='data-results';
        area.innerHTML=data.rows.length?recordsTable(data.rows,kind==='memories'?'memory':kind==='experiences'?'experience':c.mode):'<div class="ui-empty"><strong>没有符合筛选条件的记录</strong><span>换个关键词，或选择其他项目和状态。</span></div>';
        $('[data-results-info]',mount).textContent=`${data.total} 条记录`;
        $('[data-pager]',mount).innerHTML=`<button class="ui-button" data-prev ${c.page<=1?'disabled':''}>← 上一页</button><span>${c.page} / ${Math.max(1,Math.ceil(data.total/18))}</span><button class="ui-button" data-next ${c.page*18>=data.total?'disabled':''}>下一页 →</button>`;
        $('[data-prev]',mount).onclick=()=>{--c.page;c.load();};$('[data-next]',mount).onclick=()=>{++c.page;c.load();};
      } catch(error) {if(token!==c.request)return;area.className='';area.innerHTML=errorBox(error.message);$('[data-results-info]',mount).textContent='暂未加载';}
      finally{if(token===c.request)area.setAttribute('aria-busy','false');}
    };
    return c;
  }

  function sourceRows(rows) {
    return rows?.length?rows.map(row=>`<div class="ui-proof"><span class="ui-note">来源 #${row.id}</span>${row.summary?text(row.summary):''}<div class="ui-source">${esc(row.source_ref)}</div></div>`).join(''):'<p class="ui-note">没有保存来源引用。</p>';
  }
  function experienceDetail(row) {
    const sourceMap=Object.fromEntries(row.sources.map(s=>[s.id,s]));
    return `<div class="ui-detail-meta">${caseBadge(row)}${badge(row.transferable?'可跨项目参考':'限原项目')}<span>经验 #${row.id} · ${esc(name(row.project))}</span></div><h2>${esc(row.problem)}</h2>
    <div class="ui-notice">${valid(row)?'这条经验有真实结果支持，复用前仍需核对当前条件。':'这条经验当前不作为成功方案推荐，保留历史内容供验证和复核。'}<br>${row.success_count} 次独立成功 · ${row.failure_count} 次适用条件内失败</div>
    ${section('当时的条件',conditions(row.conditions))}${section('采用的步骤',list(row.steps,true))}${section('为何有效',text(row.rationale))}${section('记录的结果',text(row.result))}${section('适用范围',list(row.applicability))}${section('不适用的情况',list(row.exclusions))}
    ${section('验证依据',row.verification.length?row.verification.map(p=>`<div class="ui-proof">${badge(label(p.level),true)}${conditions(p.conditions)}<div class="ui-source">${esc(p.source?.ref||'引用未保留')}</div></div>`).join(''):'<p class="ui-note">尚无有效成功证据，结果描述本身不等于验证通过。</p>')}
    ${section('反馈记录',row.evidence.length?[...row.evidence].reverse().map(e=>`<div class="ui-proof">${badge(label(e.outcome))}<span class="ui-note"> ${esc(date(e.observed_at))}</span>${text(e.note)}${conditions(e.conditions)}<div class="ui-source">${esc(sourceMap[e.source_id]?.source_ref||'')}</div></div>`).join(''):'<p class="ui-note">暂无反馈。</p>')}
    ${section('场景派生',`${row.parent_case?`<p class="ui-note">来自原案例</p><button class="ui-row" data-detail="experience" data-id="${row.parent_case.id}"><span class="ui-row-title">${esc(row.parent_case.problem)}</span><span>↗</span></button>`:'<p class="ui-note">独立案例</p>'}${row.derived_cases.map(r=>`<button class="ui-row" data-detail="experience" data-id="${r.id}"><span class="ui-row-title">${esc(r.problem)}</span><span>↗</span></button>`).join('')}`)}
    <details class="ui-disclosure"><summary>全部来源 · ${row.sources.length} 条</summary>${sourceRows(row.sources)}</details>`;
  }
  function summaryDetail(row) {
    return `<div class="ui-detail-meta">${badge(label(row.status),row.status==='ready')}<span>内容更新于 ${esc(date(row.content_updated_at))}</span></div><h2>${esc(name(row.project))}</h2><p class="ui-note">来源截至 ${esc(date(row.covered_through))}</p>${row.status==='failed'?'<div class="ui-notice">最近一次更新失败，下面保留上一次成功保存的内容。</div>':''}${section('项目摘要',text(row.l1))}<details class="ui-disclosure"><summary>展开详细记录</summary>${section('详细内容',text(row.l2))}</details><details class="ui-disclosure"><summary>查看来源 · ${row.sources.length} 条</summary>${sourceRows(row.sources)}</details>`;
  }
  function workstreamDetail(row) {
    const finished=['completed','cancelled'].includes(row.status);
    return `<div class="ui-detail-meta">${badge(label(row.status),row.status==='completed')}<span>${esc(name(row.project))} · ${esc(date(row.updated_at))}</span></div><h2>${esc(row.objective||'任务断点')}</h2><div class="ui-notice">${finished?'这项任务已经结束，无需重新执行已完成事项。':'这是最近保存的断点，继续前会核对实际进度与工作区。'}</div>${section('已经完成',list(row.completed_steps))}${section('当前步骤',text(row.current_step))}${section('下一步',text(row.next_action))}${section('阻塞事项',row.blockers?.length?list(row.blockers):'<p class="ui-note">没有记录阻塞。</p>')}${section('已接受的决定',list(row.accepted_decisions))}${!finished&&row.content_available?'<section class="ui-detail-section"><button class="ui-button" data-copy-continuation>复制续接提示</button><span class="ui-note" data-copy-status role="status"></span><textarea class="ui-copy-fallback" aria-label="续接提示文本" readonly hidden></textarea></section>':''}<details class="ui-disclosure"><summary>断点编号与来源</summary><div class="ui-source">${esc(row.id)} · 第 ${row.checkpoint_revision} 版</div>${sourceRows(row.sources)}</details>`;
  }
  function memoryDetail(row) {
    const info={'标识':row.key,'项目':name(row.project),'状态':row.status==='active'?'活跃':label(row.status),'类型':label(row.attribute),'层级':label(row.tier),'检索命中':`${row.access_count} 次`,'重要性':row.importance,'创建时间':date(row.created_at),'更新时间':date(row.updated_at)};
    return `<div class="ui-detail-meta"><span>记忆 #${row.id}</span><span>${esc(name(row.project))}</span></div><h2>记忆详情</h2>${section('完整内容',text(row.value))}${section('记忆信息',conditions(info))}${section('标签',text(row.tags))}`;
  }
  async function openDetail(kind,id) {
    const token=++state.dialog;
    dialog.innerHTML='<div class="ui-dialog-shell"><button class="ui-dialog-close" aria-label="关闭详情">✕</button><div class="ui-detail-body" aria-live="polite">正在读取…</div></div>';
    $('.ui-dialog-close',dialog).onclick=()=>dialog.close();if(!dialog.open)dialog.showModal();
    try {
      const endpoint={experience:'experiences',summary:'project-summaries',workstream:'workstreams'}[kind];
      let row;
      if(kind==='memory') {
        // The current list response already carries this exact record's full
        // value; expanding it does not count as another retrieval or use.
        row=memoryCache.get(String(id));
        if(!row) throw new Error('记忆正文暂不可用，请刷新列表。');
      } else if(endpoint) row=await get(`/api/${endpoint}/${encodeURIComponent(id)}`);
      else throw new Error('未知记录类型。');
      if(token!==state.dialog||!dialog.open)return;
      $('.ui-detail-body',dialog).innerHTML=kind==='experience'?experienceDetail(row):kind==='summary'?summaryDetail(row):kind==='workstream'?workstreamDetail(row):memoryDetail(row);
      const copy=$('[data-copy-continuation]',dialog);
      if(copy)copy.onclick=async()=>{
        const prompt=`继续 ${row.project} 项目的任务“${row.objective}”。请先恢复任务断点 ${row.id}，核对当前工作区及完成情况，再完成剩余工作。记录的下一步：${row.next_action||'请查看断点'}。`;
        try{await navigator.clipboard.writeText(prompt);if(token===state.dialog)$('[data-copy-status]',dialog).textContent=' 已复制';}
        catch{if(token!==state.dialog)return;const field=$('textarea',dialog);field.hidden=false;field.value=prompt;field.focus();field.select();$('[data-copy-status]',dialog).textContent=' 请选择下方文字复制';}
      };
    }catch(error){if(token===state.dialog&&dialog.open){$('.ui-detail-body',dialog).innerHTML=`${text(error.message)}<button class="ui-button" data-retry-detail>重试</button>`;$('[data-retry-detail]',dialog).onclick=()=>openDetail(kind,id);}}
  }
  document.addEventListener('click',event=>{
    const nav=event.target.closest('[data-page-link]');if(nav){event.preventDefault();navigate(nav.dataset.pageLink);return;}
    const detail=event.target.closest('[data-detail]');if(detail&&!detail.disabled){openDetail(detail.dataset.detail,detail.dataset.id);return;}
    const project=event.target.closest('[data-project]');if(project){navigate('memories',project.dataset.project);return;}
    const current=[...browsers.values()].find(c=>c.kind===state.page);
    if(event.target.closest('[data-refresh]')) {
      if(!current?.canLeave||current.canLeave())boot();
      return;
    }
    const link=event.target.closest('a[href]');
    if(link&&current?.canLeave&&!current.canLeave())event.preventDefault();
  });
  document.addEventListener('evolvmem:project',event=>navigate('memories',event.detail));
  $$('[data-ui-search]').forEach(el=>el.addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();navigate('memories','',el.value);}}));
  window.addEventListener('hashchange',()=>{const page=location.hash.slice(1);if(page!==state.page)navigate(page);});
  const startPage=location.hash.slice(1);if(['memories','experiences','progress'].includes(startPage))navigate(startPage);else navigate('home');
  boot(![...browsers.values()].some(c=>c.integrated));
})();
