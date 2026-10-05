const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1100}}),base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const api=async(route,data)=>{const r=data===undefined?await page.request.get(base+'/api/knowledge/'+route):await page.request.post(base+'/api/knowledge/'+route,{data});assert.ok(r.ok(),await r.text());return r.json();};
 const done=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async s=>{await page.locator(s).click();await done();};
 try{
  const a=await api('items',{title:'待清洗 A',body:'Evo 演示项目需要先清洗后归类，请保留这条要求。','action':'draft'});
  const b=await api('items',{title:'待清洗 B',body:'这是一条准备永久删除的无用测试资料。','action':'draft'});
  await page.goto(base+'/#knowledge/cleaning');await page.waitForSelector('#clean-instructions');await done();
  const nav=await page.locator('.signal-nav a').allTextContents();assert.equal(nav[nav.indexOf('项目历史')-1],'数据清洗');
  assert.equal((await api('history/organization')).items.some(r=>r.key===`item:${a.id}`),false);
  await page.locator('#clean-instructions').fill((await page.locator('#clean-instructions').inputValue())+'\n保留处理顺序，不把助手说法当作用户决定。');
  await page.locator(`[data-clean-select="item:${a.id}"]`).check();assert.ok(await page.locator('[data-clean-action="preview"]').isDisabled());
  await click('[data-clean-action="rules-save"]');await click('[data-clean-action="preview"]');assert.match(await page.locator('#clean-notice').innerText(),/尚未保存/);
  assert.match(await page.locator(`[data-clean-text="item:${a.id}"]`).inputValue(),/先清洗资料/);
  await page.locator(`[data-clean-text="item:${a.id}"]`).fill('人工核对：Evo 演示项目先清洗，再由用户确认项目归属。');await page.locator(`[data-clean-category="item:${a.id}"]`).selectOption('decision');
  await page.screenshot({path:'/tmp/evo-cleaning-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.screenshot({path:'/tmp/evo-cleaning-mobile.png',fullPage:true});await page.setViewportSize({width:1440,height:1100});
  await click('[data-clean-action="save-all"]');assert.match(await page.locator('#clean-notice').innerText(),/成功 1 条/);
  const prepared=(await api('history/organization')).items.find(r=>r.key===`item:${a.id}`);assert.match(prepared.body,/人工核对/);assert.equal(prepared.category,'decision');
  await page.locator(`[data-clean-select="item:${b.id}"]`).check();page.once('dialog',d=>d.dismiss());await click('[data-clean-action="delete"]');assert.equal(await page.locator(`[data-clean-row="item:${b.id}"]`).count(),1);
  page.once('dialog',d=>d.accept());await click('[data-clean-action="delete"]');assert.match(await page.locator('#clean-notice').innerText(),/永久删除：成功 1 条/);assert.equal(await page.locator(`[data-clean-row="item:${b.id}"]`).count(),0);
  await page.locator('[data-view="unassigned"]').click();await page.waitForSelector('#history-instructions');await done();
  await page.locator(`[data-history-select="item:${a.id}"]`).check();await click('[data-history-action="preview"]');await click(`[data-history-action="save-row"][data-key="item:${a.id}"]`);
  const saved=await api('items/'+a.id);assert.equal(saved.project,'evo');assert.match(saved.body,/人工核对/);assert.equal(saved.learning.category,'decision');
  assert.deepEqual(errors,[]);console.log('PASS: cleaning navigation, saved rules, AI preview, manual text/category, batch save, stage gate, delete cancel/confirm, project classification uses reviewed draft, mobile');
 }catch(e){await page.screenshot({path:'/tmp/evo-cleaning-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
