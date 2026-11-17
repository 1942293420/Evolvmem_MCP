const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const api=async()=>{const r=await page.request.get(base+'/api/knowledge/facts');assert.ok(r.ok());return r.json();};
 const group=title=>page.locator('.fact-card').filter({has:page.locator('h3',{hasText:title})});
 let reads=0;page.on('request',r=>{if(r.method()==='GET'&&r.url().includes('/api/knowledge/facts'))reads++;});
 try{
  await page.goto(base+'/#knowledge/organization');await page.locator('[data-org-tab="facts"]').click();
  await page.waitForSelector('.fact-card');const before=await api();assert.equal(before.issues.length,3);
  const main=group('Demo 执行器'),linux=main.locator('.fact-version').filter({has:page.locator('h4',{hasText:'Linux'})});
  await main.getByText(/^知识关系 ·/).click();assert.match(await main.innerText(),/验证.*验证项目/s);
  await page.route('**/facts/resolve',r=>r.fulfill({status:500,json:{error:'模拟保存失败'}}),{times:1});
  await linux.locator('[data-fact-action="choose"]').click();await page.waitForFunction(()=>document.querySelector('[data-fact-status]').textContent.includes('模拟保存失败'));
  assert.equal((await api()).issues.length,3);
  await linux.locator('[data-fact-action="choose"]').click();await page.waitForFunction(()=>document.querySelector('[data-fact-status]').textContent.includes('2 项分歧'));
  assert.equal((await api()).items.find(f=>f.subject==='Demo 执行器').current.value,'Linux');
  const conditions=group('Demo 部署方式');await conditions.locator('[data-fact-action="conditions"]').click();
  const inputs=conditions.locator('[data-condition-version]');await inputs.nth(0).fill('桌面机器');await inputs.nth(1).fill('服务器');
  const count=reads;await page.locator('[data-org-tab="history"]').click();await page.locator('[data-org-tab="facts"]').click();
  assert.equal(reads,count);assert.equal(await inputs.nth(0).inputValue(),'桌面机器');
  await conditions.getByRole('button',{name:'保存适用情况'}).click();await page.waitForFunction(()=>document.querySelector('[data-fact-status]').textContent.includes('1 项分歧'));
  const split=group('Demo 目录位置');await split.locator('[data-fact-action=defer]').click();await page.waitForFunction(()=>document.querySelector('[data-fact-status]').textContent.includes('0 项分歧'));await split.getByText('资料被放错组了？',{exact:true}).click();
  await split.locator('[data-fact-action="split"]').first().click();await page.waitForFunction(()=>[...document.querySelectorAll('.fact-card h3')].filter(e=>e.textContent.includes('Demo 目录位置')).length===2);
 assert.equal(await group('Demo 目录位置').count(),2);
  await page.locator('.fact-filters [name="q"]').fill('执行器');await page.locator('.fact-filters button').click();await page.waitForFunction(()=>document.querySelectorAll('.fact-card').length===1);
  for(const width of [390,768,1440]){
   await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-facts-${width}.png`,fullPage:true});
  }
  await page.reload();await page.locator('[data-org-tab="facts"]').click();await page.waitForSelector('.fact-card');
  assert.equal((await api()).issues.length,0);
  const after=await api(),mainData=after.items.find(f=>f.subject==='Demo 执行器');
  assert.deepEqual(new Set(mainData.versions.flatMap(v=>v.sources.flatMap(s=>s.projects))),new Set(['demo','other']));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks:['保存失败无半写入，可重试采用结论','按适用情况解决分歧','拆开聚合保留原项目','切页保留草稿且不重复读取','按对象查询与刷新读回','390/768/1440 宽度可用'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-facts-failed.png',fullPage:true});throw e;}
 finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
