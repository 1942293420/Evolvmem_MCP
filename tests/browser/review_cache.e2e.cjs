const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),requests=[],errors=[];
 page.on('request',r=>{if(r.url().includes('/groups/detail?')||r.url().includes('/groups/analysis?'))requests.push(r.url());});
 page.on('pageerror',e=>errors.push(e.message));
 const select=async index=>{const b=page.locator('[data-rg-list] button').nth(index),id=await b.getAttribute('data-id');await b.click();await page.waitForFunction(id=>document.querySelector('[data-rg-form] [name=group_id]')?.value===id&&!document.querySelector('[data-rg-form]').hidden&&!document.querySelector('[data-rg-form]').inert,id);};
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');await page.locator('[data-rg-list] button').first().waitFor();
  await select(0);await select(1);const before=requests.length;
  await select(0);await select(1);await select(0);
  assert.equal(requests.length,before,'returning to loaded details must use the browser cache');
  await page.locator('[data-rg=refresh]').click();await page.waitForFunction(()=>document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
  assert.ok(requests.length>before,'explicit refresh must read fresh details');
  const refreshed=requests.length;await select(0);assert.equal(requests.length,refreshed);
  await page.locator('.signal-nav [data-knowledge-view=projects]').click();await page.locator('.project-list').waitFor();
  await page.locator('.signal-nav [data-knowledge-view=organization]').click();await page.locator('[data-rg-list] button').first().waitFor();
  await select(0);assert.equal(requests.length,refreshed,'navigation must retain the detail cache');
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['已打开详情反复切换不发请求','主动刷新更新缓存','切页返回继续使用缓存'],errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
