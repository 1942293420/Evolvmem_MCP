const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/capabilities?*',async route=>{
  const p=new URL(route.request().url()).searchParams;requests.push(p.toString());
  const data=await (await route.fetch()).json(),ip=Number(p.get('intake_page')||1),filtered=!!p.get('intake_q');
  data.items=[{memory_id:'synthetic-memory',category:'frontend',title:'合成验收：列表交互',content:'这是隔离浏览器验收数据，用于检查分类与导航布局。',keywords:['浏览器验收'],source_project:'fixture',source_ref:'synthetic'}];data.total=1;data.pages=1;data.counts={frontend:1};
  data.intake_all_total=517;data.intake_total=filtered?1:517;data.intake_page=filtered?1:ip;data.intake_pages=filtered?1:26;data.intake_source_projects=['fixture','pending-only-project'];
  data.intake=Array.from({length:filtered?1:20},(_,i)=>({intake_id:String((ip-1)*20+i+1),title:'合成验收：待分类资料 '+((ip-1)*20+i+1),content:'核对待分类分页、分类下拉框和按钮之间的间距。此数据仅在浏览器测试拦截中返回。',source_project:'fixture',suggested_category:'frontend',revision:1}));
  await route.fulfill({json:data});
 });
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39482';
 await page.goto(base+'/#home');await page.waitForSelector('[data-overview-intake]');
 assert.equal(await page.locator('[data-overview-intake]').getAttribute('href'),'#knowledge/capabilities?tab=intake');
 await page.locator('[data-overview-intake]').click();await page.waitForSelector('[data-intake-card="1"]');
 assert.equal(await page.locator('[data-tab="intake"]').getAttribute('aria-selected'),'true');
 assert.match(page.url(),/tab=intake/);
 await page.locator('[data-intake-page="next"]').click();await page.waitForSelector('[data-intake-card="21"]');
 await page.locator('[data-intake-query]').fill('保留筛选');await page.locator('[data-intake-tools]').evaluate(el=>el.requestSubmit());
 await page.waitForFunction(()=>document.querySelectorAll('[data-intake-card]').length===1);
 await page.locator('[data-tab="suggestions"]').click();assert.match(page.url(),/tab=suggestions/);
 await page.reload();await page.waitForSelector('[data-tab="suggestions"][aria-selected="true"]');
 await page.locator('.signal-nav [data-page-link="home"]').click();await page.locator('[data-overview-intake]').click();
 await page.waitForSelector('[data-intake-card]');assert.equal(await page.locator('[data-tab="intake"]').getAttribute('aria-selected'),'true');
 await page.locator('.signal-nav [data-knowledge-view="capabilities"]').click();
 await page.waitForSelector('[data-memory-card]');assert.equal(await page.locator('[data-tab="formal"]').getAttribute('aria-selected'),'true');
 for(const width of [390,768,1440,1920]){
  await page.setViewportSize({width,height:1000});
  await page.locator('[data-tab="intake"]').click();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'pending page overflow '+width);
  if(width===390)assert.ok(await page.locator('.refresh-topnav').evaluate(el=>el.getBoundingClientRect().height<240),'frozen mobile header must leave room to read');
  const trigger=page.locator('[data-intake-filter]').locator('..').getByRole('combobox');
  await trigger.click();await page.waitForSelector('.capability-select-menu');
  const rect=await page.locator('.capability-select-menu').boundingBox();
  assert.ok(rect.x>=0&&rect.x+rect.width<=width+1,'dropdown bounds '+width);
  await trigger.press('Escape');
  await page.screenshot({path:path.join(__dirname,'capability-intake-'+width+'.png')});
  await page.locator('[data-tab="formal"]').click();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'formal page overflow '+width);
 }
 await page.goto(base+'/#knowledge/intake?mode=records');await page.waitForSelector('#filter-project');
 assert.match(page.url(),/mode=records/);await page.reload();await page.waitForSelector('#filter-project');assert.match(page.url(),/mode=records/);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,checks:['home pending route','intake paging','tab URL reload','cached navigation explicit tabs','390/768/1440/1920 no overflow','custom dropdown bounds','records URL reload'],errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
