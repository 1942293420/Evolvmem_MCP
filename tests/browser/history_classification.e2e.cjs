// Real fixture HTTP: rule drafts and partial batch results remain reviewable.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478';
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const api=async(route,data)=>{const response=data===undefined?await page.request.get(base+'/api/knowledge/'+route):await page.request.post(base+'/api/knowledge/'+route,{data});assert.ok(response.ok(),await response.text());return response.json();};
 const done=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async selector=>{await page.locator(selector).click();await done();};
 try{
  const a=await api('items',{title:'并发核对 A',body:'Evo 演示项目资料 A，需要分类后核对。',scope:'project',action:'draft'});
  const b=await api('items',{title:'批量确认 B',body:'Evo 演示项目资料 B，需要分类后核对。',scope:'project',action:'draft'});
  await page.goto(base+'/#knowledge/intake');await page.waitForSelector('.intake-skill-button');await done();await click('.intake-skill-button');await page.waitForSelector('#history-instructions');await done();assert.ok(page.url().endsWith('#knowledge/unassigned'));
  const pick=id=>`[data-history-project="item:${id}"]`;
  await page.locator(`[data-history-select="item:${a.id}"]`).check();await page.locator(`[data-history-select="item:${b.id}"]`).check();
  await page.locator('#history-instructions').fill((await page.locator('#history-instructions').inputValue())+'\n先核对原话。');
  assert.equal(await page.locator('[data-history-action="preview"]').isDisabled(),true);
  page.once('dialog',d=>d.dismiss());await click('.history-tabs [data-view="projects"]');assert.ok(page.url().endsWith('#knowledge/unassigned'));
  await click('[data-history-action="rules-save"]');await click('[data-history-action="preview"]');assert.equal(await page.locator(pick(a.id)).inputValue(),'evo');
  await page.locator(pick(a.id)).selectOption('dsh-a');
  await page.locator('#history-instructions').fill((await page.locator('#history-instructions').inputValue())+'\n多个项目待确认。');await click('[data-history-action="rules-save"]');
  assert.equal(await page.locator(pick(a.id)).inputValue(),'dsh-a');assert.equal(await page.locator(pick(b.id)).inputValue(),'');
  await click('[data-history-action="preview"]');assert.equal(await page.locator(pick(a.id)).inputValue(),'dsh-a');
  const latest=await api('items/'+a.id);await api(`items/${a.id}/update`,{expected_revision:latest.revision,body:'内容已改动，旧分类预览必须核对。'});
  await click('[data-history-action="save-all"]');assert.match(await page.locator('#history-notice').innerText(),/成功 1 条，失败 1 条/);
  assert.equal(await page.locator(pick(a.id)).inputValue(),'dsh-a');assert.equal(await page.locator(pick(b.id)).count(),0);
  assert.match(await page.locator(`[data-history-row="item:${a.id}"]`).innerText(),/已变化/);
  await page.locator(pick(a.id)).selectOption('evo');await click(`[data-history-action="save-row"][data-key="item:${a.id}"]`);
  assert.match(await page.locator('#history-notice').innerText(),/成功 1 条，失败 0 条/);
  assert.equal((await api('items/'+a.id)).status,'candidate');assert.equal((await api('items/'+b.id)).project,'evo');
  assert.deepEqual(errors,[]);console.log('PASS: dirty-rule guard, leave protection, AI invalidation, manual choice preservation, partial batch and corrected retry');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
