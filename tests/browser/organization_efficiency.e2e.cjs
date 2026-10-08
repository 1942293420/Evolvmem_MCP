const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[], checks=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39488';
 const idle=async()=>page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 try{
  await page.goto(base+'/#knowledge/organization');await page.waitForSelector('[data-rg-list] summary');await idle();
  const card=page.locator('[data-rg-list] details').filter({hasText:'集中审核演示'});
  await card.locator('summary').click();await card.locator('[data-rg="choose"]').click();
  const form=page.locator('[data-rg-form]');
  await form.locator('[name="project"]').selectOption('evo');
  await form.locator('[name="condition"]').fill('编号处理');
  await form.locator('[name="exceptions"]').fill('跨平台条件');
  await form.locator('[name="scope"]').selectOption('future');
  await page.locator('.history-tabs [data-view="projects"]').click();await page.waitForSelector('.project-list');
  await page.locator('.history-tabs [data-view="organization"]').click();await page.waitForSelector('[data-rg-form]');
  assert.equal(await form.locator('[name="condition"]').inputValue(),'编号处理');
  checks.push('集中审核条件草稿切页保留');
  await form.locator('[data-rg="preview"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-preview]')?.textContent.includes('将处理 10 条；排除 2 条'));
  assert.equal(await page.locator('[data-rg-preview] li').count(),12);
  assert.match(await page.locator('[data-rg-preview]').innerText(),/命中例外：跨平台条件/);
  checks.push('同组 12 条先预览，10 条符合、2 条例外逐条可核对');
  await form.locator('[name="condition"]').fill('编号处');
  assert.equal(await form.locator('[data-rg="apply"]').isDisabled(),true);
  await form.locator('[name="condition"]').fill('编号处理');await form.locator('[data-rg="preview"]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-rg="apply"]').disabled);
  for(const width of [1440,1024,768,390]){
    await page.setViewportSize({width,height:1000});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'page overflow '+width);
    await page.screenshot({path:`/tmp/evolvmem-efficiency-${width}.png`,fullPage:true});
  }
  checks.push('四种屏宽均可查看预览，无页面横向溢出');
  await page.setViewportSize({width:1440,height:1000});await form.locator('[data-rg="apply"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('已保存 10 条，失败 0 条'));
  await idle();assert.match(await page.locator('[data-rg-status]').innerText(),/一条后续指导/);
  checks.push('一次保存 10 条并只建立一条后续指导');
  const history=page.locator('#org-review-groups section').filter({has:page.locator('[data-history-list]')});
  await history.locator('input[name="query"]').fill('上一轮');await history.locator('button[type="submit"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-history-page-label]')?.textContent.includes('共 1 条'));
  await history.locator('summary').first().click();await history.locator('[data-progress-restore]').click();
  await page.waitForFunction(()=>document.querySelector('[data-history-status]')?.textContent.includes('已恢复'));
  checks.push('仅留历史的进度可搜索并恢复到审核流程');
  const task=page.locator('.organization-task[data-org-task]').filter({hasText:'集中审核演示'});
  await task.locator('[data-org-action="toggle"]').click();await page.waitForSelector('.organization-unit');
  await task.locator('[data-org-action="feedback"][data-verdict="correct"]').first().click();
  await page.waitForFunction(()=>document.querySelector('#org-metrics')?.textContent.includes('抽检 1 条'));
  checks.push('明确抽检反馈才计数，指标说明分母与样本范围');
  await page.goto(base+'/#home');await page.waitForFunction(()=>document.querySelector('[data-slot="organization-metrics"]')?.textContent.includes('整理效果'));
  await page.locator('[data-slot="organization-metrics"] summary').click();
  assert.match(await page.locator('[data-slot="organization-metrics"]').innerText(),/不能代表全库误判率/);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
