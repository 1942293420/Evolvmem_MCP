/* Central entry permission and identity-scoped workbench state. */
(function (root) {
  'use strict';
  const E = root.EvolvMemV2;
  if (!E || !root.location || !/^https?:$/.test(root.location.protocol)) return;
  const nativeFetch = root.fetch.bind(root);
  const byId = id => root.document && root.document.getElementById(id);
  const uiKeys = ['route','theme','libraryCtx','stageFilter','inboxTrack','expSubtab','expFilters','intFilters','histQuery','histScope','histDb','histSubtab','selectedProjectKey','selectedBlockId','evoSubtab','ctrlSubtab'];
  const defaults = E.createInitialState();
  const standaloneState=E.state, standaloneCanWrite=E.canWrite;
  const S = E.session = {status:'checking',scope:'',name:'',csrf:'',epoch:0,consoleURL:'http://192.168.1.135:9600',dataReady:false};
  let checking = null, draft = null;
  const copy = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));

  function empty(value) {
    if (Array.isArray(value)) return [];
    if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).map(key => [key, empty(value[key])]));
    if (typeof value === 'number') return 0;
    if (typeof value === 'boolean') return false;
    return typeof value === 'string' ? '' : null;
  }
  function cleanState() {
    const state = empty(defaults);
    uiKeys.forEach(key => { if (defaults[key] !== undefined) state[key] = copy(defaults[key]); });
    state.apiMode='live'; state.simMode='normal'; state.ownerId=''; state.libraryCtx=''; state.csrfToken='';
    state.histDb='';state.selectedBlockId='';state.selectedProjectKey='';
    state.topoNodes=copy(defaults.topoNodes || []);
    return state;
  }
  E.state = cleanState();
  S.isReady = () => S.status === 'ready';
  S.capture = (watchLibrary=true) => ({epoch:S.epoch,state:E.state,library:E.state.libraryCtx,watchLibrary});
  S.isCurrent = ticket => S.isReady() && ticket.epoch===S.epoch && ticket.state===E.state && (!ticket.watchLibrary || ticket.library===E.state.libraryCtx);
  S.guardResult = (status,data,ticket) => ({
    get status(){return status>=200 && status<300 && !S.isCurrent(ticket)?409:status;},
    get data(){return status>=200 && status<300 && !S.isCurrent(ticket)?{ok:false,error:'stale_response'}:data;}
  });
  S.safeNext = value => {
    if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//') || /[\\\r\n\x00]/.test(value)) return '/';
    try {
      const url = new URL(value, root.location.origin);
      if (url.origin !== root.location.origin || /^\/(?:auth(?:\/|$)|login(?:\/|$))/.test(url.pathname)) return '/';
      return url.pathname + url.search + url.hash;
    } catch (_) { return '/'; }
  };
  S.loginURL = () => '/auth/login?next=' + encodeURIComponent(S.safeNext(root.location.pathname + root.location.search + root.location.hash));

  function fields() {
    const node = byId('modal-body');
    return node ? Array.from(node.querySelectorAll('input[id],textarea[id],select[id]')).filter(el => el.type !== 'password').map(el => ({id:el.id,value:el.value,checked:el.checked})) : [];
  }
  S.save = () => {
    if (!S.scope || !S.isReady() || S.clearing) return;
    const snapshot = {ui:{},draft:null};
    uiKeys.forEach(key => { if (E.state[key] !== undefined) snapshot.ui[key]=copy(E.state[key]); });
    const modal=byId('modal-backdrop'), body=byId('modal-body'), title=byId('modal-title');
    if (modal && body && modal.classList.contains('open') && fields().length) {
      snapshot.draft={html:body.innerHTML,title:title ? title.textContent : '',fields:fields()};
    }
    try {
      root.sessionStorage.setItem('evolvmem.ui.v1.' + S.scope, JSON.stringify(snapshot));
      if(E.state.libraryCtx)root.sessionStorage.setItem('evolvmem.ui.v1.' + S.scope + '.library.' + encodeURIComponent(E.state.libraryCtx), JSON.stringify(snapshot));
    } catch (_) {}
  };
  function restore() {
    draft=null;
    try {
      const snapshot=JSON.parse(root.sessionStorage.getItem('evolvmem.ui.v1.'+S.scope) || 'null');
      if (!snapshot || !snapshot.ui || typeof snapshot.ui !== 'object') return;
      uiKeys.forEach(key => { if (Object.prototype.hasOwnProperty.call(snapshot.ui,key)) E.state[key]=snapshot.ui[key]; });
      draft=snapshot.draft;
    } catch (_) {}
  }
  function applyURLRoute(){
    const route=root.location.hash.replace(/^#/, '');
    if(E.MODULES && E.MODULES.some(item=>item.id===route))E.state.route=route;
  }
  S.restoreDraft = () => {
    if (!S.isReady() || !draft || !E.canWrite()) return;
    const body=byId('modal-body'), modal=byId('modal-backdrop'), title=byId('modal-title');
    if (!body || !modal) return;
    body.innerHTML=draft.html;
    if (title) title.textContent=draft.title;
    (draft.fields || []).forEach(field => { const el=byId(field.id); if (el && body.contains(el)) {el.value=field.value;el.checked=field.checked;} });
    modal.classList.add('open'); draft=null;
  };
  S.discardDraft = () => {draft=null;};
  S.switchLibrary = id => {
    const old=E.state;
    if(!S.isReady() || !(old.availableLibraries || []).some(lib=>lib.id===id))return false;
    S.save();
    const identity={ownerId:old.ownerId,currentUser:old.currentUser,availableLibraries:old.availableLibraries,route:old.route,theme:old.theme};
    clearPrivate();Object.assign(E.state,identity,{libraryCtx:id,csrfToken:S.csrf});draft=null;
    try{
      const snapshot=JSON.parse(root.sessionStorage.getItem('evolvmem.ui.v1.'+S.scope+'.library.'+encodeURIComponent(id)) || 'null');
      if(snapshot && snapshot.ui){uiKeys.filter(key=>key!=='route'&&key!=='theme'&&key!=='libraryCtx').forEach(key=>{if(Object.prototype.hasOwnProperty.call(snapshot.ui,key))E.state[key]=snapshot.ui[key];});draft=snapshot.draft;}
    }catch(_){}
    if(E.renderAll)E.renderAll();return true;
  };

  function clearPrivate() {
    S.clearing=true;
    try {
    S.epoch++; S.dataReady=false;
    E.state=cleanState();
    ['drawer-body','modal-body','workspace-main','top-nav','toast-box'].forEach(id => {const el=byId(id);if(el)el.replaceChildren();});
    ['drawer-backdrop','modal-backdrop'].forEach(id => {const el=byId(id);if(el)el.classList.remove('open');});
    ['drawer-title','drawer-sub','modal-title'].forEach(id => {const el=byId(id);if(el)el.textContent='';});
    const select=byId('ctx-lib-sel');if(select)select.replaceChildren();
    } finally {S.clearing=false;}
  }
  S.block = status => {
    S.save(); clearPrivate(); S.status=status;
    if (E.renderAll) E.renderAll();
    S.syncHeader();
  };
  S.syncHeader = () => {
    const controls=byId('auth-controls');if(controls)controls.hidden=!S.detected || S.active===false;
    const name=byId('auth-name'), login=byId('auth-login'), logout=byId('auth-logout'), consoleLink=byId('auth-console');
    if(name)name.textContent=['ready','pending','disabled'].includes(S.status) && S.name && S.scope ? S.name : S.status==='login'?'未登录':S.status==='checking'?'正在核对登录…':'登录待核对';
    if(login){login.href=S.loginURL();login.hidden=!!S.scope && S.status!=='login';}
    if(logout){logout.hidden=!S.scope || S.status==='login';logout.disabled=false;}
    if(consoleLink)consoleLink.href=S.consoleURL;
    const select=byId('ctx-lib-sel');if(select){select.disabled=!S.isReady() || !S.dataReady;select.hidden=select.disabled;}
    const nav=byId('top-nav');if(nav)nav.hidden=!S.isReady() || !S.dataReady;
    if(root.document && root.document.querySelectorAll)root.document.querySelectorAll('[data-act="open-cmd"],[data-act="open-diag"]').forEach(el=>{el.hidden=!S.isReady() || !S.dataReady;});
  };
  S.panel = () => {
    const messages={checking:['正在核对登录',''],login:['请通过飞书登录','登录后回到当前页面。'],pending:['尚未分配 EvolvMem 内部权限','已通过总控台验证，请联系管理员分配 EvolvMem 内部账号和知识库权限。'],disabled:['EvolvMem 内部账号已停用','请联系管理员核对账号状态。'],forbidden:['当前无法进入 EvolvMem','请到总控台核对系统访问权限。'],unavailable:['暂时无法核对访问权限','服务恢复后会自动重试。']};
    const item=messages[S.status] || ['正在读取你的知识库',''];
    const esc=E.esc || (x => String(x));
    return '<section class="panel session-panel" role="status"><h1>'+esc(item[0])+'</h1>'+(item[1]?'<p>'+esc(item[1])+'</p>':'')+'<div class="session-actions">'+(S.status==='login'?'<a class="btn btn-primary" href="'+esc(S.loginURL())+'">飞书登录</a>':'')+(S.status!=='checking'?'<a class="btn" href="'+esc(S.consoleURL)+'">总控台</a><button class="btn" type="button" data-act="auth-retry">重新核对</button>':'')+'</div></section>';
  };

  S.check = options => {
    if(S.active===false)return Promise.resolve(true);
    if(checking)return checking;
    checking=(async () => {
      const wasReady=S.isReady(), previousScope=S.scope, epoch=S.epoch;
      try {
        const resp=await nativeFetch(S.detected?'/auth/ops/status':'/api/auth/me',{credentials:'same-origin',cache:'no-store'});
        const me=await resp.json();
        if(epoch!==S.epoch)return false;
        if(!S.detected){
          if(me.central_sso===true){S.detected=true;}
          else if(resp.status===200 && typeof me.enabled==='boolean'){
            S.active=false;E.state=standaloneState;E.canWrite=standaloneCanWrite;delete E.session;
            const controls=byId('auth-controls');if(controls)controls.hidden=true;
            return true;
          }else{S.block('unavailable');return false;}
        }
        if(resp.status!==200 || !me.authenticated){
          S.block(resp.status===401?'login':resp.status===403?'forbidden':'unavailable');
          if(resp.status===401){S.scope='';S.name='';S.csrf='';S.syncHeader();}
          return false;
        }
        if(typeof me.user_scope!=='string' || !/^[a-zA-Z0-9_-]{8,128}$/.test(me.user_scope)){S.block('unavailable');return false;}
        const nextStatus=me.native_status==='ready'?'ready':me.native_status==='disabled'?'disabled':'pending';
        if(previousScope!==me.user_scope || !wasReady || nextStatus!=='ready'){
          S.save();clearPrivate();S.scope=me.user_scope;restore();applyURLRoute();
        }
        S.name=String(me.name || '飞书用户');S.csrf=me.csrf_token || '';S.status=nextStatus;E.state.csrfToken=S.csrf;
        try{const url=new URL(me.central_console_url);if(/^https?:$/.test(url.protocol))S.consoleURL=url.href;}catch(_){}
        S.syncHeader();if(E.renderAll)E.renderAll();
        if(nextStatus==='ready' && (!wasReady || previousScope!==S.scope) && options?.resume!==false && E.api) await E.api.bootstrap({statusKnown:true});
        return S.isReady();
      } catch (_) {S.block('unavailable');return false;}
    })().finally(() => {checking=null;});
    return checking;
  };
  S.request = async (method,path,body) => {
    const epoch=S.epoch,library=E.state.libraryCtx;
    const ticket=S.capture();
    if(!S.isReady())return {status:409,data:{ok:false,error:'session_not_ready'}};
    try {
      const opts={method,credentials:'same-origin',cache:'no-store',headers:{}};
      if(body!==undefined){opts.headers['Content-Type']='application/json';opts.headers['X-CSRF-Token']=S.csrf;opts.body=JSON.stringify(body);}
      const resp=await nativeFetch(path,opts);let data;
      try{data=await resp.json();}catch(_){data={ok:false,error:'invalid_json'};}
      if(epoch!==S.epoch || library!==E.state.libraryCtx)return {status:409,data:{ok:false,error:'stale_response'}};
      if([401,403,503].includes(resp.status)){
        S.block(resp.status===401?'login':resp.status===503?'unavailable':data.error==='account_pending'?'pending':data.error==='account_disabled'?'disabled':'forbidden');
        if(resp.status===401){S.scope='';S.name='';S.csrf='';S.syncHeader();}
      }
      return S.guardResult(resp.status,data,ticket);
    } catch(_){if(epoch===S.epoch)S.block('unavailable');return {status:503,data:{ok:false,error:'network_unavailable'}};}
  };
  S.logout = async () => {
    S.save();
    try {
      const resp=await nativeFetch('/auth/logout',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':S.csrf},body:'{}'});
      if(!resp.ok){S.block(resp.status===503?'unavailable':'forbidden');return false;}
      S.block('login');S.scope='';S.name='';S.csrf='';S.syncHeader();return true;
    }catch(_){S.block('unavailable');return false;}
  };
  E.canWrite=() => {
    if(!S.isReady() || !E.state.ownerId || E.state.libraryCtx!==E.state.ownerId)return false;
    const library=(E.state.availableLibraries || []).find(lib=>lib.id===E.state.libraryCtx);
    return !!library && (library.can_manage_history===true || library.can_manage_experience===true);
  };
  root.addEventListener('focus',() => S.check());
  root.addEventListener('pageshow',event => {if(event.persisted && S.active!==false){S.block('checking');S.check();}});
  root.addEventListener('pagehide',S.save);
  root.setInterval(() => S.check(),5000);
  if(root.document){root.document.addEventListener('visibilitychange',() => {if(!root.document.hidden)S.check();});root.document.addEventListener('input',S.save);root.document.addEventListener('change',S.save);}
})(typeof window !== 'undefined' ? window : globalThis);
