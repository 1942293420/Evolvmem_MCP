// Real HTTP + UI against knowledge_fixture.py with EVOLVMEM_ORGANIZATION_FIXTURE=1.
// Synthetic data only; verifies background navigation, per-unit correction,
// grouped bulk staging, guidance scope/enable, arrival mode and the diagram.
const assert=require('node:assert/strict');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);
const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
const done=async()=>{await page.waitForLoadState('networkidle');await page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');};
const go=async(hash,selector)=>{await page.goto(base+'/'+hash);await page.waitForSelector(selector);await done();};
const click=async s=>{await page.locator(s).first().click();await done();};
try{
 await go('#knowledge/organization','#organization-panel');
 await page.waitForSelector('.organization-task');
 // The card must show a readable source title, not only archive:ID.
 assert.match(await page.locator('.organization-task').first().innerText(),/kimi · organization-/);
 checks.push('任务卡显示可读来源标题与来源标识');
 // Leaving and returning keeps the tasks queryable.
 await click('.history-tabs [data-view="projects"]');await page.waitForSelector('.project-list');
 await click('.history-tabs [data-view="organization"]');await page.waitForSelector('.organization-task');
 assert.equal(errors.length,0,'page errors after navigation: '+errors.join('; '));
 checks.push('后台任务页可离开再返回，任务仍可查询');
 // Open the two-unit review task once and inspect its grouped decisions.
 const reviewTask=page.locator('.organization-task[data-org-review="2"]').first();
 await reviewTask.locator('button[data-org-action="toggle"]').click();
 await page.waitForSelector('.organization-task[data-org-review="2"] .organization-group[data-org-group="review"]',{state:'attached'});
 const group=page.locator('.organization-task[data-org-review="2"] .organization-group[data-org-group="review"]');
 assert.equal(await group.locator('.organization-unit').count(),2,'two review units expected');
 checks.push('任务展示单元、状态与逐条决定');
 // Group convenience: selecting one item stages only that item.
 await group.locator('[data-org-select-unit]').first().check();
 await group.locator('[data-org-group-project]').selectOption('evo');
 await group.locator('[data-org-action="stage-group"]').click();
 await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('预填项目'));
 assert.equal(await group.locator('[data-org-project]').first().inputValue(),'evo');
 assert.equal(await group.locator('[data-org-project]').nth(1).inputValue(),'');
 assert.equal(await group.locator('.organization-unit.is-manual').count(),0);
 checks.push('组内便捷选择只预填所选条目，未选项不受影响');
 // Drafts survive a view switch exactly like the cleaning page.
 await page.locator('[data-org-guidance]').first().fill('没有项目线索时按 Evo 演示项目处理');
 await click('.history-tabs [data-view="projects"]');await page.waitForSelector('.project-list');
 await click('.history-tabs [data-view="organization"]');await page.waitForSelector('.organization-task');
 const taskId=await reviewTask.getAttribute('data-org-task');
 const stableTask=`.organization-task[data-org-task="${taskId}"]`;
 const backTask=page.locator(stableTask);
 assert.equal(await backTask.locator('[data-org-guidance]').first().inputValue(),'没有项目线索时按 Evo 演示项目处理');
 checks.push('离开页面再返回，单元草稿仍在');
 // Bulk save of the selected decisions with an explicit future-scope guidance.
 await backTask.locator('[data-org-scope]').first().selectOption('future');
 await backTask.locator('[data-org-condition]').first().fill('讨论导出时');
 await backTask.locator('[data-org-exceptions]').first().fill('已登记其他项目时除外');
 await click(stableTask+' [data-org-action="correct-selected"]');
 assert.match(await page.locator('#org-notice').innerText(),/已保存 1 条，失败 0 条/);
 assert.match(await page.locator('.organization-guidance').innerText(),/以后同类适用/);
 assert.match(await page.locator('.organization-guidance').innerText(),/讨论导出时/);
 // A future rule without a narrow condition is stored as a suggestion only.
 await backTask.locator('[data-org-group="review"] [data-org-guidance]').first().fill('资料都要整理');
 await backTask.locator('[data-org-group="review"] [data-org-scope]').first().selectOption('future');
 await backTask.locator('[data-org-group="review"] [data-org-condition]').first().fill('资料');
 await click(stableTask+' [data-org-group="review"] [data-org-action="correct"]');
 await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('已保存 1 条'));
 assert.match(await page.locator('.organization-guidance').innerText(),/条件太宽，仅作建议/);
 checks.push('批量保存所选；过宽条件只作为建议保存');
 // The correction persists across a real reload and stays a manual decision.
 await page.reload();await page.waitForSelector('#organization-panel');await done();
 await page.waitForSelector('.organization-task');
 await page.locator(stableTask+' button[data-org-action="toggle"]').click();
 await page.waitForSelector(stableTask+' .organization-unit.is-manual');
 assert.match(await page.locator('.organization-unit.is-manual').first().innerText(),/人工已确认/);
 checks.push('修正与指导读回；人工决定标记保留');
 // Guidance can be disabled and the boolean is not inverted.
 const enabledBefore=await page.locator('.organization-guidance [data-enabled="1"]').count();
 assert.ok(enabledBefore>=1,'an enabled guidance row is expected');
 await page.locator('.organization-guidance [data-org-action="toggle-guidance"]').first().click();
 await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('已停用'));
 assert.equal(await page.locator('.organization-guidance [data-enabled="0"]').count(),1);
 checks.push('指导可停用（布尔值不反向）');
 // Arrival mode toggles independently of queued work.
 await page.locator('[data-org-arrival]').check();
 await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('已开启'));
 assert.match(await page.locator('#org-arrival-state').innerText(),/已开启/);
 await page.locator('[data-org-arrival]').uncheck();
 await page.waitForFunction(()=>document.querySelector('#org-notice')?.textContent.includes('已关闭'));
 assert.ok(await page.locator('.organization-task').count()>=1,'queued work survives pausing');
 checks.push('可开启/暂停新资料自动处理，已排队任务不受影响');
 // Paging and counts are usable for a larger backlog.
 assert.match(await page.locator('#org-page').innerText(),/共 \d+ 个任务 · 第 1 \/ \d+ 页/);
 assert.ok(await page.locator('#org-filter option').count()>=7);
 checks.push('任务队列有分页、筛选与计数');
 // The product diagram documents the shipped background flow.
 await go('#principles','.product-diagram svg');
 await click('[data-principle="organization"]');
 assert.match(await page.locator('#principle-title').innerText(),/整篇自动整理/);
 assert.ok((await page.locator('.product-diagram svg').innerHTML()).includes('单个有界工作线程'));
 assert.ok(await page.locator('.product-diagram svg').evaluate(svg=>{const b=svg.getBBox(),v=svg.viewBox.baseVal;return b.x>=0&&b.x+b.width<=v.width;}),'all diagram content fits its viewBox');
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'organization diagram overflow 1440');
 await page.screenshot({path:'/tmp/evo-product-organization.png',fullPage:true});
 checks.push('自动整理活动图同步实际行为且不溢出');
 await page.setViewportSize({width:390,height:1000});
 await go('#knowledge/organization','#organization-panel');
 await page.waitForSelector('.organization-task');
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'organization overflow 390');
 await page.screenshot({path:'/tmp/evo-organization-mobile.png',fullPage:true});
 await page.setViewportSize({width:1440,height:1000});
 await page.screenshot({path:'/tmp/evo-organization-desktop.png',fullPage:true});
 assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors},null,2));
}catch(error){await page.screenshot({path:'/tmp/evo-organization-failed.png',fullPage:true});console.error('COMPLETED',checks,'ERRORS',errors);throw error;}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
