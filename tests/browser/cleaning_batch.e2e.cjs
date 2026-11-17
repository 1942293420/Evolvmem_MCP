async function revealRule(page,selector){await page.waitForSelector(selector,{state:'attached'});const rule=page.locator('.history-rules');if(await rule.count()&&!await rule.evaluate(el=>el.open))await rule.locator(':scope > summary').click();await page.waitForSelector(selector);}
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const api=async(route,data)=>{const r=data===undefined?await page.request.get(base+'/api/knowledge/'+route):await page.request.post(base+'/api/knowledge/'+route,{data});assert.ok(r.ok(),await r.text());return r.json();};
 const done=()=>page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async s=>{await page.locator(s).click();await done();};
 const select=(id)=>page.locator(`[data-clean-select="item:${id}"]`);
 const exists=id=>page.locator(`[data-clean-row="item:${id}"]`).count();
 try{
  const items=[];
  for(const [title,discard] of [['保存 A',false],['删除 B',true],['未勾选删除 C',true],['未勾选保存 D',false],['仅删除 E',true],['改为保留 F',true],['冲突删除 G',true]])items.push(await api('items',{title,body:discard?'无用测试资料，没有明确事项。':'Evo 演示项目先核对再保存的业务要求。',action:'draft'}));
  const [a,b,c,d,e,f,g]=items;
  await page.goto(base+'/#knowledge/cleaning');await revealRule(page,'#clean-instructions');await done();
  for(const item of items)await select(item.id).check();
  await click('[data-clean-action="preview"]');
  for(const item of [c,d,e,f,g])await select(item.id).uncheck();
  assert.match(await page.locator('[data-clean-action="save-all"]').innerText(),/保存 1 条.*删除 1 条/);
  let question='';page.once('dialog',dialog=>{question=dialog.message();dialog.dismiss();});await click('[data-clean-action="save-all"]');
  assert.match(question,/保存.*1.*删除.*1/s);assert.ok(question.includes(b.title));assert.ok(!question.includes(c.title));
  assert.equal(await exists(a.id),1);assert.equal(await exists(b.id),1);
  assert.equal((await api('history/organization')).items.some(r=>r.key===`item:${a.id}`),false);
  await page.screenshot({path:'/tmp/evo-cleaning-batch-desktop.png',fullPage:true});
  page.once('dialog',dialog=>dialog.accept());await click('[data-clean-action="save-all"]');
  assert.match(await page.locator('#clean-notice').innerText(),/保存成功 1 条.*删除成功 1 条/);
  assert.equal(await exists(a.id),0);assert.equal(await exists(b.id),0);
  assert.ok((await api('history/organization')).items.some(r=>r.key===`item:${a.id}`));
  assert.equal(await exists(c.id),1);assert.equal(await exists(d.id),1);
  await select(e.id).check();assert.ok(await page.locator('[data-clean-action="save-all"]').isEnabled());
  page.once('dialog',dialog=>dialog.accept());await click('[data-clean-action="save-all"]');assert.equal(await exists(e.id),0);
  await click(`[data-clean-action="keep-row"][data-key="item:${f.id}"]`);await select(f.id).check();await click('[data-clean-action="save-all"]');
  assert.ok((await api('history/organization')).items.some(r=>r.key===`item:${f.id}`));
  // Successful saves remain acknowledged while a stale delete stays available.
  await api('items/'+g.id+'/update',{expected_revision:g.revision,body:'已由另一个窗口修改的资料，必须重新核对。'});
  await select(d.id).check();await select(g.id).check();page.once('dialog',dialog=>dialog.accept());await click('[data-clean-action="save-all"]');
  assert.match(await page.locator('#clean-notice').innerText(),/保存成功 1 条.*删除成功 0 条.*失败 1 条/);
  assert.equal(await exists(d.id),0);assert.equal(await exists(g.id),1);assert.equal(await exists(c.id),1);
  assert.match(await page.locator(`[data-clean-row="item:${g.id}"]`).innerText(),/资料或规则已变化/);
  assert.deepEqual(errors,[]);console.log('PASS: mixed batch save/delete, one confirmation, cancel changes nothing, unchecked untouched, delete-only, retain override, partial conflict results');
 }catch(error){await page.screenshot({path:'/tmp/evo-cleaning-batch-failed.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
