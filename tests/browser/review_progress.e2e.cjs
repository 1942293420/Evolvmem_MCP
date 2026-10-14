// Delayed HTTP responses exercise feedback before, during and after AI work.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(20000);
 let release,submitted,phase='pending',requests=0,job;
 const gate=new Promise(r=>release=r),sent=new Promise(r=>submitted=r);
 await page.route('**/groups/analyze',async route=>{
  requests++;const response=await route.fetch();job=await response.json();submitted();await gate;
  await route.fulfill({json:{...job,status:'pending',processed:0,total:12,waiting_for:'organization',elapsed_seconds:3}});
 });
 await page.route('**/groups/analysis?*',async route=>{
  if(phase==='complete')return route.continue();
  if(phase==='error')return route.fulfill({json:{...job,status:'failed',error:'review_ai_failed',processed:4,total:12}});
  await route.fulfill({json:{...job,status:phase,processed:phase==='running'?4:0,total:12,waiting_for:'organization',elapsed_seconds:8}});
 });
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]'),button=form.locator('[data-rg=preview]'),status=form.locator('[data-rg-analysis]');
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('报表导出必须保留原始编号，以便回查来源。');
  await button.click();await sent;
  assert.equal(await button.isDisabled(),true,'prevent duplicate submission while HTTP is pending');
  assert.equal(await button.getAttribute('aria-busy'),'true');
  assert.match(await status.innerText(),/正在准备预览/);
  assert.notEqual(await status.locator('.review-spinner').evaluate(el=>getComputedStyle(el).animationName),'none');
  release();await page.waitForFunction(()=>document.querySelector('[data-rg-analysis]').textContent.includes('等待当前整理任务'));
  assert.match(await status.innerText(),/0 \/ 12/);
  phase='running';await page.waitForFunction(()=>document.querySelector('[data-rg-analysis]').textContent.includes('4 / 12'));
  assert.equal(await status.locator('progress').getAttribute('value'),'4');
  await status.scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-review-progress.png'});
  await page.setViewportSize({width:390,height:1000});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'progress must fit narrow screens');
  await status.scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-review-progress-mobile.png'});
  await page.emulateMedia({reducedMotion:'reduce'});
  assert.equal(await status.locator('.review-spinner').evaluate(el=>getComputedStyle(el).animationName),'none');
  await page.setViewportSize({width:1512,height:1000});
  phase='error';await page.waitForFunction(()=>document.querySelector('[data-rg=preview]').textContent==='重试预览');
  assert.equal(await button.isEnabled(),true);assert.match(await status.innerText(),/未完成/);
  assert.equal(await status.locator('.review-spinner').isVisible(),false);
  phase='complete';await page.unroute('**/groups/analyze');await button.click();
  await page.waitForFunction(()=>document.querySelector('[data-rg=apply]').disabled===false);
  assert.equal(await status.isVisible(),false);
  assert.equal(await button.getAttribute('aria-busy'),'false');
  assert.equal(requests,1);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['提交即反馈并禁止重复点击','等待当前任务与真实进度可见','动画持续且进度不虚增','失败可重试','完成后恢复保存'],errors}));
 }finally{release();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
