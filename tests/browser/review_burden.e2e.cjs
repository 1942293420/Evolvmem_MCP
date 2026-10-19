// Real HTTP + automatic worker; only knowledge_fixture.py synthetic data.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
 const base=process.env.EVOLVMEM_TEST_URL;
 page.setDefaultTimeout(20000);page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(base+'/#knowledge/organization');
  await page.waitForSelector('[data-rg-list]');
  assert.match(await page.locator('.organization-panel').innerText(),/AI 自动归档，分错可以修改/);
  const target=await page.evaluate(async()=>{
   const queue=await(await fetch('/api/knowledge/organization?status=current')).json();
   for(const task of queue.items){
    const d=await(await fetch('/api/knowledge/organization/detail?task_id='+task.id)).json();
    const u=d.units.find(u=>u.decision==='auto'&&u.project==='evo');
    if(u)return {id:task.id,digest:u.digest,item_id:u.item_id};
   }
  });
  assert.ok(target?.item_id,'automatic ownership must have saved real history without a human click');
  await page.locator('[data-org-tab=all]').click();
  const task=page.locator(`[data-org-task="${target.id}"]`);
  await task.locator('[data-org-action=toggle]').click();
  const unit=task.locator(`[data-org-unit="${target.digest}"]`);
  const expand=async()=>{
   const group=task.locator('details[data-org-group=auto]');
   await group.waitFor({state:'visible'});
   if(!await group.evaluate(e=>e.open))await group.locator(':scope > summary').click();
   await page.waitForFunction(id=>document.querySelector(`[data-org-task="${id}"] [data-org-group=auto]`)?.open,target.id);
   const decision=unit.locator('.unit-decision');
   if(!await decision.evaluate(e=>e.open))await decision.locator(':scope > summary').click();
  };
  await expand();await unit.locator('[data-org-project]').selectOption('dsh-b');
  await page.waitForTimeout(4500); // Exercise the real background refresh while editing.
  assert.ok(await unit.locator('.unit-decision').evaluate(e=>e.open));
  assert.equal(await unit.locator('[data-org-project]').inputValue(),'dsh-b');
  await unit.locator('[data-org-action=correct]').click();
  await page.waitForFunction(async t=>{
   const d=await(await fetch('/api/knowledge/organization/detail?task_id='+t.id)).json();
   const u=d.units.find(u=>u.digest===t.digest);return u?.decision==='manual'&&u.project==='dsh-b';
  },target);
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   if(width===390)assert.ok(await page.locator('.organization-task > .section-head > div:first-child').evaluateAll(rows=>rows.every(e=>e.getBoundingClientRect().width>200)),'task titles must remain readable beside the action buttons');
   await page.screenshot({path:`/tmp/evo-review-burden-${width}.png`,fullPage:true});
  }
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({automatic_history:true,human_correction:true,widths:[1440,390],errors}));
 }catch(error){await page.screenshot({path:'/tmp/evo-review-burden-error.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
