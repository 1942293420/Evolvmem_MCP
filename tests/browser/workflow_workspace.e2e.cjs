const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),checks=[],errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(10000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39490';
 const go=async(route,selector)=>{await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');await page.goto(base+'/'+route);await page.waitForSelector(selector);await page.waitForFunction(()=>!document.querySelector('#content')?.inert);};
 try{
  await go('#home','[data-workflow-overview] [data-flow-health]');
  assert.match(await page.locator('[data-workflow-overview]').innerText(),/无需逐条审核/);
  assert.match(await page.locator('[data-workflow-overview]').innerText(),/待你判断/);
  assert.match(await page.locator('[data-workflow-overview]').innerText(),/系统问题/);
  checks.push('首页说明自动完成与人工例外边界，真实运行状态分开呈现');
  await page.locator('[data-flow-link="review"]').click();await page.waitForSelector('[data-org-tab="review"][aria-selected="true"]');
  assert.equal(await page.locator('[data-org-pane="failed"]').isVisible(),false);
  const group=page.locator('[data-rg-list] > details').filter({hasText:'集中审核演示'});
  await group.locator(':scope > summary').click();assert.match(await group.innerText(),/为什么需要你/);assert.match(await group.innerText(),/关键原话/);
  await group.locator('[data-rg="choose"]').click();
  await page.locator('[data-rg-form] [name="condition"]').fill('编号处理');
  await page.locator('[data-org-tab="failed"]').click();
  assert.equal(await page.locator('#org-review-groups').isVisible(),false);
  assert.match(await page.locator('[data-org-pane="failed"]').innerText(),/无需重新判断项目/);
  await page.locator('[data-org-tab="review"]').click();
  assert.equal(await page.locator('[data-rg-form] [name="condition"]').inputValue(),'编号处理');
  checks.push('业务判断与系统问题分开；切换工作区保留条件草稿');
  await page.locator('[data-org-tab="failed"]').click();
  const failed=page.locator('article[data-org-task]').filter({hasText:'重试失败演示'});
  await failed.waitFor();assert.match(await failed.innerText(),/知识提炼未完成/);
  await failed.locator('[data-org-action="retry"]').click();
  await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('已重新排队 1'));
  await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
  checks.push('系统问题使用真实重试接口重新排队，不要求用户重做项目判断');
  await go('#knowledge/organization','[data-rg-list]');
  const value=page.locator('[data-rg-list] > details').filter({hasText:'保留价值演示'});
  await value.locator(':scope > summary').click();await value.locator('[data-rg="task"]').first().click();
  await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
  const valueTask=page.locator('article[data-org-task]').filter({hasText:'保留价值演示'});
  assert.equal(await valueTask.evaluate(e=>e.classList.contains('is-stale')),false,'当前任务不能误标旧版本');
  const keep=valueTask.locator('[data-org-action="disposition"][data-value="keep"]');
  await keep.waitFor();await keep.click();
  await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('保留'));
  checks.push('价值待判断条目明确展示保留操作，按真实接口恢复后续提炼');
  const routes=[['#home','[data-workflow-overview]'],['#knowledge/organization','.organization-panel'],['#knowledge/cleaning','.cleaning-review'],['#knowledge/projects','.project-list'],['#knowledge/qa','.memory-manager'],['#knowledge/skill','#stage-instructions'],['#knowledge/unassigned','.history-review'],['#knowledge/intake','#content'],['#progress','[data-slot="progress-browser"] [data-results]'],['#principles','.product-diagram'],['#knowledge/rules','#min-confidence'],['#knowledge/library?project=evo','.project-document'],['#experiences','[data-slot=experience-browser]'],['trust','.trust-main']];
  for(const width of [1920,1440,768,390]){
   await page.setViewportSize({width,height:1000});
   for(const [route,selector] of routes){
    await go(route,selector);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`overflow ${width} ${route}`);
    const tiny=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})&&[...e.childNodes].some(n=>n.nodeType===3&&n.textContent.trim())&&parseFloat(getComputedStyle(e).fontSize)<14).map(e=>e.textContent.trim().slice(0,40)));
    assert.deepEqual(tiny,[],`small text ${width} ${route}`);
    if(width>=1440&&route!=='trust'){const b=await page.locator('.signal-main').boundingBox();assert.ok(b.width>width-300,`not full width ${width} ${route}`);}
   }
   await go('#home','[data-flow-health]');await page.screenshot({path:`/tmp/evolvmem-workspace-home-${width}.png`,fullPage:true});
   await go('#knowledge/organization','.organization-panel');await page.screenshot({path:`/tmp/evolvmem-workspace-review-${width}.png`,fullPage:true});
  }
  checks.push('十四个页面在四种屏宽均无全页横向溢出与小字号文字，宽屏充分利用工作区');
  await page.route('**/api/knowledge/organization/metrics',route=>route.fulfill({status:503,contentType:'application/json',body:'{"error":"temporarily_unavailable"}'}));
  await go('#home','[data-flow-health]');await page.waitForFunction(()=>document.querySelector('[data-workflow-overview]')?.textContent.includes('统计暂不可用'));
  assert.match(await page.locator('[data-flow-link="review"]').innerText(),/—/);
  checks.push('接口不可用显示未知，不把读取失败伪装为零待办');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
