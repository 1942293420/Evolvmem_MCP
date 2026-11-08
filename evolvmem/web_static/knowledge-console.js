/* Identity-bound console. No actor identity or source paths are supplied by UI. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ROOT = '/api/knowledge-console';
  const query = new URLSearchParams(location.search);
  const inspect = query.get('inspect') === '1';
  const state = {me:null,owner:null,users:[],pending:[],grants:[],ticket:0,edit:null,expPage:1};
  const label = {verified:'已验证',candidate:'候选',inactive:'已失效',all:'全部状态'};
  const experienceCategories=['前端开发','后端与接口','数据库与数据处理','画图与表达','业务规则与流程','自动化与平台集成','部署与运维','AI工具与协作'];
  const reviewLabel={all:'全部整理状态',accepted:'整理已验收',pending_session_review:'待会话复核',pending_root_acceptance:'待验收',needs_revision:'待修订'};
  const errors = {login_required:'登录已失效，请重新登录。',account_not_bound:'此飞书身份尚未绑定用户，请联系管理员在“用户与权限”中绑定。',unbound_identity:'此飞书身份尚未绑定用户，请联系管理员。',user_disabled:'此账号已停用。',account_disabled:'此账号已停用。',access_denied:'没有读取这类知识的权限。',permission_denied:'没有读取这类知识的权限。',forbidden:'没有操作权限。',admin_required:'此操作需要管理员权限。',revision_conflict:'资料已被其他操作更新，请刷新后重试。',invalid_csrf:'登录校验已过期，请刷新页面。',console_not_initialized:'用户管理尚未初始化，请联系管理员。',legacy_web_disabled:'旧功能已关闭。'};
  const inspectPath = path => inspect && path.startsWith(ROOT) ? `${path}${path.includes('?')?'&':'?'}inspect=1` : path;
  async function api(path, body, options={}) {
    const method = body === undefined ? 'GET' : 'POST';
    const url = method === 'GET' && !options.normal ? inspectPath(path) : path;
    const response = await fetch(url,{method,credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':state.me?.csrf_token||''},...(body===undefined?{}:{body:JSON.stringify(body)})});
    let data;try{data=await response.json();}catch(_){data={};}
    if(!response.ok){const error=new Error(errors[data.error] || (response.status===403?'没有操作权限。':response.status===401?'请登录后继续。':response.status===409?'资料已更新，请刷新后重试。':'读取未完成，请刷新后重试。'));error.code=data.error;error.status=response.status;throw error;}
    return data;
  }
  function notice(text,bad=false){const el=$('console-notice');el.hidden=!text;el.textContent=text;el.classList.toggle('error',bad);}
  function heading(title,desc,action=''){return `<header class="page-head"><div><p class="eyebrow">EVOLVMEM / 知识管理</p><h1>${esc(title)}</h1><p class="muted">${esc(desc)}</p></div>${action}</header>`;}
  function libraryURL(id,kind,inspection=inspect){return `/?user=${encodeURIComponent(id)}${inspection?'&inspect=1':''}#${kind}`;}
  function sourceState(lib,kind){return !lib[`can_read_${kind}`]?'未授权':!lib[`${kind==='history'?'history':'experience'}_connected`]?'数据尚未接入':'已接入';}
  function libraryBase(){return `${ROOT}/libraries/${encodeURIComponent(state.owner.id)}`;}
  function ownerAllowed(kind){return Boolean(state.owner?.[`can_read_${kind}`]);}
  function ownerConnected(kind){return Boolean(state.owner?.[`${kind==='history'?'history':'experience'}_connected`]);}
  function fillContext(){
    const me=state.me;
    $('actor-name').textContent=me.current_user.display_name;
    $('admin-nav').hidden=!me.current_user.is_admin;
    $('admin-nav').href='/?#users';
    $('library-owner').textContent=state.owner?`当前查看：${state.owner.display_name}的知识库`:'当前查看：知识库不可用';
    $('library-access').textContent=inspect?'管理员只读查看':state.owner?.id===me.current_user.id?'我的知识库':'已授权 · 只读';
    const policy=me.policy||{};
    $('runtime-status').textContent=policy.ai_processing_enabled===false?'AI 自动总结已暂停 · 原文保留':policy.ai_processing_enabled===true?'AI 自动总结运行中':'运行状态尚未确认';
  }
  async function boot(){
    try{
      state.me=await api(`${ROOT}/me`);
      state.owner=(state.me.libraries||[]).find(x=>x.id===(query.get('user')||state.me.current_user.id)) || null;
      fillContext();
      window.EvolvMemHistory.configure({api:state.owner?`${libraryBase()}/history`:'',attachmentBase:state.owner?`${libraryBase()}/history/attachment`:'',storageKey:`evolvmem.history.${state.me.current_user.id}.${state.owner?.id||'none'}.${inspect?'inspect':'normal'}.`,canManage:!!state.owner?.can_manage_history&&!inspect,inspect});
      await route();
    }catch(e){$('actor-name').textContent='登录与绑定';$('library-context').hidden=true;$('runtime-status').textContent='';$('console-view').innerHTML=`${heading('暂时无法进入知识库','请核对登录和账号绑定状态。')}<div class="empty"><p role="alert">${esc(e.message)}</p><a class="button" href="/auth/login">重新登录</a></div>`;}
  }
  async function route(){
    if(!state.me)return;
    let parts=location.hash.replace(/^#\/?/,'').split('/'),tab=parts[0]||'libraries';
    if(!['history','experiences','libraries','users','principles'].includes(tab)){history.replaceState(null,'',`${location.pathname}${location.search}#libraries`);tab='libraries';}
    const ticket=++state.ticket;
    $('library-context').hidden=tab!=='history'&&tab!=='experiences';
    document.querySelectorAll('[data-nav]').forEach(a=>{if(a.dataset.nav===tab)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
    const isHistory=tab==='history'&&ownerAllowed('history')&&ownerConnected('history');
    $('history-view').hidden=!isHistory;$('console-view').hidden=isHistory;
    if(isHistory){window.EvolvMemHistory.mount($('history-view'));return;}
    if(tab==='history'||tab==='experiences'){
      const kind=tab==='history'?'history':'experience';
      if(!state.owner||!ownerAllowed(kind)){$('console-view').innerHTML=heading(kind==='history'?'历史资料':'项目经验','每位用户拥有独立的历史与经验。')+'<div class="empty">当前知识库或这类资料尚未获得访问授权。<a href="#libraries">查看可访问的知识库</a></div>';return;}
      if(!ownerConnected(kind)){$('console-view').innerHTML=heading(kind==='history'?'历史资料':'项目经验','每位用户拥有独立的历史与经验。')+'<div class="empty">此用户的'+(kind==='history'?'历史资料':'经验数据')+'尚未接入。接入后会显示其自己的内容。</div>';return;}
    }
    $('console-view').innerHTML='<p class="empty">正在读取…</p>';
    try{
      if(tab==='libraries')await libraries(ticket);
      else if(tab==='users')await users(parts[1]==='grants'?'grants':'accounts',ticket);
      else if(tab==='experiences')await experiences(parts[1],ticket);
      else if(tab==='principles'){$('console-view').innerHTML=window.EvolvMemPrinciples.render(state.me.policy||{});}
    }catch(e){if(ticket===state.ticket)$('console-view').innerHTML=heading('页面暂时无法读取','资料仍保留在原来的知识库中。')+`<p class="empty error" role="alert">${esc(e.message)}</p>`;}
  }
  async function libraries(ticket){
    const admin=state.me.current_user.is_admin;
    // All-user inspection is explicit, never grants ordinary cross-user access.
    const data=await api(`${ROOT}/libraries${admin?'?inspect=1':''}`,undefined,{normal:true});
    if(ticket!==state.ticket)return;
    $('console-view').innerHTML=heading('用户知识库',admin?'从这里选择要查看的知识库。自己的资料可正常管理；查看其他用户时为管理员只读模式。':'从这里选择自己的知识库，或已授权给你的知识库。查看资料不会改变登录账号。')+`<div class="library-grid">${data.libraries.map(lib=>{
      const inspection=admin&&lib.id!==state.me.current_user.id;
      return `<article class="library-card" data-library-id="${esc(lib.id)}"><div class="section-head"><div><h2>${esc(lib.display_name)}${lib.id===state.me.current_user.id?' <span class="badge">我的</span>':''}</h2><span class="library-id">${esc(lib.id)}</span></div><span class="badge ${lib.enabled?'good':'disabled'}">${lib.enabled?'已启用':'已停用'}</span></div><div class="library-kinds">${[['history','历史','history_count','段有问答原文'],['experience','经验','experience_count','条经验（全部状态）']].map(([kind,name,count,unit])=>`<div class="kind"><span>${name}</span><strong>${lib[`can_read_${kind}`]?esc(lib[count]??0):'—'}</strong><p>${esc(sourceState(lib,kind))}${lib[`can_read_${kind}`]?' · '+unit:''}</p>${lib[`can_read_${kind}`]?`<a class="button" href="${libraryURL(lib.id,kind==='history'?'history':'experiences',inspection)}">${inspection?'管理员查看（只读）':'打开'+name}</a>`:'<span class="muted">未开放</span>'}</div>`).join('')}</div></article>`;
    }).join('')||'<p class="empty">暂时没有可访问的知识库。</p>'}</div>`;
  }
  const adminTabs=tab=>`<nav class="tabs" aria-label="用户管理"><a href="#users" class="${tab==='accounts'?'active':''}">用户账号</a><a href="#users/grants" class="${tab==='grants'?'active':''}">交叉访问权限</a></nav>`;
  async function users(tab,ticket){
    if(!state.me.current_user.is_admin)throw new Error('用户与权限仅对管理员开放。');
    const data=await api(`${ROOT}/users`,undefined,{normal:true});state.users=data.users;state.pending=data.pending;
    if(ticket!==state.ticket)return;
    if(tab==='grants'){const grants=await api(`${ROOT}/grants`,undefined,{normal:true});state.grants=grants.rows;if(ticket!==state.ticket)return;}
    $('console-view').innerHTML=heading('用户与权限',tab==='accounts'?'管理账号、飞书登录绑定和启停状态。':'分别设置“谁能看谁”。历史与经验分别授权，每个方向独立生效。',tab==='accounts'?'<button class="primary" type="button" id="add-user">添加用户</button>':'')+adminTabs(tab)+(tab==='accounts'?accountTable():grantTable());
    $('add-user')?.addEventListener('click',()=>editUser(null));
    document.querySelectorAll('[data-edit-user]').forEach(b=>b.onclick=()=>editUser(state.users.find(u=>u.id===b.dataset.editUser)));
    document.querySelectorAll('[data-save-grant]').forEach(b=>b.onclick=()=>saveGrant(b));
  }
  function accountTable(){return `<section class="panel table-scroll"><table><thead><tr><th>用户</th><th>登录身份</th><th>数据接入</th><th>CLI 接入</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.users.map(u=>`<tr><td><b>${esc(u.display_name)}</b>${u.is_admin?' <span class="badge">管理员</span>':''}<small>${esc(u.id)}</small></td><td>${u.login_bound?'已绑定飞书':'尚未绑定'}</td><td>历史：${u.history_connected?'已接入':'未接入'}<small>经验：${u.experience_connected?'已接入':'未接入'}</small></td><td>${u.lan_state==='existing_identity'?'已有接入身份':'尚未配置'}</td><td><span class="badge ${u.enabled?'good':'disabled'}">${u.enabled?'已启用':'已停用'}</span></td><td><button type="button" data-edit-user="${esc(u.id)}">编辑</button></td></tr>`).join('')}</tbody></table></section><section class="panel"><h2>等待绑定的飞书登录</h2><p class="muted">用户先通过飞书登录后会出现在这里。绑定账号之前无法读取知识。</p>${state.pending.length?`<ul>${state.pending.map(p=>`<li>${esc(p.name)} <span class="muted">最近登录 ${esc(p.last_seen)}</span></li>`).join('')}</ul>`:'<p class="muted">暂无等待绑定的登录。</p>'}</section>`;}
  function grantTable(){
    const pairs=[];for(const viewer of state.users)for(const owner of state.users)if(viewer.id!==owner.id){const old=state.grants.find(r=>r.viewer_id===viewer.id&&r.owner_id===owner.id)||{revision:0,history_read:false,experience_read:false};pairs.push({viewer,owner,...old});}
    return `<div class="notice">自己的知识始终可读。开启 A → B 不会开启 B → A。普通浏览（包括管理员）遵循这里的授权；“用户知识库”中的管理员查看是单独的只读管理入口。</div><section class="panel table-scroll"><table><thead><tr><th>访问方向</th><th>允许看历史</th><th>允许看经验</th><th>操作</th></tr></thead><tbody>${pairs.map((p,i)=>`<tr data-grant-row="${i}" data-viewer="${esc(p.viewer.id)}" data-owner="${esc(p.owner.id)}" data-revision="${esc(p.revision)}"><td><div class="grant-direction"><b>${esc(p.viewer.display_name)}</b><span class="grant-arrow">→</span><b>${esc(p.owner.display_name)}</b></div><small>${esc(p.viewer.id)} → ${esc(p.owner.id)}</small></td><td><label class="check"><input type="checkbox" data-grant-history ${p.history_read?'checked':''} aria-label="${esc(p.viewer.display_name)}可看${esc(p.owner.display_name)}的历史">历史</label></td><td><label class="check"><input type="checkbox" data-grant-experience ${p.experience_read?'checked':''} aria-label="${esc(p.viewer.display_name)}可看${esc(p.owner.display_name)}的经验">经验</label></td><td><button class="grant-save" type="button" data-save-grant>保存</button></td></tr>`).join('')}</tbody></table></section>`;
  }
  async function saveGrant(button){const row=button.closest('tr');button.disabled=true;button.textContent='保存中…';try{const result=await api(`${ROOT}/grants`,{viewer_id:row.dataset.viewer,owner_id:row.dataset.owner,history_read:row.querySelector('[data-grant-history]').checked,experience_read:row.querySelector('[data-grant-experience]').checked,revision:Number(row.dataset.revision)});row.dataset.revision=String(result.row.revision);button.textContent='已保存';notice('有向授权已保存；反方向的设置保持独立。');}catch(e){button.textContent='重试';notice(e.message,true);}finally{button.disabled=false;}}
  function editUser(user){
    state.edit=user;const form=$('user-form');form.reset();form.elements.id.value=user?.id||'';form.elements.id.disabled=!!user;form.elements.display_name.value=user?.display_name||'';form.elements.enabled.checked=user?user.enabled:true;form.elements.enabled.disabled=!!user?.is_admin;
    $('user-dialog-title').textContent=user?'编辑用户':'添加用户';$('user-form-error').textContent='';
    const identities=[...(user?.open_id?[{open_id:user.open_id,name:'当前绑定身份'}]:[]),...state.pending.filter(p=>p.open_id!==user?.open_id)];
    $('login-binding').innerHTML='<option value="">暂不绑定</option>'+identities.map(p=>`<option value="${esc(p.open_id)}">${esc(p.name)} · ${esc(p.open_id.slice(-8))}</option>`).join('');
    form.elements.open_id.value=user?.open_id||'';form.elements.open_id.disabled=!!user?.is_admin;$('user-dialog').showModal();
  }
  $('user-form').onsubmit=async event=>{event.preventDefault();const form=event.currentTarget,button=form.querySelector('[type=submit]');button.disabled=true;const user=state.edit;const body={display_name:form.elements.display_name.value.trim(),enabled:form.elements.enabled.checked};if(!user){body.id=form.elements.id.value.trim();if(form.elements.open_id.value)body.open_id=form.elements.open_id.value;}else{body.revision=user.revision;if(!user.is_admin)body.open_id=form.elements.open_id.value||null;}
    try{await api(`${ROOT}/users${user?'/'+encodeURIComponent(user.id):''}`,body);$('user-dialog').close();notice('用户设置已保存。');await route();}catch(e){$('user-form-error').textContent=e.message;}finally{button.disabled=false;}};
  document.querySelectorAll('[data-close-user]').forEach(b=>b.onclick=()=>$('user-dialog').close());
  function asText(value){return typeof value==='string'?value:JSON.stringify(value,null,2);}
  function detailSection(title,value){if(value===null||value===undefined||value===''||Array.isArray(value)&&!value.length)return '';return `<section><h3>${esc(title)}</h3>${Array.isArray(value)?`<ol>${value.map(x=>`<li class="experience-text">${esc(asText(x))}</li>`).join('')}</ol>`:`<div class="experience-text">${esc(asText(value))}</div>`}</section>`;}
  function experienceState(d){return d.origin==='history'?`${reviewLabel[d.review_state]||'待复核'} · 未独立验证`:d.status==='active'&&d.success_count>0&&!d.expired?'已验证':d.status==='candidate'?'候选':'已失效';}
  function experienceProjects(d){return d.origin==='history'?(d.source_projects||[]).join('、')||'来源项目待确认':d.project||'未归属';}
  function experienceSources(d){
    if(d.origin!=='history')return detailSection('来源',d.sources);
    const kinds={question:'用户提问',problem:'问题原文',solution:'解决过程',answer:'回答原文',result:'结果记录',evidence:'验证依据'};
    return `<section><h3>原文依据</h3><p>来源项目：${esc(experienceProjects(d))}。来源关联不代表方法适用于所有这些项目。</p>${(d.sources||[]).map((s,i)=>`<figure class="experience-source"><figcaption>依据 ${i+1}${s.kind?' · '+esc(kinds[s.kind]||s.kind):''}${s.ordinal!==undefined?' · 原文段落 '+esc(s.ordinal):''}</figcaption><blockquote class="experience-text">${esc(s.quote)}</blockquote></figure>`).join('')||'<p>此条资料尚未提供可展示的原文摘录。</p>'}</section>`;
  }
  function experienceURL(values={},clear=false){
    const url=new URL(location.href);
    if(clear)for(const name of ['q','project','category','status','page'])url.searchParams.delete(name);
    for(const [name,value] of Object.entries(values)){if(value)url.searchParams.set(name,value);else url.searchParams.delete(name);}
    url.searchParams.delete('page');url.hash='experiences';return url.href;
  }
  function pendingCount(counts){return ['pending_session_review','pending_root_acceptance','needs_revision'].reduce((sum,key)=>sum+(counts?.[key]||0),0);}
  function categoryCount(counts,review){return !counts?'':review==='pending'?pendingCount(counts):counts[review]??counts.all??0;}
  async function experiences(itemId,ticket){
    const params=new URLSearchParams({origin:query.get('origin')||'history',status:query.get('status')||'all',q:query.get('q')||'',project:query.get('project')||'',category:query.get('category')||'',review:query.get('review')||'accepted',page:query.get('page')||'1',page_size:'25'});
    const data=await api(`${libraryBase()}/experiences?${params}`);
    if(ticket!==state.ticket)return;
    const fromHistory=params.get('origin')==='history',review=params.get('review'),summary=data.summary;
    const selected=String(itemId||data.rows[0]?.id||'');
    const selectedView=review==='accepted'?'accepted':review==='all'?'all':'pending';
    const views=[['accepted','已整理完成',summary?.reviews?.accepted],['all','全部资料',summary?.total],['pending','处理中',summary?pendingCount(summary.reviews):undefined]];
    const progress=fromHistory?`<section class="experience-progress" aria-label="本库历史提炼进度"><p class="experience-progress-title">本库历史提炼</p><nav class="experience-states" aria-label="整理状态，本库全部资料">${views.map(([value,name,count])=>`<button type="button" data-experience-review="${value}" aria-current="${selectedView===value}"><span>${name}</span><strong>${count===undefined?'—':esc(count)}</strong></button>`).join('')}</nav><p class="experience-boundary">整理验收与独立验证分别记录；这里的历史提炼仍是候选经验。</p></section>`:'';
    const listTitle=fromHistory?views.find(([value])=>value===selectedView)[1]:'其他已有经验';
    $('console-view').innerHTML=heading('项目经验',fromHistory?'从已整理完成的经验开始，查看解决方法、关键改动和原文依据。':'查看已有经验的问题、解决方法和验证记录。','<button id="experience-refresh" type="button">刷新资料</button>')+progress+`
      <form id="experience-filters" class="filters experience-filters">
        <input type="hidden" name="review" value="${esc(review)}">
        <label class="grow">查找经验<input name="q" type="search" value="${esc(params.get('q'))}" placeholder="搜索问题、方法或关键改动"></label>
        <label>经验分类<select name="category"><option value="">全部分类</option>${experienceCategories.map(v=>{const count=categoryCount(summary?.categories?.[v],review);return `<option value="${esc(v)}" ${params.get('category')===v?'selected':''}>${esc(v)}${count!==''?' · '+count:''}</option>`;}).join('')}</select></label>
        <label>来源项目<input name="project" value="${esc(params.get('project'))}" placeholder="项目名称"></label>
        <label>资料来源<select name="origin"><option value="history" ${fromHistory?'selected':''}>历史提炼</option><option value="core" ${!fromHistory?'selected':''}>其他已有经验</option></select></label>
        <label id="experience-validation-filter" ${fromHistory?'hidden':''}>验证状态<select name="status">${Object.entries(label).map(([v,t])=>`<option value="${v}" ${params.get('status')===v?'selected':''}>${t}</option>`).join('')}</select></label>
        <button type="submit" class="primary">查找</button><a id="experience-clear" class="button" href="${experienceURL({review,origin:params.get('origin')},true)}">清除筛选</a>
      </form>
      <div class="experience-results-head"><h2>${esc(listTitle)}</h2><p role="status">找到 ${esc(data.total)} 条经验</p></div>
      <div class="experience-layout"><div class="experience-results" id="experience-results"><div class="experience-list" aria-label="经验列表">${data.rows.map(r=>`<button class="experience-entry" type="button" data-experience="${esc(r.id)}" aria-current="${String(r.id)===selected}"><span class="experience-card-meta"><span class="badge">${esc(r.category||'项目经验')}</span><span>${esc(experienceState(r))}</span></span><strong>${esc(r.title||r.problem)}</strong>${r.title&&r.title!==r.problem?`<span class="experience-card-problem">问题：${esc(r.problem)}</span>`:''}${r.method_summary?`<span class="experience-card-method">方法：${esc(r.method_summary)}</span>`:''}<span class="experience-card-source">来源：${esc(experienceProjects(r))}</span></button>`).join('')||`<div class="empty"><p>${fromHistory&&review==='accepted'&&!summary?.reviews?.accepted?'此知识库尚无已整理完成的经验。可查看全部资料或处理中记录。':'当前筛选下没有经验。可清除筛选，或切换到全部资料。'}</p><a class="button" href="${experienceURL({review:'all'},true)}">查看全部资料</a></div>`}</div><div class="pager"><button id="exp-prev" type="button" ${data.page<=1?'disabled':''}>上一页</button><span>${esc(data.page)} / ${Math.max(1,Math.ceil(data.total/data.page_size))}</span><button id="exp-next" type="button" ${data.page*data.page_size>=data.total?'disabled':''}>下一页</button></div></div><article id="experience-detail" class="experience-detail panel" aria-label="经验详情">${selected?'<p role="status">正在读取经验详情…</p>':'<p>选择其他整理状态或调整筛选后，可在这里查看经验全文。</p>'}</article></div>`;
    $('experience-filters').onsubmit=e=>{e.preventDefault();const f=e.currentTarget,values={};for(const name of ['q','project','status','origin','category','review'])values[name]=f.elements[name].value;if(values.origin==='history')values.status='all';else values.category='';location.href=experienceURL(values);};
    const toggleExtractionFilters=()=>{const history=$('experience-filters').elements.origin.value==='history';$('experience-filters').elements.category.disabled=!history;$('experience-validation-filter').hidden=history;};
    $('experience-filters').elements.origin.onchange=toggleExtractionFilters;toggleExtractionFilters();
    document.querySelectorAll('[data-experience-review]').forEach(b=>b.onclick=()=>{location.href=experienceURL({review:b.dataset.experienceReview,status:'all'});});
    $('experience-refresh').onclick=()=>route();
    document.querySelectorAll('[data-experience]').forEach(b=>b.onclick=()=>{location.hash=`experiences/${encodeURIComponent(b.dataset.experience)}`;});
    const pageChange=delta=>{const url=new URL(location.href);url.searchParams.set('page',String(data.page+delta));url.hash='experiences';location.href=url.href;};$('exp-prev').onclick=()=>pageChange(-1);$('exp-next').onclick=()=>pageChange(1);
    if(selected){
      const result=await api(`${libraryBase()}/experiences/${encodeURIComponent(decodeURIComponent(selected))}`);if(ticket!==state.ticket)return;
      const d=result.experience||result.item||result,box=$('experience-detail');
      box.innerHTML=`<div class="experience-detail-top"><span class="badge">${esc(d.category||'项目经验')}</span><button type="button" id="experience-back-list" class="quiet">返回列表</button></div><h2 tabindex="-1" id="experience-detail-title">${esc(d.title||d.problem)}</h2><p class="experience-detail-state">${esc(d.origin==='history'?`${reviewLabel[d.review_state]||'待复核'} · 候选经验 · 未独立验证`:experienceState(d))}</p>${detailSection('问题',d.problem)}${detailSection('解决步骤',d.steps)}${detailSection('关键改动',d.key_changes)}${detailSection('结果',d.origin==='history'?d.result?.summary:d.result)}${detailSection('适用条件',d.conditions)}${detailSection('为什么这样解决',d.rationale)}${detailSection('适用范围',d.applicability)}${d.origin==='history'&&!d.project?'<p>具体适用项目待确认，以适用条件和范围为准。</p>':''}${detailSection('不适用情况',d.exclusions)}${detailSection('验证记录',d.verification)}${experienceSources(d)}`;
      $('experience-back-list').onclick=()=>{const current=document.querySelector('.experience-entry[aria-current=true]')||$('experience-results');current.scrollIntoView({block:'start'});current.focus({preventScroll:true});};
      if(itemId){$('experience-detail-title').focus({preventScroll:true});if(innerWidth<=780)box.scrollIntoView({block:'start'});}
    }
  }
  $('logout').onclick=async()=>{try{await api('/auth/logout',{});location.href='/login';}catch(e){notice(e.message,true);}};
  window.addEventListener('hashchange',route);
  boot();
})();
