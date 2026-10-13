const assert=require('node:assert/strict');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];let writes=0,analyzes=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().endsWith('/groups/decisions'))writes++;if(r.url().endsWith('/groups/analyze'))analyzes++;});page.setDefaultTimeout(20000);
 const ready=()=>page.waitForFunction(()=>document.querySelector('[data-rg-preview]')?.textContent.includes('AI 已按业务依据')&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]');await form.locator('[name=projects][value=evo]').check();await form.locator('[name=selection]').selectOption('group');await form.locator('[name=condition]').fill('导出数据保留原始编号的要求。');await form.locator('[data-rg=preview]').click();await ready();
  const save=form.locator('[data-rg=apply]');assert.equal(await save.isVisible(),true,'zero recommendations must still show Save');assert.match(await save.innerText(),/保存审核结果/);
  await form.locator('[data-rg-preview] details').evaluateAll(nodes=>nodes.forEach(n=>n.open=true));
  const boxes=form.locator('[data-rg-pick]');assert.ok(await boxes.count()>=5);
  for(let i=0;i<4;i++)await boxes.nth(i).check();await form.locator('[data-rg=batch-exclude]').click();
  await boxes.nth(4).check();await form.locator('[data-rg=batch-include]').click();assert.equal(writes,0,'draft decisions must not write');
  assert.match(await form.locator('[data-rg-save-status]').innerText(),/录入 1 条.*不录入 4 条/);
  await page.reload();await ready();assert.equal(analyzes,1);assert.match(await form.locator('[data-rg-save-status]').innerText(),/录入 1 条.*不录入 4 条/);
  for(const width of [1512,390]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);}
  await page.setViewportSize({width:1512,height:1000});await save.click();await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('已保存审核结果'));
  assert.equal(writes,1);await page.reload();await ready();assert.match(await form.locator('[data-rg-preview]').innerText(),/已人工处理：1 条/);
  const excluded=form.locator('li').filter({hasText:'已保存：不录入'});assert.equal(await excluded.count(),4);
  await excluded.nth(0).locator('[data-rg-pick]').check();await excluded.nth(1).locator('[data-rg-pick]').check();await form.locator('[data-rg=batch-include]').click();await save.click();await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('录入 2 条'));
  assert.equal(writes,2);assert.equal(analyzes,1,'reversing saved exclusions does not call AI');assert.equal(await excluded.count(),2);
  await form.locator('.review-batch-toolbar').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-review-batch-decisions.png'});assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['零推荐仍显示保存按钮','批量勾选录入和不录入，保存前不写库','刷新保留待保存选择','混合结果一次保存','批量将已排除资料改为录入','宽屏手机无横向溢出'],errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
