// Real HTTP against disposable fixtures: no model retry on preview/refresh.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}), errors=[],jobs=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(20000);
 page.on('response',async r=>{if(r.url().endsWith('/groups/analyze')&&r.ok())jobs.push((await r.json()).id);});
 const base=process.env.EVOLVMEM_TEST_URL;
 const previewReady=()=>page.waitForFunction(()=>document.querySelector('[data-rg=apply]')?.disabled===false);
 try{
  await page.goto(base+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]');
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('报表导出必须保留原始编号，以便回查来源。');
  await form.locator('[name=exceptions]').fill('跨平台条件需要单独判断。');
  await form.locator('[data-rg=preview]').click();await previewReady();
  const original=await form.locator('[data-rg-preview]').innerText();
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/groups/analyze')),form.locator('[data-rg=preview]').click()]);await previewReady();
  assert.equal(jobs.length,2);assert.equal(jobs[0],jobs[1]);
  // Merely reading another source fragment must not clear batch results.
  await form.locator('[data-rg-preview] > details').last().locator(':scope > summary').click();
  assert.equal(await form.locator('[data-rg=apply]').isEnabled(),true);
  await page.reload();await previewReady();
  assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  assert.equal(await form.locator('[name=condition]').inputValue(),'报表导出必须保留原始编号，以便回查来源。');
  assert.equal(await form.locator('[data-rg-preview]').innerText(),original);
  assert.equal(jobs.length,2,'reload must not create an analysis');
  await form.locator('[data-rg-preview] h3').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-review-cache-restored.png'});
  await form.locator('[data-rg=apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('已保存审核结果'));
  await page.reload();await page.waitForSelector('[data-rg-list]');
  const recent=await page.evaluate(async()=>await(await fetch('/api/knowledge/organization/groups/recent-analysis')).json());
  assert.equal(recent.job.proposal.eligible.length,0,'saved items must not be offered again');assert.equal(recent.job.proposal.handled_count,10);assert.equal(recent.job.proposal.excluded.length,2,'unreviewed choices remain available');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['再次预览复用同一任务','阅读另一条原文不清空判断','刷新恢复项目、依据、结果与可保存状态，没有启动 AI','保存后只保留未处理选择，已录入资料不再重复提供'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-review-cache-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
