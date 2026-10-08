async function revealRule(page,selector){await page.waitForSelector(selector,{state:'attached'});const rule=page.locator('.history-rules');if(await rule.count()&&!await rule.evaluate(el=>el.open))await rule.locator(':scope > summary').click();await page.waitForSelector(selector);}
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[],dialogs=[];
 page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>{dialogs.push(d.message());d.dismiss();});
 const done=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert);
 const nav=async(view,selector)=>{await page.locator(`.signal-nav [data-knowledge-view="${view}"]`).click();if(['#clean-instructions','#history-instructions'].includes(selector))await revealRule(page,selector);else await page.waitForSelector(selector);await done();};
 let release,started;const gate=new Promise(r=>release=r),requested=new Promise(r=>started=r);let calls=0;
 try{
  const response=await page.request.post(base+'/api/knowledge/items',{data:{title:'缓存预览测试',body:'Evo 演示项目应保留原文和人工修改。',action:'draft'}});assert.ok(response.ok());const item=await response.json();
  await page.route('**/api/knowledge/cleaning/preview',async route=>{calls++;started();const result=await route.fetch();await gate;await route.fulfill({response:result});});
  await page.goto(base+'/#knowledge/cleaning');await revealRule(page,'#clean-instructions');await done();
  const selected=`[data-clean-select="item:${item.id}"]`,text=`[data-clean-text="item:${item.id}"]`;
  await page.locator(selected).check();await page.locator('[data-clean-action="preview"]').click();await requested;
  await nav('projects','.project-list');
  await nav('cleaning','#clean-instructions');assert.ok(await page.locator(selected).isChecked());assert.ok(await page.locator('[data-clean-action="preview"]').isDisabled());
  await nav('qa','#qa-project-filter');release();
  await page.waitForResponse(r=>r.url().endsWith('/cleaning/preview'));
  await page.waitForTimeout(100);assert.equal(await page.locator('#qa-project-filter').count(),1);assert.equal(await page.locator('#clean-instructions').count(),0);
  await nav('cleaning','#clean-instructions');await page.waitForFunction(s=>document.querySelector(s)?.value.includes('先清洗资料'),text);
  await page.locator(text).fill('人工核对后的缓存文本。');await page.locator(`[data-clean-category="item:${item.id}"]`).selectOption('decision');
  const rule=(await page.locator('#clean-instructions').inputValue())+'\n尚未保存的清洗规则草稿。';await page.locator('#clean-instructions').fill(rule);
  await nav('projects','.project-list');await nav('cleaning','#clean-instructions');
  assert.equal(await page.locator(text).inputValue(),'人工核对后的缓存文本。');assert.equal(await page.locator(`[data-clean-category="item:${item.id}"]`).inputValue(),'decision');assert.ok(await page.locator(selected).isChecked());assert.equal(await page.locator('#clean-instructions').inputValue(),rule);
  assert.equal(calls,1);assert.deepEqual(dialogs,[]);assert.deepEqual(errors,[]);
  console.log('PASS: leave/return during AI, background completion does not overwrite other page, cached preview/text/category/rule/selection, no duplicate AI call or discard dialog');
 }finally{release();await browser.close();}
})().catch(error=>{console.error(error);process.exit(1);});
