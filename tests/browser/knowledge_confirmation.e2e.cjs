const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(15000);
 try{
  await page.goto(process.env.EVOLVMEM_TEST_URL+'/#knowledge/organization');
  await page.getByRole('heading',{name:'待归档的新内容'}).waitFor();
  await page.locator('[data-rg-list] button').filter({hasText:'新增要求'}).click();
  await page.locator('[data-rg-original]').filter({hasText:'新增要求'}).waitFor();
  assert.ok(!(await page.locator('[data-rg-original]').innerText()).includes('Demo 已确认开关要求'));
  const bg=page.locator('[data-rg-background] details');
  assert.equal(await bg.getAttribute('open'),null);
  await bg.locator('summary').click();assert.ok((await bg.innerText()).includes('Demo 已确认开关要求'));
  await bg.locator('summary').click();
  for(const width of [1440,390]){
   await page.setViewportSize({width,height:1050});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));
   await page.screenshot({path:`/tmp/evo-new-content-${width}.png`,fullPage:true});
  }
  await page.setViewportSize({width:1440,height:1050});
  await page.locator('[data-rg-list] button').filter({hasText:'运行环境的另一种说法'}).click();
  await page.locator('[data-ke-open] > summary').click();
  await page.locator('[data-ke-candidate]').filter({hasText:'已归档 Linux 环境'}).click();
  await page.locator('[data-ke-query]').fill('Linux');
  await page.locator('[data-rg-list] button').filter({hasText:'新增要求'}).click();
  await page.locator('[data-rg-list] button').filter({hasText:'运行环境的另一种说法'}).click();
  await page.waitForFunction(()=>document.querySelector('[data-rg-title]').textContent==='运行环境的另一种说法'&&!document.querySelector('[data-rg-form]').hidden);
  assert.equal(await page.locator('[data-ke-query]').inputValue(),'Linux');
  assert.ok(await page.locator('[data-ke-confirm]').isVisible());
  await page.screenshot({path:'/tmp/evo-confirm-expression-1440.png',fullPage:true});
  await page.locator('[data-ke-confirm]').click();
  await page.getByRole('heading',{name:'已确认是同一条知识'}).waitFor();
  assert.equal(await page.locator('[data-rg-list] button').filter({hasText:'运行环境的另一种说法'}).count(),0);
  const data=await page.evaluate(async()=>await (await fetch('/api/knowledge/directory?project=demo')).json());
  assert.equal(data.items.filter(x=>x.expressions?.length).length,1);
  assert.ok(data.items.find(x=>x.expressions?.length).body.includes('Demo 的运行系统是 Linux'));
  await page.getByRole('tab',{name:'同义表达',exact:true}).click();
  await page.getByRole('heading',{name:'已确认的同义表达'}).waitFor();
  await page.getByRole('button',{name:'撤销同义关联'}).click();
  await page.getByText('还没有确认过同义表达。',{exact:true}).waitFor();
  const after=await page.evaluate(async()=>await (await fetch('/api/knowledge/directory?project=demo')).json());
  assert.equal(after.items.length,data.items.length+1);
  await page.getByRole('tab',{name:'待你判断',exact:true}).click();
  await page.locator('[data-rg-list] button').filter({hasText:'新增要求'}).click();
  await page.locator('[data-rg-form] [name=projects][value=other]').check();
  await page.locator('[data-rg=file]').click();
  await page.getByRole('heading',{name:'归档已保存'}).waitFor();
  const newlySaved=await page.evaluate(async()=>await (await fetch('/api/knowledge/directory?project=other')).json());
  assert.ok(newlySaved.items.some(x=>x.body.includes('Other 支持独立开关')));
  assert.ok(newlySaved.items.every(x=>!x.body.includes('Demo 已确认开关要求')));
  const retained=await page.evaluate(async()=>await (await fetch('/api/knowledge/directory?project=demo')).json());
  assert.ok(retained.items.some(x=>x.body.includes('Demo 已确认开关要求')));
  assert.equal(errors.length,0,errors.join('\n'));
  console.log(JSON.stringify({passed:['仅新增内容待审，已确认背景折叠','1440/390 布局无溢出','同义候选与核对入口、切换后缓存','人工确认后归入项目、统一目录合并来源','撤销关联保留归档','新增要求保存到 Other，旧 Demo 归属不变'],screenshots:['/tmp/evo-new-content-1440.png','/tmp/evo-new-content-390.png','/tmp/evo-confirm-expression-1440.png']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
