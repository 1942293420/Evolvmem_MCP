// Real saved changes on a disposable fixture. No provider is contacted.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true}),page=await browser.newPage({viewport:{width:1440,height:1000}});
 const base=process.env.EVOLVMEM_TEST_URL,errors=[];page.on('pageerror',e=>errors.push(e.message));
 const api=async(route,body)=>{const r=body?await page.request.post(base+'/api/knowledge/'+route,{data:body}):await page.request.get(base+'/api/knowledge/'+route);assert.ok(r.ok(),await r.text());return r.json();};
 try{
  const rows=[];for(let n=0;n<25;n++)rows.push(await api('items',{title:`集中归档样例 ${n}`,body:`第 ${n} 份业务资料，保存时必须保留原编号。`,project:'evo',action:'draft'}));
  await page.goto(base+'/#knowledge/intake');await page.locator('[data-action=project-queue][data-project=evo]').click();
  await page.locator('.project-review [data-pr-save=archive]').waitFor();
  assert.match(await page.locator('.project-review').innerText(),/集中归档/);
  await page.screenshot({path:'/tmp/evo-project-review-overview.png',fullPage:true});
  for(const box of await page.locator('[data-pr-group]').all())await box.uncheck();
  await page.locator('[data-pr-group=knowledge]').check();
  const section=page.locator('[data-pr-section=knowledge]');await section.locator('summary').click();
  const excluded=await section.locator('[data-pr-item]').first().getAttribute('data-pr-item');
  await section.locator('[data-pr-item]').first().uncheck();
  await page.locator('[data-pr-refresh]').click();
  await page.waitForLoadState('networkidle');
  assert.equal(await page.locator(`[data-pr-item="${excluded}"]`).isChecked(),false,'refresh preserves excluded choices');
  await page.evaluate(()=>location.hash='#knowledge/projects');await page.locator('.project-list').waitFor();
  await page.locator('[data-action=project-queue][data-project=evo]').click();
  assert.equal(await page.locator(`[data-pr-item="${excluded}"]`).isChecked(),false,'switching preserves selected batch');
  for(const width of [390,768,1440]){
   await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-project-review-${width}.png`,fullPage:true});
  }
  let posts=0,release;const hold=new Promise(r=>release=r);
  await page.route('**/project-review/apply',async route=>{posts++;if(posts===1)await hold;await route.continue();});
  await page.locator('[data-pr-save=archive]').click();
  await page.locator('[data-pr-stop]').waitFor();
  await page.evaluate(()=>location.hash='#knowledge/projects');await page.locator('.project-list').waitFor();
  release();await page.locator('[data-action=project-queue][data-project=evo]').click();
  await page.waitForFunction(()=>/已保存 \d+ 份/.test(document.querySelector('.pr-status')?.textContent||'')&&!document.querySelector('[data-pr-stop]'),null,{timeout:30000});
  assert.ok(posts>=2,'one confirmation is split into short save requests');
  for(const row of rows){const saved=await api('items/'+row.id);assert.equal(saved.status,String(row.id)===excluded?'candidate':'active');}
  const result=await api('project-review?project=evo');assert.ok(result.groups.flatMap(g=>g.items).some(i=>String(i.id)===excluded));
  assert.deepEqual(errors,[]);await page.screenshot({path:'/tmp/evo-project-review-saved.png',fullPage:true});
  console.log('PASS: grouped confirmation, exclusion, multi-request save, navigation during saving, preserved draft and three widths');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
