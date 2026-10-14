const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}});let release,recoveryJob=null;
 const gate=new Promise(r=>release=r),errors=[],requested=[];page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>requested.push(new URL(r.url()).pathname));
 let waiting;const pending=new Promise(r=>waiting=r);
 await page.route('**/organization/groups/recent-analysis',async route=>{waiting();await gate;await route.fulfill({json:{job:recoveryJob}});});
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await pending;
  const first=page.locator('[data-rg-list] button').first();await first.waitFor();
  assert.ok(!requested.includes('/api/memories')&&!requested.includes('/api/stats'),'review must not load the hidden home dashboard');
  // A slow optional recovery response must not disable the visible review list.
  await first.click({timeout:2000});
  await page.locator('[data-rg-title]').waitFor();
  const title=await page.locator('[data-rg-title]').textContent();
  const second=page.locator('[data-rg-list] button').nth(1);
  await second.click();await page.waitForFunction(t=>document.querySelector('[data-rg-title]').textContent!==t,title);
  const selected=await page.locator('[data-rg-title]').textContent();
  recoveryJob={id:'older-batch',status:'running',request:{group_id:await first.getAttribute('data-id')},resume_group_id:await first.getAttribute('data-id')};
  release();await page.waitForResponse('**/organization/groups/recent-analysis');
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  assert.equal(await page.locator('[data-rg-title]').textContent(),selected);
  assert.equal(await page.locator('[data-rg-form]').evaluate(el=>el.inert),false);
  let openFirst;const firstPending=new Promise(r=>openFirst=r);let releaseFirst;
  const firstGate=new Promise(r=>releaseFirst=r),firstId=await first.getAttribute('data-id');
  await page.route('**/organization/groups/detail?*',async route=>{
   if(new URL(route.request().url()).searchParams.get('group_id')!==firstId)return route.continue();
   const response=await route.fetch();openFirst();await firstGate;await route.fulfill({response});
  });
  await first.click();await firstPending;
  await second.click();
  await page.waitForFunction(()=>!document.querySelector('[data-rg-form]').inert);
  assert.equal(await page.locator('[data-rg-title]').textContent(),selected);
  const oldResponse=page.waitForResponse(r=>r.url().includes('/groups/detail?')&&r.url().includes(firstId));releaseFirst();await oldResponse;
  await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
  assert.equal(await page.locator('[data-rg-title]').textContent(),selected,'late detail must not replace the last selection');
  assert.ok(!requested.includes('/api/knowledge/organization/metrics'),'hidden metrics must not delay review clicks');
  await page.locator('[data-org-tab=all]').click();
  const metricsLoaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/knowledge/organization/metrics');
  await page.locator('#org-metrics').locator('..').locator('summary').click();await metricsLoaded;
  const homeLoaded=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/stats');
  await page.locator('.signal-nav [data-page-link=home]').click();await homeLoaded;
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['旧结果恢复期间可点击资料','可切换第二条资料','后台返回不覆盖当前选择','快速切换只显示最后选择'],errors}));
 }finally{release();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
