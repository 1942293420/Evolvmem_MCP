const assert=require('node:assert/strict');const fs=require('node:fs');const {chromium}=require('playwright');
const base=process.env.TEST_BASE||'http://127.0.0.1:39480';fs.mkdirSync('artifacts/slim-nav',{recursive:true});
(async()=>{const b=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});try{
 const p=await b.newPage({viewport:{width:1600,height:1100}});const errors=[];p.on('pageerror',e=>errors.push(e.message));
 for(const variant of ['contrast','cards']){
  await p.goto(base+'/?nav-design='+variant+'#home');await p.waitForSelector('[data-overview-capabilities]');
  assert.equal(await p.locator('body').getAttribute('data-nav-design'),variant);
  assert.equal(await p.locator('.nav-preview-link').count(),11);assert.equal(await p.locator('.nav-preview-group').count(),3);assert.equal(await p.locator('[data-more-toggle]').count(),0);
  // Synthetic account markup exercises the signed-in header geometry only.
  await p.locator('[data-auth-status]').evaluate(el=>{el.hidden=false;el.innerHTML='<span>Jiangli 范志豪 · 可编辑</span><button type="button">退出登录</button>';});
  for(const width of [390,768,901,960,961,1024,1239,1240,1279,1280,1366,1440,1499,1500,1560,1600,1679,1680,1694,1920,2118]){
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
   assert.ok(layout.mainTop>=layout.height-1,'content under header');assert.ok(layout.mainLeft<2,'old sidebar offset');if(width>=1500)assert.ok(layout.height<=60,'slim header too tall '+layout.height);else if(width>=1280)assert.ok(layout.height<=96,'compact header too tall '+layout.height);assert.equal(layout.clipped,false,'clipped navigation '+width);
   assert.ok(await p.locator('.nav-preview-link').evaluateAll(els=>els.every(el=>{const r=el.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);return el===hit||el.contains(hit)})),'covered navigation '+width);
   if(width>=1500)assert.ok(await p.evaluate(()=>{const es=[document.querySelector('.signal-brand'),...document.querySelectorAll('.nav-preview-link'),document.querySelector('.auth-status')];const centers=es.map(e=>{const r=e.getBoundingClientRect();return r.y+r.height/2});return Math.max(...centers)-Math.min(...centers)<2}),'single aligned row '+width);
   // Add length only: the actual production header/content layout remains unchanged.
   await p.evaluate(()=>{document.querySelector('.refresh-main').style.minHeight='2500px';window.scrollTo(0,900)});
   await p.waitForTimeout(70);
   assert.ok(await p.evaluate(()=>scrollY>100&&Math.abs(document.querySelector('.refresh-topnav').getBoundingClientRect().top)<1),'header must stay frozen '+width);
   await p.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.refresh-main').style.minHeight=''});
   if(width===1600||width===390)await p.screenshot({path:`artifacts/slim-nav/${variant}-${width}.png`,fullPage:width===390});
  }
  await p.setViewportSize({width:1600,height:1100});
  const link=p.locator('.nav-preview-link').filter({hasText:'开发能力'});
  await link.hover();await p.waitForTimeout(250);assert.notEqual(await link.evaluate(el=>getComputedStyle(el).transform),'none');
  await link.click();await p.waitForSelector('[data-memory-card]');assert.equal(await link.getAttribute('aria-current'),'page');
  await p.evaluate(()=>{document.querySelector('.refresh-main').style.minHeight='2500px';window.scrollTo(0,320)});await p.waitForTimeout(80);
  assert.ok(await p.evaluate(()=>document.querySelector('.capability-sidebar').getBoundingClientRect().top>=document.querySelector('.refresh-topnav').getBoundingClientRect().bottom),'category below frozen header');
  await p.evaluate(()=>{window.scrollTo(0,0);document.querySelector('.refresh-main').style.minHeight=''});
  await p.screenshot({path:`artifacts/slim-nav/${variant}-capabilities.png`});
  const routes=await p.locator('.nav-preview-link').evaluateAll(els=>els.map(el=>el.getAttribute('href')));
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();await p.waitForFunction(()=>!document.querySelector('#content')?.inert);assert.equal(new URL(p.url()).hash.split('?')[0],href);
  }
  assert.equal(await p.locator('.nav-preview-switcher,.nav-preview-banner').count(),0);
  await p.emulateMedia({reducedMotion:'reduce'});await link.hover();assert.equal(await link.evaluate(el=>getComputedStyle(el).transitionDuration),'0s');await p.emulateMedia({reducedMotion:'no-preference'});
  await p.setViewportSize({width:390,height:500});
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();assert.equal(new URL(p.url()).hash.split('?')[0],href);}
  assert.ok(await p.evaluate(()=>document.querySelector('.refresh-topnav').getBoundingClientRect().height<=innerHeight*.65+1),'short screen preserves content space');
  await p.setViewportSize({width:844,height:390});
  for(const href of routes){await p.locator(`.nav-preview-link[href="${href}"]`).click();assert.equal(new URL(p.url()).hash.split('?')[0],href);}
  await p.setViewportSize({width:1600,height:1100});
 }
 await p.goto(base+'/#home');await p.waitForSelector('[data-overview-capabilities]');assert.equal(await p.locator('body').getAttribute('data-nav-design'),'contrast');assert.equal(await p.locator('[data-more-toggle]').count(),0);assert.equal(new URL(p.url()).search,'');
 assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,variants:2,links:11,widths:[390,768,901,960,961,1024,1239,1240,1279,1280,1366,1440,1499,1500,1560,1600,1679,1680,1694,1920,2118],shortViewports:['390x500','844x390'],errors}));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exitCode=1});
