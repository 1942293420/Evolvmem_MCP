const assert=require('node:assert/strict');const fs=require('node:fs');const {chromium}=require('playwright');
const base=process.env.TEST_BASE||'http://127.0.0.1:39480';fs.mkdirSync('artifacts/nav-previews',{recursive:true});
(async()=>{const b=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});try{
 const p=await b.newPage({viewport:{width:1600,height:1100}});const errors=[];p.on('pageerror',e=>errors.push(e.message));
 for(const variant of ['contrast','cards']){
  await p.goto(base+'/?nav-design='+variant+'#home');await p.waitForSelector('[data-overview-capabilities]');
  assert.equal(await p.locator('body').getAttribute('data-nav-design'),variant);
  assert.equal(await p.locator('.nav-preview-link').count(),11);assert.equal(await p.locator('.nav-preview-group').count(),3);assert.equal(await p.locator('[data-more-toggle]').count(),0);
  // Synthetic account markup exercises the signed-in header geometry only.
  await p.locator('[data-auth-status]').evaluate(el=>{el.hidden=false;el.innerHTML='<span>测试账号 · 可编辑</span><button type="button">退出登录</button>';});
  for(const width of [390,768,901,960,961,1024,1280,1366,1440,1560,1600,1920]){
   await p.setViewportSize({width,height:1100});
   await p.evaluate(()=>window.scrollTo(0,0));
   assert.ok(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),'overflow '+variant+width);
   assert.equal(await p.locator('.nav-preview-link:visible').count(),11);
   const layout=await p.evaluate(()=>{
    const h=document.querySelector('.refresh-topnav'),r=h.getBoundingClientRect(),m=document.querySelector('.refresh-main').getBoundingClientRect();
    return {top:r.top,width:r.width,height:r.height,mainTop:m.top,mainLeft:m.left,position:getComputedStyle(h).position,
      clipped:[...document.querySelectorAll('.nav-preview-link')].some(a=>a.scrollWidth>a.clientWidth+1)};
   });
   assert.equal(layout.position,'sticky');assert.ok(Math.abs(layout.top)<1);assert.ok(layout.width>=width-20);
   assert.ok(layout.mainTop>=layout.height-1,'content under header');assert.ok(layout.mainLeft<2,'old sidebar offset');assert.equal(layout.clipped,false,'clipped navigation '+width);
   // Add length only: the actual production header/content layout remains unchanged.
   await p.evaluate(()=>{document.querySelector('.refresh-main').style.minHeight='2500px';window.scrollTo(0,900)});
   await p.waitForTimeout(70);
   assert.ok(await p.evaluate(()=>scrollY>100&&Math.abs(document.querySelector('.refresh-topnav').getBoundingClientRect().top)<1),'header must stay frozen '+width);
   await p.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.refresh-main').style.minHeight=''});
   if(width===1600||width===390)await p.screenshot({path:`artifacts/nav-previews/${variant}-${width}.png`,fullPage:width===390});
  }
  await p.setViewportSize({width:1600,height:1100});
  const link=p.locator('.nav-preview-link').filter({hasText:'开发能力'});
  await link.hover();await p.waitForTimeout(250);assert.notEqual(await link.evaluate(el=>getComputedStyle(el).transform),'none');
  await link.click();await p.waitForSelector('[data-memory-card]');assert.equal(await link.getAttribute('aria-current'),'page');
  await p.evaluate(()=>{document.querySelector('.refresh-main').style.minHeight='2500px';window.scrollTo(0,320)});await p.waitForTimeout(80);
  assert.ok(await p.evaluate(()=>document.querySelector('.capability-sidebar').getBoundingClientRect().top>=document.querySelector('.refresh-topnav').getBoundingClientRect().bottom),'category below frozen header');
  await p.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.refresh-main').style.minHeight=''});
  await p.screenshot({path:`artifacts/nav-previews/${variant}-capabilities.png`});
  const routes=await p.locator('.nav-preview-link').evaluateAll(els=>els.map(el=>el.getAttribute('href')));
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();await p.waitForFunction(()=>!document.querySelector('#content')?.inert);assert.equal(new URL(p.url()).hash.split('?')[0],href);
  }
  await p.locator('[data-nav-variant="'+(variant==='contrast'?'cards':'contrast')+'"]').click();
  assert.equal(await p.locator('body').getAttribute('data-nav-design'),variant==='contrast'?'cards':'contrast');
  await p.emulateMedia({reducedMotion:'reduce'});await link.hover();assert.equal(await link.evaluate(el=>getComputedStyle(el).transitionDuration),'0s');await p.emulateMedia({reducedMotion:'no-preference'});
  await p.setViewportSize({width:390,height:500});
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();assert.equal(new URL(p.url()).hash.split('?')[0],href);}
  assert.ok(await p.evaluate(()=>document.querySelector('.refresh-topnav').getBoundingClientRect().height<=innerHeight*.65+1),'short screen preserves content space');
  await p.setViewportSize({width:844,height:390});
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();assert.equal(new URL(p.url()).hash.split('?')[0],href);}
  await p.setViewportSize({width:1600,height:1100});
 }
 await p.goto(base+'/#home');await p.waitForSelector('[data-overview-capabilities]');assert.equal(await p.locator('body').getAttribute('data-nav-design'),null);assert.equal(await p.locator('[data-more-toggle]').count(),1);
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,variants:2,links:11,widths:[390,768,901,960,961,1024,1280,1366,1440,1560,1600,1920],shortViewports:['390x500','844x390'],errors}));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
