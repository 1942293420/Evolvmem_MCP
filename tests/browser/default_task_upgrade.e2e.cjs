const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(30000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] button').filter({hasText:'旧待审资料自动更新演示'}).click();
  assert.equal(await page.locator('[data-rg=complete-task]').count(),0);
  assert.equal(await page.locator('[data-rg-decision]').isVisible(),false);
  assert.match(await page.locator('[data-rg-legacy]').innerText(),/自动整理/);
  await page.locator('[data-rg-narrative] h4').first().waitFor({timeout:45000});
  assert.match(await page.locator('[data-rg-narrative]').innerText(),/增加写入开关/);
  assert.match(await page.locator('[data-rg-narrative]').innerText(),/待用户实际验收/);
  assert.equal(await page.locator('[data-rg-decision]').isVisible(),true);
  assert.match(await page.locator('[data-rg-status]').innerText(),/已自动更新为完整任务/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['旧待审资料自动进入完整任务整理','无需点击转换','当前阅读内容自动更新','待整理时不误分类，完成后可审核'],errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
