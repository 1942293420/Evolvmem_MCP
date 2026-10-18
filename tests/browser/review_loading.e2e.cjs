const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true});const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39541';
 try{
  await page.goto(base+'/#knowledge/organization');
  await page.locator('[data-rg="choose"]').first().click();
  await page.waitForFunction(()=>!document.querySelector('[data-rg-form]').inert);
  await page.locator('[data-rg-form] [name="projects"][value="evo"]').check();
  // A navigation fails while the mounted review editor contains a draft.
  const endpoint='**/api/knowledge/projects';
  await page.route(endpoint,route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,error:'service_busy'})}));
  await page.evaluate(()=>location.hash='#knowledge/projects');
  await page.locator('[data-action="retry"]').waitFor();
  assert.match(await page.locator('#knowledge-receipt').innerText(),/正在处理其他资料/);
  assert.equal(await page.locator('#knowledge-loading').isVisible(),false);
  await page.unroute(endpoint);
  await page.locator('[data-action="retry"]').click();
  await page.locator('.project-list').waitFor();
  await page.evaluate(()=>location.hash='#knowledge/organization');
  await page.locator('[data-rg-form]:not([hidden])').waitFor();
  assert.equal(await page.locator('[data-rg-form] [name="projects"][value="evo"]').isChecked(),true);
  // A read which never responds must become recoverable, not spin forever.
  let release;const hold=new Promise(resolve=>release=resolve);
  await page.route(endpoint,async route=>{await hold;await route.abort().catch(()=>{});});
  await page.evaluate(()=>location.hash='#knowledge/projects');
  await page.locator('#knowledge-receipt [data-action="retry"]').waitFor({timeout:20000});
  assert.match(await page.locator('#knowledge-receipt').innerText(),/读取资料超时/);
  assert.equal(await page.locator('#knowledge-loading').isVisible(),false);
  await page.screenshot({path:'/tmp/evo-loading-recovery.png',fullPage:true});
  release();await page.unroute(endpoint);
  await page.locator('#knowledge-receipt [data-action="retry"]').click();await page.locator('.project-list').waitFor();
  console.log('PASS: busy response, stalled read, retry and unsaved draft preservation');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
