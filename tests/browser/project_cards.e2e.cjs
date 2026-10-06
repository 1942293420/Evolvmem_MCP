// Run against knowledge_fixture.py with EVOLVMEM_CARD_FIXTURE=1 (synthetic data).
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[],checks=[];
 page.on('pageerror',e=>errors.push(e.message));
 const done=async()=>{await page.waitForLoadState('networkidle');await page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');};
 const go=async(hash,selector)=>{await page.goto(base+'/'+hash);await page.waitForSelector(selector);await done();};
 const click=async selector=>{await page.locator(selector).first().click();await done();};
 const ids=()=>page.locator('[data-project-card]').evaluateAll(xs=>xs.map(x=>x.dataset.projectCard));
 try{
  await go('#knowledge/projects','#project-search');
  for(const [query,expected] of [['  CARDS-ALPHA  ',['cards-alpha']],['产品设计',['cards-alpha']],['工具箱',['cards-gamma']],['cards 售后',['cards-beta']]]){
   await page.locator('#project-search').fill(query);assert.deepEqual(await ids(),expected);
   assert.ok(await page.locator('#project-search').evaluate(e=>e===document.activeElement));
  }
  await page.locator('#project-search').fill('没有匹配项目');assert.equal((await ids()).length,0);assert.match(await page.locator('.project-empty').innerText(),/没有找到/);
  await click('.project-empty [data-action="project-search-clear"]');assert.ok((await ids()).length>=6);assert.equal(await page.locator('#project-search').inputValue(),'');checks.push('名称、标识、别名、多词搜索；无结果与清除；输入保持焦点');
  await page.locator('#project-search').fill('cards');
  for(const [sort,expected] of [['recent',['gamma','beta','alpha']],['name',['alpha','gamma','beta']],['materials',['beta','gamma','alpha']],['pending',['gamma','alpha','beta']]]){
   await page.locator('#project-sort').selectOption(sort);assert.deepEqual(await ids(),expected.map(x=>'cards-'+x));
  }
  await page.reload();await page.waitForSelector('#project-search');await done();assert.equal(await page.locator('#project-search').inputValue(),'cards');assert.equal(await page.locator('#project-sort').inputValue(),'pending');
  // A click in the row body must open the project, not only its name link.
  const card=page.locator('[data-project-card="cards-alpha"]');
  await card.locator('time').click();await done();await page.waitForSelector('.project-document');assert.ok(page.url().includes('project=cards-alpha'));
  await page.goBack();await page.waitForSelector('#project-search');await done();assert.equal(await page.locator('#project-search').inputValue(),'cards');assert.equal(await page.locator('#project-sort').inputValue(),'pending');checks.push('四种排序、刷新和返回保留条件、整张卡片可打开');
  await click('[data-action="project-edit"][data-project="cards-alpha"]');await page.waitForSelector('#project-name');assert.ok(page.url().includes('/projects'));
  await page.locator('#project-name').fill('北辰 · 产品设计与需求协作管理');await click('[data-action="project-save"]');assert.match(await page.locator('[data-project-card="cards-alpha"] .project-open-main').innerText(),/需求协作/);
  await click('[data-project-card="cards-alpha"] [data-action="project-queue"]');await page.waitForSelector('#filter-queue');assert.equal(await page.locator('#filter-project').inputValue(),'cards-alpha');
  assert.equal(await page.locator('.knowledge-flow span,.knowledge-flow p,#content>.intro').count(),0);
  assert.equal(await page.locator('.intake-actionbar button').count(),2);assert.equal((await page.locator('[data-action="organize-page"]').innerText()).trim(),'✦ AI 整理');
  await click('[data-action="organize-page"]');await page.waitForSelector('.proposals');assert.match(await page.locator('#dialog-content').innerText(),/整理结果预览/);await click('#dialog [data-action="close"]');
  await click('.intake-actionbar [data-view="skill"]');await page.waitForSelector('#stage-instructions');checks.push('项目编辑和待确认入口独立可点；AI 整理先预览；Skill 按钮可跳转；说明小字移除');
  await go('#knowledge/projects','#project-search');await click('.project-utilities [data-project="__global__"]');await page.waitForSelector('.project-document');assert.ok(page.url().includes('__global__'));
  await go('#knowledge/projects','#project-search');await click('.project-utilities [data-view="unassigned"]');await page.waitForSelector('#history-instructions');await go('#knowledge/library','#search');
  for(const [view,selector] of [['library','#search'],['rules','#min-confidence'],['skill','#stage-instructions'],['intake','#filter-queue']]){
   await click(`.knowledge-tabs [data-view="${view}"]`);await page.waitForSelector(selector);assert.equal(await page.locator(`.knowledge-tabs [data-view="${view}"]`).getAttribute('aria-current'),'page');
  }checks.push('全局与未归属辅助入口、四个顶部导航按钮及选中状态');
  for(const width of [390,768,1024,1440]){
   await page.setViewportSize({width,height:1000});await go('#knowledge/projects','#project-search');
   assert.equal(await page.locator('[data-project-card="old-evo"]').count(),0);
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`cards overflow ${width}`);
   assert.ok(await page.locator('.project-list tbody tr[data-project-card]').count()>=1,`rows visible ${width}`);
   await page.screenshot({path:`/tmp/evo-project-cards-${width}.png`,fullPage:true});
   await go('#knowledge/intake','#filter-queue');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`intake overflow ${width}`);
   const tab=page.locator('.knowledge-tabs [data-view="intake"]');assert.notEqual(await tab.evaluate(e=>getComputedStyle(e).backgroundColor),'rgba(0, 0, 0, 0)');
   await click('.intake-actionbar [data-view="skill"]');await page.waitForSelector('#stage-instructions');await click('.knowledge-tabs [data-view="intake"]');await page.waitForSelector('#filter-queue');
   await page.screenshot({path:`/tmp/evo-intake-${width}.png`,fullPage:true});
  }checks.push('四种屏宽的卡片列数、导航与操作按钮、无页面横向溢出');
  // Narrow desktop columns must also fit real-world larger totals and long names.
  await page.route('**/api/knowledge/projects',async route=>{
   const response=await route.fetch();const result=await response.json();
   result.projects=result.projects.filter(p=>p.project.startsWith('cards-')).map(p=>({...p,total:12345,pending:9876,display_name:'项目名称很长时依然可以识别与打开历史资料',aliases:['很长的业务别名应该清晰截断而不会撑破卡片','中文别名','另一个别名']}));
   await route.fulfill({response,json:result});
  });
  for(const width of [390,768,1101]){await page.setViewportSize({width,height:1000});await go('#knowledge/projects?sort=unknown','#project-search');assert.equal(await page.locator('#project-sort').inputValue(),'recent');assert.ok(await page.locator('[data-project-card]').evaluateAll(xs=>xs.every(x=>[...x.cells].every(td=>td.scrollWidth<=td.clientWidth+2))),`large counts overflow ${width}`);}
  await page.unroute('**/api/knowledge/projects');
  await page.route('**/api/knowledge/projects',route=>route.fulfill({json:{projects:[],pending:0,total:0,unassigned:{}}}));await page.reload();await page.waitForSelector('.project-empty');await done();await click('.project-empty [data-action="project-new"]');await page.waitForSelector('#project-id');await click('#dialog [data-action="close"]');await page.unroute('**/api/knowledge/projects');checks.push('长名称、别名、大资料数、无效排序回退及空项目添加入口');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,page_errors:errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-project-cards-failed.png',fullPage:true});console.error('CHECKS',checks,'ERRORS',errors);throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
