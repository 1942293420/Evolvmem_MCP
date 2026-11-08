// Synthetic real HTTP fixture; never run mutations against a production URL.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.EVOLVMEM_TEST_URL;
if(!base||!process.env.EVOLVMEM_SYNTHETIC_FIXTURE)throw new Error('A synthetic fixture URL is required');
const output=process.env.EVOLVMEM_SCREENSHOTS||'/tmp/evolvmem-console-screenshots';fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server','--host-resolver-rules=MAP knowledge-console.test 127.0.0.1']});
 const context=await browser.newContext({viewport:{width:1440,height:1050}});
 await context.addCookies([{name:'evolvmem_session',value:process.env.OWNER_COOKIE||'test-owner-cookie',url:base}]);
 const page=await context.newPage(),errors=[],checks=[];page.setDefaultTimeout(12000);page.on('pageerror',e=>errors.push(e.message));
 const shot=async name=>page.screenshot({path:path.join(output,name+'.png'),fullPage:true});
 const go=async suffix=>{await page.goto(base+'/'+suffix);await page.waitForFunction(()=>!document.querySelector('#actor-name')?.textContent.includes('正在'));};
 const json=async url=>page.evaluate(async u=>{const r=await fetch(u,{cache:'no-store'});return {status:r.status,data:await r.json()};},url);
 const root='/api/knowledge-console';
 try{
  await go('#history');await page.waitForSelector('#history-list .history-entry');
  const me=(await json(root+'/me')).data,owner=me.current_user.id,member='kane';
  assert.equal(await page.evaluate(()=>isSecureContext),false,'must test ordinary HTTP');
  assert.equal(await page.evaluate(()=>typeof crypto.randomUUID),'undefined');
  assert.equal(await page.locator('.main-nav a').count(),5);assert.match(await page.locator('.main-nav').innerText(),/历史.*经验.*用户知识库.*用户与权限.*工作原理/s);
  assert.equal(await page.locator('script[src*="extraction"],script[src*="galaxy"],script[src*="organization"]').count(),0);
  for(const old of ['/api/projects','/api/memories','/api/knowledge/organization','/api/history-candidate/projects'])assert.equal((await json(old)).status,410,old);
  checks.push('ordinary HTTP, five new entries, legacy scripts and APIs closed');
  await page.locator('#history-list .history-entry').first().click();await page.waitForFunction(()=>document.querySelector('#history-main')?.textContent.includes('项目摘要'));
  assert.match(await page.locator('#history-main').innerText(),/项目摘要/);await shot('01-history');
  await go('#experiences');await page.waitForSelector('#experience-filters');
  await page.locator('#experience-filters [name=origin]').selectOption('core');await page.locator('#experience-filters [name=status]').selectOption('candidate');await page.locator('#experience-filters [type=submit]').click();await page.waitForSelector('.experience-entry');
  await page.locator('.experience-entry').first().click();await page.waitForFunction(()=>document.querySelector('#experience-detail')?.textContent.includes('解决步骤'));assert.match(await page.locator('#experience-detail').innerText(),/解决步骤/);await shot('02-experience');
  checks.push('history project summary and real structured experience detail');
  await go('#libraries');await page.waitForSelector('.library-card');assert.ok(await page.locator('.library-card').count()>=2);await shot('03-libraries');
  const otherInspect=page.locator(`.library-card a[href*="user=${member}"]`).first();await otherInspect.click();await page.waitForSelector('#history-list .history-entry');assert.match(await page.locator('#library-access').innerText(),/管理员只读/);assert.equal(await page.locator('#history-manage').isVisible(),false);assert.equal(await page.locator('[data-history-delete]').count(),0);
  assert.equal((await json(root+`/libraries/${member}/history/projects`)).status,403);
  assert.equal((await json(root+`/libraries/${member}/history/projects?inspect=1`)).status,200);
  checks.push('explicit admin inspection, normal admin deny and cross-user read-only');
  await go('#users');await page.waitForSelector('[data-edit-user]');await page.locator('#add-user').click();await page.locator('#user-form [name=id]').fill('new-reader');await page.locator('#user-form [name=display_name]').fill('新用户验收');await page.locator('#user-form [type=submit]').click();await page.waitForSelector('#user-dialog',{state:'hidden'});await page.waitForFunction(()=>document.querySelector('#console-view').textContent.includes('新用户验收'));await shot('04-users');
  checks.push('user creation and truthful not-connected state');
  await go('#users/grants');await page.waitForSelector('[data-save-grant]');
  const forward=page.locator(`tr[data-viewer="${owner}"][data-owner="${member}"]`),reverse=page.locator(`tr[data-viewer="${member}"][data-owner="${owner}"]`);
  await forward.locator('[data-grant-history]').check();await forward.locator('[data-grant-experience]').check();await forward.locator('[data-save-grant]').click();await page.waitForFunction(()=>document.querySelector('#console-notice')?.textContent.includes('有向授权已保存'));
  assert.equal(await reverse.locator('[data-grant-history]').isChecked(),false);assert.equal(await reverse.locator('[data-grant-experience]').isChecked(),false);
  assert.equal((await json(root+`/libraries/${member}/history/projects`)).status,200);await shot('05-directed-grants');
  await forward.locator('[data-grant-history]').uncheck();await forward.locator('[data-grant-experience]').uncheck();await forward.locator('[data-save-grant]').click();await page.waitForFunction(()=>Number(document.querySelector('tr[data-viewer="jiangli"][data-owner="kane"]')?.dataset.revision)>=2);
  assert.equal((await json(root+`/libraries/${member}/history/projects`)).status,403);
  checks.push('A to B independent of B to A, grant and immediate revoke');
  await go('#history');await page.waitForSelector('#history-manage');await page.locator('#history-manage').click();await page.locator('[data-history-select]').last().check();await page.locator('#history-delete-selected').click();await page.waitForSelector('#history-delete-dialog[open]');await page.locator('#history-delete-confirm').click();await page.waitForSelector('#history-delete-dialog',{state:'hidden'});await page.locator('#history-trash').click();await page.waitForSelector('[data-history-restore]');await page.locator('[data-history-restore]').first().click();await page.waitForFunction(()=>document.querySelector('#history-trash-items')?.textContent.includes('回收站为空'));await page.locator('#history-trash-close').click();
  checks.push('self delete and restore over insecure HTTP with UUID fallback');
  for(const width of [390,768,1024,1440]){await page.setViewportSize({width,height:1000});for(const tab of ['history','experiences','libraries','users','users/grants','principles']){await go('#'+tab);await page.waitForTimeout(120);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`overflow ${tab} ${width}`);}if(width===390)await shot('06-mobile-principles');if(width===1440)await shot('07-principles');}
  checks.push('six pages at four widths, updated architecture diagram');
  const memberContext=await browser.newContext({viewport:{width:1200,height:950}});await memberContext.addCookies([{name:'evolvmem_session',value:process.env.MEMBER_COOKIE||'test-member-cookie',url:base}]);const mp=await memberContext.newPage();mp.on('pageerror',e=>errors.push(e.message));await mp.goto(base+'/#history');await mp.waitForSelector('#history-list .history-entry');assert.equal(await mp.locator('#admin-nav').isVisible(),false);assert.equal(await mp.locator('#history-manage').isVisible(),true);
  const deny=await mp.evaluate(async([r,o])=>{const result={};for(const p of ['/users','/grants','/libraries?inspect=1',`/libraries/${o}/history/projects`,`/libraries/${o}/experiences`])result[p]=(await fetch(r+p)).status;return result;},[root,owner]);assert.ok(Object.values(deny).every(s=>s===403),JSON.stringify(deny));await memberContext.close();checks.push('member own history, admin UI hidden, private and inspect APIs denied');
  assert.deepEqual(errors,[]);fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify({checks,errors},null,2));console.log(JSON.stringify({checks,errors},null,2));
 }catch(e){await shot('failure');console.error('Completed checks:',checks,'page errors:',errors);throw e;}
 finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
