// Real App/knowledge/router integration with synthetic read responses only.
// Start the disposable fixture at EVOLVMEM_TEST_URL. Never point this at production.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39482';
const items=[
 {project:'alpha',display_name:'Synthetic Alpha',aliases:['Alpha alias'],status:'active',has_archive:true,event_count:1,updated_at:'2026-01-02',has_legacy_document:true},
 {project:'empty',display_name:'Synthetic Empty Summary',aliases:[],status:'active',has_archive:false,event_count:1,updated_at:'2026-01-01',has_legacy_document:true},
];
const stats={registered:2,active:2,archived:0,with_archive:1,without_archive:1,active_with_archive:1,active_without_archive:1};
const detail=project=>({project,display_name:items.find(i=>i.project===project).display_name,project_status:'active',has_archive:project!=='empty',summary:'## Synthetic summary\nFixture content',started_at:null,development_days:null,revision:1,events_total:1,events_page:1,events_page_size:100,events_pages:1,events:[{event_id:project==='empty'?2:1,title:'Synthetic event '+project,content:'Synthetic source content',source_ref:'fixture:source',event_type:'milestone',occurred_at:null,occurred_at_precision:'unknown',recorded_at:'2026-01-02'}]});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined});
 let release;
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],writes=[],checks=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/**',route=>{
   if(!['GET','HEAD'].includes(route.request().method())){writes.push(route.request().method()+' '+new URL(route.request().url()).pathname);return route.abort();}
   return route.fallback();
  });
  let hold=false,started;
  const pendingStarted=()=>new Promise(resolve=>{started=resolve;});
  await page.route('**/api/project-archives**',async route=>{
   const url=new URL(route.request().url());
   assert.equal(route.request().method(),'GET');
   if(url.pathname==='/api/project-archives'){
    const q=(url.searchParams.get('q')||'').toLowerCase();
    const result=items.filter(item=>!q||(item.project+' '+item.display_name+' '+item.aliases.join(' ')).toLowerCase().includes(q));
    return route.fulfill({json:{items:result,total:result.length,page:1,page_size:Number(url.searchParams.get('page_size')||20),pages:1,stats}});
   }
   const project=decodeURIComponent(url.pathname.slice('/api/project-archives/'.length));
   if(hold&&project==='alpha'){
    started();await new Promise(resolve=>{release=resolve;});
   }
   // AbortController may have canceled a held request while another view mounted.
   await route.fulfill({json:detail(project)}).catch(error=>{if(!/closed|disposed|intercept|abort/i.test(error.message))throw error;});
  });
  await page.goto(base+'/#home');
  await page.locator('[data-archive-project="alpha"]').click();
  await page.waitForSelector('[data-project-event="1"]');
  assert.equal(new URL(page.url()).hash,'#knowledge/archives?project=alpha');
  assert.match(await page.locator('[data-archive-summary]').innerText(),/Synthetic Alpha/);
  checks.push('homepage directory card opens exact project detail');
  await page.locator('[data-project-back]').click();
  await page.waitForSelector('[data-project-card="alpha"]');
  assert.equal(new URL(page.url()).hash,'#knowledge/archives');
  assert.equal(await page.locator('[data-project-card]').count(),2);
  checks.push('default archives route is a visible project directory');
  await page.locator('#project-archive-form input').fill('Empty');
  await page.locator('#project-archive-form').evaluate(form=>form.requestSubmit());
  await page.waitForFunction(()=>document.querySelectorAll('[data-project-card]').length===1);
  await page.locator('[data-project-card="empty"]').click();
  await page.waitForSelector('[data-project-event="2"]');
  assert.match(await page.locator('[data-archive-empty]').innerText(),/尚无项目摘要/);
  await page.locator('[data-project-event="2"]').click();
  assert.match(await page.locator('[data-event-source]').innerText(),/fixture:source/);
  await page.locator('[data-project-back]').click();
  assert.equal(await page.locator('#project-archive-form input').inputValue(),'Empty');
  assert.equal(await page.locator('[data-project-card]').count(),1);
  await page.locator('[data-project-card="empty"]').click();
  await page.waitForSelector('[data-project-event="2"]');
  await page.reload();
  await page.waitForSelector('[data-project-event="2"]');
  assert.equal(new URL(page.url()).hash,'#knowledge/archives?project=empty');
  assert.equal(await page.locator('.signal-nav [data-knowledge-view="projects"]').isVisible(),true);
  const legacy=page.locator('[data-project-legacy]');
  assert.equal(await legacy.getAttribute('href'),'#knowledge/library?project=empty');
  await legacy.click();
  await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.dataset.workspaceView==='library');
  await page.waitForFunction(()=>!document.querySelector('#content').inert);
  assert.equal(new URL(page.url()).hash,'#knowledge/library?project=empty');
  checks.push('search and return preserve filters; detail reload keeps events without summary');
  checks.push('missing-summary detail links to the exact project legacy materials');
  await page.locator('.signal-nav [data-knowledge-view="projects"]').click();
  await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.dataset.workspaceView==='projects');
  await page.waitForFunction(()=>!document.querySelector('#content').inert);
  assert.match(await page.locator('#knowledge-title').innerText(),/旧版项目/);
  await page.locator('.signal-nav [data-knowledge-view="qa"]').click();
  await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.dataset.workspaceView==='qa');
  await page.waitForFunction(()=>!document.querySelector('#content').inert);
  assert.match(await page.locator('#knowledge-title').innerText(),/旧版经验问答/);
  checks.push('legacy project and QA remain separately reachable');
  await page.locator('.signal-nav [data-knowledge-view="archives"]').click();
  await page.waitForSelector('[data-project-card]');
  await page.locator('#project-archive-form input').fill('');
  await page.locator('#project-archive-form').evaluate(form=>form.requestSubmit());
  await page.waitForSelector('[data-project-card="alpha"]');
  hold=true;const began=pendingStarted();
  await page.locator('[data-project-card="alpha"]').click();await began;
  await page.locator('.signal-nav [data-knowledge-view="capabilities"]').click();
  await page.waitForSelector('.capability-workspace');
  release();release=null;
  await page.waitForTimeout(120);
  assert.equal(await page.locator('.capability-workspace').count(),1);
  assert.equal(await page.locator('[data-project-library]').count(),0);
  assert.equal(new URL(page.url()).hash,'#knowledge/capabilities');
  checks.push('late detail cannot overwrite newer capabilities view');
  assert.deepEqual(writes,[]);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,checks,errors,writes,fixture:'real app, synthetic project reads'}));
 }finally{release?.();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});