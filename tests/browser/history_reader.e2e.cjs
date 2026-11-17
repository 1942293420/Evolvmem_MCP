// Synthetic HTTP only. Mutations below affect the in-memory fixture, never production.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const assets=process.env.CONSOLE_V2_ROOT || path.resolve(__dirname,'../../evolvmem/web_static/v2');
const out=process.env.EVOLVMEM_SCREENSHOTS || '/tmp/history-reader-browser';
const requests=[],posts=[],errors=[],checks=[];let deleted=false,failPage=false;
const blocks=Array.from({length:45},(_,i)=>({block_id:'block-'+i,title:'&#x20;长会话阅读测试 '+i,projects:['folder'],message_count:116,turn_count:29,observed_first_at:'2026-11-17T01:00:00Z',observed_last_at:'2026-11-17T02:00:00Z',workspace_path:'C:\\Synthetic\\Reader'}));
const messages=Array.from({length:116},(_,i)=>({ordinal:i,role:i<2?'user':i<4?'assistant':i%2?'assistant':'user',recorded_at:'2026-11-17T02:00:00Z',content:i===1?'<skill>\n'+'Injected instruction\n'.repeat(100)+'</skill>':i===2?'## 正文标题\n\n第一段\n第二行\n\n```js\nconst value = 1;\n```':('第 '+i+' 条消息\n保留段落换行。\n'+'正常对话内容 '.repeat(12))}));
const server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://fixture');
  if(url.pathname.startsWith('/api/') || url.pathname==='/auth/ops/status') {
    let data={ok:true};
    if(req.method==='POST'){let text='';for await(const chunk of req)text+=chunk;const body=JSON.parse(text || '{}');posts.push(body);if(body.action==='delete')deleted=true;else if(body.action==='restore')deleted=false;}
    else if(url.pathname==='/api/auth/me' || url.pathname==='/auth/ops/status')data={central_sso:true,authenticated:true,user_scope:'reader-fixture',native_status:'ready',name:'隔离验收用户',csrf_token:'fixture'};
    else if(url.pathname.endsWith('/me'))data={ok:true,current_user:{id:'owner'},libraries:[{id:'owner',display_name:'本人测试库',can_manage_history:true,can_manage_experience:true},{id:'peer',display_name:'授权只读库',can_manage_history:false,can_manage_experience:false}]};
    else if(url.pathname.endsWith('/history')) {
      requests.push(url.search);const view=url.searchParams.get('view');
      if(view==='projects')data={ok:true,projects:deleted?[]:[{project:'folder',display_name:'测试文件夹',workspace_path:'C:\\Synthetic\\Reader',block_count:45}],unassigned_count:161};
      if(view==='trash')data.items=deleted?[{kind:'workspace',key:'folder',title:'测试文件夹',block_count:45}]:[];
      if(view==='project' || view==='unassigned')data.blocks=blocks;
      if(view==='search')data.items=blocks.slice(0,2);
      if(view==='turns'){const offset=Number(url.searchParams.get('offset') || 0),limit=Number(url.searchParams.get('limit') || 40),end=Math.min(116,offset+limit);if(failPage && offset>0){res.statusCode=503;data={ok:false,error:'temporary'};}else data={ok:true,messages:messages.slice(offset,end),has_more:end<116,next_offset:end};}
    }
    res.setHeader('Content-Type','application/json');return res.end(JSON.stringify(data));
  }
  const file=path.resolve(assets,url.pathname==='/v2/'?'index.html':url.pathname.replace(/^\/v2\//,''));
  if(!file.startsWith(path.resolve(assets)+path.sep)||!fs.existsSync(file)){res.statusCode=404;return res.end();}
  res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));
});
(async()=>{
 fs.mkdirSync(out,{recursive:true});await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--no-proxy-server']});
 const page=await browser.newPage({viewport:{width:1280,height:800}});page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
 const check=(name,pass,detail)=>{checks.push({name,pass,detail});assert(pass,name+': '+JSON.stringify(detail));};
 try {
  await page.goto(`http://127.0.0.1:${server.address().port}/v2/#history`);await page.waitForSelector('.chat-bubble');
  const roleStyles=await page.locator('.chat-bubble').evaluateAll(els=>els.slice(0,4).map(e=>e.classList.contains('assistant')));
  check('consecutive roles',[false,false,true,true].every((v,i)=>v===roleStyles[i]),roleStyles);
  check('injection collapsed',await page.locator('.history-injection:not([open])').count()===1);
  check('no entity noise',!(await page.locator('.prose-lead').innerText()).includes('&#x20;'));
  check('newlines preserved',await page.locator('.history-prose').first().evaluate(e=>getComputedStyle(e).whiteSpace)==='pre-wrap');
  const desktop=await page.evaluate(()=>{const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return {top:r.top,bottom:r.bottom,height:r.height,width:r.width};};return {list:rect('.list-panel'),reader:rect('.history-reader'),pageHeight:document.documentElement.scrollHeight,innerHeight,navClipped:document.querySelector('#top-nav').scrollWidth>document.querySelector('#top-nav').clientWidth+1};});
  check('equal independent columns and compact first screen',Math.abs(desktop.list.height-desktop.reader.height)<2 && desktop.reader.top<250 && desktop.pageHeight<=801 && !desktop.navClipped,desktop);
  const heights=await page.locator('#hist-proj-toggle,#hist-q,#hist-search-range,.cat-pill').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().height));check('uniform filter heights',Math.max(...heights)-Math.min(...heights)<1,heights);
  await page.screenshot({path:path.join(out,'01-laptop-1280.png'),fullPage:true});
  await page.locator('#hist-proj-toggle').click();const popup=await page.locator('#hist-proj-search,#hist-proj-clear').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().height));check('picker clear aligned',Math.abs(popup[0]-popup[1])<1,popup);await page.locator('#hist-proj-search').press('Escape');
  await page.evaluate(()=>{document.querySelector('#history-reader-scroll').scrollTop=700;document.querySelector('#history-list-scroll').scrollTop=300;});
  const before=await page.evaluate(()=>({reader:document.querySelector('#history-reader-scroll').scrollTop,list:document.querySelector('#history-list-scroll').scrollTop}));
  await page.evaluate(()=>EvolvMemV2.renderAll());const after=await page.evaluate(()=>({reader:document.querySelector('#history-reader-scroll').scrollTop,list:document.querySelector('#history-list-scroll').scrollTop,y:scrollY}));
  check('redraw keeps both scroll positions',before.reader===after.reader && before.list===after.list && after.y===0,{before,after});
  failPage=true;await page.locator('[data-act="history-load-more"]').click();await page.waitForSelector('.history-load-error');check('failed page preserves messages',await page.locator('.chat-bubble').count()===40);failPage=false;
  await page.locator('[data-act="history-load-more"]').click();await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===80);await page.locator('[data-act="history-load-more"]').click();await page.waitForFunction(()=>document.querySelectorAll('.chat-bubble').length===116);
  check('last page reachable',await page.locator('[data-message-index="115"]').count()===1 && await page.locator('[data-act="history-load-more"]').count()===0);
  await page.locator('#hist-q').fill('正文');await page.waitForFunction(()=>EvolvMemV2.state.historyBlocks.length===2);check('folder remains during search',await page.locator('#hist-proj-toggle').isVisible());
  check('folder-scoped request',requests.some(q=>new URLSearchParams(q).get('view')==='search' && new URLSearchParams(q).get('project')==='folder'));
  await page.selectOption('#hist-search-range','all');await page.waitForFunction(()=>EvolvMemV2.state.historyBlocks.length===2);check('all-folder explicit request',requests.some(q=>new URLSearchParams(q).get('view')==='search' && !new URLSearchParams(q).has('project')));
  await page.locator('#hist-q').fill('');await page.waitForFunction(()=>EvolvMemV2.state.historyBlocks.length===45);
  await page.locator('[data-act="toggle-proj-del"]').click();check('delete needs confirmation',posts.length===0 && (await page.locator('#modal-body').innerText()).includes('45 个会话片段'));
  await page.locator('#modal-body [data-act="close-modal"]').click();check('cancel leaves data intact',posts.length===0);
  await page.locator('[data-act="history-source-info"]').click();check('source shown in compact modal',await page.locator('#modal-backdrop.open').count()===1 && (await page.locator('#modal-body').innerText()).includes('pg:block-0'));await page.locator('#modal-backdrop [data-act="close-modal"]').first().click();
  for(const width of [1440,1024,390]){await page.setViewportSize({width,height:900});check('no horizontal overflow '+width,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await page.screenshot({path:path.join(out,'layout-'+width+'.png'),fullPage:true});}
  await page.setViewportSize({width:1440,height:900});await page.locator('[data-sub="capture"]').click();const widths=await page.locator('.history-actions .btn').evaluateAll(es=>es.map(e=>e.getBoundingClientRect().width));check('capture buttons size to labels',widths.every(w=>w<400),widths);await page.screenshot({path:path.join(out,'capture.png'),fullPage:true});
  await page.locator('#top-nav [data-route="overview"]').click();await page.waitForSelector('[aria-label="会话阅读与文件夹管理说明"]');
  check('overview explains current history flow',(await page.locator('#workspace-main').innerText()).includes('确认移入回收站'));
  await page.screenshot({path:path.join(out,'overview-flow.png'),fullPage:true});
  check('no browser errors',errors.length===0,errors);check('no production mutations',posts.length===0,posts);
 } catch(e){await page.screenshot({path:path.join(out,'failure.png'),fullPage:true});throw e;}
 finally {fs.writeFileSync(path.join(out,'results.json'),JSON.stringify({checks,errors,posts},null,2));console.log(JSON.stringify({checks,errors,posts}));await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
