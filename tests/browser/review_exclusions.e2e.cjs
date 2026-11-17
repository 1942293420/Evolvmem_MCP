// Human exclusions stay scoped, survive refresh, and can be undone without model work.
const assert=require('node:assert/strict');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];let analyzes=0;
 page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>{if(r.url().endsWith('/groups/analyze'))analyzes++;});page.setDefaultTimeout(30000);
 const ready=()=>page.waitForFunction(()=>document.querySelector('[data-rg-preview]')?.textContent.includes('AI 已按业务依据')&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]');await form.locator('[name=projects][value=evo]').check();await form.locator('[name=selection]').selectOption('group');await form.locator('[name=condition]').fill('导出数据保留原始编号的要求。');
  await form.locator('[data-rg=preview]').click();await ready();
  const row=form.locator('[data-rg=review-one]').first().locator('xpath=ancestor::li[1]');await row.locator('[data-rg=review-one]').click();
  await row.locator('[data-rg=exclude-one]').waitFor();assert.match(await row.locator('.review-original').innerText(),/审核条目/);
  for(const width of [1512,390]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);}
  await page.setViewportSize({width:1512,height:1000});
  await row.locator('[data-rg=exclude-one]').click();assert.equal(await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).count(),0,'not persisted before save');await form.locator('[data-rg=apply]').click();await page.locator('[data-rg=resume-batch]').click();await ready();await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).waitFor();
  assert.equal(await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).count(),1);assert.equal(await form.locator('[name=exceptions]').inputValue(),'');
  assert.match(await form.locator('[data-rg-preview]').innerText(),/你已确认不归入/);
  await page.reload();await ready();assert.equal(await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).count(),1);assert.equal(analyzes,1,'refresh must not start AI');
  await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-manual-excluded.png'});
  await form.locator('[data-rg-choice]').filter({has:page.locator('option', {hasText:'已保存：不录入'})}).selectOption('include');await form.locator('[data-rg=apply]').click();await page.waitForFunction(()=>![...document.querySelectorAll('[data-rg-choice] option')].some(x=>x.textContent==='已保存：不录入'));
  assert.equal(analyzes,1,'undo must restore saved judgments');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['原文下同时提供归入与不归入决定','排除只保存当前范围，例外条件保持空白','刷新仍记得人工排除，无重复 AI 调用','已排除资料可选为录入并统一保存'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-manual-exclude-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
