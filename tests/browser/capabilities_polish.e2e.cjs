const assert = require('node:assert/strict');
const {chromium} = require('playwright');
require('node:fs').mkdirSync('artifacts',{recursive:true});
const base = process.env.TEST_BASE || 'http://127.0.0.1:39480';
(async()=>{
 const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL,headless:true});
 try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[]; let reads=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().includes('/api/capabilities?'))reads++});
 await page.goto(base+'/#knowledge/capabilities');await page.waitForSelector('[data-memory-card]');
 await page.locator('[data-memory-query]').fill('未提交草稿');
 await page.evaluate(()=>{window.originalQuery=document.querySelector('[data-memory-query]');window.originalNeighbor=document.querySelectorAll('[data-memory-card]')[1]});
 const start=Date.now();await page.locator('[data-memory-card]').first().click();
 assert.ok(await page.locator('[data-memory-source-detail]').first().isVisible());
 assert.ok(await page.evaluate(()=>originalQuery===document.querySelector('[data-memory-query]')&&originalNeighbor===document.querySelectorAll('[data-memory-card]')[1]),'expanding one card must preserve other DOM nodes');
 assert.equal(await page.locator('[data-memory-query]').inputValue(),'未提交草稿');
 console.log('expandMs',Date.now()-start);
 const beforeReads=reads;
 await page.locator('a[href="#home"]').first().click();await page.waitForSelector('[data-overview-capabilities]');
 await page.locator('a[href="#knowledge/capabilities"]').first().click();await page.waitForSelector('[data-memory-query]');
 assert.equal(await page.locator('[data-memory-query]').inputValue(),'未提交草稿');
 assert.equal(reads,beforeReads,'return navigation must reuse recent data');
 await page.locator('[data-memory-query]').fill('Vue表单');await page.locator('[data-memory-search]').click();
 await page.waitForFunction(()=>document.querySelectorAll('[data-memory-card]').length===1);
 await page.locator('[data-memory-query]').fill('');await page.locator('[data-memory-search]').click();
 await page.waitForFunction(()=>document.querySelectorAll('[data-memory-card]').length===20);
 await page.locator('[data-page="next"]').click();await page.waitForFunction(()=>document.querySelector('[data-page-status]').textContent.includes('第 2 / 2 页'));
 await page.locator('[data-memory-source]').selectOption('integration');await page.waitForFunction(()=>[...document.querySelectorAll('[data-memory-card]')].every(e=>e.textContent.includes('integration')));
 await page.route('**/api/capabilities?*',async route=>{if(new URL(route.request().url()).searchParams.get('q')==='旧查询')await new Promise(r=>setTimeout(r,1000));await route.continue().catch(()=>{})});
 await page.locator('[data-memory-query]').fill('旧查询');await page.locator('[data-memory-search]').click();
 await page.locator('[data-memory-source]').selectOption('all');
 await page.locator('[data-memory-query]').fill('Vue表单');await page.locator('[data-memory-search]').click();
 await page.waitForFunction(()=>document.querySelectorAll('[data-memory-card]').length===1);await page.waitForTimeout(1200);
 assert.match(await page.locator('[data-memory-card]').innerText(),/Vue表单/);
 await page.unroute('**/api/capabilities?*');
 await page.route('**/api/capabilities?*',route=>route.fulfill({status:500,json:{error:'测试读取失败'}}));
 await page.locator('[data-memory-query]').fill('强制失败');await page.locator('[data-memory-search]').click();
 await page.waitForSelector('.refresh-notice');assert.match(await page.locator('.refresh-notice').innerText(),/失败/);
 assert.equal(await page.locator('[data-memory-query]').inputValue(),'强制失败');assert.equal(await page.locator('[data-memory-card]').count(),1);
 await page.unroute('**/api/capabilities?*');
 await page.locator('[data-memory-query]').fill('');await page.locator('[data-memory-search]').click();await page.waitForFunction(()=>document.querySelectorAll('[data-memory-card]').length===20);
 for(const width of [390,768,1024,1440,1920]) {
  await page.setViewportSize({width,height:1000});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`overflow at ${width}`);
  if(width>=1440)assert.ok(await page.locator('.refresh-main').evaluate(e=>e.getBoundingClientRect().width>innerWidth*.9),'workspace should use full width');
  await page.screenshot({path:`artifacts/after-${width}.png`});
 }
 await page.locator('[data-tab="intake"]').click();await page.locator('[data-assign="1"]').click();
 await page.waitForFunction(()=>document.querySelector('.refresh-notice')?.textContent.includes('正式'));
 await page.locator('[data-tab="suggestions"]').click();await page.locator('[data-suggestion="accept"]').click();
 await page.waitForFunction(()=>document.querySelector('.refresh-notice')?.textContent.includes('创建'));
 assert.deepEqual(errors,[]);
 const slow=await browser.newPage();
 await slow.route('**/api/capabilities?*',async route=>{await new Promise(r=>setTimeout(r,900));await route.continue().catch(()=>{})});
 await slow.goto(base+'/#knowledge/capabilities');
 await slow.locator('[data-memory-query]').fill('加载中输入的草稿',{timeout:700});
 await slow.waitForSelector('[data-memory-card]');
 assert.equal(await slow.locator('[data-memory-query]').inputValue(),'加载中输入的草稿');
 await slow.locator('[data-memory-query]').fill('延迟读取');await slow.locator('[data-memory-search]').click();
 await slow.locator('a[href="#knowledge/archives"]').first().click();await slow.waitForSelector('#project-archive-form');
 await slow.waitForTimeout(1100);assert.ok(await slow.locator('#project-archive-form').isVisible(),'detached capability response must not overwrite another route');
 await slow.close();
 await page.goto(base+'/#principles');await page.locator('[data-principle="capabilities"]').click();
 for(const width of [390,768,1440]) {
  await page.setViewportSize({width,height:1000});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  assert.ok(await page.locator('.product-diagram svg').isVisible());
  await page.screenshot({path:`artifacts/capability-flow-${width}.png`});
 }
 for(const route of ['browse','classify']) {
  await page.locator('[data-route-select]').selectOption(route);await page.locator('[data-trace-next]').click();
  assert.match(await page.locator('[data-trace-status]').innerText(),/1\//);
 }
 console.log(JSON.stringify({passed:true,errors,capabilityReads:reads,widths:[390,768,1024,1440,1920]}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
