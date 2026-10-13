const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}});page.setDefaultTimeout(20000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  const records=Array.from({length:400},(_,i)=>({source_key:'archive:'+i,title:'原始来源 '+i,adapter:'codex',created_at:'2026-10-13',current:true,focus:i===250,unit_start:0,unit_end:12,relation:i===250?'current':'session_background',text:(i===0?'最初需求：执行器需要支持取消。':i===399?'最终结果：取消后没有残留进程。':`处理过程 ${i}。`)+('原始会话段落，保留完整内容。'.repeat(500))}));
  records[250].text='完整的长段前文。'.repeat(2000)+'正在核对的记忆。'+'完整的后续结果。'.repeat(2000);
  records[250].unit_start='完整的长段前文。'.length*2000;records[250].unit_end=records[250].unit_start+'正在核对的记忆。'.length;
  await page.route('**/api/knowledge/organization/context?*',r=>r.fulfill({contentType:'application/json',body:JSON.stringify({records,stages:[],notices:[]})}));
  await page.locator('[data-rg-list] button').first().click();
  await page.locator('[data-rg-source] summary').first().click();
  const reader=page.locator('[data-rg-source-text]');
  await reader.locator('.task-context-source').first().waitFor();
  assert.ok(await reader.locator('.task-context-source').count()<=20,'only a bounded page of original sources may be rendered');
  assert.ok((await reader.innerText()).length<150000,'reader must not insert megabytes into the document');
  assert.match(await reader.innerText(),/最初需求/);
  await reader.locator('[data-context-locate]').click();
  await reader.locator('[data-context-focus]').waitFor();
  assert.match(await reader.innerText(),/原始来源 250/);
  assert.equal(await reader.locator('[data-context-focus]').textContent(),'正在核对的记忆。');
  await reader.locator('[data-context-start]').click();
  assert.match(await reader.innerText(),/最初需求/);
  assert.equal(await reader.locator('[data-context-records]').evaluate(el=>el.scrollTop),0,'returning to the beginning resets the reading scroll position');
  await reader.locator('[data-context-next]').click();
  assert.doesNotMatch(await reader.innerText(),/最初需求/);
  await reader.locator('[data-context-previous]').click();
  assert.match(await reader.innerText(),/最初需求/);
  assert.equal(await reader.locator('[data-context-records]').evaluate(el=>el.scrollTop),0);
  const text=[];
  while(true){
   text.push(...await reader.locator('.task-context-text').allTextContents());
   if(await reader.locator('[data-context-next]').isDisabled())break;
   await reader.locator('[data-context-next]').click();
  }
  assert.equal(text.join(''),records.map(r=>r.text).join(''),'every original character remains readable across pages, including long sources');
  assert.match(await reader.innerText(),/最终结果/);
  await reader.locator('[data-context-start]').click();
  for(const width of [390,768,1512]){
   await page.setViewportSize({width,height:1000});
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
  }
  await page.screenshot({path:'/tmp/evo-context-paging.png'});
  console.log(JSON.stringify({sources:400,checks:['bounded original rendering','first requirement','locate current memory across pages','previous and next original pages','reset reading position','all original characters retained','three viewport widths']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
