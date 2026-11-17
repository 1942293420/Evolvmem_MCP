// Uses the synthetic knowledge_fixture.py server. Never point at a live database.
// 验收：待确认原因筛选传参、卡片显示真实原因、无页面脚本异常。
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(12000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[],checks=[],requests=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.url().includes('/api/knowledge/qa?'))requests.push(r.url());});
 const done=async()=>{await page.waitForLoadState('networkidle');return page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');};
 const go=async(hash,selector)=>{await done();await page.goto(base+'/'+hash);await page.waitForSelector(selector);await done();};
 const click=async(selector)=>{await page.locator(selector).first().click();await done();};
 try{
  await go('#knowledge/qa','#qa-project-filter');
  await page.locator('#qa-project-filter').selectOption('evo');await done();
  await page.waitForSelector('.qa-card');
  // 与夹具中有效问答同问题、同条件但不同答案：走真实保存路径进入待确认。
  await click('[data-memory-action="new"]');
  await page.locator('#qa-question').fill('修改界面前应先明确什么？');
  await page.locator('#qa-answer').fill('先明确操作目标与验收条件，再动手修改。');
  await page.locator('#qa-category').selectOption('project_convention');
  await page.locator('#qa-trigger').fill('修改界面时');
  await click('[data-memory-action="draft"]');
  await page.waitForFunction(()=>document.querySelector('.qa-cards')?.textContent.includes('冲突待确认'));
  assert.match(await page.locator('.qa-cards').innerText(),/冲突待确认：/,{message:'候选卡片必须显示实际原因而不是泛化文案'});
  const options=await page.locator('#qa-reason-filter option').allTextContents();
  assert.ok(options.some(text=>text.includes('冲突待确认')&&text.includes('1')),`原因筛选应显示中文原因及组数：${options}`);
  checks.push('候选卡片显示真实待确认原因，筛选选项含中文原因与组数');
  requests.length=0;
  await page.locator('#qa-reason-filter').selectOption('conflict');await done();
  assert.ok(requests.some(url=>url.includes('reason_code=conflict')),`切换原因必须带上 reason_code：${requests}`);
  assert.equal(await page.locator('.qa-card').count(),1);
  assert.match(await page.locator('.qa-cards').innerText(),/冲突待确认：/);
  assert.doesNotMatch(await page.locator('.qa-cards').innerText(),/可用问答/);
  checks.push('原因筛选重置后按 reason_code 传参，只显示同类待确认问答');
  // 切到没有该原因的项目：兜底选项仍须显示中文标签，不能暴露内部 code。
  requests.length=0;
  await page.locator('#qa-project-filter').selectOption('dsh-a');await done();
  assert.ok(requests.some(url=>url.includes('reason_code=conflict')),`切换项目仍应带上所选原因：${requests}`);
  const selected=await page.locator('#qa-reason-filter').evaluate(select=>select.options[select.selectedIndex]?.textContent||'');
  assert.match(selected,/冲突待确认/);
  assert.doesNotMatch(selected,/conflict/);
  checks.push('切换项目后已选原因仍显示中文标签（0 组），不暴露内部 code');
  await page.locator('#qa-project-filter').selectOption('evo');await done();
  await page.screenshot({path:'/tmp/evo-review-reasons-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:1000});await done();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'reason filter overflow 390');
  await page.screenshot({path:'/tmp/evo-review-reasons-mobile.png',fullPage:true});
  checks.push('原因筛选在手机宽度不产生横向溢出');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks,page_errors:errors},null,2));
 }catch(error){console.error('CHECKS',checks,'TOAST',await page.locator('#toast').textContent().catch(()=>''));await page.screenshot({path:'/tmp/evo-review-reasons-failure.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
