// Uses the synthetic knowledge_fixture.py server. Never point at a live database.
// 验收：资料详情与提炼预览能看到独立核对结论、原因和修正前后。
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE,headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(12000);
 const base=process.env.EVOLVMEM_TEST_URL||'http://127.0.0.1:39478',errors=[],checks=[];
 page.on('pageerror',e=>errors.push(e.message));
 const done=async()=>{await page.waitForLoadState('networkidle');return page.waitForFunction(()=>!document.querySelector('#content')?.inert&&document.querySelector('.knowledge-workspace')?.getAttribute('aria-busy')!=='true');};
 const go=async(hash,selector)=>{await done();await page.goto(base+'/'+hash);await page.waitForSelector(selector);await done();};
 const click=async(selector)=>{await page.locator(selector).first().click();await done();};
 try{
  await go('#knowledge/intake','table');
  const row=page.locator('tr',{hasText:'入口范围待确认'}).first();
  await row.locator('button, a').first().click();await done();
  await page.waitForSelector('#dialog');
  const detail=page.locator('#dialog');
  await detail.locator('.answer-support').waitFor();
  const text=await detail.locator('.answer-support').textContent();
  assert.match(text,/独立核对答案范围/);
  assert.match(text,/核对未通过，待确认/);
  assert.match(text,/答案增加了用户未说的入口范围/,{message:'必须显示实际核对原因'});
  assert.match(text,/设置入口在卡片本身，退款入口也在卡片上。/,{message:'必须显示实际引用的用户原话'});
  checks.push('资料详情显示独立核对结论、原因与实际用户原话');
  await detail.locator('[data-action="close"]').click();await done();
  const narrow=page.locator('tr',{hasText:'入口位置已收窄'}).first();
  await narrow.locator('button, a').first().click();await done();
  await page.waitForSelector('#dialog .answer-support',{state:'attached'});
  const narrowed=await page.locator('#dialog .answer-support').textContent();
  assert.match(narrowed,/已按原话收窄/);
  assert.match(narrowed,/核对答案设置入口在卡片本身/);
  assert.match(narrowed,/修正前答案：设置入口和退款入口都在卡片上/);
  assert.match(narrowed,/原引用：.*设置入口在卡片本身，退款入口也在卡片上。/);
  checks.push('收窄条目标出修正前答案与原引用');
  await page.locator('#dialog [data-action="close"]').click();await done();
  // QA detail (memory.js panel, not the source-item panel) must show the same review.
  await go('#knowledge/qa','#qa-project-filter');
  await page.locator('#qa-project-filter').selectOption('evo');await done();
  await page.waitForSelector('.qa-card');
  // The extracted Q&A card (a legacy unformatted row has no review metadata).
  const card=page.locator('.qa-card',{hasText:'修改界面前应先明确什么？'}).first();
  await card.locator('[data-memory-action="edit"]').click();await done();
  await page.waitForSelector('dialog[open] .answer-support',{state:'attached'});
  const qaSupport=await page.locator('dialog[open] .answer-support').last().textContent();
  assert.match(qaSupport,/独立核对答案范围/);
  assert.match(qaSupport,/核对依据（用户原话）/);
  assert.match(qaSupport,/核对答案/);
  assert.ok(!qaSupport.includes('<script'),'核对面板必须转义文本，不能注入脚本');
  checks.push('QA 详情（memory.js）显示核对结论、原话依据与核对答案并转义');
  await page.keyboard.press('Escape');await done();

  await go('#knowledge/rules','#min-confidence');
  await click('[data-rule-stage="trial"]');
  await page.locator('#extraction-sample').fill('用户：以后上传资料前先确认目标项目。\n助手：我会先确认项目。\n用户：纠正：禁止上传任何资料。');
  await page.locator('#extraction-project').selectOption('evo');
  await click('#extraction-preview-button');
  await page.waitForSelector('.extraction-comparison .extraction-candidate');
  const preview=await page.locator('.extraction-comparison').textContent();
  assert.match(preview,/独立核对答案范围/,{message:'预览必须显示独立核对结论'});
  assert.match(preview,/核对未通过，待确认/);
  assert.match(preview,/用户随后明确纠正：禁止上传任何资料/);
  checks.push('提炼预览显示核对未通过及用户纠正原因');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({checks,page_errors:errors},null,2));
 }catch(error){console.error('CHECKS',checks);await page.screenshot({path:'/tmp/evo-answer-support-failure.png',fullPage:true});throw error;}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
