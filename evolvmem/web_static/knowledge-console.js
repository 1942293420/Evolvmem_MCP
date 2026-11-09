/* Identity-bound console. No actor identity or source paths are supplied by UI. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const ROOT = '/api/knowledge-console';
  const query = new URLSearchParams(location.search);
  const inspect = query.get('inspect') === '1';
  const state = {me:null,owner:null,users:[],pending:[],grants:[],ticket:0,edit:null,expPage:1};
  const experienceCategories=['前端经验','业务经验','系统环境经验'];
  const errors = {login_required:'登录已失效，请重新登录。',account_not_bound:'此飞书身份尚未绑定用户，请联系管理员在“用户与权限”中绑定。',unbound_identity:'此飞书身份尚未绑定用户，请联系管理员。',user_disabled:'此账号已停用。',account_disabled:'此账号已停用。',access_denied:'没有读取这类知识的权限。',permission_denied:'没有读取这类知识的权限。',forbidden:'没有操作权限。',admin_required:'此操作需要管理员权限。',revision_conflict:'资料已被其他操作更新，请刷新后重试。',invalid_csrf:'登录校验已过期，请刷新页面。',console_not_initialized:'用户管理尚未初始化，请联系管理员。',legacy_web_disabled:'旧功能已关闭。',experience_revision_conflict:'这条经验已被其他操作更新，请刷新后再编辑。',experience_not_found:'经验已移动或删除，请刷新列表。',invalid_experience_category:'请选择正确的经验分类。',experience_management_forbidden:'只能管理自己的经验。',invalid_experience_text:'请检查填写的内容和字数。',experience_request_id_conflict:'本次请求内容已变化，请关闭后重新操作。'};
  const inspectPath = path => inspect && path.startsWith(ROOT) ? `${path}${path.includes('?')?'&':'?'}inspect=1` : path;
  async function api(path, body, options={}) {
    const method = body === undefined ? 'GET' : 'POST';
    const url = method === 'GET' && !options.normal ? inspectPath(path) : path;
    let response;try{response=await fetch(url,{method,credentials:'same-origin',cache:'no-store',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':state.me?.csrf_token||''},...(body===undefined?{}:{body:JSON.stringify(body)})});}catch(_){throw new Error(method==='POST'?'网络连接中断，操作结果尚未确认。请保留内容后重试。':'网络连接中断，请重试。');}
    let data;try{data=await response.json();}catch(_){if(response.ok)throw new Error('服务器返回内容无法读取，操作结果尚未确认，请重试。');data={};}
    if(!response.ok){const error=new Error(errors[data.error] || (response.status===403?'没有操作权限。':response.status===401?'请登录后继续。':response.status===409?'资料已更新，请刷新后重试。':method==='POST'?'保存未完成，请检查内容后重试。':'读取未完成，请刷新后重试。'));error.code=data.error;error.status=response.status;throw error;}
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
    $('skills-nav').hidden=!me.current_user.is_admin;
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
  async function route(options={}){
    if(!state.me)return;
    state.routedURL=location.href;
    let parts=location.hash.replace(/^#\/?/,'').split('/'),tab=parts[0]||'libraries';
    if(!['history','experiences','libraries','skills','users','principles'].includes(tab)){history.replaceState(null,'',`${location.pathname}${location.search}#libraries`);tab='libraries';}
    const ticket=++state.ticket;
    if(tab!=='experiences')state.expView=null;
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
    if(tab!=='experiences')$('console-view').innerHTML='<p class="empty">正在读取…</p>';
    try{
      if(tab==='libraries')await libraries(ticket);
      else if(tab==='users')await users(parts[1]==='grants'?'grants':'accounts',ticket);
      else if(tab==='experiences')await experiences(parts[1],ticket,options);
      else if(tab==='skills'){await skills(ticket);}
      else if(tab==='principles'){$('console-view').innerHTML=window.EvolvMemPrinciples.render(state.me.policy||{});}
    }catch(e){if(ticket===state.ticket)$('console-view').innerHTML=heading('页面暂时无法读取','资料仍保留在原来的知识库中。')+`<p class="empty error" role="alert">${esc(e.message)}</p>`;}
  }
  async function skills(ticket) {
    const base = ROOT + '/skills';
    const options = {normal:true};
    const sources = [['','全部来源'],['official','官方'],['windows','Windows']];
    const formatSync = value => value ? new Date(value).toLocaleString('zh-CN') : '尚未同步';
    const initial = await api(base, undefined, options);
    if (ticket !== state.ticket) return;

    $('console-view').innerHTML = heading('Skill 库','查找官方和 Windows 上已同步的 Skill。') +
      '<form id="skill-filters" class="filters experience-filters">' +
      '<label class="grow">查找 Skill<input name="q" type="search" maxlength="160" placeholder="搜索名称、用途或来源"></label>' +
      '<label>来源<select name="source">' + sources.map(([key,label]) =>
        '<option value="' + key + '">' + label + '</option>').join('') +
      '</select></label><button type="submit" class="primary">查找</button></form>' +
      '<p id="skill-sync" class="muted"></p><p id="skill-count" role="status"></p>' +
      '<div class="experience-layout"><div class="experience-list" id="skill-results" aria-label="Skill 列表"></div>' +
      '<section class="panel skill-detail" id="skill-detail" aria-label="Skill 详情"><p class="muted">从列表选择一条 Skill。</p></section></div>';
    $('skill-sync').textContent = '官方同步 ' + formatSync(initial.sync.official) +
      ' · Windows 同步 ' + formatSync(initial.sync.windows);
    let listRequest = 0, detailRequest = 0;

    async function showDetail(id, file) {
      const request = ++detailRequest;
      const params = new URLSearchParams({id});
      if (file) params.set('file',file);
      const box = $('skill-detail');
      box.setAttribute('aria-busy','true');
      try {
        const data = await api(base + '?' + params, undefined, options);
        if (ticket !== state.ticket || request !== detailRequest) return;
        const skill = data.skill;
        box.innerHTML = '<h2>' + esc(skill.name) + '</h2><p class="muted skill-version">' +
          esc(skill.origin) + ' · ' + esc(skill.version) + '</p>' +
          (skill.url ? '<p><a target="_blank" rel="noopener noreferrer" href="' +
            esc(skill.url) + '">查看官方版本</a></p>' : '') +
          '<label>文件 <select id="skill-file">' + data.files.map(path =>
            '<option value="' + esc(path) + '"' + (path === data.file ? ' selected' : '') + '>' +
            esc(path) + '</option>').join('') + '</select></label>' +
          (data.base64 ? '<p><a class="button" download="' + esc(data.file.split('/').pop()) +
            '" href="data:application/octet-stream;base64,' + esc(data.base64) + '">下载文件</a></p>'
            : '<pre class="skill-text">' + esc(data.text) + '</pre>');
        $('skill-file').onchange = event => showDetail(id, event.target.value);
        document.querySelectorAll('[data-skill]').forEach(button =>
          button.setAttribute('aria-current', String(button.dataset.skill === id)));
      } catch (error) {
        if (ticket === state.ticket && request === detailRequest) box.textContent = error.message;
      } finally {
        if (request === detailRequest) box.removeAttribute('aria-busy');
      }
    }

    function showRows(data) {
      $('skill-count').textContent = data.rows.length + ' 条 Skill';
      $('skill-results').innerHTML = data.rows.map(skill =>
        '<button class="experience-entry" type="button" data-skill="' + esc(skill.id) + '">' +
        '<span class="experience-card-meta">' + esc(skill.origin) + '</span><strong>' + esc(skill.name) +
        '</strong><span class="experience-card-method">' + esc(skill.description) + '</span></button>'
      ).join('') || '<div class="empty">没有符合条件的 Skill。</div>';
      document.querySelectorAll('[data-skill]').forEach(button =>
        button.onclick = () => showDetail(button.dataset.skill));
    }

    showRows(initial);
    $('skill-filters').onsubmit = async event => {
      event.preventDefault();
      const request = ++listRequest;
      const params = new URLSearchParams(new FormData(event.currentTarget));
      try {
        const data = await api(base + '?' + params, undefined, options);
        if (ticket === state.ticket && request === listRequest) showRows(data);
      } catch (error) {
        if (ticket === state.ticket && request === listRequest) notice(error.message,true);
      }
    };
  }

  async function libraries(ticket){
    const admin=state.me.current_user.is_admin;
    // All-user inspection is explicit, never grants ordinary cross-user access.
    const data=await api(`${ROOT}/libraries${admin?'?inspect=1':''}`,undefined,{normal:true});
    if(ticket!==state.ticket)return;
    $('console-view').innerHTML=heading('用户知识库',admin?'从这里选择要查看的知识库。自己的资料可正常管理；查看其他用户时为管理员只读模式。':'从这里选择自己的知识库，或已授权给你的知识库。查看资料不会改变登录账号。')+`<div class="library-grid">${data.libraries.map(lib=>{
      const inspection=admin&&lib.id!==state.me.current_user.id;
      return `<article class="library-card" data-library-id="${esc(lib.id)}"><div class="section-head"><div><h2>${esc(lib.display_name)}${lib.id===state.me.current_user.id?' <span class="badge">我的</span>':''}</h2><span class="library-id">${esc(lib.id)}</span></div><span class="badge ${lib.enabled?'good':'disabled'}">${lib.enabled?'已启用':'已停用'}</span></div><div class="library-kinds">${[['history','历史','history_count','段有问答原文'],['experience','经验','experience_count','条经验']].map(([kind,name,count,unit])=>`<div class="kind"><span>${name}</span><strong>${lib[`can_read_${kind}`]?esc(lib[count]??0):'—'}</strong><p>${esc(sourceState(lib,kind))}${lib[`can_read_${kind}`]?' · '+unit:''}</p>${lib[`can_read_${kind}`]?`<a class="button" href="${libraryURL(lib.id,kind==='history'?'history':'experiences',inspection)}">${inspection?'管理员查看（只读）':'打开'+name}</a>`:'<span class="muted">未开放</span>'}</div>`).join('')}</div></article>`;
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
  function experienceURL(values={},itemId){
    const url=new URL(location.href);
    for(const name of ['origin','review','status','project'])url.searchParams.delete(name);
    for(const [name,value] of Object.entries(values)){if(value!==''&&value!==null&&value!==undefined)url.searchParams.set(name,String(value));else url.searchParams.delete(name);}
    url.hash=itemId ? 'experiences/'+encodeURIComponent(itemId) : 'experiences';return url.href;
  }
  function experienceNavigate(values={},options={}){
    const next=experienceURL(values,options.itemId);
    if(next!==location.href)history[options.replace?'replaceState':'pushState'](null,'',next);
    return route(options);
  }
  function requestId(){if(typeof crypto?.randomUUID==='function')return crypto.randomUUID();const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return 'manual-'+Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('');}
  function experienceCanManage(){return !inspect&&state.owner?.id===state.me.current_user.id&&state.owner?.can_manage_experience!==false;}
  function experienceDate(value){const date=new Date(value);return isNaN(date)?'':date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});}
  const experienceFields=['category','problem','solution','reusable_experience'];
  function experienceDraft(){
    const fields=$('experience-form').elements;
    return JSON.stringify(experienceFields.map(name=>fields[name].value));
  }
  function requestExperienceClose(){
    if(state.expSaving)return;
    if(state.expDraft===experienceDraft()){$('experience-dialog').close();return;}
    state.expDraftFocus=document.activeElement;
    $('experience-discard').hidden=false;
    $('experience-keep-editing').focus();
  }
  function keepEditingExperience(){
    $('experience-discard').hidden=true;
    (state.expDraftFocus || $('experience-form').elements.problem).focus();
  }
  function experienceEditor(record=null,category=experienceCategories[0]){
    if(!experienceCanManage())return;
    const form=$('experience-form');state.expEdit=record;state.expRequest=null;form.reset();
    $('experience-dialog-title').textContent=record?'编辑经验':'新增经验';
    form.elements.category.value=record?.category||category;
    for(const name of ['problem','solution','reusable_experience'])form.elements[name].value=record?.[name]||'';
    state.expEditorTrigger=document.activeElement;
    state.expDraft=experienceDraft();
    state.expDraftFocus=null;
    $('experience-discard').hidden=true;
    $('experience-form-error').textContent='';$('experience-dialog').showModal();form.elements.problem.focus();
  }
  $('experience-form').onsubmit=async event=>{
    event.preventDefault();if(!experienceCanManage())return;
    const form=event.currentTarget,button=form.querySelector('[type=submit]');if(button.disabled)return;
    const record=state.expEdit,body={action:record?'update':'create'};
    if(record){body.id=record.id;body.revision=record.revision;}
    for(const key of experienceFields)body[key]=form.elements[key].value.trim();
    if(['problem','solution','reusable_experience'].some(key=>!body[key])){$('experience-form-error').textContent='请填写问题、解决办法和可复用经验。';return;}
    const signature=JSON.stringify(body);if(state.expRequest?.signature!==signature)state.expRequest={signature,id:requestId()};
    state.expSaving=true;for(const control of form.elements)control.disabled=true;button.textContent='正在保存…';$('experience-form-error').textContent='';
    try{
      const result=await api(`${libraryBase()}/experiences`,{...body,request_id:state.expRequest.id});
      $('experience-dialog').close();state.expEdit=null;state.expRequest=null;
      notice('经验已保存。');await experienceNavigate({category:result.experience.category,q:'',page:'',deleted:''},{refreshExperience:true});
    }catch(e){$('experience-form-error').textContent=e.message||'保存未确认，请保留内容后重试。';}
    finally{state.expSaving=false;for(const control of form.elements)control.disabled=false;button.textContent='保存经验';}
  };
  document.querySelectorAll('[data-close-experience]').forEach(button=>button.onclick=requestExperienceClose);
  $('experience-keep-editing').onclick=keepEditingExperience;
  $('experience-discard-confirm').onclick=()=>{if(!state.expSaving)$('experience-dialog').close();};
  $('experience-dialog').addEventListener('cancel',event=>{
    event.preventDefault();
    if(state.expSaving)return;
    if($('experience-discard').hidden)requestExperienceClose();
    else keepEditingExperience();
  });
  $('experience-dialog').addEventListener('close',()=>{
    const trigger=state.expEditorTrigger;
    state.expEditorTrigger=null;
    if(trigger?.isConnected)trigger.focus({preventScroll:true});
    else $('experience-new')?.focus({preventScroll:true});
  });
  $('experience-delete-dialog').addEventListener('cancel',event=>{if(state.expDeleting)event.preventDefault();});
  $('experience-delete-cancel').onclick=()=>{if(!state.expDeleting)$('experience-delete-dialog').close();};
  $('experience-delete-confirm').onclick=async()=>{
    const pending=state.expDelete,button=$('experience-delete-confirm');if(!pending||button.disabled||!experienceCanManage())return;
    state.expDeleting=true;button.disabled=true;$('experience-delete-cancel').disabled=true;button.textContent='正在移入…';$('experience-delete-error').textContent='';
    try{await api(`${libraryBase()}/experiences`,{action:'delete',id:pending.record.id,revision:pending.record.revision,request_id:pending.requestId});$('experience-delete-dialog').close();notice('经验已移至回收站，可随时恢复。');await experienceNavigate({},{refreshExperience:true});}
    catch(e){$('experience-delete-error').textContent=e.message;}
    finally{state.expDeleting=false;button.disabled=false;$('experience-delete-cancel').disabled=false;button.textContent='移至回收站';}
  };
  function mountExperiences(view) {
    const actions = (view.canManage ? '<button type="button" class="primary" id="experience-new">新增经验</button>' : '') +
      '<button type="button" id="experience-refresh">刷新</button>';
    const categories = experienceCategories.map(name => '<button type="button" data-experience-category="' +
      esc(name) + '" class="experience-category"><span class="experience-category-top"><span class="experience-category-name">' +
      esc(name) + '</span><strong data-experience-category-count></strong></span></button>').join('');
    $('console-view').innerHTML =
      '<header class="page-head experience-page-head"><h1>经验</h1><div class="experience-page-actions">' + actions + '</div></header>' +
      '<form id="experience-filters" class="filters experience-filters"><label class="grow">查找经验' +
      '<input name="q" type="search" maxlength="200" placeholder="输入关键词或描述遇到的问题"></label>' +
      '<button type="submit" class="primary">查找</button><button id="experience-clear" type="button">清除搜索</button>' +
      '<button id="experience-trash" class="quiet" type="button">回收站</button></form>' +
      '<nav class="experience-categories" aria-label="经验分类">' + categories + '</nav>' +
      '<div class="experience-results-head"><h2 id="experience-results-title"></h2><p id="experience-count" role="status"></p></div>' +
      '<div class="experience-layout"><div class="experience-results" id="experience-results">' +
      '<div class="experience-list" aria-label="经验列表" id="experience-list"></div>' +
      '<div class="pager"><button id="exp-prev" type="button" disabled>上一页</button>' +
      '<span id="experience-page">1 / 1</span><button id="exp-next" type="button" disabled>下一页</button></div></div>' +
      '<article id="experience-detail" class="experience-detail panel" aria-label="经验详情"></article></div>';

    $('experience-filters').onsubmit = event => {
      event.preventDefault();
      const input = event.currentTarget.elements.q;
      input.value=input.value.trim();
      input.focus({preventScroll:true});
      experienceNavigate({q:input.value,page:''});
    };
    $('experience-clear').onclick = () => {
      const input=$('experience-filters').elements.q;
      input.value='';
      input.focus({preventScroll:true});
      experienceNavigate({q:'',page:''});
    };
    $('experience-refresh').onclick = () => route({refreshExperience:true});
    $('experience-trash').hidden = !view.canReadTrash;
    $('experience-trash').onclick = () => experienceNavigate({deleted:view.trash?'':'1',q:'',page:''});
    $('exp-prev').onclick = () => experienceNavigate({page:view.page-1});
    $('exp-next').onclick = () => experienceNavigate({page:view.page+1});
    document.querySelectorAll('[data-experience-category]').forEach(button => {
      button.onclick = () => experienceNavigate({category:button.dataset.experienceCategory,page:''});
    });
    $('experience-list').onclick = event => {
      const row = event.target.closest('[data-experience]');
      if (row && view.key === view.requestedKey) experienceNavigate({}, {itemId:row.dataset.experience, revealDetail:true});
    };
    if ($('experience-new')) $('experience-new').onclick = () => experienceEditor(null, view.category);
  }

  function renderExperienceList(view, data, query) {
    const summary = data.summary || {};
    view.first = data.rows[0]?.id || '';
    view.page = data.page;
    $('experience-trash').textContent = view.trash ? '返回经验' : '回收站 · ' + (summary.deleted_total || 0);
    $('experience-trash').setAttribute('aria-pressed', String(view.trash));
    $('experience-results-title').textContent = view.category + (view.trash ? ' · 回收站' : '');
    $('experience-count').textContent = data.total + ' 条' +
      (query && !view.trash ? (data.search_mode === 'hybrid' ? ' · 关键词＋语义' : ' · 关键词查找') : '') +
      (view.canManage ? '' : ' · 只读');
    document.querySelectorAll('[data-experience-category]').forEach(button => {
      const name = button.dataset.experienceCategory;
      button.setAttribute('aria-current', String(view.category === name));
      button.querySelector('[data-experience-category-count]').textContent = summary.categories?.[name] || 0;
    });
    $('experience-list').innerHTML = data.rows.map(row =>
      '<button class="experience-entry" type="button" data-experience="' + esc(row.id) + '">' +
      '<strong>' + esc(row.problem) + '</strong><span class="experience-card-meta"><span>' + esc(row.category) +
      '</span><span class="experience-card-source">' + esc(experienceDate(row.updated_at)) + '</span></span></button>'
    ).join('') || '<div class="empty"><p>' + (query ? '没有匹配的经验。' : view.trash ?
      '此分类的回收站为空。' : '还没有' + esc(view.category) + '。') + '</p>' +
      (query ? '<button type="button" id="experience-empty-clear">清除搜索</button>' :
      view.canManage && !view.trash ? '<button type="button" id="experience-empty-new" class="primary">添加第一条经验</button>' : '') + '</div>';
    $('experience-list').scrollTop = 0;
    if ($('experience-empty-clear')) $('experience-empty-clear').onclick = () => $('experience-clear').click();
    if ($('experience-empty-new')) $('experience-empty-new').onclick = () => experienceEditor(null,view.category);
    $('experience-page').textContent = data.page + ' / ' + Math.max(1,Math.ceil(data.total/data.page_size));
    $('exp-prev').disabled = data.page <= 1;
    $('exp-next').disabled = data.page * data.page_size >= data.total;
  }

  function revealExperienceDetail(box) {
    if (window.matchMedia('(max-width: 780px)').matches) {
      $('experience-detail-title')?.focus({preventScroll:true});
      box.scrollIntoView({block:'start'});
    }
  }

  async function selectExperience(view, itemId, ticket, revealDetail) {
    const selected = /^manual-[a-f0-9]{32}$/.test(itemId || '') ? itemId : view.first;
    const box = $('experience-detail');
    document.querySelectorAll('[data-experience]').forEach(button =>
      button.setAttribute('aria-current', String(button.dataset.experience === selected)));
    if (selected && selected === view.selected) {
      if (revealDetail) revealExperienceDetail(box);
      return;
    }
    view.selected = '';
    if (!selected) {
      box.removeAttribute('aria-busy');
      box.innerHTML = '<div class="experience-detail-empty"><p>选择一条经验查看详情。</p></div>';
      return;
    }

    box.setAttribute('aria-busy','true');
    box.innerHTML = '<p role="status">正在读取经验…</p>';
    try {
      const result = await api(libraryBase() + '/experiences/' + encodeURIComponent(selected) +
        '?origin=manual&deleted=' + (view.trash?'1':'0'));
      if (ticket !== state.ticket) return;
      const record = result.experience;
      view.selected = selected;
      const manageActions = view.canManage ? view.trash ?
        '<button type="button" class="primary" id="experience-restore">恢复经验</button>' :
        '<button type="button" id="experience-edit">编辑</button><button type="button" class="danger-quiet" id="experience-delete">移至回收站</button>' : '';
      box.innerHTML = '<div class="experience-detail-top"><span class="badge">' + esc(record.category) +
        '</span><div class="experience-detail-actions"><button type="button" id="experience-back-to-list">返回列表</button>' +
        manageActions + '</div></div><section class="experience-problem-section">' +
        '<h2 id="experience-detail-title" tabindex="-1">问题</h2><div class="experience-text">' + esc(record.problem) + '</div></section>' +
        detailSection('解决办法',record.solution) + detailSection('可复用经验',record.reusable_experience) +
        '<p class="experience-updated">更新于 ' + esc(experienceDate(record.updated_at)) + '</p>';
      $('experience-back-to-list').onclick = () => {
        const row = document.querySelector('[data-experience][aria-current="true"]');
        (row || $('experience-filters').elements.q).focus({preventScroll:true});
        $('experience-results').scrollIntoView({block:'start'});
        row?.scrollIntoView({block:'nearest'});
      };
      if ($('experience-edit')) $('experience-edit').onclick = () => experienceEditor(record,view.category);
      if ($('experience-delete')) $('experience-delete').onclick = () => {
        state.expDelete = {record,requestId:requestId()};
        $('experience-delete-problem').textContent = record.problem;
        $('experience-delete-error').textContent = '';
        $('experience-delete-dialog').showModal();
      };
      if ($('experience-restore')) {
        const id = requestId();
        $('experience-restore').onclick = async event => {
          const button = event.currentTarget;
          button.disabled = true;
          button.textContent = '正在恢复…';
          try {
            await api(libraryBase()+'/experiences',{action:'restore',id:record.id,revision:record.revision,request_id:id});
            notice('经验已恢复。');
            await experienceNavigate({}, {refreshExperience:true});
          } catch (error) {
            notice(error.message,true);
            button.disabled = false;
            button.textContent = '恢复经验';
          }
        };
      }
      if (revealDetail) revealExperienceDetail(box);
    } catch (error) {
      if (ticket === state.ticket) box.innerHTML = '<p class="error" role="alert">' + esc(error.message) +
        '</p><p>可以重新选择列表中的经验，或刷新资料。</p>';
    } finally {
      if (ticket === state.ticket) box.removeAttribute('aria-busy');
    }
  }

  async function experiences(itemId, ticket, {refreshExperience=false,revealDetail=false}={}) {
    const current = new URLSearchParams(location.search);
    const category = experienceCategories.includes(current.get('category')) ? current.get('category') : experienceCategories[0];
    if (!state.expView) {
      state.expView = {canReadTrash:state.owner?.id === state.me.current_user.id, canManage:experienceCanManage()};
      mountExperiences(state.expView);
    }
    const view = state.expView;
    const trash = view.canReadTrash && current.get('deleted') === '1';
    const q = current.get('q') || '';
    const params = new URLSearchParams({origin:'manual',category,q,
      page:String(Math.max(1,parseInt(current.get('page'),10)||1)),page_size:'20',deleted:trash?'1':'0'});
    const key = params.toString();
    view.requestedKey = key;
    const input = $('experience-filters').elements.q;
    if (view.query !== q) {
      input.value = q;
      view.query = q;
    }
    if (view.key === key && !refreshExperience) {
      $('experience-results').removeAttribute('aria-busy');
      await selectExperience(view,itemId,ticket,revealDetail);
      return;
    }

    $('experience-results').setAttribute('aria-busy','true');
    $('experience-count').textContent = '正在查找…';
    try {
      const data = await api(libraryBase() + '/experiences?' + params);
      if (ticket !== state.ticket) return;
      if (!data.rows.length && data.page > 1) {
        await experienceNavigate({page:Math.max(1,Math.ceil(data.total/data.page_size))}, {replace:true,itemId});
        return;
      }
      Object.assign(view,{key,category,trash,selected:''});
      renderExperienceList(view,data,q);
    } catch (error) {
      if (ticket === state.ticket) {
        $('experience-count').textContent = error.message;
        const box = $('experience-detail');
        if (box.hasAttribute('aria-busy')) {
          box.removeAttribute('aria-busy');
          box.innerHTML = '<p>刷新后可以继续查看经验。</p>';
        }
      }
      return;
    } finally {
      if (ticket === state.ticket) $('experience-results').removeAttribute('aria-busy');
    }
    await selectExperience(view,itemId,ticket,revealDetail);
  }

  $('logout').onclick=async()=>{try{await api('/auth/logout',{});location.href='/login';}catch(e){notice(e.message,true);}};
  $('skip-to-content').onclick=event=>{
    event.preventDefault();
    $('workspace').focus();
  };
  function onLocationChange(){
    if(location.href!==state.routedURL)route();
  }
  window.addEventListener('hashchange',onLocationChange);
  window.addEventListener('popstate',onLocationChange);
  boot();
})();
