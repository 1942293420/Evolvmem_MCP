const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(30000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] button').filter({hasText:'完整任务演示'}).click();
  const narrative=page.locator('[data-rg-narrative]');
  await narrative.locator('dt').first().waitFor();
  assert.equal(await page.locator('[data-rg-list] button').filter({hasText:'已确认归属演示'}).count(),0,'confirmed requirements with later repair progress must not reappear');
  for(const label of ['需求','原因','处理重点','结果'])assert.match(await narrative.innerText(),new RegExp(label));
  assert.match(await narrative.innerText(),/旧配置缺少迁移/);
  assert.match(await narrative.innerText(),/尚需你在电脑上验证/);
  assert.equal(await narrative.locator('details').count(),0,'no per-evidence controls');
  assert.equal(await narrative.locator('dl').count(),1,'one compact memory card');
  assert.equal(await page.locator('[data-rg-source] > summary').count(),1,'one full context entry');
  assert.equal(await page.locator('[data-rg-original]').isVisible(),false);
  assert.equal(await page.locator('[data-rg-unit-picker]').isVisible(),false);
  const source=page.locator('[data-rg-source]');
  await source.locator('summary').first().click();
  await source.locator('.task-context-source').waitFor();
  assert.match(await source.innerText(),/增加写入开关/);
  assert.match(await source.innerText(),/修复迁移和连接提示/);
  await source.locator('.task-context-stages > summary').click();
  const stages=source.locator('.task-context-stages');
  assert.match(await stages.innerText(),/增加写入开关/);
  assert.match(await stages.innerText(),/尚需你在电脑上验证/);
  assert.doesNotMatch(await stages.innerText(),/尚未识别到对应原话/);
  for(const width of [390,768,1512]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);
  }
  await page.locator('[name=projects][value=evo]').check();
  await page.locator('[name=projects][value=dsh-a]').check();
  await page.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-rg=apply]').disabled);
  assert.match(await page.locator('[data-rg-save-status]').innerText(),/录入 1 条/);
  await page.screenshot({path:'/tmp/evo-complete-task-ui.png'});
  await page.locator('[data-rg=apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-status]').textContent.includes('已保存审核结果'));
  await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  assert.match(await page.locator('[data-rg-empty]').innerText(),/审核结果已保存/);
  assert.equal(await page.locator('[data-rg-list] button').filter({hasText:'完整任务演示'}).count(),0);
  assert.equal(await page.locator('[data-rg=complete-task]').count(),0);
  assert.match(await page.locator('#org-complete-status').innerText(),/已整理/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['已确认需求不因后续修复而再次待确认','完整任务单项审核','需求过程结果可读','同一窗口全文','多项目归类保存','默认完整任务流程无需转换按钮','三种宽度无溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-complete-task-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
