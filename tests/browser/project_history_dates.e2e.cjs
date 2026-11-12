// Real HTTP + browser regression against project_history_fixture.py only.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
  const options = {headless:true,args:['--no-sandbox']};
  if (process.env.CHROMIUM_EXECUTABLE) options.executablePath=process.env.CHROMIUM_EXECUTABLE;
  if (process.env.CHROMIUM_CHANNEL) options.channel=process.env.CHROMIUM_CHANNEL;
  const browser=await chromium.launch(options), errors=[];
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const base=process.env.EVOLVMEM_HISTORY_TEST_URL || 'http://127.0.0.1:39482';
  const output=process.env.EVOLVMEM_SCREENSHOT_DIR || '.';
  fs.mkdirSync(output,{recursive:true});
  page.on('pageerror',e=>errors.push(e.message));
  try {
    await page.goto(base+'/#knowledge/archives');
    await page.locator('[name=project]').fill('AI采购');
    await page.locator('#project-archive-form button').click();
    await page.waitForSelector('[data-archive-summary]');
    assert.equal(await page.locator('[data-archive-summary] h2').innerText(),'AI采购');
    assert.match(await page.locator('[data-archive-summary] .subtitle').innerText(),/最早有日期记录 2026-07-20/);
    assert.doesNotMatch(await page.locator('[data-archive-summary] .subtitle').innerText(),/开始于|2026-10-28|开发周期/);
    for (const date of ['2026-07-20','2026-08-05','2026-08-31']) {
      const item=page.locator('[data-project-event]').filter({hasText:date});
      assert.match(await item.innerText(),/仅到日，时区未确认/);
    }
    const unknown=page.locator('[data-project-event]').filter({hasText:'undated'});
    assert.match(await unknown.innerText(),/发生时间未确认 · 入库时间：2026-10-28/);
    await unknown.click();
    assert.match(await unknown.innerText(),/context_item:/);
    for(const width of [390,768,1440]){
      await page.setViewportSize({width,height:1000});
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),`overflow ${width}`);
      await page.screenshot({path:path.join(output,`history-dates-${width}.png`),fullPage:true});
    }
    await page.locator('[name=project]').fill('日期未知项目');
    await page.locator('#project-archive-form button').click();
    await page.waitForFunction(()=>document.querySelector('[data-archive-summary] h2')?.textContent==='日期未知项目');
    assert.match(await page.locator('[data-archive-summary] .subtitle').innerText(),/最早有日期记录 未确认 · 距最早记录 未确认/);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({checks:['Chinese canonical display','July 20 August 5 August 31 literal source days','unknown occurrence distinct from import time','unknown project start','source expansion','390 768 1440 layouts'],errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
