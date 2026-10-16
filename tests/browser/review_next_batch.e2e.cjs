// Real provider stub and HTTP fixture with 25 review fragments.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[],jobs=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('response',async r=>{if(r.url().endsWith('/groups/analyze')&&r.ok())jobs.push(await r.json());});page.setDefaultTimeout(30000);
 const ready=n=>page.waitForFunction(n=>new RegExp(`(?:已判断|已由 AI 判断)[： ]+${n} 条`).test(document.querySelector('[data-rg-preview]')?.textContent||'')&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true',n);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]');await form.locator('[name=projects][value=evo]').check();await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('编号处理保留原始值。');await form.locator('[name=exceptions]').fill('跨平台条件另行判断。');
  await form.locator('[data-rg=preview]').click();await ready(20);
  assert.match(await form.locator('[data-rg-preview]').innerText(),/判断[： ]+20 条/);
  const unjudged=form.locator('[data-rg-preview] > details').filter({hasText:'待你决定是否录入'});
  assert.equal(await unjudged.locator('[data-rg-choice]').count(),5);
  assert.equal(await unjudged.locator('[data-rg-choice]').first().inputValue(),'','unjudged material must not be selected automatically');
  const judged=form.locator('[data-rg-preview] > details').filter({hasText:'按相关度排序的判断结果'});
  for(const checkbox of await judged.locator('[data-rg-pick]').all())await checkbox.check();
  await form.locator('[data-rg=batch-include]').click();await form.locator('[data-rg=apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  await page.locator('[data-rg=resume-batch]').click();await ready(20);
  assert.equal(jobs.length,1,'continuing saved review reads existing results');
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/groups/analyze')),form.locator('[data-rg=next-batch]').click()]);await ready(5);
  assert.match(await form.locator('[data-rg-preview]').innerText(),/判断[： ]+5 条/);assert.equal(jobs[1].total,5);
  const second=jobs[1].id;
  await form.locator('[data-rg=previous-batch]').click();await ready(20);assert.match(await form.locator('[data-rg-preview]').innerText(),/判断[： ]+20 条/);
  assert.equal(await form.locator('[data-rg=apply]').isEnabled(),false,'previously saved decisions must not be offered again');
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/groups/analyze')),form.locator('[data-rg=next-batch]').click()]);await ready(5);assert.equal(jobs[2].id,second);
  await page.reload();await ready(5);assert.match(await form.locator('[data-rg-preview]').innerText(),/判断[： ]+5 条/);assert.equal(jobs.length,3,'reload creates no job');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['保存前 20 条后继续剩余 5 条，未切到其他会话','原先选中项已处理后仍可返回上一批，已保存项不再提供','重复下一批复用结果，刷新保留批次且不启动 AI'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-next-batch-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
