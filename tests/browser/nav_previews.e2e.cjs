const assert=require('node:assert/strict');const fs=require('node:fs');const {chromium}=require('playwright');
const base=process.env.TEST_BASE||'http://127.0.0.1:39480';fs.mkdirSync('artifacts/nav-previews',{recursive:true});
(async()=>{const b=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});try{
 const p=await b.newPage({viewport:{width:1600,height:1100}});const errors=[];p.on('pageerror',e=>errors.push(e.message));
 for(const variant of ['contrast','cards']){
  await p.goto(base+'/?nav-design='+variant+'#home');await p.waitForSelector('[data-overview-capabilities]');
  assert.equal(await p.locator('body').getAttribute('data-nav-design'),variant);
  assert.equal(await p.locator('.nav-preview-link').count(),11);assert.equal(await p.locator('.nav-preview-group').count(),3);assert.equal(await p.locator('[data-more-toggle]').count(),0);
  for(const width of [390,768,1024,1600,1920]){
   await p.setViewportSize({width,height:1100});
   assert.ok(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+variant+width);
   assert.equal(await p.locator('.nav-preview-link:visible').count(),11);
   if(width===1600||width===390)await p.screenshot({path:`artifacts/nav-previews/${variant}-${width}.png`,fullPage:width===390});
  }
  await p.setViewportSize({width:1600,height:1100});
  const link=p.locator('.nav-preview-link').filter({hasText:'开发能力'});
  await link.hover();await p.waitForTimeout(250);assert.notEqual(await link.evaluate(el=>getComputedStyle(el).transform),'none');
  await link.click();await p.waitForSelector('[data-memory-card]');assert.equal(await link.getAttribute('aria-current'),'page');
  await p.screenshot({path:`artifacts/nav-previews/${variant}-capabilities.png`});
  const routes=await p.locator('.nav-preview-link').evaluateAll(els=>els.map(el=>el.getAttribute('href')));
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();await p.waitForFunction(()=>!document.querySelector('#content')?.inert);assert.equal(new URL(p.url()).hash.split('?')[0],href);}
  await p.locator('[data-nav-variant="'+(variant==='contrast'?'cards':'contrast')+'"]').click();
  assert.equal(await p.locator('body').getAttribute('data-nav-design'),variant==='contrast'?'cards':'contrast');
  await p.emulateMedia({reducedMotion:'reduce'});await link.hover();assert.equal(await link.evaluate(el=>getComputedStyle(el).transitionDuration),'0s');await p.emulateMedia({reducedMotion:'no-preference'});
 }
 await p.goto(base+'/#home');await p.waitForSelector('[data-overview-capabilities]');assert.equal(await p.locator('body').getAttribute('data-nav-design'),null);assert.equal(await p.locator('[data-more-toggle]').count(),1);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,variants:2,links:11,widths:[390,768,1024,1600,1920],errors}));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
