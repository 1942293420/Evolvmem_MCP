// Synthetic browser-only write/read fixture; never contacts PG or a model service.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assets=path.resolve(__dirname,'../../evolvmem/web_static/v2'),out='/tmp/experience-editor-browser';
let owner='alice',scope='scope-editor-alice',mode='normal',refreshFailed=false;
let note={id:'manual-edit',revision:7,origin:'manual',status:'active',category:'业务经验',kind:'procedure',
 problem:'核心问题第一行\n核心问题第二行',solution:'完整解决办法'.repeat(65),reusable_experience:'可复用规则'.repeat(30),scope:{project:'Evolvmem'},evidence:[]};
const writes=[],receipts=new Map(),errors=[],checks=[];
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox','--no-proxy-server']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 page.on('pageerror',e=>errors.push(String(e)));
 await page.route('**/*',async route=>{
  const r=route.request(),u=new URL(r.url()),p=u.pathname;
  if(p==='/api/auth/me'||p==='/auth/ops/status')return route.fulfill({json:{central_sso:true,authenticated:true,user_scope:scope,native_status:'ready',name:'Synthetic editor',csrf_token:'synthetic-csrf'}});
  if(p==='/api/console-v2/me')return route.fulfill({json:{ok:true,current_user:{id:owner},csrf_token:'synthetic-csrf',libraries:[{id:owner,can_manage_experience:true},{id:'peer',can_read_experience:true}]}});
  if(p==='/api/console-v2/experiences/write'){
   const body=r.postDataJSON();writes.push(body);assert.equal(r.headers()['x-csrf-token'],'synthetic-csrf');
   if(mode==='cas'){note={...note,revision:note.revision+1};return route.fulfill({status:409,json:{ok:false,error:'experience_revision_conflict'}});}
   if(mode==='encoder')return route.fulfill({status:503,json:{ok:false,error:'experience_embedding_unavailable'}});
   if(!receipts.has(body.request_id)){
    note={...note,...body,id:body.action==='create'?'manual-created':note.id,revision:note.revision+1};
    receipts.set(body.request_id,{...note});
   }
   if(mode==='network'){mode='normal';return route.abort('failed');}
   if(mode==='refresh-fail')refreshFailed=true;
   await new Promise(r=>setTimeout(r,70));
   return route.fulfill({status:body.action==='create'?201:200,json:{ok:true,note:receipts.get(body.request_id)}});
  }
  if(p==='/api/console-v2/experiences'||p==='/api/console-v2/overview'){
   if(refreshFailed)return route.fulfill({status:503,json:{ok:false,error:'synthetic_read_unavailable'}});
   if(p.endsWith('/overview'))return route.fulfill({json:{ok:true,metrics:{}}});
   const items=mode==='filter-out'?[]:[note,{...note,id:'legacy',origin:'legacy'},{...note,id:'retired',status:'retired'}].filter(n=>(u.searchParams.get('status') || 'active')==='all'||n.status===(u.searchParams.get('status') || 'active'));
   return route.fulfill({json:{ok:true,items,total:items.length,category_counts:{'业务经验':items.length}}});
  }
  const rel=p==='/v2/'?'index.html':p.replace(/^\/v2\//,'');
  const file=path.resolve(assets,rel);
  if(file.startsWith(assets+path.sep)&&fs.existsSync(file))return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
  return route.fulfill({status:404,body:'synthetic missing'});
 });
 const status=()=>page.locator('#new-exp-status');
 const close=()=>page.locator('#modal-body [data-act="close-modal"]').click();
 const edit=()=>page.locator('[data-act="edit-manual-exp"]').click();
 const submit=async()=>{await page.locator('#new-exp-submit').click();await page.waitForFunction(()=>!document.querySelector('#new-exp-submit')?.getAttribute('aria-busy') || document.querySelector('#new-exp-submit').getAttribute('aria-busy')==='false');};
 try{
  fs.mkdirSync(out,{recursive:true});
  await page.goto('http://editor-fixture.test/v2/#experiences');
  await page.waitForFunction(()=>EvolvMemV2.session.dataReady&&EvolvMemV2.state.experiences.length);
  await page.evaluate(()=>{EvolvMemV2.state.expFilters.q='保留搜索';EvolvMemV2.state.expOffset=60;EvolvMemV2.renderAll();});
  await edit();
  assert.equal(await page.locator('#new-exp-prob').inputValue(),note.problem);
  assert.equal(await page.locator('#new-exp-sol').inputValue(),note.solution);
  await page.screenshot({path:path.join(out,'editor-desktop.png'),fullPage:true});
  await page.locator('#new-exp-prob').fill('修改后的手工问题');
  const before=writes.length;
  await page.evaluate(()=>{EvolvMemV2.submitExperienceEditor();EvolvMemV2.submitExperienceEditor();});
  await page.waitForFunction(()=>document.querySelector('#new-exp-status').textContent.includes('保存成功') && document.querySelector('#new-exp-submit').textContent==='已保存');
  assert.equal(writes.length,before+1);assert.equal(writes.at(-1).expected_revision,7);assert.equal(writes.at(-1).action,'update');
  assert.equal(await page.evaluate(()=>EvolvMemV2.state.expOffset),60);assert.equal(await page.locator('#exp-f-q').inputValue(),'保留搜索');
  await close();assert.equal(await page.locator('.prose-lead').textContent(),'修改后的手工问题');checks.push('full multiline prefill; CAS update; duplicate submit blocked; search/page/detail retained');

  await edit();await page.locator('#new-exp-prob').fill('CAS冲突草稿');mode='cas';await submit();
  assert.match(await status().innerText(),/输入已保留/);assert.equal(await page.locator('#new-exp-prob').inputValue(),'CAS冲突草稿');
  assert.equal(await page.locator('#new-exp-submit').isDisabled(),true);
  await page.setViewportSize({width:390,height:900});await page.locator('#new-exp-submit').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(out,'conflict-mobile.png'),fullPage:true});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),true);
  mode='normal';await page.locator('#new-exp-reload').click();
  await close();await page.setViewportSize({width:1440,height:1000});await edit();
  assert.equal(await page.locator('#new-exp-prob').inputValue(),note.problem);checks.push('CAS retains draft; explicit reread; no automatic overwrite');

  await page.locator('#new-exp-prob').fill('编码失败后保留的正文');mode='encoder';await submit();
  assert.match(await status().innerText(),/向量编码未完成/);assert.equal(await page.evaluate(()=>EvolvMemV2.session.status),'ready');
  assert.equal(await page.locator('#new-exp-prob').inputValue(),'编码失败后保留的正文');
  const rejectedKey=writes.at(-1).request_id;mode='normal';await submit();
  assert.equal(writes.at(-1).request_id,rejectedKey);assert.match(await status().innerText(),/保存成功/);await close();
  checks.push('encoder503 keeps ready session and draft; same-payload retry key retained');

  await edit();await page.locator('#new-exp-prob').fill('回执已确认但列表刷新失败');mode='refresh-fail';await submit();
  assert.match(await status().innerText(),/保存成功.*刷新失败/);
  const writeCount=writes.length;refreshFailed=false;mode='normal';await page.locator('#new-exp-reload').click();
  await page.waitForFunction(()=>document.querySelector('#new-exp-status').textContent.includes('列表和详情已刷新'));
  assert.equal(writes.length,writeCount);await close();checks.push('write receipt survives read503; refresh-only retry never repeats POST');

  await edit();await page.locator('#new-exp-prob').fill('未知回执同键核对');mode='network';await submit();
  assert.match(await status().innerText(),/保存结果暂未确认/);const unknown=writes.at(-1);
  await page.evaluate(async()=>{const E=EvolvMemV2;E.session.block('unavailable');await E.session.check({resume:false});await E.api.bootstrap({statusKnown:true});});
  assert.equal(await page.locator('#new-exp-submit').isDisabled(),false);
  assert.equal(await page.locator('#new-exp-submit').textContent(),'核对并重试');
  await submit();assert.deepEqual(writes.at(-1),unknown);assert.match(await status().innerText(),/保存成功/);
  await close();checks.push('network result unknown; same-identity rehydrate resets busy state and repeats exact request');

  await edit();await page.locator('#new-exp-prob').fill('保存后不再匹配原搜索');mode='filter-out';await submit();
  assert.match(await status().innerText(),/保存成功.*筛选/);assert.equal(await page.evaluate(()=>EvolvMemV2.state.experiences.length),0);await close();
  mode='normal';await page.locator('[data-act="open-new-exp"]').click();const emptyCount=writes.length;await submit();
  assert.equal(writes.length,emptyCount);assert.match(await page.locator('#new-exp-prob-error').innerText(),/请填写/);
  await page.locator('#new-exp-prob').fill('新建一个问题');await page.locator('#new-exp-sol').fill('具体解决办法');await page.locator('#new-exp-reus').fill('一条可复用规则');await submit();
  assert.equal(writes.at(-1).action,'create');assert.match(await status().innerText(),/保存成功/);await close();
  checks.push('nonmatching saved result stays outside query; create validates empty fields without placeholders');

  await page.locator('[data-act="open-new-exp"]').click();
  await page.locator('#new-exp-prob').fill('关闭后仍只创建一次');await page.locator('#new-exp-sol').fill('保存回执未知时复用原请求');await page.locator('#new-exp-reus').fill('同键确认');
  mode='network';await submit();const pendingCreate=writes.at(-1);assert.match(await status().innerText(),/保存结果暂未确认/);
  await close();
  await page.evaluate(async()=>{const E=EvolvMemV2;E.session.block('unavailable');await E.session.check({resume:false});await E.api.bootstrap({statusKnown:true});});
  assert.equal(await page.locator('#new-exp-submit').textContent(),'核对并重试');await close();
  await page.locator('[data-act="open-new-exp"]').click();assert.equal(await page.locator('#new-exp-prob').inputValue(),'关闭后仍只创建一次');
  await submit();assert.deepEqual(writes.at(-1),pendingCreate);await close();
  checks.push('unconfirmed create survives close, same-identity reconnect and reopen with the identical request');

  await page.locator('[data-act="select-exp"][data-id="legacy"]').click();assert.equal(await page.locator('[data-act="edit-manual-exp"]').count(),0);
  await page.evaluate(async()=>{EvolvMemV2.state.expFilters.status='all';await EvolvMemV2.api.loadExperiences();EvolvMemV2.state.selectedExpId='retired';EvolvMemV2.renderAll();});
  assert.equal(await page.locator('[data-act="edit-manual-exp"]').count(),0);
  await page.evaluate(async()=>{EvolvMemV2.state.selectedExpId='manual-created';await EvolvMemV2.api.switchLibrary('peer');});
  assert.equal(await page.locator('[data-act="edit-manual-exp"]').count(),0);
  owner='bob';scope='scope-editor-bob';await page.evaluate(async()=>{const E=EvolvMemV2;await E.session.check({resume:false});await E.api.bootstrap({statusKnown:true});});
  assert.equal(await page.locator('#new-exp-prob').count(),0);
  assert.deepEqual(errors,[]);checks.push('legacy/retired/peer edit hidden; different identity has no old draft; no page errors');
  console.log(JSON.stringify({passed:true,browser:await browser.version(),writes:writes.length,checks,artifacts:out}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
