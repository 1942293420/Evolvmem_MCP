// Legacy cases remain preserved, while the experience page uses manual notes only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE);
const base=process.env.EVOLVMEM_TEST_URL,output=process.env.EVOLVMEM_SCREENSHOTS;
if(!base||!process.env.EVOLVMEM_SYNTHETIC_FIXTURE)throw new Error('Synthetic fixture required');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP knowledge-console.test 127.0.0.1']});
 const context=await browser.newContext(),errors=[],checks=[];await context.addCookies([{name:'evolvmem_session',value:'test-owner-cookie',url:base}]);
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
 try{
  for(const old of ['?origin=history&review=accepted','?origin=core&status=verified','?category='+encodeURIComponent('后端与接口')]){
   await page.goto(base+'/'+old+'#experiences');await page.waitForSelector('#experience-filters');
   assert.equal(await page.locator('[data-experience-category]').count(),3);assert.equal(await page.locator('.experience-entry').count(),0);
   assert.equal(await page.locator('[name=origin],[name=review],[name=status],.experience-progress').count(),0);
  }
  checks.push('old extraction/Core URL parameters cannot reopen removed interfaces');
  const data=await page.evaluate(async()=>{
   const root='/api/knowledge-console/libraries/jiangli/experiences';return {core:await(await fetch(root+'?origin=core&status=all')).json(),manual:await(await fetch(root+'?origin=manual')).json(),other:(await fetch('/api/knowledge-console/libraries/kane/experiences?origin=manual')).status};
  });assert.ok(data.core.total>=2);assert.equal(data.manual.total,0);assert.equal(data.other,403);
  checks.push('old Core records retained, manual source isolated, cross-user denial preserved');
  for(const width of [390,768,1440]){await page.setViewportSize({width,height:1050});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
  assert.deepEqual(errors,[]);fs.mkdirSync(output,{recursive:true});fs.writeFileSync(path.join(output,'legacy-isolation-results.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
