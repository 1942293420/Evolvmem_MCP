// Real HTTP and disposable data: an unjudged value-review item can be filed once.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const api=async route=>{const r=await page.request.get(base+'/api/knowledge/'+route);assert.ok(r.ok(),await r.text());return r.json();};
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async selector=>{await page.locator(selector).click();await idle();};
 const form=page.locator('[data-rg-form]'),preview=page.locator('[data-rg-preview]');
 const open=async title=>{
  while(await page.locator('[data-rg=previous]').isEnabled())await click('[data-rg=previous]');
  const item=page.locator('[data-rg-list] button').filter({has:page.locator('strong',{hasText:new RegExp('^'+title+'$')})});
  while(!await item.count()&&await page.locator('[data-rg=next]').isEnabled())await click('[data-rg=next]');
  await item.click();await page.waitForFunction(t=>!document.querySelector('[data-rg-form]').inert&&document.querySelector('[data-rg-title]').textContent===t,title);
 };
 let analyses=0,writes=0;
 page.on('request',r=>{if(r.url().endsWith('/groups/analyze'))analyses++;if(r.url().endsWith('/groups/decisions'))writes++;});
 try{
  await page.goto(base+'/#knowledge/organization');await idle();await open('保留价值演示');
  const member={task_id:Number(await form.locator('[name=task_id]').inputValue()),digest:await form.locator('[name=digest]').inputValue()};
  const unit=async()=> (await api('organization/detail?task_id='+member.task_id)).units.find(u=>u.digest===member.digest);
  await form.locator('[name=projects][value=evo]').check();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('演示项目的导出规则归入演示项目。');
  await click('[data-rg=preview]');await preview.locator('h3').waitFor();await idle();
  assert.match(await preview.innerText(),/本次待处理资料：1 条/);
  assert.match(await preview.innerText(),/待你决定是否录入 · 1 条/);
  assert.match(await preview.innerText(),/需要你判断：1 条/);
  const choice=preview.locator('[data-rg-choice]');
  assert.equal(await choice.locator('option[value=include]').innerText(),'保留并录入');
  assert.equal(await form.locator('[data-rg=apply]').isEnabled(),false);
  assert.equal((await unit()).disposition,'review','preview must not silently retain material');
  await choice.selectOption('include');assert.equal(await form.locator('[data-rg=apply]').isEnabled(),true);
  assert.match(await form.locator('[data-rg-save-status]').innerText(),/待保存：录入 1 条/);
  assert.equal(writes,0);
  await page.reload();await choice.waitFor();await idle();
  assert.equal(await choice.inputValue(),'include','refresh must retain the unsaved choice');
  assert.equal(analyses,1,'restore and manual save must not rerun AI');
  for(const width of [390,1512]){
   await page.setViewportSize({width,height:1000});await preview.locator('h3').scrollIntoViewIfNeeded();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-review-value-save-${width}.png`});
  }
  await click('[data-rg=apply]');await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  const saved=await unit();assert.equal(saved.disposition,'keep');assert.equal(saved.decision,'manual');assert.equal(saved.project,'evo');assert.ok(saved.item_id);
  assert.equal(writes,1);assert.equal(analyses,1);
  assert.match(await page.locator('[data-rg-empty]').innerText(),/已保存/);
  await page.reload();await idle();
  assert.equal((await api('organization/groups/recent-analysis')).job,null,'completed filing must not return as pending');
  // Single-item preview offers the same explicit choice without invoking AI.
  await open('保留价值演示二');await form.locator('[name=selection]').selectOption('item');
  await form.locator('[name=projects][value=evo]').check();await click('[data-rg=preview]');
  await preview.locator('[data-rg-choice]').waitFor();
  assert.equal(await form.locator('[data-rg=apply]').isEnabled(),false);
  // The user may already have kept the material through another entry point.
  const retainedId=Number(await form.locator('[name=task_id]').inputValue()),retainedDigest=await form.locator('[name=digest]').inputValue();
  await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('演示项目的导出规则归入演示项目。');
  await click('[data-rg=preview]');await preview.locator('h3').waitFor();await idle();
  const before=(await api('organization/detail?task_id='+retainedId)).units.find(u=>u.digest===retainedDigest);
  const keep=await page.request.post(base+'/api/knowledge/organization/disposition',{data:{task_id:retainedId,digest:retainedDigest,expected_revision:before.revision,disposition:'keep'}});assert.ok(keep.ok());
  await page.reload();await preview.locator('[data-rg-choice]').waitFor();await idle();
  assert.match(await preview.innerText(),/本次待处理资料：1 条/);
  assert.equal(await preview.locator('[data-rg-choice] option[value=include]').innerText(),'录入');
  await preview.locator('[data-rg-choice]').selectOption('include');await click('[data-rg=apply]');
  await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  const retained=(await api('organization/detail?task_id='+retainedId)).units.find(u=>u.digest===retainedDigest);
  assert.equal(retained.decision,'manual');assert.equal(retained.project,'evo');
  assert.equal(writes,2);assert.equal(analyses,2);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['零条 AI 结果仍显示一条待处理资料','待保留资料可明确选择保留并录入','刷新恢复未保存的选择且不重复调用 AI','一次保存同时保留和归类并退出待审','单条预览同样可选择处理','已通过其他入口保留的资料恢复旧预览后仍可直接归类','手机和桌面无横向溢出'],errors}));
 }catch(error){await page.screenshot({path:'/tmp/evo-review-value-save-failed.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
