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
  await narrative.locator('h4').first().waitFor();
  for(const label of ['用户当时要做什么','为什么需要处理','处理经过与各次修复','最后记录的结果'])assert.match(await narrative.innerText(),new RegExp(label));
  assert.match(await narrative.innerText(),/旧配置缺少迁移/);
  assert.match(await narrative.innerText(),/尚需你在电脑上验证/);
  assert.equal(await page.locator('[data-rg-original]').isVisible(),false);
  assert.equal(await page.locator('[data-rg-unit-picker]').isVisible(),false);
  const source=page.locator('[data-rg-source]');
  await source.locator('summary').first().click();
  await source.locator('.task-context-source').waitFor();
  assert.match(await source.innerText(),/增加写入开关/);
  assert.match(await source.innerText(),/修复迁移和连接提示/);
  for(const width of [390,768,1512]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);
  }
  await page.locator('[name=projects][value=evo]').check();
  await page.locator('[name=projects][value=dsh-a]').check();
  await page.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>!document.querySelector('[data-rg=apply]').disabled);
  assert.match(await page.locator('[data-rg=apply]').innerText(),/1 条/);
  await page.screenshot({path:'/tmp/evo-complete-task-ui.png'});
  await page.locator('[data-rg=apply]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-status]').textContent.includes('已归入'));
  const legacy=page.locator('[data-rg-list] button').filter({hasText:'其余内容'}).first();
  await legacy.click();
  await page.locator('[data-rg=complete-task]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-status]').textContent.includes('已开始按完整任务整理'));
  await page.locator('[data-rg-list] button').filter({hasText:'完整任务演示'}).first().waitFor();
  await page.locator('[data-rg-list] button').filter({hasText:'完整任务演示'}).first().click();
  await page.locator('[data-rg-narrative] h4').first().waitFor();
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['完整任务单项审核','需求过程结果可读','同一窗口全文','多项目归类保存','旧片段重新整理完整任务','三种宽度无溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-complete-task-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
