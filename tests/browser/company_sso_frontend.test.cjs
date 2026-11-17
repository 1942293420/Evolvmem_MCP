const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const staged = path.join(__dirname, '..', 'staging', 'evolvmem', 'web_static', 'v2');
const root = fs.existsSync(staged) ? staged : path.resolve(__dirname, '../../evolvmem/web_static/v2');

function harness(actualState=false) {
  const store = new Map(), listeners = {}, calls = [], queue = [];
  const initial = () => ({apiMode:'sandbox',simMode:'normal',route:'overview',theme:'light',ownerId:'demo',libraryCtx:'demo',csrfToken:'',expFilters:{category:'all',q:''},histQuery:'',experiences:[{problem:'DEMO MUST NOT SHOW'}],proposals:[],historyProjects:[],controlUsers:[],availableLibraries:[]});
  const context = {URL,URLSearchParams,JSON,Promise,Map,Set,Date,console,location:{protocol:'http:',origin:'http://app.test',pathname:'/v2/',search:'?view=mine',hash:'#history',href:'http://app.test/v2/?view=mine#history'},sessionStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},setTimeout:fn=>{fn();return 1;},clearTimeout:()=>{},setInterval:()=>1,addEventListener:(n,fn)=>{listeners[n]=fn;},EvolvMemV2:{createInitialState:initial,state:initial(),renderAll(){},closeDrawer(){},closeModal(){}}};
  context.fetch = async (url,opts) => {calls.push({url,opts});const answer=queue.shift();if(answer instanceof Error)throw answer;return typeof answer==='function'?answer():{status:answer.status,ok:answer.status>=200&&answer.status<300,json:async()=>answer.body};};
  context.window=context;vm.createContext(context);
  if(actualState)vm.runInContext(fs.readFileSync(path.join(root,'js/state-and-data.js'),'utf8'),context);
  const source=path.join(root,'js/sso-session.js');assert.ok(fs.existsSync(source),'统一会话协调层尚未实现');
  vm.runInContext(fs.readFileSync(source,'utf8'),context);
  return {context,E:context.EvolvMemV2,queue,calls,listeners,store};
}
const ready = scope => ({status:200,body:{central_sso:true,authenticated:true,name:'测试用户',csrf_token:'csrf-test',user_scope:scope,native_status:'ready',central_console_url:'http://console.test'}});

test('unchanged identity polling updates credentials without replacing the reading view', async()=>{
  const h=harness();let renders=0;h.E.renderAll=()=>{renders++;};
  h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});
  h.E.session.dataReady=true;h.E.state.historyBlocks=[{id:'reading',transcript:['keep this text']}];
  const state=h.E.state,before=renders;
  h.queue.push({status:200,body:{...ready('scope-aaaa').body,name:'更新后的姓名',csrf_token:'fresh-csrf'}});
  await h.E.session.check({resume:false});
  assert.equal(renders,before,'background status checks must not rebuild the reading DOM');
  assert.equal(h.E.state,state);assert.equal(h.E.state.historyBlocks[0].id,'reading');
  assert.equal(h.E.session.dataReady,true);assert.equal(h.E.session.name,'更新后的姓名');
  assert.equal(h.E.state.csrfToken,'fresh-csrf');
});

test('polling still replaces private content when identity or permission changes', async()=>{
  const h=harness();let renders=0;h.E.renderAll=()=>{renders++;};
  h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});
  h.E.session.dataReady=true;h.E.state.historyBlocks=[{id:'private-A'}];const before=renders;
  h.queue.push(ready('scope-bbbb'));await h.E.session.check({resume:false});
  assert.ok(renders>before);assert.equal(h.E.state.historyBlocks?.length||0,0);assert.equal(h.E.session.dataReady,false);
  const changed=renders;h.E.state.historyBlocks=[{id:'private-B'}];
  h.queue.push({status:403,body:{authenticated:false}});await h.E.session.check({resume:false});
  assert.ok(renders>changed);assert.equal(h.E.state.historyBlocks?.length||0,0);assert.equal(h.E.session.isReady(),false);
});

test('online startup removes demo payload before any data request',()=>{
  const {E}=harness();assert.equal(E.state.apiMode,'live');assert.equal(E.state.experiences.length,0);assert.equal(E.session.isReady(),false);
});
test('login link preserves complete local route and rejects unsafe next',()=>{
  const {E}=harness();assert.equal(E.session.loginURL(),'/auth/login?next=%2Fv2%2F%3Fview%3Dmine%23history');
  for(const value of ['//evil.test/','https://evil.test','/\\evil.test','/auth/login','/x\nLocation: evil'])assert.equal(E.session.safeNext(value),'/');
});
test('pending native permission shows clear state and does not request business data',async()=>{
  const h=harness();h.queue.push({status:200,body:{...ready('scope-aaaa').body,native_status:'pending'}});await h.E.session.check({resume:false});
  assert.equal(h.E.session.status,'pending');assert.match(h.E.session.panel(),/尚未分配 EvolvMem 内部权限/);assert.equal(h.calls.length,1);assert.equal(h.E.state.experiences.length,0);
});
test('same user retains UI filters while a different user cannot inherit data or filters',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.expFilters.q='用户甲筛选';h.E.state.experiences=[{problem:'甲的原文'}];
  h.queue.push({status:503,body:{error:'sso_unavailable'}});await h.E.session.check({resume:false});assert.equal(h.E.state.experiences.length,0);
  h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});assert.equal(h.E.state.expFilters.q,'用户甲筛选');assert.equal(h.E.state.experiences.length,0);
  h.queue.push(ready('scope-bbbb'));await h.E.session.check({resume:false});assert.equal(h.E.state.expFilters.q,'');assert.equal(h.E.state.experiences.length,0);
});
test('late response from previous identity or library is discarded',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});let finish;
  h.queue.push(()=>new Promise(resolve=>{finish=resolve;}));const req=h.E.session.request('GET','/api/console-v2/experiences');
  h.queue.push(ready('scope-bbbb'));await h.E.session.check({resume:false});finish({status:200,json:async()=>({ok:true,items:[{problem:'private-old'}]})});
  const result=await req;assert.equal(result.status,409);assert.equal(result.data.items,undefined);
  h.queue.push(()=>new Promise(resolve=>{finish=resolve;}));const libReq=h.E.session.request('GET','/api/console-v2/history');h.E.state.libraryCtx='new-library';finish({status:200,json:async()=>({ok:true,items:['old-library']})});assert.equal((await libReq).status,409);
});
test('401, 403 and 503 immediately remove private state',async()=>{
  for(const status of [401,403,503]){const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.experiences=[{problem:'private'}];h.queue.push({status,body:{error:status===403?'account_disabled':'unavailable'}});await h.E.session.request('GET','/api/console-v2/overview');assert.equal(h.E.state.experiences.length,0);assert.equal(h.E.session.isReady(),false);}
});
test('logout sends CSRF, clears private data and does not automatically log in again',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.experiences=[{problem:'private'}];h.queue.push({status:200,body:{authenticated:false}});await h.E.session.logout();
  const call=h.calls.at(-1);assert.equal(call.url,'/auth/logout');assert.equal(call.opts.method,'POST');assert.equal(call.opts.headers['X-CSRF-Token'],'csrf-test');assert.equal(h.E.session.status,'login');assert.equal(h.E.state.experiences.length,0);assert.equal(h.context.location.hash,'#history');
});
test('v2 bootstrap waits for central status, retains native identity and allowed library',async()=>{
  const h=harness();vm.runInContext(fs.readFileSync(path.join(root,'js/api-adapter.js'),'utf8'),h.context);
  h.E.api.loadOverview=async()=>{};h.E.api.loadTab=async()=>{};
  h.queue.push(ready('scope-aaaa'),{status:200,body:{ok:true,current_user:{id:'native-test',display_name:'测试用户',is_admin:false},libraries:[{id:'native-test',can_manage_history:true,can_manage_experience:true}]}});
  await h.E.api.bootstrap();assert.equal(h.calls[0].url,'/api/auth/me');assert.equal(h.E.state.currentUser.id,'native-test');assert.equal(h.E.session.dataReady,true);assert.equal(h.E.state.libraryCtx,'native-test');
});
test('server without central SSO marker retains original standalone mode',async()=>{
  const h=harness();h.queue.push({status:200,body:{enabled:true,authenticated:true,name:'独立模式',csrf_token:'legacy-csrf'}});assert.equal(await h.E.session.check({resume:false}),true);assert.equal(h.E.session,undefined);assert.equal(h.E.state.apiMode,'sandbox');assert.equal(h.E.state.experiences[0].problem,'DEMO MUST NOT SHOW');
});
test('v2 bootstrap has no business calls when native account is pending',async()=>{
  const h=harness();vm.runInContext(fs.readFileSync(path.join(root,'js/api-adapter.js'),'utf8'),h.context);
  h.queue.push({status:200,body:{...ready('scope-aaaa').body,native_status:'pending'}});await h.E.api.bootstrap();assert.equal(h.calls.length,1);assert.equal(h.E.session.status,'pending');assert.equal(h.E.state.experiences.length,0);
});
test('actual entry loads the gate before API and offers named identity controls',()=>{
  const html=fs.readFileSync(path.join(root,'index.html'),'utf8');assert.ok(html.indexOf('js/sso-session.js')<html.indexOf('js/api-adapter.js'));for(const id of ['auth-name','auth-login','auth-logout','auth-console'])assert.match(html,new RegExp('id="'+id+'"'));
});
test('library switch removes old records and restores only that library UI',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.ownerId='native-test';h.E.state.libraryCtx='native-test';h.E.state.availableLibraries=[{id:'native-test',can_manage_experience:true},{id:'peer-test',can_manage_experience:false}];
  h.E.state.experiences=[{problem:'self-private'}];h.E.state.expFilters.q='self-query';h.E.session.switchLibrary('peer-test');assert.equal(h.E.state.experiences.length,0);assert.equal(h.E.state.expFilters.q,'');assert.equal(h.E.canWrite(),false);
  h.E.state.expFilters.q='peer-query';h.E.session.switchLibrary('native-test');assert.equal(h.E.state.expFilters.q,'self-query');assert.equal(h.E.state.experiences.length,0);assert.equal(h.E.canWrite(),true);
  h.E.session.switchLibrary('unavailable-library');assert.equal(h.E.state.libraryCtx,'native-test');
});
test('real v2 views tolerate cleared private payload and show the real SSO boundary',async()=>{
  const h=harness(true);for(const name of ['history-time','history-content','history-project-picker','render-overview-and-core','render-interceptions','render-pipeline-and-history','render-evolution-and-control','app-shell'])vm.runInContext(fs.readFileSync(path.join(root,'js/'+name+'.js'),'utf8'),h.context);
  h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.session.dataReady=true;h.E.state.ownerId='native-test';h.E.state.libraryCtx='native-test';h.E.state.availableLibraries=[{id:'native-test',can_manage_experience:true,can_manage_history:true}];
  for(const route of ['overview','review-inbox','experiences','history','evolution','skills','control']){h.E.state.route=route;assert.equal(typeof h.E.renderMain(),'string',route);}
  h.E.state.route='overview';assert.match(h.E.renderMain(),/总控台确认当前用户是否获准进入 EvolvMem/);
});
test('initial direct hash opens that route after identity verification',async()=>{
  const h=harness(true);h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});assert.equal(h.E.state.route,'history');
});
test('status response started before logout cannot restore the revoked user',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});let finish;h.queue.push(()=>new Promise(resolve=>{finish=resolve;}));const check=h.E.session.check({resume:false});h.queue.push({status:200,body:{authenticated:false}});await h.E.session.logout();finish({status:200,json:async()=>ready('scope-aaaa').body});await check;assert.equal(h.E.session.status,'login');assert.equal(h.E.session.scope,'');
});
test('DOM change fired during private cleanup cannot overwrite the saved UI',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.expFilters.q='保留筛选';h.context.document={getElementById:id=>id==='modal-body'?{replaceChildren(){h.E.session.save();}}:null};h.E.session.block('unavailable');h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});assert.equal(h.E.state.expFilters.q,'保留筛选');
});
test('central legacy-owner flag does not remove native member management permissions',async()=>{
  const h=harness();h.queue.push({status:200,body:{...ready('scope-aaaa').body,can_write:false}});await h.E.session.check({resume:false});h.E.state.ownerId='native-member';h.E.state.libraryCtx='native-member';h.E.state.currentUser={id:'native-member',is_admin:false};h.E.state.availableLibraries=[{id:'native-member',can_manage_experience:true,can_manage_history:true}];assert.equal(h.E.canWrite(),true);
});
test('decoded result becomes unusable after its identity changes',async()=>{
  const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.queue.push({status:200,body:{ok:true,items:['A private']}});const response=await h.E.session.request('GET','/api/console-v2/experiences');h.queue.push(ready('scope-bbbb'));await h.E.session.check({resume:false});assert.equal(response.status,409);assert.equal(response.data.items,undefined);
});
test('API consumers reject identity changes at each microtask boundary',async()=>{
  for(const phase of ['bootstrap','loadExperiences','loadInterceptions','loadInbox'])for(let depth=1;depth<=10;depth++){
    const h=harness();vm.runInContext(fs.readFileSync(path.join(root,'js/api-adapter.js'),'utf8'),h.context);h.E.api.loadOverview=async()=>{};h.E.api.loadTab=async()=>{};h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});h.E.state.ownerId='native-A';h.E.state.libraryCtx='native-A';h.E.state.availableLibraries=[{id:'native-A',can_manage_experience:true}];h.E.state.inboxTrack='live';h.E.state.intFilters={layer:'all',track:'all',category:'all',q:''};let finishA,finishB;
    h.queue.push(()=>new Promise(resolve=>{finishA=resolve;}));const loading=phase==='bootstrap'?h.E.api.bootstrap({statusKnown:true}):h.E.api[phase]();h.queue.push(()=>({status:200,json:()=>new Promise(resolve=>{finishB=resolve;})}));const checking=h.E.session.check({resume:false});for(let j=0;j<10&&!finishB;j++)await Promise.resolve();
    const change=()=>finishB(ready('scope-bbbb').body);const after=n=>queueMicrotask(()=>n>1?after(n-1):change());finishA({status:200,json:()=>Promise.resolve({ok:true,current_user:{id:'native-A'},libraries:[{id:'native-A',can_manage_experience:true}],items:[{id:'A-private',problem:'A PRIVATE CONTENT',solution:'A solution',source_track:'live'}]})});after(depth);await Promise.all([loading,checking]);
    assert.equal(h.E.session.scope,'scope-bbbb');assert.notEqual(h.E.state.currentUser?.id,'native-A',phase+' depth '+depth);assert.equal(h.E.session.dataReady,false,phase+' dataReady depth '+depth);assert.equal(h.E.state.experiences.length,0,phase+' experiences depth '+depth);assert.equal(h.E.state.interceptions?.length||0,0,phase+' interceptions depth '+depth);assert.equal(h.E.state.proposals.length,0,phase+' inbox depth '+depth);
  }
});


test('experience encoder 503 retains a ready session only after verifying live identity',async()=>{
 const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});
 h.E.session.dataReady=true;const current=h.E.state;h.E.state.experiences=[{problem:'private'}];
 h.queue.push({status:503,body:{ok:false,error:'experience_embedding_unavailable'}},ready('scope-aaaa'));
 const res=await h.E.session.request('POST','/api/console-v2/experiences/write',{action:'update'});
 assert.equal(res.status,503);assert.equal(h.E.session.isReady(),true);assert.equal(h.E.state,current);
 assert.equal(h.E.state.experiences[0].problem,'private');
 assert.equal(h.calls.at(-1).url,'/auth/ops/status');
});

test('an expired live identity still clears private data after an encoder failure',async()=>{
 const h=harness();h.queue.push(ready('scope-aaaa'));await h.E.session.check({resume:false});
 h.E.state.experiences=[{problem:'private'}];
 h.queue.push({status:503,body:{ok:false,error:'experience_embedding_unavailable'}},
              {status:401,body:{authenticated:false}});
 await h.E.session.request('POST','/api/console-v2/experiences/write',{action:'update'});
 assert.equal(h.E.session.isReady(),false);assert.equal(h.E.state.experiences.length,0);
});
