// Synthetic loopback UI fixture. No production connection, cookies, or skill writes.
'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../../evolvmem/web_static');
const output = process.env.EVOLVMEM_SCREENSHOTS || '/tmp/evolvmem-skill-grid-screenshots';
fs.mkdirSync(output,{recursive:true});
const categoryLabels={frontend:'前端与界面',development:'开发与代码',testing:'测试与验收',business:'业务与数据',feishu:'飞书协作',ziniao:'紫鸟管理',visual:'绘图与图像',system:'系统与工具',skills:'技能管理',uncategorized:'待分类'};
const assigned = i => i===35?['uncategorized']:i%3===0?['frontend','testing']:i%3===1?['business']:['development'];
const rows = Array.from({length:36},(_,i)=>({
  id: String(i).padStart(24,'0'),name: 'original-skill-'+i,description:'Original English purpose '+i,
  categories: assigned(i).map(id=>({id,label:categoryLabels[id]})),
  source: i%2?'windows':'official',origin: i%2?'Codex':'Official',version:'a'.repeat(64),url:'',
  presentation:{title_zh: i===0?'设计页面布局与交互':'测试用途 '+i,summary_zh:'根据页面内容设计布局，检查窄屏、键盘操作与用户反馈。',
    content_zh:'适用场景：需要调整页面布局或交互。\n主要功能：检查信息层级、空间分配和反馈。\n内容中的 <script>window.skillXSS=true</script> 只作为文字显示。'}
}));
delete rows[1].presentation;
const files = {'SKILL.md':'---\nname: native-original\n---\n# Keep this original <img src=x onerror="window.skillXSS=true">\nDo not translate this file.\n',
  'references/guide.md':'# Raw reference\nOriginal guide content unchanged.\n'};
const sync={official:'2026-11-10T00:00:00Z',windows:'2026-11-10T01:00:00Z'};
let listRequests=0,detailRequests=0,failList=false,failDetail=false,failFile=false,delayDetail=false,delayCategory='';
const server=http.createServer((req,res)=>{
  const u=new URL(req.url,'http://127.0.0.1'),send=(value,status=200)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));};
  if(u.pathname==='/api/knowledge-console/me')return send({current_user:{id:'synthetic',display_name:'合成管理员',is_admin:true},libraries:[],policy:{ai_processing_enabled:false}});
  if(u.pathname==='/api/knowledge-console/skills'){
    const id=u.searchParams.get('id'),file=u.searchParams.get('file')||'SKILL.md';
    if(id){
      detailRequests++;
      const finish=()=>{if(failDetail || (failFile&&file!=='SKILL.md')) return send({error:'fixture_failure'},503);
        return send({ok:true,skill:rows.find(r=>r.id===id),files:Object.keys(files),file,text:files[file]});};
      if(delayDetail){setTimeout(finish,300);return;}return finish();
    }
    listRequests++;if(failList)return send({error:'fixture_failure'},503);
    const q=(u.searchParams.get('q')||'').toLowerCase(),source=u.searchParams.get('source'),category=u.searchParams.get('category')||'';
    const matches=rows.filter(r=>(!source||r.source===source)&&JSON.stringify(r).toLowerCase().includes(q));
    const result={ok:true,rows:matches.filter(r=>!category||r.categories.some(c=>c.id===category)),sync,
      matched_total:matches.length,categories:Object.entries(categoryLabels).map(([id,label])=>({id,label,count:matches.filter(r=>r.categories.some(c=>c.id===id)).length}))};
    if(category===delayCategory && delayCategory){setTimeout(()=>send(result),350);return;}
    return send(result);
  }
  if(u.pathname==='/api/knowledge-console/libraries')return send({libraries:[]});
  const name=u.pathname==='/'?'knowledge-console.html':u.pathname.slice(1);
  if(!/^[a-z-]+\.(html|css|js)$/.test(name)||!fs.existsSync(path.join(root,name))){res.writeHead(404);return res.end();}
  res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');
  res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base='http://127.0.0.1:'+server.address().port,checks=[],errors=[];
 let browser,context,page;
 try{
  browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE,args:['--no-sandbox']});
  context=await browser.newContext({viewport:{width:1440,height:1000}});
  page=await context.newPage();page.setDefaultTimeout(6000);page.on('pageerror',error=>errors.push(error.message));
  await page.goto(base+'/#skills');await page.locator('.skill-card').first().waitFor();
  assert.equal(await page.locator('.skill-card').count(),36);
  assert.equal(await page.locator('.experience-layout:visible,.experience-list:visible').count(),0);
  assert.equal(await page.locator('#skill-detail').isVisible(),false);
  assert.equal(detailRequests,0,'do not automatically load the first skill');
  assert.equal(await page.locator('.skill-card-title').first().textContent(),'设计页面布局与交互');
  assert.match(await page.locator('.skill-card').nth(1).textContent(),/中文说明待更新/);
  assert.equal(await page.locator('[data-skill-category=""][aria-pressed="true"]').count(),1);
  assert.equal(await page.locator('[data-skill-category="frontend"] .skill-category-count').textContent(),'12');
  assert.match(await page.locator('.skill-card').first().textContent(),/前端与界面.*测试与验收/s);
  checks.push('grid with translated purpose title, original name, category badges, fallback, and no eager detail');

  for(const width of [320,390,768,1440,2560,3440]){
    await page.setViewportSize({width,height:1000});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'page overflow '+width);
    const geometry=await page.locator('#skill-results').evaluate(el=>({width:el.getBoundingClientRect().width,columns:getComputedStyle(el).gridTemplateColumns.split(' ').length}));
    assert.ok(geometry.width>=width-65,'use available width '+width);
    assert.equal(width<600?geometry.columns===1:geometry.columns>1,true,'responsive column count '+width);
    if(width===1440||width===390)await page.screenshot({path:path.join(output,'gallery-'+width+'.png'),fullPage:false});
  }
  checks.push('full-width grid, 320–3440 widths, no horizontal overflow');

  await page.setViewportSize({width:1440,height:800});
  const last=page.locator('.skill-card').last();await last.scrollIntoViewIfNeeded();
  const beforeY=await page.evaluate(()=>window.scrollY),listBefore=listRequests,selectedId=await last.getAttribute('data-skill');
  await last.focus();await page.keyboard.press('Enter');await page.locator('#skill-title').waitFor();
  assert.equal(await page.locator('#skill-catalog').isVisible(),false);
  assert.equal(await page.locator('.skill-original').evaluate(el=>el.open),false);
  assert.match(await page.locator('.skill-explanation').textContent(),/适用场景/);
  assert.equal(await page.evaluate(()=>window.skillXSS),undefined);
  assert.equal(await page.locator('#skill-detail script,#skill-detail img').count(),0);
  await page.locator('.skill-original summary').click();
  assert.equal(await page.locator('.skill-text').textContent(),files['SKILL.md']);
  await page.locator('#skill-file').selectOption('references/guide.md');
  await page.waitForFunction(()=>document.querySelector('.skill-text')?.textContent.includes('Original guide'));
  assert.equal(await page.locator('.skill-text').textContent(),files['references/guide.md']);
  checks.push('Chinese detail first, native file kept byte-for-byte, inert raw text and file switch');

  await page.locator('#skill-back').click();
  assert.equal(await page.locator(':focus').getAttribute('data-skill'),selectedId);
  assert.ok(Math.abs((await page.evaluate(()=>window.scrollY))-beforeY)<=2,'restore grid scroll');
  assert.equal(listRequests,listBefore,'Back uses retained grid and no new catalog request');
  checks.push('keyboard open; Back restores card focus and scroll without refetch');

  await page.locator('#skill-filters [name=q]').fill('布局');await page.locator('#skill-filters [name=source]').selectOption('official');
  await page.locator('#skill-filters [type=submit]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.skill-card').length===18);
  await page.locator('.skill-card').first().click();await page.locator('#skill-title').waitFor();await page.locator('#skill-back').click();
  assert.equal(await page.locator('#skill-filters [name=q]').inputValue(),'布局');
  assert.equal(await page.locator('#skill-filters [name=source]').inputValue(),'official');
  await page.locator('[data-skill-category="frontend"]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.skill-card').length===6);
  assert.equal(await page.locator('[data-skill-category="frontend"][aria-pressed="true"]').count(),1);
  assert.equal(await page.locator('[data-skill-category=""] .skill-category-count').textContent(),'18');
  assert.equal(await page.locator('[data-skill-category="uncategorized"]').count(),0);
  await page.locator('.skill-card').first().click();await page.locator('#skill-title').waitFor();await page.locator('#skill-back').click();
  assert.equal(await page.locator('[data-skill-category="frontend"][aria-pressed="true"]').count(),1);
  assert.equal(await page.locator('#skill-filters [name=q]').inputValue(),'布局');
  assert.equal(await page.locator('#skill-filters [name=source]').inputValue(),'official');
  await page.locator('#skill-filters [name=source]').selectOption('windows');
  await page.waitForFunction(()=>document.querySelector('[data-skill-category=""] .skill-category-count')?.textContent==='17');
  assert.equal(await page.locator('.skill-card').count(),6);
  checks.push('combined Chinese query/source/category counts and preserved detail/Back filters; source change auto-applies');

  await page.locator('#skill-filters [name=q]').fill('missing-query');await page.locator('#skill-filters [type=submit]').click();
  await page.locator('#skill-clear').waitFor();await page.locator('#skill-clear').click();
  await page.waitForFunction(()=>document.querySelectorAll('.skill-card').length===36);
  failList=true;await page.locator('#skill-filters [name=q]').fill('will-fail');await page.locator('[data-skill-category="frontend"]').click();
  await page.locator('#console-notice.error').waitFor();assert.equal(await page.locator('.skill-card').count(),36);
  assert.equal(await page.locator('[data-skill-category=""][aria-pressed="true"]').count(),1);
  assert.equal(await page.locator('#skill-filters [name=q]').inputValue(),'');
  assert.match(await page.locator('#console-notice').textContent(),/筛选未生效/);
  failList=false;await page.locator('#skill-filters [type=submit]').click();await page.locator('#console-notice').waitFor({state:'hidden'});
  checks.push('empty search reset; failed category/query restores committed filters and truthfully retains cards');
  delayCategory='frontend';await page.locator('[data-skill-category="frontend"]').click();
  await page.locator('[data-skill-category="business"]').click();
  await page.waitForFunction(()=>document.querySelector('[data-skill-category="business"]')?.getAttribute('aria-pressed')==='true');
  await page.waitForTimeout(450);
  assert.equal(await page.locator('[data-skill-category="business"][aria-pressed="true"]').count(),1);
  assert.equal(await page.locator('.skill-card').count(),12);
  delayCategory='';await page.locator('#skill-reset').click();await page.waitForFunction(()=>document.querySelectorAll('.skill-card').length===36);
  checks.push('rapid category changes ignore late responses; persistent reset clears every filter');
  delayCategory='frontend';await page.locator('[data-skill-category="frontend"]').click();
  await page.locator('#skill-filters [name=source]').selectOption('official');
  await page.waitForFunction(()=>document.querySelector('[data-skill-category="frontend"]')?.getAttribute('aria-pressed')==='true' && document.querySelectorAll('.skill-card').length===6);
  assert.equal(await page.locator('#skill-filters [name=source]').inputValue(),'official');
  assert.equal(await page.locator('[data-skill-category=""] .skill-category-count').textContent(),'18');
  delayCategory='';await page.locator('#skill-reset').click();await page.waitForFunction(()=>document.querySelectorAll('.skill-card').length===36);
  checks.push('pending category then source change preserves both latest filter intentions');

  failDetail=true;await page.locator('.skill-card').first().click();await page.locator('#skill-detail-retry').waitFor();
  failDetail=false;await page.locator('#skill-detail-retry').click();await page.locator('#skill-title').waitFor();
  await page.locator('.skill-original summary').click();failFile=true;await page.locator('#skill-file').selectOption('references/guide.md');
  await page.locator('#skill-file-retry').waitFor();failFile=false;await page.locator('#skill-file-retry').click();
  await page.waitForFunction(()=>document.querySelector('.skill-text')?.textContent.includes('Original guide'));
  checks.push('detail and raw-file errors remain recoverable');

  for(const width of [320,390,1440,3440]){
    await page.setViewportSize({width,height:1000});assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'detail overflow '+width);
  }
  await page.setViewportSize({width:1440,height:1000});await page.locator('.skill-original summary').click();
  await page.screenshot({path:path.join(output,'detail-1440.png'),fullPage:false});
  await page.setViewportSize({width:390,height:1000});await page.screenshot({path:path.join(output,'detail-390.png'),fullPage:false});
  await page.locator('#skill-back').click();delayDetail=true;await page.locator('.skill-card').first().click();await page.locator('#skill-back').click();
  await page.waitForTimeout(450);assert.equal(await page.locator('#skill-detail').isVisible(),false);
  assert.equal(await page.locator('#skill-catalog').isVisible(),true);
  delayDetail=false;checks.push('detail responsive; late request cannot reopen detail after Back');

  assert.deepEqual(errors,[]);assert.equal(await page.evaluate(()=>window.skillXSS),undefined);
  checks.push('no JavaScript errors or text execution');
  const result={checks,errors,output};fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify(result,null,2));
 }finally{if(context)await context.close();if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);process.exitCode=1;});
