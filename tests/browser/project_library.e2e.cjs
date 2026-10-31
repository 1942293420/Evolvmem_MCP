// Real Chromium, real DOM and HTTP against a metadata-only synthetic fixture.
// Never connects to production or reads its database/credentials/transcripts.
const assert=require('node:assert/strict'),http=require('node:http'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const source=process.env.EVOLVMEM_SOURCE_DIR||path.resolve(__dirname,'../..');
const output=process.env.EVOLVMEM_SCREENSHOT_DIR;
const selected=process.argv.includes('--race')?'race':'all';
const longSummary='## 项目概述\n'+('合成完整摘要'.repeat(85))+'末尾完整标记\n'+JSON.stringify({fixture:'<img src=x onerror="alert(1)">',details:'结构化合成证据'});
const projects=[
 {project:'alpha',display_name:'甲项目',aliases:['别名甲'],status:'active',has_archive:true,event_count:203,archive_status:'active',updated_at:'2026-10-30 12:00:00',has_legacy_document:true},
 {project:'missing',display_name:'已登记待存档',aliases:[],status:'active',has_archive:false,event_count:0,archive_status:null,updated_at:'2026-10-29 12:00:00',has_legacy_document:false},
 {project:'beta',display_name:'乙项目',aliases:[],status:'active',has_archive:true,event_count:1,archive_status:'active',updated_at:'2026-10-28 12:00:00',has_legacy_document:false},
 ...Array.from({length:42},(_,i)=>({project:`sample-${i}`,display_name:`样例项目 ${i}`,aliases:[],status:'active',has_archive:true,event_count:1,archive_status:'active',updated_at:'2026-10-01 12:00:00',has_legacy_document:false})),
 {project:'archived',display_name:'已归档旧项目',aliases:['历史别名'],status:'archived',has_archive:true,event_count:1,archive_status:'active',updated_at:'2026-09-01 12:00:00',has_legacy_document:false},
];
let listFailure=false,holdAlpha=false,releaseAlpha=null,catalogRequests=0;
let eventsWithoutSummary=false,holdList=false,releaseList=null;
const html=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/refresh.css"><link rel="stylesheet" href="/project-library.css"><body data-design="signal"><main class="refresh-main"><div id="mount"></div></main><script src="/project-archives.js"></script><script>
window.EvolvAuth={fetch:(...args)=>fetch(...args)};
window.esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
window.renderArchives=async(options={})=>{window.handle=await EvolvProjectArchives.render({mount:document.querySelector('#mount'),esc,...options});};renderArchives().then(()=>handle.showCatalog?.());
</script>`;
function reply(res,status,data){if(!res.destroyed){res.writeHead(status,{'Content-Type':'application/json;charset=utf-8'});res.end(JSON.stringify(data));}}
const server=http.createServer(async(req,res)=>{
 const url=new URL(req.url,'http://127.0.0.1');
 if(url.pathname==='/'){res.writeHead(200,{'Content-Type':'text/html;charset=utf-8'});res.end(html);return;}
 if(['/project-archives.js','/refresh.css','/project-library.css'].includes(url.pathname)){
  const file=path.join(source,'evolvmem/web_static',url.pathname.slice(1));
  if(!fs.existsSync(file)){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'Content-Type':url.pathname.endsWith('.js')?'text/javascript;charset=utf-8':'text/css;charset=utf-8'});res.end(fs.readFileSync(file));return;
 }
 if(url.pathname==='/api/project-archives'){
  catalogRequests++;
  if(holdList)await new Promise(resolve=>{releaseList=resolve;});
  if(listFailure){reply(res,503,{ok:false,error:'service_busy'});return;}
  const q=(url.searchParams.get('q')||'').trim(),status=url.searchParams.get('status')||'active',archive=url.searchParams.get('archive')||'all';
  let rows=projects.filter(p=>(status==='all'||p.status===status)&&(archive==='all'||p.has_archive===(archive==='available'))&&(!q||[p.project,p.display_name,...p.aliases].join(' ').includes(q)));
  if(url.searchParams.get('sort')==='name')rows.sort((a,b)=>a.display_name.localeCompare(b.display_name));
  const page=Number(url.searchParams.get('page')||1),page_size=Number(url.searchParams.get('page_size')||20);
  reply(res,200,{items:rows.slice((page-1)*page_size,page*page_size),total:rows.length,page,page_size,pages:Math.ceil(rows.length/page_size),stats:{registered:46,active:45,archived:1,with_archive:45,without_archive:1,active_with_archive:44,active_without_archive:1}});return;
 }
 if(url.pathname.startsWith('/api/project-archives/')){
  const id=decodeURIComponent(url.pathname.split('/').pop()),p=id==='legacy/&"<项目>'?{...projects[2],project:id}:projects.find(row=>row.project===id||row.display_name===id||row.aliases.includes(id));
  if(!p){reply(res,404,{ok:false,error:'project_not_registered'});return;}
  if(id==='alpha'&&holdAlpha)await new Promise(resolve=>{releaseAlpha=resolve;});
  const events_page=Number(url.searchParams.get('events_page')||1),events_page_size=Number(url.searchParams.get('events_page_size')||100);
  const count=p.project==='missing'&&eventsWithoutSummary?1:p.event_count;
  const events=Array.from({length:count},(_,i)=>({event_id:i+1,title:`${p.display_name}事件 ${i+1}`,event_type:'milestone',content:`合成证据 ${i+1}`,source_ref:`archive:fixture#${i+1}`,occurred_at:i===1?null:'2026-07-20',occurred_at_precision:i===1?'unknown':'date',recorded_at:'2026-10-28 12:00:00'}));
  reply(res,200,{project:p.project,display_name:p.display_name,project_status:p.status,has_archive:p.has_archive,summary:p.has_archive?(p.project==='alpha'?longSummary:'## 项目概述\n合成项目摘要'):'',started_at:p.has_archive?'2026-07-20':null,development_days:p.has_archive?103:null,revision:2,events:events.slice((events_page-1)*events_page_size,events_page*events_page_size),events_total:count,events_page,events_page_size,events_pages:Math.ceil(count/events_page_size)});return;
 }
 res.writeHead(404);res.end();
});
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',error=>errors.push(error.message));page.setDefaultTimeout(4000);
 try {
  await page.goto(base);
  if(selected==='all'){
   await page.waitForSelector('[data-project-card]');
   assert.equal(catalogRequests,1,'Initial showCatalog must reuse its in-flight directory read');
   assert.equal(await page.locator('[data-project-card]').count(),20);
   assert.match(await page.locator('[data-project-count]').innerText(),/45/);
   await page.locator('[data-catalog-page="next"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-catalog-page-status]')?.textContent.includes('2'));
   const secondPage=await page.locator('[data-project-card]').first().getAttribute('data-project-card');
   await page.locator('[data-project-card]').first().focus();await page.keyboard.press('Enter');
   await page.waitForSelector('[data-archive-summary]');
   await page.locator('[data-project-back]').click();
   assert.match(await page.locator('[data-catalog-page-status]').innerText(),/2/);
   assert.equal(await page.locator(':focus').getAttribute('data-project-card'),secondPage,'Back must restore the opened project focus');
   await page.locator('#project-archive-form [name=project]').fill('别名甲');
   await page.locator('#project-archive-form button[type=submit]').click();
   await page.waitForFunction(()=>document.querySelectorAll('[data-project-card]').length===1);
   await page.locator('[data-project-card="alpha"]').click();
   await page.waitForSelector('[data-project-event="1"]');
   assert.match(await page.locator('[data-events-total]').innerText(),/203/);
   assert.match(await page.locator('[data-project-event="1"]').innerText(),/仅到日，时区未确认/);
   assert.match(await page.locator('[data-project-event="2"]').innerText(),/发生时间未确认.*入库时间/);
   assert.equal(await page.locator('[data-project-full-summary]').isVisible(),false);
   await page.locator('[data-project-full-summary-toggle]').click();
   assert.equal(await page.locator('[data-project-full-summary]').innerText(),longSummary,'Truncated lines and structured records must remain available in full');
   assert.equal(await page.locator('[data-project-full-summary] img').count(),0,'Full summary must be escaped text');
   await page.locator('[data-project-event="1"]').focus();await page.keyboard.press('Enter');
   assert.match(await page.locator('[data-project-event="1"]').innerText(),/archive:fixture#1/);
   await page.locator('[data-events-page="next"]').click();
   await page.waitForSelector('[data-project-event="101"]');
   assert.equal(await page.locator('[data-project-event="1"]').count(),0);
   await page.locator('[data-project-back]').click();
   assert.equal(await page.locator('#project-archive-form [name=project]').inputValue(),'别名甲');
   await page.locator('#project-archive-form [name=project]').fill('');
   await page.locator('[data-project-status]').selectOption('archived');
   await page.waitForSelector('[data-project-card="archived"]');
   assert.equal(await page.locator('[data-project-card]').count(),1);
   await page.locator('[data-project-status]').selectOption('all');
   await page.locator('[data-project-archive-filter]').selectOption('missing');
   await page.waitForSelector('[data-project-card="missing"]');
   await page.locator('[data-project-card="missing"]').click();
   await page.waitForSelector('[data-archive-empty]');
   assert.match(await page.locator('[data-archive-empty]').innerText(),/已登记|尚无|暂无/);
   assert.equal(await page.locator('[role=alert]').count(),0,'Registered empty project is not a failed read');
   await page.locator('[data-project-back]').click();
   await page.locator('#project-archive-form [name=project]').fill('不存在的项目');
   await page.locator('#project-archive-form button[type=submit]').click();
   await page.waitForSelector('[data-catalog-empty]');
   assert.equal(await page.locator('[role=alert]').count(),0,'Search empty and network failure must be distinct');
   listFailure=true;await page.locator('[data-clear-project-filters]').click();
   await page.waitForSelector('[data-catalog-error]');
   assert.ok(await page.locator('[data-catalog-retry]').isVisible());
   listFailure=false;await page.locator('[data-catalog-retry]').click();await page.waitForSelector('[data-project-card]');
   for(const width of [1440,768,390]){
    await page.setViewportSize({width,height:1000});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`Directory overflow at ${width}`);
    if(output){fs.mkdirSync(output,{recursive:true});await page.screenshot({path:path.join(output,`project-library-${width}.png`),fullPage:true});}
   }
   await page.evaluate(()=>window.handle.openProject('alpha'));await page.waitForSelector('[data-archive-summary]');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Detail overflow at 390');
   if(output)await page.screenshot({path:path.join(output,'project-library-detail-390.png'),fullPage:true});
  }
  if(selected==='all'){
   const before=catalogRequests;
   await page.evaluate(async()=>{window.selection=[];await window.renderArchives({initialProject:'beta',onProject:project=>selection.push(project)});});
   await page.waitForFunction(()=>document.querySelector('[data-archive-summary] h2')?.textContent==='乙项目');
   assert.equal(catalogRequests,before,'A direct project link must not also fetch the directory');
   await page.locator('[data-project-back]').click();await page.waitForSelector('[data-project-card]');
   assert.equal(await page.evaluate(()=>selection.at(-1)),'','Returning to directory must clear the selected project route');
   await page.locator('[data-project-card="alpha"]').click();await page.waitForSelector('[data-archive-summary]');
   assert.equal(await page.evaluate(()=>selection.at(-1)),'alpha','Selecting a project must notify the route owner');
   await page.evaluate(()=>{handle.suspend();const node=document.querySelector('[data-project-library]');node.remove();document.querySelector('#mount').append(node);handle.resume('');});
   await page.waitForSelector('[data-project-card]');
   assert.equal(await page.locator('[data-project-detail]').isVisible(),false,'Resuming to main navigation must show the directory');
   eventsWithoutSummary=true;
   await page.evaluate(()=>handle.openProject('missing'));await page.waitForSelector('[data-archive-empty]');
   assert.equal(await page.locator('[data-project-event]').count(),1,'Missing summary must not hide existing project events');
   assert.doesNotMatch(await page.locator('[data-archive-empty]').innerText(),/没有.*历史事件/);
   assert.equal(await page.locator('[data-project-legacy]').getAttribute('href'),'#knowledge/library?project=missing');
   await page.locator('[data-project-back]').click();
   // A canceled filter read must restart on resume rather than label old rows as new filters.
   holdList=true;await page.locator('#project-archive-form [name=project]').fill('乙项目');
   await page.locator('#project-archive-form button[type=submit]').click();
   for(let i=0;i<40&&!releaseList;i++)await wait(25);assert.ok(releaseList);
   await page.evaluate(()=>handle.suspend());holdList=false;releaseList();releaseList=null;
   await page.evaluate(()=>handle.resume(''));
   await page.waitForFunction(()=>document.querySelectorAll('[data-project-card]').length===1&&document.querySelector('[data-project-card="beta"]'));
   const unusual='legacy/&"<项目>';
   await page.evaluate(project=>handle.openProject(project),unusual);await page.waitForSelector('[data-archive-summary]');
   const legacyHref='#knowledge/library?project='+encodeURIComponent(unusual);
   assert.equal(await page.locator('[data-project-legacy]').getAttribute('href'),legacyHref,'Legacy link must encode the canonical identifier without introducing markup');
   await page.locator('[data-project-legacy]').click();assert.equal(new URL(page.url()).hash,legacyHref);
  }
  // Re-render the same mount while A is in flight: late A must not replace B.
  holdAlpha=true;
  await page.evaluate(async()=>{await window.renderArchives();if(window.handle?.openProject)void window.handle.openProject('alpha');else{const f=document.querySelector('form');f.elements.project.value='alpha';f.requestSubmit();}});
  for(let i=0;i<40&&!releaseAlpha;i++)await wait(25);assert.ok(releaseAlpha,'A request was not held');
  await page.evaluate(async()=>{await window.renderArchives();if(window.handle?.openProject)void window.handle.openProject('beta');else{const f=document.querySelector('form');f.elements.project.value='beta';f.requestSubmit();}});
  await page.waitForFunction(()=>document.querySelector('[data-archive-summary] h2')?.textContent==='乙项目');
  holdAlpha=false;releaseAlpha();releaseAlpha=null;await wait(200);
  assert.equal(await page.locator('[data-archive-summary] h2').innerText(),'乙项目','Late A must not replace current B');
  // A detached route must not mutate the shared content mount or throw.
  holdAlpha=true;await page.evaluate(()=>{if(window.handle?.openProject)void window.handle.openProject('alpha');else{const f=document.querySelector('form');f.elements.project.value='alpha';f.requestSubmit();}});
  for(let i=0;i<40&&!releaseAlpha;i++)await wait(25);assert.ok(releaseAlpha);
  await page.evaluate(()=>document.querySelector('#mount').innerHTML='<section data-other-route>另一个页面</section>');
  holdAlpha=false;releaseAlpha();releaseAlpha=null;await wait(200);
  assert.equal(await page.locator('[data-other-route]').innerText(),'另一个页面');assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,case:selected,checks:['default real-project directory','search aliases and filters','catalog and event pagination','empty versus failed reads','back retains filters/page/focus','source date semantics','keyboard and responsive layout','same-mount late-response guard','detached-route guard'],errors}));
 } finally {holdAlpha=false;holdList=false;releaseAlpha?.();releaseList?.();await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
