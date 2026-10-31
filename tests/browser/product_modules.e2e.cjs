async function revealRule(page,selector){await page.waitForSelector(selector,{state:'attached'});const rule=page.locator('.history-rules');if(await rule.count()&&!await rule.evaluate(el=>el.open))await rule.locator(':scope > summary').click();await page.waitForSelector(selector);}
// Real HTTP + UI against knowledge_fixture.py (synthetic data only).
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const desktopOnly=process.env.EVOLVMEM_DESKTOP_ONLY==='1',output=process.env.EVOLVMEM_SCREENSHOT_DIR||path.join(os.tmpdir(),'evolvmem-product-qa');fs.mkdirSync(output,{recursive:true});
const screenshot=async(page,name)=>{await page.evaluate(()=>{document.activeElement?.blur();window.scrollTo(0,0);});await page.screenshot({path:path.join(output,name),fullPage:true});};
async function diagramBounds(page,kind){
 const outside=await page.locator('.product-diagram svg').evaluate(svg=>{const v=svg.viewBox.baseVal;return [...svg.querySelectorAll('[data-node]')].filter(node=>{const b=node.getBBox();return b.x<v.x-1||b.y<v.y-1||b.x+b.width>v.x+v.width+1||b.y+b.height>v.y+v.height+1;}).map(node=>node.dataset.node);});
 assert.deepEqual(outside,[],kind+' nodes must fit the SVG viewBox');
 if(kind==='modules'){const overflow=await page.locator('[data-node]').evaluateAll(nodes=>nodes.filter(node=>{const box=node.querySelector('rect').getBBox();return [...node.querySelectorAll('text')].some(text=>{const b=text.getBBox();return b.x<box.x||b.x+b.width>box.x+box.width;});}).map(node=>node.dataset.node));assert.deepEqual(overflow,[],'module labels must fit their cards');}
}
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});const qaProject='qa-'+Date.now(),caseProject='case-'+Date.now();const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
const done=async()=>{await page.waitForLoadState('networkidle');await page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');};
const go=async(hash,selector)=>{await page.goto(base+'/'+hash);if(['#clean-instructions','#history-instructions'].includes(selector))await revealRule(page,selector);else await page.waitForSelector(selector);await done();};const click=async s=>{await page.locator(s).first().click();await done();};
try{
 if(process.env.EVOLVMEM_PRINCIPLES_ONLY!=='1'){
 await go('#knowledge/qa','#qa-project-filter');await click('[data-memory-action="new"]');await page.locator('#qa-question').fill('怎样在经验中新增项目？');await page.locator('#qa-answer').fill('在问答编辑处创建项目，创建成功后自动选中。');await page.locator('[data-memory-action="project-new"]').click();await page.waitForSelector('#new-project-id');await page.locator('#new-project-id').fill(qaProject);await page.locator('#new-project-name').fill('问答内新增项目');await page.locator('#new-project-aliases').fill(qaProject+'-alias');await click('.project-create-dialog [type="submit"]');await page.waitForSelector('.project-create-dialog',{state:'detached'});assert.equal(await page.locator('.memory-dialog [name=projects]:checked').inputValue(),qaProject);assert.equal(await page.locator('#qa-question').inputValue(),'怎样在经验中新增项目？');await click('[data-memory-action="publish"]');await go('#knowledge/projects','.project-list');assert.match(await page.locator('.project-list').innerText(),/问答内新增项目/);checks.push('问答原位新增项目、自动选中、草稿保留、入库可查');
 await go('#experiences','[data-case-project-new]');await click('[data-case-project-new]');await page.locator('#new-project-id').fill(caseProject);await page.locator('#new-project-name').fill('案例内新增项目');await click('.project-create-dialog [type="submit"]');await page.waitForSelector('.project-create-dialog',{state:'detached'});assert.equal(await page.locator('[data-slot="experience-browser"] [data-filter="project"]').inputValue(),caseProject);checks.push('经验案例原位新增项目与筛选选中');
 await go('#progress','[data-slot="progress-browser"] [data-results]');assert.equal(await page.locator('[data-progress-mode="summary"]').count(),0);assert.match(await page.locator('[data-slot="progress-browser"] [data-filter="status"]').innerText(),/全部任务/);await go('#knowledge/library?project=evo&lane=summary','.project-document');assert.match(await page.locator('.project-document').innerText(),/项目摘要/);await page.getByText('AI 项目摘要 · 已生成',{exact:true}).click();assert.match(await page.locator('.project-document').innerText(),/项目摘要应归入项目历史/);checks.push('任务断点只列任务，项目摘要归入历史');
 await go('#knowledge/projects','.history-tabs');assert.match(await page.locator('.history-tabs').innerText(),/已整理/);await click('.history-tabs [data-view="unassigned"]');await revealRule(page,'#history-instructions');assert.ok((await page.locator('#history-instructions').inputValue()).length>10);
 const instructions=await page.locator('#history-instructions').inputValue();await page.locator('#history-instructions').fill(instructions+'\n演示资料按原话确认，多个项目保留待确认。');await click('[data-history-action="rules-save"]');await page.reload();await revealRule(page,'#history-instructions');assert.match(await page.locator('#history-instructions').inputValue(),/演示资料按原话确认/);
 await page.locator('#history-select-all').check();await click('[data-history-action="preview"]');assert.match(await page.locator('#history-notice').innerText(),/尚未保存分类/);assert.equal(await page.locator('[data-history-row]').count(),3);assert.equal(await page.locator('[data-history-project]').first().inputValue(),'evo');
 await page.locator('[data-history-project]').first().selectOption('dsh-a');await click('[data-history-action="detail"]');await page.waitForSelector('.history-source-dialog');assert.match(await page.locator('.history-source-dialog').innerText(),/Evo/);await page.locator('.history-source-dialog button').click();await screenshot(page,'evo-history-classification-desktop.png');
 if(!desktopOnly){await page.setViewportSize({width:390,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'history overflow 390');await screenshot(page,'evo-history-classification-mobile.png');await page.setViewportSize({width:1440,height:1000});}
 await click('[data-history-action="save-all"]');assert.match(await page.locator('#history-notice').innerText(),/成功 3 条，失败 0 条/);assert.equal(await page.locator('[data-history-row]').count(),0);await click('.history-tabs [data-view="projects"]');await page.waitForSelector('.project-list');checks.push('项目历史已整理与未归属分栏，规则保存读回、真实接口 AI 预览、手动修正、批量分类、桌面页面');
 await go('#knowledge/skill','[data-pipeline-stage]');assert.equal(await page.locator('[data-pipeline-stage]').count(),1);assert.equal(await page.locator('[data-pipeline-stage="extraction"]').count(),1);assert.match(await page.locator('#content').innerText(),/经验提取与验证/);assert.equal(await page.locator('a[data-view="learning"]').count(),0);checks.push('提取与验证合并，协作学习入口移除');
 const admission=await page.locator('#stage-admission').inputValue();await page.locator('#stage-admission').fill(admission+'\n方法必须核对实际验证依据。');await page.locator('#stage-min_chars').fill('12');await click('[data-pipeline-action="save"]');await page.reload();await page.waitForSelector('#stage-admission');assert.match(await page.locator('#stage-admission').inputValue(),/方法必须核对实际验证依据/);assert.equal(await page.locator('#stage-min_chars').inputValue(),'12');checks.push('提取说明与准入条件统一保存并读回');
 await page.locator('#stage-instructions').fill('未保存的临时规则');page.once('dialog',d=>d.dismiss());await click('.nav-primary-link[data-nav-group="project"]');assert.equal(await page.locator('#stage-instructions').inputValue(),'未保存的临时规则');page.once('dialog',d=>d.accept());await click('.nav-primary-link[data-nav-group="project"]');await page.waitForFunction(()=>!location.hash.startsWith('#knowledge/skill'));await go('#knowledge/skill','#stage-instructions');checks.push('统一规则编辑的离开保护');
 const download=page.waitForEvent('download');await click('[data-pipeline-action="download"]');assert.equal((await download).suggestedFilename(),'SKILL.md');
 await page.locator('#stage-sample').fill('用户：我想在经验里直接加项目，不要让我到处找。');await page.locator('#stage-project').selectOption('evo');await click('[data-pipeline-action="verify"]');assert.match(await page.locator('#stage-result').innerText(),/放入待确认 [1-9]\d* 条/);checks.push('统一环节提取候选，保存不冒充验证成功');
 await go('#knowledge/learning','.panel');assert.match(await page.locator('#content').innerText(),/协作学习已停用/);assert.equal(await page.locator('[data-learning-action]').count(),0);checks.push('旧协作页面提示停用，保留数据');
 }
 for(const path of ['/workflow','/workflow-diagram','/architecture']){await page.goto(base+path);await page.waitForSelector('.product-diagram svg');assert.ok(page.url().endsWith('#principles'));assert.equal(await page.locator('.signal-masthead').count(),1);}checks.push('原工作原理与架构地址统一进入主界面');
 for(const width of (desktopOnly?[1280,1440]:[390,768,1024,1440])){await page.setViewportSize({width,height:1000});await go('#knowledge/skill','[data-pipeline-stage]');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`skill overflow ${width}`);if(width===1440)await screenshot(page,'evo-skills-desktop.png');await go('#principles','.product-diagram svg');for(const kind of ['modules','restructure','capabilities','flow','activity','organization','recall','health','dataflow','storage']){await click(`[data-principle="${kind}"]`);await diagramBounds(page,kind);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`${kind} overflow ${width}`);if(kind==='recall'){assert.match(await page.locator('.product-diagram').innerText(),/重新排序/);assert.match(await page.locator('.product-diagram').innerText(),/保留各自的来源/);}if(width===1440)await screenshot(page,`evo-product-${kind}.png`);}if(width===390)await screenshot(page,'evo-product-mobile.png');}checks.push(desktopOnly?'十个业务页面、十类业务图与 Skill 页在1280/1440桌面可用':'业务图与 Skill 页多屏宽可用');
 await page.setViewportSize({width:1440,height:1000});await go('#principles','.product-diagram svg');
 await click('[data-principle="health"]');
 assert.match(await page.locator('.diagram-goal').innerText(),/其他 Context 操作仍沿用原检查/);
 await click('[data-node="candidate"]');assert.match(await page.locator('[data-node-detail]').innerText(),/返回前重新检查必要证明.*旧诊断不能认可已撤回或损坏的条目/);
 await click('[data-node="status"]');assert.match(await page.locator('[data-node-detail]').innerText(),/revision 与 health 不等待业务锁.*旧诊断不是当前全库正常的证明/);
 await screenshot(page,'evo-product-health-status-detail.png');
 await click('[data-node="result"]');assert.match(await page.locator('[data-node-detail]').innerText(),/能力查询仍共用业务锁.*不宣称所有业务并发/);
 await click('[data-principle="modules"]');await click('[data-node="rules"]');
 assert.match(await page.locator('[data-node-detail]').innerText(),/处理规则与样例预览/);await screenshot(page,'evo-product-modules-last-row-detail.png');
 await click('[data-principle="restructure"]');await click('[data-node="scope"]');
 assert.match(await page.locator('[data-node-detail]').innerText(),/相似度只缩小候选.*人工归属与撤回优先.*未知模块不自动激活/);
 await screenshot(page,'evo-product-restructure-scope-detail.png');
 await click('[data-node="gate"]');assert.match(await page.locator('[data-node-detail]').innerText(),/一个来源.*不能借用另一来源.*旧资料.*尚未评估/);
 await page.locator('[data-node="recall"]').focus();await page.keyboard.press('Enter');await done();await screenshot(page,'evo-product-restructure-bottom-detail.png');
 await click('[data-principle="capabilities"]');await click('[data-node="source"]');
 assert.match(await page.locator('[data-node-detail]').innerText(),/项目归属、参考价值和方法结果分别显示.*业务模块.*技术分类/);
 await screenshot(page,'evo-product-capabilities-source-detail.png');
 await click('[data-node="saved"]');assert.match(await page.locator('[data-node-detail]').innerText(),/分类确认只改变技术分类.*仍待核对/);
 await screenshot(page,'evo-product-capabilities-classification-detail.png');
 checks.push('治理新图来源与价值同源门槛、业务模块/技术分类分离、分类确认不等于验证及节点边界');
 await click('[data-principle="recall"]');await click('[data-node="knowledge"]');
 assert.match(await page.locator('[data-node-detail]').innerText(),/context_project_recall.*project_archive_get.*capability_search/);
 assert.match(await page.locator('[data-node-detail]').innerText(),/输入/);
 assert.match(await page.locator('[data-node-detail]').innerText(),/输出/);
 await click('[data-principle="dataflow"]');
 assert.match(await page.locator('.product-diagram').innerText(),/项目知识目录与历史/);
 assert.match(await page.locator('.product-diagram').innerText(),/可用问答/);
 await click('[data-principle="activity"]');
 assert.ok(await page.locator('.diagram-start').count());assert.ok(await page.locator('.diagram-end').count());
 await click('[data-principle="organization"]');
 assert.equal(await page.locator('[data-lane]').count(),3);
 await page.locator('[data-route-select]').selectOption('review');
 await click('[data-trace-next]');assert.match(await page.locator('[data-trace-status]').innerText(),/1\//);
 await click('[data-trace-play]');await page.waitForFunction(()=>document.querySelector('[data-trace-play]')?.textContent==='暂停演示');
 await click('[data-principle="recall"]');assert.equal(await page.locator('[data-trace-play]').innerText(),'演示路径');
 await page.emulateMedia({reducedMotion:'reduce'});await page.waitForFunction(()=>document.querySelector('[data-trace-play]')?.disabled);assert.equal(await page.locator('[data-trace-play]').isDisabled(),true);
 await click('[data-trace-next]');assert.match(await page.locator('[data-trace-status]').innerText(),/1\//);
 await page.emulateMedia({reducedMotion:'no-preference'});
 await go('#knowledge/qa','#qa-project-filter');await go('#principles','.product-diagram svg');
 assert.equal(await page.locator('[data-principle="recall"]').getAttribute('aria-pressed'),'true');
 checks.push('业务节点输入输出、数据流、三泳道、活动起止、分支演示、减少动画与切页状态');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({checks,errors},null,2));
}catch(error){await screenshot(page,'evo-modules-failed.png');console.error('COMPLETED',checks,'ERRORS',errors);throw error;}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1)});
