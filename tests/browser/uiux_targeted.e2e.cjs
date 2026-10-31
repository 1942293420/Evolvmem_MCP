'use strict';
// Disposable fixture only. API interception adds synthetic option labels and auth chrome.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const BASE=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39484';
const OUTPUT_DIR=process.env.OUTPUT_DIR||path.join(process.cwd(),'test-results','uiux-targeted');
fs.mkdirSync(OUTPUT_DIR,{recursive:true});
const OUT=process.env.UIUX_RESULTS||path.join(OUTPUT_DIR,'results.json');
assert.ok(['127.0.0.1','localhost','[::1]'].includes(new URL(BASE).hostname),'run only against an isolated loopback fixture');
const LONG='跨境电商采购物流成本核算与数据库服务可靠性优化项目完整中文名称';
const sources=[LONG,'不可用合成项目',...Array.from({length:16},(_,i)=>`合成来源项目${String(i).padStart(2,'0')}`)];
const ONLY=new Set((process.env.UIUX_CASES||'').split(',').filter(Boolean));
const results=[];let browser;
function rgba(s){const a=(s.match(/[\d.]+/g)||[]).map(Number);return [...a.slice(0,3),a.length>3?a[3]:1];}
function luminance(rgb){return rgb.slice(0,3).map(n=>n/255).map(n=>n<=.04045?n/12.92:((n+.055)/1.055)**2.4).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);}
function contrast(fg,bg){const c=fg.slice(0,3).map((v,i)=>v*fg[3]+bg[i]*(1-fg[3])),a=luminance(c),b=luminance(bg);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);}
async function setup(options={}){
 const context=await browser.newContext({viewport:{width:1280,height:800},...options});
 const page=await context.newPage();page.setDefaultTimeout(6000);
 const requests={capabilities:0};const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/auth/me',r=>r.fulfill({json:{enabled:true,authenticated:true,can_write:true,name:'合成测试账号',csrf_token:'fixture'}}));
 await page.route('**/api/capabilities?*',async r=>{requests.capabilities++;const response=await r.fetch();const data=await response.json();data.source_projects=sources;await r.fulfill({response,json:data});});
 await page.goto(BASE+'/#knowledge/capabilities');
 await page.locator('[data-memory-card]').first().waitFor();
 await page.locator('.nav-account-toggle').waitFor();
 const trigger=page.locator('.capability-select').filter({has:page.locator('[data-memory-source]')}).locator('[role=combobox]');
 return {context,page,trigger,errors,requests};
}
async function run(name,options,fn){if(ONLY.size&&!ONLY.has(name))return;let env;const evidence={};try{env=await setup(options);await fn(env,evidence);assert.deepEqual(env.errors,[]);results.push({name,status:'pass',evidence});await env.page.screenshot({path:path.join(OUTPUT_DIR,name+'.png'),fullPage:false});}catch(e){results.push({name,status:'fail',error:e.message,evidence});if(env)await env.page.screenshot({path:path.join(OUTPUT_DIR,name+'.png'),fullPage:false}).catch(()=>{});}finally{if(env){await env.page.waitForLoadState('networkidle').catch(()=>{});await env.context.close();}}console.log(JSON.stringify(results.at(-1)));}
(async()=>{browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined});
 await run('skip-navigation-first-tab',{},async({page},e)=>{
  const hash=new URL(page.url()).hash;await page.keyboard.press('Tab');
  e.first=await page.locator(':focus').evaluate(el=>({tag:el.tagName,text:el.textContent,label:el.getAttribute('aria-label'),rect:el.getBoundingClientRect().toJSON()}));
  assert.match((e.first.label||'')+' '+e.first.text,/跳过|跳至|Skip/i,'first Tab must expose skip navigation');
  assert.ok(e.first.rect.top>=0&&e.first.rect.bottom<=800,'focused skip control visible in viewport');
  await page.keyboard.press('Enter');
  assert.equal(new URL(page.url()).hash,hash,'skip activation must preserve SPA route');
  e.focus=await page.evaluate(()=>({inMain:document.activeElement.matches('main')||!!document.activeElement.closest('main'),rect:document.activeElement.getBoundingClientRect().toJSON(),headerBottom:document.querySelector('header.refresh-topnav').getBoundingClientRect().bottom}));
  assert.ok(e.focus.inMain,'skip moves actual keyboard focus into main');
  assert.ok(e.focus.rect.top>=e.focus.headerBottom-1,'skip target not covered by header');
 });
 await run('menu-keeps-header-clear',{viewport:{width:1280,height:360}},async({page,trigger},e)=>{
  await trigger.evaluate(el=>{const hb=document.querySelector('.refresh-topnav').getBoundingClientRect().bottom;window.scrollBy(0,el.getBoundingClientRect().top-(hb+60));});
  await trigger.click();await page.getByRole('listbox').waitFor();
  e.geometry=await page.evaluate(()=>({header:document.querySelector('.refresh-topnav').getBoundingClientRect().toJSON(),trigger:document.querySelector('[data-memory-source]').parentNode.querySelector('[role=combobox]').getBoundingClientRect().toJSON(),menu:document.querySelector('[role=listbox]').getBoundingClientRect().toJSON()}));
  assert.ok(e.geometry.menu.top>=e.geometry.header.bottom,'listbox must stay below fixed header');
  assert.ok(e.geometry.menu.bottom<=360,'listbox stays within viewport');
  await page.keyboard.press('End');
  e.last=await trigger.evaluate(el=>{const row=document.getElementById(el.getAttribute('aria-activedescendant'));const r=row.getBoundingClientRect(),m=row.parentNode.getBoundingClientRect();return {top:r.top,bottom:r.bottom,menuTop:m.top,menuBottom:m.bottom};});
  assert.ok(e.last.top>=e.last.menuTop&&e.last.bottom<=e.last.menuBottom,'last option keyboard-scrolls into view');
 });
 await run('upward-menu-keeps-header-clear',{viewport:{width:1280,height:360}},async({page,trigger},e)=>{
  await trigger.evaluate(el=>window.scrollBy(0,el.getBoundingClientRect().top-(innerHeight-70)));
  await trigger.click();await page.getByRole('listbox').waitFor();
  e.geometry=await page.evaluate(()=>({header:document.querySelector('.refresh-topnav').getBoundingClientRect().toJSON(),trigger:document.querySelector('[data-memory-source]').parentNode.querySelector('[role=combobox]').getBoundingClientRect().toJSON(),menu:document.querySelector('[role=listbox]').getBoundingClientRect().toJSON()}));
  assert.ok(e.geometry.menu.bottom<e.geometry.trigger.top,'fixture exercises upward placement');assert.ok(e.geometry.menu.top>=e.geometry.header.bottom+8,'upward menu clears header and required gap');assert.ok(e.geometry.menu.bottom<360);
  await page.keyboard.press('End');await page.keyboard.press('Home');await page.keyboard.press('ArrowDown');
  e.long=await trigger.evaluate(el=>{const row=document.getElementById(el.getAttribute('aria-activedescendant')),r=row.getBoundingClientRect(),m=row.parentNode.getBoundingClientRect();return{text:row.textContent,top:r.top,bottom:r.bottom,menuTop:m.top,menuBottom:m.bottom};});
  assert.ok(e.long.text.includes(LONG));assert.ok(e.long.top>=e.long.menuTop&&e.long.bottom<=e.long.menuBottom,'long option reachable and fully visible via keyboard scrolling');
 });
 await run('long-option-readable',{viewport:{width:390,height:844}},async({page,trigger},e)=>{
  await trigger.click();const option=page.getByRole('option',{name:LONG,exact:true});
  await option.scrollIntoViewIfNeeded();
  e.text=await option.locator('span').first().evaluate(el=>{const r=el.getBoundingClientRect(),range=document.createRange();range.selectNodeContents(el);return{text:el.textContent,rect:r.toJSON(),lines:[...range.getClientRects()].map(r=>r.toJSON()),scrollWidth:el.scrollWidth,clientWidth:el.clientWidth};});
  assert.equal(e.text.text,LONG);assert.ok(e.text.lines.length>1,'long Chinese option wraps on phone');
  assert.ok(e.text.scrollWidth<=e.text.clientWidth+1,'option text has no hidden horizontal overflow');
  for(const line of e.text.lines)assert.ok(line.left>=0&&line.right<=390&&line.top>=e.text.rect.top-1&&line.bottom<=e.text.rect.bottom+1,'all text lines rendered inside visible option');
 });
 await run('keyboard-selection-cancel-and-disabled',{},async({page,trigger,requests},e)=>{
  await page.locator('[data-memory-source]').evaluate(el=>{el.options[2].disabled=true;window.fixtureChangeCount=0;el.addEventListener('change',()=>window.fixtureChangeCount++);});
  const baselineRequests=requests.capabilities;
  await page.keyboard.press('Tab');await trigger.focus();await page.keyboard.press('ArrowDown');
  const initial=await page.locator('[data-memory-source]').inputValue();
  for(const key of ['ArrowDown','ArrowDown','End','Home','ArrowDown','ArrowDown'])await page.keyboard.press(key);
  e.preview=await trigger.getAttribute('aria-activedescendant');
  assert.notEqual(await page.locator('#'+e.preview).getAttribute('aria-disabled'),'true','keyboard skips disabled options');
  assert.equal(await page.locator('[data-memory-source]').inputValue(),initial,'keyboard preview does not commit business value');assert.match(await page.locator('.capability-select-option').filter({has:page.locator('.capability-select-check',{hasText:'✓'})}).innerText(),/^全部来源项目/,'checkmark remains committed value');
  await page.keyboard.press('Escape');assert.equal(await page.getByRole('listbox').count(),0);assert.equal(await page.locator('[data-memory-source]').inputValue(),initial);assert.ok(await trigger.evaluate(el=>document.activeElement===el));
  e.previewChanges=await page.evaluate(()=>window.fixtureChangeCount);e.previewRequests=requests.capabilities-baselineRequests;assert.equal(e.previewChanges,0);assert.equal(e.previewRequests,0,'Arrow/Home/End/Escape preview performs no business reads');
  await page.keyboard.press('ArrowDown');await page.keyboard.press('End');await page.keyboard.press('Home');await page.keyboard.press('ArrowDown');
  await Promise.all([page.waitForResponse(r=>r.url().includes('/api/capabilities?')),page.keyboard.press('Enter')]);
  assert.equal(await page.locator('[data-memory-source]').inputValue(),LONG,'Enter commits preview');
  e.commitChanges=await page.evaluate(()=>window.fixtureChangeCount);e.commitRequests=requests.capabilities-baselineRequests;assert.equal(e.commitChanges,1,'Enter dispatches one change');assert.equal(e.commitRequests,1,'Enter triggers one business read');
  await page.keyboard.press('ArrowDown');await page.keyboard.press('Tab');assert.equal(await page.getByRole('listbox').count(),0);assert.ok(await trigger.evaluate(el=>document.activeElement!==el),'Tab leaves combobox without trap');
  await page.locator('[data-memory-source]').evaluate(el=>{el.disabled=true;});await page.waitForFunction(()=>document.querySelector('[data-memory-source]').parentElement.querySelector('[role=combobox]').disabled);
  e.disabled=await trigger.isDisabled();assert.ok(e.disabled);await trigger.dispatchEvent('keydown',{key:'ArrowDown'});assert.equal(await page.getByRole('listbox').count(),0);
 });
 await run('aria-selection-tracks-active-preview',{},async({page,trigger},e)=>{
  await trigger.focus();await page.keyboard.press('ArrowDown');const nativeBefore=await page.locator('[data-memory-source]').inputValue();await page.keyboard.press('ArrowDown');
  e.active=await trigger.getAttribute('aria-activedescendant');e.selected=await page.getByRole('option',{selected:true}).evaluateAll(els=>els.map(el=>el.id));
  assert.deepEqual(e.selected,[e.active],'aria-selected follows highlighted activedescendant in select-only combobox');
  assert.equal(await page.locator('[data-memory-source]').inputValue(),nativeBefore);await page.keyboard.press('Escape');assert.equal(await page.locator('[data-memory-source]').inputValue(),nativeBefore);
 });
 await run('mobile-nav-focus-outline-unclipped',{viewport:{width:390,height:844}},async({page},e)=>{
  await page.keyboard.press('Tab');const links=page.locator('.nav-preview-link');e.items=[];
  for(const width of [390,360]){await page.setViewportSize({width,height:844});
  for(let i=0;i<await links.count();i++){
   await links.nth(i).focus();
   e.items.push(await links.nth(i).evaluate(el=>{const r=el.getBoundingClientRect(),n=el.closest('nav').getBoundingClientRect(),s=getComputedStyle(el),extra=parseFloat(s.outlineWidth)+parseFloat(s.outlineOffset);return{label:el.textContent,focus:el.matches(':focus-visible'),extra,box:r.toJSON(),clip:n.toJSON(),fits:r.left-extra>=n.left-.5&&r.right+extra<=n.right+.5&&r.top-extra>=n.top-.5&&r.bottom+extra<=n.bottom+.5};}));
  }
  }
  assert.equal(e.items.length,22);assert.ok(e.items.every(item=>item.focus),'each destination receives keyboard-visible focus');assert.ok(e.items.every(item=>item.fits),'focused outlines must fit inside mobile scroll strip');
 });
 await run('focus-ring-contrast',{},async({page,trigger},e)=>{
  await page.keyboard.press('Tab');await trigger.focus();
  e.style=await trigger.evaluate(el=>{const s=getComputedStyle(el);let p=el.parentElement,bg;while(p){bg=getComputedStyle(p).backgroundColor;if(bg!=='rgba(0, 0, 0, 0)')break;p=p.parentElement;}return {outlineColor:s.outlineColor,outlineWidth:s.outlineWidth,outlineStyle:s.outlineStyle,background:bg,focusVisible:el.matches(':focus-visible')};});
  assert.ok(e.style.focusVisible);e.contrast=contrast(rgba(e.style.outlineColor),rgba(e.style.background));
  assert.ok(parseFloat(e.style.outlineWidth)>=2&&e.style.outlineStyle!=='none');assert.ok(e.contrast>=3,`focus ring contrast ${e.contrast.toFixed(2)} below chosen 3:1 target`);
 });
 await run('mobile-touch-targets',{viewport:{width:390,height:844},isMobile:true,hasTouch:true},async({page,trigger},e)=>{
  const account=page.locator('.nav-account-toggle'),logout=page.locator('.nav-logout');
  e.account=await account.boundingBox();e.logout=await logout.boundingBox();
  assert.ok(e.account.width>=44&&e.account.height>=44&&e.logout.width>=44&&e.logout.height>=44,'account and logout touch targets at least 44px');
  assert.ok(e.logout.x>=e.account.x+e.account.width||e.account.x>=e.logout.x+e.logout.width,'account/logout targets do not overlap');
  await account.tap();assert.equal(await account.getAttribute('aria-expanded'),'true');await account.tap();assert.equal(await account.getAttribute('aria-expanded'),'false');
  await trigger.tap();await page.getByRole('option',{name:LONG,exact:true}).tap();assert.equal(await page.locator('[data-memory-source]').inputValue(),LONG);
 });
 await run('mobile-touch-dropdown',{viewport:{width:390,height:844},isMobile:true,hasTouch:true},async({page,trigger},e)=>{
  const account=page.locator('.nav-account-toggle');await account.tap();assert.equal(await account.getAttribute('aria-expanded'),'true');await account.tap();assert.equal(await account.getAttribute('aria-expanded'),'false');
  await trigger.tap();const option=page.getByRole('option',{name:LONG,exact:true});await option.tap();assert.equal(await page.locator('[data-memory-source]').inputValue(),LONG);e.committed=LONG;
 });
 await run('reduced-motion-keeps-controls',{viewport:{width:390,height:844},reducedMotion:'reduce'},async({page,trigger},e)=>{
  e.matches=await page.evaluate(()=>matchMedia('(prefers-reduced-motion: reduce)').matches);assert.ok(e.matches);
  await trigger.click();e.styles=await page.locator('.capability-select-chevron,.nav-preview-icon').evaluateAll(els=>els.map(el=>{const s=getComputedStyle(el);return{transition:s.transitionDuration,animation:s.animationName};}));
  assert.ok(e.styles.every(s=>s.transition.split(',').every(v=>parseFloat(v)===0)&&s.animation==='none'));
  await page.keyboard.press('End');await page.keyboard.press('Escape');assert.equal(await trigger.getAttribute('aria-expanded'),'false');
 });
 for(const width of (ONLY.size?[]:[1440,390])){const env=await setup({viewport:{width,height:900}});await env.page.screenshot({path:path.join(OUTPUT_DIR,'overview-'+width+'.png'),fullPage:false});await env.page.waitForLoadState('networkidle');await env.context.close();}
 await browser.close();browser=null;fs.writeFileSync(OUT,JSON.stringify({base:BASE,results},null,2));
 if(results.some(r=>r.status==='fail'))process.exitCode=1;
})().catch(async e=>{console.error(e);if(browser)await browser.close();process.exitCode=1;});






