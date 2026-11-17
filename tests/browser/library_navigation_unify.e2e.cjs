// Focused navigation contract. Only the existing isolated HTTP fixture is allowed.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=process.env.EVOLVMEM_TEST_URL,fixtureFile=process.env.EVOLVMEM_SYNTHETIC_FIXTURE;
assert.ok(base&&fixtureFile,'Provide the isolated fixture URL and its fixture.json');
assert.equal(new URL(base).hostname,'knowledge-console.test','Browser resolver must map this test-only hostname to loopback');
const fixture=JSON.parse(fs.readFileSync(fixtureFile,'utf8'));
assert.ok(fixture.owner_cookie.startsWith('test-')&&fixture.member_cookie.startsWith('test-'),'Never use production credentials');
const output=process.env.EVOLVMEM_SCREENSHOTS||path.join(__dirname,'screenshots');fs.mkdirSync(output,{recursive:true});
const ROOT='/api/knowledge-console',results=[],errors=[];
const names={[fixture.owner_id]:'合成管理员',[fixture.member_id]:'合成成员'};
let browser,contexts=[],closed=false;
async function check(name,work){try{await work();results.push({name,passed:true});}catch(e){results.push({name,passed:false,error:e.message});}}
async function json(page,url,body,csrf){return page.evaluate(async({url,body,csrf})=>{const response=await fetch(url,{method:body===undefined?'GET':'POST',credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},...(body===undefined?{}:{body:JSON.stringify(body)})});return {status:response.status,data:await response.json()};},{url,body,csrf});}
async function boot(page){await page.goto(base+'/#libraries');await page.waitForSelector('.library-card');}
async function nav(page,kind){await page.locator(`.main-nav [data-nav="${kind}"]`).click();await ready(page,kind);}
async function ready(page,kind){if(kind==='libraries')await page.waitForSelector('.library-card');else if(kind==='history')await page.waitForSelector('#history-list .history-entry');else if(kind==='experiences')await page.waitForSelector('.experience-entry');else if(kind==='users')await page.waitForSelector('[data-edit-user]');else await page.waitForFunction(()=>document.querySelector('#console-view')?.textContent.includes('工作原理'));}
async function choose(page,owner,kind){
  const cards=page.locator('.library-card').filter({has:page.locator('.library-id').filter({hasText:new RegExp('^'+owner+'$')})});
  assert.equal(await cards.count(),1,'Choose an owner through its overview card');
  await cards.locator(`a[href$="#${kind}"]`).click();await ready(page,kind);
  assert.equal(new URL(page.url()).searchParams.get('user'),owner,'Card must select its own library');
}
async function actor(page,id){const me=await json(page,ROOT+'/me');assert.equal(me.status,200);assert.equal(me.data.current_user.id,id,'Viewing a library must never replace login actor');assert.equal(await page.locator('#actor-name').innerText(),names[id]);}
async function contentContext(page,owner,label){
  assert.equal(await page.locator('#library-context').isVisible(),true,'History/experience needs visible static owner context');
  assert.equal((await page.locator('#library-owner').innerText()).replace(/\s+/g,''),'当前查看：'+names[owner]+'的知识库');
  assert.match(await page.locator('#library-access').innerText(),label);
  assert.equal(await page.locator('.library-context select').count(),0,'Content context must not offer another owner selector');
}
async function globalContext(page){
  assert.equal(await page.locator('#library-context').isVisible(),false,'Overview/admin/principles must not imply one library owner');
  assert.equal(await page.locator('.library-context:visible').count(),0,'Global routes must hide the actual owner-context component');
  assert.equal(await page.locator('select[aria-label="切换用户知识库"]').count(),0,'Overview cards are the sole owner-selection entry');
  const text=await page.locator('#console-view .eyebrow').allTextContents();
  for(const name of Object.values(names))assert.ok(!text.some(value=>value.includes(name)),'Global heading must not contain an owner: '+name);
  const notice=page.locator('#console-notice');if(await notice.isVisible())assert.doesNotMatch(await notice.innerText(),/管理员查看模式|管理员只读查看|当前查看/,'Inspection notice belongs to content pages only');
}
async function readonly(page){for(const selector of ['#history-manage','#history-trash','#history-delete-selected','[data-history-delete]','[data-history-select]','[data-history-restore]'])assert.equal(await page.locator(selector+':visible').count(),0,'Read-only must hide '+selector);}
async function target(page,owner,inspection){const url=new URL(page.url());assert.equal(url.searchParams.get('user'),owner);assert.equal(url.searchParams.get('inspect')==='1',inspection);}
async function layoutWithoutOverflow(page,kind,width){
  const geometry=await page.evaluate(()=>{
    const context=document.querySelector('#library-context'),rect=context.getBoundingClientRect();
    return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,
      contextLeft:rect.left,contextRight:rect.right,contextWidth:context.clientWidth,contextScrollWidth:context.scrollWidth};
  });
  assert.equal(geometry.viewport,width,kind+' viewport');
  assert.ok(geometry.documentWidth<=width+2,`${kind} document overflows at ${width}: ${JSON.stringify(geometry)}`);
  assert.ok(geometry.contextLeft>=-1&&geometry.contextRight<=width+1,`${kind} context escapes viewport at ${width}: ${JSON.stringify(geometry)}`);
  assert.ok(geometry.contextScrollWidth<=geometry.contextWidth+1,`${kind} static context overflows its own box at ${width}: ${JSON.stringify(geometry)}`);
}
async function makePage(cookie){const context=await browser.newContext({viewport:{width:1440,height:1000}});contexts.push(context);await context.addCookies([{name:fixture.cookie_name,value:cookie,url:base}]);const page=await context.newPage();page.setDefaultTimeout(6000);page.on('pageerror',e=>errors.push(e.message));return page;}

(async()=>{
  try{
    browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-proxy-server','--host-resolver-rules=MAP knowledge-console.test 127.0.0.1']});
    const admin=await makePage(fixture.owner_cookie);await boot(admin);
    await check('no-hash homepage defaults to the user-library overview',async()=>{
      await admin.goto(base+'/');await ready(admin,'libraries');
      assert.ok(['','#libraries'].includes(new URL(admin.url()).hash),'Default landing must not navigate to a library content page');
      assert.equal(await admin.locator('.main-nav [data-nav="libraries"]').getAttribute('aria-current'),'page');
      assert.equal(await admin.locator('#console-view h1').innerText(),'用户知识库');
      await globalContext(admin);await actor(admin,fixture.owner_id);
    });
    // Arrange one directed read grant in the synthetic metadata DB only.
    const me=(await json(admin,ROOT+'/me')).data,grants=(await json(admin,ROOT+'/grants')).data.rows;
    const grant=grants.find(row=>row.viewer_id===fixture.member_id&&row.owner_id===fixture.owner_id);
    const granted=await json(admin,ROOT+'/grants',{viewer_id:fixture.member_id,owner_id:fixture.owner_id,history_read:true,experience_read:true,revision:grant.revision},me.csrf_token);
    assert.equal(granted.status,200,'Synthetic member grant setup');
    await check('admin overview is global and actor remains admin',async()=>{await globalContext(admin);await actor(admin,fixture.owner_id);});
    await choose(admin,fixture.owner_id,'history');
    await check('admin opens own card in normal mode with self management',async()=>{await target(admin,fixture.owner_id,false);await contentContext(admin,fixture.owner_id,/我的知识库/);assert.equal(await admin.locator('#history-manage').isVisible(),true);await actor(admin,fixture.owner_id);});
    await nav(admin,'experiences');
    await check('own history to experience keeps owner and actor',async()=>{await target(admin,fixture.owner_id,false);await contentContext(admin,fixture.owner_id,/我的知识库/);assert.match(await admin.locator('.experience-list').innerText(),/Owner verified experience/);await actor(admin,fixture.owner_id);});
    await nav(admin,'libraries');await choose(admin,fixture.member_id,'history');
    await check('admin other card is explicit inspection with static readonly context',async()=>{await target(admin,fixture.member_id,true);await contentContext(admin,fixture.member_id,/管理员只读查看/);await readonly(admin);await actor(admin,fixture.owner_id);});
    await check('admin inspect never exposes history deletion controls',async()=>{await readonly(admin);await actor(admin,fixture.owner_id);});
    await nav(admin,'experiences');
    await check('admin inspected owner continues from history to experience',async()=>{await target(admin,fixture.member_id,true);await contentContext(admin,fixture.member_id,/管理员只读查看/);assert.match(await admin.locator('.experience-list').innerText(),/Member verified experience/);assert.doesNotMatch(await admin.locator('.experience-list').innerText(),/Owner verified experience/);await actor(admin,fixture.owner_id);});
    await nav(admin,'history');
    await check('admin experience back to history keeps inspected owner readonly',async()=>{await target(admin,fixture.member_id,true);await contentContext(admin,fixture.member_id,/管理员只读查看/);await readonly(admin);await actor(admin,fixture.owner_id);});
    await admin.screenshot({path:path.join(output,'admin-inspect.png'),fullPage:true});
    await check('logo returns to library overview instead of resetting to own history',async()=>{await admin.locator('.brand').click();assert.equal(new URL(admin.url()).hash,'#libraries');await ready(admin,'libraries');await globalContext(admin);await actor(admin,fixture.owner_id);});
    await nav(admin,'libraries');
    await check('return from inspect to overview clears misleading owner context',async()=>{await globalContext(admin);await actor(admin,fixture.owner_id);});
    await choose(admin,fixture.member_id,'history');await nav(admin,'users');
    await check('account administration is global and administrator actor unchanged',async()=>{await globalContext(admin);assert.equal(await admin.locator('#add-user').isVisible(),true);await actor(admin,fixture.owner_id);});
    await nav(admin,'principles');
    await check('working principles has no library-owner context',async()=>{await globalContext(admin);await actor(admin,fixture.owner_id);});
    const member=await makePage(fixture.member_cookie);await boot(member);
    await check('granted member overview is global and admin UI remains hidden',async()=>{await globalContext(member);assert.equal(await member.locator('#admin-nav').isVisible(),false);await actor(member,fixture.member_id);});
    await choose(member,fixture.owner_id,'history');
    await check('ordinary member opens granted owner through overview in readonly normal mode',async()=>{await target(member,fixture.owner_id,false);await contentContext(member,fixture.owner_id,/已授权.*只读/);await readonly(member);await actor(member,fixture.member_id);});
    await check('ordinary cross-user history never offers management',async()=>{await readonly(member);await actor(member,fixture.member_id);});
    await nav(member,'experiences');
    await check('ordinary granted history to experience preserves owner without actor swap',async()=>{await target(member,fixture.owner_id,false);await contentContext(member,fixture.owner_id,/已授权.*只读/);assert.match(await member.locator('.experience-list').innerText(),/Owner verified experience/);await actor(member,fixture.member_id);});
    await nav(member,'history');
    await check('ordinary granted experience back to history preserves owner readonly',async()=>{await target(member,fixture.owner_id,false);await contentContext(member,fixture.owner_id,/已授权.*只读/);await readonly(member);await actor(member,fixture.member_id);});
    await member.screenshot({path:path.join(output,'member-granted.png'),fullPage:true});
    await nav(member,'libraries');
    await check('ordinary return to overview removes selected-owner context',async()=>{await globalContext(member);await actor(member,fixture.member_id);});
    await choose(member,fixture.member_id,'history');
    await check('member switches back only through own card and retains self management',async()=>{await target(member,fixture.member_id,false);await contentContext(member,fixture.member_id,/我的知识库/);assert.equal(await member.locator('#history-manage').isVisible(),true);await actor(member,fixture.member_id);});
    await check('static owner context fits mobile 390 and desktop 1440 on history and experience',async()=>{
      for(const width of [390,1440]){
        await admin.setViewportSize({width,height:1000});await nav(admin,'libraries');
        await choose(admin,fixture.member_id,'history');
        await contentContext(admin,fixture.member_id,/管理员只读查看/);await target(admin,fixture.member_id,true);
        await readonly(admin);await actor(admin,fixture.owner_id);await layoutWithoutOverflow(admin,'history',width);
        await admin.screenshot({path:path.join(output,(width===390?'mobile':'desktop')+'-history.png'),fullPage:true});
        await nav(admin,'experiences');await contentContext(admin,fixture.member_id,/管理员只读查看/);
        await target(admin,fixture.member_id,true);await actor(admin,fixture.owner_id);await layoutWithoutOverflow(admin,'experiences',width);
        await nav(admin,'libraries');await globalContext(admin);
        assert.ok(await admin.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'Library overview must also fit '+width);
        await admin.screenshot({path:path.join(output,(width===390?'mobile':'desktop')+'-libraries.png'),fullPage:true});
      }
    });
    await check('all actual browser JavaScript paths remain error-free',async()=>assert.deepEqual(errors,[]));
  }catch(e){results.push({name:'scenario execution',passed:false,error:e.stack||e.message});}
  finally{for(const context of contexts)await context.close().catch(()=>{});if(browser)await browser.close();closed=true;}
  const report={passed:results.every(row=>row.passed),browser_closed:closed,checks:results,errors};
  fs.writeFileSync(path.join(output,'navigation-results.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report,null,2));process.exitCode=report.passed?0:1;
})().catch(e=>{console.error(e);process.exitCode=1;});
