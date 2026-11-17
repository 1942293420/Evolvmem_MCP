'use strict';
// Run only on the disposable synthetic fixture. No business writes are allowed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const BASE = process.env.EVOLVMEM_TEST_URL || 'http://127.0.0.1:39486';
const OUTPUT = process.env.OUTPUT_DIR || path.join(__dirname, 'test-results', new Date().toISOString().replace(/[:.]/g, '-'));
const ONLY = new Set((process.env.NAV_CASES || '').split(',').filter(Boolean));
assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(new URL(BASE).hostname), 'isolated loopback fixture required');
fs.mkdirSync(OUTPUT, {recursive:true});
const results = [], startedAt = new Date().toISOString();
const groups = ['home', 'project', 'experience', 'organize', 'task', 'principle'];
const sections = {project:['archives','projects'], experience:['capabilities','experiences','qa'], organize:['organization','cleaning','intake','skill']};
const topPages = new Set(['home', 'experiences', 'progress', 'principles']);
const hashFor = key => topPages.has(key) ? '#'+key : '#knowledge/'+key;
let browser;
function save(){fs.writeFileSync(path.join(OUTPUT,'results.json'),JSON.stringify({base:BASE,startedAt,updatedAt:new Date().toISOString(),results},null,2));}
async function setup(width=1440, options={}){
  const context = await browser.newContext({viewport:{width,height:1000},...options});
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  const errors=[], failedReads=[], blockedWrites=[], reads=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{if(r.url().startsWith(BASE)&&r.status()>=400)failedReads.push({url:r.url().replace(BASE,''),status:r.status()});});
  await page.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());
    if(!['http:','https:'].includes(url.protocol))return route.continue();
    if(url.origin!==new URL(BASE).origin){blockedWrites.push({method:req.method(),url:url.href,reason:'external'});return route.abort('blockedbyclient');}
    if(!['GET','HEAD'].includes(req.method())){blockedWrites.push({method:req.method(),url:url.pathname});return route.abort('blockedbyclient');}
    if(url.pathname==='/api/auth/me')return route.fulfill({json:{enabled:true,authenticated:true,can_write:true,name:'合成导航验收账号',csrf_token:'fixture-only'}});
    if(url.pathname.startsWith('/api/'))reads.push(url.pathname+url.search);
    return route.continue();
  });
  await page.goto(BASE+'/#home');
  await page.locator('.nav-primary-link[data-nav-group="home"]').waitFor();
  await page.locator('.nav-account-toggle').waitFor();
  await settled(page,'home');
  return {context,page,errors,failedReads,blockedWrites,reads};
}
async function settled(page,group,section){
  await page.waitForFunction(({group,section})=>{
    const primary=document.querySelector('.nav-primary-link[aria-current="page"]');
    if(primary?.dataset.navGroup!==group)return false;
    if(section&&document.querySelector('.nav-secondary-link[aria-current="page"]')?.dataset.navSection!==section)return false;
    const expected=['project','organize'].includes(group)||group==='experience'&&section!=='experiences'?'memories':group==='task'?'progress':group==='principle'?'principles':group==='experience'?'experiences':'home';
    if(document.body.dataset.activePage!==expected)return false;
    const panel=document.querySelector(`[data-page-panel="${expected}"]`);
    if(!panel||panel.hidden)return false;
    if(expected==='memories')return !!document.querySelector('#content')&&!document.querySelector('#content').inert&&!document.querySelector('#knowledge-loading:not([hidden])');
    return !!panel.textContent.trim();
  },{group,section});
  await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
}
async function hit(page,locator){
  await locator.scrollIntoViewIfNeeded();
  const value=await locator.evaluate(el=>{const r=el.getBoundingClientRect(),top=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return {text:el.textContent.trim(),x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,viewportWidth:innerWidth,viewportHeight:innerHeight,hit:top===el||el.contains(top)};});
  assert.ok(value.hit&&value.x>=-.5&&value.right<=value.viewportWidth+.5&&value.y>=-.5&&value.bottom<=value.viewportHeight+.5,'unreachable nav item: '+JSON.stringify(value));
  return value;
}
async function click(page,locator,group,section){
  const geometry=await hit(page,locator);
  const href=await locator.getAttribute('href');
  await locator.click();
  await settled(page,group,section);
  assert.equal(new URL(page.url()).hash.split('?')[0],href.split('?')[0],'actual navigation must match destination');
  assert.equal(await page.locator('.nav-primary-link[aria-current="page"]').count(),1,'one active primary item');
  return {...geometry,href,actualHash:new URL(page.url()).hash};
}
async function layout(page){
  const data=await page.evaluate(()=>{
    const header=document.querySelector('.refresh-topnav'),hr=header.getBoundingClientRect();
    const main=[...header.querySelectorAll('.nav-primary-link')].map(el=>{const r=el.getBoundingClientRect(),s=getComputedStyle(el),i=el.querySelector('svg'),ir=i.getBoundingClientRect(),is=getComputedStyle(i),label=el.querySelector('.nav-label'),ls=getComputedStyle(label),lr=label.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(label);return {group:el.dataset.navGroup,box:r.toJSON(),fontSize:s.fontSize,label:{text:label.textContent,box:lr.toJSON(),textBounds:range.getBoundingClientRect().toJSON(),display:ls.display,visibility:ls.visibility,fontSize:ls.fontSize,color:ls.color,opacity:ls.opacity},icon:{box:ir.toJSON(),viewBox:i.getAttribute('viewBox'),fill:is.fill,stroke:is.stroke,strokeWidth:is.strokeWidth}};});
    const brand=header.querySelector('.signal-brand'),br=brand.getBoundingClientRect(),mark=brand.querySelector('.signal-brand-mark').getBoundingClientRect();
    const secondary=header.querySelector('.nav-secondary');
    return {viewport:innerWidth,documentWidth:document.documentElement.scrollWidth,header:hr.toJSON(),position:getComputedStyle(header).position,offset:parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--nav-header-height')),scrollPadding:parseFloat(getComputedStyle(document.documentElement).scrollPaddingTop),brand:{text:brand.textContent.trim(),box:br.toJSON(),mark:mark.toJSON()},secondary:secondary&&!secondary.hidden?secondary.getBoundingClientRect().toJSON():null,main};
  });
  assert.ok(data.documentWidth<=data.viewport+2,'document horizontal overflow: '+JSON.stringify(data));
  assert.equal(data.position,'sticky');
  assert.ok(data.header.height<340,'navigation leaves usable content height');
  assert.ok(Math.abs(data.offset-Math.ceil(data.header.height))<=1,'header CSS offset includes secondary/scope rows');
  assert.ok(data.scrollPadding>=data.header.height+14,'anchor scroll padding clears full sticky header');
  assert.match(data.brand.text,/EvolvMem/,'full brand retained');
  assert.ok(data.brand.mark.width>=32&&data.brand.mark.height>=32,'logo remains visible');
  for(const item of data.main){
    assert.ok(Math.abs(item.icon.box.width-24)<.2&&Math.abs(item.icon.box.height-24)<.2,'24px icon retained: '+item.group);
    assert.equal(item.icon.viewBox,'0 0 24 24');assert.equal(item.icon.fill,'none');assert.ok(Math.abs(parseFloat(item.icon.strokeWidth)-1.7)<.01);
    assert.ok(parseFloat(item.fontSize)>=15&&item.box.height>=44,'readable label and click target: '+item.group);
    assert.ok(item.label.display!=='none'&&item.label.visibility==='visible'&&parseFloat(item.label.opacity)>0&&item.label.box.width>0&&item.label.box.height>0,'actual nav label visible: '+item.group);
    assert.ok(parseFloat(item.label.fontSize)>=15,'actual label font preserved: '+item.group);
    assert.ok(item.label.textBounds.left>=item.box.left-.5&&item.label.textBounds.right<=item.box.right+.5&&item.label.textBounds.top>=item.box.top-.5&&item.label.textBounds.bottom<=item.box.bottom+.5,'full label fits link: '+item.group);
    assert.ok(item.label.box.left>=item.icon.box.right+3,'label and icon spacing: '+item.group);
  }
  for(let i=0;i<data.main.length;i++)for(let j=i+1;j<data.main.length;j++){
    const a=data.main[i].box,b=data.main[j].box;
    assert.ok(a.right<=b.left+.5||b.right<=a.left+.5||a.bottom<=b.top+.5||b.bottom<=a.top+.5,'primary items overlap');
  }
  if(data.secondary)assert.ok(data.secondary.bottom<=data.header.bottom+.5,'secondary inside sticky header');
  return data;
}
async function run(name,width,fn,options){
  if(ONLY.size&&!ONLY.has(name))return;
  let env;const evidence={};const item={name,status:'running',evidence};results.push(item);save();
  try{env=await setup(width,options);await fn(env,evidence);
    assert.deepEqual(env.errors,[],'no browser runtime errors');
    assert.deepEqual(env.blockedWrites,[],'navigation must not trigger writes/external requests');
    assert.deepEqual(env.failedReads,[],'all actual fixture reads succeed');
    item.status='pass';
  }catch(error){item.status='fail';item.error=error.stack||String(error);}
  finally{if(env){evidence.reads=[...new Set(env.reads)];evidence.errors=env.errors;evidence.failedReads=env.failedReads;evidence.blockedWrites=env.blockedWrites;
    await env.page.screenshot({path:path.join(OUTPUT,name+'.png'),fullPage:false}).catch(()=>{});await env.context.close();}
    save();console.log(JSON.stringify({name,status:item.status,error:item.error||null}));}
}
(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,channel:process.env.BROWSER_CHANNEL||undefined});
  for(const width of [320,360,390,768,1024,1178,1440,1920])await run('navigation-'+width,width,async({page},e)=>{
    assert.deepEqual(await page.locator('.nav-primary-link').evaluateAll(els=>els.map(el=>el.dataset.navGroup)),groups);
    assert.equal(await page.locator('.nav-preview-link,[data-more-toggle]').count(),0,'old duplicate navigation absent');
    e.initial=await layout(page);e.clicked=[];e.layouts=[];
    for(const group of groups){const section=sections[group]?.[0];e.clicked.push(await click(page,page.locator(`[data-nav-group="${group}"]`),group,section));}
    for(const [group,keys] of Object.entries(sections)){
      await click(page,page.locator(`[data-nav-group="${group}"]`),group,keys[0]);
      assert.deepEqual(await page.locator('.nav-secondary-link').evaluateAll(els=>els.map(el=>el.dataset.navSection)),keys);
      for(const key of keys){e.clicked.push(await click(page,page.locator(`[data-nav-section="${key}"]`),group,key));e.layouts.push({key,...await layout(page)});}
      await page.screenshot({path:path.join(OUTPUT,`navigation-${width}-${group}.png`),fullPage:false});
    }
    await click(page,page.locator('[data-nav-section="intake"]'),'organize','intake');
    for(const key of ['library','intake']){
      const link=page.locator('.nav-scope-link').filter({hasText:key==='library'?'全部资料':'待确认'});
      e.clicked.push(await click(page,link,'organize','intake'));assert.equal(await link.getAttribute('aria-current'),'page');e.layouts.push({key,...await layout(page)});
    }
    await page.screenshot({path:path.join(OUTPUT,`navigation-${width}-material-scope.png`),fullPage:false});
    await page.goto(BASE+'/#knowledge/capabilities');await settled(page,'experience','capabilities');
    await page.locator('[data-memory-card]').first().waitFor();await page.evaluate(()=>scrollTo(0,600));
    await page.waitForFunction(()=>scrollY>0);e.sticky=await layout(page);assert.ok(Math.abs(e.sticky.header.top)<1,'header remains frozen while actual content scrolls');
    await hit(page,page.locator('[data-nav-section="capabilities"]'));
  });
  await run('legacy-routes',1440,async({page},e)=>{
    e.routes=[];
    for(const [hash,group,section] of [
      ['#knowledge/library?project=bluewhale&lane=history','project','projects'],['#library?project=bluewhale','project','projects'],
      ['#knowledge/library','organize','intake'],['#library?project=__global__','organize','intake'],
      ['#knowledge/unassigned','project','projects'],['#projects','project','projects'],['#memories','project','projects'],
      ['#knowledge/rules','organize','skill'],['#rules','organize','skill'],['#knowledge/learning','organize','skill'],['#learning','organize','skill'],
      ['#intake?mode=records','organize','intake'],['#knowledge/qa','experience','qa'],['#experiences','experience','experiences']]){
      await page.goto(BASE+'/'+hash);await settled(page,group,section);e.routes.push({requested:hash,actual:new URL(page.url()).hash,group,section});
      assert.equal(await page.locator('.nav-primary-link[aria-current="page"]').count(),1);
      assert.equal(await page.locator('.nav-secondary-link[aria-current="page"]').count(),1);
    }
  });
  await run('library-project-filter-syncs-shell',1440,async({page},e)=>{
    await page.goto(BASE+'/#knowledge/library');await settled(page,'organize','intake');
    const filter=page.locator('#filter-project');await filter.waitFor();
    e.before={hash:new URL(page.url()).hash,primary:await page.locator('.nav-primary-link[aria-current="page"]').getAttribute('data-nav-group'),secondary:await page.locator('.nav-secondary-link[aria-current="page"]').getAttribute('data-nav-section')};
    await Promise.all([page.waitForResponse(response=>response.url().includes('/api/knowledge/items?project=bluewhale')),filter.selectOption('bluewhale')]);
    await page.waitForFunction(()=>!document.querySelector('#content').inert);
    e.after={hash:new URL(page.url()).hash,primary:await page.locator('.nav-primary-link[aria-current="page"]').getAttribute('data-nav-group'),secondary:await page.locator('.nav-secondary-link[aria-current="page"]').getAttribute('data-nav-section')};
    assert.equal(e.after.primary,'project','selecting an owned project from all materials must immediately select the project primary group');
    assert.equal(e.after.secondary,'projects','project scoped materials must select the legacy project section');
    assert.equal(new URLSearchParams(e.after.hash.split('?')[1]).get('project'),'bluewhale','selected project persists in the route');
    await settled(page,'project','projects');
  });
  await run('delayed-library-read-does-not-replace-home',1440,async({page},e)=>{
    await page.goto(BASE+'/#knowledge/library');await settled(page,'organize','intake');
    let release,markHeld;const gate=new Promise(resolve=>release=resolve),held=new Promise(resolve=>markHeld=resolve);
    await page.route('**/api/knowledge/items?*',async route=>{
      if(new URL(route.request().url()).searchParams.get('project')!=='bluewhale')return route.fallback();
      assert.equal(route.request().method(),'GET');const response=await route.fetch();e.heldReadStatus=response.status();markHeld();await gate;return route.fulfill({response});
    });
    try{
      await page.locator('#filter-project').selectOption('bluewhale');await held;
      e.pendingHash=new URL(page.url()).hash;
      await click(page,page.locator('[data-nav-group="home"]'),'home');e.beforeRelease=new URL(page.url()).hash;
      const returned=page.waitForResponse(response=>response.url().includes('/api/knowledge/items?project=bluewhale'));release();await returned;
      await page.waitForFunction(()=>!document.querySelector('#content')?.inert);
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      e.after={hash:new URL(page.url()).hash,primary:await page.locator('.nav-primary-link[aria-current="page"]').getAttribute('data-nav-group'),page:await page.locator('body').getAttribute('data-active-page')};
      assert.equal(e.heldReadStatus,200);assert.equal(e.after.hash,'#home','stale library read must not replace the current home URL');assert.equal(e.after.primary,'home');assert.equal(e.after.page,'home');
    }finally{release();}
  });
  await run('account-escape-and-keyboard',1440,async({page},e)=>{
    const account=page.locator('.nav-account-toggle');await account.click();
    assert.equal(await account.getAttribute('aria-expanded'),'true');assert.ok(await page.locator('.nav-account-panel').isVisible());
    e.panel=await page.locator('.nav-account-panel').boundingBox();assert.ok(e.panel.x>=0&&e.panel.x+e.panel.width<=1440);
    await page.keyboard.press('Escape');assert.equal(await account.getAttribute('aria-expanded'),'false');assert.ok(await account.evaluate(el=>document.activeElement===el));
    e.focus=[];await page.keyboard.press('Tab');
    for(const group of groups){const link=page.locator(`[data-nav-group="${group}"]`);await link.focus();e.focus.push(await hit(page,link));assert.ok(await link.evaluate(el=>el.matches(':focus-visible')));}
    await page.locator('.refresh-skip-link').focus();const oldHash=new URL(page.url()).hash;await page.keyboard.press('Enter');assert.equal(new URL(page.url()).hash,oldHash);assert.ok(await page.evaluate(()=>document.activeElement.matches('main')));
  });
  await run('project-recent-hash-restores-filter',1440,async({page},e)=>{
    await page.goto(BASE+'/#knowledge/archives');await settled(page,'project','archives');await page.locator('[data-project-card]').first().waitFor();
    await page.locator('#project-archive-form [name=project]').fill('鲸鱼别名');await page.locator('#project-archive-form button[type=submit]').click();
    await page.waitForFunction(()=>document.querySelectorAll('[data-project-card]').length===1);await page.locator('[data-project-card="bluewhale"]').click();
    await page.locator('[data-project-event]').first().waitFor();e.remembered=new URL(page.url()).hash;assert.equal(new URLSearchParams(e.remembered.split('?')[1]).get('project'),'bluewhale');
    await click(page,page.locator('[data-nav-group="home"]'),'home');
    assert.equal(await page.locator('[data-nav-group="project"]').getAttribute('href'),e.remembered);
    await click(page,page.locator('[data-nav-group="project"]'),'project','archives');await page.locator('[data-project-event]').first().waitFor();assert.equal(new URL(page.url()).hash,e.remembered);
    await page.locator('[data-project-back]').click();await page.locator('[data-project-card]').first().waitFor();e.filter=await page.locator('#project-archive-form [name=project]').inputValue();assert.equal(e.filter,'鲸鱼别名');
    // Hashchange-driven navigation must remember the OLD controller hash, not the new location.
    await page.goto(BASE+'/#knowledge/library?project=bluewhale&lane=history');await settled(page,'project','projects');e.legacyHash=new URL(page.url()).hash;
    await page.evaluate(()=>{location.hash='#home';});await settled(page,'home');await click(page,page.locator('[data-nav-group="project"]'),'project','projects');assert.equal(new URL(page.url()).hash,e.legacyHash);
  });
  await run('rules-cancel-preserves-draft-and-active',1440,async({page},e)=>{
    await page.goto(BASE+'/#knowledge/rules');await settled(page,'organize','skill');const field=page.locator('#min-confidence');await field.waitFor();
    const old=await field.inputValue(),draft=old==='0.55'?'0.65':'0.55';await field.fill(draft);await page.waitForFunction(()=>document.querySelector('#rules-save-state')?.textContent.includes('未保存'));
    e.hash=new URL(page.url()).hash;e.draft=draft;e.dialogs=[];
    for(const selector of ['[data-nav-group="home"]','[data-nav-section="cleaning"]']){
      const dialogPromise=page.waitForEvent('dialog');const clickPromise=page.locator(selector).click();const dialog=await dialogPromise;e.dialogs.push(dialog.message());assert.equal(dialog.type(),'confirm');await dialog.dismiss();await clickPromise;
      await settled(page,'organize','skill');assert.equal(new URL(page.url()).hash,e.hash);assert.equal(await field.inputValue(),draft);assert.ok(await page.locator('#rules-save-state').textContent().then(t=>t.includes('未保存')));
    }
    const dialogPromise=page.waitForEvent('dialog');await page.evaluate(()=>{location.hash='#home';});const dialog=await dialogPromise;e.dialogs.push(dialog.message());await dialog.dismiss();await settled(page,'organize','skill');
    assert.equal(new URL(page.url()).hash,e.hash);assert.equal(await field.inputValue(),draft);assert.equal(await page.locator('[data-nav-group="organize"]').getAttribute('aria-current'),'page');
    assert.equal(await page.locator('[data-nav-section="skill"]').getAttribute('aria-current'),'page');
  });
  for(const [kind,group,section,slot,endpoint] of [['experiences','experience','experiences','experience-browser','experiences'],['progress','task',undefined,'progress-browser','workstreams']])await run(kind+'-query-restored',1440,async({page},e)=>{
    await page.goto(BASE+'/#'+kind);await settled(page,group,section);
    const query=page.locator(`[data-slot="${slot}"] [data-filter="query"]`),term='合成筛选保留-'+kind;
    await query.waitFor();await Promise.all([page.waitForResponse(response=>new URL(response.url()).pathname==='/api/'+endpoint&&new URL(response.url()).searchParams.get('q')===term),query.fill(term)]);
    await page.locator(`[data-slot="${slot}"] [data-results][aria-busy="false"]`).waitFor();
    e.before={hash:new URL(page.url()).hash,query:await query.inputValue()};
    await click(page,page.locator('[data-nav-group="home"]'),'home');await click(page,page.locator(`[data-nav-group="${group}"]`),group,section);
    e.after={hash:new URL(page.url()).hash,query:await query.inputValue()};assert.equal(e.after.query,term,kind+' search must survive leaving and returning through primary navigation');
  });
  await run('reduced-motion',1440,async({page},e)=>{
    await page.goto(BASE+'/#knowledge/intake');await settled(page,'organize','intake');
    e.motion=await page.locator('.nav-primary-link,.nav-line-icon,.nav-secondary-link,.nav-scope-link').evaluateAll(els=>els.map(el=>({className:el.className,duration:getComputedStyle(el).transitionDuration,animation:getComputedStyle(el).animationName})));
    assert.ok(e.motion.every(v=>v.duration.split(',').every(n=>parseFloat(n)===0)&&v.animation==='none'));
  },{reducedMotion:'reduce'});
  await browser.close();browser=null;save();console.log(JSON.stringify({output:OUTPUT,passed:results.filter(r=>r.status==='pass').length,failed:results.filter(r=>r.status==='fail').length}));
  if(results.some(r=>r.status==='fail'))process.exitCode=1;
})().catch(async error=>{results.push({name:'runner',status:'fail',error:error.stack});save();console.error(error);if(browser)await browser.close();process.exitCode=1;});
