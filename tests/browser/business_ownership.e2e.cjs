const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(30000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'集中审核演示'}).locator('button').click();
  const form=page.locator('[data-rg-form]');
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('导出数据保留原始编号的业务规则。');
  await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-preview] .business-evidence'));
  const evidence=form.locator('.business-evidence').first();
  await evidence.locator('summary').click();
  assert.match(await evidence.innerText(),/需求目标/);
  assert.match(await evidence.innerText(),/审核条目/);
  assert.match(await evidence.innerText(),/Evo 演示项目：直接负责的项目/);
  assert.doesNotMatch(await evidence.innerText(),/undefined|\[object Object\]/);
  for(const width of [390,768,1512]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+width);
  }
  await evidence.scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-business-evidence.png'});
  await page.reload();await page.waitForSelector('[data-rg-preview] .business-evidence');
  assert.equal(await form.locator('[name=condition]').inputValue(),'导出数据保留原始编号的业务规则。');
  await form.locator('.business-evidence').first().locator('summary').click();
  assert.match(await form.locator('.business-evidence').first().innerText(),/需求目标/);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['逐条展示归属角色与需求原话','依据可展开核对','刷新保留依据与草稿','三种屏宽无溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-business-evidence-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
