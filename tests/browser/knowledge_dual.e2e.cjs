const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(12000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478';
 await page.goto(base+'/#knowledge/projects');await page.getByRole('link',{name:'Evo 演示项目',exact:true}).click();
 await page.waitForSelector('.memory-lanes');assert.match(await page.locator('.project-document').innerText(),/本次对话/);
 assert.ok(!(await page.locator('.project-document').innerText()).includes('修改界面前先明确验收条件。'));
 const tab=name=>page.locator('.memory-lanes').getByRole('button',{name,exact:true});
 await tab('历史记录').click();await page.getByRole('button',{name:'清洗旧对话入库 · 1 次',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.project-conversations')?.textContent.includes('清洗正文已入库')&&!document.querySelector('[data-memory-action="migrate"]'));
 await page.locator('.project-conversations [data-memory-action="conversation"]').last().click();
 await page.waitForSelector('.memory-dialog[open]');assert.match(await page.locator('.memory-dialog').innerText(),/数据库/);assert.ok(!(await page.locator('.memory-dialog').innerText()).includes('HIDDEN_TOOL_LOG'));
 await page.locator('.memory-dialog [data-memory-action="close"]').last().click();
 await page.screenshot({path:'/tmp/evo-dual-history.png',fullPage:true});
 await tab('经验问答').click();await page.waitForSelector('.qa-card');
 const card=page.locator('.qa-card').filter({hasText:'修改界面前应先明确什么？'});
 await card.getByRole('button',{name:'查看 / 编辑问答'}).click();
 await page.locator('#qa-question').fill('界面开发应该先明确什么？');
 await page.locator('#qa-trigger').fill('界面功能发生变化时');
 await page.locator('.memory-dialog').getByRole('button',{name:'确认入库',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('.memory-dialog')?.open&&document.querySelector('.qa-cards')?.textContent.includes('界面开发应该先明确什么？'));
 await page.getByRole('button',{name:'＋ 新增问答',exact:true}).click();
 await page.locator('#qa-question').fill('界面开发应该先明确什么？');await page.locator('#qa-answer').fill('先画出交互草图，再核对验收条件。');await page.locator('#qa-category').selectOption('project_convention');await page.locator('#qa-trigger').fill('界面功能发生变化时');
 await page.locator('.memory-dialog').getByRole('button',{name:'确认入库',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#toast')?.textContent.includes('已有不同答案'));
 assert.ok(await page.locator('.memory-dialog').isVisible());await page.locator('#qa-replace').check();
 await page.locator('.memory-dialog').getByRole('button',{name:'确认入库',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('.memory-dialog')?.open&&document.querySelector('.qa-cards')?.textContent.includes('先画出交互草图'));
 assert.equal(await page.locator('.qa-card').filter({hasText:'界面开发应该先明确什么？'}).count(),1);
 await page.screenshot({path:'/tmp/evo-dual-qa-desktop.png',fullPage:true});
 const hist=await (await page.request.get(base+'/api/knowledge/recall?project=evo&kind=history&query=以前讨论')).json();
 const qa=await (await page.request.get(base+'/api/knowledge/recall?project=evo&kind=experience&query=界面开发')).json();
 assert.equal(hist.qa.length,0);assert.ok(hist.history.length);assert.equal(qa.history.length,0);assert.ok(qa.qa.length);
 for(const width of [390,768,1024]){await page.setViewportSize({width,height:900});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await tab('历史记录').click();await tab('经验问答').click();if(width===390)await page.screenshot({path:'/tmp/evo-dual-qa-mobile.png',fullPage:true});}
 await page.waitForSelector('.knowledge-workspace[aria-busy="false"]');await page.goto(base+'/#knowledge/rules');await page.getByRole('button',{name:'2 · 试运行效果',exact:true}).click();await page.locator('#extraction-project').selectOption('evo');
 await page.getByRole('button',{name:'比较提炼结果',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#extraction-result')?.textContent.includes('答：以后，修改界面时先明确操作目标和验收条件。'));
 assert.match(await page.locator('#extraction-result').innerText(),/答：以后/);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({history_database_migration:true,clean_dialogue:true,separate_qa:true,edit_publish:true,conflict_review_and_replace:true,two_lane_recall:true,qa_extraction_preview:true,viewports:[390,768,1024,1440],page_errors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
