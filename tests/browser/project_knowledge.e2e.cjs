// Real HTTP + disposable SQLite; no production history or model calls.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const api=async r=>{const res=await page.request.get(base+'/api/knowledge/'+r);assert.ok(res.ok(),await res.text());return res.json();};
 let reads=0;page.on('request',r=>{if(r.url().includes('/api/knowledge/directory?'))reads++;});
 const open=async()=>{await page.goto(base+'/#knowledge/library?project=demo');await page.waitForSelector('[data-pk-select]');};
 try{
  await open();
  const projection=await api('directory?project=demo');
  const recalled=await api('recall?project=demo&kind=knowledge');
  assert.deepEqual(recalled.knowledge,projection.items);
  assert.equal(await page.locator('[data-pk-select]').count(),projection.total);assert.ok(projection.total>=2);
  const first=page.locator('[data-pk-select]').first(),last=page.locator('[data-pk-select]').last();
  await last.click();const title=await last.locator('strong').innerText();
  assert.equal(await page.locator('[data-pk-detail]>h3').innerText(),title);
  await first.click();await last.click();assert.equal(await page.locator('[data-pk-detail]>h3').innerText(),title);
  await page.locator('.pk-filters [name=q]').fill('尚未提交的查找');
  const before=reads;
  await page.locator('[data-memory-lane=history]').click();await page.locator('[data-memory-lane=overview]').click();
  assert.equal(await page.locator('.pk-filters [name=q]').inputValue(),'尚未提交的查找');
  assert.equal(await page.locator('[data-pk-detail]>h3').innerText(),title);assert.equal(reads,before);
  await page.evaluate(()=>location.hash='#knowledge/projects');await page.waitForSelector('.project-collection');
  await page.evaluate(()=>location.hash='#knowledge/library?project=demo');await page.waitForSelector('[data-pk-select]');
  assert.equal(await page.locator('.pk-filters [name=q]').inputValue(),'尚未提交的查找');assert.equal(reads,before);
  await page.locator('.pk-filters [name=q]').fill('执行器');await page.locator('.pk-filters button').click();
  await page.waitForFunction(()=>document.querySelector('[data-pk-status]').textContent==='已归档知识 1 条');
  assert.match(await page.locator('[data-pk-detail]').innerText(),/Windows/);
  assert.match(await page.locator('[data-pk-detail]').innerText(),/分歧资料 · 未归入本项目/);
  await page.locator('.pk-filters [name=conditionKey]').fill('环境');await page.locator('.pk-filters [name=conditionValue]').fill('不存在的环境');await page.locator('.pk-filters button').click();
  await page.waitForFunction(()=>document.querySelector('[data-pk-status]').textContent==='已归档知识 0 条');
  await page.locator('.pk-filters [name=conditionValue]').fill('生产');await page.locator('.pk-filters [name=at]').fill('2026-10-19');await page.locator('.pk-filters button').click();await page.waitForSelector('[data-pk-select]');
  await page.locator('[data-pk-source]').first().click();await page.waitForSelector('[data-pk-original] h3');
  await page.locator('[data-pk-detail] a[href*=facts]').click();await page.waitForSelector('#org-facts:not([hidden])');
  await page.evaluate(()=>location.hash='#knowledge/library?project=demo');await page.waitForSelector('[data-pk-select]');
  const cached=reads;await page.locator('[data-memory-lane=summary]').click();await page.locator('[data-memory-lane=overview]').click();assert.equal(reads,cached);
  // A slow old search must not replace the newer query's result.
  await page.route('**/api/knowledge/directory?*',async route=>{const response=await route.fetch();if(new URL(route.request().url()).searchParams.get('q')==='执行器')await new Promise(r=>setTimeout(r,550));await route.fulfill({response});});
  await page.locator('[data-pk-refresh]').click();await page.locator('.pk-filters [name=q]').fill('不存在的卫星');await page.locator('.pk-filters button').click();
  await page.waitForFunction(()=>document.querySelector('[data-pk-status]').textContent==='已归档知识 0 条');await page.waitForTimeout(650);
  assert.equal(await page.locator('[data-pk-select]').count(),0);await page.unroute('**/api/knowledge/directory?*');
  await page.locator('.pk-filters [name=conditionKey]').fill('');await page.locator('.pk-filters [name=conditionValue]').fill('');
  await page.locator('.pk-filters [name=q]').fill('');await page.locator('.pk-filters button').click();await page.waitForSelector('[data-pk-select]');
  await page.locator('[data-pk-select]').filter({hasText:'Demo 执行器'}).click();
  for(const width of [390,768,1440]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await page.screenshot({path:`/tmp/evo-project-knowledge-${width}.png`,fullPage:true});}
  await page.route('**/api/knowledge/directory?*',r=>r.fulfill({status:500,json:{error:'读取暂时失败'}}),{times:1});
  await page.locator('[data-pk-refresh]').click();await page.waitForFunction(()=>document.querySelector('[data-pk-status]').textContent.includes('读取失败'));
  assert.ok(await page.locator('[data-pk-select]').count());await page.locator('[data-pk-refresh]').click();await page.waitForFunction(()=>document.querySelector('[data-pk-status]').textContent.startsWith('已归档知识'));
  await page.locator('[data-pk-select]').filter({hasText:'Demo 执行器'}).click();
  await page.locator('[data-pk-detail] a[href*=facts]').click();await page.waitForSelector('#org-facts:not([hidden])');
  const savedVersion=projection.items.find(i=>i.kind==='fact').current[0].id;
  await page.locator(`[data-fact-version="${savedVersion}"] [data-fact-action=choose]`).click();
  await page.waitForFunction(id=>document.querySelector(`[data-fact-version="${id}"] .badge`)?.textContent==='已确认内容',savedVersion);
  const afterSaveReads=reads;
  await page.evaluate(()=>location.hash='#knowledge/library?project=demo');await page.waitForSelector('[data-pk-select]');
  await page.waitForFunction(()=>document.querySelector('[data-pk-detail]')?.textContent.includes('已确认的结论'));
  assert.ok(reads>afterSaveReads,'a successful save invalidates the cached projection');
  assert.equal(await page.locator('[data-pk-detail] .pk-warning').count(),0);
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['目录与 Agent 结果及来源一致','条件、版本、分歧分开展示','切换知识即时对应','切页恢复查询草稿与选择且不重读','迟到查找不覆盖新结果','原文可查','读取失败保留内容并可重试','三种宽度不溢出'],errors}));
 }catch(e){await page.screenshot({path:'/tmp/evo-project-knowledge-failed.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
