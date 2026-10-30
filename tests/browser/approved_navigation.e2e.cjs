// Run against tests/browser/capability_archives_fixture.py in a fresh TMPDIR.
// Only static assets from this checkout are overlaid; API reads reach the fixture.
// Non-GET/HEAD and non-loopback requests are rejected. No production data.
'use strict';
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const BASE=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39483';
assert.ok(['127.0.0.1','localhost','[::1]'].includes(new URL(BASE).hostname),'use a disposable loopback fixture');
const assets=process.env.EVOLVMEM_STATIC_ROOT||path.resolve(__dirname,'../../evolvmem/web_static');
const patched=process.env.EVOLVMEM_STATIC_PATCH||assets;
const OUTPUT=process.env.EVOLVMEM_TEST_OUTPUT||path.join(__dirname,'approved-navigation-results');
fs.mkdirSync(OUTPUT,{recursive:true});
const focused=false;
const results={kind:'public-navigation-desktop-synthetic-fixture',errors:[],reads:[],blocked:[],widths:[],routes:[]};
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,headless:true});
try{const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(15000);page.on('pageerror',e=>results.errors.push(e.message));
await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url());
if(!['http:','https:'].includes(u.protocol))return route.continue();
if(u.origin!==BASE||!['GET','HEAD'].includes(req.method())){results.blocked.push({url:u.href,method:req.method()});return route.abort();}
if(u.pathname==='/api/auth/me')return route.fulfill({json:{enabled:true,authenticated:true,can_write:true,name:'Public navigation fixture',csrf_token:'fixture-only'}});
if(u.pathname.startsWith('/api/')){results.reads.push(u.pathname+u.search);return route.continue();}
const rel=u.pathname==='/'?'designs/signal.html':u.pathname.slice(1);if(rel.includes('..'))throw Error('unsafe path');
const file=[path.join(patched,rel),path.join(assets,rel)].find(f=>fs.existsSync(f)&&fs.statSync(f).isFile());
if(file)return route.fulfill({path:file,contentType:({'.html':'text/html; charset=utf-8','.js':'application/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream'});
return route.continue();});
await page.goto(BASE+'/#home');await page.locator('.nav-account-toggle').waitFor();
for(const width of [1440]){
await page.setViewportSize({width,height:1000});await page.evaluate(()=>location.hash='#home');await page.waitForTimeout(120);
const shape=await page.locator('.refresh-topnav').evaluate(el=>({width:innerWidth,doc:document.documentElement.scrollWidth,header:el.getBoundingClientRect().toJSON(),items:[...el.querySelectorAll('.nav-primary-link')].map(a=>({box:a.getBoundingClientRect().toJSON(),label:getComputedStyle(a.querySelector('.nav-label')).display,icon:a.querySelector('svg').getBoundingClientRect().width}))}));
assert.ok(shape.doc<=width+2,JSON.stringify(shape));assert.equal(shape.items.length,6);assert.ok(shape.items.every(x=>x.label!=='none'&&x.icon===24&&x.box.left>=0&&x.box.right<=width+1));results.widths.push(shape);
for(const group of ['project','experience','organize','task','principle','home']){await page.locator(`[data-nav-group="${group}"]`).click();await page.waitForFunction(g=>document.querySelector('.nav-primary-link[aria-current="page"]')?.dataset.navGroup===g,group);await page.waitForTimeout(90);}
if(width===1440){await page.locator('[data-nav-group="experience"]').click();await page.waitForTimeout(180);await page.screenshot({path:path.join(OUTPUT,`public-export-${width}.png`)});}
}
await page.setViewportSize({width:1440,height:1000});
for(const [hash,group,section] of focused?[]:[['#knowledge/archives','project','archives'],['#knowledge/projects','project','projects'],['#knowledge/capabilities','experience','capabilities'],['#experiences','experience','experiences'],['#knowledge/qa','experience','qa'],['#knowledge/organization','organize','organization'],['#knowledge/cleaning','organize','cleaning'],['#knowledge/intake','organize','intake'],['#knowledge/skill','organize','skill'],['#knowledge/library','organize','intake'],['#knowledge/library?project=bluewhale','project','projects'],['#knowledge/unassigned','project','projects'],['#knowledge/rules','organize','skill'],['#knowledge/learning','organize','skill']]){
await page.evaluate(h=>location.hash=h,hash);await page.waitForFunction(({group,section})=>document.querySelector('.nav-primary-link[aria-current="page"]')?.dataset.navGroup===group&&document.querySelector('.nav-secondary-link[aria-current="page"]')?.dataset.navSection===section,{group,section});await page.waitForTimeout(220);results.routes.push({hash,group,section});}
await page.goto(BASE+'/#knowledge/intake?mode=records');await page.locator('.nav-account-toggle').waitFor();await page.waitForTimeout(400);assert.ok(page.url().includes('mode=records'));
await page.locator('[data-nav-group="home"]').click();await page.locator('[data-nav-group="organize"]').click();await page.waitForTimeout(250);assert.ok(page.url().includes('mode=records'));results.intakeModeRestored=true;
await page.locator('.nav-account-toggle').click();await page.locator('.nav-account-panel').waitFor({state:'visible'});await page.keyboard.press('Escape');assert.equal(await page.locator('.nav-account-panel').isVisible(),false);results.accountEscape=true;
await page.goto(BASE+'/#knowledge/library');await page.locator('#filter-project').waitFor();
await Promise.all([page.waitForResponse(r=>r.url().includes('/api/knowledge/items?project=bluewhale')),page.locator('#filter-project').selectOption('bluewhale')]);
await page.waitForFunction(()=>document.querySelector('.nav-primary-link[aria-current="page"]')?.dataset.navGroup==='project');
assert.equal(await page.locator('.nav-secondary-link[aria-current="page"]').getAttribute('data-nav-section'),'projects');assert.equal(new URLSearchParams(new URL(page.url()).hash.split('?')[1]).get('project'),'bluewhale');results.internalScopeSync=true;
results.searchRestored=[];
for(const [key,group,endpoint,term] of [['experiences','experience','experiences','navigation experience filter'],['progress','task','workstreams','navigation task filter']]){
  await page.goto(BASE+'/#'+key);
  const search=page.locator(`[data-page-panel="${key}"] [data-filter="query"]`);await search.waitFor();
  await Promise.all([page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/'+endpoint&&u.searchParams.get('q')===term;}),search.fill(term)]);
  await page.waitForFunction(k=>document.querySelector(`[data-page-panel="${k}"] [data-results]`)?.getAttribute('aria-busy')==='false',key);
  await page.locator('[data-nav-group="home"]').click();
  await Promise.all([page.waitForResponse(r=>{const u=new URL(r.url());return u.pathname==='/api/'+endpoint&&u.searchParams.get('q')===term;}),page.locator(`[data-nav-group="${group}"]`).click()]);
  assert.equal(await search.inputValue(),term,'returning to '+key+' must preserve the existing controller search');
  assert.equal(new URL(page.url()).hash,'#'+key);results.searchRestored.push({page:key,query:term});
}
await page.goto(BASE+'/#knowledge/rules');const field=page.locator('#min-confidence');await field.waitFor();const old=await field.inputValue(),draft=old==='0.55'?'0.65':'0.55';await field.fill(draft);await page.waitForFunction(()=>document.querySelector('#rules-save-state')?.textContent.includes('未保存'));
const hash=new URL(page.url()).hash;
for(const selector of ['[data-nav-group="home"]','[data-nav-section="cleaning"]']){const dialogPromise=page.waitForEvent('dialog'),clicked=page.locator(selector).click();const dialog=await dialogPromise;assert.equal(dialog.type(),'confirm');await dialog.dismiss();await clicked;assert.equal(new URL(page.url()).hash,hash);assert.equal(await field.inputValue(),draft);assert.equal(await page.locator('.nav-primary-link[aria-current="page"]').getAttribute('data-nav-group'),'organize');assert.equal(await page.locator('.nav-secondary-link[aria-current="page"]').getAttribute('data-nav-section'),'skill');}
results.draftCancelPreserved=true;await page.screenshot({path:path.join(OUTPUT,'public-export-desktop-final.png')});
assert.deepEqual(results.errors,[]);assert.deepEqual(results.blocked,[]);results.passed=true;
}catch(e){results.error=e.stack;throw e;}finally{await browser.close();fs.writeFileSync(path.join(OUTPUT,'results.json'),JSON.stringify(results,null,2));}
})().catch(e=>{console.error(e);process.exit(1)});
