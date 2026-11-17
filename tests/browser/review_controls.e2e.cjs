// Exercise delayed writes, inline project creation and truthful model feedback.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message));
 const base=process.env.EVOLVMEM_TEST_URL;
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const form=page.locator('[data-rg-form]');
 const open=async title=>{await page.locator('[data-rg-list] button').filter({hasText:title}).first().click();await idle();await form.waitFor({state:'visible'});};
 if(process.env.EVOLVMEM_BASELINE_SCRIPT)await page.route('**/review-groups.js*',route=>route.fulfill({contentType:'application/javascript',body:require('node:fs').readFileSync(process.env.EVOLVMEM_BASELINE_SCRIPT,'utf8')}));
 let writes=0,release;
 try{
  await page.goto(base+'/#knowledge/organization');await idle();await open('审核条目00');
  await form.locator('[name=projects][value=evo]').check();
  const gate=new Promise(r=>release=r);
  await page.route('**/organization/archive',async route=>{writes++;await gate;await route.fulfill({status:500,json:{error:'模拟保存失败'}});});
  const save=form.locator('[data-rg=file]');await save.click();
  assert.equal(await save.getAttribute('aria-busy'),'true');assert.match(await save.innerText(),/正在保存/);
  await save.evaluate(b=>{b.click();b.click();});assert.equal(writes,1);
  assert.match(await form.locator('[data-rg-action-status]').innerText(),/保存/);
  await page.screenshot({path:'/tmp/evo-controls-saving.png'});
  release();await idle();assert.match(await form.locator('[data-rg-action-status]').innerText(),/模拟保存失败/);
  assert.equal(await save.isEnabled(),true);assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  await page.unroute('**/organization/archive');
  await form.locator('[data-rg-ai] summary').first().click();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('保留原始编号');
  await form.locator('[data-rg=project-new]').click();
  const dialog=page.locator('.project-create-dialog');
  await dialog.locator('#new-project-id').fill('review-inline');await dialog.locator('#new-project-name').fill('当前页新项目');
  await dialog.locator('[type=submit]').click();await dialog.waitFor({state:'detached'});await idle();
  assert.equal(await form.locator('[name=projects][value=review-inline]').isChecked(),true);
  assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  assert.equal(await form.locator('[name=condition]').inputValue(),'保留原始编号');
  assert.match(await form.locator('[data-ke-open] summary').innerText(),/关联已有知识，沿用归属/);
  for(const width of [390,1440]){
   await page.setViewportSize({width,height:1000});await save.scrollIntoViewIfNeeded();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-controls-save-${width}.png`});
  }
  await save.click();await form.waitFor({state:'hidden'});assert.match(await page.locator('[data-rg-empty]').innerText(),/归档已保存/);
  await idle();await open('审核条目01');await form.locator('[name=projects][value=evo]').check();
  await form.locator('[data-rg-ai] summary').first().click();
  await form.locator('[name=selection]').selectOption('group');await form.locator('[name=condition]').fill('保留原始编号');
  await form.locator('[name=model_choice]').selectOption('fallback');
  let job,phase='running',choice,hasCall=true;
  await page.route('**/groups/analyze',async route=>{
   choice=route.request().postDataJSON().model_choice;const r=await route.fetch();job=await r.json();
   await route.fulfill({json:{...job,status:'running',phase:'retrieval',pool_count:8,indexed:3,elapsed_seconds:5}});
  });
  await page.route('**/groups/analysis?*',route=>route.fulfill({json:{...job,status:phase,phase:'judging',elapsed_seconds:12,
   total:hasCall?4:0,model_status:hasCall?{provider:'mimo',model:'mimo-v2.6-flash',fallback:false,state:'running'}:{},
   model_calls:hasCall?[{provider:'mimo',model:'mimo-v2.6-flash',fallback:false,state:phase==='running'?'running':'succeeded',elapsed_ms:1200,call_id:'1'}]:[],
   ...(phase==='completed'?{proposal:{...job.proposal,group_id:job.request.group_id,revision:'empty',eligible:[],excluded:[],matching_mode:'semantic',searched_count:8,ai_judged_count:0,projects:['evo'],knowledge_scope:'project'}}:{})}}));
  await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-model-status]').textContent.includes('MiMo'));
  assert.equal(choice,'fallback');assert.match(await form.locator('[data-rg-model-status]').innerText(),/mimo-v2.6-flash.*调用中/);
  phase='completed';await page.waitForFunction(()=>document.querySelector('[data-rg-model-status]').textContent.includes('调用成功'));
  assert.match(await form.locator('[data-rg-model-status]').innerText(),/1.2/);
  await form.locator('[data-rg-model-status]').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-controls-model.png'});
  hasCall=false;await form.locator('[name=model_choice]').selectOption('primary');await form.locator('[data-rg=preview]').click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-model-status]').textContent.includes('本次未调用判断模型'));
  assert.ok(!(await form.locator('[data-rg-model-status]').innerText()).includes('MiMo'));
  await form.locator('[data-rg-model-status]').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-controls-no-model.png'});
  assert.deepEqual(errors,[]);console.log('PASS: immediate save feedback, one delayed write, retry preserves draft, inline project creation and selection, explicit model choice and completed call result.');
 }catch(e){await page.screenshot({path:'/tmp/evo-controls-failed.png',fullPage:true});throw e;}
 finally{release?.();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
