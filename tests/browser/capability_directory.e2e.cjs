const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  await page.setContent('<div id="root"></div>');
  await page.evaluate(()=>{
   window.calls=[];window.tabs=[];window.failNext=false;
   window.EvolvAuth={canWrite:false,fetch:async url=>{
    calls.push(url);const p=new URL(url,'http://fixture').searchParams;
    if(failNext){failNext=false;return {ok:false,json:async()=>({error:'fixture_failure'})};}
    const filtered=!!p.get('intake_q'),total=filtered?1:517,ip=filtered?1:Number(p.get('intake_page')||1);
    return {ok:true,json:async()=>({categories:[{id:'frontend',label:'前端'},{id:'backend',label:'后端'}],counts:{frontend:24,backend:12},source_projects:['fixture'],items:[],total:36,page:1,pages:2,intake_total:total,intake_all_total:517,intake_page:ip,intake_pages:Math.ceil(total/20),intake_page_size:20,suggestions:[],suggestions_total:0,intake:Array.from({length:filtered?1:20},(_,i)=>({intake_id:String((ip-1)*20+i+1),title:'待分类 '+((ip-1)*20+i+1),content:'测试内容',source_project:'fixture',suggested_category:'frontend',revision:1}))})};
   }};
  });
  const source=process.env.CAPABILITIES_JS||path.join(__dirname,'../../evolvmem/web_static/capabilities.js');
  await page.addScriptTag({content:fs.readFileSync(fs.existsSync(source)?source:path.join(__dirname,'root-src/capabilities.js'),'utf8')});
  await page.evaluate(()=>{window.handle=EvolvCapabilities.render({mount:document.querySelector('#root'),esc:v=>String(v??''),initialTab:'intake',onTab:tab=>tabs.push(tab)});});
  await page.waitForFunction(()=>document.querySelector('[data-intake-count]').textContent==='517');
  assert.equal(await page.locator('[data-tab="intake"]').getAttribute('aria-selected'),'true','deep link must open pending tab');
  await page.locator('[data-intake-page="next"]').click();
  await page.waitForSelector('[data-intake-card="21"]');
  assert.match(await page.locator('[data-intake-page-status]').textContent(),/2.*26.*517/);
  await page.locator('[data-intake-query]').fill('unique');
  await page.locator('[data-intake-tools]').evaluate(form=>form.requestSubmit());
  await page.waitForFunction(()=>document.querySelectorAll('[data-intake-card]').length===1);
  assert.equal(await page.locator('[data-intake-count]').textContent(),'517','badge is all pending, not filtered count');
  const params=new URL((await page.evaluate(()=>calls.at(-1))),'http://fixture').searchParams;
  assert.equal(params.get('intake_page'),'1');assert.equal(params.get('intake_q'),'unique');assert.equal(params.get('q'),'');
  await page.evaluate(()=>{handle.suspend();handle.resume('formal');});
  assert.equal(await page.locator('[data-tab="formal"]').getAttribute('aria-selected'),'true','cached return honors explicit route');
  await page.locator('[data-tab="intake"]').click();
  assert.equal(await page.evaluate(()=>tabs.at(-1)),'intake');
  await page.evaluate(()=>{failNext=true;handle.refresh();});
  await page.waitForSelector('[data-capability-notice].is-error');
  assert.equal(await page.locator('[data-intake-card]').count(),1,'failure retains prior results');
  assert.match(await page.locator('[data-result-count]').textContent(),/读取未完成/);
  assert.equal(await page.locator('[data-assign]').isDisabled(),true,'read-only permission preserved');
  console.log(JSON.stringify({checks:['pending deep link','all 517 pageable','independent filters and global badge','cached explicit route','tab callback','failure retains results','read-only'],passed:true}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
