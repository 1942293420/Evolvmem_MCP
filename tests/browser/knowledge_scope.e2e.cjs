// Synthetic fixture only: business criteria, shared scope, one edited Q&A.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(20000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39521';
 const api=async(route,body)=>page.evaluate(async({route,body})=>{const r=await fetch('/api/knowledge/'+route,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const v=await r.json();if(!r.ok)throw Error(JSON.stringify(v));return v;},{route,body});
 try{
  await page.goto(base+'/#knowledge/organization');await page.waitForSelector('[data-rg-list]');
  await api('projects',{project:'scope-second',display_name:'第二个演示项目'});await page.reload();await page.waitForSelector('[data-rg-list]');
  await page.locator('[data-rg-list] > *').filter({hasText:'审核条目'}).first().locator('button').click();
  const form=page.locator('[data-rg-form]');
  await form.locator('[name=projects][value=evo]').check();await form.locator('[name=projects][value=scope-second]').check();
  assert.match(await form.locator('[data-scope-selected]').innerText(),/已选 2 个项目/);
  await form.locator('[name=knowledge_scope]').selectOption('global');assert.equal(await form.locator('[data-scope-projects]').isVisible(),false);
  await form.locator('[name=knowledge_scope]').selectOption('project');assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('报表导出必须保留原始编号，以便回查来源。资料应当是在规定数据保真要求，而不是介绍术语。');
  await form.locator('[name=exceptions]').fill('提到跨平台条件、要求单独判断的资料暂不归入。');await form.locator('[name=scope]').selectOption('future');
  const started=Date.now();await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-preview]')?.textContent.includes('AI 已按业务依据'));
  const preview=await form.locator('[data-rg-preview]').innerText();assert.match(preview,/Evo 演示项目、第二个演示项目/);assert.match(preview,/10 条/);assert.match(preview,/未推荐或此前不录入：2 条/);assert.match(preview,/模拟核对/);
  assert.ok(Date.now()-started<20000);
  await form.locator('[name=condition]').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-business-criteria-desktop.png'});
  for(const width of [1512,390]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.screenshot({path:`/tmp/evo-business-scope-${width}.png`,fullPage:true});}
  await page.setViewportSize({width:1512,height:1000});await form.locator('[data-rg=apply]').click();await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('已保存审核结果'));
  const rule=(await api('organization/guidance')).items.find(r=>r.matching_mode==='semantic');assert.ok(rule);assert.deepEqual(JSON.parse(rule.projects),['evo','scope-second']);
  await page.locator('[data-org-tab=guidance]').click();await page.locator('[data-org-action=edit-guidance]').first().click();
  const rd=page.getByRole('dialog',{name:'修改业务归类依据'});await rd.locator('[name=condition]').fill('只有规定导出内容与原始数据对应关系的业务要求才归入；概念介绍除外。');await rd.locator('[type=submit]').click();await rd.waitFor({state:'detached'});
  const row=await api('qa',{question:'共享演示的导出编号怎样保留？',answer:'导出编号保留原始值。',projects:['evo','scope-second'],knowledge_scope:'project',action:'publish'});
  await page.goto(base+'/#knowledge/qa');await page.waitForSelector('#qa-search');await page.locator('#qa-search').fill('共享演示');await page.locator('[data-memory-action=search]').click();
  await page.locator(`[data-memory-action=edit][data-id="${row.id}"]`).click();
  const dialog=page.locator('.memory-dialog[open]');await dialog.locator('[name=projects]').first().waitFor();assert.equal(await dialog.locator('[name=projects]:checked').count(),2);
  await dialog.locator('#qa-answer').fill('导出编号保留原始值和前导零。');await dialog.locator('[data-memory-action=publish]').click();await dialog.waitFor({state:'hidden'});
  const a=await api('qa?project=evo&q=共享演示'),b=await api('qa?project=scope-second&q=共享演示');
  assert.equal(a.items[0].id,row.id);assert.equal(b.items[0].id,row.id);assert.equal(a.items[0].answer,b.items[0].answer);assert.match(a.items[0].answer,/前导零/);
  await page.locator(`[data-memory-action=edit][data-id="${row.id}"]`).click();await dialog.locator('[name=knowledge_scope]').selectOption('global');await dialog.locator('[data-memory-action=publish]').click();await dialog.waitFor({state:'hidden'});
  const general=await api('qa/'+row.id);assert.equal(general.scope,'global');assert.deepEqual(general.projects,[]);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['AI 业务依据预览提供理由与原文，10 条建议、2 条排除','个人通用与项目多选切换，预览显示共同范围','归类依据可继续编辑，保存到后续自动整理','一条问答在两个项目中共用，修改同步，且可改为个人通用','桌面及手机无横向溢出'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-business-scope-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
