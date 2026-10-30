const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const base=process.env.TEST_BASE||'http://127.0.0.1:39480';
(async()=>{
 const browser=await chromium.launch({channel:process.env.BROWSER_CHANNEL||'msedge',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}}), errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let payload=null, snapshot, assigned=false, sources=[];
  await page.route('**/api/capabilities?*',async route=>{
   sources.push(new URL(route.request().url()).searchParams.get('source_project'));
   const response=await route.fetch(),data=await response.json();
   snapshot=data;
   data.intake=assigned?[]:[{intake_id:'select-fixture',title:'下拉交互验收',content:'仅供页面交互验证的合成资料。',source_project:'bluewhale',suggested_category:data.categories[0].id,revision:1}];
   data.intake_total=data.intake.length;
   await route.fulfill({response,json:data});
  });
  await page.route('**/api/capabilities/intake/select-fixture/assign',async route=>{
   payload=route.request().postDataJSON();assigned=true;
   await new Promise(resolve=>setTimeout(resolve,150));await route.fulfill({json:{ok:true}});
  });
  await page.goto(base+'/#knowledge/capabilities');
  await page.waitForSelector('[data-memory-card]');
  const source=page.locator('[data-memory-source]'),sourceButton=page.locator('.capability-source-select [role=combobox]');
  assert.equal(await source.getAttribute('tabindex'),'-1');assert.equal(await source.getAttribute('aria-hidden'),'true');
  await sourceButton.click();await page.waitForSelector('.capability-select-menu');
  assert.equal(await sourceButton.getAttribute('aria-expanded'),'true');
  await page.keyboard.press('End');await page.keyboard.press('Enter');
  await page.waitForFunction(()=>!document.querySelector('.capability-workspace')?.getAttribute('aria-busy')||document.querySelector('.capability-workspace').getAttribute('aria-busy')==='false');
  const sourceValue=await source.inputValue();assert.notEqual(sourceValue,'all');assert.ok(sources.includes(sourceValue));
  // Existing native select consumers still drive the same change event/value.
  await source.selectOption('all');assert.equal(await source.inputValue(),'all');
  await page.waitForTimeout(100);assert.match(await sourceButton.innerText(),/全部来源项目/);
  await page.locator('[data-tab=intake]').click();
  const select=page.locator('[data-intake-category="select-fixture"]');
  const trigger=page.locator('[data-intake-card="select-fixture"] [role=combobox]');
  await trigger.click();await page.waitForSelector('.capability-select-menu');
  const options=await select.locator('option').evaluateAll(items=>items.map(item=>({value:item.value,label:item.textContent})));
  assert.ok(options.length>1);
  await page.locator('.capability-select-option').nth(1).click();
  assert.equal(await select.inputValue(),options[1].value);assert.match(await trigger.innerText(),new RegExp(options[1].label));
  assert.equal(await page.locator('.capability-select-menu').count(),0);
  assert.equal(await trigger.evaluate(el=>document.activeElement===el),true);
  await trigger.press('ArrowDown');await trigger.press('Home');await trigger.press('ArrowDown');await trigger.press('Enter');
  assert.equal(await select.inputValue(),options[1].value);
  await trigger.click();await trigger.press('Escape');assert.equal(await page.locator('.capability-select-menu').count(),0);
  await trigger.click();await page.locator('.capability-heading h1').click();assert.equal(await page.locator('.capability-select-menu').count(),0);
  await trigger.click();await trigger.press('Tab');assert.equal(await page.locator('.capability-select-menu').count(),0);
  // Disabled state is observed from the real select, including while open.
  await trigger.click();await select.evaluate(el=>el.disabled=true);await page.waitForFunction(()=>document.querySelector('[data-intake-card] [role=combobox]').disabled);
  assert.equal(await page.locator('.capability-select-menu').count(),0);
  await select.evaluate(el=>el.disabled=false);await page.waitForFunction(()=>!document.querySelector('[data-intake-card] [role=combobox]').disabled);
  await trigger.click();await page.locator('.signal-nav [data-page-link=home]').click();
  assert.equal(await page.locator('.capability-select-menu').count(),0);
  await page.locator('.signal-nav [data-knowledge-view=capabilities]').click();
  await page.waitForSelector('[data-intake-card]');assert.equal(await select.inputValue(),options[1].value);
  await page.setViewportSize({width:390,height:760});
  await page.evaluate(()=>window.scrollTo(0,0));
  await trigger.click();await page.waitForTimeout(220);
  assert.equal(await page.locator('.capability-select-menu').isVisible(),true,'first click after automatic scroll must remain open');
  assert.ok(await page.locator('.capability-select-menu').evaluate(el=>{const r=el.getBoundingClientRect();const hit=document.elementFromPoint(r.x+r.width/2,r.y+18);return el.contains(hit);}), 'upward menu is not covered by header');
  await page.evaluate(()=>window.scrollBy(0,-40));await page.waitForTimeout(60);
  assert.equal(await page.locator('.capability-select-menu').count(),0,'actual document scroll closes anchored menu');
  await trigger.press('Escape');
  for(const width of [390,768,1440]) {
   await page.setViewportSize({width,height:1000});await trigger.scrollIntoViewIfNeeded();
   const layout=await trigger.evaluate(el=>{const r=el.getBoundingClientRect(),label=el.closest('.intake-category-label').querySelector(':scope>span').getBoundingClientRect();return{top:r.top,labelBottom:label.bottom,right:r.right,viewport:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth+1};});
   assert.ok(layout.top>=layout.labelBottom+5,'label overlaps control at '+width);assert.ok(layout.right<=layout.viewport);assert.equal(layout.overflow,false);
   await trigger.click();const box=await page.locator('.capability-select-menu').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1&&box.y>=0&&box.y+box.height<=1001,'menu viewport bounds '+width);
   fs.mkdirSync('artifacts/select-ui/screenshots',{recursive:true});await page.waitForTimeout(180);await page.screenshot({path:`artifacts/select-ui/screenshots/intake-${width}.png`});
   await trigger.press('Escape');
  }
  // Selection is submitted through the original assignment workflow.
  await page.locator('[data-assign="select-fixture"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-intake-card] [role=combobox]')?.disabled===true);
  await page.waitForFunction(()=>!document.querySelector('[data-intake-card="select-fixture"]'));
  assert.deepEqual(payload,{category:options[1].value,expected_revision:1});assert.equal(assigned,true);assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,categories:snapshot.categories.length,widths:[390,768,1440],checks:['keyboard','mouse','native-change','disabled-sync','escape','outside','tab','navigation-close','draft-preserved','label-spacing','viewport-bounds','auto-scroll-open','real-scroll-close','upward-menu-hit','assignment-payload'],errors}));
 } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
