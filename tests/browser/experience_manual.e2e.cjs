// Real HTTP, synthetic owner/member databases only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.EVOLVMEM_TEST_URL;
if(!base||!process.env.EVOLVMEM_SYNTHETIC_FIXTURE||!['127.0.0.1','knowledge-console.test'].includes(new URL(base).hostname))throw new Error('Synthetic loopback fixture required');
const output=process.env.EVOLVMEM_SCREENSHOTS||'/tmp/evolvmem-manual-screenshots';fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP knowledge-console.test 127.0.0.1']});
 const context=await browser.newContext({viewport:{width:1440,height:1050}});await context.addCookies([{name:'evolvmem_session',value:'test-owner-cookie',url:base}]);
 const page=await context.newPage(),checks=[],errors=[],calls=[];page.setDefaultTimeout(9000);page.on('pageerror',e=>errors.push(e.message));page.on('request',r=>calls.push(r.url()));
 const go=async(suffix='')=>{await page.goto(base+'/'+suffix+'#experiences');await page.waitForSelector('#experience-filters');};
 const shot=async name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
 const fill=async(category,problem,solution,reusable)=>{const f=page.locator('#experience-form');await f.locator('[name=category]').selectOption(category);await f.locator('[name=problem]').fill(problem);await f.locator('[name=solution]').fill(solution);await f.locator('[name=reusable_experience]').fill(reusable);};
 const save=async()=>{await page.locator('#experience-form [type=submit]').click();await page.waitForSelector('#experience-dialog',{state:'hidden'});};
 try{
  await go('?origin=history&review=accepted');
  assert.deepEqual(await page.locator('[data-experience-category] .experience-category-name').allTextContents(),['前端经验','业务经验','系统环境经验']);
  assert.equal(await page.locator('#experience-new').count(),1);
  assert.equal(await page.locator('.experience-progress,[name=origin],[name=review],[name=status]').count(),0);
  assert.equal(await page.locator('.experience-entry').count(),0);assert.equal(await page.evaluate(()=>isSecureContext),false);
  checks.push('three manual categories; old extraction/Core excluded; ordinary HTTP');await shot('01-empty-desktop');
  await page.locator('#experience-new').click();assert.equal(await page.locator('#experience-form [name=title]').count(),0);
  await fill('前端经验','窗口缩放后弹层超出边界 <img src=x onerror=alert(1)>','按可用视口重新计算弹层位置。','页面缩放和窗口尺寸变化后重新定位。');
  const mutationUrl=base+'/api/knowledge-console/libraries/jiangli/experiences';const requestIds=[];
  page.on('request',r=>{if(r.method()==='POST'&&r.url()===mutationUrl)requestIds.push(r.postDataJSON().request_id);});
  let release;const delay=new Promise(resolve=>release=resolve);let reached;const received=new Promise(resolve=>reached=resolve);
  await page.route(mutationUrl,async route=>{const response=await route.fetch({url:route.request().url().replace('knowledge-console.test','127.0.0.1'),headers:{...route.request().headers(),host:new URL(base).host}});assert.equal(response.status(),201);reached();await delay;await route.abort('failed');});
  await page.locator('#experience-form [type=submit]').click();await received;
  assert.equal(await page.locator('#experience-dialog [data-close-experience]').first().isDisabled(),true);
  await page.keyboard.press('Escape');assert.equal(await page.locator('#experience-dialog').evaluate(e=>e.open),true);release();
  await page.waitForFunction(()=>document.querySelector('#experience-form-error')?.textContent.includes('网络连接中断'));
  assert.match(await page.locator('#experience-form [name=problem]').inputValue(),/窗口缩放/);await page.unroute(mutationUrl);await save();
  assert.equal(requestIds.length,2);assert.equal(requestIds[0],requestIds[1]);checks.push('pending dialog protected; lost response retry reuses request ID');
  await page.waitForSelector('#experience-edit');assert.equal(await page.locator('.experience-entry').count(),1);
  assert.match(await page.locator('#experience-detail').innerText(),/解决办法/);assert.equal(await page.locator('#experience-detail img').count(),0);
  assert.doesNotMatch(await page.locator('#experience-detail').innerText(),/已验证|解决步骤|关键改动|待会话复核/);checks.push('manual create, three fields, escaped content, no verification inflation');
  await go();await page.waitForSelector('.experience-entry');await page.locator('#experience-edit').click();
  await fill('业务经验','同一费用被重复扣减','核对费用是否已计入应付款，再决定是否另扣。','先确定费用是否已包含在主金额中。');await save();
  await page.locator('[data-experience-category="业务经验"]').click();await page.waitForSelector('#experience-edit');assert.match(await page.locator('#experience-detail').innerText(),/同一费用被重复扣减/);checks.push('persistent edit and category move');
  await page.locator('#experience-filters [name=q]').fill('不存在的词');await page.locator('#experience-filters [type=submit]').click();await page.waitForFunction(()=>document.querySelector('#experience-results')?.textContent.includes('没有匹配'));
  await page.locator('#experience-clear').click();await page.waitForSelector('.experience-entry');checks.push('search and empty-state clear retain category');
  await page.locator('.experience-entry').first().click();await page.waitForFunction(()=>location.hash.includes('manual-'));await page.waitForSelector('#experience-delete');
  await page.locator('#experience-delete').click();await page.waitForSelector('#experience-delete-dialog[open]');await page.locator('#experience-delete-cancel').click();assert.equal(await page.locator('.experience-entry').count(),1);
  await page.locator('#experience-delete').click();await page.locator('#experience-delete-confirm').click();await page.waitForSelector('#experience-delete-dialog',{state:'hidden'});await page.waitForFunction(()=>document.querySelectorAll('.experience-entry').length===0);
  assert.equal(await page.locator('#experience-detail [role=alert]').count(),0);assert.equal(await page.evaluate(()=>location.hash),'#experiences');
  await page.locator('#experience-trash').click();await page.waitForSelector('#experience-restore');await page.locator('.experience-entry').first().click();await page.waitForFunction(()=>location.hash.includes('manual-'));await page.waitForSelector('#experience-restore');await page.locator('#experience-restore').click();await page.waitForFunction(()=>document.querySelectorAll('.experience-entry').length===0);assert.equal(await page.locator('#experience-detail [role=alert]').count(),0);
  await page.locator('#experience-trash').click();await page.waitForSelector('.experience-entry');checks.push('cancel deletion, soft delete, recycle-bin restore');await shot('02-business-desktop');
  await page.locator('[data-experience-category="系统环境经验"]').click();await page.locator('#experience-new').click();await fill('系统环境经验','服务切换环境后配置未生效','核对当前进程使用的配置来源。','保留配置来源和生效范围。');
  for(const width of [390,768,1440]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));assert.ok(await page.locator('#experience-dialog').evaluate(e=>e.scrollWidth<=e.clientWidth+2));await shot('03-editor-'+width);}
  await save();for(const width of [390,768,1440]){await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await shot('04-library-'+width);}checks.push('editor and reading layouts at 390/768/1440');
  const csrf=(await page.evaluate(async()=>{return(await(await fetch('/api/knowledge-console/me')).json()).csrf_token;}));
  const grant=await page.evaluate(async token=>{const r=await fetch('/api/knowledge-console/grants',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':token},body:JSON.stringify({viewer_id:'kane',owner_id:'jiangli',history_read:false,experience_read:true,revision:0})});return r.status;},csrf);assert.equal(grant,200);
  const memberContext=await browser.newContext();await memberContext.addCookies([{name:'evolvmem_session',value:'test-member-cookie',url:base}]);const mp=await memberContext.newPage();await mp.goto(base+'/?user=jiangli&category='+encodeURIComponent('业务经验')+'#experiences');await mp.waitForSelector('.experience-entry');assert.equal(await mp.locator('#experience-new,#experience-edit,#experience-delete,#experience-restore').count(),0);assert.equal(await mp.locator('#experience-trash').isVisible(),false);assert.equal(await mp.evaluate(async()=>{return(await fetch('/api/knowledge-console/libraries/jiangli/experiences?origin=manual&deleted=1')).status;}),403);await memberContext.close();
  await go('?user=kane&inspect=1');assert.equal(await page.locator('#experience-new').count(),0);assert.equal(await page.locator('#experience-trash').isVisible(),false);checks.push('directed cross-user read and admin inspection remain read-only; foreign trash denied');
  assert.ok(!calls.some(url=>/\/api\/.*(extract|organize|summari[sz]e)/.test(url)));assert.deepEqual(errors,[]);checks.push('no extraction requests or browser errors');
  fs.writeFileSync(path.join(output,'manual-browser-results.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors}));
 }catch(e){await shot('failure');console.error('Checks:',checks,'errors:',errors);throw e;}finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
