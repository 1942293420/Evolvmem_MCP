const assert = require('node:assert/strict');
const {chromium} = require('playwright');
(async () => {
 const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL,headless:true});
 try {
  const page=await browser.newPage(); let posts=0;
  page.on('pageerror', error=>{throw error;});
  await page.route('**/api/capabilities?*',async route=>{
   const response=await route.fetch(), data=await response.json();
   data.intake=[{intake_id:999999,title:'Guard fixture',content:'Retained draft',source_project:'test',revision:1,suggested_category:data.categories[0].id}];
   data.intake_total=1;
   data.suggestions=[{suggestion_id:999999,label:'Guard suggestion',reason:'Local test only'}];
   data.suggestions_total=1;
   await route.fulfill({response,json:data});
  });
  await page.route('**/api/capabilities/intake/999999/assign',async route=>{posts++;await new Promise(resolve=>setTimeout(resolve,650));await route.fulfill({status:200,json:{ok:true}});});
  await page.route('**/api/capability-categories/suggestions/999999/*',async route=>{posts++;await new Promise(resolve=>setTimeout(resolve,650));await route.fulfill({status:200,json:{ok:true}});});
  await page.goto((process.env.TEST_BASE||'http://127.0.0.1:39480')+'/#knowledge/capabilities');
  await page.waitForSelector('[data-memory-card]');
  assert.equal(await page.locator('[data-intake-results] > *').count(),0,'intake should not render until opened');
  assert.equal(await page.locator('[data-suggestion-results] > *').count(),0,'suggestions should not render until opened');
  await page.locator('[data-tab="intake"]').click();
  const options=await page.locator('[data-intake-category] option').evaluateAll(nodes=>nodes.map(n=>n.value));
  await page.locator('[data-intake-category]').selectOption(options[1]);
  await page.locator('[data-tab="formal"]').click();await page.locator('[data-memory-refresh]').click();
  await page.waitForFunction(()=>document.querySelector('.capability-workspace').getAttribute('aria-busy')==='false');
  await page.locator('[data-tab="intake"]').click();
  assert.equal(await page.locator('[data-intake-category]').inputValue(),options[1],'refresh and tab navigation retain intake draft');
  await page.evaluate(()=>{const b=document.querySelector('[data-assign]');b.click();b.dispatchEvent(new MouseEvent('click',{bubbles:true}));});
  await page.waitForTimeout(250);assert.equal(posts,1,'double activation must submit once');
  await page.locator('[data-tab="suggestions"]').click();
  assert.equal(await page.locator('[data-suggestion="accept"]').isDisabled(),true,'shared lock prevents conflicting writes');
  await page.waitForFunction(()=>!document.querySelector('[data-suggestion="accept"]').disabled);
  await page.evaluate(()=>{EvolvAuth.canWrite=false;document.querySelector('[data-suggestion="accept"]').dispatchEvent(new MouseEvent('click',{bubbles:true}));});
  assert.equal(posts,1,'permission revocation must block submit');
  assert.equal(await page.locator('[data-suggestion="accept"]').isDisabled(),true);
  assert.match(await page.locator('[data-notice-text]').textContent(),/权限/);
  await page.evaluate(()=>{EvolvAuth.canWrite=true;});
  await page.locator('[data-tab="formal"]').click();
  const card=page.locator('[data-memory-card]').first();await card.focus();await page.keyboard.press('Enter');
  assert.equal(await card.getAttribute('aria-expanded'),'true');
  await page.keyboard.press('Space');assert.equal(await card.getAttribute('aria-expanded'),'false');
  await page.locator('[data-tab="formal"]').focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('[data-tab="intake"]').getAttribute('aria-selected'),'true');
  console.log(JSON.stringify({passed:true,checks:['lazy queues','intake draft retention','double write lock','cross-tab write lock','live permission revocation','card keyboard','tab keyboard'],interceptedWrites:posts}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
