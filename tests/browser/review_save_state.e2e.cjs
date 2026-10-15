// Disposable fixture only: real saves, refresh failure, draft retry and page shrink.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1512,height:1000}}),base=process.env.EVOLVMEM_TEST_URL,errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 const api=async(route,data)=>{const r=data===undefined?await page.request.get(base+'/api/knowledge/'+route):await page.request.post(base+'/api/knowledge/'+route,{data});assert.ok(r.ok(),await r.text());return r.json();};
 const idle=()=>page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');
 const click=async selector=>{await page.locator(selector).click();await idle();};
 const form=page.locator('[data-rg-form]');
 const firstPage=async()=>{while(await page.locator('[data-rg=previous]').isEnabled())await click('[data-rg=previous]');};
 const openTitle=async title=>{await firstPage();let item=page.locator('[data-rg-list] button').filter({hasText:title});while(!await item.count()&&await page.locator('[data-rg=next]').isEnabled())await click('[data-rg=next]');await item.first().click();await page.waitForFunction(t=>!document.querySelector('[data-rg-form]').inert&&!document.querySelector('[data-rg-form]').hidden&&document.querySelector('[data-rg-title]').textContent.includes(t),title);};
 const preview=async()=>{await form.locator('[name=projects][value=evo]').check();await click('[data-rg=preview]');await page.waitForFunction(()=>!document.querySelector('[data-rg=apply]').disabled);};
 const receipt=async()=>{await page.waitForFunction(()=>document.querySelector('[data-rg-form]').hidden);assert.equal(await page.locator('[data-rg=apply]').isVisible(),false);assert.equal(await page.locator('[data-rg-list] [aria-pressed=true]').count(),0);};
 try{
  await page.goto(base+'/#knowledge/organization');await idle();
  await openTitle('完整任务演示');await preview();
  const member={task_id:Number(await form.locator('[name=task_id]').inputValue()),digest:await form.locator('[name=digest]').inputValue()};
  let writes=0;page.on('request',r=>{if(r.url().endsWith('/groups/decisions'))writes++;});
  await page.route('**/organization/groups/decisions',r=>r.fulfill({status:500,json:{error:'临时保存失败'}}),{times:1});
  await click('[data-rg=apply]');assert.equal(await form.isVisible(),true);assert.equal(await form.locator('[data-rg=apply]').isEnabled(),true);assert.equal(await form.locator('[name=projects][value=evo]').isChecked(),true);assert.match(await page.locator('[data-rg-status]').innerText(),/临时保存失败/);
  // Saving succeeds, then all list refreshes fail until the user retries Refresh.
  await page.route('**/organization/groups?*',r=>r.fulfill({status:500,json:{error:'列表暂时不可用'}}));
  await click('[data-rg=apply]');await receipt();assert.match(await page.locator('[data-rg-empty]').innerText(),/审核结果已保存/);assert.match(await page.locator('[data-rg-status]').innerText(),/已保存.*刷新失败.*无需再次保存/);
  let saved=await api('organization/detail?task_id='+member.task_id);assert.equal(saved.units.find(u=>u.digest===member.digest).decision,'manual');assert.equal(writes,2);
  await page.unroute('**/organization/groups?*');await click('[data-rg=refresh]');await receipt();assert.equal(await page.locator('[data-rg-list] button').filter({hasText:'完整任务演示'}).count(),0);assert.equal(writes,2);
  await click('.signal-nav [data-knowledge-view=projects]');await click('.signal-nav [data-knowledge-view=organization]');await receipt();assert.match(await page.locator('[data-rg-empty]').innerText(),/审核结果已保存/);
  await page.screenshot({path:'/tmp/evo-review-save-receipt.png'});
  // Setting a value-review item aside removes the entire old editor.
  await openTitle('保留价值演示');await click('[data-rg=aside]');await receipt();assert.match(await page.locator('[data-rg-empty]').innerText(),/已暂存/);assert.equal(await page.locator('[data-rg-list] button').filter({hasText:'保留价值演示'}).filter({hasNotText:'保留价值演示二'}).count(),0);
  await openTitle('保留价值演示二');const valueId=Number(await form.locator('[name=task_id]').inputValue()),valueDigest=await form.locator('[name=digest]').inputValue();await click('[data-rg=keep]');
  assert.equal(await page.locator('[data-rg=keep]').isVisible(),false,'saved Keep must not leave the old value decision');
  const kept=await api('organization/detail?task_id='+valueId);assert.equal(kept.units.find(u=>u.digest===valueDigest).disposition,'keep');
  await firstPage();
  await page.locator('[data-rg-list] button').filter({hasText:'审核条目'}).first().click();await preview();await form.locator('[data-rg-choice]').selectOption('exclude');await click('[data-rg=apply]');await receipt();assert.match(await page.locator('[data-rg-empty]').innerText(),/不录入仅针对这次选择的范围/);
  // The last page shrinks after another review, then the final visible item is saved.
  let last=await api('organization/groups?page=1'),lastPage=Math.ceil(last.total/last.page_size);assert.ok(lastPage>1);
  await firstPage();for(let i=1;i<lastPage;i++)await click('[data-rg=next]');
  last=await api('organization/groups?page='+lastPage);
  for(const group of last.items.slice(0,-1)){
   const detail=await api('organization/groups/detail?group_id='+group.id);
   for(const unit of detail.items)await api('organization/disposition',{task_id:unit.task_id,digest:unit.digest,expected_revision:unit.revision,disposition:'set_aside',reason:'测试最后一页收缩'});
  }
  await click('[data-rg=refresh]');assert.equal(await page.locator('[data-rg-list] button').count(),1);
  await page.locator('[data-rg-list] button').click();await preview();await click('[data-rg=apply]');await receipt();
  assert.match(await page.locator('[data-rg-page]').innerText(),new RegExp(`第 ${lastPage-1} / ${lastPage-1} 页`));assert.ok(await page.locator('[data-rg-list] button').count()>0);
  const current=await api('organization/groups');assert.match(await page.locator('[data-rg-total]').innerText(),new RegExp(`共 ${current.unit_count} 条`));
  // External handling followed by Refresh must not leave an obsolete editor.
  await page.locator('[data-rg-list] button').first().click();await form.locator('[data-rg-title]').waitFor();await page.waitForFunction(()=>!document.querySelector('[data-rg-form]').inert);
  const detail=await api('organization/groups/detail?group_id='+await form.locator('[name=group_id]').inputValue());
  for(const unit of detail.items)await api('organization/disposition',{task_id:unit.task_id,digest:unit.digest,expected_revision:unit.revision,disposition:'set_aside',reason:'测试其他入口已处理'});
  await click('[data-rg=refresh]');await receipt();assert.match(await page.locator('[data-rg-empty]').innerText(),/已处理/);
  for(const width of [390,768,1512]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));}
  assert.deepEqual(errors,[]);console.log(JSON.stringify({checks:['保存失败保留选择并可重试','保存成功但刷新失败明确区分，无重复提交','保存后退出旧详情、更新列表，切页返回不恢复旧审核','暂存退出旧审核，保留后显示最新状态','不录入保持项目范围语义','处理最后一条后回到有效分页，数量一致','其他入口处理后刷新清除旧详情','三种宽度无横向溢出'],errors}));
 }catch(error){await page.screenshot({path:'/tmp/evo-review-save-state-failed.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
