// Failed optional retrieval must leave a visible direct-save path. Synthetic DB only.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.EVOLVMEM_TEST_URL;
 page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const form=page.locator('[data-rg-form]');let saves=0;
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 page.on('request',r=>{if(r.url().endsWith('/organization/archive'))saves++;});
 const unit=async()=>{
  const task=Number(await form.locator('[name=task_id]').inputValue()),digest=await form.locator('[name=digest]').inputValue();
  const r=await page.request.get(base+'/api/knowledge/organization/detail?task_id='+task);
  return (await r.json()).units.find(u=>u.digest===digest);
 };
 const open=async title=>{
  const item=page.locator('[data-rg-list] button').filter({has:page.locator('strong',{hasText:new RegExp('^'+title+'$')})});
  while(!await item.count()&&await page.locator('[data-rg=next]').isEnabled()){await page.locator('[data-rg=next]').click();await idle();}
  await item.click();await page.waitForFunction(t=>document.querySelector('[data-rg-title]').textContent===t&&!document.querySelector('[data-rg-form]').inert,title);
 };
 const fail=async()=>{
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[data-rg-ai]>summary').click();
  await form.locator('[name=selection]').selectOption('all');
  await form.locator('[name=condition]').fill('按模拟项目部署要求建议归档');
  await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-analysis]').classList.contains('is-error'));
  assert.equal(await form.locator('[data-rg=apply]').isVisible(),false,'no disabled batch-save trap after retrieval failure');
  assert.equal(await form.locator('[data-rg=archive-current]').isEnabled(),true);
 };
 try{
  if(process.env.EVOLVMEM_BASELINE_SCRIPT)await page.route('**/review-groups.js',r=>r.fulfill({contentType:'application/javascript',body:require('node:fs').readFileSync(process.env.EVOLVMEM_BASELINE_SCRIPT,'utf8')}));
  await page.route('**/organization/groups/analyze',r=>r.fulfill({json:{id:'synthetic-failure',status:'pending'}}));
  await page.route('**/organization/groups/analysis?*',r=>r.fulfill({json:{id:'synthetic-failure',status:'failed',error:'review_retrieval_unavailable'}}));
  await page.goto(base+'/#knowledge/organization');await page.locator('[data-rg=choose]').first().waitFor();await idle();await open('保留价值演示');
  await fail();assert.equal((await unit()).decision,'review');
  for(const width of [390,1440]){
   await page.setViewportSize({width,height:1000});await form.locator('[data-rg-failure-actions]').scrollIntoViewIfNeeded();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-retrieval-failure-${width}.png`});
  }
  const selected={task:Number(await form.locator('[name=task_id]').inputValue()),digest:await form.locator('[name=digest]').inputValue()};
  await form.locator('[data-rg=archive-current]').click();await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  const saved=await page.request.get(base+'/api/knowledge/organization/detail?task_id='+selected.task);
  const row=(await saved.json()).units.find(u=>u.digest===selected.digest);
  assert.equal(row.decision,'manual');assert.equal(row.project,'evo');assert.ok(row.item_id);assert.equal(saves,1);
  await open('保留价值演示二');await fail();
  await form.locator('[name=projects][value=evo]').uncheck();
  // Changing scope clears the failed preview; retry with a scope, then defer.
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[data-rg=preview]').click();await page.waitForFunction(()=>document.querySelector('[data-rg-analysis]').classList.contains('is-error'));
  await form.locator('[data-rg=defer-current]').click();await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  assert.match(await page.locator('[data-rg-empty]').innerText(),/暂不归档/);assert.equal(saves,2);assert.deepEqual(errors,[]);
  console.log('PASS: failed batch preview leaves current-item archive/defer available, no automatic batch writes, desktop/mobile layout.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
