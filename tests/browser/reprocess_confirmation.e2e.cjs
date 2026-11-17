// Disposable real HTTP: recovery updates the preview; zero AI matches can be reviewed manually.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(30000);
 const zero=process.env.EVOLVMEM_REVIEW_ZERO_FIXTURE==='1',base=process.env.EVOLVMEM_TEST_URL;
 const api=async(route,body)=>page.evaluate(async({route,body})=>{const r=await fetch('/api/knowledge/'+route,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});const v=await r.json();if(!r.ok)throw Error(JSON.stringify(v));return v;},{route,body});
 try{
  await page.goto(base+'/#knowledge/organization');
  await page.locator('[data-rg-list] > *').filter({hasText:'集中审核演示'}).locator('button').click();
  const form=page.locator('[data-rg-form]');
  await form.locator('[name=projects][value=evo]').check();await form.locator('[name=selection]').selectOption('group');
  await form.locator('[name=condition]').fill('导出编号必须保留原始值，便于回查来源。');await form.locator('[name=exceptions]').fill('跨平台条件另行判断。');
  if(!zero){const rule=await api('rules');await api('rules',{expected_revision:rule.revision,instructions:rule.instructions+'\n浏览器验证重整后自动预览。'});}
  await form.locator('[data-rg=preview]').click();await page.locator('[data-rg-preview] h3').waitFor();
  if(zero){
   assert.equal(await form.locator('[data-rg=apply]').isDisabled(),true);
   assert.doesNotMatch(await form.locator('[data-rg-save-status]').innerText(),/请先点击.*重新整理/);
   const row=form.locator('[data-rg=review-one]').first().locator('..');
   await row.locator('[data-rg=review-one]').click();await row.locator('[data-rg=confirm-one]').waitFor();
   assert.match(await row.locator('.review-original').innerText(),/审核条目/);
   await row.locator('[data-rg=confirm-one]').click();
   await page.waitForFunction(()=>document.querySelector('[data-rg-status]')?.textContent.includes('已人工归入'));
   await page.waitForFunction(()=>document.querySelector('[data-rg-preview]')?.textContent.includes('已人工处理：1 条'));
   assert.match(await form.locator('[data-rg-preview]').innerText(),/已人工处理：1 条/);
  }else{
   await form.locator('[data-rg=reprocess]').click();
   // No second click on preview: the completed recovery must do it itself.
   await page.waitForFunction(()=>document.querySelector('[data-rg=apply]')?.disabled===false);
   assert.equal(await form.locator('[data-rg=reprocess]').count(),0);
   assert.doesNotMatch(await form.locator('[data-rg-reprocess-status]').innerText(),/再点击/);
   assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);
  }
  await form.locator('[data-rg-preview] h3').scrollIntoViewIfNeeded();await page.screenshot({path:'/tmp/evo-reprocess-'+(zero?'manual':'automatic')+'.png'});
  assert.deepEqual(errors,[]);console.log(JSON.stringify({mode:zero?'zero-match-manual':'automatic-recovery',checks:zero?['零条推荐明确显示原因，不误导重整','预览中原地阅读全文，人工确认实际保存','已处理条目从待审核清单退出']:['重整完成自动更新预览，无须第二次点击','已完成来源不再显示重整按钮','保留项目与归类依据'],errors},null,2));
 }catch(e){await page.screenshot({path:'/tmp/evo-reprocess-confirm-failure.png',fullPage:true});throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
