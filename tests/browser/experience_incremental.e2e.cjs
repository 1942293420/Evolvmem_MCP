const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const base=process.env.EVOLVMEM_TEST_URL,output=process.env.EVOLVMEM_SCREENSHOTS;
if(!base||!process.env.EVOLVMEM_SYNTHETIC_FIXTURE)throw new Error('Synthetic fixture required');
(async()=>{
 const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP knowledge-console.test 127.0.0.1']});
 const context=await browser.newContext({viewport:{width:1440,height:1050}}),errors=[],checks=[];
 await context.addCookies([{name:'evolvmem_session',value:'test-owner-cookie',url:base}]);
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(12000);
 try{
  await page.goto(base+'/#experiences');await page.waitForSelector('.experience-entry');
  assert.equal(await page.locator('.experience-entry').count(),2);
  assert.equal(await page.locator('#experience-filters [name=category] option').count(),9);
  await page.locator('.experience-entry').first().click();await page.waitForSelector('#experience-detail blockquote');
  assert.match(await page.locator('#experience-detail').innerText(),/未独立验证/);
  assert.match(await page.locator('#experience-detail').innerText(),/关键改动/);
  assert.match(await page.locator('#experience-detail blockquote').first().innerText(),/精确原问答/);
  assert.equal(await page.locator('#experience-detail img').count(),0,'raw HTML must be escaped');
  checks.push('incremental cards, eight categories, full source-bound quotes, escaped source HTML');
  await page.locator('#experience-filters [name=review]').selectOption('accepted');
  await page.locator('#experience-filters [type=submit]').click();await page.waitForSelector('.experience-entry');
  assert.equal(await page.locator('.experience-entry').count(),1);
  assert.match(await page.locator('.experience-entry').innerText(),/整理已验收/);
  await page.locator('#experience-filters [name=status]').selectOption('verified');
  await page.locator('#experience-filters [type=submit]').click();await page.waitForSelector('.experience-list .empty');
  assert.equal(await page.locator('.experience-entry').count(),0);
  checks.push('accepted filtering does not turn extraction into verified evidence');
  for(const width of [390,768,1440]){
   await page.setViewportSize({width,height:1050});await page.goto(base+'/#experiences');await page.waitForSelector('.experience-entry');
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'horizontal overflow '+width);
   await page.locator('.experience-entry').first().click();await page.waitForSelector('#experience-detail blockquote');
   await page.screenshot({path:path.join(output,'incremental-'+width+'.png'),fullPage:true});
  }
  checks.push('three responsive widths and readable experience detail');
  const denied=await page.evaluate(async()=>{const root='/api/knowledge-console/libraries/kane/experiences';return (await fetch(root+'?origin=history')).status;});
  assert.equal(denied,403);
  const foreign=await page.evaluate(async()=>{return (await fetch('/api/knowledge-console/libraries/jiangli/experiences/history-'+('c'.repeat(64)))).status;});
  assert.equal(foreign,404);
  checks.push('existing directed permissions and stored-owner material isolation');
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'incremental-browser-results.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
