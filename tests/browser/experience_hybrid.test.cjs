const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assets=path.resolve(__dirname,'../../evolvmem/web_static/v2');
function harness(fetch){
  const timers=new Map(),listeners={};let seq=0;
  const ctx=vm.createContext({console,fetch,location:{protocol:'http:',hash:''},
    setTimeout:fn=>{timers.set(++seq,fn);return seq;},clearTimeout:id=>timers.delete(id)});
  for(const [,file] of fs.readFileSync(path.join(assets,'index.html'),'utf8').matchAll(/<script src="([^"]+)"/g))
    if(!file.endsWith('sso-session.js'))vm.runInContext(fs.readFileSync(path.join(assets,file),'utf8'),ctx);
  const E=ctx.EvolvMemV2;E.state.apiMode='live';E.state.ownerId=E.state.libraryCtx='alice';E.canWrite=()=>true;
  E.state.expSubtab='list';E.renderAll=()=>{};ctx.document={addEventListener:(name,fn)=>listeners[name]=fn};E.bindEvents();
  return {E,ctx,listeners,timers,flush:async()=>{for(const [id,fn] of [...timers]){timers.delete(id);fn();}await new Promise(setImmediate);}};
}
const response=items=>({status:200,json:async()=>({ok:true,total:items.length,items})});
const note={id:'semantic',category:'业务经验',status:'active',origin:'manual',problem:'数据库连接等候过长',solution:'检查连接池',reusable_experience:'核实后采用'};
test('typing debounces a real experience API request and semantic result remains visible',async()=>{
  const calls=[];const h=harness(async url=>{calls.push(url);return response([note]);});
  h.listeners.input({target:{id:'exp-f-q',value:'database'}});
  h.listeners.input({target:{id:'exp-f-q',value:'database connection wait'}});
  assert.equal(calls.length,0);await h.flush();
  assert.equal(calls.length,1);assert.match(decodeURIComponent(calls[0]),/q=database connection wait/);
  assert.match(h.E.views.experiences(),/数据库连接等候过长/);
});
test('a later same-library query wins even if the old response or old error arrives last',async()=>{
  for(const oldStatus of [200,503]){
    const pending=[];const h=harness(url=>new Promise(resolve=>pending.push(resolve)));
    h.E.state.expFilters.q='old';const old=h.E.api.loadExperiences();
    h.E.state.expFilters.q='new';const current=h.E.api.loadExperiences();
    pending[1](response([{...note,id:'new'}]));await current;
    pending[0](oldStatus===200?response([{...note,id:'old'}]):{status:503,json:async()=>({error:'late_error'})});await old;
    assert.equal(h.E.state.experiences[0].id,'new');assert.equal(h.E.state.apiError,null);
  }
});
