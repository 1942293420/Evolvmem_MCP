// Actual browser -> isolated Web HTTP -> PG -> resident fake encoder chain.
const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.EDITOR_PG_URL,noteId=process.env.EDITOR_PG_NOTE_ID;
if(!base||new URL(base).hostname!=='127.0.0.1'||new URL(base).port==='9377'||process.env.EVOLVMEM_SYNTHETIC_FIXTURE!=='1')throw new Error('isolated synthetic URL required');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server']});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1050}});
  await context.addCookies([{name:'evolvmem_session',value:'tok-owner-v2',url:base}]);
  const page=await context.newPage(),errors=[],responses=[];page.on('response',r=>{if(r.url().includes('/api/'))responses.push({path:new URL(r.url()).pathname,status:r.status()});});page.on('pageerror',e=>errors.push(String(e)));page.setDefaultTimeout(12000);
  await page.goto(base+'/v2/');
  await page.waitForFunction(()=>window.EvolvMemV2?.state?.apiMode==='live' && window.EvolvMemV2?.state?.ownerId==='admin_owner');
  await page.locator('[data-act="goto-tab"][data-route="experiences"]:visible').first().click();
  try{await page.locator('[data-act="edit-manual-exp"][data-id="'+noteId+'"]').click();}
  catch(e){const d=await page.evaluate(()=>{const E=window.EvolvMemV2;return {url:location.pathname,session:E?.session?.status,mode:E?.state?.apiMode,owner:E?.state?.ownerId,library:E?.state?.libraryCtx,error:E?.state?.apiError,route:E?.state?.route,subtab:E?.state?.expSubtab,notes:E?.state?.experiences?.map(x=>({id:x.id,origin:x.origin,status:x.status,revision:x.revision}))};});console.error(JSON.stringify({state:d,responses}));throw e;}
  assert.match(await page.locator('#new-exp-prob').inputValue(),/PG RLS/);
  await page.locator('#new-exp-prob').fill('真实浏览器修改的经验正文');
  await page.locator('#new-exp-sol').fill('通过页面保存后核对数据库与向量哈希');
  await page.locator('#new-exp-reus').fill('编辑后同时核对正文和双向量');
  const response=page.waitForResponse(r=>r.request().method()==='POST'&&r.url().endsWith('/experiences/write'));
  await page.locator('#new-exp-submit').click();const res=await response,data=await res.json();
  assert.equal(res.status(),200,JSON.stringify(data));assert.equal(data.note.revision,2);
  await page.waitForFunction(()=>/保存成功/.test(document.getElementById('new-exp-status')?.textContent||''));
  assert.equal(await page.locator('#new-exp-prob').inputValue(),'真实浏览器修改的经验正文');
  assert.equal(await page.locator('#new-exp-submit').isDisabled(),true);
  assert.deepEqual(errors,[]);fs.mkdirSync('/tmp/web-manual-editor-pg',{recursive:true});
  await page.screenshot({path:'/tmp/web-manual-editor-pg/saved.png',fullPage:true});
  console.log(JSON.stringify({passed:true,id:data.note.id,revision:data.note.revision,visible_success:true,page_errors:errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
