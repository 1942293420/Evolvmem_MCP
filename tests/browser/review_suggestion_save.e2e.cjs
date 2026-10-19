// A completed optional suggestion must still expose and save the batch choices.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  const item=page.locator('[data-rg-list] button').filter({hasText:'审核条目00'}).first();await item.click();
  const form=page.locator('[data-rg-form]');await form.locator('[name=projects][value=evo]').check();
  await form.locator('[data-rg-ai]>summary').click();
  await form.locator('[name=selection]').selectOption('group');await form.locator('[name=condition]').fill('保留原始编号的导出要求');
  await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>!!document.querySelector('[data-rg-choice]')&&!document.querySelector('[data-rg=apply]').disabled,{},{timeout:30000});
  assert.equal(await form.locator('[data-rg=apply]').isVisible(),true);
  assert.equal(await form.locator('[data-rg-failure-actions]').isVisible(),false);
  await form.locator('[name=condition]').fill('保留原始编号的项目导出要求');
  assert.equal(await form.locator('[data-rg=apply]').isVisible(),false,'changing criteria clears the old save action');
  await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>!!document.querySelector('[data-rg-choice]')&&!document.querySelector('[data-rg=apply]').disabled,{},{timeout:30000});
  const response=page.waitForResponse(r=>r.url().endsWith('/organization/groups/decisions'));
  await form.locator('[data-rg=apply]').click();const saved=await (await response).json();
  assert.ok(saved.included>0);await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  assert.deepEqual(errors,[]);console.log('PASS: successful AI suggestions expose batch choices and save actual selected materials.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
