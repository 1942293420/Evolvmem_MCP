const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(30000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'集中审核演示'}).locator('button').click();
  const form=page.locator('[data-rg-form]'),source=form.locator('[data-rg-source]');
  await source.locator('.task-context-source').waitFor();
  assert.equal(await source.evaluate(el=>el.open),true);
  assert.match(await source.innerText(),/审核条目00/);
  assert.match(await source.innerText(),/审核条目11/);
  const choices=await form.locator('[name=unit] option').evaluateAll(es=>es.map(e=>e.value));
  await form.locator('[name=unit]').selectOption(choices.at(-1));
  await page.waitForFunction(()=>document.querySelector('[data-rg-source] [data-context-focus]')?.textContent.includes('审核条目11'));
  assert.match(await source.innerText(),/审核条目00/);
  await source.locator('[data-context-locate]').click();
  await source.locator('[data-context-start]').click();
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('编号处理规则。');
  await form.locator('[data-rg=preview]').click();
  const row=form.locator('[data-rg=review-one]').first().locator('xpath=ancestor::li[1]');
  const contextRoute='**/api/knowledge/organization/context?*';
  await page.route(contextRoute,r=>r.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'context_unavailable'})}));
  await row.locator('[data-rg=review-one]').click();
  await page.waitForFunction(()=>[...document.querySelectorAll('[data-rg-manual-original]')].some(e=>e.textContent.includes('原始上下文读取失败，请重新打开核查。')));
  assert.equal(await row.locator('[data-rg=confirm-one]').count(),0,'unread context must not offer confirmation');
  await page.unroute(contextRoute);
  await row.locator('[data-rg=review-one]').click();
  await row.locator('.task-context-source').waitFor();
  assert.match(await row.locator('.task-context-source').innerText(),/审核条目00/);
  assert.match(await row.locator('.task-context-source').innerText(),/审核条目11/);
  assert.equal(await row.locator('[data-rg=confirm-one]').isVisible(),true);
  assert.equal(await row.locator('[data-rg=exclude-one]').isVisible(),true);
  for(const width of [390,768,1512]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);
  }
  await row.locator('.task-context').scrollIntoViewIfNeeded();
  await page.screenshot({path:'/tmp/evo-task-context-ui.png'});
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/qa');
  await page.locator('[data-memory-action=context]').first().click();
  await page.locator('[data-memory-context] .task-context').waitFor();
  assert.doesNotMatch(await page.locator('[data-memory-context]').innerText(),/undefined|\[object Object\]|读取失败/);
  await page.getByRole('button',{name:'关闭记忆详情',exact:true}).click();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['核查默认展开原始上下文','切换到末尾片段仍能看到起始需求','完整来源与当前记忆定位','批量核查保留归入和排除操作','经验问答核查实际来源','三种屏宽无溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-task-context-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
