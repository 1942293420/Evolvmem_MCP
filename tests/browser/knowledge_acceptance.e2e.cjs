// Identical real page clicks against old and new code; no model calls.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}), base=process.env.EVOLVMEM_TEST_URL;
 const errors=[];page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const fixture=async action=>{const r=await page.request.post(base+'/__acceptance__/'+action);assert.ok(r.ok());return r.json();};
 let decisionClicks=0,refreshClicks=0,aiRequests=0;const started=Date.now();
 page.on('request',r=>{if(/\/groups\/(preview|analyze)$/.test(r.url()))aiRequests++;});
 try{
  await page.goto(base+'/#knowledge/organization');await idle();
  for(let i=0;i<6;i++){
   const step=await fixture('next');await page.locator('[data-rg=refresh]').click();refreshClicks++;await idle();
   await page.waitForFunction(p=>document.querySelectorAll('[data-rg-list] button').length===(p?1:0),step.pending);
   if(step.pending){
    await page.locator('[data-rg-list] button').click();decisionClicks++;
    const form=page.locator('[data-rg-form]');await page.waitForFunction(()=>!document.querySelector('[data-rg-form]').inert);
    const project=form.locator('[name=projects][value=demo]');
    if(!await project.isChecked()){await project.check();decisionClicks++;}
    await form.locator('[data-rg=file]').click();decisionClicks++;await idle();
    await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);
   }
  }
  const result={...await fixture('result'),decision_clicks:decisionClicks,refresh_clicks:refreshClicks,
    total_clicks:decisionClicks+refreshClicks,automation_elapsed_ms:Date.now()-started,ai_requests:aiRequests,errors};
  assert.equal(result.source_count,3);assert.equal(result.missing_sources,0);assert.equal(result.wrong_projects,0);
  assert.equal(aiRequests,0);assert.deepEqual(errors,[]);
  if(process.env.EVOLVMEM_EXPECT_IDENTITY==='1'){
   assert.equal(result.human_saves,1);assert.equal(result.repeat_confirmations,0);assert.equal(result.active_item_ids.length,1);
  }
  if(process.env.EVOLVMEM_ACCEPTANCE_OUTPUT)fs.writeFileSync(process.env.EVOLVMEM_ACCEPTANCE_OUTPUT,JSON.stringify(result,null,2));
  console.log(JSON.stringify(result));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
