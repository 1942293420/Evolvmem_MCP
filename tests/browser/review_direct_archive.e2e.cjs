// Real HTTP, disposable data: project selection saves without an AI/keep step.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const api=async(route,data)=>{const r=data===undefined?await page.request.get(base+'/api/knowledge/'+route):await page.request.post(base+'/api/knowledge/'+route,{data});assert.ok(r.ok(),await r.text());return r.json();};
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async s=>{await page.locator(s).click();await idle();};
 const form=page.locator('[data-rg-form]');
 const open=async title=>{
  while(await page.locator('[data-rg=previous]').isEnabled())await click('[data-rg=previous]');
  const item=page.locator('[data-rg-list] button').filter({has:page.locator('strong',{hasText:new RegExp('^'+title+'$')})});
  while(!await item.count()&&await page.locator('[data-rg=next]').isEnabled())await click('[data-rg=next]');
  await item.click();await page.waitForFunction(t=>!document.querySelector('[data-rg-form]').inert&&document.querySelector('[data-rg-title]').textContent===t,title);
 };
 const current=async()=>({task_id:Number(await form.locator('[name=task_id]').inputValue()),digest:await form.locator('[name=digest]').inputValue()});
 const unit=async u=>(await api('organization/detail?task_id='+u.task_id)).units.find(x=>x.digest===u.digest);
 let previews=0,writes=0;
 page.on('request',r=>{if(/\/groups\/(preview|analyze)$/.test(r.url()))previews++;if(r.url().endsWith('/organization/archive'))writes++;});
 try{
  await page.goto(base+'/#knowledge/organization');await idle();await open('保留价值演示');
  const member=await current(),before=await unit(member);
  assert.equal(await form.locator('[data-rg=file]').isEnabled(),false);
  assert.equal(await form.locator('[data-rg=defer]').isEnabled(),true);
  assert.equal(await form.locator('[data-rg-ai]').getAttribute('open'),null);
  assert.equal(await form.locator('[data-rg=keep]').count(),0);
  await form.locator('[name=projects][value=evo]').check();
  assert.equal(await form.locator('[data-rg=file]').isEnabled(),true);
  assert.equal((await unit(member)).decision,'review','choosing a project is still an unsaved draft');
  await open('保留价值演示二');assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),false);
  await open('保留价值演示');assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  for(const width of [390,1512]){
   await page.setViewportSize({width,height:1000});await form.locator('[data-rg=file]').scrollIntoViewIfNeeded();
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-direct-archive-${width}.png`});
  }
  await page.route('**/organization/archive',r=>r.fulfill({status:500,json:{error:'保存暂时失败'}}),{times:1});
  await click('[data-rg=file]');assert.equal(await form.isVisible(),true);
  assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  assert.deepEqual(await unit(member),before);
  await click('[data-rg=file]');await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  const saved=await unit(member);assert.equal(saved.decision,'manual');assert.equal(saved.project,'evo');assert.equal(saved.disposition,'keep');assert.ok(saved.item_id);
  assert.equal(previews,0);assert.equal(writes,2);
  await open('保留价值演示二');const deferred=await current(),original=(await unit(deferred)).text;
  await click('[data-rg=defer]');await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
  const aside=await unit(deferred);assert.equal(aside.disposition,'set_aside');assert.equal(aside.project,'');assert.equal(aside.text,original);
  assert.equal(previews,0);assert.equal(writes,3);
  await api('organization/disposition',{...deferred,expected_revision:aside.revision,disposition:'keep'});
  await click('[data-rg=refresh]');await open('保留价值演示二');
  assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),false);
  await form.locator('[name=projects][value=evo]').check();await click('[data-rg=file]');
  await page.reload();await idle();assert.equal((await unit(deferred)).decision,'manual');
  assert.equal(previews,0);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['选项目直接保存，无预览或 AI 请求','选项目未保存不写入归属','切换保留各自项目草稿','保存失败不改数据且可重试','暂不归档无需项目且原文可恢复','返回待归档后可直接归入项目','桌面和手机无横向溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-direct-archive-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
