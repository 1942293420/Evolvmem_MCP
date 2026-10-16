// Slow detail reads must reflect the new selection, preserve drafts and recover.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[],checks=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 let release;const gate=new Promise(r=>release=r);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  const first=page.locator('[data-rg-list] button').first(),second=page.locator('[data-rg-list] button').nth(1);
  await first.click();await page.waitForFunction(()=>!document.querySelector('[data-rg-form]').hidden&&!document.querySelector('[data-rg-form]').inert);
  const firstId=await first.getAttribute('data-id'),secondId=await second.getAttribute('data-id'),title=await second.locator('strong').innerText();
  await page.locator('[data-rg-ai] > summary').click();
  await page.getByText('补充说明（选填）',{exact:true}).click();
  await page.locator('[data-rg-form] [name=guidance]').fill('保留这份尚未保存的归类说明');
  let requested;const pending=new Promise(r=>requested=r);
  const slow=async route=>{
   if(new URL(route.request().url()).searchParams.get('group_id')!==secondId)return route.continue();
   requested();await gate;if(!route.request().failure())await route.continue();
  };
  await page.route('**/organization/groups/detail?*',slow);
  await second.click();await pending;
  assert.equal(await second.getAttribute('aria-pressed'),'true','clicked item must highlight before the response');
  assert.equal(await page.locator('[data-rg-form]').isVisible(),false,'previous detail must not look like the new selection');
  assert.ok((await page.locator('[data-rg-empty]').innerText()).includes(title));
  await page.screenshot({path:'/tmp/evo-selection-loading.png',fullPage:true});
  const canceled=page.waitForEvent('requestfailed',{predicate:r=>r.url().includes(secondId)});
  await first.click();await canceled;
  await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]').value===id&&!document.querySelector('[data-rg-form]').hidden,firstId);
  assert.equal(await page.locator('[data-rg-form] [name=guidance]').inputValue(),'保留这份尚未保存的归类说明');
  release();await page.unroute('**/organization/groups/detail?*',slow);
  checks.push('加载即高亮并隐藏旧详情；切换取消旧请求且保留草稿');

  let timeoutRelease;const timeoutGate=new Promise(r=>timeoutRelease=r);
  const hanging=async route=>{if(new URL(route.request().url()).searchParams.get('group_id')!==secondId)return route.continue();await timeoutGate;if(!route.request().failure())await route.continue();};
  await page.route('**/organization/groups/detail?*',hanging);
  const timeoutCanceled=page.waitForEvent('requestfailed',{predicate:r=>r.url().includes(secondId),timeout:20000});
  await second.click();await timeoutCanceled;
  const retry=page.locator('[data-rg=retry-detail]');await retry.waitFor({state:'visible',timeout:2000});
  assert.equal(await page.locator('[data-rg-form]').isVisible(),false);
  for(const width of [1512,390]){
   await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-selection-retry-${width}.png`,fullPage:true});
  }
  timeoutRelease();await page.unroute('**/organization/groups/detail?*',hanging);
  await retry.click();await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]').value===id&&!document.querySelector('[data-rg-form]').hidden,secondId);
  checks.push('请求超时终止等待，桌面及手机显示重试，重试打开正确资料');
  const third=page.locator('[data-rg-list] button').nth(2),thirdId=await third.getAttribute('data-id');
  await page.route('**/organization/groups/detail?*',route=>route.fulfill({status:503,json:{error:'temporary failure'}}));
  await third.click();await retry.waitFor();assert.equal(await page.locator('[data-rg-form]').isVisible(),false);
  await page.unroute('**/organization/groups/detail?*');await retry.click();
  await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]').value===id&&!document.querySelector('[data-rg-form]').hidden,thirdId);
  await first.click();await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]').value===id&&!document.querySelector('[data-rg-form]').hidden,firstId);
  assert.equal(await page.locator('[data-rg-form] [name=guidance]').inputValue(),'保留这份尚未保存的归类说明');
  checks.push('服务失败显示可重试状态，恢复后草稿仍在');
  let navigateRelease;const navigateGate=new Promise(r=>navigateRelease=r);let navigateRequested;const navigatePending=new Promise(r=>navigateRequested=r);
  await page.route('**/organization/groups/detail?*',async route=>{navigateRequested();await navigateGate;await route.continue();});
  const fourth=page.locator('[data-rg-list] button').nth(3),fourthId=await fourth.getAttribute('data-id');
  await fourth.click();await navigatePending;
  await page.locator('.signal-nav [data-knowledge-view=projects]').click();await page.locator('.project-list').waitFor();
  const navigatedResponse=page.waitForResponse(r=>r.url().includes('/groups/detail?')&&r.url().includes(fourthId));navigateRelease();await navigatedResponse;
  await page.locator('.signal-nav [data-knowledge-view=organization]').click();
  await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]')?.value===id&&!document.querySelector('[data-rg-form]').hidden&&!document.querySelector('[data-rg-form]').inert,fourthId);
  checks.push('加载期间切页，返回后仍显示刚才选择的资料');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors}));
 }finally{release();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
