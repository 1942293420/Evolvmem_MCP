// Synthetic local HTTP only: never reads production credentials or databases.
const http = require('node:http'), fs = require('node:fs'), path = require('node:path');
exports.startFixture = async function () {
  const staticDir = process.env.EVOLVMEM_STATIC_DIR || path.resolve(__dirname, '../../evolvmem/web_static');
  const modules = [
    {module_id:'mod_orders',project:'alpha',name:'订单处理',aliases:['订单'],description:'核对与处理订单',status:'active',revision:1},
    {module_id:'mod_suggested',project:'alpha',name:'售后处理',aliases:[],description:'待确认的业务划分',status:'pending',revision:1},
  ];
  const state = {modules, bindings:[], writes:[], gets:[], conflict:false, nextId:1, quote:'订单核对需要保留原始备注。<img src=x onerror="window.injected=1">'};
  const reply = (res, code, value) => {res.writeHead(code, {'Content-Type':'application/json;charset=utf-8'});res.end(JSON.stringify(value));};
  const server = http.createServer(async (req,res) => {
    const url = new URL(req.url,'http://127.0.0.1');
    if (url.pathname === '/') {
      res.writeHead(200, {'Content-Type':'text/html;charset=utf-8'});
      res.end(`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/refresh.css"><link rel="stylesheet" href="/project-library.css"><body data-design="signal"><main class="refresh-main"><div id="project"></div><div id="memory" class="panel"></div></main><script>window.EvolvAuth={canWrite:${url.searchParams.get('readonly') !== '1'},fetch:(...args)=>fetch(...args)};window.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));</script><script src="/project-archives.js"></script><script>window.handle=EvolvProjectArchives.render({mount:document.querySelector('#project'),esc,initialProject:'alpha'});if(window.EvolvMemModules)EvolvMemModules.mountMemory(document.querySelector('#memory'),1);</script>`);
      return;
    }
    if (['/project-archives.js','/refresh.css','/project-library.css'].includes(url.pathname)) {
      res.writeHead(200, {'Content-Type':url.pathname.endsWith('.js')?'text/javascript;charset=utf-8':'text/css;charset=utf-8'});
      res.end(fs.readFileSync(path.join(staticDir,url.pathname.slice(1))));return;
    }
    if (req.method === 'GET') state.gets.push(url.pathname);
    if (url.pathname === '/api/project-archives') {
      reply(res,200,{items:[{project:'alpha',display_name:'合成项目',aliases:[],status:'active',has_archive:true,event_count:1,historical_event_count:2,unsupported_event_count:1}],total:1,page:1,pages:1,stats:{registered:1,active:1,archived:0,with_archive:1,without_archive:0}});return;
    }
    if (url.pathname === '/api/project-archives/alpha') {
      reply(res,200,{project:'alpha',display_name:'合成项目',project_status:'active',has_archive:true,summary:'## 项目概述\n当前摘要仅采用有效来源。',revision:1,events_total:2,current_events_total:1,events_page:1,events_pages:1,events:[
        {event_id:1,title:'当前事件',event_type:'milestone',content:'当前内容',current_support:true,source_ref:'fixture:1'},
        {event_id:2,title:'旧事件 <script>window.injected=1</script>',event_type:'milestone',content:'保留的历史内容',current_support:false,source_diagnostics:[{valid:false,reason:'archive_unavailable',current_support:false}],source_ref:'fixture:2'}]});return;
    }
    if (req.method === 'GET' && url.pathname === '/api/project-modules') {reply(res,200,{project:'alpha',items:modules.filter(m=>m.project===url.searchParams.get('project'))});return;}
    if (req.method === 'GET' && url.pathname === '/api/project-modules/memory/1') {
      const current = modules.filter(m=>m.status==='active' && state.bindings.some(b=>b.module_id===m.module_id&&b.status==='active'));
      reply(res,200,{memory_id:1,title:'合成能力',status:'pending',modules:current.map(m=>({...m,not_recalled:true,binding_ids:state.bindings.filter(b=>b.module_id===m.module_id&&b.status==='active').map(b=>b.binding_id)})),bindings:state.bindings,sources:[
        {source_id:11,source_kind:'unit',valid:true,reason:'',projects:['alpha'],quote_text:state.quote},
        {source_id:12,source_kind:'unit',valid:false,reason:'archive_unavailable',projects:[],quote_text:''}]});return;
    }
    if (req.method === 'POST') {
      let raw='';for await (const chunk of req) raw+=chunk;
      const body=JSON.parse(raw);state.writes.push({path:url.pathname,body});
      if (state.conflict) {state.conflict=false;const binding=url.pathname.includes('/bindings/')?state.bindings.find(b=>url.pathname.includes('/'+b.binding_id+'/')):null;const target=binding||modules.find(m=>url.pathname.includes(m.module_id));if(target)target.revision++;reply(res,409,{ok:false,error:'revision_conflict'});return;}
      if (url.pathname === '/api/project-modules') {
        const item={...body,module_id:'mod_created'+state.nextId++,status:'active',revision:1};modules.push(item);reply(res,200,{item});return;
      }
      let match=url.pathname.match(/^\/api\/project-modules\/(mod_[a-z0-9]+)$/);
      if(match){const item=modules.find(m=>m.module_id===match[1]);if(!item||body.expected_revision!==item.revision){reply(res,409,{ok:false,error:'revision_conflict'});return;}
        if(body.action==='activate')item.status='active';else if(body.action==='deactivate')item.status='inactive';else for(const key of ['name','aliases','description'])if(key in body)item[key]=body[key];item.revision++;reply(res,200,{item});return;}
      match=url.pathname.match(/^\/api\/project-modules\/(mod_[a-z0-9]+)\/bindings$/);
      if(match){let binding=state.bindings.find(b=>b.module_id===match[1]&&b.capability_source_id===body.source_id);
        if(body.expected_revision!==(binding?.revision||0)){reply(res,409,{ok:false,error:'revision_conflict'});return;}
        if(!body.quote||!state.quote.includes(body.quote)){reply(res,400,{ok:false,error:'quote_not_verifiable'});return;}
        if(!binding){binding={binding_id:state.nextId++,module_id:match[1],capability_source_id:body.source_id,source_kind:'capability_source',source_key:'capability_source:'+body.source_id,decision:'manual',revision:0};state.bindings.push(binding);}binding.revision++;binding.status='active';reply(res,200,{binding});return;}
      match=url.pathname.match(/^\/api\/project-modules\/bindings\/(\d+)\/remove$/);
      if(match){const binding=state.bindings.find(b=>b.binding_id===Number(match[1]));if(body.expected_revision!==binding?.revision){reply(res,409,{ok:false,error:'revision_conflict'});return;}binding.status='inactive';binding.revision++;reply(res,200,{binding});return;}
    }
    reply(res,404,{ok:false,error:'not_found'});
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  return {state,base:`http://127.0.0.1:${server.address().port}`,close:()=>new Promise(resolve=>server.close(resolve))};
};
